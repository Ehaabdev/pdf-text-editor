"""A line added in a list becomes its next item, numbered in the list's own style, and the
items after it are renumbered; deleting an item counts the rest down. A list never reads
1, 2, 2."""

from pathlib import Path

import pymupdf
import pytest

from app.deleting import delete_lines
from app.editing import EditRejected, comparable
from app.fonts import library
from app.lists import add_item, markers, renumber_after_delete, shifted
from app.textlayout import page_lines

FIXTURES = Path(__file__).parent / "fixtures"
HAVE_FONTS = all(library().find(f) is not None for f in ("Calibri", "ArialMT", "TimesNewRomanPSMT"))
ADDITIONS = {source: (FIXTURES / "additions" / f"additions-{source}.pdf").read_bytes() for source in ("word", "chrome")}


def texts(pdf: bytes, page_no: int = 0) -> list[str]:
    return [l.text for l in page_lines(pymupdf.open("pdf", pdf)[page_no])]


def find(pdf: bytes, needle: str, page_no: int = 0):
    return next(l for l in page_lines(pymupdf.open("pdf", pdf)[page_no]) if comparable(l.text).startswith(comparable(needle)))


@pytest.mark.parametrize(
    "marker, step, expected",
    [
        ("3.", 1, "4."), ("9)", 1, "10)"), ("c.", 1, "d."), ("C)", 1, "D)"), ("iv.", 1, "v."), ("(2)", 1, "(3)"),
        ("٣.", 1, "٤."), ("۹.", 1, "۱۰."), (".3", 1, ".4"), ("3.", -1, "2."), ("ii.", -1, "i."),
        ("•", 1, "•"), ("z.", 1, None), ("1.", -1, "0."),
    ],
)
def test_the_next_marker(marker, step, expected):
    assert shifted(marker, step) == expected


def test_lines_know_the_marker_a_new_item_gets():
    src = ADDITIONS["word"]
    lines = page_lines(pymupdf.open("pdf", src)[0])
    got = markers(src, 0, lines)
    by_text = {l.text[:9]: got.get(l.id) for l in lines}
    assert by_text["1. Confir"] == {"below": "2.", "above": "1."}
    assert by_text["3. Book t"] == {"below": "4.", "above": "3."}
    assert by_text["• Laptops"] == {"below": "•", "above": "•"}
    assert by_text["Project K"] is None


def test_a_wrapped_item_takes_the_next_number_only_after_its_last_line():
    src = (FIXTURES / "word" / "english-styles-word.pdf").read_bytes()
    lines = page_lines(pymupdf.open("pdf", src)[1])
    got = markers(src, 1, lines)
    first, second = find(src, "1. Extend", 1), find(src, "and refunds", 1)
    assert got[first.id] == {"below": None, "above": "1."}
    assert got[second.id] == {"below": "2.", "above": None}


pytestmark = pytest.mark.skipif(not HAVE_FONTS, reason="the fonts of the test documents aren't installed")


@pytest.mark.parametrize("source", ADDITIONS)
def test_an_item_added_in_the_middle_renumbers_the_rest(source):
    src = ADDITIONS[source]
    ref = find(src, "1. Confirm")
    result = add_item(src, 0, "Call the client", ref.id, ref.text)
    assert texts(result.pdf)[1:5] == [
        "1. Confirm the delivery dates with the client",
        "2. Call the client",
        "3. Share the draft budget with finance",
        "4. Book the review meeting",
    ]
    assert result.moved > 0


def test_an_item_added_above_takes_the_items_number():
    src = ADDITIONS["word"]
    ref = find(src, "2. Share")
    result = add_item(src, 0, "2. Typed with its number", ref.id, ref.text, "above")
    assert texts(result.pdf)[1:5] == [
        "1. Confirm the delivery dates with the client",
        "2. Typed with its number",
        "3. Share the draft budget with finance",
        "4. Book the review meeting",
    ]


@pytest.mark.parametrize("source", ADDITIONS)
def test_an_arabic_item(source):
    src = ADDITIONS[source]
    ref = find(src, "1. مراجعة")
    result = add_item(src, 0, "تأكيد الموعد مع العميل", ref.id, ref.text)
    arabic = [t for t in texts(result.pdf) if t[:2] in ("1.", "2.", "3.") and any("؀" <= c <= "ۿ" for c in t)]
    assert [t[:2] for t in arabic] == ["1.", "2.", "3."]
    assert arabic[1] == "2. تأكيد الموعد مع العميل"


def test_after_a_wrapped_items_last_line():
    src = (FIXTURES / "word" / "english-styles-word.pdf").read_bytes()
    ref = find(src, "and refunds", 1)
    result = add_item(src, 1, "Train the team on the new replies.", ref.id, ref.text)
    numbered = [t for t in texts(result.pdf, 1) if t[:2] in ("1.", "2.", "3.", "4.")]
    assert [t[:2] for t in numbered] == ["1.", "2.", "3.", "4."]
    assert numbered[1] == "2. Train the team on the new replies."


def test_a_line_outside_a_list_is_refused():
    src = ADDITIONS["word"]
    ref = find(src, "The team agreed")
    with pytest.raises(EditRejected, match="list item"):
        add_item(src, 0, "Not an item", ref.id, ref.text)


def test_a_two_language_list_whose_items_take_turns():
    """English and Arabic items alternate (1., ١., 2., ٢., …): each list counts on past the
    other language's lines, in its own digits."""
    src = (FIXTURES / "chrome" / "bilingual-contract-chrome.pdf").read_bytes()
    ref = find(src, "١. النطاق")
    result = add_item(src, 0, "تأكيد الموعد مع العميل", ref.id, ref.text)
    markers_now = [t.split(" ")[0] for t in texts(result.pdf) if t[:1] in "12345١٢٣٤٥٦"]
    assert markers_now == ["1.", "١.", "٢.", "2.", "٣.", "3.", "٤.", "4.", "٥.", "5.", "٦."]


def test_deleting_items_apart_counts_each_down_by_those_above_it():
    """Delete items 1 and 3 of 5: 2, 4, 5 become 1, 2, 3 (not 2, 2, 3)."""
    src = ADDITIONS["word"]
    ref = find(src, "3. Book")
    src = add_item(src, 0, "Send the minutes", ref.id, ref.text).pdf
    ref = find(src, "4. Send")
    src = add_item(src, 0, "Plan the next meeting", ref.id, ref.text).pdf
    first, third = find(src, "1. Confirm"), find(src, "3. Book")
    targets = [(0, first.id, first.text), (0, third.id, third.text)]
    result = renumber_after_delete(src, delete_lines(src, targets), targets)
    english = [t for t in texts(result.pdf) if t[:2] in ("1.", "2.", "3.", "4.", "5.") and t.isascii()]
    assert english == [
        "1. Share the draft budget with finance",
        "2. Send the minutes",
        "3. Plan the next meeting",
    ]


def test_deleting_an_item_counts_the_rest_down():
    src = ADDITIONS["word"]
    gone = find(src, "2. Share")
    targets = [(0, gone.id, gone.text)]
    result = renumber_after_delete(src, delete_lines(src, targets), targets)
    assert texts(result.pdf)[1:3] == ["1. Confirm the delivery dates with the client", "2. Book the review meeting"]
    assert not result.warnings
