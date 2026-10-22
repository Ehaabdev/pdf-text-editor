"""The list of changes, undo and redo, going back to the original, and page images of
any version: what the before/after view is built on."""

from pathlib import Path

import numpy as np
import pymupdf
import pytest

from app.fonts import library
from conftest import upload

FIXTURE = Path(__file__).parent / "fixtures" / "word" / "english-styles-word.pdf"
needs_fonts = pytest.mark.skipif(library().find("Calibri") is None, reason="Calibri isn't installed")


def _line(client, doc, needle, page=0):
    lines = client.get(f"/api/documents/{doc['id']}/pages/{page}/lines").json()["lines"]
    return next(l for l in lines if needle in l["text"])


def _chain(client):
    """Upload, then edit, delete and add on page 1: three changes."""
    doc = upload(client, FIXTURE.read_bytes(), name="report.pdf").json()
    line = _line(client, doc, "Quarterly Service Report")
    doc = client.post(f"/api/documents/{doc['id']}/edits", json={
        "revision": doc["revision"], "page": 0, "lineId": line["id"], "oldText": line["text"],
        "newText": "Annual Service Report"}).json()["document"]
    line = _line(client, doc, "Report ID")
    doc = client.post(f"/api/documents/{doc['id']}/deletions", json={
        "revision": doc["revision"], "lines": [{"page": 0, "lineId": line["id"], "oldText": line["text"]}]}).json()["document"]
    line = _line(client, doc, "Figures are rounded")
    doc = client.post(f"/api/documents/{doc['id']}/additions", json={
        "revision": doc["revision"], "page": 0, "lineId": line["id"], "oldText": line["text"],
        "text": "Source: the support team's own records."}).json()["document"]
    return doc


def _post(client, doc, what, version=None):
    url = f"/api/documents/{doc['id']}/" + (what if version is None else f"versions/{version}")
    r = client.post(url, json={"revision": doc["revision"]})
    assert r.status_code == 200, r.text
    return r.json()


@needs_fonts
def test_the_list_of_changes(client):
    doc = _chain(client)
    assert [h["kind"] for h in doc["history"]] == ["edit", "delete", "add"]
    plain = lambda h: h["summary"].replace("\u2068", "").replace("\u2069", "")  # direction isolates
    assert plain(doc["history"][0]) == "Changed “Quarterly Service Report” to “Annual Service Report”"
    assert plain(doc["history"][1]).startswith("Deleted “Report ID")
    assert plain(doc["history"][2]) == "Added “Source: the support team's own records.”"
    assert all(h["applied"] and h["pages"] == [0] and h["areas"] for h in doc["history"])
    assert doc["version"] == 3 and doc["canUndo"] and not doc["canRedo"]


@needs_fonts
def test_undo_then_redo(client):
    doc = _chain(client)
    doc = _post(client, doc, "undo")
    assert [h["applied"] for h in doc["history"]] == [True, True, False]  # undone, still listed
    assert doc["canRedo"] and not any("Source:" in l["text"] for l in client.get(f"/api/documents/{doc['id']}/pages/0/lines").json()["lines"])
    doc = _post(client, doc, "redo")
    assert all(h["applied"] for h in doc["history"]) and not doc["canRedo"]
    assert _line(client, doc, "Source: the support team")


@needs_fonts
def test_a_new_change_after_undo_drops_the_undone_one(client):
    doc = _chain(client)
    doc = _post(client, doc, "undo")
    doc = _post(client, doc, "undo")
    line = _line(client, doc, "Figures are rounded")
    doc = client.post(f"/api/documents/{doc['id']}/edits", json={
        "revision": doc["revision"], "page": 0, "lineId": line["id"], "oldText": line["text"],
        "newText": "Figures are rounded to whole numbers."}).json()["document"]
    assert [h["kind"] for h in doc["history"]] == ["edit", "edit"] and not doc["canRedo"]
    assert client.post(f"/api/documents/{doc['id']}/redo", json={"revision": doc["revision"]}).status_code == 409


@needs_fonts
def test_start_over_and_come_back(client):
    doc = _chain(client)
    doc = _post(client, doc, None, version=0)
    assert not doc["edited"] and doc["canRedo"] and not any(h["applied"] for h in doc["history"])
    assert _line(client, doc, "Quarterly Service Report")
    doc = _post(client, doc, None, version=3)
    assert doc["version"] == 3 and _line(client, doc, "Annual Service Report")
    assert client.post(f"/api/documents/{doc['id']}/versions/9", json={"revision": doc["revision"]}).status_code == 404
    assert client.post(f"/api/documents/{doc['id']}/versions/0", json={"revision": 0}).status_code == 409


def _image(client, doc, **params) -> np.ndarray:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    r = client.get(f"/api/documents/{doc['id']}/pages/0/image?{query}")
    assert r.status_code == 200, r.text
    pix = pymupdf.Pixmap(r.content)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)


@needs_fonts
def test_changed_pixels_lie_inside_the_recorded_areas(client):
    """Independently of each change's own check: the original and the current page differ
    only inside the areas the applied changes recorded (with an undo and a redo between)."""
    doc = _chain(client)
    doc = _post(client, _post(client, doc, "undo"), "redo")
    width = 1190
    before, after = _image(client, doc, width=width, version=0), _image(client, doc, width=width)
    scale = width / doc["pages"][0]["width"]
    inside = np.zeros(before.shape[:2], bool)
    for h in doc["history"]:
        for page, x0, y0, x1, y1 in h["areas"]:
            if h["applied"] and page == 0:
                inside[max(0, int(y0 * scale) - 1) : int(y1 * scale) + 2, max(0, int(x0 * scale) - 1) : int(x1 * scale) + 2] = True
    changed = (before != after).any(axis=2)
    assert changed.sum() > 100  # the changes show
    assert not (changed & ~inside).any(), f"{int((changed & ~inside).sum())} pixels changed outside the recorded areas"


@needs_fonts
def test_images_of_any_version_and_part_of_a_page(client):
    doc = _chain(client)
    original = _image(client, doc, width=600, version=0)
    assert original.shape[1] == 600
    crop = _image(client, doc, width=400, version=1, clip="60,60,300,120")
    assert crop.shape[1] == 400 and crop.shape[0] < 150
    assert client.get(f"/api/documents/{doc['id']}/pages/0/image?version=9").status_code == 404
    assert client.get(f"/api/documents/{doc['id']}/pages/0/image?clip=1,2,3").status_code == 400
