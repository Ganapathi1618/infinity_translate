"""End-to-end flow through the API with the mock translator (worker run inline)."""
import pymupdf

from app import credits
from conftest import login
from sample_pdfs import make_sample


def _upload(client, tmp_path):
    path = make_sample(tmp_path / "sample.pdf")
    r = client.post("/api/upload", files={"file": ("report.pdf", path.read_bytes(), "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()


def test_free_sample_flow(client, app, tmp_path):
    up = _upload(client, tmp_path)
    assert up["pages"] == 3 and up["credit_estimate"] == 30 and up["sample_pages"] == 2
    job = client.post("/api/jobs", json={"file_id": up["file_id"], "target": "te", "mode": "sample"}).json()
    assert job["status"] == "queued" and job["cost"] == 0
    assert app.state.worker.run_once()
    job = client.get(f"/api/jobs/{job['id']}").json()
    assert job["status"] == "done" and job["stage"] == "done", job
    link = client.get(f"/api/jobs/{job['id']}/download").json()
    pdf = client.get(link["url"])
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    with pymupdf.open(stream=pdf.content, filetype="pdf") as doc:
        assert doc.page_count == 2
    png = client.get(f"{link['preview_url']}1.png")
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    assert client.get("/files/bogus-token").status_code == 404


def test_full_job_requires_login_and_credits(client, app, db, user, tmp_path):
    up = _upload(client, tmp_path)
    r = client.post("/api/jobs", json={"file_id": up["file_id"], "target": "hi", "mode": "full"})
    assert r.status_code == 401
    login(client, app, user)
    r = client.post("/api/jobs", json={"file_id": up["file_id"], "target": "hi", "mode": "full"})
    assert r.status_code == 402
    credits.add_purchase(db, user.id, 1000, "pay_full")
    r = client.post("/api/jobs", json={"file_id": up["file_id"], "target": "hi", "mode": "full"})
    assert r.status_code == 200
    assert client.get("/api/me").json()["user"]["balance"] == 970
    assert app.state.worker.run_once()
    job = client.get(f"/api/jobs/{r.json()['id']}").json()
    assert job["status"] == "done"
    me = client.get("/api/me").json()["user"]
    assert me["balance"] == 970 and me["reserved"] == 0
    # the upload is deleted after a full translation
    assert client.post("/api/jobs", json={"file_id": up["file_id"], "target": "hi",
                                          "mode": "sample"}).status_code == 404


def test_failed_job_releases_credits(client, app, db, user, tmp_path, monkeypatch):
    login(client, app, user)
    credits.add_purchase(db, user.id, 100, "pay_fail")
    up = _upload(client, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("translator down")
    monkeypatch.setattr("app.worker.translate_document", boom)
    r = client.post("/api/jobs", json={"file_id": up["file_id"], "target": "te", "mode": "full"})
    assert client.get("/api/me").json()["user"]["balance"] == 70
    app.state.worker.run_once()
    job = client.get(f"/api/jobs/{r.json()['id']}").json()
    assert job["status"] == "failed" and job["stage"] == "failed"
    assert client.get("/api/me").json()["user"]["balance"] == 100


def test_rate_limit(client, app, user, tmp_path, settings):
    login(client, app, user)
    up = _upload(client, tmp_path)
    for _ in range(settings.jobs_per_hour):
        assert client.post("/api/jobs", json={"file_id": up["file_id"], "target": "te",
                                              "mode": "sample"}).status_code == 200
    r = client.post("/api/jobs", json={"file_id": up["file_id"], "target": "te", "mode": "sample"})
    assert r.status_code == 429


def test_magic_link_login(client, app, capsys):
    r = client.post("/api/auth/request", json={"email": "New.User@Example.com"})
    assert r.status_code == 200
    out = capsys.readouterr().out
    link = [ln for ln in out.splitlines() if "/auth/verify?token=" in ln][0].strip()
    path = link[link.index("/auth/verify"):]
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?login=ok"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert client.get("/api/me").json()["user"]["email"] == "new.user@example.com"
    # links work once
    client.cookies.clear()
    r = client.get(path, follow_redirects=False)
    assert r.headers["location"] == "/?login=invalid"


def test_restart_recovery_releases_credits(client, app, db, user, tmp_path):
    login(client, app, user)
    credits.add_purchase(db, user.id, 100, "pay_rec")
    up = _upload(client, tmp_path)
    r = client.post("/api/jobs", json={"file_id": up["file_id"], "target": "te", "mode": "full"})
    from app.db import Job
    job = db.get(Job, r.json()["id"])
    job.status = "running"            # simulate a crash mid-job
    db.commit()
    app.state.worker.recover()
    assert client.get(f"/api/jobs/{job.id}").json()["status"] == "failed"
    assert credits.balance(db, user.id) == 100
