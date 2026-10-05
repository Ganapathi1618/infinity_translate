import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

WEBHOOK_SECRET = "test_webhook_secret"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("TRANSLATION_PROVIDER", "mock")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret_x")
    monkeypatch.setenv("CREDITS_PER_PAGE", "10")
    monkeypatch.setenv("JOBS_PER_HOUR", "10")
    for k in ("SMTP_HOST",):
        monkeypatch.delenv(k, raising=False)
    from app.config import load_settings
    return load_settings()


@pytest.fixture
def app(settings):
    from app.main import create_app
    return create_app(settings, start_worker=False)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(app):
    with app.state.Session() as s:
        yield s


@pytest.fixture
def user(db):
    from app.db import User
    u = User(email="reader@example.com")
    db.add(u)
    db.commit()
    return u


def login(client, app, user):
    from app import auth
    client.cookies.set(auth.COOKIE_NAME, auth.session_cookie_value(app.state.signer, user.id))
