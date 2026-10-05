import json
import os
import time
import unicodedata
from datetime import timedelta

import httpx

from app.db import Job, Upload, utcnow
from providers import GoogleProvider, MockProvider


def test_google_provider_request_and_nfc():
    seen = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        seen.append((request.url.params["key"], body))
        # decomposed form; the provider must normalise to NFC
        out = [{"translatedText": unicodedata.normalize("NFD", "क़") + t} for t in body["q"]]
        return httpx.Response(200, json={"data": {"translations": out}})

    gp = GoogleProvider(api_key="k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    gp.MAX_SEGMENTS = 2
    res = gp.translate_batch(["a", "b", "c"], "en", "hi")
    assert len(seen) == 2                                   # batched
    assert seen[0][0] == "k" and seen[0][1]["target"] == "hi" and seen[0][1]["format"] == "text"
    assert all(unicodedata.is_normalized("NFC", r) for r in res)
    assert res[2].endswith("c")


def test_mock_provider_lengths():
    out = MockProvider().translate_batch(["Hello world, this is 2024 text."], "en", "te")
    assert "2024" in out[0] and len(out[0]) >= 10


def test_cleanup_removes_old_outputs(app, db, settings):
    out = settings.outputs_dir / "old.pdf"
    out.write_bytes(b"%PDF-1.4")
    up_path = settings.uploads_dir / "u.pdf"
    up_path.write_bytes(b"%PDF-1.4")
    old = utcnow() - timedelta(days=8)
    db.add(Upload(id="u", filename="a.pdf", path=str(up_path), pages=1, size_bytes=8, created_at=old))
    db.flush()
    db.add(Job(id="j", client_key="ip:x", upload_id="u", filename="a.pdf", target="te", pages=1,
               status="done", stage="done", output_path=str(out), finished_at=old, created_at=old))
    db.commit()
    removed = app.state.worker.cleanup()
    assert removed == {"outputs": 1, "uploads": 1}
    assert not out.exists() and not up_path.exists()
    db.expire_all()
    assert db.get(Job, "j").output_deleted is True


def test_cleanup_removes_orphan_files_by_age(app, settings):
    f = settings.uploads_dir / "orphan.pdf"
    f.write_bytes(b"x")
    past = time.time() - 2 * 86400
    os.utime(f, (past, past))
    app.state.worker.cleanup()
    assert not f.exists()
