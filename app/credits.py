"""Credit ledger operations.

Balance is never stored; it is always SUM(amount) over the user's ledger rows.
  reserve(job)  -> hold    -cost
  deduct(job)   -> release +cost and charge -cost   (job succeeded)
  release(job)  -> release +cost                    (job failed)
A unique (job_id, kind) constraint makes each step happen at most once per job,
and a unique payment_id makes purchases idempotent.
"""
from __future__ import annotations

import threading

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import LedgerEntry

# SQLite has no SELECT ... FOR UPDATE; the app runs as a single process, so a
# process-wide lock is enough to make "check balance then hold" atomic.
_reserve_lock = threading.Lock()


class InsufficientCredits(Exception):
    def __init__(self, needed: int, available: int):
        super().__init__(f"needs {needed} credits, {available} available")
        self.needed = needed
        self.available = available


class LedgerError(Exception):
    pass


def balance(session: Session, user_id: int) -> int:
    """Credits available to spend (holds already subtracted)."""
    total = session.scalar(select(func.coalesce(func.sum(LedgerEntry.amount), 0))
                           .where(LedgerEntry.user_id == user_id))
    return int(total or 0)


def reserved(session: Session, user_id: int) -> int:
    """Credits currently held for running jobs."""
    holds = session.scalars(select(LedgerEntry).where(LedgerEntry.user_id == user_id,
                                                      LedgerEntry.kind == "hold")).all()
    open_total = 0
    for h in holds:
        released = session.scalar(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.job_id == h.job_id, LedgerEntry.kind == "release"))
        if not released:
            open_total += -h.amount
    return open_total


def add_purchase(session: Session, user_id: int, credits: int, payment_id: str, note: str = "") -> bool:
    """Add purchased credits once per payment id. Returns False if already applied."""
    if credits <= 0:
        raise LedgerError("credits must be positive")
    session.add(LedgerEntry(user_id=user_id, kind="purchase", amount=credits,
                            payment_id=payment_id, note=note))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return False
    return True


def reserve(session: Session, user_id: int, job_id: str, cost: int) -> None:
    if cost <= 0:
        raise LedgerError("cost must be positive")
    with _reserve_lock:
        available = balance(session, user_id)
        if available < cost:
            raise InsufficientCredits(cost, available)
        session.add(LedgerEntry(user_id=user_id, kind="hold", amount=-cost, job_id=job_id))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise LedgerError(f"credits already reserved for job {job_id}")


def _get_hold(session: Session, job_id: str) -> LedgerEntry:
    hold = session.scalar(select(LedgerEntry).where(LedgerEntry.job_id == job_id,
                                                    LedgerEntry.kind == "hold"))
    if hold is None:
        raise LedgerError(f"no reservation for job {job_id}")
    return hold


def deduct(session: Session, job_id: str) -> None:
    """Job succeeded: turn the hold into a final charge."""
    hold = _get_hold(session, job_id)
    cost = -hold.amount
    session.add(LedgerEntry(user_id=hold.user_id, kind="release", amount=cost, job_id=job_id,
                            note="settled"))
    session.add(LedgerEntry(user_id=hold.user_id, kind="charge", amount=-cost, job_id=job_id))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise LedgerError(f"job {job_id} already settled")


def release(session: Session, job_id: str) -> None:
    """Job failed: give the held credits back."""
    hold = _get_hold(session, job_id)
    session.add(LedgerEntry(user_id=hold.user_id, kind="release", amount=-hold.amount,
                            job_id=job_id, note="job failed"))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise LedgerError(f"job {job_id} already settled")
