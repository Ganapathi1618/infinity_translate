"""Settings, read from the environment (.env is loaded if present)."""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("infinity")

ROOT = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass
class Settings:
    base_url: str
    secret_key: str
    data_dir: Path
    database_url: str
    translation_provider: str
    credits_per_page: int
    free_sample_pages: int
    max_upload_mb: int
    max_pages: int
    jobs_per_hour: int
    download_url_ttl: int
    output_retention_days: int
    upload_retention_hours: int
    plan_name: str
    plan_price_inr: int
    plan_credits: int
    razorpay_key_id: str
    razorpay_key_secret: str
    razorpay_webhook_secret: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_starttls: bool

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR", ROOT / "data")).resolve()
    secret = os.environ.get("SECRET_KEY", "")
    if not secret:
        secret = secrets.token_urlsafe(32)
        log.warning("SECRET_KEY is not set; using a random key (sessions and links reset on restart)")
    s = Settings(
        base_url=os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/"),
        secret_key=secret,
        data_dir=data_dir,
        database_url=os.environ.get("DATABASE_URL", f"sqlite:///{data_dir / 'app.db'}"),
        translation_provider=os.environ.get("TRANSLATION_PROVIDER", "mock"),
        credits_per_page=_int("CREDITS_PER_PAGE", 10),
        free_sample_pages=_int("FREE_SAMPLE_PAGES", 2),
        max_upload_mb=_int("MAX_UPLOAD_MB", 25),
        max_pages=_int("MAX_PAGES", 50),
        jobs_per_hour=_int("JOBS_PER_HOUR", 10),
        download_url_ttl=_int("DOWNLOAD_URL_TTL_SECONDS", 3600),
        output_retention_days=_int("OUTPUT_RETENTION_DAYS", 7),
        upload_retention_hours=_int("UPLOAD_RETENTION_HOURS", 24),
        plan_name=os.environ.get("PLAN_NAME", "Basic Plan"),
        plan_price_inr=_int("PLAN_PRICE_INR", 649),
        plan_credits=_int("PLAN_CREDITS", 1000),
        razorpay_key_id=os.environ.get("RAZORPAY_KEY_ID", ""),
        razorpay_key_secret=os.environ.get("RAZORPAY_KEY_SECRET", ""),
        razorpay_webhook_secret=os.environ.get("RAZORPAY_WEBHOOK_SECRET", ""),
        smtp_host=os.environ.get("SMTP_HOST", ""),
        smtp_port=_int("SMTP_PORT", 587),
        smtp_user=os.environ.get("SMTP_USER", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        smtp_from=os.environ.get("SMTP_FROM", "Infinity Translate <no-reply@localhost>"),
        smtp_starttls=os.environ.get("SMTP_STARTTLS", "true").lower() in ("1", "true", "yes"),
    )
    s.uploads_dir.mkdir(parents=True, exist_ok=True)
    s.outputs_dir.mkdir(parents=True, exist_ok=True)
    return s
