"""Passwordless login: magic links and a signed, HTTP-only session cookie."""
from __future__ import annotations

import logging
import re
import secrets
import smtplib
from email.message import EmailMessage

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .db import LoginToken, User, utcnow

log = logging.getLogger("infinity.auth")

COOKIE_NAME = "it_session"
SESSION_MAX_AGE = 30 * 24 * 3600
LINK_MAX_AGE = 20 * 60
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def normalize_email(email: str) -> str | None:
    email = (email or "").strip().lower()
    return email if len(email) <= 320 and EMAIL_RE.match(email) else None


class Signer:
    def __init__(self, secret: str):
        self.login = URLSafeTimedSerializer(secret, salt="login-link")
        self.session = URLSafeTimedSerializer(secret, salt="session")
        self.download = URLSafeTimedSerializer(secret, salt="download")


def create_login_link(session: Session, settings: Settings, signer: Signer, email: str) -> str:
    nonce = secrets.token_urlsafe(24)
    session.add(LoginToken(nonce=nonce, email=email))
    session.commit()
    token = signer.login.dumps({"e": email, "n": nonce})
    return f"{settings.base_url}/auth/verify?token={token}"


def consume_login_token(session: Session, signer: Signer, token: str) -> User | None:
    """Validate a magic-link token (signature, age, single use) and return the user."""
    try:
        data = signer.login.loads(token, max_age=LINK_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    row = session.get(LoginToken, data.get("n", ""))
    if row is None or row.used_at is not None or row.email != data.get("e"):
        return None
    row.used_at = utcnow()
    user = session.scalar(select(User).where(User.email == row.email))
    if user is None:
        user = User(email=row.email)
        session.add(user)
    session.commit()
    return user


def session_cookie_value(signer: Signer, user_id: int) -> str:
    return signer.session.dumps(user_id)


def user_id_from_cookie(signer: Signer, value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(signer.session.loads(value, max_age=SESSION_MAX_AGE))
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None


def send_login_email(settings: Settings, email: str, link: str) -> None:
    if not settings.smtp_host:
        log.warning("SMTP not configured. Magic link for %s: %s", email, link)
        print(f"\n*** Magic login link for {email}:\n{link}\n", flush=True)
        return
    msg = EmailMessage()
    msg["Subject"] = "Your Infinity Translate sign-in link"
    msg["From"] = settings.smtp_from
    msg["To"] = email
    msg.set_content(
        f"Click the link below to sign in to Infinity Translate:\n\n{link}\n\n"
        f"The link expires in {LINK_MAX_AGE // 60} minutes and works once.\n"
        "If you did not request it, ignore this email."
    )
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            if settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
    except Exception:
        log.exception("Failed to send login email to %s", email)
