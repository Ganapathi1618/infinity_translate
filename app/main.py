"""Infinity Translate web app.

Run with:  uvicorn app.main:app   (single process - the job worker runs in-process)
"""
from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import pymupdf
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired
from pydantic import BaseModel
from sqlalchemy import func, select

from translate_pdf import SUPPORTED_TARGETS, PdfRejected, check_document

from . import auth, credits, payments
from .config import ROOT, Settings, load_settings
from .db import Job, Upload, User, make_engine, make_sessionmaker, utcnow
from .worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("infinity")

STATIC_DIR = ROOT / "static"
LANG_NAMES = {"te": "Telugu", "hi": "Hindi"}


class JobRequest(BaseModel):
    file_id: str
    target: str
    mode: str = "sample"          # "sample" (first pages, free) or "full" (uses credits)


class LoginRequest(BaseModel):
    email: str


def create_app(settings: Settings | None = None, start_worker: bool = True) -> FastAPI:
    settings = settings or load_settings()
    engine = make_engine(settings.database_url)
    Session = make_sessionmaker(engine)
    signer = auth.Signer(settings.secret_key)
    worker = Worker(settings, Session)

    @asynccontextmanager
    async def lifespan(_app):
        if start_worker:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(title="Infinity Translate", lifespan=lifespan)
    app.state.settings = settings
    app.state.Session = Session
    app.state.worker = worker
    app.state.signer = signer
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ------------------------------------------------------------------ helpers
    def get_db():
        with Session() as s:
            yield s

    def current_user(request: Request, db=Depends(get_db)) -> User | None:
        uid = auth.user_id_from_cookie(signer, request.cookies.get(auth.COOKIE_NAME))
        return db.get(User, uid) if uid else None

    def require_user(user: User | None = Depends(current_user)) -> User:
        if user is None:
            raise HTTPException(401, "Please sign in first.")
        return user

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def job_json(job: Job) -> dict:
        expired = job.output_deleted
        return {
            "id": job.id,
            "status": job.status,
            "stage": job.stage,
            "error": job.error,
            "filename": job.filename,
            "target": job.target,
            "mode": "sample" if job.sample else "full",
            "pages": job.pages,
            "cost": job.cost,
            "overflow_count": job.overflow_count,
            "expired": expired,
            "created_at": job.created_at.isoformat() + "Z",
            "finished_at": job.finished_at.isoformat() + "Z" if job.finished_at else None,
        }

    def job_from_token(token: str, db) -> Job:
        try:
            job_id = signer.download.loads(token, max_age=settings.download_url_ttl)
        except SignatureExpired:
            raise HTTPException(410, "This download link has expired.")
        except BadSignature:
            raise HTTPException(404, "Not found")
        job = db.get(Job, job_id)
        if job is None or job.status != "done" or job.output_deleted or not job.output_path \
                or not Path(job.output_path).exists():
            raise HTTPException(404, "The translated file is no longer available.")
        return job

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    def public_config():
        return {
            "credits_per_page": settings.credits_per_page,
            "free_sample_pages": settings.free_sample_pages,
            "max_upload_mb": settings.max_upload_mb,
            "max_pages": settings.max_pages,
            "output_retention_days": settings.output_retention_days,
            "languages": [{"code": c, "name": LANG_NAMES[c]} for c in SUPPORTED_TARGETS],
            "plan": {"name": settings.plan_name, "price_inr": settings.plan_price_inr,
                     "credits": settings.plan_credits},
            "payments_enabled": bool(settings.razorpay_key_id and settings.razorpay_key_secret),
        }

    # ------------------------------------------------------------------ auth
    @app.post("/api/auth/request")
    def request_login(body: LoginRequest, background: BackgroundTasks, db=Depends(get_db)):
        email = auth.normalize_email(body.email)
        if not email:
            raise HTTPException(400, "Please enter a valid email address.")
        link = auth.create_login_link(db, settings, signer, email)
        background.add_task(auth.send_login_email, settings, email, link)
        return {"ok": True, "message": "Check your email for a sign-in link."}

    @app.get("/auth/verify")
    def verify_login(token: str, db=Depends(get_db)):
        user = auth.consume_login_token(db, signer, token)
        if user is None:
            return RedirectResponse("/?login=invalid", status_code=303)
        resp = RedirectResponse("/?login=ok", status_code=303)
        resp.set_cookie(auth.COOKIE_NAME, auth.session_cookie_value(signer, user.id),
                        max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax",
                        secure=settings.secure_cookies, path="/")
        return resp

    @app.post("/api/auth/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(auth.COOKIE_NAME, path="/")
        return resp

    @app.get("/api/me")
    def me(user: User | None = Depends(current_user), db=Depends(get_db)):
        if user is None:
            return {"user": None}
        return {"user": {"email": user.email, "balance": credits.balance(db, user.id),
                         "reserved": credits.reserved(db, user.id)}}

    # ------------------------------------------------------------------ upload
    @app.post("/api/upload")
    async def upload(request: Request, file: UploadFile = File(...),
                     user: User | None = Depends(current_user), db=Depends(get_db)):
        max_bytes = settings.max_upload_mb * 1024 * 1024
        name = Path(file.filename or "document.pdf").name[:200]
        if not name.lower().endswith(".pdf"):
            raise HTTPException(400, "Only PDF files are accepted.")
        file_id = str(uuid.uuid4())
        path = settings.uploads_dir / f"{file_id}.pdf"
        size = 0
        try:
            with open(path, "wb") as fh:
                first = True
                while chunk := await file.read(1024 * 1024):
                    if first and not chunk.startswith(b"%PDF-"):
                        raise HTTPException(400, "This file is not a valid PDF.")
                    first = False
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(413, f"The file is larger than {settings.max_upload_mb} MB.")
                    fh.write(chunk)
            if size == 0:
                raise HTTPException(400, "The file is empty.")
            try:
                doc = pymupdf.open(path)
            except Exception:
                raise HTTPException(400, "This file is not a valid PDF.")
            try:
                if doc.page_count > settings.max_pages:
                    raise HTTPException(400, f"The PDF has {doc.page_count} pages; the limit is "
                                             f"{settings.max_pages}.")
                check_document(doc)
                pages = doc.page_count
            except PdfRejected as e:
                raise HTTPException(400, str(e))
            finally:
                doc.close()
        except HTTPException:
            path.unlink(missing_ok=True)
            raise
        db.add(Upload(id=file_id, user_id=user.id if user else None, filename=name, path=str(path),
                      pages=pages, size_bytes=size))
        db.commit()
        return {
            "file_id": file_id,
            "filename": name,
            "pages": pages,
            "credits_per_page": settings.credits_per_page,
            "credit_estimate": pages * settings.credits_per_page,
            "sample_pages": min(settings.free_sample_pages, pages),
        }

    # ------------------------------------------------------------------ jobs
    @app.post("/api/jobs")
    def create_job(body: JobRequest, request: Request, user: User | None = Depends(current_user),
                   db=Depends(get_db)):
        if body.target not in SUPPORTED_TARGETS:
            raise HTTPException(400, "Target language must be Telugu (te) or Hindi (hi).")
        if body.mode not in ("sample", "full"):
            raise HTTPException(400, "mode must be 'sample' or 'full'.")
        up = db.get(Upload, body.file_id)
        if up is None or up.deleted_at is not None or not Path(up.path).exists():
            raise HTTPException(404, "File not found. Please upload it again.")
        if up.user_id is not None and (user is None or user.id != up.user_id):
            raise HTTPException(404, "File not found. Please upload it again.")
        client_key = f"user:{user.id}" if user else f"ip:{client_ip(request)}"
        recent = db.scalar(select(func.count()).select_from(Job).where(
            Job.client_key == client_key, Job.created_at > utcnow() - timedelta(hours=1)))
        if recent >= settings.jobs_per_hour:
            raise HTTPException(429, f"Limit reached: at most {settings.jobs_per_hour} jobs per hour.")
        sample = body.mode == "sample"
        if not sample and user is None:
            raise HTTPException(401, "Please sign in to translate the full document.")
        pages = min(settings.free_sample_pages, up.pages) if sample else up.pages
        cost = 0 if sample else up.pages * settings.credits_per_page
        job = Job(id=str(uuid.uuid4()), user_id=user.id if user else None, client_key=client_key,
                  upload_id=up.id, filename=up.filename, target=body.target, sample=sample,
                  pages=pages, cost=cost, status="new")
        db.add(job)
        db.commit()
        if cost:
            # Reserve before the job becomes visible to the worker as "queued".
            try:
                credits.reserve(db, user.id, job.id, cost)
            except credits.InsufficientCredits as e:
                db.delete(job)
                db.commit()
                raise HTTPException(402, f"Not enough credits: this document needs {e.needed}, "
                                         f"you have {e.available}.")
        job.status = "queued"
        db.commit()
        worker.notify()
        return job_json(job)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, db=Depends(get_db)):
        job = db.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job_json(job)

    @app.get("/api/jobs/{job_id}/download")
    def download_link(job_id: str, db=Depends(get_db)):
        job = db.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        if job.status != "done":
            raise HTTPException(409, "The translation is not finished.")
        if job.output_deleted or not job.output_path or not Path(job.output_path).exists():
            raise HTTPException(410, "The translated file has expired.")
        token = signer.download.dumps(job.id)
        return {"url": f"/files/{token}", "preview_url": f"/files/{token}/page/",
                "pages": job.pages, "expires_in": settings.download_url_ttl}

    @app.get("/files/{token}")
    def download_file(token: str, db=Depends(get_db)):
        job = job_from_token(token, db)
        stem = re.sub(r"[^\w.-]+", "_", Path(job.filename).stem)[:80] or "document"
        suffix = "_sample" if job.sample else ""
        return FileResponse(job.output_path, media_type="application/pdf",
                            filename=f"{stem}_{job.target}{suffix}.pdf")

    @app.get("/files/{token}/page/{page_no}.png")
    def preview_page(token: str, page_no: int, db=Depends(get_db)):
        job = job_from_token(token, db)
        with pymupdf.open(job.output_path) as doc:
            if not 1 <= page_no <= doc.page_count:
                raise HTTPException(404, "No such page")
            png = doc[page_no - 1].get_pixmap(dpi=110).tobytes("png")
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})

    # ------------------------------------------------------------------ payments
    @app.post("/api/checkout")
    def checkout(user: User = Depends(require_user), db=Depends(get_db)):
        try:
            order = payments.create_order(db, settings, user.id)
        except payments.PaymentError as e:
            raise HTTPException(503, str(e))
        return {"order_id": order.id, "amount": order.amount_paise, "currency": "INR",
                "key_id": settings.razorpay_key_id, "name": "Infinity Translate",
                "description": f"{settings.plan_name}: {order.credits} credits", "email": user.email}

    @app.post("/api/razorpay/webhook")
    async def razorpay_webhook(request: Request, db=Depends(get_db)):
        body = await request.body()
        try:
            result = payments.handle_webhook(db, settings, body,
                                             request.headers.get("X-Razorpay-Signature"))
        except payments.PaymentError:
            raise HTTPException(400, "Invalid signature")
        return result

    return app


_default_app: FastAPI | None = None


def __getattr__(name: str):
    # `uvicorn app.main:app` builds the app on first access, so importing this
    # module (e.g. in tests) has no side effects.
    global _default_app
    if name == "app":
        if _default_app is None:
            _default_app = create_app()
        return _default_app
    raise AttributeError(name)
