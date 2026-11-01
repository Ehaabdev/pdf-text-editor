"""A full page: what no longer fits flows onto the next page (overflow.py), drawn there
exactly as it was (pagecopy.py); past the last page, a new page with the same header
and footer."""

import io
from pathlib import Path

import numpy as np
import pikepdf
import pymupdf
import pytest

from app import moving, overflow, pagecopy
from app.adding import add_line
from app.changes import render
from app.editing import comparable
from app.fonts import library
from app.pdfcontent import replay
from app.textlayout import page_lines

FIXTURES = Path(__file__).parent / "fixtures"
FULL = (FIXTURES / "flow-full" / "flow-full-chrome.pdf").read_bytes()  # one full page
TWO = (FIXTURES / "flow" / "flow-chrome.pdf").read_bytes()  # a full page, then a short one
needs_fonts = pytest.mark.skipif(library().find("Calibri") is None, reason="the fonts of the test documents aren't installed")
NEW = "A new line added here"
HEADER = "Harbor Supplies - Proposal HS-2026-07"


def body(src: bytes) -> list[str]:
    """The text of every page but its header and footer, all pages in order."""
    doc = pymupdf.open("pdf", src)
    out = []
    for n in range(doc.page_count):
        page = doc[n]
        lines = page_lines(page)
        _, footer = moving.bottom_limit(doc, n, lines, (0.0, 0.0, page.rect.width, page.rect.height))
        header = overflow.header_lines(doc, n, lines)
        out += [comparable(l.text) for l in lines if l.id not in footer | header]
    return out


def line(src: bytes, page_no: int, start: str):
    return next(l for l in page_lines(pymupdf.open("pdf", src)[page_no]) if comparable(l.text).startswith(comparable(start)))


def add_below(src: bytes, page_no: int, start: str):
    ref = line(src, page_no, start)
    result = add_line(src, page_no, NEW, ref.id, ref.text)
    want = body(src)
    at = want.index(comparable(ref.text))
    assert body(result.pdf) == want[: at + 1] + [comparable(NEW)] + want[at + 1 :]
    return result


def links(src: bytes) -> list[list[str]]:
    return [[l.get("uri") for l in page.get_links()] for page in pymupdf.open("pdf", src)]


def at_place(src: bytes, page_no: int, text: str):
    l = line(src, page_no, text)
    return [round(v, 1) for v in l.bbox]


# Copying and taking off ----------------------------------------------------------------------


def test_every_piece_of_a_page_drawn_again_on_a_blank_page_looks_the_same():
    pdf = pikepdf.open(io.BytesIO(TWO))
    rp = replay(pdf, 0)
    page = pdf.pages[0]
    blank = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=page.mediabox, Resources=pikepdf.Dictionary(), Contents=pdf.make_stream(b""))
    if "/Group" in page.obj:
        blank.Group = page.obj.Group
    pdf.pages.append(pikepdf.Page(blank))
    dst = len(pdf.pages) - 1
    pieces = [p for p in moving._pieces(rp) if p.kind != "clip"]
    pagecopy.copy_pieces(pdf, rp, 0, dst, pieces, 0.0, replay(pdf, dst))
    out = io.BytesIO()
    pdf.save(out)
    keep = {k for k, p in enumerate(moving._pieces(rp)) if p.kind != "clip"}
    want_mu, want_pf = render(moving._only(TWO, 0, keep), 0)
    got_mu, got_pf = render(out.getvalue(), dst)
    assert np.array_equal(want_mu, got_mu)
    assert int((np.abs(want_pf.astype(int) - got_pf.astype(int)) > 40).sum()) == 0


def test_taking_a_line_off_leaves_the_page_as_drawn_without_it():
    rp = replay(pikepdf.open(io.BytesIO(FULL)), 0)
    last = line(FULL, 0, "Questions about these terms")
    glyphs = set(last.members)
    starts = {p.start for p in moving._pieces(rp) if set(p.glyphs) & glyphs}
    out = overflow.take_off(FULL, 0, starts)  # checked inside, pixel for pixel
    assert not any(comparable(l.text).startswith("Questions") for l in page_lines(pymupdf.open("pdf", out)[0]))
    assert links(out) == [[]]  # its link went with it


# Adding a line on a full page ----------------------------------------------------------------


@needs_fonts
def test_the_last_line_flows_onto_a_new_page_with_the_same_header_and_footer():
    result = add_below(FULL, 0, "1. Delivery of the ordered goods")
    doc = pymupdf.open("pdf", result.pdf)
    assert doc.page_count == 2
    assert doc[1].rect == doc[0].rect
    # The line that no longer fit: at the top of the new page, under the header, its link with it.
    assert comparable(page_lines(doc[1])[1].text).startswith("Questions about these terms")
    assert links(result.pdf) == [[], ["mailto:sales@example.com"]]
    assert at_place(result.pdf, 1, HEADER) == at_place(FULL, 0, HEADER)
    assert at_place(result.pdf, 1, "Page 1 of 1") == at_place(FULL, 0, "Page 1 of 1")
    assert any("new page was added" in w and "page number" in w for w in result.warnings)
    assert any("moved to page 2" in w for w in result.warnings)


@needs_fonts
def test_a_line_added_under_the_last_line_of_a_full_page_goes_to_the_next_page():
    result = add_below(FULL, 0, "Questions about these terms")
    doc = pymupdf.open("pdf", result.pdf)
    assert doc.page_count == 2
    # The full page is drawn as it was.
    before_mu, before_pf = render(FULL, 0)
    after_mu, after_pf = render(result.pdf, 0)
    assert np.array_equal(before_mu, after_mu) and np.array_equal(before_pf, after_pf)
    # The new line: at the top of the new page, where the body starts, as the change shows.
    first = page_lines(doc[1])[1]
    assert comparable(first.text) == comparable(NEW)
    assert abs(first.bbox[1] - line(FULL, 0, "Delivery terms").bbox[1]) < 12
    assert result.marks[0][0] == 1
    assert any("went to the top of page 2" in w for w in result.warnings)


@needs_fonts
def test_the_line_left_over_flows_to_the_top_of_the_next_page_and_its_text_moves_down():
    before = page_lines(pymupdf.open("pdf", TWO)[1])
    result = add_below(TWO, 0, "16. Smaller orders carry")
    doc = pymupdf.open("pdf", result.pdf)
    assert doc.page_count == 2
    after = page_lines(doc[1])
    moved = [l for l in after if comparable(l.text).startswith(comparable("given in euros, without tax"))]
    assert len(moved) == 1 and moved[0].bbox[1] < 100
    # The next page's own text: all of it, lower by the same amount, in order.
    old = [l for l in before if comparable(l.text).startswith(comparable("2. Prices"))][0]
    new = [l for l in after if comparable(l.text).startswith(comparable("2. Prices"))][0]
    assert new.bbox[1] > old.bbox[1] + 10
    assert links(result.pdf) == [["mailto:sales@example.com"], []]


@needs_fonts
@pytest.mark.parametrize("name,start", [("full", "9. Delivery"), ("two", "17. Delivery of the ordered"), ("full", "Questions about")])
def test_nothing_runs_into_the_bottom_margin(name, start):
    src = {"full": FULL, "two": TWO}[name]
    old = pymupdf.open("pdf", src)
    page = old[0]
    limit, _ = moving.bottom_limit(old, 0, page_lines(page), (0.0, 0.0, page.rect.width, page.rect.height))
    lowest = page.rect.height - limit  # as displayed
    doc = pymupdf.open("pdf", add_below(src, 0, start).pdf)
    for n in range(doc.page_count):
        for l in page_lines(doc[n]):
            if not comparable(l.text).startswith("Page "):
                assert l.bbox[3] <= lowest + 0.5, (n, l.text)  # a line's box may reach a little past its letters


@needs_fonts
def test_a_line_under_a_footnote_goes_to_a_new_page_once():
    """Drawn first under the footnote, in the footer's band: the new page's footer isn't
    copied with it."""
    src = (FIXTURES / "decorations" / "decorations-word.pdf").read_bytes()
    ref = line(src, 0, "1A footnote under the separator")
    result = add_line(src, 0, NEW, ref.id, ref.text)
    doc = pymupdf.open("pdf", result.pdf)
    assert doc.page_count == 2
    found = [(n, l.bbox[1]) for n in range(2) for l in page_lines(doc[n]) if comparable(l.text) == comparable(NEW)]
    assert len(found) == 1 and found[0][0] == 1 and found[0][1] < 120


@needs_fonts
def test_a_footnote_stays_at_the_bottom_while_the_text_above_moves_down():
    src = (FIXTURES / "decorations" / "decorations-word.pdf").read_bytes()
    ref = line(src, 0, "Plain line with an underlined")
    result = add_line(src, 0, NEW, ref.id, ref.text)
    assert pymupdf.open("pdf", result.pdf).page_count == 1
    assert at_place(result.pdf, 0, "1A footnote under the separator") == at_place(src, 0, "1A footnote under the separator")
    assert line(result.pdf, 0, "This line has a").bbox[1] > line(src, 0, "This line has a").bbox[1] + 10


# Through the website's API -------------------------------------------------------------------


@needs_fonts
def test_a_page_added_by_flowing_text_shows_in_the_document_and_undo_takes_it_away(client):
    from conftest import upload

    doc = upload(client, FULL, name="flow-full-chrome.pdf").json()
    assert doc["pageCount"] == 1 and doc["originalPageCount"] == 1
    lines = client.get(f"/api/documents/{doc['id']}/pages/0/lines").json()["lines"]
    ref = next(l for l in lines if l["text"].startswith("Questions about these terms"))
    r = client.post(f"/api/documents/{doc['id']}/additions", json={
        "revision": doc["revision"], "page": 0, "lineId": ref["id"], "oldText": ref["text"], "text": NEW})
    assert r.status_code == 200, r.text
    now = r.json()["document"]
    assert now["pageCount"] == 2 and now["originalPageCount"] == 1
    assert len(now["pages"]) == 2
    assert now["history"][-1]["pages"] == [1]
    assert any("top of page 2" in w for w in r.json()["warnings"])
    assert client.get(f"/api/documents/{doc['id']}/pages/1/image?width=200").status_code == 200
    assert client.get(f"/api/documents/{doc['id']}/pages/1/image?width=200&version=0").status_code == 404
    back = client.post(f"/api/documents/{doc['id']}/undo", json={"revision": now["revision"]}).json()
    assert back["pageCount"] == 1
