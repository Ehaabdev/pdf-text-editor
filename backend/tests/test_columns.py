"""Table columns: one deleted, the table narrower, the columns right of it moved left,
nothing else changing (columns.delete_column checks it, pixel for pixel)."""

from pathlib import Path

import pymupdf
import pytest

from app import columns, limits
from app.editing import EditRejected, comparable
from app.tables import find_tables
from app.textlayout import page_lines
from conftest import upload

FIXTURES = Path(__file__).parent / "fixtures"


def table_of(src: bytes, page_no: int = 0):
    page = pymupdf.open("pdf", src)[page_no]
    return next(t for t in find_tables(page, page_lines(page)) if t.ruled)


def grid(table) -> list[list[str]]:
    rows = len(table.rows) - 1
    cols = len(table.cols) - 1
    out = [["" for _ in range(cols)] for _ in range(rows)]
    for c in table.cells:
        out[c.row][c.col] = comparable(" ".join(l.text for l in c.lines))
    return out


@pytest.mark.parametrize(
    "name",
    ["word/french-quote-word.pdf", "word/turkish-quote-word.pdf", "word/arabic-quotation-word.pdf", "word/english-styles-word.pdf", "word/bilingual-contract-word.pdf", "decorations/decorations-chrome.pdf"],
)
def test_each_column_can_be_deleted(name):
    src = (FIXTURES / name).read_bytes()
    table = table_of(src)
    before = grid(table)
    for col in range(len(table.cols) - 1):
        line = next(l for c in table.cells if c.col == col for l in c.lines)
        result = columns.delete_column(src, 0, line.id, line.text)
        after = table_of(result.pdf)
        assert grid(after) == [row[:col] + row[col + 1:] for row in before]
        # Narrower by the column's width; the rows as they were.
        width = table.cols[col + 1] - table.cols[col]
        assert after.bbox[2] - after.bbox[0] == pytest.approx(table.bbox[2] - table.bbox[0] - width, abs=0.6)
        assert after.rows == pytest.approx(table.rows, abs=0.6)


def test_pictures_go_with_their_column():
    src = (FIXTURES / "images" / "images-word.pdf").read_bytes()
    table = table_of(src)
    line = next(l for c in table.cells if c.col == 0 for l in c.lines)
    result = columns.delete_column(src, 0, line.id, line.text)
    page = pymupdf.open("pdf", result.pdf)[0]
    before = [im["bbox"] for im in pymupdf.open("pdf", src)[0].get_image_info()]
    after = [im["bbox"] for im in page.get_image_info()]
    width = table.cols[1] - table.cols[0]
    inside = [b for b in before if table.bbox[0] < b[0] < table.bbox[2] and table.bbox[1] < b[1] < table.bbox[3]]
    assert inside and all(any(abs(a[0] - (b[0] - width)) < 0.1 and abs(a[1] - b[1]) < 0.1 for a in after) for b in inside)


def test_refused_with_a_reason():
    # A table's only column.
    contract = (FIXTURES / "word" / "bilingual-contract-word.pdf").read_bytes()
    page = pymupdf.open("pdf", contract)[1]
    one = [t for t in find_tables(page, page_lines(page)) if t.ruled and len(t.cols) == 2]
    if one:
        line = one[0].cells[0].lines[0]
        with pytest.raises(EditRejected, match="only column"):
            columns.delete_column(contract, 1, line.id, line.text)
    # Not a table.
    src = (FIXTURES / "word" / "french-quote-word.pdf").read_bytes()
    plain = next(l for l in page_lines(pymupdf.open("pdf", src)[0]) if l.text.startswith("Madame"))
    with pytest.raises(EditRejected, match="isn't in a table"):
        columns.delete_column(src, 0, plain.id, plain.text)


def test_through_the_website(client):
    src = (FIXTURES / "word" / "turkish-quote-word.pdf").read_bytes()
    doc = upload(client, src, "teklif.pdf").json()
    base = f"/api/documents/{doc['id']}"
    lines = client.get(f"{base}/pages/0/lines").json()["lines"]
    line = next(l for l in lines if l["text"] == "Miktar")
    body = {"revision": doc["revision"], "page": 0, "lineId": line["id"], "oldText": line["text"]}
    done = client.post(f"{base}/columns/delete", json=body)
    assert done.status_code == 200, done.text
    assert done.json()["document"]["history"][-1]["summary"] == "Deleted a table column"
    assert client.post(f"{base}/columns/delete", json=body).status_code == 409


def test_column_changes_count_as_changes():
    assert limits.kind_of("POST", "/api/documents/abc/columns/delete") == "change"


# Adding a column -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["word/french-quote-word.pdf", "word/arabic-quotation-word.pdf", "word/english-styles-word.pdf", "additions/additions-chrome.pdf"])
@pytest.mark.parametrize("where", ["left", "right"])
def test_an_empty_column_is_added_beside_each_column(name, where):
    src = (FIXTURES / name).read_bytes()
    table = table_of(src)
    before = grid(table)
    for col in range(len(table.cols) - 1):
        line = next(l for c in table.cells if c.col == col for l in c.lines)
        result = columns.add_column(src, 0, line.id, line.text, where, [])
        after = table_of(result.pdf)
        at = col + 1 if where == "right" else col
        assert grid(after) == [row[:at] + [""] + row[at:] for row in before]
        assert after.rows == pytest.approx(table.rows, abs=0.6)
        # The table stays as wide (taking the columns' empty space) or grows where the page has room.
        assert after.bbox[2] - after.bbox[0] >= table.bbox[2] - table.bbox[0] - 0.6


@pytest.mark.parametrize("fonts", ["installed", "server"])
def test_a_column_with_text(fonts, monkeypatch):
    if fonts == "server":
        from app.fonts import library

        monkeypatch.setattr(library(), "find", lambda name: None)
    src = (FIXTURES / "word" / "turkish-quote-word.pdf").read_bytes()
    table = table_of(src)
    line = next(l for c in table.cells if c.col == 2 for l in c.lines)  # Birim Fiyat
    texts = ["Fiyat 2", "%5", "%5", "%0", ""]  # letters the PDF's copies have (bold has no "d")
    result = columns.add_column(src, 0, line.id, line.text, "right", texts)
    after = grid(table_of(result.pdf))
    assert [row[3] for row in after] == [comparable(t) for t in texts]
    assert result.lookalikes == []


def test_no_room_for_a_column():
    # Both columns of the contract's table are full and it spans the page.
    src = (FIXTURES / "word" / "bilingual-contract-word.pdf").read_bytes()
    table = table_of(src)
    line = table.cells[0].lines[0]
    with pytest.raises(EditRejected, match="no room for another column"):
        columns.add_column(src, 0, line.id, line.text, "right", [])


def test_adding_through_the_website(client):
    src = (FIXTURES / "word" / "french-quote-word.pdf").read_bytes()
    doc = upload(client, src, "devis.pdf").json()
    base = f"/api/documents/{doc['id']}"
    lines = client.get(f"{base}/pages/0/lines").json()["lines"]
    line = next(l for l in lines if l["text"] == "Quantité")
    body = {"revision": doc["revision"], "page": 0, "lineId": line["id"], "oldText": line["text"], "where": "right", "cells": ["Unité", "pièce", "forfait", "jour", ""]}
    done = client.post(f"{base}/columns/add", json=body)
    assert done.status_code == 200, done.text
    assert done.json()["document"]["history"][-1]["summary"] == "Added a table column"
    assert limits.kind_of("POST", "/api/documents/abc/columns/add") == "change"
