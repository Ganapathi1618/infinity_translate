"""Database models (SQLite via SQLAlchemy)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
                        create_engine, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LoginToken(Base):
    """One row per magic link, so each link works only once."""
    __tablename__ = "login_tokens"
    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    filename: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(Text)
    pages: Mapped[int] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    client_key: Mapped[str] = mapped_column(String(100), index=True)   # "user:<id>" or "ip:<addr>"
    upload_id: Mapped[str] = mapped_column(ForeignKey("uploads.id"))
    filename: Mapped[str] = mapped_column(String(255))
    target: Mapped[str] = mapped_column(String(5))
    sample: Mapped[bool] = mapped_column(Boolean, default=False)
    pages: Mapped[int] = mapped_column(Integer)              # pages to translate
    cost: Mapped[int] = mapped_column(Integer, default=0)    # credits
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)  # new/queued/running/done/failed
    stage: Mapped[str] = mapped_column(String(20), default="queued")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    overflow_count: Mapped[int] = mapped_column(Integer, default=0)
    chars_translated: Mapped[int] = mapped_column(Integer, default=0)
    processing_seconds: Mapped[float] = mapped_column(default=0.0)
    output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LedgerEntry(Base):
    """Append-only credit ledger. Balance = SUM(amount).

    kinds: purchase (+), hold (-, reserve for a job), release (+, undo a hold),
           charge (-, final cost of a successful job; written together with a release).
    """
    __tablename__ = "ledger"
    __table_args__ = (UniqueConstraint("job_id", "kind", name="uq_ledger_job_kind"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    amount: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payment_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Order(Base):
    """Razorpay orders we created, so the webhook can map a payment to a user."""
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)    # Razorpay order id
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount_paise: Mapped[int] = mapped_column(Integer)
    credits: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def make_engine(url: str):
    engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=10000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    Base.metadata.create_all(engine)
    return engine


def make_sessionmaker(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
