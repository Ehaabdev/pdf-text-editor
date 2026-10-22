"""Deleting lines, and the underlines, strikethroughs, highlights and links that belong
to text: they go when their text is deleted and follow it when it's edited, while the
page's own lines (table borders, paragraph borders, the footnote separator) stay."""

import io
from pathlib import Path

import pikepdf
import pymupdf
import pytest

from app import decorations
from app.deleting import delete_lines
from app.editing import EditRejected, comparable, replace_line
from app.fonts import library
from app.pdfcontent import replay
from app.textlayout import page_lines

FIXTURES = Path(__file__).parent / "fixtures"
DECORATED = {source: FIXTURES / "decorations" / f"decorations-{source}.pdf" for source in ("word", "chrome")}
HAVE_FONTS = library().find("ArialMT") is not None
needs_fonts = pytest.mark.skipif(not HAVE_FONTS, reason="Arial isn't installed")

# What each line of the decorations document carries: (text it runs under, kind).
EXPECTED = {
    "Plain line with an": [("underlined phrase", "underline")],
    "This line has a": [("struck-out", "strike")],
    "Twice struck": [("old price 450", "strike")] * 2,  # Word only: a double strikethrough
    "Double underline": [("total amount", "underline")] * 2,
    "Part of a word": [("believ", "underline"), ("colored underline", "underline")],
    "A highlighted": [("highlighted phrase", "highlight")],
    "Write to": [("help@example.com", "underline")],
    "Fully underlined": [("Fully underlined line of text", "underline")],
    "هذا سطر": [("كلمة مسطرة", "underline")],
    "وهنا": [("عبارة مظللة", "highlight")],
    "السعر": [("القديم", "strike")],
    "Ligne française": [("souligné", "underline")],
}


def find(src: bytes, needle: str):
    for page in pymupdf.open("pdf", src):
        for line in page_lines(page):
            if comparable(needle) in comparable(line.text):
                return page.number, line
    return None


def covered_text(line, d) -> str:
    return "".join(line.glyph_text(i) for i in line.order if line.glyphs[i].traces and line.glyphs[i].traces[0] in d.traces).strip()


# Telling decorations apart from the page's own lines ------------------------------------


@pytest.mark.parametrize("source", DECORATED)
def test_decorations_are_found_and_nothing_else(source):
    src = DECORATED[source].read_bytes()
    doc = pymupdf.open("pdf", src)
    rp = replay(pikepdf.open(io.BytesIO(src)), 0)
    seen = {}
    for line in page_lines(doc[0]):
        found = [(comparable(covered_text(line, d)), d.kind) for d in decorations.for_line(line, rp)]
        key = next((k for k in EXPECTED if line.text.startswith(k)), None)
        if key is None:
            # Table cells, the bordered paragraph, the footnote: their lines are the page's.
            assert found == [], line.text
        else:
            seen[key] = found
    for key, want in EXPECTED.items():
        if key == "Twice struck" and source == "chrome":
            continue  # not in the Chrome version
        assert seen.get(key) == [(comparable(t), k) for t, k in want], key


# Deleting -------------------------------------------------------------------------------


@pytest.mark.parametrize("source", DECORATED)
def test_deleting_lines_takes_their_decorations_and_links_with_them(source):
    src = DECORATED[source].read_bytes()
    lines = page_lines(pymupdf.open("pdf", src)[0])
    pick = [l for l in lines if l.text.startswith(("Plain line", "A highlighted", "Write to", "Cell text", "وهنا"))]
    assert len(pick) == 5
    before = pymupdf.open("pdf", src)[0]
    result = delete_lines(src, [(0, l.id, l.text) for l in pick])
    after = pymupdf.open("pdf", result.pdf)[0]
    texts = [l.text for l in page_lines(after)]
    for l in pick:
        assert l.text not in texts
    # The underline, the two highlights and the link's underline are gone; table borders stay.
    assert len(after.get_drawings()) == len(before.get_drawings()) - 4
    assert [l.get("uri") for l in after.get_links()] == [l.get("uri") for l in before.get_links() if l.get("uri") != "mailto:help@example.com"]


def test_an_empty_edit_is_the_same_as_deleting():
    src = DECORATED["word"].read_bytes()
    page_no, line = find(src, "Plain line with an")
    emptied = replace_line(src, page_no, line.id, line.text, "  ").pdf
    deleted = delete_lines(src, [(page_no, line.id, line.text)]).pdf
    # (The files' IDs differ: they're made from the time of saving.)
    def contents(pdf: bytes) -> bytes:
        with pikepdf.open(io.BytesIO(pdf)) as doc:
            return doc.pages[page_no].Contents.read_bytes()

    links = lambda pdf: pymupdf.open("pdf", pdf)[page_no].get_links()
    assert contents(emptied) == contents(deleted)
    assert [l.get("uri") for l in links(emptied)] == [l.get("uri") for l in links(deleted)]


def test_lines_on_several_pages_are_deleted_as_one_change():
    src = (FIXTURES / "word" / "english-styles-word.pdf").read_bytes()
    doc = pymupdf.open("pdf", src)
    first, second = page_lines(doc[0])[3], page_lines(doc[1])[2]
    result = delete_lines(src, [(0, first.id, first.text), (1, second.id, second.text)])
    out = pymupdf.open("pdf", result.pdf)
    assert first.text not in [l.text for l in page_lines(out[0])]
    assert second.text not in [l.text for l in page_lines(out[1])]


def test_lines_that_cant_be_edited_can_still_be_deleted():
    """Deleting needs no fonts or layout: a line whose letters Word stored wrongly goes too."""
    src = (FIXTURES / "word" / "bilingual-contract-word.pdf").read_bytes()
    doc = pymupdf.open("pdf", src)
    hit = next((p.number, l) for p in doc for l in page_lines(p) if l.uncertain)
    page_no, line = hit
    result = delete_lines(src, [(page_no, line.id, line.text)])
    assert line.text not in [l.text for l in page_lines(pymupdf.open("pdf", result.pdf)[page_no])]


def test_a_stale_line_is_not_deleted():
    src = DECORATED["word"].read_bytes()
    page_no, line = find(src, "Plain line with an")
    with pytest.raises(EditRejected, match="changed since"):
        delete_lines(src, [(page_no, line.id, "something else")])


# Editing: decorations follow their words --------------------------------------------------


def _decoration_under(src: bytes, page_no: int, phrase: str, kind: str = "underline"):
    """The ends (PDF x) of the decorations under a phrase, and the phrase's own ends."""
    page = pymupdf.open("pdf", src)[page_no]
    rp = replay(pikepdf.open(io.BytesIO(src)), page_no)
    for line in page_lines(page):
        for d in decorations.for_line(line, rp):
            if d.kind == kind and comparable(covered_text(line, d)) == comparable(phrase):
                return d.x0, d.x1
    return None


@needs_fonts
@pytest.mark.parametrize(
    "line_start, old, new, phrase",
    [
        ("Plain line with an", "underlined phrase", "underlined words here", "underlined words here"),
        ("Plain line with an", "underlined phrase", "underlined new phrase", "underlined new phrase"),  # added inside: continues
        ("Plain line with an", "phrase in", "phrase today in", "underlined phrase"),  # added after: doesn't
        ("Plain line with an", "Plain line with an", "A", "underlined phrase"),  # moved left with its words
        ("A highlighted", "highlighted phrase", "marked text", "marked text"),
        ("هذا سطر", "كلمة مسطرة", "عبارة مسطرة", "عبارة مسطرة"),
    ],
)
def test_decorations_follow_their_words(line_start, old, new, phrase):
    src = DECORATED["word"].read_bytes()
    page_no, line = find(src, line_start)
    kind = "highlight" if line_start == "A highlighted" else "underline"
    result = replace_line(src, page_no, line.id, line.text, line.text.replace(old, new))
    assert _decoration_under(result.pdf, page_no, phrase, kind) is not None, f"no {kind} under “{phrase}”"


@needs_fonts
def test_changing_a_partly_underlined_word_is_refused():
    src = DECORATED["word"].read_bytes()
    page_no, line = find(src, "Part of a word")
    with pytest.raises(EditRejected, match="Part of a word"):
        replace_line(src, page_no, line.id, line.text, line.text.replace("unbelievable", "incredible"))
    # Other words on the line can change; the partial underline moves with its word.
    result = replace_line(src, page_no, line.id, line.text, line.text.replace("Part of a word", "Part of one word"))
    assert _decoration_under(result.pdf, page_no, "believ") is not None


@needs_fonts
def test_a_link_follows_its_address_and_a_foreign_target_is_reported():
    src = DECORATED["word"].read_bytes()
    page_no, line = find(src, "Write to")
    result = replace_line(src, page_no, line.id, line.text, line.text.replace("help@example.com", "support@example.org"))
    assert "mailto:support@example.org" in [l.get("uri") for l in pymupdf.open("pdf", result.pdf)[page_no].get_links()]
    assert _decoration_under(result.pdf, page_no, "support@example.org") is not None
    assert result.warnings == []

    # A link whose target isn't its own text keeps its target, and the user is told.
    pdf = pikepdf.open(io.BytesIO(src))
    for a in pdf.pages[page_no].obj.Annots:
        if "/A" in a and str(a.A.get("/URI", "")) == "mailto:help@example.com":
            a.A.URI = pikepdf.String("https://example.com/contact")
    out = io.BytesIO()
    pdf.save(out)
    result = replace_line(out.getvalue(), page_no, line.id, line.text, line.text.replace("help@example.com", "support@example.org"))
    assert "https://example.com/contact" in [l.get("uri") for l in pymupdf.open("pdf", result.pdf)[page_no].get_links()]
    assert any("still opens https://example.com/contact" in w for w in result.warnings)


@needs_fonts
def test_delete_edit_delete_on_one_page():
    """Changes build on each other: every step starts from the previous result."""
    pdf = (FIXTURES / "word" / "english-styles-word.pdf").read_bytes()
    page_no, line = find(pdf, "Report ID")
    pdf = delete_lines(pdf, [(page_no, line.id, line.text)]).pdf
    page_no, line = find(pdf, "Quarterly Service Report")
    pdf = replace_line(pdf, page_no, line.id, line.text, "Annual Service Report").pdf
    page_no, line = find(pdf, "reports@example.com")
    pdf = delete_lines(pdf, [(page_no, line.id, line.text)]).pdf
    texts = [l.text for l in page_lines(pymupdf.open("pdf", pdf)[page_no])]
    assert "Annual Service Report" in texts
    assert not any("Report ID" in t or "reports@example.com" in t or "Quarterly" in t for t in texts)
    assert "mailto:reports@example.com" not in [l.get("uri") for l in pymupdf.open("pdf", pdf)[page_no].get_links()]


# Rotated and cropped pages ------------------------------------------------------------------
#
# Every check converts between the PDF's own coordinates, what's displayed, MuPDF's shapes
# and links, and PDFium's letter boxes. These pages make each of those conversions matter.


def _turned(rotation: int, crop=None) -> bytes:
    if crop == "shifted":
        # A media box that doesn't start at 0,0 (the content moves with it).
        pdf = pikepdf.open(DECORATED["word"])
        page = pdf.pages[0]
        page.obj.Contents = pdf.make_stream(b"q 1 0 0 1 100 200 cm " + page.obj.Contents.read_bytes() + b" Q")
        for a in page.obj.get("/Annots") or []:
            x0, y0, x1, y1 = (float(v) for v in a.Rect)
            a.Rect = pikepdf.Array([x0 + 100, y0 + 200, x1 + 100, y1 + 200])
            if "/QuadPoints" in a:
                a.QuadPoints = pikepdf.Array([float(v) + (100 if i % 2 == 0 else 200) for i, v in enumerate(a.QuadPoints)])
        page.obj.MediaBox = pikepdf.Array([100, 200, 695.32, 1041.92])
        page.obj.Rotate = rotation
        out = io.BytesIO()
        pdf.save(out)
        return out.getvalue()
    doc = pymupdf.open(DECORATED["word"])
    page = doc[0]
    if crop:
        page.set_cropbox(pymupdf.Rect(crop))
    if rotation:
        page.set_rotation(rotation)
    return doc.tobytes()


CROP = (40, 40, 560, 500)
PAGES = [(90, None), (180, None), (270, None), (0, CROP), (90, CROP), (270, CROP), (0, "shifted"), (90, "shifted")]


@pytest.mark.parametrize("rotation, crop", PAGES)
def test_pdfium_letters_land_on_our_lines(rotation, crop):
    """The mapping the delete check relies on: PDFium's letter boxes, shown on the page,
    fall inside the lines we found."""
    import pypdfium2 as pdfium

    from app.changes import to_display

    src = _turned(rotation, crop)
    page = pymupdf.open("pdf", src)[0]
    boxes = [pymupdf.Rect(l.bbox) + (-1, -1, 1, 1) for l in page_lines(page)]
    doc = pdfium.PdfDocument(src)
    tp = doc[0].get_textpage()
    inside = total = 0
    for i in range(tp.count_chars()):
        if not tp.get_text_range(i, 1).isalnum():
            continue
        r = to_display(page, tp.get_charbox(i))
        point = pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
        if not page.rect.contains(point):
            continue  # cropped away
        total += 1
        inside += any(point in b for b in boxes)
    doc.close()
    assert total > 100 and inside >= 0.98 * total, f"{inside} of {total}"


@pytest.mark.parametrize("rotation, crop", PAGES)
def test_deleting_on_rotated_and_cropped_pages(rotation, crop):
    src = _turned(rotation, crop)
    page_no, line = find(src, "Plain line with an")
    before = pymupdf.open("pdf", src)[0]
    result = delete_lines(src, [(page_no, line.id, line.text)])
    after = pymupdf.open("pdf", result.pdf)[0]
    assert find(result.pdf, "Plain line with an") is None
    assert len(after.get_drawings()) == len(before.get_drawings()) - 1  # its underline


@needs_fonts
@pytest.mark.parametrize("rotation, crop", PAGES)
def test_editing_on_rotated_and_cropped_pages(rotation, crop):
    src = _turned(rotation, crop)
    page_no, line = find(src, "Plain line with an")
    result = replace_line(src, page_no, line.id, line.text, line.text.replace("underlined phrase", "underlined words here"))
    assert _decoration_under(result.pdf, page_no, "underlined words here") is not None
    page_no, line = find(src, "Write to")
    result = replace_line(src, page_no, line.id, line.text, line.text.replace("help@example.com", "support@example.org"))
    assert "mailto:support@example.org" in [l.get("uri") for l in pymupdf.open("pdf", result.pdf)[0].get_links()]


@needs_fonts
def test_rotated_text_can_be_deleted():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    arial = library().find("ArialMT").path
    page.insert_text((100, 300), "Upright line of text", fontname="F1", fontfile=arial, fontsize=12)
    page.insert_text((300, 500), "Sideways label", fontname="F1", fontfile=arial, fontsize=12, rotate=90)
    src = doc.tobytes()
    page_no, line = find(src, "Sideways label")
    with pytest.raises(EditRejected, match="Rotated"):
        replace_line(src, page_no, line.id, line.text, "Sideways sign")
    result = delete_lines(src, [(page_no, line.id, line.text)])
    assert find(result.pdf, "Sideways label") is None
    assert find(result.pdf, "Upright line of text") is not None


def test_a_bullet_drawn_as_a_shape_goes_with_its_line():
    """Chrome draws list bullets as small filled circles, not text."""
    src = (FIXTURES / "additions" / "additions-chrome.pdf").read_bytes()
    page_no, line = find(src, "Access cards")
    before = pymupdf.open("pdf", src)[page_no].get_drawings()
    result = delete_lines(src, [(page_no, line.id, line.text)])
    after = pymupdf.open("pdf", result.pdf)[page_no].get_drawings()
    assert len(after) == len(before) - 1 and result.warnings == []
