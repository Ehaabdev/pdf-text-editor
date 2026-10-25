"""Turkish documents: edits, new rows and downloads, also as on the server, where the
documents' fonts aren't installed. Word puts a font in a PDF as two copies: the letters
of its old Western code page (ç, ö, ü) in one, the others (ğ, ı, İ, ş, ₺) in another.
New words take their letters from either, never from a stand-in when the page has them."""

import io
import json
from pathlib import Path
from zipfile import ZipFile

import pymupdf
import pytest

from app import exporting
from app.adding import add_line
from app.editing import comparable, replace_line
from app.fonts import library
from app.rows import Spot, add_row
from app.tables import find_tables
from app.textlayout import page_lines

FIXTURES = Path(__file__).parent / "fixtures"
EDITS = [e for e in json.loads((FIXTURES / "word" / "edits.json").read_text(encoding="utf-8")) if e["doc"] == "turkish-quote"]
WORD = (FIXTURES / "word" / "turkish-quote-word.pdf").read_bytes()


def source(name: str) -> bytes:
    return (FIXTURES / name / f"turkish-quote-{name}.pdf").read_bytes()


def find_line(src: bytes, needle: str):
    doc = pymupdf.open("pdf", src)
    for page in doc:
        for line in page_lines(page):
            if comparable(needle) in comparable(line.text):
                return page.number, line
    return None


def letters_on_page(src: bytes, page_no: int, font: str) -> set[str]:
    """The letters a page shows in a font (bold is a font of its own): those its copies
    of the font have."""
    page = pymupdf.open("pdf", src)[page_no]
    return {ch for l in page_lines(page) for g in l.glyphs if g.style.font == font for ch in g.text}


@pytest.fixture
def no_fonts(monkeypatch):
    monkeypatch.setattr(library(), "find", lambda name: None)


@pytest.mark.parametrize("name", ["word", "chrome"])
@pytest.mark.parametrize("fonts", ["installed", "server"])
@pytest.mark.parametrize("edit", EDITS, ids=lambda e: e["kind"])
def test_planned_edit(edit, fonts, name, monkeypatch):
    if fonts == "server":
        monkeypatch.setattr(library(), "find", lambda n: None)
    src = source(name)
    page_no, line = find_line(src, edit["old"])
    new = comparable(line.text).replace(comparable(edit["old"]), edit["new"])
    result = replace_line(src, page_no, line.id, line.text, new)
    assert find_line(result.pdf, new) is not None
    # A stand-in draws only letters the page doesn't show in that font.
    have = letters_on_page(src, page_no, line.style.font)
    for note in result.lookalikes:
        assert not set(note["letters"]) & have, note


def test_a_new_row_as_on_the_server(no_fonts):
    page = pymupdf.open("pdf", WORD)[0]
    (table,) = [t for t in find_tables(page, page_lines(page)) if t.ruled]
    line = Spot(table, 1).cells[0].lines[0]
    texts = ["Işık sensörü (Çağrı)", "16", "350,00 ₺", "5.600,00 ₺"]  # letters and digits the page has
    result = add_row(WORD, 0, line.id, line.text, "below", texts)
    page = pymupdf.open("pdf", result.pdf)[0]
    (table,) = [t for t in find_tables(page, page_lines(page)) if t.ruled]
    assert [comparable(" ".join(l.text for l in c.lines)) for c in Spot(table, 2).cells] == [comparable(t) for t in texts]
    assert result.lookalikes == []


def test_a_lira_sign_no_font_here_has(no_fonts):
    # The Segoe UI line's copy has no "₺", nor has Selawik (its look-alike) or Liberation
    # Sans: DejaVu Sans draws it, and the user is told. The brackets, not in the copy
    # either, come from Selawik.
    page_no, line = find_line(WORD, "Şirketimiz 2009")
    result = replace_line(WORD, page_no, line.id, line.text, line.text.replace("2009", "2009 (₺)"))
    assert find_line(result.pdf, "2009 (₺)") is not None
    assert sorted((n["substitute"], n["letters"]) for n in result.lookalikes) == [("DejaVu Sans", "₺"), ("Selawik", "()")]


@pytest.mark.parametrize("page_no, find, text", [(1, "Genel Müdür", "Satış Departmanı"), (0, "İstanbul, 21", "Ref: 2026/İST-44")])
def test_a_new_line_under_a_right_aligned_one_lines_up_with_it(page_no, find, text):
    line = next(l for l in page_lines(pymupdf.open("pdf", WORD)[page_no]) if find in l.text)
    result = add_line(WORD, page_no, text, line.id, line.text, "below")
    new = next(l for l in page_lines(pymupdf.open("pdf", result.pdf)[page_no]) if comparable(l.text) == comparable(text))
    assert abs(new.bbox[2] - line.bbox[2]) < 0.5


def test_turkish_amounts_become_numbers_in_excel():
    assert exporting._value("1.250,00 ₺") == (1250.0, '#,##0.00 "₺"')
    assert exporting._value("10.220,00 TL") == (10220.0, '#,##0.00 "TL"')
    assert exporting._value("₺150") == (150, '"₺"0')
    assert exporting._value("%20") == (0.2, "0%")
    assert exporting._value("3 gün") == ("3 gün", None)


def test_word_and_powerpoint_know_the_language():
    assert exporting.latin_language(["Sayın Şükrü Öztürk, görüşmemize istinaden teklifimizi sunarız."]) == "tr-TR"
    assert exporting.latin_language(["Madame, suite à notre rencontre du 14 octobre, nous avons le plaisir de vous adresser notre proposition pour la rénovation complète de l’éclairage de vos bureaux."]) == "fr-FR"
    english = "Thank you for meeting us last week. As agreed, we will send the updated plan, the prices and the delivery dates by Friday, and call you on Monday to go through them together."
    assert exporting.latin_language(["Dear Mrs Hélène Dupré,", english]) == "en-US"
    word = exporting.export(WORD, "teklif.pdf", "docx").data
    xml = ZipFile(io.BytesIO(word)).read("word/document.xml").decode("utf-8")
    assert 'w:val="tr-TR"' in xml and 'w:val="en-US"' not in xml
    deck = exporting.export(WORD, "teklif.pdf", "pptx").data
    slide = ZipFile(io.BytesIO(deck)).read("ppt/slides/slide1.xml").decode("utf-8")
    assert 'lang="tr-TR"' in slide
