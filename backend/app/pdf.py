"""PDF reading and rendering (MuPDF via PyMuPDF)."""

from dataclasses import dataclass
from pathlib import Path

import pymupdf

from . import config


class InvalidPdf(Exception):
    """The upload can't be used. The message is shown to the user."""


@dataclass
class PageInfo:
    # Size in PDF points as the page is displayed (rotation and crop box applied).
    width: float
    height: float


@dataclass
class PdfInfo:
    page_count: int
    pages: list[PageInfo]
    is_signed: bool


def inspect_upload(data: bytes) -> PdfInfo:
    """Check an uploaded file is a PDF we can work with, and describe it."""
    if not data.startswith(b"%PDF") and b"%PDF" not in data[:1024]:
        raise InvalidPdf("This file isn't a PDF.")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        raise InvalidPdf("This PDF is damaged and can't be opened.")
    with doc:
        if doc.needs_pass:
            raise InvalidPdf("This PDF is password-protected. Remove the password and upload it again.")
        if doc.page_count == 0:
            raise InvalidPdf("This PDF has no pages.")
        if doc.page_count > config.MAX_PAGES:
            raise InvalidPdf(f"This PDF has {doc.page_count} pages. The limit is {config.MAX_PAGES}.")
        return describe(doc)


def describe(doc: pymupdf.Document) -> PdfInfo:
    pages = [PageInfo(width=round(p.rect.width, 3), height=round(p.rect.height, 3)) for p in doc]
    # get_sigflags() is 3 when the document contains at least one applied signature.
    is_signed = doc.get_sigflags() == 3
    return PdfInfo(page_count=doc.page_count, pages=pages, is_signed=is_signed)


def describe_file(path: Path) -> PdfInfo:
    with pymupdf.open(path) as doc:
        return describe(doc)


def render_page_png(path: Path, page_no: int, width: int) -> bytes:
    """Render one page to PNG at the requested pixel width."""
    width = max(config.MIN_RENDER_WIDTH, min(config.MAX_RENDER_WIDTH, width))
    with pymupdf.open(path) as doc:
        if not 0 <= page_no < doc.page_count:
            raise IndexError(page_no)
        page = doc[page_no]
        zoom = width / page.rect.width
        # Very tall pages: keep the image to a sane number of pixels.
        pixels = (page.rect.width * zoom) * (page.rect.height * zoom)
        if pixels > config.MAX_RENDER_PIXELS:
            zoom *= (config.MAX_RENDER_PIXELS / pixels) ** 0.5
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png")
