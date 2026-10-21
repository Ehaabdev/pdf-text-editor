"""Editing a line: the result reads back as typed, keeps its style, and nothing else
on the page changes (replace_line verifies that itself before returning)."""

import json
from pathlib import Path

import pikepdf
import pymupdf
import pytest

from app.editing import EditRejected, comparable, replace_line
from app.fonts import library
from app.pdfcontent import matches_trace, replay
from app.textlayout import page_lines

FIXTURES = Path(__file__).parent / "fixtures"
SOURCES = ["word", "chrome"]
EDITS = json.loads((FIXTURES / "word" / "edits.json").read_text(encoding="utf-8"))
SAMPLES = sorted((Path(__file__).resolve().parents[2] / "samples").glob("*.pdf"))
HAVE_FONTS = library().find("ArialMT") is not None and library().find("TraditionalArabic") is not None
needs_fonts = pytest.mark.skipif(not HAVE_FONTS, reason="the fonts of the test documents aren't installed")

# Edits that must be refused, and why. Everything else in edits.json must succeed.
REFUSED = {
    ("word", "longer text in justified paragraph", "french-quote"): "too long",  # the justified line is full
    ("word", "English name in a line with Arabic", "bilingual-contract"): "too long",  # Word would wrap it
    ("chrome", "English name in a line with Arabic", "bilingual-contract"): "couldn't be read",  # Chrome left glyphs unlabelled
    ("chrome", "longer text in justified paragraph", "english-styles"): "too long",
}


def pdf_path(source, doc):
    return FIXTURES / source / f"{doc}-{source}.pdf"


def find_line(src: bytes, needle: str):
    doc = pymupdf.open("pdf", src)
    for page in doc:
        for line in page_lines(page):
            if comparable(needle) in comparable(line.text):
                return page.number, line
    return None


# The page model every edit relies on -------------------------------------------------------


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*/*.pdf")) + SAMPLES, ids=lambda p: p.name)
def test_replay_agrees_with_mupdf_on_every_glyph(path):
    pdf, doc = pikepdf.open(path), pymupdf.open(path)
    for i in range(len(pdf.pages)):
        assert matches_trace(replay(pdf, i), doc[i]), f"page {i + 1}"


# The planned edits ---------------------------------------------------------------------------


@needs_fonts
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("edit", EDITS, ids=lambda e: f"{e['doc']}-{e['kind']}")
def test_planned_edit(source, edit):
    src = pdf_path(source, edit["doc"]).read_bytes()
    hit = find_line(src, edit["old"])
    if hit is None:
        pytest.skip("the text is split over two lines in this copy")
    page_no, line = hit
    new = comparable(line.text).replace(comparable(edit["old"]), edit["new"])
    refusal = REFUSED.get((source, edit["kind"], edit["doc"]))
    if refusal:
        with pytest.raises(EditRejected, match=refusal):
            replace_line(src, page_no, line.id, line.text, new)
        return
    result = replace_line(src, page_no, line.id, line.text, new)
    after = find_line(result.pdf, edit["new"])
    assert after is not None
    _, new_line = after
    assert comparable(new_line.text) == comparable(new)
    assert (new_line.style.font, round(new_line.style.size, 1), new_line.style.color) == (
        line.style.font, round(line.style.size, 1), line.style.color)


# Things that must never happen --------------------------------------------------------------------


def test_stale_line_is_refused():
    src = pdf_path("word", "english-styles").read_bytes()
    page_no, line = find_line(src, "Quarterly Service Report")
    with pytest.raises(EditRejected, match="changed since"):
        replace_line(src, page_no, line.id, "something else", "Annual Service Report")


def test_unchanged_text_returns_the_same_file():
    src = pdf_path("word", "english-styles").read_bytes()
    page_no, line = find_line(src, "Quarterly Service Report")
    assert replace_line(src, page_no, line.id, line.text, line.text).pdf == src


@needs_fonts
def test_old_text_is_really_gone():
    src = pdf_path("word", "arabic-quotation").read_bytes()
    page_no, line = find_line(src, "Q-2026-0417")
    result = replace_line(src, page_no, line.id, line.text, line.text.replace("0417", "0533"))
    doc = pymupdf.open("pdf", result.pdf)
    assert "0417" not in doc[page_no].get_text()
    assert "0417" not in "".join(chr(c[0]) for s in doc[page_no].get_texttrace() for c in s["chars"])


@needs_fonts
def test_links_survive_editing_their_line():
    src = pdf_path("word", "english-styles").read_bytes()
    page_no, line = find_line(src, "reports@example.com")
    before = pymupdf.open("pdf", src)[page_no].get_links()
    result = replace_line(src, page_no, line.id, line.text, line.text.replace("reports@", "stats@"))
    after = pymupdf.open("pdf", result.pdf)[page_no].get_links()
    assert [l.get("uri") for l in after] == [l.get("uri") for l in before]


@needs_fonts
def test_deleting_all_text_removes_the_line():
    src = pdf_path("word", "english-styles").read_bytes()
    page_no, line = find_line(src, "Report ID")
    result = replace_line(src, page_no, line.id, line.text, "")
    assert find_line(result.pdf, "Report ID") is None
