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


# Merged cells -----------------------------------------------------------------------------------

MERGED = FIXTURES / "tables" / "tables-chrome.pdf"  # tables.html: four tables with merged cells


def tables_of(src: bytes) -> list:
    page = pymupdf.open("pdf", src)[0]
    return [t for t in find_tables(page, page_lines(page)) if t.ruled]


def cells_of(table) -> list:
    """(row, column, rows, columns, text) of each cell."""
    return sorted((c.row, c.col, c.rowspan, c.colspan, comparable(" ".join(l.text for l in c.lines))) for c in table.cells)


def test_a_column_through_merged_cells_is_deleted():
    src = MERGED.read_bytes()
    for n, table in enumerate(tables_of(src)):
        for col in range(len(table.cols) - 1):
            cell = next(c for c in table.cells if c.col == col and c.colspan == 1 and c.lines)
            result = columns.delete_column(src, 0, cell.lines[0].id, cell.lines[0].text)
            after = tables_of(result.pdf)[n]
            width = table.cols[col + 1] - table.cols[col]
            assert after.bbox[2] - after.bbox[0] == pytest.approx(table.bbox[2] - table.bbox[0] - width, abs=0.6)
            # Its cells go; a cell merged across it is a column narrower.
            gone = lambda c: c.col == col and c.colspan == 1
            across = lambda c: c.col <= col < c.col + c.colspan
            want = sorted((c.row, c.col if c.col <= col else c.col - 1, c.rowspan, c.colspan - across(c), comparable(" ".join(l.text for l in c.lines))) for c in table.cells if not gone(c))
            # Its cells were all that split two rows: they read as one now.
            if (n, col) == (1, 1):
                want = [(0, 0, 2, 1, "North"), (0, 1, 1, 1, "120"), (0, 2, 1, 1, "135"), (0, 3, 1, 1, "150"), (1, 1, 1, 1, "4"), (1, 2, 1, 1, "6"), (1, 3, 1, 1, "5")]
            if (n, col) == (3, 1):
                want = [(0, 0, 2, 1, "Ground floor"), (0, 1, 1, 1, "40 m²"), (1, 1, 1, 1, "25 m²"), (2, 0, 1, 1, "First floor"), (2, 1, 1, 1, "30 m²")]
            assert cells_of(after) == want, (n, col)
    # A merged cell's centered text stays in its middle.
    table = tables_of(src)[0]
    line = next(c for c in table.cells if c.row == 0 and c.col == 1).lines[0]  # Site survey
    total = next(c for c in tables_of(columns.delete_column(src, 0, line.id, line.text).pdf)[0].cells if c.colspan == 1 and c.row == 5 and c.col == 1)
    left, right = total.lines[0].bbox[0] - total.bbox[0], total.bbox[2] - total.lines[0].bbox[2]
    assert left == pytest.approx(right, abs=1.0)


def test_deleting_from_a_merged_cell_is_refused():
    src = MERGED.read_bytes()
    total = next(c for c in tables_of(src)[0].cells if c.colspan > 1)
    with pytest.raises(EditRejected, match="one column wide"):
        columns.delete_column(src, 0, total.lines[0].id, total.lines[0].text)


def test_a_column_is_added_beside_merged_cells():
    src = MERGED.read_bytes()
    for n, table in enumerate(tables_of(src)):
        for cell in (c for c in table.cells if c.lines):
            for where in ("left", "right"):
                after = tables_of(columns.add_column(src, 0, cell.lines[0].id, cell.lines[0].text, where, []).pdf)[n]
                assert len(after.cols) == len(table.cols) + 1
                # Every cell keeps its text and its rows.
                assert sorted((r, rs, t) for r, _, rs, _, t in cells_of(after) if t) == sorted((r, rs, t) for r, _, rs, _, t in cells_of(table) if t)
    schedule = tables_of(src)[0]
    # Inside "Total" (two columns wide): it gets wider, the other rows get a cell.
    line = next(c for c in schedule.cells if c.row == 0 and c.col == 1).lines[0]
    after = cells_of(tables_of(columns.add_column(src, 0, line.id, line.text, "right", []).pdf)[0])
    assert (5, 1, 1, 3, "Total: 15 working days") in after and [c for c in after if c[1] == 2] == [(r, 2, 1, 1, "") for r in range(5)]
    # Beside "Phase one" (five rows tall): a new cell as tall, with text typed for it.
    phase = next(c for c in schedule.cells if c.rowspan > 1)
    result = columns.add_column(src, 0, phase.lines[0].id, phase.lines[0].text, "right", ["Phase two", "", "", "", "", "Later"])
    assert [c for c in cells_of(tables_of(result.pdf)[0]) if c[1] == 1] == [(0, 1, 5, 1, "Phase two"), (5, 1, 1, 1, "Later")]
    # A row whose merged cell gets wider has no new cell to type in.
    with pytest.raises(EditRejected, match="gets wider"):
        columns.add_column(src, 0, line.id, line.text, "right", ["", "", "", "", "", "x"])


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


# Resizing a column ------------------------------------------------------------------------------


def test_a_border_dragged_between_two_columns():
    src = (FIXTURES / "word" / "french-quote-word.pdf").read_bytes()
    table = table_of(src)
    before = grid(table)
    page = pymupdf.open("pdf", src)[0]
    lo, hi = columns.border_limits(page, table, page_lines(page))[1]
    line = table.cells[0].lines[0]
    for x in (lo, hi):
        after = table_of(columns.resize_column(src, 0, line.id, line.text, 1, x).pdf)
        # The two columns beside it trade width; the table keeps its width and its text.
        assert after.cols == pytest.approx([table.cols[0], x, *table.cols[2:]], abs=0.6)
        assert grid(after) == before


def test_the_table_edge_dragged_and_merged_cells():
    src = MERGED.read_bytes()
    for n, table in enumerate(tables_of(src)):
        page = pymupdf.open("pdf", src)[0]
        limits = columns.border_limits(page, table, page_lines(page))
        line = next(l for c in table.cells for l in c.lines)
        for j, (lo, hi) in enumerate(limits):
            for x in (lo, hi):
                if abs(x - table.cols[j]) < 1:
                    continue
                after = tables_of(columns.resize_column(src, 0, line.id, line.text, j, x).pdf)[n]
                want = list(table.cols)
                want[j] = x
                assert after.cols == pytest.approx(want, abs=0.6)
                assert cells_of(after) == cells_of(table)


def test_a_column_cant_get_narrower_than_its_text():
    src = (FIXTURES / "word" / "french-quote-word.pdf").read_bytes()
    table = table_of(src)
    page = pymupdf.open("pdf", src)[0]
    limits = columns.border_limits(page, table, page_lines(page))
    line = table.cells[0].lines[0]
    with pytest.raises(EditRejected, match="narrower than its text"):
        columns.resize_column(src, 0, line.id, line.text, 1, limits[1][0] - 5)
    # Chrome's table is as wide as the page's text: it can't grow.
    merged = MERGED.read_bytes()
    first = tables_of(merged)[0]
    line = first.cells[0].lines[0]
    with pytest.raises(EditRejected, match="no room on the page"):
        columns.resize_column(merged, 0, line.id, line.text, len(first.cols) - 1, first.cols[-1] + 20)


def test_resizing_through_the_website(client):
    src = (FIXTURES / "word" / "french-quote-word.pdf").read_bytes()
    doc = upload(client, src, "devis.pdf").json()
    base = f"/api/documents/{doc['id']}"
    tables = client.get(f"{base}/pages/0/tables").json()["tables"]
    table = next(t for t in tables if len(t["cols"]) > 2)
    assert len(table["limits"]) == len(table["cols"])
    lines = client.get(f"{base}/pages/0/lines").json()["lines"]
    line = next(l for l in lines if l["text"] == "Quantité")
    lo, hi = table["limits"][1]
    body = {"revision": doc["revision"], "page": 0, "lineId": line["id"], "oldText": line["text"], "edge": 1, "x": hi}
    done = client.post(f"{base}/columns/resize", json=body)
    assert done.status_code == 200, done.text
    assert done.json()["document"]["history"][-1]["summary"] == "Resized a table column"
    assert limits.kind_of("POST", "/api/documents/abc/columns/resize") == "change"


# Making a whole table bigger or smaller ---------------------------------------------------------


def test_a_table_made_smaller_like_a_picture():
    from app import scaling

    src = (FIXTURES / "word" / "french-quote-word.pdf").read_bytes()
    table = table_of(src)
    before = grid(table)
    w = table.bbox[2] - table.bbox[0]
    line = table.cells[0].lines[0]
    result = scaling.scale_table(src, 0, line.id, line.text, round(0.8 * w))
    after = table_of(result.pdf)
    s = round(0.8 * w) / w
    # Its top-left corner stays; everything else is 80% as big, text included.
    assert after.bbox[0] == pytest.approx(table.bbox[0], abs=0.6) and after.bbox[1] == pytest.approx(table.bbox[1], abs=0.6)
    assert after.bbox[2] - after.bbox[0] == pytest.approx(w * s, abs=0.6)
    assert grid(after) == before
    sizes = lambda t: sorted(l.style.size for c in t.cells for l in c.lines)
    assert sizes(after) == pytest.approx([v * s for v in sizes(table)], abs=0.05)


def test_a_table_cant_get_too_small_or_too_big():
    from app import scaling

    src = (FIXTURES / "word" / "french-quote-word.pdf").read_bytes()
    table = table_of(src)
    page = pymupdf.open("pdf", src)[0]
    low, high = scaling.scale_limits(page, table, page_lines(page))
    w = table.bbox[2] - table.bbox[0]
    line = table.cells[0].lines[0]
    with pytest.raises(EditRejected, match="too small to read"):
        scaling.scale_table(src, 0, line.id, line.text, w * low - 5)
    with pytest.raises(EditRejected, match="no room on the page"):
        scaling.scale_table(src, 0, line.id, line.text, w * high + 5)


def test_merged_tables_scale_too(client):
    src = MERGED.read_bytes()
    doc = upload(client, src, "tables.pdf").json()
    base = f"/api/documents/{doc['id']}"
    tables = client.get(f"{base}/pages/0/tables").json()["tables"]
    low, high = tables[3]["scale"]
    assert low < 0.7 and high < 1.01  # as wide as the page's text: it can't grow
    lines = client.get(f"{base}/pages/0/lines").json()["lines"]
    line = next(l for l in lines if l["text"] == "Ground floor")
    width = tables[3]["bbox"][2] - tables[3]["bbox"][0]
    body = {"revision": doc["revision"], "page": 0, "lineId": line["id"], "oldText": line["text"], "width": round(width * 0.7)}
    done = client.post(f"{base}/tables/scale", json=body)
    assert done.status_code == 200, done.text
    assert done.json()["document"]["history"][-1]["summary"] == "Resized a table"
    assert limits.kind_of("POST", "/api/documents/abc/tables/scale") == "change"
