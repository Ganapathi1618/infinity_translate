import pymupdf
import pytest

from sample_pdfs import make_encrypted, make_sample, make_scanned
from translate_pdf import PdfRejected, check_document, translate_document


def test_scanned_pdf_rejected(tmp_path):
    path = make_scanned(tmp_path / "scan.pdf")
    with pymupdf.open(path) as doc:
        with pytest.raises(PdfRejected, match="scanned"):
            check_document(doc)
    with pytest.raises(PdfRejected, match="scanned"):
        translate_document(path, tmp_path / "out.pdf", "te")
    assert not (tmp_path / "out.pdf").exists()


def test_encrypted_pdf_rejected(tmp_path):
    path = make_encrypted(tmp_path / "enc.pdf")
    with pytest.raises(PdfRejected, match="encrypted"):
        translate_document(path, tmp_path / "out.pdf", "hi")


def test_text_pdf_accepted(tmp_path):
    path = make_sample(tmp_path / "sample.pdf")
    with pymupdf.open(path) as doc:
        check_document(doc)


def test_upload_endpoint_rejects_scanned(client, tmp_path, settings):
    path = make_scanned(tmp_path / "scan.pdf")
    r = client.post("/api/upload", files={"file": ("scan.pdf", path.read_bytes(), "application/pdf")})
    assert r.status_code == 400
    assert "scanned" in r.json()["detail"]
    assert list(settings.uploads_dir.glob("*.pdf")) == []      # rejected file is not kept


def test_upload_endpoint_rejects_non_pdf(client):
    r = client.post("/api/upload", files={"file": ("a.pdf", b"hello world", "application/pdf")})
    assert r.status_code == 400
