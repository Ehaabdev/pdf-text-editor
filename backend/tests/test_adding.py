"""Adding a new line under (or over) a line: its style, place, list numbering, and the
places where it must be refused."""

import io
from pathlib import Path

import pikepdf
import pymupdf
import pytest

from app.adding import _item_of, add_line
from app.editing import EditRejected, comparable
from app.fonts import library
from app.pdfcontent import replay
from app.textlayout import page_lines

FIXTURES = Path(__file__).parent / "fixtures"
HAVE_FONTS = all(library().find(f) is not None for f in ("Calibri", "ArialMT", "TimesNewRomanPSMT"))
pytestmark = pytest.mark.skipif(not HAVE_FONTS, reason="the fonts of the test documents aren't installed")


def find(src: bytes, needle: str):
    for page in pymupdf.open("pdf", src):
        for line in page_lines(page):
            if line.text.startswith(needle):
                return page.number, line
    return None


def added(src: bytes, result: bytes, page_no: int, text: str):
    """The new line, read back from the result."""
    lines = [l for l in page_lines(pymupdf.open("pdf", result)[page_no]) if comparable(l.text) == comparable(text)]
    assert len(lines) == 1, [l.text for l in page_lines(pymupdf.open("pdf", result)[page_no])]
    return lines[0]


def baseline(src: bytes, page_no: int, line) -> float:
    from app.adding import _baseline

    with pikepdf.open(io.BytesIO(src)) as pdf:
        return _baseline(line, replay(pdf, page_no))


# Lists, alignment and spacing ----------------------------------------------------------------


ADDITIONS = {source: (FIXTURES / "additions" / f"additions-{source}.pdf").read_bytes() for source in ("word", "chrome")}


@pytest.mark.parametrize("source", ADDITIONS)
def test_a_new_list_item_lines_up_with_the_items_above(source):
    src = ADDITIONS[source]
    page_no, ref = find(src, "3. Book")
    _, above = find(src, "2. Share")
    result = add_line(src, page_no, "4. Send the minutes to everyone", ref.id, ref.text)
    new = added(src, result.pdf, page_no, "4. Send the minutes to everyone")
    rp_old = replay(pikepdf.open(io.BytesIO(src)), page_no)
    rp_new = replay(pikepdf.open(io.BytesIO(result.pdf)), page_no)
    item_ref, item_new = _item_of(ref, rp_old), _item_of(new, rp_new)
    assert abs(item_new.marker_edges[0] - item_ref.marker_edges[0]) < 0.5  # the number where the numbers are
    assert abs(item_new.text_edge - item_ref.text_edge) < 0.5  # the text at the item's tab
    assert (new.style.font, round(new.style.size, 1), new.style.color) == (ref.style.font, round(ref.style.size, 1), ref.style.color)
    # One line spacing below, the same as between the items above.
    spacing = baseline(src, page_no, above) - baseline(src, page_no, ref)
    assert abs((baseline(src, page_no, ref) - baseline(result.pdf, page_no, new)) - spacing) < 0.1


@pytest.mark.parametrize("source", ADDITIONS)
def test_more_text_under_a_list_item_lines_up_with_its_text(source):
    src = ADDITIONS[source]
    page_no, ref = find(src, "3. Book")
    result = add_line(src, page_no, "Ask the client about the date", ref.id, ref.text)
    new = added(src, result.pdf, page_no, "Ask the client about the date")
    item = _item_of(ref, replay(pikepdf.open(io.BytesIO(src)), page_no))
    assert abs(_pdf_x0(result.pdf, page_no, new) - item.text_edge) < 0.5


def _pdf_x0(pdf: bytes, page_no: int, line) -> float:
    """The left edge of the line's visible letters (PDF x)."""
    with pikepdf.open(io.BytesIO(pdf)) as doc:
        rp = replay(doc, page_no)
        return min(rp.glyphs[g.traces[0]].origin[0] for g in line.glyphs if g.traces and not g.is_space)


def test_a_new_bullet_is_the_lists_own_bullet():
    src = ADDITIONS["word"]  # Chrome draws its bullets as shapes, not text
    page_no, ref = find(src, "• Access")
    result = add_line(src, page_no, "• Monitors and keyboards", ref.id, ref.text)
    new = added(src, result.pdf, page_no, "• Monitors and keyboards")
    assert abs(new.bbox[0] - ref.bbox[0]) < 0.5


@pytest.mark.parametrize("source", ADDITIONS)
def test_a_new_arabic_list_item(source):
    src = ADDITIONS[source]
    page_no, ref = find(src, "2. إرسال")
    result = add_line(src, page_no, "3. تحديد موعد الاجتماع التالي", ref.id, ref.text)
    new = added(src, result.pdf, page_no, "3. تحديد موعد الاجتماع التالي")
    assert new.rtl and abs(new.bbox[2] - ref.bbox[2]) < 0.5  # the numbers' right edge
    item_ref = _item_of(ref, replay(pikepdf.open(io.BytesIO(src)), page_no))
    item_new = _item_of(new, replay(pikepdf.open(io.BytesIO(result.pdf)), page_no))
    assert abs(item_new.text_edge - item_ref.text_edge) < 0.5


@pytest.mark.parametrize("source", ADDITIONS)
@pytest.mark.parametrize(
    "needle, text, edge",
    [
        ("week ahead", "A new sentence after the paragraph.", "left"),  # justified: ends start-aligned
        ("the week ahead", "A new sentence after the paragraph.", "left"),
        ("Prepared by", "Reviewed by the steering group", "right"),  # right-aligned stays so
        ("ملاحظة", "شكراً لتعاونكم.", "right"),  # Arabic starts at the right
    ],
)
def test_a_new_line_keeps_its_paragraphs_alignment(source, needle, text, edge):
    src = ADDITIONS[source]
    hit = find(src, needle)
    if hit is None:
        pytest.skip("not a line of its own in this copy")
    page_no, ref = hit
    result = add_line(src, page_no, text, ref.id, ref.text)
    new = added(src, result.pdf, page_no, text)
    k = 0 if edge == "left" else 2
    assert abs(new.bbox[k] - ref.bbox[k]) < 0.5
    assert (new.style.font, new.style.color) == (ref.style.font, ref.style.color)


@pytest.mark.parametrize("source", ADDITIONS)
@pytest.mark.parametrize(
    "needle, why",
    [
        ("Project Kickoff", "overlap other text"),  # the list is right under the title
        ("Planning", "table cell"),  # the table's border is under it
        ("2. Share", "overlap other text"),  # the next item is under it
    ],
)
def test_no_room_is_refused_with_the_reason(source, needle, why):
    src = ADDITIONS[source]
    page_no, ref = find(src, needle)
    with pytest.raises(EditRejected, match=why):
        add_line(src, page_no, "Some new text", ref.id, ref.text)


def test_too_long_is_refused():
    src = ADDITIONS["word"]
    page_no, ref = find(src, "Prepared by")
    with pytest.raises(EditRejected, match="too long"):
        add_line(src, page_no, "word " * 60, ref.id, ref.text)


def test_a_line_above():
    src = ADDITIONS["word"]
    page_no, ref = find(src, "ملاحظة")
    result = add_line(src, page_no, "ملاحظات إضافية", ref.id, ref.text, where="above")
    new = added(src, result.pdf, page_no, "ملاحظات إضافية")
    assert new.bbox[3] < ref.bbox[1] and abs(new.bbox[2] - ref.bbox[2]) < 0.5


# The other test documents -------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, needle, text",
    [
        ("word/english-styles-word.pdf", "Figures are rounded", "Source: the support team's own records."),
        ("decorations/decorations-word.pdf", "Ligne française", "Une nouvelle ligne, avec des accents : é è à ç."),
        ("decorations/decorations-chrome.pdf", "Ligne française", "Une nouvelle ligne, avec des accents : é è à ç."),
        ("word/arabic-quotation-word.pdf", "للاستفسار", "سطر جديد للتواصل مع فريق المبيعات"),
    ],
)
def test_adding_after_the_last_line_of_a_paragraph(path, needle, text):
    src = (FIXTURES / path).read_bytes()
    page_no, ref = find(src, needle)
    result = add_line(src, page_no, text, ref.id, ref.text)
    new = added(src, result.pdf, page_no, text)
    assert (new.style.font, round(new.style.size, 1), new.style.color) == (ref.style.font, round(ref.style.size, 1), ref.style.color)
    assert new.bbox[1] > ref.bbox[3]


def test_the_page_is_otherwise_untouched():
    """The page's own instructions are kept byte for byte; the new line comes after them."""
    src = ADDITIONS["word"]
    page_no, ref = find(src, "Prepared by")
    result = add_line(src, page_no, "Reviewed by the steering group", ref.id, ref.text)
    with pikepdf.open(io.BytesIO(src)) as pdf:
        before = pdf.pages[page_no].obj.Contents.read_bytes()
    with pikepdf.open(io.BytesIO(result.pdf)) as pdf:
        streams = pdf.pages[page_no].obj.Contents
        assert isinstance(streams, pikepdf.Array) and streams[1].read_bytes() == before


def test_an_added_line_can_be_edited_and_deleted():
    from app.deleting import delete_lines
    from app.editing import replace_line

    src = ADDITIONS["word"]
    page_no, ref = find(src, "Prepared by")
    pdf = add_line(src, page_no, "Reviewed by the steering group", ref.id, ref.text).pdf
    _, new = find(pdf, "Reviewed by")
    pdf = replace_line(pdf, page_no, new.id, new.text, "Approved by the steering group").pdf
    _, new = find(pdf, "Approved by")
    pdf = delete_lines(pdf, [(page_no, new.id, new.text)]).pdf
    assert find(pdf, "Approved by") is None and find(pdf, "Prepared by") is not None


# Turned and cropped pages ---------------------------------------------------------------------


@pytest.mark.parametrize("rotation, crop", [(90, None), (270, None), (0, (40, 40, 560, 500)), (90, (40, 40, 560, 500))])
def test_adding_on_turned_and_cropped_pages(rotation, crop):
    doc = pymupdf.open(FIXTURES / "decorations" / "decorations-word.pdf")
    if crop:
        doc[0].set_cropbox(pymupdf.Rect(crop))
    doc[0].set_rotation(rotation)
    src = doc.tobytes()
    page_no, ref = find(src, "Ligne française")
    result = add_line(src, page_no, "Une nouvelle ligne.", ref.id, ref.text)
    added(src, result.pdf, page_no, "Une nouvelle ligne.")
