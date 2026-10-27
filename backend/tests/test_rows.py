"""Table rows: a new row below or above a row, looking like it, with its cells' text; a
row deleted; the rows below moving down or up; and nothing else on the page changing."""

from pathlib import Path

import pymupdf
import pytest

from app import limits
from app.editing import EditRejected, comparable
from app.rows import Spot, add_row, delete_row
from app.tables import find_tables
from app.textlayout import page_lines
from conftest import upload

FIXTURES = Path(__file__).parent / "fixtures"
FRENCH = (FIXTURES / "word" / "french-quote-word.pdf").read_bytes()
ARABIC = (FIXTURES / "word" / "arabic-quotation-word.pdf").read_bytes()


def grid(src: bytes, page_no: int = 0) -> list[list[str]]:
    """The first bordered table's text, row by row, in the table's order."""
    page = pymupdf.open("pdf", src)[page_no]
    (table,) = [t for t in find_tables(page, page_lines(page)) if t.ruled]
    return [[comparable(" ".join(l.text for l in c.lines)) for c in Spot(table, r).cells] for r in range(len(table.rows) - 1)]


def line_in_row(src: bytes, row: int, page_no: int = 0):
    page = pymupdf.open("pdf", src)[page_no]
    (table,) = [t for t in find_tables(page, page_lines(page)) if t.ruled]
    return next(l for c in Spot(table, row).cells for l in c.lines)


def lines_outside(src: bytes, top: float) -> list[tuple]:
    """The page's lines above a height, as they read and where they are."""
    page = pymupdf.open("pdf", src)[0]
    return [(l.text, [round(v, 1) for v in l.bbox]) for l in page_lines(page) if l.bbox[3] < top]


SOURCES = {"french": FRENCH, "arabic": ARABIC}


@pytest.mark.parametrize(
    "name, row, where, texts",
    [
        ("french", 1, "below", ["Pose et réglage", "2", "150,00 €", "300,00 €"]),
        ("french", 0, "above", ["Devis", "", "", "Total"]),  # above the shaded header: shaded too
        ("french", 4, "below", ["Remise", "", "", "100,00 €"]),  # under the last row
        ("arabic", 2, "below", ["رسائل الترحيب", "1,000", "0.05", "50.00"]),  # right to left
    ],
)
def test_a_new_row(name, row, where, texts):
    src = SOURCES[name]
    before = grid(src)
    line = line_in_row(src, row)
    result = add_row(src, 0, line.id, line.text, where, texts)
    at = row + 1 if where == "below" else row
    assert grid(result.pdf) == before[:at] + [[comparable(t) for t in texts]] + before[at:]
    # Above the table nothing moved.
    page = pymupdf.open("pdf", src)[0]
    (table,) = [t for t in find_tables(page, page_lines(page)) if t.ruled]
    assert lines_outside(result.pdf, table.bbox[1]) == lines_outside(src, table.bbox[1])
    assert result.marks and result.areas


@pytest.mark.parametrize(
    "row, texts",
    [
        # One copy of Calibri lists "€" with width 0 (a code Word didn't use): drawn from
        # it, the sign took no room and overflowed its cell. The other copy has it.
        (1, ["Pose et réglage", "2", "150,00 €", "300,00 €"]),
        # The other copy has a "-" too, but without a label: it read back as "�".
        (3, ["Main-d’œuvre (forfait)", "", "", ""]),
    ],
)
def test_a_new_row_without_the_font_installed(monkeypatch, row, texts):
    # As on the server, without Calibri: the letters come from the PDF's own copies of it.
    from app.fonts import library

    monkeypatch.setattr(library(), "find", lambda name: None)
    line = line_in_row(FRENCH, row)
    result = add_row(FRENCH, 0, line.id, line.text, "below", texts)
    assert grid(result.pdf)[row + 1] == [comparable(t) for t in texts]
    assert result.lookalikes == []
    # As wide as the same text on the page: no letter drawn taking no room ("’" and "œ"
    # have width 0 in that copy too).
    widths = {}
    for l in page_lines(pymupdf.open("pdf", result.pdf)[0]):
        widths.setdefault(comparable(l.text), []).append(l.bbox[2] - l.bbox[0])
    for t in texts:
        if t and len(widths[comparable(t)]) == 2:
            old, new = widths[comparable(t)]
            assert abs(old - new) < 0.5, t


def test_a_new_row_like_the_one_next_to_it():
    # The same text twice, one row under the other: the new row is the one that wasn't there.
    src = (FIXTURES / "decorations" / "decorations-chrome.pdf").read_bytes()
    before = grid(src)
    line = line_in_row(src, 0)
    result = add_row(src, 0, line.id, line.text, "below", before[0])
    assert grid(result.pdf) == [before[0], before[0], *before[1:]]


def test_a_row_is_deleted_and_the_rows_below_move_up():
    before = grid(FRENCH)
    for row in (0, 2, 4):  # the shaded header, a middle row, the last row
        line = line_in_row(FRENCH, row)
        result = delete_row(FRENCH, 0, line.id, line.text)
        assert grid(result.pdf) == before[:row] + before[row + 1:]


def test_add_then_delete_gives_the_table_back():
    line = line_in_row(FRENCH, 1)
    added = add_row(FRENCH, 0, line.id, line.text, "below", ["Pose", "2", "", ""])
    line = line_in_row(added.pdf, 2)
    deleted = delete_row(added.pdf, 0, line.id, line.text)
    assert grid(deleted.pdf) == grid(FRENCH)
    page_a = pymupdf.open("pdf", FRENCH)[0]
    page_b = pymupdf.open("pdf", deleted.pdf)[0]
    assert [(l.text, [round(v, 1) for v in l.bbox]) for l in page_lines(page_a)] == [(l.text, [round(v, 1) for v in l.bbox]) for l in page_lines(page_b)]


def test_refused_with_a_reason():
    # A cell's text stays on one line: too long for its cell.
    line = line_in_row(FRENCH, 1)
    with pytest.raises(EditRejected, match="doesn't fit in its cell"):
        add_row(FRENCH, 0, line.id, line.text, "below", ["", "A quantity far too long for this narrow column of the table", "", ""])
    # A full page: nothing goes onto the next one.
    english = (FIXTURES / "word" / "english-styles-word.pdf").read_bytes()
    line = line_in_row(english, 1)
    with pytest.raises(EditRejected, match="no room on this page"):
        add_row(english, 0, line.id, line.text, "below", ["x", "", "", ""])
    # A table's only row.
    contract = (FIXTURES / "word" / "bilingual-contract-word.pdf").read_bytes()
    line = line_in_row(contract, 0, page_no=1)
    with pytest.raises(EditRejected, match="only row"):
        delete_row(contract, 1, line.id, line.text)
    # Not a table.
    page = pymupdf.open("pdf", FRENCH)[0]
    plain = next(l for l in page_lines(page) if l.text.startswith("Madame"))
    with pytest.raises(EditRejected, match="isn't in a table"):
        delete_row(FRENCH, 0, plain.id, plain.text)


def test_through_the_website(client):
    doc = upload(client, FRENCH, "devis.pdf").json()
    base = f"/api/documents/{doc['id']}"
    (table,) = client.get(f"{base}/pages/0/tables").json()["tables"]
    assert len(table["rows"]) == 5 and not table["rtl"]
    cell = table["rows"][1][0]
    lines = client.get(f"{base}/pages/0/lines").json()["lines"]
    line = next(l for l in lines if l["id"] == cell["lines"][0])
    added = client.post(
        f"{base}/rows/add",
        json={"revision": doc["revision"], "page": 0, "lineId": line["id"], "oldText": line["text"], "where": "below", "cells": ["Pose", "2", "", ""]},
    )
    assert added.status_code == 200, added.text
    assert added.json()["document"]["history"][-1]["summary"] == "Added a table row"
    stale = client.post(f"{base}/rows/delete", json={"revision": doc["revision"], "page": 0, "lineId": line["id"], "oldText": line["text"]})
    assert stale.status_code == 409


def test_row_changes_count_as_changes():
    assert limits.kind_of("POST", "/api/documents/abc/rows/add") == "change"
    assert limits.kind_of("POST", "/api/documents/abc/rows/delete") == "change"


# Merged cells -----------------------------------------------------------------------------------

MERGED = FIXTURES / "tables" / "tables-chrome.pdf"  # tables.html: four tables with merged cells


def merged_tables(src: bytes) -> list:
    page = pymupdf.open("pdf", src)[0]
    return [t for t in find_tables(page, page_lines(page)) if t.ruled]


def cells_of(table) -> list:
    """(row, column, rows, columns, text) of each cell."""
    return sorted((c.row, c.col, c.rowspan, c.colspan, comparable(" ".join(l.text for l in c.lines))) for c in table.cells)


def line_in(table, text: str):
    return next(l for c in table.cells for l in c.lines if l.text == text)


def centered(table, text: str) -> bool:
    cell = next(c for c in table.cells if any(l.text == text for l in c.lines))
    top = min(l.bbox[1] for l in cell.lines) - cell.bbox[1]
    bottom = cell.bbox[3] - max(l.bbox[3] for l in cell.lines)
    return abs(top - bottom) <= 1.5  # Chrome puts a line's box half a point off its middle


ROOMS = [
    (0, 0, 3, 1, "Ground floor"), (0, 1, 1, 1, "Lobby"), (0, 2, 1, 1, "40 m²"),
    (1, 1, 1, 1, "Office"), (1, 2, 2, 1, "25 m²"),
    (2, 1, 1, 1, "Store"),
    (3, 0, 1, 1, "First floor"), (3, 1, 1, 1, "Meeting room"), (3, 2, 1, 1, "30 m²"),
]


def test_rows_added_inside_merged_cells():
    src = MERGED.read_bytes()
    rooms = merged_tables(src)[3]
    assert cells_of(rooms) == sorted(ROOMS)
    # Below "Lobby": inside "Ground floor", which gets a row taller (its text staying in its
    # middle); the new row has cells under "Lobby" and "40 m²".
    result = add_row(src, 0, line_in(rooms, "Lobby").id, "Lobby", "below", ["Hall", "8 m²"])
    after = merged_tables(result.pdf)[3]
    assert cells_of(after) == sorted([
        (0, 0, 4, 1, "Ground floor"), (0, 1, 1, 1, "Lobby"), (0, 2, 1, 1, "40 m²"),
        (1, 1, 1, 1, "Hall"), (1, 2, 1, 1, "8 m²"),
        (2, 1, 1, 1, "Office"), (2, 2, 2, 1, "25 m²"),
        (3, 1, 1, 1, "Store"),
        (4, 0, 1, 1, "First floor"), (4, 1, 1, 1, "Meeting room"), (4, 2, 1, 1, "30 m²"),
    ])
    assert centered(after, "Ground floor")
    # Below "Office": inside both merged cells; the new row has one cell.
    result = add_row(src, 0, line_in(rooms, "Office").id, "Office", "below", ["Kitchen"])
    after = merged_tables(result.pdf)[3]
    assert (0, 0, 4, 1, "Ground floor") in cells_of(after) and (1, 2, 3, 1, "25 m²") in cells_of(after)
    assert (2, 1, 1, 1, "Kitchen") in cells_of(after)
    assert centered(after, "Ground floor") and centered(after, "25 m²")
    # Above "Lobby": above "Ground floor" too; the new row has a cell under each.
    result = add_row(src, 0, line_in(rooms, "Lobby").id, "Lobby", "above", ["Basement", "Garage", "60 m²"])
    after = merged_tables(result.pdf)[3]
    assert cells_of(after)[:3] == [(0, 0, 1, 1, "Basement"), (0, 1, 1, 1, "Garage"), (0, 2, 1, 1, "60 m²")]
    assert (1, 0, 3, 1, "Ground floor") in cells_of(after)
    # Below "Ground floor": below its last row.
    result = add_row(src, 0, line_in(rooms, "Ground floor").id, "Ground floor", "below", ["", "Stairs", ""])
    after = merged_tables(result.pdf)[3]
    assert [c for c in cells_of(after) if c[0] == 3] == [(3, 0, 1, 1, ""), (3, 1, 1, 1, "Stairs"), (3, 2, 1, 1, "")]


def test_rows_beside_cells_with_text_at_their_top():
    src = MERGED.read_bytes()
    schedule = merged_tables(src)[0]
    phase = next(c for c in schedule.cells if c.rowspan > 1)
    result = add_row(src, 0, line_in(schedule, "Cabling").id, "Cabling", "below", ["Painting", "2 days"])
    after = merged_tables(result.pdf)[0]
    assert (0, 0, 6, 1, "Phase one") in cells_of(after) and (3, 1, 1, 1, "Painting") in cells_of(after)
    # "Phase one" stays at its top.
    moved = next(c for c in after.cells if c.rowspan > 1)
    assert moved.lines[0].bbox == pytest.approx(phase.lines[0].bbox, abs=0.05)
    # "North" and the three numbers two rows tall all get taller.
    quarters = merged_tables(src)[1]
    result = add_row(src, 0, line_in(quarters, "Orders").id, "Orders", "below", ["Late orders"])
    after = cells_of(merged_tables(result.pdf)[1])
    assert (0, 0, 4, 1, "North") in after and (0, 2, 3, 1, "120") in after and (1, 1, 1, 1, "Late orders") in after


def test_rows_deleted_from_merged_cells():
    src = MERGED.read_bytes()
    rooms = merged_tables(src)[3]
    # "Office": "Ground floor" and "25 m²" (which starts there) a row shorter.
    after = merged_tables(delete_row(src, 0, line_in(rooms, "Office").id, "Office").pdf)[3]
    assert cells_of(after) == sorted([
        (0, 0, 2, 1, "Ground floor"), (0, 1, 1, 1, "Lobby"), (0, 2, 1, 1, "40 m²"),
        (1, 1, 1, 1, "Store"), (1, 2, 1, 1, "25 m²"),
        (2, 0, 1, 1, "First floor"), (2, 1, 1, 1, "Meeting room"), (2, 2, 1, 1, "30 m²"),
    ])
    assert centered(after, "Ground floor") and centered(after, "25 m²")
    # "Store": the last row of both; their bottom border goes up with them.
    after = merged_tables(delete_row(src, 0, line_in(rooms, "Store").id, "Store").pdf)[3]
    assert cells_of(after) == sorted([
        (0, 0, 2, 1, "Ground floor"), (0, 1, 1, 1, "Lobby"), (0, 2, 1, 1, "40 m²"),
        (1, 1, 1, 1, "Office"), (1, 2, 1, 1, "25 m²"),
        (2, 0, 1, 1, "First floor"), (2, 1, 1, 1, "Meeting room"), (2, 2, 1, 1, "30 m²"),
    ])
    # "Lobby": the first row of "Ground floor".
    after = merged_tables(delete_row(src, 0, line_in(rooms, "Lobby").id, "Lobby").pdf)[3]
    assert (0, 0, 2, 1, "Ground floor") in cells_of(after) and centered(after, "Ground floor")
    # A cell merged over several rows: which row?
    with pytest.raises(EditRejected, match="one row tall"):
        delete_row(src, 0, line_in(rooms, "Ground floor").id, "Ground floor")
