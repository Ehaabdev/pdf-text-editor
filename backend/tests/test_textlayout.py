"""Text detection: every line's text, direction, style and position.

Two independent checks of the text:
  * Answer key (made-up Word documents): each line is text that was typed into the
    document, and every typed paragraph is found again, in order.
  * Round trip (any PDF, including private samples): laying out the extracted text
    with the standard bidi algorithm, as a browser or edit box does, gives the same
    order as the glyphs on the page. python-bidi is only used here, as an oracle.
"""

import json
import re
import unicodedata
from pathlib import Path

import numpy as np
import pymupdf
import pytest
from bidi import get_display

from app.textlayout import LRI, PDI, RLI, page_lines

WORD = Path(__file__).parent / "fixtures" / "word"
DOCS = ["arabic-quotation", "french-quote", "bilingual-contract", "english-styles"]
EXPECTED = json.loads((WORD / "expected.json").read_text(encoding="utf-8"))
SAMPLES = sorted((Path(__file__).resolve().parents[2] / "samples").glob("*.pdf"))

# Paragraphs where Word wrote wrong characters into the PDF for some letters (kashida
# justification; Calibri's Arabic ligatures). Their lines must be flagged uncertain.
UNREADABLE = {
    "arabic-quotation": ["نلتزم بتقديم خدمة"],
    "bilingual-contract": ["The Client’s representative"],
}

_INVISIBLE = dict.fromkeys(map(ord, "‎‏⁦⁧⁨⁩‪‫‬‭‮ـ"))
_FOOTER = re.compile(r"^(Page|صفحة)\s*[\d٠-٩]+\s*(of|sur|من|/)\s*[\d٠-٩]+$")
_MARKER = re.compile(r"^([•\-–]|[\d٠-٩]+[.)])$")


def norm(text: str, loose: bool = False) -> str:
    text = unicodedata.normalize("NFC", text).translate(_INVISIBLE)
    text = re.sub(r"[\s\u00a0\u202f\u2009]+", " ", text).strip()
    # Word draws the space at a line break inside a bracket: "( Agent" on the page.
    text = re.sub(r"([(\[{«]) ", r"\1", text)
    text = re.sub(r" ([)\]}»])", r"\1", text)
    return text.replace(" ", "").casefold() if loose else text


def all_lines(path: Path):
    with pymupdf.open(path) as doc:
        return [line for page in doc for line in page_lines(page)]


@pytest.fixture(scope="module")
def lines():
    return {name: all_lines(WORD / f"{name}-word.pdf") for name in DOCS}


def paragraphs(name):
    return [p for p in EXPECTED if p["doc"] == name]


def loose_ok(p) -> bool:
    return any(r.get("changesLetters") for r in p["runs"])


def is_unreadable(name, text) -> bool:
    return any(marker in text for marker in UNREADABLE.get(name, []))


_LIST_NUMBER = re.compile(r"^(?:[\d٠-٩]{1,3}[.)]|•)\s+")


def _spells(p, texts: list[str]) -> bool:
    """Do these consecutive lines, joined, spell the paragraph (with or without the
    list number Word put in front of its first line)?"""
    target, target_loose = norm(p["text"]), norm(p["text"], loose=True)
    for first in (texts[0], _LIST_NUMBER.sub("", norm(texts[0]))) if texts else ():
        joined = ""
        for t in [first] + texts[1:]:
            joined = (joined + " " + t).strip()
            if norm(joined) == target or (loose_ok(p) and norm(joined, loose=True) == target_loose):
                return True
            if len(norm(joined, loose=True)) > len(target_loose):
                break
    return False


def matches(line_text: str, p) -> bool:
    # A line may start with the list number or bullet Word added to the paragraph.
    for text in (line_text, _LIST_NUMBER.sub("", norm(line_text))):
        if norm(text) in norm(p["text"]):
            return True
        if loose_ok(p) and norm(text, loose=True) in norm(p["text"], loose=True):
            return True
    return False


# Answer key -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", DOCS)
def test_every_line_is_typed_text(lines, name):
    wrong = []
    for line in lines[name]:
        t = norm(line.text)
        if _FOOTER.match(t) or _MARKER.match(t) or line.uncertain:
            continue
        if not any(matches(line.text, p) for p in paragraphs(name)):
            wrong.append((line.id, t))
    assert wrong == []


@pytest.mark.parametrize("name", DOCS)
def test_every_paragraph_is_found_in_order(lines, name):
    missing = []
    for p in paragraphs(name):
        if is_unreadable(name, p["text"]) or not p["text"].strip():
            continue
        found = [l for l in lines[name] if matches(l.text, p)]
        if not any(_spells(p, [l.text for l in found[start:]]) for start in range(len(found))):
            missing.append(p["text"][:60])
    assert missing == []


@pytest.mark.parametrize("name", DOCS)
def test_lines_with_wrong_characters_are_flagged(lines, name):
    flagged = {l.id for l in lines[name] if l.uncertain}
    # Lines that match no typed paragraph but share most words with an unreadable one.
    expected = {
        l.id
        for l in lines[name]
        if not any(matches(l.text, q) for q in paragraphs(name))
        and any(is_unreadable(name, p["text"]) and _overlaps(l.text, p["text"]) for p in paragraphs(name))
    }
    assert expected <= flagged, "lines with wrong text must be marked uncertain"
    assert len(flagged) <= 3, f"too many uncertain lines: {flagged}"


def _overlaps(line_text: str, paragraph: str) -> bool:
    words = [w for w in norm(line_text).split() if len(w) > 2]
    return bool(words) and sum(w in paragraph for w in words) >= len(words) / 2


# Round trip -------------------------------------------------------------------------------

_BRACKETS = str.maketrans({c: "|" for c in "()[]{}<>«»‹›"})
# Word draws "10%" in Arabic text as "%10"; the extracted text is "10%".
_PERCENT_LEFT = re.compile(r"([%٪‰])([\d٠-٩][\d٠-٩.,]*)")


def page_order(line) -> str:
    out = []
    for g in line.glyphs:
        base = "".join(c for c in g.text if not unicodedata.combining(c))
        out.append(base[::-1] if len(base) > 1 and g.bidi in ("R", "AL") else base)
    text = "".join(out).translate(_BRACKETS)
    return _PERCENT_LEFT.sub(r"\2\1", text) if line.rtl else text


def standard_layout(line) -> str:
    shown = get_display(line.display, base_dir="R" if line.rtl else "L")
    return "".join(c for c in shown if not unicodedata.combining(c) and c not in (LRI, RLI, PDI)).translate(_BRACKETS)


def round_trip_failures(lines):
    return [(l.id, l.text, standard_layout(l), page_order(l)) for l in lines if not l.uncertain and standard_layout(l) != page_order(l)]


@pytest.mark.parametrize("name", DOCS)
def test_text_lays_out_like_the_page(lines, name):
    assert round_trip_failures(lines[name]) == []


@pytest.mark.parametrize("path", SAMPLES or [None], ids=lambda p: p.name if p else "none")
def test_private_samples_lay_out_like_the_page(path):
    if path is None:
        pytest.skip("no private samples")
    assert round_trip_failures(all_lines(path)) == []


# Lines and styles ----------------------------------------------------------------------------


def find(lines, name, needle):
    hits = [l for l in lines[name] if needle in l.text]
    assert hits, f"no line with {needle!r}"
    return hits[0]


@pytest.mark.parametrize(
    "name, a, b",
    [
        ("arabic-quotation", "10,000", "0.17"),
        ("arabic-quotation", "رسائل الخدمة", "5,000"),
        ("french-quote", "Luminaires LED encastrés", "89,90"),
        ("english-styles", "Messages sent", "120,400"),
        ("bilingual-contract", "This Agreement", "أُبرمت"),
    ],
)
def test_table_cells_stay_separate(lines, name, a, b):
    find(lines, name, a)
    find(lines, name, b)
    assert not [l.id for l in lines[name] if a in l.text and b in l.text]


@pytest.mark.parametrize(
    "name, needle, font, size, color, bold, rtl",
    [
        ("arabic-quotation", "عرض سعر خدمات", "SimplifiedArabic-Bold", 20, "1f4e79", True, True),
        ("arabic-quotation", "ملاحظة: لا تشمل", "ArialMT", 12, "c00000", False, True),
        ("arabic-quotation", "الإجمالي شامل الضريبة", "Arial-BoldMT", 12, "00b050", True, True),
        ("english-styles", "Quarterly Service Report", "Georgia-Bold", 28, "1f3864", True, False),
        ("french-quote", "Devis n°", "Cambria-Bold", 20, "1f4e79", True, False),
        ("bilingual-contract", "Service Agreement", "Calibri-Bold", 20, "000000", True, False),
    ],
)
def test_line_style(lines, name, needle, font, size, color, bold, rtl):
    line = find(lines, name, needle)
    s = line.style
    assert (s.font, round(s.size), s.color, s.bold, line.rtl) == (font, size, color, bold, rtl)


def test_style_changes_inside_a_line(lines):
    line = find(lines, "english-styles", "delivery rates improved")
    spans = {t.strip(): st for t, st in line.spans}
    assert spans["improved"].bold and not line.style.bold
    assert spans["third"].color == "c00000"


def test_italic_line(lines):
    assert find(lines, "french-quote", "Une lumière bien pensée").style.italic


def test_superscript_stays_in_its_line(lines):
    assert "du 1er étage" in find(lines, "french-quote", "rez-de-chaussée").text


def test_arabic_ligatures_and_vowel_marks(lines):
    assert "الأفق" in find(lines, "arabic-quotation", "شركة الأفق").text
    assert "الإِتْقَانُ" in find(lines, "arabic-quotation", "الحُكَمَاءُ").text


def test_mixed_direction_lines(lines):
    assert find(lines, "arabic-quotation", "الموقع").text == "الموقع: www.example.com"
    assert find(lines, "bilingual-contract", "Meeting location").text == "Meeting location: Riyadh office, برج المملكة, Floor 12."
    date = find(lines, "bilingual-contract", "Date /")
    assert (date.text, date.rtl) == ("Date / التاريخ: 21/10/2026", False)


# Rotated and cropped pages --------------------------------------------------------------------


def _ink_rect(page) -> pymupdf.Rect:
    pix = page.get_pixmap(alpha=False, colorspace=pymupdf.csGRAY)
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w)
    ys, xs = np.where(a < 160)
    return pymupdf.Rect(xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)


@pytest.mark.parametrize("rotation, crop", [(0, None), (90, None), (180, None), (270, None), (0, (60, 80, 460, 640)), (90, (60, 80, 460, 640))])
def test_boxes_sit_on_the_text_on_rotated_and_cropped_pages(rotation, crop):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((150, 200), "Hello world", fontsize=20)
    if crop:
        page.set_cropbox(pymupdf.Rect(crop))
    if rotation:
        page.set_rotation(rotation)
    page = doc.reload_page(page)
    (line,) = page_lines(page)
    box, ink = pymupdf.Rect(line.bbox), _ink_rect(page)
    assert line.text == "Hello world"
    assert box + (-1.5, -1.5, 1.5, 1.5) & ink == ink, f"box {box} misses ink {ink}"
    assert box.get_area() < 3 * ink.get_area()
