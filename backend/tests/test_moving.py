"""Text below moves down to make room for a new line, and up into a deleted line's space,
as in a word processor: by exactly the right amount, with everything that has to stay
(footers, the lines above, a full table cell) left where it is."""

import io
from pathlib import Path

import pikepdf
import pymupdf
import pytest

from app.adding import _baseline, add_line
from app.deleting import delete_lines
from app.editing import comparable, replace_line
from app.fonts import library
from app.pdfcontent import replay
from app.textlayout import page_lines

FIXTURES = Path(__file__).parent / "fixtures"
HAVE_FONTS = all(library().find(f) is not None for f in ("Calibri", "ArialMT", "TimesNewRomanPSMT"))
pytestmark = pytest.mark.skipif(not HAVE_FONTS, reason="the fonts of the test documents aren't installed")
ADDITIONS = {source: (FIXTURES / "additions" / f"additions-{source}.pdf").read_bytes() for source in ("word", "chrome")}


def lines_of(pdf: bytes, page_no: int = 0):
    return page_lines(pymupdf.open("pdf", pdf)[page_no])


def find(pdf: bytes, needle: str, page_no: int = 0):
    return next(l for l in lines_of(pdf, page_no) if comparable(l.text).startswith(comparable(needle)))


def base(pdf: bytes, line, page_no: int = 0) -> float:
    """The line's baseline (PDF y: up is more)."""
    with pikepdf.open(io.BytesIO(pdf)) as p:
        return _baseline(line, replay(p, page_no))


def order(pdf: bytes, page_no: int = 0):
    return [l.text for l in lines_of(pdf, page_no)]


@pytest.mark.parametrize("source", ADDITIONS)
def test_adding_inside_a_list_moves_the_rest_down_by_one_line(source):
    src = ADDITIONS[source]
    ref = find(src, "1. Confirm")
    result = add_line(src, 0, "Call the client", ref.id, ref.text)
    new = find(result.pdf, "Call the client")
    step = base(src, ref) - base(result.pdf, new)
    assert step > 5
    before, after = lines_of(src), lines_of(result.pdf)
    assert result.moved == len([l for l in before if l.bbox[1] > ref.bbox[3]])
    for old in before:
        now = next(l for l in after if l.text == old.text)
        if old.bbox[1] > ref.bbox[3]:  # below: exactly one line spacing lower
            assert abs((base(src, old) - base(result.pdf, now)) - step) < 0.01, old.text
        else:  # above, and the line itself: untouched
            assert now.bbox == pytest.approx(old.bbox, abs=0.01), old.text
    # The order of the text is as it reads: the new line right after the line it follows.
    texts = order(result.pdf)
    assert texts.index(new.text) == texts.index(ref.text) + 1


def test_adding_above_takes_the_lines_place():
    src = ADDITIONS["word"]
    ref = find(src, "2. Share")
    result = add_line(src, 0, "A line before", ref.id, ref.text, where="above")
    new = find(result.pdf, "A line before")
    moved = find(result.pdf, "2. Share")
    assert abs(base(result.pdf, new) - base(src, ref)) < 0.01
    assert base(result.pdf, moved) < base(result.pdf, new)
    assert find(result.pdf, "1. Confirm").bbox == pytest.approx(find(src, "1. Confirm").bbox, abs=0.01)


@pytest.mark.parametrize("source", ADDITIONS)
def test_deleting_a_line_closes_the_gap(source):
    """The next item takes the deleted one's place; everything below moves up as far."""
    src = ADDITIONS[source]
    gone = find(src, "2. Share")
    nxt = find(src, "3. Book")
    result = delete_lines(src, [(0, gone.id, gone.text)])
    assert result.moved > 0 and not result.warnings
    assert "2. Share" not in " ".join(order(result.pdf))
    now = find(result.pdf, "3. Book")
    assert abs(base(result.pdf, now) - base(src, gone)) < 0.01
    step = base(result.pdf, now) - base(src, nxt)
    for old in lines_of(src):
        if old.bbox[1] > nxt.bbox[1] - 0.5 and old.text != gone.text:
            moved = next(l for l in lines_of(result.pdf) if l.text == old.text)
            assert abs((base(result.pdf, moved) - base(src, old)) - step) < 0.01, old.text


def test_deleting_a_paragraphs_last_line_keeps_the_gap_after_it():
    """The next paragraph moves up by one line, not into the deleted line's place: the
    gap between paragraphs stays."""
    src = ADDITIONS["word"]
    first = find(src, "The team agreed")
    last = find(src, "week ahead")
    after = find(src, "Prepared by")
    result = delete_lines(src, [(0, last.id, last.text)])
    assert result.moved > 0
    gap_before = base(src, last) - base(src, after)
    gap_now = base(result.pdf, find(result.pdf, "The team agreed")) - base(result.pdf, find(result.pdf, "Prepared by"))
    assert abs(gap_now - gap_before) < 0.01
    assert find(result.pdf, "The team agreed").bbox == pytest.approx(first.bbox, abs=0.01)


def test_deleting_can_leave_the_space():
    src = ADDITIONS["word"]
    gone = find(src, "2. Share")
    result = delete_lines(src, [(0, gone.id, gone.text)], close_gap=False)
    assert result.moved == 0
    assert find(result.pdf, "3. Book").bbox == pytest.approx(find(src, "3. Book").bbox, abs=0.01)


def test_deleting_lines_apart_leaves_the_space_and_says_why():
    src = ADDITIONS["word"]
    a, b = find(src, "1. Confirm"), find(src, "3. Book")
    result = delete_lines(src, [(0, a.id, a.text), (0, b.id, b.text)])
    assert result.moved == 0
    assert any("stays empty" in w and "next to each other" in w for w in result.warnings)


@pytest.mark.parametrize(
    "path, page_no, needle, footer",
    [
        ("word/english-styles-word.pdf", 1, "Next steps", "Page 2 of 2"),
        ("word/bilingual-contract-word.pdf", 1, None, "Page 2 / 2"),
        ("word/arabic-quotation-word.pdf", 1, None, "صفحة 2 من 2"),
    ],
)
def test_the_footer_stays_where_it_is(path, page_no, needle, footer):
    src = (FIXTURES / path).read_bytes()
    lines = lines_of(src, page_no)
    ref = find(src, needle, page_no) if needle else lines[0]
    before = find(src, footer, page_no)
    text = "سطر جديد" if ref.rtl else "A new line"
    result = add_line(src, page_no, text, ref.id, ref.text)
    assert result.moved > 0
    assert find(result.pdf, footer, page_no).bbox == pytest.approx(before.bbox, abs=0.01)


def test_chrome_underlines_with_gaps_move_with_their_text():
    """Chrome cuts the gaps in an underline (under a "p" or "y") with a page-sized clip;
    it has to move with the underline."""
    src = (FIXTURES / "chrome" / "english-styles-chrome.pdf").read_bytes()
    ref = find(src, "Report ID")
    result = add_line(src, 0, "A new line", ref.id, ref.text)
    assert result.moved > 0
    assert any("dashboard.example.com" in t for t in order(result.pdf))


def test_moved_lines_can_be_edited_deleted_and_added_to_again():
    src = ADDITIONS["word"]
    ref = find(src, "1. Confirm")
    pdf = add_line(src, 0, "Call the client", ref.id, ref.text).pdf
    ref = find(pdf, "Call the client")
    pdf = add_line(pdf, 0, "Then email them", ref.id, ref.text).pdf  # twice in the same list
    moved = find(pdf, "• Laptops")
    pdf = replace_line(pdf, 0, moved.id, moved.text, "• Laptops and screens for the new team members").pdf
    moved = find(pdf, "• Access cards")
    pdf = delete_lines(pdf, [(0, moved.id, moved.text)]).pdf
    texts = order(pdf)
    assert texts[:5] == [
        "Project Kickoff Notes",
        "1. Confirm the delivery dates with the client",
        "Call the client",
        "Then email them",
        "2. Share the draft budget with finance",
    ]
    assert "• Laptops and screens for the new team members" in texts and not any("Access cards" in t for t in texts)
