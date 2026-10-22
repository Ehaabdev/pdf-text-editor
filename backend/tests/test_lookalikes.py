"""Look-alike fonts: when a PDF's font isn't installed, a bundled free font that looks
like it draws the new letters, and the user is told so they can check the result."""

import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path

import pikepdf
import pymupdf
import pytest

from app import fonts as fontlib
from app.adding import add_line
from app.editing import comparable, replace_line
from app.textlayout import page_lines

FIXTURES = Path(__file__).parent / "fixtures"
LOOKALIKES = Path(__file__).resolve().parents[1] / "lookalikes"
needs_fonts = pytest.mark.skipif(
    fontlib.library().find("Calibri") is None or fontlib.library().find("TraditionalArabic") is None,
    reason="the fonts of the test documents aren't installed",
)


def find(src: bytes, needle: str):
    for page in pymupdf.open("pdf", src):
        for line in page_lines(page):
            if comparable(needle) in comparable(line.text):
                return page.number, line
    raise AssertionError(f"no line with {needle!r}")


class _Hiding:
    """The font library, with some fonts missing (as on a server without them)."""

    def __init__(self, real, hidden: tuple[str, ...]):
        self.real, self.hidden = real, hidden

    def find(self, name: str):
        plain = name.split("+")[-1].lower()
        if any(plain == h or (h.endswith("*") and plain.startswith(h[:-1])) for h in self.hidden):
            return None
        return self.real.find(name)

    def __getattr__(self, key):
        return getattr(self.real, key)


@pytest.fixture
def hide(monkeypatch):
    def hide(*names: str):
        hiding = _Hiding(fontlib.library(), tuple(n.lower() for n in names))
        monkeypatch.setattr(fontlib, "library", lambda: hiding)

    return hide


def _fonts_on(pdf: bytes, page_no: int) -> set[str]:
    with pikepdf.open(io.BytesIO(pdf)) as doc:
        res = doc.pages[page_no].obj.Resources.get("/Font") or {}
        return {str(f.get("/BaseFont", "")).split("+")[-1] for f in res.values()}


# The fonts that ship with the code ----------------------------------------------------------


def test_the_bundled_fonts_are_what_the_manifest_says():
    manifest = json.loads((LOOKALIKES / "MANIFEST.json").read_text(encoding="utf-8"))
    assert len(manifest) >= 20
    for item in manifest:
        data = (LOOKALIKES / item["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"], item["file"]
        assert (LOOKALIKES / item["licence"]).exists(), item["licence"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git isn't available")
def test_the_bundled_fonts_are_in_the_repository():
    """They must reach a server: a font folder that git ignores would pass every test here."""
    inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=LOOKALIKES.parent, capture_output=True, text=True)
    if inside.stdout.strip() != "true":
        pytest.skip("not a git checkout")
    tracked = subprocess.run(["git", "ls-files", "lookalikes"], cwd=LOOKALIKES.parent, capture_output=True, text=True).stdout.split()
    names = {Path(t).name for t in tracked}
    manifest = json.loads((LOOKALIKES / "MANIFEST.json").read_text(encoding="utf-8"))
    for item in manifest:
        assert item["file"] in names and item["licence"] in names


# Choosing the look-alike ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "font, arabic, expected",
    [
        ("Calibri", False, "Carlito-Regular"),
        ("Calibri-BoldItalic", False, "Carlito-BoldItalic"),
        ("Cambria-Bold", False, "Caladea-Bold"),
        ("ArialMT", False, "LiberationSans"),
        ("Arial-BoldMT", False, "LiberationSans-Bold"),
        ("TimesNewRomanPSMT", False, "LiberationSerif"),
        ("CourierNewPSMT", False, "LiberationMono"),
        ("ArialMT", True, "MarkaziText-Regular"),
        ("TraditionalArabic", True, "Amiri-Regular"),
        ("TraditionalArabic,Bold", True, "Amiri-Bold"),
        ("SimplifiedArabic", True, "MarkaziText-Regular"),
        ("Tahoma-Bold", True, "IBMPlexSansArabic-Bold"),
    ],
)
def test_the_look_alike_has_the_same_weight_and_style(font, arabic, expected):
    assert fontlib.lookalike(font, arabic).entry.ps_name == expected


@pytest.mark.parametrize("font, arabic", [("Tahoma", False), ("Verdana", False), ("Aptos", False), ("TraditionalArabic,Italic", True), ("Calibri-Light", False)])
def test_no_look_alike_where_none_is_close(font, arabic):
    assert fontlib.lookalike(font, arabic) is None


# Editing with a look-alike --------------------------------------------------------------------


@needs_fonts
def test_with_the_real_font_installed_no_look_alike_is_used():
    src = (FIXTURES / "word" / "english-styles-word.pdf").read_bytes()
    page_no, line = find(src, "passed complex cases")
    result = replace_line(src, page_no, line.id, line.text, line.text.replace("passed complex", "handed jazzy"))
    assert result.lookalikes == []


@needs_fonts
def test_a_missing_latin_font_is_replaced_by_its_look_alike_with_the_same_widths(hide):
    src = (FIXTURES / "word" / "english-styles-word.pdf").read_bytes()
    page_no, line = find(src, "Figures are rounded")  # Calibri Italic
    new = line.text.replace("one decimal place", "jazzy quick waves")
    real = replace_line(src, page_no, line.id, line.text, new)  # the real Calibri Italic
    hide("Calibri*")
    result = replace_line(src, page_no, line.id, line.text, new)
    assert result.lookalikes == [{"font": "Calibri Italic", "substitute": "Carlito", "sameWidths": True}]
    assert any("Carlito" in w for w in result.warnings)
    assert "Carlito-Italic" in _fonts_on(result.pdf, page_no)
    _, with_real = find(real.pdf, "jazzy quick waves")
    _, with_lookalike = find(result.pdf, "jazzy quick waves")
    assert comparable(with_lookalike.text) == comparable(new)
    assert abs(with_lookalike.bbox[2] - with_real.bbox[2]) < 0.5  # same widths, same line


@needs_fonts
def test_a_missing_bold_font_gets_the_bold_look_alike(hide):
    src = (FIXTURES / "word" / "english-styles-word.pdf").read_bytes()
    page_no, line = find(src, "Overall delivery rates improved")
    hide("calibri-bold")  # regular Calibri is there; it mustn't stand in for the bold
    result = replace_line(src, page_no, line.id, line.text, line.text.replace("improved", "jazzed"))
    assert result.lookalikes == [{"font": "Calibri Bold", "substitute": "Carlito", "sameWidths": True}]
    assert "Carlito-Bold" in _fonts_on(result.pdf, page_no)


@needs_fonts
def test_a_missing_arabic_font_is_replaced_by_a_similar_one(hide):
    src = (FIXTURES / "word" / "arabic-quotation-word.pdf").read_bytes()
    page_no, line = find(src, "الطلبات في أسرع وقت ممكن")  # Traditional Arabic
    hide("traditionalarabic*")
    result = replace_line(src, page_no, line.id, line.text, line.text.replace("أسرع", "أقرب"))
    assert result.lookalikes == [{"font": "Traditional Arabic", "substitute": "Amiri", "sameWidths": False}]
    _, new = find(result.pdf, "الطلبات في أقرب وقت ممكن")
    assert comparable(new.text) == comparable(line.text.replace("أسرع", "أقرب"))


@needs_fonts
def test_an_added_line_uses_the_look_alike_too(hide):
    src = (FIXTURES / "word" / "english-styles-word.pdf").read_bytes()
    page_no, line = find(src, "Figures are rounded")
    hide("Calibri*")
    result = add_line(src, page_no, "Jazzy quick waves by the sea.", line.id, line.text)
    assert result.lookalikes and result.lookalikes[0]["substitute"] == "Carlito"
    find(result.pdf, "Jazzy quick waves by the sea.")


@pytest.mark.parametrize("font", ["TraditionalArabic", "TraditionalArabic,Bold", "SimplifiedArabic", "ArialMT", "Tahoma", "Tahoma-Bold"])
def test_arabic_look_alikes_draw_each_letter_as_one_glyph(font):
    """A font that splits a letter into a base and separate dots gives glyphs whose copy
    and search labels can't be right (ش would read as س)."""
    full = fontlib.load_full_font(fontlib.lookalike(font, True).entry)
    text = "شركة الواحة للتجارة والمقاولات ثبت جيش ظفر"
    shaped = full.shape(text, True, latin_ligatures=False, kerning=False)
    assert not [g for g in shaped if g[2] == 0], "letters split into parts"
    assert len(shaped) <= len(text)


@needs_fonts
@pytest.mark.parametrize(
    "path, needle, first, second, hidden",
    [
        # Calibri Italic hidden: "jazzy" needs letters its copy lacks, so Carlito draws them.
        ("word/english-styles-word.pdf", "Figures are rounded", ("one decimal place", "jazzy quick waves"), ("jazzy", "fuzzy"), "Calibri*"),
        # Traditional Arabic hidden: Amiri, drawn at the matching size.
        ("word/arabic-quotation-word.pdf", "الطلبات في أسرع وقت ممكن", ("أسرع", "أقرب"), ("أقرب", "أفضل"), "traditionalarabic*"),
    ],
)
def test_a_word_in_a_look_alike_can_be_edited_again(hide, path, needle, first, second, hidden):
    """The look-alike is now that word's font: a second edit draws with it exactly, at
    the size the first edit gave it, and needs no look-alike."""
    src = (FIXTURES / path).read_bytes()
    hide(hidden)
    page_no, line = find(src, needle)
    once = replace_line(src, page_no, line.id, line.text, line.text.replace(*first))
    assert once.lookalikes
    page_no, line = find(once.pdf, line.text.replace(*first))
    twice = replace_line(once.pdf, page_no, line.id, line.text, line.text.replace(*second))
    assert twice.lookalikes == []
    _, new = find(twice.pdf, line.text.replace(*second))
    assert abs((new.bbox[3] - new.bbox[1]) - (line.bbox[3] - line.bbox[1])) < 0.5  # same size: not scaled twice
