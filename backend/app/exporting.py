"""Download as…: the edited PDF as another kind of file.

- "jpg", "png": a picture of every page (one file, or a zip of them).
- "pptx-pictures": PowerPoint, every page a slide showing a picture of it: it looks the
  same, but its text can't be edited.
- "pptx": PowerPoint, every page a slide with its text in text boxes you can edit (slides.py).
- "xlsx": Excel, a sheet for every table (tables.py), numbers as numbers.

Text in Word, PowerPoint and Excel files always comes from our own reading of the page
(textlayout), never from another library's: that keeps Arabic and mixed text right.

Every format is made from the current PDF, the same one "Download" gives.
"""

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePath

import pymupdf

from . import flow
from .tables import find_tables
from .textlayout import page_lines

DPI = 200  # sharp on screen and in print, without huge files
MAX_PAGE_PIXELS = 25_000_000  # a poster-sized page is drawn at a lower resolution


class UnknownFormat(ValueError):
    pass


@dataclass
class Export:
    data: bytes
    filename: str
    media_type: str


ZIP = "application/zip"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def stem(name: str) -> str:
    """The file's name without .pdf (the name the person's file had)."""
    s = PurePath(name).name
    return s[:-4] if s.lower().endswith(".pdf") else s


def _zoom(page: pymupdf.Page, dpi: int = DPI) -> float:
    zoom = dpi / 72
    pixels = page.rect.width * page.rect.height * zoom * zoom
    if pixels > MAX_PAGE_PIXELS:
        zoom *= (MAX_PAGE_PIXELS / pixels) ** 0.5
    return zoom


def page_picture(page: pymupdf.Page, kind: str, dpi: int = DPI) -> bytes:
    """A page as a picture, as it shows in a PDF viewer (form fields and notes too)."""
    zoom = _zoom(page, dpi)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    pix.set_dpi(round(zoom * 72), round(zoom * 72))  # so it prints at the page's own size
    return pix.tobytes("jpeg", jpg_quality=90) if kind == "jpg" else pix.tobytes("png")


def _pictures(doc: pymupdf.Document, name: str, kind: str) -> Export:
    media = "image/jpeg" if kind == "jpg" else "image/png"
    if doc.page_count == 1:
        return Export(page_picture(doc[0], kind), f"{name}.{kind}", media)
    digits = len(str(doc.page_count))
    out = io.BytesIO()
    # Pictures are compressed already: storing them is as small and much faster.
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for n, page in enumerate(doc):
            z.writestr(f"{name} - page {n + 1:0{digits}d}.{kind}", page_picture(page, kind))
    return Export(out.getvalue(), f"{name} (pictures).zip", ZIP)


# PowerPoint ------------------------------------------------------------------------------

EMU_PER_PT = 12700
# PowerPoint's slide size limits: 1 inch to 56 inches.
_MIN_SIDE, _MAX_SIDE = 72, 56 * 72


def slide_size(doc: pymupdf.Document) -> tuple[float, float]:
    """The slides' size, in points: the first page's, within PowerPoint's limits. Pages
    of other sizes are fitted onto it."""
    w, h = doc[0].rect.width, doc[0].rect.height
    scale = min(1.0, _MAX_SIDE / w, _MAX_SIDE / h)
    w, h = w * scale, h * scale
    scale = max(1.0, _MIN_SIDE / w, _MIN_SIDE / h)
    return min(_MAX_SIDE, w * scale), min(_MAX_SIDE, h * scale)


def _fitted(page: pymupdf.Page, slide: tuple[float, float]) -> tuple[float, float, float, float]:
    """Where a page goes on a slide (points): as large as it fits, centered."""
    pw, ph = page.rect.width, page.rect.height
    s = min(slide[0] / pw, slide[1] / ph)
    w, h = pw * s, ph * s
    return (slide[0] - w) / 2, (slide[1] - h) / 2, w, h


def new_deck(doc: pymupdf.Document):
    from pptx import Presentation
    from pptx.util import Emu

    deck = Presentation()
    w, h = slide_size(doc)
    deck.slide_width, deck.slide_height = Emu(round(w * EMU_PER_PT)), Emu(round(h * EMU_PER_PT))
    return deck, (w, h)


def blank_slide(deck):
    return deck.slides.add_slide(deck.slide_layouts[6])  # the blank layout


def emu(points: float):
    from pptx.util import Emu

    return Emu(round(points * EMU_PER_PT))


def save_deck(deck) -> bytes:
    out = io.BytesIO()
    deck.save(out)
    return out.getvalue()


def _picture_for_slide(page: pymupdf.Page) -> bytes:
    """PNG keeps text crisp; a page that's mostly photo is much smaller as JPEG."""
    png = page_picture(page, "png")
    if len(png) < 1_500_000:
        return png
    jpg = page_picture(page, "jpg")
    return jpg if len(jpg) < len(png) else png


def _pptx_pictures(doc: pymupdf.Document, name: str) -> Export:
    deck, size = new_deck(doc)
    for page in doc:
        x, y, w, h = _fitted(page, size)
        slide = blank_slide(deck)
        slide.shapes.add_picture(io.BytesIO(_picture_for_slide(page)), emu(x), emu(y), emu(w), emu(h))
    return Export(save_deck(deck), f"{name}.pptx", PPTX)


# Excel -----------------------------------------------------------------------------------

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_CURRENCY = r"€|\$|£|SAR|USD|EUR|GBP|AED"
_AMOUNT = re.compile(rf"^(?P<pre>(?:{_CURRENCY})\s?)?(?P<num>-?[0-9][0-9 .,]*)(?P<post>\s?(?:{_CURRENCY}|%))?$")


def _number(s: str):
    """(value, decimals, grouped) for a number written the English or the French way:
    1,700.00 / 1 700,00 / 1.700,00 / 0.17 / 89,90. Codes with leading zeros aren't."""
    for pattern, group, point in (
        (r"-?\d{1,3}(,\d{3})+(\.\d+)?", ",", "."),  # 1,700.00
        (r"-?\d{1,3}( \d{3})+(,\d+)?", " ", ","),  # 1 700,00 (French)
        (r"-?\d{1,3}(\.\d{3})+(,\d+)?", ".", ","),  # 1.700,00
        (r"-?(0|[1-9]\d*)(\.\d+)?", None, "."),  # 1700.00
        (r"-?(0|[1-9]\d*),\d{1,2}", None, ","),  # 89,90 (a comma before 3 digits groups thousands)
    ):
        if re.fullmatch(pattern, s):
            whole, _, frac = s.replace(group, "").partition(point) if group else s.partition(point)
            value = float(f"{whole}.{frac}") if frac else int(whole)
            return value, len(frac), group is not None
    return None


def _value(text: str):
    """A cell's value: a number when it's written as one (1,700.00, 89,90 €, 15%), so
    Excel can add it up, with a format that shows it as written; otherwise the text."""
    t = re.sub(r"[   ]", " ", text.strip())
    m = _AMOUNT.match(t)
    parsed = _number(m["num"].strip()) if m else None
    if parsed is None:
        return text.strip(), None
    value, decimals, grouped = parsed
    fmt = ("#,##0" if grouped else "0") + ("." + "0" * decimals if decimals else "")
    pre, post = (m["pre"] or "").strip(), (m["post"] or "").strip()
    if post == "%":
        return value / 100, fmt.replace("#,##", "") + "%"
    if pre:
        fmt = f'"{pre}{" " if len(pre) > 1 else ""}"{fmt}'
    elif post:
        fmt = f'{fmt} "{post}"'
    elif not grouped and not decimals:
        fmt = None
    return value, fmt


def _sheet_name(base: str, taken: set[str]) -> str:
    name = re.sub(r"[\[\]:*?/\\]", " ", base)[:31]
    n, out = 2, name
    while out.lower() in taken:
        out = f"{name[:27]} ({n})"
        n += 1
    taken.add(out.lower())
    return out


def _main_style(lines):
    """The style most of the text is in."""
    weight: dict = {}
    for line in lines:
        for text, style in line.spans:
            weight[style] = weight.get(style, 0) + len(text.strip())
    return max(weight, key=weight.get) if weight else None


def _write_table(ws, table) -> None:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    nc = len(table.cols) - 1
    ws.sheet_view.rightToLeft = table.rtl
    thin = Side(style="thin", color="808080")
    border = Border(left=thin, right=thin, top=thin, bottom=thin) if table.ruled else None
    for cell in table.cells:
        # Right-to-left tables start on the right: so does the sheet.
        col = nc - cell.col - cell.colspan if table.rtl else cell.col
        r0, c0 = cell.row + 1, col + 1
        paras = flow.paragraphs(cell.lines, cell.bbox[0] + 1.5, cell.bbox[2] - 1.5)
        text = "\n".join(p.text for p in paras)
        target = ws.cell(r0, c0)
        value, fmt = _value(text) if text else (None, None)
        target.value = value
        if fmt:
            target.number_format = fmt
        style = _main_style(cell.lines)
        if style is not None:
            target.font = Font(bold=style.bold, italic=style.italic, color=None if style.color == "000000" else "FF" + style.color.upper())
        target.alignment = Alignment(wrap_text=True, vertical="top", readingOrder=2 if flow.rtl(cell.lines) else 0)
        if cell.fill:
            target.fill = PatternFill("solid", fgColor="FF" + cell.fill.upper())
        if border is not None:
            for i in range(cell.rowspan):
                for k in range(cell.colspan):
                    ws.cell(r0 + i, c0 + k).border = border
        if cell.rowspan > 1 or cell.colspan > 1:
            ws.merge_cells(start_row=r0, start_column=c0, end_row=r0 + cell.rowspan - 1, end_column=c0 + cell.colspan - 1)
    widths = [table.cols[c + 1] - table.cols[c] for c in range(nc)]
    if table.rtl:
        widths.reverse()
    for c, w in enumerate(widths):
        ws.column_dimensions[get_column_letter(c + 1)].width = max(6.0, round(w / 5.3, 1))  # about 5.3 pt a character


def _xlsx(doc: pymupdf.Document, name: str) -> Export:
    """Every table becomes a sheet. A PDF without tables gets its text, a paragraph a row,
    a sheet for each page, so there's always something to work with."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment

    wb = Workbook()
    wb.remove(wb.active)
    taken: set[str] = set()
    pages = [(page, page_lines(page)) for page in doc]
    for n, (page, lines) in enumerate(pages):
        found = find_tables(page, lines)
        for k, table in enumerate(found):
            label = f"Page {n + 1}" if len(found) == 1 else f"Page {n + 1} table {k + 1}"
            _write_table(wb.create_sheet(_sheet_name(label, taken)), table)
    if not wb.worksheets:
        left, right = flow.edges([l for _, lines in pages for l in lines])
        for n, (page, lines) in enumerate(pages):
            ws = wb.create_sheet(_sheet_name(f"Page {n + 1}", taken))
            ws.column_dimensions["A"].width = 100
            paras = flow.paragraphs(lines, left, right)
            ws.sheet_view.rightToLeft = flow.rtl(lines)
            for r, p in enumerate(paras, start=1):
                c = ws.cell(r, 1, p.text)
                c.alignment = Alignment(wrap_text=True, vertical="top", readingOrder=2 if p.rtl else 0)
    if not wb.worksheets:
        wb.create_sheet("Page 1")
    out = io.BytesIO()
    wb.save(out)
    return Export(out.getvalue(), f"{name}.xlsx", XLSX)


# All formats -----------------------------------------------------------------------------

FORMATS = ("jpg", "png", "pptx-pictures", "pptx", "xlsx")


def export(src: bytes, name: str, fmt: str) -> Export:
    if fmt not in FORMATS:
        raise UnknownFormat(f"Unknown format: {fmt}.")
    doc = pymupdf.open("pdf", src)
    name = stem(name)
    if fmt in ("jpg", "png"):
        return _pictures(doc, name, fmt)
    if fmt == "xlsx":
        return _xlsx(doc, name)
    if fmt == "pptx":
        from .slides import deck

        return deck(src, name)
    return _pptx_pictures(doc, name)
