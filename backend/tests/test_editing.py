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


def _letters_near(src: bytes, page_no: int, line, word: str):
    """(size, baseline) of each letter of a word on the line, left to right."""
    page = pymupdf.open("pdf", src)[page_no]
    box = pymupdf.Rect(line.bbox) + (-1, -3, 1, 3)
    chars = sorted(
        ((c[2][0], chr(c[0]), round(s["size"], 2), round(c[2][1], 1)) for s in page.get_texttrace() for c in s["chars"]
         if c[0] > 32 and box.contains(pymupdf.Point(c[2]))),
    )
    text = "".join(c[1] for c in chars)
    at = text.index(word)
    return [c[2:] for c in chars[at : at + len(word)]]


@needs_fonts
@pytest.mark.skipif(library().find("Calibri") is None, reason="Calibri isn't installed")
@pytest.mark.parametrize(
    "old, new, like",
    [
        ("3rd", "21st", [0, 0, 1, 1]),  # "st" stays raised and small
        ("CO2", "CO3", [0, 1, 2]),  # the "3" stays lowered
        ("EXECUTIVE", "EXECUTIVES", [0, 1, 1, 1, 1, 1, 1, 1, 1, 1]),  # small capitals
    ],
)
def test_letters_keep_their_own_size_and_height(old, new, like):
    """Each new letter looks like the old letter it lines up with."""
    src = pdf_path("word", "english-styles").read_bytes()
    page_no, line = find_line(src, old)
    before = _letters_near(src, page_no, line, old)
    result = replace_line(src, page_no, line.id, line.text, line.text.replace(old, new))
    after = _letters_near(result.pdf, page_no, find_line(result.pdf, new)[1], new)
    assert after == [before[i] for i in like]


@needs_fonts
def test_a_different_installed_font_is_reported(monkeypatch):
    """If the installed "ArialMT" isn't the font in the PDF, the user is told."""
    lib = library()
    verdana = lib.find("Verdana")
    real_find = lib.find
    monkeypatch.setattr(lib, "find", lambda name: verdana if name == "ArialMT" else real_find(name))
    src = pdf_path("word", "arabic-quotation").read_bytes()
    page_no, line = find_line(src, "Q-2026-0417")
    try:
        result = replace_line(src, page_no, line.id, line.text, line.text.replace("0417", "0533"))
    except EditRejected:
        return  # refusing is also acceptable
    assert any("isn't exactly the version" in w for w in result.warnings)


def _check_all(pdf: bytes, expected: list[str]):
    pdf_doc, doc = pikepdf.open(__import__("io").BytesIO(pdf)), pymupdf.open("pdf", pdf)
    for i in range(len(pdf_doc.pages)):
        assert matches_trace(replay(pdf_doc, i), doc[i])
    for text in expected:
        assert find_line(pdf, text) is not None, f"lost: {text}"


@needs_fonts
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("doc", ["arabic-quotation", "french-quote", "english-styles", "bilingual-contract"])
def test_many_edits_in_a_row(source, doc):
    """Each edit runs on the previous result: pages already rewritten by the editor."""
    pdf = pdf_path(source, doc).read_bytes()
    done = []
    for e in [e for e in EDITS if e["doc"] == doc]:
        hit = find_line(pdf, e["old"])
        if hit is None or REFUSED.get((source, e["kind"], doc)):
            continue
        page_no, line = hit
        new = comparable(line.text).replace(comparable(e["old"]), e["new"])
        pdf = replace_line(pdf, page_no, line.id, line.text, new).pdf
        done.append(e["new"])
        _check_all(pdf, done)


@needs_fonts
def test_same_line_twice_then_its_neighbour():
    pdf = pdf_path("word", "arabic-quotation").read_bytes()
    for old, new in (("0417", "0533"), ("0533", "0999"), ("Q-2026", "Q-2027")):
        page_no, line = find_line(pdf, old)
        pdf = replace_line(pdf, page_no, line.id, line.text, line.text.replace(old, new)).pdf
    page_no, line = find_line(pdf, "21/10/2026")  # the line just below
    pdf = replace_line(pdf, page_no, line.id, line.text, line.text.replace("21/10/2026", "01/01/2027")).pdf
    _check_all(pdf, ["Q-2027-0999", "01/01/2027"])


def test_undo_undo_then_a_new_edit(client=None):
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from conftest import upload
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        data = pdf_path("word", "english-styles").read_bytes()
        doc = upload(client, data, name="r.pdf").json()

        def edit(doc, old, new):
            lines = client.get(f"/api/documents/{doc['id']}/pages/0/lines").json()["lines"]
            line = next(l for l in lines if old in l["text"])
            body = {"revision": doc["revision"], "page": 0, "lineId": line["id"], "oldText": line["text"], "newText": line["text"].replace(old, new)}
            r = client.post(f"/api/documents/{doc['id']}/edits", json=body)
            assert r.status_code == 200, r.text
            return r.json()["document"]

        def undo(doc):
            return client.post(f"/api/documents/{doc['id']}/undo", json={"revision": doc["revision"]}).json()

        def has(doc, text):
            return any(text in l["text"] for l in client.get(f"/api/documents/{doc['id']}/pages/0/lines").json()["lines"])

        doc = edit(doc, "Quarterly", "Annual")
        doc = edit(doc, "NW-Q3-2026-118", "NW-Q3-2026-200")
        doc = undo(undo(doc))
        assert has(doc, "Quarterly Service Report") and has(doc, "NW-Q3-2026-118") and not doc["canUndo"]
        doc = edit(doc, "Quarterly", "Yearly")  # the undone versions are dropped
        assert has(doc, "Yearly Service Report") and has(doc, "NW-Q3-2026-118")
        doc = undo(doc)
        assert has(doc, "Quarterly Service Report") and not doc["canUndo"]
