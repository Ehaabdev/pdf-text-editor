"""Download as…: the edited PDF as pictures, PowerPoint, Word or Excel."""

import io
import zipfile
from pathlib import Path
from urllib.parse import unquote

import pymupdf
import pytest
from PIL import Image
from pptx import Presentation

from app import exporting, limits
from conftest import make_pdf, upload

FIXTURES = Path(__file__).parent / "fixtures"
REPORT = (FIXTURES / "images" / "images-word.pdf").read_bytes()


def test_one_page_is_one_picture():
    out = exporting.export(make_pdf(1), "notes.pdf", "png")
    assert out.filename == "notes.png" and out.media_type == "image/png"
    png = Image.open(io.BytesIO(out.data))
    assert png.width == round(595 * 200 / 72)  # 200 dots per inch
    assert round(png.info["dpi"][0]) == 200  # so it prints at the page's size


def test_several_pages_are_pictures_in_a_zip():
    out = exporting.export(make_pdf(3), "notes.pdf", "jpg")
    assert out.filename == "notes (pictures).zip"
    names = zipfile.ZipFile(io.BytesIO(out.data)).namelist()
    assert names == ["notes - page 1.jpg", "notes - page 2.jpg", "notes - page 3.jpg"]


def test_a_huge_page_is_drawn_smaller():
    doc = pymupdf.open()
    doc.new_page(width=5000, height=5000)
    png = Image.open(io.BytesIO(exporting.export(doc.tobytes(), "poster.pdf", "png").data))
    assert png.width * png.height <= exporting.MAX_PAGE_PIXELS * 1.01


def test_powerpoint_as_pictures_has_a_slide_per_page():
    out = exporting.export(REPORT, "report.pdf", "pptx-pictures")
    assert out.filename == "report.pptx"
    deck = Presentation(io.BytesIO(out.data))
    assert len(deck.slides) == 2
    assert abs(deck.slide_width - 595 * 12700) < 12700 and abs(deck.slide_height - 842 * 12700) < 12700
    for slide in deck.slides:
        (picture,) = slide.shapes
        assert picture.shape_type == 13  # a picture
        assert picture.left == 0 and picture.top == 0 and picture.width == deck.slide_width


def test_pages_of_another_size_fit_the_slides():
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)  # A4, upright
    doc.new_page(width=842, height=595)  # A4, sideways
    deck = Presentation(io.BytesIO(exporting.export(doc.tobytes(), "mixed.pdf", "pptx-pictures").data))
    sideways = deck.slides[1].shapes[0]
    assert sideways.width == deck.slide_width  # as wide as the slide, centered top to bottom
    assert sideways.height < deck.slide_height and abs(sideways.top * 2 + sideways.height - deck.slide_height) <= 2


def test_slides_stay_within_powerpoints_limits():
    doc = pymupdf.open()
    doc.new_page(width=6000, height=3000)
    w, h = exporting.slide_size(doc)
    assert w <= 56 * 72 and abs(w / h - 2) < 0.01


def test_download_as_through_the_website(client):
    doc = upload(client, REPORT, "تقرير.pdf").json()
    r = client.get(f"/api/documents/{doc['id']}/export?format=pptx-pictures")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(exporting.PPTX)
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment; filename*=utf-8''") and unquote(disposition.split("''")[1]) == "تقرير.pptx"
    assert client.get(f"/api/documents/{doc['id']}/export?format=exe").status_code == 400


def test_downloads_have_their_own_limit():
    assert limits.kind_of("GET", "/api/documents/abc/export") == "export"
    assert "export" in limits.HEAVY
    assert limits.kind_of("GET", "/api/documents/abc/pages/0/image") == "read"


@pytest.mark.parametrize("name, expected", [("a.pdf", "a"), ("A.PDF", "A"), ("report", "report"), ("../x.pdf", "x")])
def test_file_names(name, expected):
    assert exporting.stem(name) == expected
