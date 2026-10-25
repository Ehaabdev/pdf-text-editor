"""Text detection: every line's text, direction, style and position.

Checks of the text:
  * Answer key: made-up documents saved as PDF by Microsoft Word and printed to PDF
    by Chrome. Each line is text that was typed, and every typed paragraph is found
    again, in order. Word and Chrome write PDFs very differently.
  * Consistency (any PDF): laying out the extracted display text with the standard
    bidi algorithm, as a browser or edit box does, gives the same order as the glyphs
    on the page. Because embedded runs are isolated this mostly checks the runs are
    consistent; it cannot tell a wrong base direction. The report test lists the
    lines in private samples whose direction was a guess.
"""

import json
import re
import unicodedata
from pathlib import Path

import numpy as np
import pymupdf
import pytest
from bidi import get_display

from app.textlayout import LRI, PDI, RLI, page_lines, page_order as plain_page_order, standard_layout as plain_layout

FIXTURES = Path(__file__).parent / "fixtures"
SOURCES = {"word": "word", "chrome": "chrome"}  # folder, and the file suffix
DOCS = ["arabic-quotation", "french-quote", "bilingual-contract", "english-styles", "turkish-quote"]
CASES = [(source, name) for source in SOURCES for name in DOCS]
EXPECTED = {source: json.loads((FIXTURES / source / "expected.json").read_text(encoding="utf-8")) for source in SOURCES}
SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"
SAMPLES = sorted(SAMPLES_DIR.glob("*.pdf"))

# Paragraphs where the PDF itself holds wrong characters for some letters. Their
# lines must be flagged uncertain.
UNREADABLE = {
    ("word", "arabic-quotation"): ["نلتزم بتقديم خدمة"],  # Word, kashida justification
    ("word", "bilingual-contract"): ["The Client’s representative"],  # Word, Calibri's Arabic ligatures
    ("chrome", "bilingual-contract"): ["The Client’s representative"],  # Chrome, the same font
    ("chrome", "arabic-quotation"): ["قَالَ الحُكَمَاءُ"],  # Chrome left two shaped Sakkal Majalla glyphs unlabelled
}

_INVISIBLE = dict.fromkeys(map(ord, "‎‏⁦⁧⁨⁩‪‫‬‭‮ـ"))
_FOOTER = re.compile(r"^(Page|Sayfa|صفحة)\s*[\d٠-٩]+\s*(of|sur|من|/)\s*[\d٠-٩]+$")
_MARKER = re.compile(r"^([•\-–]|[\d٠-٩]+[.)])$")
_LIST_NUMBER = re.compile(r"^(?:[\d٠-٩]{1,3}[.)]|•)\s+")


def norm(text: str, loose: bool = False) -> str:
    text = unicodedata.normalize("NFC", text).translate(_INVISIBLE)
    text = re.sub(r"[\s   ]+", " ", text).strip()
    # Word draws the space at a line break inside a bracket: "( Agent" on the page.
    text = re.sub(r"([(\[{«]) ", r"\1", text)
    text = re.sub(r" ([)\]}»])", r"\1", text)
    return text.replace(" ", "").casefold() if loose else text


def all_lines(path: Path):
    with pymupdf.open(path) as doc:
        return [line for page in doc for line in page_lines(page)]


@pytest.fixture(scope="module")
def lines():
    return {(s, n): all_lines(FIXTURES / s / f"{n}-{SOURCES[s]}.pdf") for s, n in CASES}


def paragraphs(source, name):
    return [p for p in EXPECTED[source] if p["doc"] == name]


def loose_ok(p) -> bool:
    return any(r.get("changesLetters") for r in p["runs"])


def is_unreadable(case, text) -> bool:
    return any(marker in text for marker in UNREADABLE.get(case, []))


def matches(line_text: str, p) -> bool:
    # A line may start with the list number or bullet Word added to the paragraph.
    for text in (line_text, _LIST_NUMBER.sub("", norm(line_text))):
        if norm(text) in norm(p["text"]):
            return True
        if loose_ok(p) and norm(text, loose=True) in norm(p["text"], loose=True):
            return True
    return False


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


def _overlaps(line_text: str, paragraph: str) -> bool:
    words = [w for w in norm(line_text).split() if len(w) > 2]
    return bool(words) and sum(w in paragraph for w in words) >= len(words) / 2


# Answer key -----------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c[0]}-{c[1]}")
def test_every_line_is_typed_text(lines, case):
    wrong = []
    for line in lines[case]:
        t = norm(line.text)
        if _FOOTER.match(t) or _MARKER.match(t) or line.uncertain:
            continue
        if not any(matches(line.text, p) for p in paragraphs(*case)):
            wrong.append((line.id, t))
    assert wrong == []


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c[0]}-{c[1]}")
def test_every_paragraph_is_found_in_order(lines, case):
    missing = []
    for p in paragraphs(*case):
        if is_unreadable(case, p["text"]) or not p["text"].strip():
            continue
        found = [l for l in lines[case] if matches(l.text, p)]
        if not any(_spells(p, [l.text for l in found[start:]]) for start in range(len(found))):
            missing.append(p["text"][:60])
    assert missing == []


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c[0]}-{c[1]}")
def test_lines_with_wrong_characters_are_flagged(lines, case):
    flagged = {l.id for l in lines[case] if l.uncertain}
    # Lines that match no typed paragraph but share most words with an unreadable one.
    expected = {
        l.id
        for l in lines[case]
        if not any(matches(l.text, q) for q in paragraphs(*case))
        and any(is_unreadable(case, p["text"]) and _overlaps(l.text, p["text"]) for p in paragraphs(*case))
    }
    assert expected <= flagged, "lines with wrong text must be marked uncertain"
    assert len(flagged) <= 3, f"too many uncertain lines: {flagged}"


# Consistency -------------------------------------------------------------------------------

_BRACKETS = str.maketrans({c: "|" for c in "()[]{}<>«»‹›"})
# Word draws "10%" in Arabic text as "%10"; the extracted text is "10%".
_PERCENT_LEFT = re.compile(r"([%٪‰])([\d٠-٩][\d٠-٩.,]*)")


def page_order(line) -> str:
    text = plain_page_order(line)
    return _PERCENT_LEFT.sub(r"\2\1", text) if line.rtl else text


def display_layout(line) -> str:
    shown = get_display(line.display, base_dir="R" if line.rtl else "L")
    return "".join(c for c in shown if not unicodedata.combining(c) and c not in (LRI, RLI, PDI)).translate(_BRACKETS)


def round_trip_failures(lines):
    return [(l.id, l.text, display_layout(l), page_order(l)) for l in lines if not l.uncertain and display_layout(l) != page_order(l)]


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c[0]}-{c[1]}")
def test_display_text_lays_out_like_the_page(lines, case):
    assert round_trip_failures(lines[case]) == []


@pytest.mark.parametrize("path", SAMPLES or [None], ids=lambda p: p.name if p else "none")
def test_private_samples_lay_out_like_the_page(path):
    if path is None:
        pytest.skip("no private samples")
    assert round_trip_failures(all_lines(path)) == []


def test_report_on_private_samples():
    """Never fails. Writes samples/detection-report.txt: the lines in private samples
    whose direction was a guess, and those whose plain text a browser would show in a
    different order than the page. Read it after adding real files."""
    if not SAMPLES:
        pytest.skip("no private samples")
    out = []
    for path in SAMPLES:
        for line in all_lines(path):
            notes = []
            if line.direction_basis not in ("script",):
                notes.append(f"direction from {line.direction_basis}")
            if plain_layout(line.text, line.rtl) != plain_page_order(line):
                notes.append("plain text shows in a different order")
            if line.uncertain:
                notes.append("uncertain")
            if notes:
                out.append(f"{path.name} {line.id} {'RTL' if line.rtl else 'LTR'} [{'; '.join(notes)}]\n    {line.text}")
    (SAMPLES_DIR / "detection-report.txt").write_text("\n".join(out) + "\n", encoding="utf-8")


# Lines and styles (Word documents) ------------------------------------------------------------


def find(lines, name, needle, source="word"):
    hits = [l for l in lines[(source, name)] if needle in l.text]
    assert hits, f"no line with {needle!r}"
    return hits[0]


@pytest.mark.parametrize("source", list(SOURCES))
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
def test_table_cells_stay_separate(lines, source, name, a, b):
    if source == "chrome":  # Chrome's copies have no tables: each cell is a paragraph
        a_line, b_line = find(lines, name, a, source), find(lines, name, b, source)
        assert a_line is not b_line
        return
    find(lines, name, a)
    find(lines, name, b)
    assert not [l.id for l in lines[("word", name)] if a in l.text and b in l.text]


@pytest.mark.parametrize("source", list(SOURCES))
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
def test_line_style(lines, source, name, needle, font, size, color, bold, rtl):
    line = find(lines, name, needle, source)
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


@pytest.mark.parametrize("source", list(SOURCES))
def test_arabic_ligatures_and_vowel_marks(lines, source):
    assert "الأفق" in find(lines, "arabic-quotation", "شركة الأفق", source).text
    assert "الإِتْقَانُ" in find(lines, "arabic-quotation", "الإِتْقَانُ", source).text


@pytest.mark.parametrize("source", list(SOURCES))
def test_mixed_direction_lines(lines, source):
    assert find(lines, "arabic-quotation", "الموقع", source).text == "الموقع: www.example.com"
    assert find(lines, "arabic-quotation", "الهاتف", source).text == "الهاتف: +966 55 123 4567"
    assert "Riyadh office, برج المملكة, Floor 12." in find(lines, "bilingual-contract", "Meeting location", source).text
    date = find(lines, "bilingual-contract", "Date /", source)
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


def test_long_vowelled_page_is_fast():
    import time

    path = FIXTURES / "chrome" / "arabic-quotation-chrome.pdf"
    start = time.perf_counter()
    all_lines(path)
    assert time.perf_counter() - start < 2.0


@pytest.mark.parametrize("source", ["word", "chrome"])
def test_a_vowel_mark_next_to_a_full_stop_stays_on_its_letter(source):
    """Word anchors the tanween of "صالحاً." a little inside the full stop's space."""
    page = pymupdf.open(FIXTURES / "decorations" / f"decorations-{source}.pdf")[0]
    assert "السعر القديم لم يعد صالحاً." in [l.text for l in page_lines(page)]


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*/*.pdf")), ids=lambda p: p.name)
def test_turning_a_page_doesnt_change_what_is_read(path):
    """Same lines in the same order on a page shown turned (/Rotate), with and without a
    crop box: table borders, paragraph rules and reading order use the page as written."""
    plain = [[l.text for l in page_lines(p)] for p in pymupdf.open(path)]
    for rotation, crop in ((90, None), (180, None), (270, None), (90, (20, 30, 575, 820))):
        doc = pymupdf.open(path)
        for page in doc:
            if crop:
                page.set_cropbox(pymupdf.Rect(crop))
            page.set_rotation(rotation)
        turned = pymupdf.open("pdf", doc.tobytes())
        for n, page in enumerate(turned):
            texts = [l.text for l in page_lines(page)]
            want = plain[n] if not crop else [t for t in plain[n] if t in texts]
            assert texts == want, f"rotation {rotation}, crop {crop}, page {n + 1}"
