"""Accuracy checks for replacing text in a line, measured independently of the editor:
MuPDF's own text extraction, and pixels in MuPDF and PDFium.

Written in step 1 against a prototype, with its gaps marked xfail; they now run
against the real editor (app.editing.replace_line) and pass.
"""

import io
import json
from functools import lru_cache
from pathlib import Path

import pymupdf
import pytest

from app.editing import EditRejected, replace_line
from app.fonts import library
from app.textlayout import page_lines
from fidelity import (
    changed_lines,
    find_line,
    ink_agreement,
    ink_chars,
    line_text,
    pixels_changed_outside,
    render_mupdf,
    render_pdfium,
    undrawable_chars,
)

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"

PARAGRAPH = [
    "Our platform helps teams delight customers with remarkable service across",
    "every channel they use. Messages, calls and chat arrive in one shared view,",
    "so nothing gets lost and every reply is quick, friendly and well informed.",
    "Pricing is listed below.",
]


ARIAL = library().find("ArialMT")


def build_fixture() -> bytes:
    """A Word-like PDF: subset embedded font (Arial), full-page background image,
    a link, and a justified paragraph."""
    doc = pymupdf.open()
    arial = Path(ARIAL.path).read_bytes()
    font = pymupdf.Font(fontbuffer=arial)

    cover = doc.new_page(width=595, height=842)
    bg = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 56), False)
    bg.set_rect(bg.irect, (30, 214, 125))
    cover.insert_image(cover.rect, pixmap=bg)
    tw = pymupdf.TextWriter(cover.rect)
    tw.append((40, 380), "From: Sample Sender", font=font, fontsize=14)
    tw.append((40, 400), "Email: sender@example.com", font=font, fontsize=14)
    tw.append((40, 420), "Date: 09/2/2026", font=font, fontsize=14)
    tw.write_text(cover)
    link_x0 = 40 + font.text_length("Email: ", 14)
    link_x1 = 40 + font.text_length("Email: sender@example.com", 14)
    cover.insert_link(
        {"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(link_x0, 388, link_x1, 403), "uri": "mailto:sender@example.com"}
    )

    # Justified like Word writes it: one TJ per line, extra space as kerning after each space.
    body = doc.new_page(width=595, height=842)
    body.insert_font(fontname="F1", fontbuffer=arial)
    width, size = 420, 12
    gid = lambda s: "".join(f"{font.has_glyph(ord(c)):04x}" for c in s)
    ops = []
    for i, line in enumerate(PARAGRAPH):
        words = line.split(" ")
        last = i == len(PARAGRAPH) - 1
        extra = 0 if last else (width - font.text_length(line, size)) / (len(words) - 1)
        assert extra >= 0
        parts = []
        for j, w in enumerate(words):
            parts.append(f"<{gid(w if j == len(words) - 1 else w + ' ')}>")
            if extra and j < len(words) - 1:
                parts.append(f"{-extra * 1000 / size:.3f}")
        ops.append(f"BT /F1 {size} Tf 1 0 0 1 54 {842 - (140 + i * 19.5):.2f} Tm [{' '.join(parts)}] TJ ET")
    contents = doc.get_new_xref()
    doc.update_object(contents, "<<>>")
    doc.update_stream(contents, "\n".join(ops).encode())
    doc.xref_set_key(body.xref, "Contents", f"{contents} 0 R")

    doc.subset_fonts()
    return doc.tobytes(garbage=3, deflate=True)


def real_replace(pdf: bytes, pno: int, needle: str, old: str, new: str):
    """The app's editor, used the way the website uses it."""
    doc = pymupdf.open("pdf", pdf)
    line = next(l for l in page_lines(doc[pno]) if needle in l.text)
    new_text = line.text.replace(old, new)
    result = replace_line(pdf, pno, line.id, line.text, new_text)
    return result.pdf, pymupdf.Rect(line.bbox), new_text


def edit_and_measure(pdf: bytes, pno: int, needle: str, old: str, new: str) -> dict:
    out, rect, expected = real_replace(pdf, pno, needle, old, new)
    with pymupdf.open("pdf", pdf) as a, pymupdf.open("pdf", out) as b:
        before, after = a[pno], b[pno]
        old_line, new_line = find_line(before, needle), find_line(after, new)
        o_ink, n_ink = ink_chars(old_line), ink_chars(new_line)
        o_span, n_span = old_line["spans"][0], new_line["spans"][0]
        m = {
            "expected": expected,
            "old_text": line_text(old_line),
            "new_text": line_text(new_line),
            "changed": changed_lines(before, after),
            "origin_shift": max(abs(p - q) for p, q in zip(o_ink[0]["origin"], n_ink[0]["origin"])),
            "right_edge_shift": abs(o_ink[-1]["bbox"][2] - n_ink[-1]["bbox"][2]),
            "size": (o_span["size"], n_span["size"]),
            "color": (o_span["color"], n_span["color"]),
            "font": (o_span["font"], n_span["font"]),
            "links": (before.get_links(), after.get_links()),
        }
    m["mupdf_outside"] = pixels_changed_outside(render_mupdf(pdf, pno), render_mupdf(out, pno), rect)
    m["pdfium_outside"] = pixels_changed_outside(render_pdfium(pdf, pno), render_pdfium(out, pno), rect, shift_px=1)
    # Chrome's engine must draw the new text where MuPDF does, as well as it does for the original.
    m["engines_agree_before"] = ink_agreement(render_mupdf(pdf, pno), render_pdfium(pdf, pno), rect)
    m["engines_agree_after"] = ink_agreement(render_mupdf(out, pno), render_pdfium(out, pno), rect)
    return m


def same_words(a: str, b: str) -> bool:
    """Equal up to how spaces are written: the file's label for its space glyph, and
    the extra space MuPDF reports for a wide justified gap."""
    return a.replace(" ", " ").split() == b.replace(" ", " ").split()


def same_font_name(a: str, b: str) -> bool:
    norm = lambda n: n.split("+")[-1].replace(" ", "").replace("-", "").lower().removesuffix("regular")
    return norm(a) == norm(b)


def assert_invisible(m: dict) -> None:
    """Everything except exact space characters and the right edge, which have their own tests."""
    # The file's own label for its space glyph; MuPDF adds a space for wide justified gaps.
    nb = lambda t: " ".join(t.replace("\xa0", " ").split()) if t else t
    assert [(nb(o), nb(n)) for o, n in m["changed"]] == [(nb(m["old_text"]), nb(m["expected"]))]
    assert m["origin_shift"] < 0.01
    assert m["size"][0] == m["size"][1]
    assert m["color"][0] == m["color"][1]
    assert same_font_name(*m["font"])
    assert m["mupdf_outside"] == 0
    assert m["pdfium_outside"] == 0
    assert m["engines_agree_after"] >= m["engines_agree_before"] - 0.05


@pytest.fixture(scope="module")
def fixture_pdf() -> bytes:
    if ARIAL is None:
        pytest.skip("Arial isn't installed")
    return build_fixture()




# Generated fixture -------------------------------------------------------------


def test_subset_font_check_sees_removed_glyphs(fixture_pdf):
    """The editor only reuses an embedded subset for glyphs it really contains."""
    import pikepdf
    from app.fonts import EmbeddedFont, load_full_font

    pdf = pikepdf.open(io.BytesIO(fixture_pdf))
    font = next(iter(pdf.pages[1].Resources.Font.values()))
    subset, full = EmbeddedFont(font), load_full_font(ARIAL)
    used = [full.cmap and full.tt.getGlyphID(full.cmap[ord(c)]) for c in "wonderful"]
    unused = [full.tt.getGlyphID(full.cmap[ord(c)]) for c in "Zé"]
    assert all(subset.has_same_glyph(g, full) for g in used)
    assert not any(subset.has_same_glyph(g, full) for g in unused)


def test_justified_line_edit_is_invisible(fixture_pdf):
    m = edit_and_measure(fixture_pdf, 1, "remarkable", "remarkable", "wonderful")
    assert_invisible(m)
    assert same_words(m["new_text"], m["expected"])
    assert m["right_edge_shift"] < 0.25


def test_plain_line_edit_on_background_image_is_invisible(fixture_pdf):
    assert_invisible(edit_and_measure(fixture_pdf, 0, "Date:", "09/2/2026", "29/6/2026"))


def test_refuses_characters_the_subset_font_cannot_draw(fixture_pdf, monkeypatch):
    # As on a server without Arial: the fixture never uses the digit 1, so it can't be drawn.
    monkeypatch.setattr(library(), "find", lambda name: None)
    with pytest.raises(EditRejected, match="doesn't contain all the letters"):
        real_replace(fixture_pdf, 0, "Date:", "09/2/2026", "10/2/2026")


def test_digits_already_in_the_subset_work_without_the_installed_font(fixture_pdf, monkeypatch):
    monkeypatch.setattr(library(), "find", lambda name: None)
    assert_invisible(edit_and_measure(fixture_pdf, 0, "Date:", "09/2/2026", "29/6/2026"))


def test_with_the_installed_font_any_digit_works(fixture_pdf):
    assert_invisible(edit_and_measure(fixture_pdf, 0, "Date:", "09/2/2026", "10/2/2026"))


def test_plain_line_edit_keeps_its_spaces(fixture_pdf):
    # The space is the original glyph, moved if needed: it keeps the file's own label
    # (this generator labels it U+00A0). Step 1's prototype turned plain spaces into U+00A0.
    m = edit_and_measure(fixture_pdf, 0, "Date:", "09/2/2026", "29/6/2026")
    assert m["new_text"].replace("29/6", "09/2") == m["old_text"]


def test_link_on_another_line_survives(fixture_pdf):
    m = edit_and_measure(fixture_pdf, 0, "Date:", "09/2/2026", "29/6/2026")
    assert len(m["links"][0]) == len(m["links"][1]) == 1


def test_link_on_edited_line_follows_the_address(fixture_pdf):
    m = edit_and_measure(fixture_pdf, 0, "Email:", "sender", "orders")
    assert [l["uri"] for l in m["links"][1]] == ["mailto:orders@example.com"]


# Private real-world PDFs (local only) ---------------------------------------------
#
# samples/ is never committed. samples/cases.json lists edits to try on its PDFs:
#   [{"file": "x.pdf", "page": 0, "line": "text that finds the line", "old": "a", "new": "b",
#     "justified": true, "known_gaps": ["spaces", "right_edge"]}]
# A check named in known_gaps is expected to fail until step 3 fixes it.


def _cases() -> list[dict]:
    path = SAMPLES_DIR / "cases.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _params(check: str):
    params = []
    for case in _cases():
        if check == "right_edge" and not case.get("justified"):
            continue
        marks = []
        if check in case.get("known_gaps", []):
            marks.append(pytest.mark.xfail(strict=True, reason=f"known gap until step 3: {check}"))
        params.append(pytest.param(json.dumps(case), id=f"{case['file']}-p{case['page']}-{case['old']}", marks=marks))
    return params or [pytest.param(None, marks=pytest.mark.skip(reason="no private samples"))]


@lru_cache(maxsize=None)
def _measure(case_json: str) -> dict:
    case = json.loads(case_json)
    pdf = (SAMPLES_DIR / case["file"]).read_bytes()
    return edit_and_measure(pdf, case["page"], case["line"], case["old"], case["new"])


@pytest.mark.parametrize("case", _params("invisible"))
def test_sample_edit_is_invisible(case):
    assert_invisible(_measure(case))


@pytest.mark.parametrize("case", _params("spaces"))
def test_sample_edit_keeps_real_spaces(case):
    m = _measure(case)
    assert same_words(m["new_text"], m["expected"])


@pytest.mark.parametrize("case", _params("right_edge"))
def test_sample_justified_edit_keeps_right_edge(case):
    assert _measure(case)["right_edge_shift"] < 0.25
