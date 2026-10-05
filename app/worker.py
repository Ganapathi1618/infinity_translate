"""In-process background worker (one thread, DB-backed job table) and file cleanup."""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select, update

from translate_pdf import PdfRejected, translate_document
from providers import get_provider

from . import credits
from .config import Settings
from .db import Job, LedgerEntry, Upload, utcnow

log = logging.getLogger("infinity.worker")


class Worker:
    def __init__(self, settings: Settings, sessionmaker):
        self.settings = settings
        self.Session = sessionmaker
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.recover()
        self.cleanup()
        for target, name in ((self._run, "job-worker"), (self._cleanup_loop, "cleanup")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self) -> None:
        self.stop_event.set()
        self.wake.set()

    def notify(self) -> None:
        self.wake.set()

    def recover(self) -> None:
        """Jobs left 'running' by a crash/restart are failed and their credits released."""
        with self.Session() as s:
            for job in s.scalars(select(Job).where(Job.status.in_(("new", "running")))).all():
                self._fail(s, job, "The server restarted while this job was running. Please try again.")

    # ------------------------------------------------------------------ jobs
    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                worked = self.run_once()
            except Exception:
                log.exception("worker loop error")
                worked = False
            if not worked:
                self.wake.wait(timeout=2)
                self.wake.clear()

    def run_once(self) -> bool:
        """Claim and process the oldest queued job. Returns False if there was none."""
        with self.Session() as s:
            job = s.scalar(select(Job).where(Job.status == "queued").order_by(Job.created_at).limit(1))
            if job is None:
                return False
            claimed = s.execute(update(Job).where(Job.id == job.id, Job.status == "queued")
                                .values(status="running", stage="extracting", started_at=utcnow()))
            s.commit()
            if claimed.rowcount != 1:
                return True
            self._process(s, s.get(Job, job.id))
        return True

    def _set_stage(self, job_id: str, stage: str) -> None:
        with self.Session() as s:
            s.execute(update(Job).where(Job.id == job_id).values(stage=stage))
            s.commit()

    def _process(self, s, job: Job) -> None:
        upload = s.get(Upload, job.upload_id)
        out_path = self.settings.outputs_dir / f"{job.id}.pdf"
        t0 = time.monotonic()
        try:
            if upload is None or upload.deleted_at is not None or not Path(upload.path).exists():
                raise PdfRejected("The uploaded file is no longer available. Please upload it again.")
            report = translate_document(
                upload.path, out_path, job.target,
                max_pages=self.settings.free_sample_pages if job.sample else None,
                provider=get_provider(self.settings.translation_provider),
                on_stage=lambda st: self._set_stage(job.id, st),
            )
        except PdfRejected as e:
            self._fail(s, job, str(e), seconds=time.monotonic() - t0)
        except Exception as e:
            log.exception("job %s failed", job.id)
            self._fail(s, job, f"Translation failed: {type(e).__name__}", seconds=time.monotonic() - t0)
        else:
            job.status = "done"
            job.stage = "done"
            job.output_path = str(out_path)
            job.overflow_count = report.overflow_total
            job.chars_translated = report.characters_sent
            job.pages = report.pages_processed
            job.processing_seconds = round(time.monotonic() - t0, 2)
            job.finished_at = utcnow()
            s.commit()
            if job.cost > 0:
                credits.deduct(s, job.id)
            log.info("job %s done: pages=%d chars=%d seconds=%.2f overflow=%d target=%s sample=%s",
                     job.id, job.pages, job.chars_translated, job.processing_seconds,
                     job.overflow_count, job.target, job.sample)
        finally:
            self._maybe_delete_upload(s, job)

    def _fail(self, s, job: Job, message: str, seconds: float = 0.0) -> None:
        had_hold = job.cost > 0 and self._has_hold(s, job)
        job.status = "failed"
        job.stage = "failed"
        job.error = message
        job.processing_seconds = round(seconds, 2)
        job.finished_at = utcnow()
        s.commit()
        if had_hold:
            try:
                credits.release(s, job.id)
            except credits.LedgerError:
                log.exception("could not release credits for job %s", job.id)
        log.info("job %s failed: pages=%d seconds=%.2f error=%s", job.id, job.pages, seconds, message)

    @staticmethod
    def _has_hold(s, job: Job) -> bool:
        return s.scalar(select(LedgerEntry.id).where(LedgerEntry.job_id == job.id,
                                                     LedgerEntry.kind == "hold")) is not None

    def _maybe_delete_upload(self, s, job: Job) -> None:
        """Delete the upload after a full translation. After a free sample it is kept
        (for up to UPLOAD_RETENTION_HOURS) so the user can buy the full document."""
        if job.sample:
            return
        upload = s.get(Upload, job.upload_id)
        if upload is None or upload.deleted_at is not None:
            return
        pending = s.scalar(select(Job.id).where(Job.upload_id == upload.id,
                                                Job.status.in_(("queued", "running"))).limit(1))
        if pending:
            return
        Path(upload.path).unlink(missing_ok=True)
        upload.deleted_at = utcnow()
        s.commit()

    # ------------------------------------------------------------------ cleanup
    def _cleanup_loop(self) -> None:
        while not self.stop_event.wait(timeout=3600):
            try:
                self.cleanup()
            except Exception:
                log.exception("cleanup failed")

    def cleanup(self) -> dict:
        """Delete outputs older than the retention period and stale uploads."""
        now = utcnow()
        out_cutoff = now - timedelta(days=self.settings.output_retention_days)
        up_cutoff = now - timedelta(hours=self.settings.upload_retention_hours)
        removed = {"outputs": 0, "uploads": 0}
        with self.Session() as s:
            for job in s.scalars(select(Job).where(Job.status == "done", Job.output_deleted.is_(False),
                                                   Job.finished_at < out_cutoff)).all():
                if job.output_path:
                    Path(job.output_path).unlink(missing_ok=True)
                job.output_deleted = True
                removed["outputs"] += 1
            for up in s.scalars(select(Upload).where(Upload.deleted_at.is_(None),
                                                     Upload.created_at < up_cutoff)).all():
                busy = s.scalar(select(Job.id).where(Job.upload_id == up.id,
                                                     Job.status.in_(("queued", "running"))).limit(1))
                if busy:
                    continue
                Path(up.path).unlink(missing_ok=True)
                up.deleted_at = now
                removed["uploads"] += 1
            s.commit()
        # Files on disk without a live DB row (e.g. rejected uploads), by file age.
        ages = ((self.settings.uploads_dir, self.settings.upload_retention_hours * 3600),
                (self.settings.outputs_dir, self.settings.output_retention_days * 86400))
        for folder, max_age in ages:
            for f in folder.glob("*.pdf"):
                try:
                    if f.stat().st_mtime < time.time() - max_age:
                        f.unlink(missing_ok=True)
                except FileNotFoundError:
                    pass
        if removed["outputs"] or removed["uploads"]:
            log.info("cleanup removed %s", removed)
        return removed
