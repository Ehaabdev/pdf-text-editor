"""Dragging lines or a table to another place on their page: they read and look the same
there (dragging.move checks it), their old place is empty, nothing else changes, and a
place that isn't empty, or not on the page, is refused with the reason."""

from pathlib import Path

import pymupdf
import pytest

from app import dragging, limits
from app.editing import EditRejected, comparable
from app.tables import find_tables
from app.textlayout import page_lines
from conftest import upload

FIXTURES = Path(__file__).parent / "fixtures"
TURKISH = (FIXTURES / "word" / "turkish-quote-word.pdf").read_bytes()
DECORATIONS = (FIXTURES / "decorations" / "decorations-word.pdf").read_bytes()
BILINGUAL = (FIXTURES / "word" / "bilingual-contract-word.pdf").read_bytes()
FRENCH_CHROME = (FIXTURES / "chrome" / "french-quote-chrome.pdf").read_bytes()


def lines_of(src: bytes, page_no: int = 0):
    return page_lines(pymupdf.open("pdf", src)[page_no])


def find(src: bytes, start: str, page_no: int = 0):
    return next(l for l in lines_of(src, page_no) if l.text.startswith(start))


def read(src: bytes, page_no: int = 0) -> list[tuple[str, list[float]]]:
    return sorted((comparable(l.text), [round(v, 1) for v in l.bbox]) for l in lines_of(src, page_no))


def free_spot(src: bytes, page_no: int, targets, table: bool, offsets):
    """The first of these offsets whose place is empty and on the page."""
    mu, page, pdf, lines, plan = dragging._plan(src, page_no, targets, table)
    pieces = dragging._pieces(plan.rp)
    for dx, dy in offsets:
        try:
            dragging._check_spot(page, pdf, plan, pieces, dx, dy)
            return dx, dy
        except EditRejected:
            continue
    raise AssertionError("no empty spot")


@pytest.mark.parametrize("offsets", [[(0, d) for d in (40, 80, 120, 160, -40, -80)], [(d, 0) for d in (-200, -150, -100, -60)], [(-150, 40), (-100, 80), (-200, 120)]], ids=["down", "sideways", "diagonal"])
def test_lines_move_and_read_the_same(offsets):
    """The signature (two lines) moved down, sideways or both ways."""
    sig = [find(TURKISH, "Mehmet Çelik", 1), find(TURKISH, "Genel Müdür", 1)]
    targets = [(l.id, l.text) for l in sig]
    dx, dy = free_spot(TURKISH, 1, targets, False, offsets)
    result = dragging.move(TURKISH, 1, targets, dx, dy)
    after = read(result.pdf, 1)
    moved = {comparable(l.text): [round(v, 1) for v in (l.bbox[0] + dx, l.bbox[1] + dy, l.bbox[2] + dx, l.bbox[3] + dy)] for l in sig}
    for text, box in moved.items():
        assert any(t == text and all(abs(a - b) < 0.2 for a, b in zip(bb, box)) for t, bb in after)
    # The rest reads as it did, where it was.
    before = [x for x in read(TURKISH, 1) if x[0] not in moved]
    assert [x for x in after if x[0] not in moved] == before
    assert result.moved == 2 and result.areas and result.marks


def test_a_move_goes_by_half_points():
    line = find(TURKISH, "Mehmet Çelik", 1)
    result = dragging.move(TURKISH, 1, [(line.id, line.text)], -100.3, 40.2)
    after = next(l for l in lines_of(result.pdf, 1) if l.text == "Mehmet Çelik")
    assert after.bbox[0] == pytest.approx(line.bbox[0] - 100.5, abs=0.05)
    assert after.bbox[1] == pytest.approx(line.bbox[1] + 40.0, abs=0.05)


def test_a_table_moves_with_its_borders_and_shading():
    page = pymupdf.open("pdf", TURKISH)[0]
    (table,) = [t for t in find_tables(page, page_lines(page)) if t.ruled]
    line = table.lines[0]
    targets = [(line.id, line.text)]
    dx, dy = free_spot(TURKISH, 0, targets, True, [(0, d) for d in (20, 30, 40, 60, 80, 120, 160, 200)])
    result = dragging.move(TURKISH, 0, targets, dx, dy, table=True)
    page = pymupdf.open("pdf", result.pdf)[0]
    (moved,) = [t for t in find_tables(page, page_lines(page)) if t.ruled]
    assert [round(v, 1) for v in moved.bbox] == [round(v, 1) for v in (table.bbox[0] + dx, table.bbox[1] + dy, table.bbox[2] + dx, table.bbox[3] + dy)]
    assert [comparable(l.text) for l in moved.lines] == [comparable(l.text) for l in table.lines]
    assert result.moved == len(table.lines)


def test_an_underline_and_a_link_go_with_their_text():
    page = pymupdf.open("pdf", DECORATIONS)[0]
    for start in ("Fully underlined line", "Write to help@example.com"):
        line = find(DECORATIONS, start)
        targets = [(line.id, line.text)]
        dx, dy = free_spot(DECORATIONS, 0, targets, False, [(d, 0) for d in (60, 100, 150, 200, -60)] + [(0, d) for d in (300, 250, 200, 150)])
        result = dragging.move(DECORATIONS, 0, targets, dx, dy)
        assert not result.warnings  # nothing left behind
        new = pymupdf.open("pdf", result.pdf)[0]
        if start.startswith("Write"):
            (old_link,) = [k for k in page.get_links() if "help@example.com" in (k.get("uri") or "")]
            (new_link,) = [k for k in new.get_links() if "help@example.com" in (k.get("uri") or "")]
            assert new_link["from"].x0 == pytest.approx(old_link["from"].x0 + dx, abs=0.05)
            assert new_link["from"].y0 == pytest.approx(old_link["from"].y0 + dy, abs=0.05)


def test_text_in_a_table_cell_moves_with_the_cell_clipping():
    # Word clips a cell's text to its cell: the clipping moves with the text, out of the cell.
    src = (FIXTURES / "additions" / "additions-word.pdf").read_bytes()
    line = find(src, "Owner")
    result = dragging.move(src, 0, [(line.id, line.text)], -40, 0)
    assert any(comparable(l.text) == "Owner" and abs(l.bbox[0] - (line.bbox[0] - 40)) < 0.1 for l in lines_of(result.pdf))


def test_text_leaves_a_shaded_cell_whose_clip_is_its_shading():
    # As in real files: a cell's text clipped to exactly the rectangle of the cell's
    # color. The clip goes with the text; the color stays, as MuPDF lists it.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    font = str(Path(__file__).parents[1] / "lookalikes" / "LiberationSans-Regular.ttf")
    page.insert_text((110, 230), "Cell text", fontname="LS", fontfile=font, fontsize=12)
    page.insert_text((100, 540), "Another line far below", fontname="LS", fontfile=font, fontsize=12)
    first = page.get_contents()[0]
    body = doc.xref_stream(first).decode("latin-1").strip()
    cell = "q 0.62 0.77 0.91 rg 100 600 200 30 re f Q", "q 100 600 200 30 re W n", body, "Q"
    doc.update_stream(first, "\n".join(cell).encode("latin-1"))
    src = doc.tobytes()
    line = find(src, "Cell text")
    result = dragging.move(src, 0, [(line.id, line.text)], 0, 100)
    assert read(result.pdf) == sorted([("Another line far below", [round(v, 1) for v in find(src, "Another").bbox]), ("Cell text", [round(v + d, 1) for v, d in zip(line.bbox, (0, 100, 0, 100))])])


def test_refused_with_a_reason():
    sig = find(TURKISH, "Mehmet Çelik", 1)
    other = find(TURKISH, "Saygılarımızla", 1)
    targets = [(sig.id, sig.text)]
    with pytest.raises(EditRejected, match="land on other text"):
        dragging.move(TURKISH, 1, targets, other.bbox[0] - sig.bbox[0], other.bbox[1] - sig.bbox[1])
    with pytest.raises(EditRejected, match="off the page"):
        dragging.move(TURKISH, 1, targets, 200, 0)
    with pytest.raises(EditRejected, match="same place"):
        dragging.move(TURKISH, 1, targets, 0.2, -0.1)
    # Onto the table's borders and shading.
    page = pymupdf.open("pdf", TURKISH)[0]
    (table,) = [t for t in find_tables(page, page_lines(page)) if t.ruled]
    date = find(TURKISH, "İstanbul, 21")
    with pytest.raises(EditRejected, match="land on"):
        dragging.move(TURKISH, 0, [(date.id, date.text)], 0, table.bbox[1] - date.bbox[1] + 2)
    # Chrome shows a page's text only inside its margins.
    line = find(FRENCH_CHROME, "Lyon, le 21")
    with pytest.raises(EditRejected, match="cut off"):
        dragging.move(FRENCH_CHROME, 0, [(line.id, line.text)], 60, 0)


def test_lines_stored_together_move_together():
    english = find(BILINGUAL, "This Agreement is made on")
    with pytest.raises(dragging.Together) as caught:
        dragging.preview(BILINGUAL, 0, [(english.id, english.text)])
    arabic = caught.value.lines
    assert [l.text for l in arabic] and all(l.rtl for l in arabic)
    png, box, ids = dragging.preview(BILINGUAL, 0, [(english.id, english.text)] + [(l.id, l.text) for l in arabic])
    assert png.startswith(b"\x89PNG") and box[2] > box[0] and set(ids) == {english.id, *(l.id for l in arabic)}


def test_through_the_website(client):
    doc = upload(client, TURKISH, "teklif.pdf").json()
    base = f"/api/documents/{doc['id']}"
    lines = client.get(f"{base}/pages/1/lines").json()["lines"]
    sig = next(l for l in lines if l["text"] == "Mehmet Çelik")
    body = {"revision": doc["revision"], "page": 1, "lines": [{"lineId": sig["id"], "oldText": sig["text"]}], "table": False}
    preview = client.post(f"{base}/moves/preview", json=body).json()
    assert preview["image"].startswith("data:image/png;base64,") and preview["lines"] == [sig["id"]]
    moved = client.post(f"{base}/moves", json={**body, "dx": -150, "dy": 40})
    assert moved.status_code == 200, moved.text
    summary = moved.json()["document"]["history"][-1]["summary"]
    assert summary.replace("⁨", "").replace("⁩", "") == "Moved “Mehmet Çelik”"
    stale = client.post(f"{base}/moves", json={**body, "dx": -150, "dy": 40})
    assert stale.status_code == 409
    # Lines stored as one with others: the answer names them, to move them together.
    doc = upload(client, BILINGUAL, "contract.pdf").json()
    base = f"/api/documents/{doc['id']}"
    english = next(l for l in client.get(f"{base}/pages/0/lines").json()["lines"] if l["text"].startswith("This Agreement is made on"))
    body = {"revision": doc["revision"], "page": 0, "lines": [{"lineId": english["id"], "oldText": english["text"]}]}
    answer = client.post(f"{base}/moves/preview", json=body).json()
    assert answer["together"] and "together" in answer["message"]


def test_moves_count_as_changes():
    assert limits.kind_of("POST", "/api/documents/abc/moves") == "change"
    assert limits.kind_of("POST", "/api/documents/abc/moves/preview") == "read"
