import time
from pathlib import Path

import pymupdf
import pytest

from app import config
from app.main import store
from conftest import make_pdf, upload


def test_upload_describes_pages(client):
    r = upload(client, make_pdf(pages=3))
    assert r.status_code == 201
    body = r.json()
    assert body["pageCount"] == 3
    assert body["pages"][0] == {"width": 595, "height": 842}
    assert body["revision"] == 0
    assert body["edited"] is False
    assert body["isSigned"] is False
    assert len(body["id"]) == 22


def test_rotated_page_reports_displayed_size(client):
    body = upload(client, make_pdf(rotate=90)).json()
    assert body["pages"][0] == {"width": 842, "height": 595}


def test_cropped_page_reports_crop_box_size(client):
    body = upload(client, make_pdf(cropbox=(50, 50, 450, 650))).json()
    assert body["pages"][0] == {"width": 400, "height": 600}


def test_rejects_non_pdf(client):
    r = upload(client, b"hello, not a pdf", name="notes.txt")
    assert r.status_code == 400
    assert "isn't a PDF" in r.json()["detail"]


def test_rejects_damaged_pdf(client):
    r = upload(client, b"%PDF-1.7\n garbage garbage")
    assert r.status_code == 400


def test_rejects_password_protected(client):
    r = upload(client, make_pdf(password="secret"))
    assert r.status_code == 400
    assert "password" in r.json()["detail"]


def test_rejects_too_many_pages(client):
    r = upload(client, make_pdf(pages=config.MAX_PAGES + 1))
    assert r.status_code == 400
    assert "limit" in r.json()["detail"]


def test_rejects_oversized_upload(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1000)
    r = upload(client, make_pdf() + b"\n" * 2000)
    assert r.status_code == 413


def test_page_image_is_png_at_requested_width(client):
    doc_id = upload(client, make_pdf(pages=2)).json()["id"]
    r = client.get(f"/api/documents/{doc_id}/pages/1/image", params={"width": 800})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    pix = pymupdf.Pixmap(r.content)
    assert pix.width == 800
    assert abs(pix.height - round(800 * 842 / 595)) <= 1


def test_page_image_of_rotated_page_is_landscape(client):
    doc_id = upload(client, make_pdf(rotate=90)).json()["id"]
    pix = pymupdf.Pixmap(client.get(f"/api/documents/{doc_id}/pages/0/image", params={"width": 842}).content)
    assert pix.width > pix.height


def test_page_image_missing_page(client):
    doc_id = upload(client, make_pdf()).json()["id"]
    assert client.get(f"/api/documents/{doc_id}/pages/7/image").status_code == 404


def test_download_returns_original_bytes(client):
    data = make_pdf(pages=2)
    doc_id = upload(client, data, name="Quotation.pdf").json()["id"]
    r = client.get(f"/api/documents/{doc_id}/download")
    assert r.status_code == 200
    assert r.content == data
    assert 'filename="Quotation.pdf"' in r.headers["content-disposition"]


def test_download_keeps_arabic_filename(client):
    doc_id = upload(client, make_pdf(), name="عرض سعر.pdf").json()["id"]
    r = client.get(f"/api/documents/{doc_id}/download")
    assert "filename*=utf-8''%D8%B9%D8%B1%D8%B6" in r.headers["content-disposition"]


def test_filename_path_parts_are_stripped(client):
    body = upload(client, make_pdf(), name="..\\..\\evil/name").json()
    assert body["name"] == "name.pdf"


def test_delete_removes_document(client):
    doc_id = upload(client, make_pdf()).json()["id"]
    assert client.delete(f"/api/documents/{doc_id}").status_code == 204
    assert client.get(f"/api/documents/{doc_id}").status_code == 404
    assert client.get(f"/api/documents/{doc_id}/download").status_code == 404


def test_unknown_and_malformed_ids_are_404(client):
    assert client.get("/api/documents/AAAAAAAAAAAAAAAAAAAAAA").status_code == 404
    assert client.get("/api/documents/..%2F..%2Fetc").status_code == 404
    assert client.get("/api/documents/short").status_code == 404


def test_cleanup_removes_only_expired(client):
    old = upload(client, make_pdf()).json()["id"]
    fresh = upload(client, make_pdf()).json()["id"]
    meta = store.get(old)
    meta.last_access = time.time() - config.DOC_TTL_SECONDS - 10
    store._write_meta(meta)
    store.cleanup_expired()
    assert client.get(f"/api/documents/{old}").status_code == 404
    assert client.get(f"/api/documents/{fresh}").status_code == 200


SAMPLES = sorted((Path(__file__).resolve().parents[2] / "samples").glob("*.pdf"))


@pytest.mark.parametrize("path", SAMPLES or [None], ids=lambda p: p.name if p else "none")
def test_private_samples_upload(client, monkeypatch, path):
    # Real-world PDFs from samples/, which is never committed.
    if path is None:
        pytest.skip("no private samples")
    monkeypatch.setattr(config, "MAX_PAGES", 1000)
    data = path.read_bytes()
    r = upload(client, data, name=path.name)
    assert r.status_code == 201, r.text
    with pymupdf.open("pdf", data) as doc:
        assert r.json()["pageCount"] == doc.page_count


def test_page_lines_endpoint(client):
    fixture = Path(__file__).parent / "fixtures" / "word" / "arabic-quotation-word.pdf"
    doc_id = upload(client, fixture.read_bytes(), name="quote.pdf").json()["id"]
    r = client.get(f"/api/documents/{doc_id}/pages/0/lines")
    assert r.status_code == 200
    body = r.json()
    assert (round(body["width"]), round(body["height"])) == (595, 842)
    title = next(l for l in body["lines"] if l["text"] == "عرض سعر خدمات الرسائل والمحادثات")
    assert title["dir"] == "rtl" and title["bold"] and title["color"] == "1f4e79"
    assert {"id", "display", "bbox", "font", "size", "italic", "spans", "uncertain"} <= title.keys()
    assert len({l["id"] for l in body["lines"]}) == len(body["lines"])
    assert client.get(f"/api/documents/{doc_id}/pages/9/lines").status_code == 404
