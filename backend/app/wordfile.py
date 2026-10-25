"""Download as Word: the pages as a document you can edit, with paragraphs that wrap.

A PDF keeps lines at fixed places; Word keeps paragraphs that flow. The document is
rebuilt from our own reading of each page, top to bottom:

- text: lines joined into paragraphs (flow.py), with their alignment, indents and the
  space before them; every piece in its font, size, color, bold, italic, raised or
  lowered, underlined, struck through, highlighted, or linked, as on the page;
- tables (tables.py): Word tables with the same columns, merged cells, borders and
  shading; Arabic tables start on the right;
- pictures, each where it was: on its own line, inside a table cell, or within a line
  of text (an icon);
- text repeated at the top or bottom of every page (a logo, "Page 1 of 2") goes into
  Word's header and footer, with live page numbers.

Arabic paragraphs and pieces are marked right-to-left, and Arabic letters get their
font as Word's "complex script" font, as Word itself saves them. Each page starts on a
new page. Lines whose letters the PDF stores wrongly are highlighted, so they can be
checked.
"""

import io
import re
from dataclasses import dataclass, field

import pikepdf
import pymupdf

from . import exporting, flow, images
from .changes import page_links
from .decorations import for_line
from .fonts import family_name
from .pdfcontent import Unsupported, matches_trace, replay
from .tables import Table, _segments, find_tables
from .textlayout import Line, page_lines

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PICTURE_DPI = 200
NEAR = 3.0  # points: "at the edge", "centered"


# What each letter looks like -------------------------------------------------------------


@dataclass
class Look:
    """Everything about a piece of text besides its letters."""

    style: object  # textlayout.Style
    rtl: bool
    rise: int = 0  # thousandths of a percent of the size: raised (3rd) or lowered (CO2)
    underline: bool = False
    strike: bool = False
    shade: str | None = None  # "rrggbb" behind the letters (a highlight)
    link: str | None = None
    unsure: bool = False  # the PDF stores some of the line's letters wrongly


@dataclass
class PageInfo:
    """What the page's drawing tells about its letters: decorations and links, by the
    page's glyph numbers (Line.members, Glyph.traces)."""

    underline: set = field(default_factory=set)
    strike: set = field(default_factory=set)
    shade: dict = field(default_factory=dict)  # glyph -> color
    link: dict = field(default_factory=dict)  # glyph -> uri


def _page_info(pdf: pikepdf.Pdf, page_no: int, page: pymupdf.Page, lines: list[Line], fills) -> PageInfo:
    info = PageInfo()
    try:
        rp = replay(pdf, page_no)
    except Unsupported:
        return info
    if not matches_trace(rp, page):
        return info
    for line in lines:
        for d in for_line(line, rp):
            if d.kind == "underline":
                info.underline |= d.traces
            elif d.kind == "strike":
                info.strike |= d.traces
            else:
                color = _color_at(fills, line) or "ffff00"
                for t in d.traces:
                    info.shade[t] = color
    try:
        for link in page_links(pdf, page_no, rp):
            if link.uri:
                for t in link.glyphs:
                    info.link[t] = link.uri
    except Exception:
        pass
    return info


def _color_at(fills, line: Line) -> str | None:
    """The color of a highlight: the filled shape under the letters."""
    x0, y0, x1, y1 = line.bbox
    mid = pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2)
    under = [(r.width * r.height, c) for r, c in fills if r.y0 - 1 <= mid.y <= r.y1 + 1 and r.x0 < x1 and r.x1 > x0 and c != "ffffff"]
    return min(under)[1] if under else None


def _trim(pieces: list[tuple[str, Look]]) -> list[tuple[str, Look]]:
    """Without spaces at either end: the space between parts is added once."""
    out = list(pieces)
    if out:
        out[0] = (out[0][0].lstrip(), out[0][1])
        out[-1] = (out[-1][0].rstrip(), out[-1][1])
    return [p for p in out if p[0]]


def segments(line: Line, info: PageInfo, keep=None) -> list[tuple[str, Look]]:
    """The line in typing order, in pieces that look the same; only its glyphs that
    `keep` accepts, when given."""
    weight: dict = {}
    for g in line.glyphs:
        if not g.is_space and not g.synthetic:
            weight[g.origin[1]] = weight.get(g.origin[1], 0) + 1
    baseline = max(weight, key=weight.get) if weight else 0.0
    out: list[list] = []
    for run in line.runs:
        for i in run.glyphs:
            g = line.glyphs[i]
            if keep is not None and not keep(g):
                continue
            traces = set(g.traces) or set()
            size = g.style.size or 1.0
            rise = 0 if g.is_space else round((baseline - g.origin[1]) / size * 100) * 1000
            if abs(rise) < 8000:
                rise = 0
            look = Look(
                g.style,
                run.rtl,
                rise,
                bool(traces) and traces <= info.underline,
                bool(traces) and traces <= info.strike,
                next((info.shade[t] for t in traces if t in info.shade), None),
                next((info.link[t] for t in traces if t in info.link), None),
                line.uncertain,
            )
            text = line.glyph_text(i)
            if out and (out[-1][1] == look or (g.is_space and out[-1][1].rtl == run.rtl and not out[-1][1].rise)):
                out[-1][0] += text
            else:
                out.append([text, look])
    return [(t, l) for t, l in out if t]


# Word's XML ------------------------------------------------------------------------------


def _el(tag: str, **attrs):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    e = OxmlElement(tag)
    for k, v in attrs.items():
        e.set(qn(f"w:{k}"), str(v))
    return e


def _set(parent, tag: str, **attrs):
    """Put an element in a property list, replacing any of the same kind. Word
    requires its own order of elements in a property list: callers add them in it."""
    from docx.oxml.ns import qn

    for old in parent.findall(qn(tag)):
        parent.remove(old)
    e = _el(tag, **attrs)
    parent.append(e)
    return e


_RPR_ORDER = ["rStyle", "rFonts", "b", "bCs", "i", "iCs", "strike", "color", "sz", "szCs", "highlight", "u", "shd", "position", "rtl", "lang"]


def _rpr(look: Look):
    """A run's properties, as Word saves them for Arabic and Latin text alike: the font
    and size for both kinds of letters, the direction, and the decorations."""
    style = look.style
    family = family_name(style.font)
    half = max(2, round(style.size * 2))
    props = {
        "rFonts": dict(ascii=family, hAnsi=family, cs=family, eastAsia=family),
        "b": {} if style.bold else None,
        "bCs": {} if style.bold else None,
        "i": {} if style.italic else None,
        "iCs": {} if style.italic else None,
        "strike": {} if look.strike else None,
        "color": dict(val=style.color.upper()),
        "sz": dict(val=half),
        "szCs": dict(val=half),
        "highlight": dict(val="yellow") if look.unsure else None,
        "u": dict(val="single") if look.underline else None,
        "shd": dict(val="clear", color="auto", fill=look.shade.upper()) if look.shade and not look.unsure else None,
        "position": dict(val=round(look.rise / 100000 * style.size * 2)) if look.rise else None,
        "rtl": {} if look.rtl else dict(val="false"),
        "lang": dict(val="en-US", bidi="ar-SA"),
    }
    rpr = _el("w:rPr")
    for tag in _RPR_ORDER:
        attrs = props.get(tag)
        if attrs is not None:
            rpr.append(_el(f"w:{tag}", **attrs))
    return rpr


def _add_runs(paragraph, pieces: list[tuple[str, Look]], links: dict) -> None:
    """Runs for the pieces; linked pieces inside a hyperlink."""
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    p = paragraph._p
    for text, look in pieces:
        r = _el("w:r")
        r.append(_rpr(look))
        t = _el("w:t")
        t.text = text
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        r.append(t)
        if look.link:
            rid = links.get(look.link)
            if rid is None:
                rid = links[look.link] = paragraph.part.relate_to(look.link, RT.HYPERLINK, is_external=True)
            link = p[-1] if len(p) and p[-1].tag == qn("w:hyperlink") and p[-1].get(qn("r:id")) == rid else None
            if link is None:
                link = _el("w:hyperlink")
                link.set(qn("r:id"), rid)
                p.append(link)
            link.append(r)
        else:
            p.append(r)


def _para_props(paragraph, *, rtl: bool, align: str, before: float, start: float = 0.0, first: float = 0.0, end: float = 0.0, line: float | None = None, exact: bool = False, shade: str | None = None, page_break: bool = False, keep: bool = False) -> None:
    """Paragraph properties, in Word's order. In a right-to-left paragraph "start" is the
    right side: Word's left/right indents and alignments are measured from the start."""
    ppr = paragraph._p.get_or_add_pPr()
    for child in list(ppr):
        ppr.remove(child)
    if keep:
        ppr.append(_el("w:keepNext"))
    if page_break:
        ppr.append(_el("w:pageBreakBefore"))
    if shade:
        ppr.append(_el("w:shd", val="clear", color="auto", fill=shade.upper()))
    if rtl:
        ppr.append(_el("w:bidi"))
    spacing = dict(before=max(0, round(before * 20)), after=0)
    if line:
        spacing.update(line=round(line * 20), lineRule="exact" if exact else "atLeast")
    ppr.append(_el("w:spacing", **spacing))
    ind = {}
    if start > 0.5:
        ind["left"] = round(start * 20)
    if end > 0.5:
        ind["right"] = round(end * 20)
    if abs(first) > 0.5:
        ind["firstLine" if first > 0 else "hanging"] = round(abs(first) * 20)
    if ind:
        ppr.append(_el("w:ind", **ind))
    if align != "start":
        # Word's "left" in a right-to-left paragraph is its start: the right side.
        jc = {"center": "center", "both": "both", "kashida": "mediumKashida", "end": "left" if rtl else "right"}[align]
        ppr.append(_el("w:jc", val=jc))


# Layout ----------------------------------------------------------------------------------

_heights: dict = {}

# Word's single line height, in ems, of fonts the server may not have (Office's and
# Windows' own), read from the fonts on Windows: (Windows ascent + descent) / em.
_KNOWN = {
    "calibri": 1.221, "calibrilight": 1.221, "cambria": 1.172, "candara": 1.221, "consolas": 1.171,
    "constantia": 1.221, "corbel": 1.221, "segoeui": 1.33, "aptos": 1.285, "aptosdisplay": 1.285,
    "arial": 1.117, "timesnewroman": 1.107, "verdana": 1.215, "tahoma": 1.207, "georgia": 1.136,
    "garamond": 1.125, "centurygothic": 1.191, "couriernew": 1.133, "trebuchetms": 1.161,
    "bookantiqua": 1.243, "palatinolinotype": 1.349, "franklingothicmedium": 1.134, "gillsansmt": 1.16,
    "lucidasansunicode": 1.537, "traditionalarabic": 1.495, "simplifiedarabic": 1.658,
    "sakkalmajalla": 1.396, "arabictypesetting": 1.14, "dubai": 1.688, "andalus": 1.526,
    "aldhabi": 1.75, "urdutypesetting": 1.703, "bahnschrift": 1.2, "leelawadeeui": 1.33, "nirmalaui": 1.33,
}


def word_line(style) -> float:
    """How tall Word makes a single-spaced line in this font and size (points): from
    the font's own metrics when it's installed or known, else a usual 1.15 times the
    size."""
    from .fonts import library

    entry = library().find(style.font)
    key = (entry.path, entry.index) if entry else None
    if key not in _heights:
        _heights[key] = None
        if entry is not None:
            try:
                from fontTools.ttLib import TTCollection, TTFont

                tt = TTCollection(entry.path, lazy=True).fonts[entry.index] if entry.path.lower().endswith(".ttc") else TTFont(entry.path, lazy=True)
                os2, upm = tt["OS/2"], tt["head"].unitsPerEm
                _heights[key] = (os2.usWinAscent + os2.usWinDescent) / upm
            except Exception:
                pass
    em = _heights[key] or _KNOWN.get(family_name(style.font).lower().replace(" ", ""))
    return (em if em else 1.15) * style.size


def _extra(band: list[Line]) -> float:
    """How much taller Word draws the row than the page does."""
    line = max(band, key=lambda l: l.bbox[3] - l.bbox[1])
    return max(0.0, word_line(line.style) - (line.bbox[3] - line.bbox[1]))



@dataclass
class Frame:
    """Where text can go: the page's text area, or a table cell's."""

    left: float
    right: float


def _alignment(para: flow.Paragraph, frame: Frame) -> str:
    """start, end, center, both or kashida, from where the lines sit."""
    rtl = para.rtl
    rows = para.bands
    starts, ends = [], []
    for band in rows:
        x0, x1 = min(l.bbox[0] for l in band), max(l.bbox[2] for l in band)
        starts.append(frame.right - x1 if rtl else x0 - frame.left)
        ends.append(x0 - frame.left if rtl else frame.right - x1)
    width = frame.right - frame.left
    # Justified: every line but the last reaches both sides. With one such line, it must
    # end right at the side: wrapped text that isn't justified ends anywhere within a word.
    tight = 1.0 if len(rows) == 2 else NEAR + 1
    if len(rows) > 1 and all(e < tight for e in ends[:-1]) and all(s < NEAR + 1 for s in starts[1:]):
        if any(l.kashida for l in para.lines):
            return "kashida"
        return "both"
    if all(abs(s - e) < 2 * NEAR for s, e in zip(starts, ends)) and min(starts) > 0.05 * width:
        return "center"
    if all(e < NEAR for e in ends) and min(starts) > 0.15 * width:
        return "end"
    return "start"


def _indents(para: flow.Paragraph, frame: Frame, align: str) -> tuple[float, float]:
    """(start indent, first line's extra indent) in points, for text that starts at its
    side; none for centered or end-aligned text."""
    if align in ("center", "end"):
        return 0.0, 0.0
    rtl = para.rtl
    starts = []
    for band in para.bands:
        x0, x1 = min(l.bbox[0] for l in band), max(l.bbox[2] for l in band)
        starts.append(frame.right - x1 if rtl else x0 - frame.left)
    body = min(starts[1:]) if len(starts) > 1 else starts[0]
    body = max(0.0, body)
    first = starts[0] - body
    return body, first if abs(first) > 1 else 0.0


@dataclass
class Picture:
    image: images.PageImage
    png: bytes

    @property
    def bbox(self):
        return self.image.bbox


def _picture(src, page_no: int, image: images.PageImage) -> Picture | None:
    x0, y0, x1, y1 = image.bbox
    try:
        png = images.picture_png(src, page_no, image.id, round((x1 - x0) * PICTURE_DPI / 72))
    except Exception:
        return None
    return Picture(image, png)


def _add_picture(paragraph, picture: Picture, scale: float = 1.0) -> None:
    from docx.shared import Pt

    x0, y0, x1, y1 = picture.bbox
    run = paragraph.add_run()
    run.add_picture(io.BytesIO(picture.png), width=Pt((x1 - x0) * scale), height=Pt((y1 - y0) * scale))


def _inline_pictures(band: list[Line], pictures: list[Picture]) -> list[Picture]:
    """Small pictures within a row of text (an icon in a sentence)."""
    top = min(l.bbox[1] for l in band)
    bottom = max(l.bbox[3] for l in band)
    height = bottom - top
    return [p for p in pictures if p.bbox[3] - p.bbox[1] <= 1.8 * height and top - 0.5 * height <= (p.bbox[1] + p.bbox[3]) / 2 <= bottom + 0.5 * height]


def _write_paragraph(paragraph, para: flow.Paragraph, frame: Frame, info: PageInfo, links: dict, pictures: list[Picture], *, before: float, page_break: bool = False, shade: str | None = None) -> None:
    # Text in a narrower column or box is aligned within it, and wraps where it did.
    a, b = para.wrap
    narrower = b > a and (a - frame.left > 12 or frame.right - b > 12)
    local = Frame(max(a, frame.left), min(b, frame.right)) if narrower else frame
    align = _alignment(para, local)
    start, first = _indents(para, frame, align)
    end = (local.left - frame.left) if para.rtl else (frame.right - local.right)
    end = end if end > 12 and align != "center" else 0.0
    pitch = None
    exact = False
    if len(para.bands) > 1:
        tops = [min(l.bbox[1] for l in b) for b in para.bands]
        pitch = (tops[-1] - tops[0]) / (len(tops) - 1)
        # The page's own line spacing; exactly, when Word's lines would be taller.
        exact = word_line(para.lines[0].style) > pitch + 0.5
    _para_props(paragraph, rtl=para.rtl, align=align, before=before, start=start, first=first, end=end, line=pitch, exact=exact, shade=shade, page_break=page_break)
    for k, band in enumerate(para.bands):
        icons = _inline_pictures(band, pictures)
        for icon in icons:
            pictures.remove(icon)
        # In reading order: by x, from the right for right-to-left text. A line with an
        # icon inside it is cut there, the icon between its two parts.
        items: list = []
        for line in band:
            inside = sorted((i for i in icons if line.bbox[0] < (i.bbox[0] + i.bbox[2]) / 2 < line.bbox[2]), key=lambda i: i.bbox[0])
            if not inside:
                items.append(((line.bbox[0] + line.bbox[2]) / 2, segments(line, info)))
            else:
                # Glyph boxes are in the page's own space, icons as displayed: the same
                # on pages that aren't turned, the only ones cut here.
                cuts = [line.bbox[0] - 1] + [(i.bbox[0] + i.bbox[2]) / 2 for i in inside] + [line.bbox[2] + 1]
                for c0, c1 in zip(cuts, cuts[1:]):
                    part = segments(line, info, lambda g, c0=c0, c1=c1: c0 <= (g.bbox[0] + g.bbox[2]) / 2 < c1)
                    if part:
                        items.append(((c0 + c1) / 2, part))
            icons = [i for i in icons if i not in inside]
            items += [((i.bbox[0] + i.bbox[2]) / 2, i) for i in inside]
        items += [((i.bbox[0] + i.bbox[2]) / 2, i) for i in icons]
        items.sort(key=lambda it: it[0], reverse=para.rtl)
        for j, (_, item) in enumerate(items):
            if isinstance(item, list) and not item:
                continue
            if (k or j) and not (isinstance(item, list) and item[0][0][:1].isspace()):
                _add_runs(paragraph, [(" ", _space_look(para))], links)
            if isinstance(item, Picture):
                _add_picture(paragraph, item)
            else:
                _add_runs(paragraph, _trim(item), links)


def _space_look(para: flow.Paragraph) -> Look:
    line = para.lines[0]
    return Look(line.style, para.rtl)


# Tables ----------------------------------------------------------------------------------


def _write_table(word, table: Table, frame: Frame, info: PageInfo, links: dict, pictures: list[Picture], before: float, page_break: bool):
    from docx.oxml.ns import qn

    if before > 1 or page_break:
        spacer = word.add_paragraph()
        _para_props(spacer, rtl=table.rtl, align="start", before=max(0.0, before - 6), page_break=page_break)
        # A small empty line keeps the gap above the table.
        spacer.paragraph_format.line_spacing = 0.5
    nr, nc = len(table.rows) - 1, len(table.cols) - 1
    t = word.add_table(rows=nr, cols=nc)
    t.autofit = False
    tbl = t._tbl
    tblpr = tbl.tblPr
    for child in list(tblpr):
        if child.tag in (qn("w:tblStyle"),):
            tblpr.remove(child)
    widths = [table.cols[c + 1] - table.cols[c] for c in range(nc)]
    if table.rtl:
        widths.reverse()
        tblpr.append(_el("w:bidiVisual"))
    total = sum(widths)
    tblpr.append(_el("w:tblW", w=round(total * 20), type="dxa"))
    indent = (frame.right - table.cols[-1]) if table.rtl else (table.cols[0] - frame.left)
    if abs(indent) > 1:
        tblpr.append(_el("w:tblInd", w=round(indent * 20), type="dxa"))
    borders = _el("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        if table.ruled:
            borders.append(_el(f"w:{side}", val="single", sz=4, space=0, color="808080"))
        else:
            borders.append(_el(f"w:{side}", val="nil"))
    tblpr.append(borders)
    tblpr.append(_el("w:tblLayout", type="fixed"))
    margins = _el("w:tblCellMar")
    for side in ("left", "right"):
        margins.append(_el(f"w:{side}", w=round(_padding(table) * 20), type="dxa"))
    tblpr.append(margins)
    grid = tbl.tblGrid
    for gc, w in zip(grid.findall(qn("w:gridCol")), widths):
        gc.set(qn("w:w"), str(round(w * 20)))
    for r, row in enumerate(t.rows):
        trpr = row._tr.get_or_add_trPr()
        trpr.append(_el("w:trHeight", val=round((table.rows[r + 1] - table.rows[r]) * 20), hRule="atLeast"))
        for c, cell in enumerate(row.cells):
            cell.width = round(widths[c] * 12700)
    for cell in table.cells:
        col = nc - cell.col - cell.colspan if table.rtl else cell.col
        target = t.cell(cell.row, col)
        if cell.rowspan > 1 or cell.colspan > 1:
            target = target.merge(t.cell(cell.row + cell.rowspan - 1, col + cell.colspan - 1))
        tcpr = target._tc.get_or_add_tcPr()
        if cell.fill:
            tcpr.append(_el("w:shd", val="clear", color="auto", fill=cell.fill.upper()))
        x0, y0, x1, y1 = cell.bbox
        inside = Frame(x0 + _padding(table), x1 - _padding(table))
        paras = flow.paragraphs(cell.lines, inside.left, inside.right)
        cell_pictures = [p for p in pictures if x0 <= (p.bbox[0] + p.bbox[2]) / 2 <= x1 and y0 <= (p.bbox[1] + p.bbox[3]) / 2 <= y1]
        for p in cell_pictures:
            pictures.remove(p)
        blocks = [(p.bbox[1], "p", p) for p in paras] + [(p.bbox[1], "i", p) for p in cell_pictures]
        blocks.sort(key=lambda b: b[0])
        first = True
        prev_bottom = y0 + 2
        for top, kind, item in blocks:
            paragraph = target.paragraphs[0] if first else target.add_paragraph()
            before = max(0.0, top - prev_bottom - 2) if not first else 0.0
            if kind == "p":
                if item.text.strip() or any(_inline_pictures(b, cell_pictures) for b in item.bands):
                    _write_paragraph(paragraph, item, inside, info, links, cell_pictures, before=before)
            else:
                _picture_paragraph(paragraph, item, inside, table.rtl, before)
            prev_bottom = item.bbox[3]
            first = False
        if first:
            _para_props(target.paragraphs[0], rtl=table.rtl, align="start", before=0)


def _padding(table: Table) -> float:
    """The space between a cell's border and its text, as the page shows it."""
    gaps = []
    for cell in table.cells:
        if cell.lines:
            x0 = min(l.bbox[0] for l in cell.lines)
            x1 = max(l.bbox[2] for l in cell.lines)
            gaps.append(min(x0 - cell.bbox[0], cell.bbox[2] - x1))
    return max(1.0, min(8.0, sorted(gaps)[len(gaps) // 4])) if gaps else 5.4


def _picture_paragraph(paragraph, picture: Picture, frame: Frame, rtl: bool, before: float, page_break: bool = False) -> None:
    x0, y0, x1, y1 = picture.bbox
    width = frame.right - frame.left
    mid = (x0 + x1) / 2
    if abs(mid - (frame.left + frame.right) / 2) < 0.05 * width:
        align, start = "center", 0.0
    elif rtl:
        align, start = ("start", max(0.0, frame.right - x1)) if frame.right - x1 < x0 - frame.left else ("end", 0.0)
    else:
        align, start = ("start", max(0.0, x0 - frame.left)) if x0 - frame.left < frame.right - x1 or x1 - x0 > 0.5 * width else ("end", 0.0)
    _para_props(paragraph, rtl=rtl, align=align, before=before, start=start, page_break=page_break)
    scale = min(1.0, width / (x1 - x0)) if x1 - x0 > width else 1.0
    _add_picture(paragraph, picture, scale)


# Headers and footers ---------------------------------------------------------------------

_DIGITS = re.compile(r"[0-9٠-٩]+")


def _repeated(pages) -> tuple[set[int], set[int]]:
    """Lines at the top or bottom of the page found on most pages at the same place
    (numbers aside): the header and footer. ids of the Line objects."""
    if len(pages) < 2:
        return set(), set()
    seen: dict[tuple, set[int]] = {}
    for n, (page, lines) in enumerate(pages):
        h = page.rect.height
        for l in lines:
            if 0.12 * h < l.bbox[1] and l.bbox[3] < 0.88 * h:
                continue
            key = (_DIGITS.sub("#", l.text.strip()), round(l.bbox[1] / 3), "top" if l.bbox[1] < h / 2 else "bottom")
            seen.setdefault(key, set()).add(n)
    keep = {k for k, ns in seen.items() if len(ns) >= max(2, len(pages) // 2 + 1)}
    top, bottom = set(), set()
    for page, lines in pages:
        h = page.rect.height
        for l in lines:
            if 0.12 * h < l.bbox[1] and l.bbox[3] < 0.88 * h:
                continue
            key = (_DIGITS.sub("#", l.text.strip()), round(l.bbox[1] / 3), "top" if l.bbox[1] < h / 2 else "bottom")
            if key in keep:
                (top if key[2] == "top" else bottom).add(id(l))
    return top, bottom


def _page_fields(paragraph, text: str, look: Look, page_no: int, count: int) -> bool:
    """ "Page 1 of 2" as Word fields that count by themselves. False when the text has
    no page numbers in it."""
    numbers = [(m.start(), m.end(), m.group()) for m in _DIGITS.finditer(text)]
    if not numbers or not any(int(_ascii(n)) == page_no + 1 for *_, n in numbers):
        return False
    pos = 0
    for a, b, n in numbers:
        if a > pos:
            _add_runs(paragraph, [(text[pos:a], look)], {})
        value = int(_ascii(n))
        instr = "PAGE" if value == page_no + 1 else "NUMPAGES" if value == count else None
        if instr:
            field_ = _el("w:fldSimple", instr=instr)
            r = _el("w:r")
            r.append(_rpr(look))
            t = _el("w:t")
            t.text = n
            r.append(t)
            field_.append(r)
            paragraph._p.append(field_)
        else:
            _add_runs(paragraph, [(n, look)], {})
        pos = b
    if pos < len(text):
        _add_runs(paragraph, [(text[pos:], look)], {})
    return True


def _ascii(digits: str) -> str:
    return digits.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))


def _write_band(part, lines: list[Line], pictures: list[Picture], frame: Frame, page_no: int, count: int, info: PageInfo) -> None:
    """A header or footer: its pictures and lines, top to bottom."""
    blocks = [(p.bbox[1], p) for p in pictures] + [(p.bbox[1], p) for p in flow.paragraphs(lines, frame.left, frame.right)]
    blocks.sort(key=lambda b: b[0])
    first = True
    for _, item in blocks:
        paragraph = part.paragraphs[0] if first else part.add_paragraph()
        first = False
        if isinstance(item, Picture):
            _picture_paragraph(paragraph, item, frame, False, 0.0)
            continue
        align = _alignment(item, frame)
        start, first_ind = _indents(item, frame, align)
        _para_props(paragraph, rtl=item.rtl, align=align, before=0, start=start, first=first_ind)
        look = segments(item.lines[0], info)[0][1] if segments(item.lines[0], info) else _space_look(item)
        if not _page_fields(paragraph, item.text, look, page_no, count):
            for k, band in enumerate(item.bands):
                for j, line in enumerate(band):
                    if k or j:
                        _add_runs(paragraph, [(" ", look)], {})
                    _add_runs(paragraph, segments(line, info), {})


# The document ----------------------------------------------------------------------------


def document(src: bytes, name: str) -> exporting.Export:
    import docx
    from docx.shared import Pt

    doc = pymupdf.open("pdf", src)
    pdf = pikepdf.open(io.BytesIO(src))
    pages = [(page, page_lines(page)) for page in doc]
    count = len(pages)
    header_ids, footer_ids = _repeated(pages)
    body_lines = [l for _, lines in pages for l in lines if id(l) not in header_ids | footer_ids]
    left, right = flow.edges(body_lines or [l for _, ls in pages for l in ls])
    first = doc[0].rect
    if right <= left:
        left, right = 72.0, first.width - 72.0
    # Where lines reach is a little inside the text area when they don't fill it (text
    # that isn't justified). Pages usually have equal side margins: the text area is
    # taken as that when it's close.
    rtl_doc = flow.rtl(body_lines) if body_lines else False
    if rtl_doc and 0 < left - (first.width - right) < 30:
        frame = Frame(first.width - right, right)
    elif not rtl_doc and 0 < (first.width - left) - right < 30:
        frame = Frame(left, first.width - left)
    else:
        frame = Frame(left, right)
    if frame.left < 0 or frame.right > first.width or frame.right - frame.left < 0.3 * first.width:
        frame = Frame(72.0, first.width - 72.0)  # text too odd to tell the margins by

    word = docx.Document()
    normal = word.styles["Normal"]
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.line_spacing = 1.0
    tops = [min((l.bbox[1] for l in lines if id(l) not in header_ids), default=72.0) for _, lines in pages]
    bottoms = [max((l.bbox[3] for l in lines if id(l) not in footer_ids), default=first.height - 72.0) for _, lines in pages]
    top_margin = max(18.0, min(min(tops) - 2, first.height / 3))
    bottom_margin = max(18.0, min(first.height - max(bottoms) - 2, 72.0))

    def page_setup(section, rect) -> None:
        """A page's size; margins as the text area, kept on the page."""
        section.page_width, section.page_height = Pt(rect.width), Pt(rect.height)
        side = min(frame.left, rect.width / 4)
        section.left_margin = Pt(max(12.0, side))
        section.right_margin = Pt(max(12.0, min(first.width - frame.right, rect.width / 4)))
        section.top_margin = Pt(min(top_margin, rect.height / 4))
        section.bottom_margin = Pt(min(bottom_margin, rect.height / 4))
        section.header_distance = Pt(18)
        section.footer_distance = Pt(18)

    section = word.sections[0]
    page_setup(section, first)

    links: dict = {}
    notes = 0
    listed = images.all_page_images(src)
    for n, (page, lines) in enumerate(pages):
        h = page.rect.height
        # A page of another size or the other way round starts a new Word section.
        new_size = n > 0 and (abs(page.rect.width - pages[n - 1][0].rect.width) > 1 or abs(h - pages[n - 1][0].rect.height) > 1)
        if new_size:
            from docx.enum.section import WD_SECTION

            page_setup(word.add_section(WD_SECTION.NEW_PAGE), page.rect)
        fills = _segments(page)[2]
        info = _page_info(pdf, n, page, lines, fills)
        found = listed[n]
        text_lines = [l for l in lines if l.text.strip()]
        pictures = [p for im in found if (not im.background or not text_lines) and (p := _picture(pdf, n, im))]
        head = [l for l in lines if id(l) in header_ids]
        foot = [l for l in lines if id(l) in footer_ids]
        head_pics = [p for p in pictures if p.image.pages >= max(2, count // 2 + 1) and p.bbox[3] < 0.15 * h]
        foot_pics = [p for p in pictures if p.image.pages >= max(2, count // 2 + 1) and p.bbox[1] > 0.85 * h]
        if n == 0:
            if head or head_pics:
                _write_band(section.header, head, head_pics, frame, n, count, info)
            if foot or foot_pics:
                _write_band(section.footer, foot, foot_pics, frame, n, count, info)
        pictures = [p for p in pictures if p not in head_pics and p not in foot_pics]
        body = [l for l in lines if id(l) not in header_ids | footer_ids]
        notes += sum(1 for l in body if l.uncertain)
        tables = find_tables(page, body)
        in_tables = {id(l) for t in tables for l in t.lines}
        paras = flow.paragraphs([l for l in body if id(l) not in in_tables], left, right)
        # Pictures inside a table go into its cells; small ones within a line of text
        # into that line; the rest on lines of their own.
        free = [p for p in pictures if not any(t.bbox[0] <= (p.bbox[0] + p.bbox[2]) / 2 <= t.bbox[2] and t.bbox[1] <= (p.bbox[1] + p.bbox[3]) / 2 <= t.bbox[3] for t in tables)]
        inline = [p for p in free if any(_inline_pictures(b, [p]) for para in paras for b in para.bands)]
        blocks = [(p.bbox[1], "p", p) for p in paras] + [(t.bbox[1], "t", t) for t in tables] + [(p.bbox[1], "i", p) for p in free if p not in inline]
        blocks.sort(key=lambda b: b[0])
        prev_bottom = min(tops)
        extra = 0.0
        for k, (top, kind, item) in enumerate(blocks):
            before = max(0.0, top - prev_bottom - 2) if k else max(0.0, top - min(tops))
            page_break = n > 0 and k == 0 and not new_size
            before = max(0.0, before - extra)
            extra = 0.0
            if kind == "p":
                shade = _shade_behind(fills, item)
                _write_paragraph(word.add_paragraph(), item, frame, info, links, inline, before=before, page_break=page_break, shade=shade)
                prev_bottom = item.bbox[3]
                last = item.bands[-1]
                if len(item.bands) == 1 or word_line(item.lines[0].style) <= (item.bbox[3] - item.bbox[1]) / len(item.bands) + 0.5:
                    extra = _extra(last)
            elif kind == "t":
                _write_table(word, item, frame, info, links, pictures, before, page_break)
                prev_bottom = item.bbox[3]
            else:
                _picture_paragraph(word.add_paragraph(), item, frame, flow.rtl(lines) if lines else False, before, page_break)
                prev_bottom = item.bbox[3]
        if not blocks and n > 0 and not new_size:
            _para_props(word.add_paragraph(), rtl=False, align="start", before=0, page_break=True)
    # Word wants something after a table that ends the document.
    if len(word.element.body) > 1 and word.element.body[-2].tag.endswith("}tbl"):
        word.add_paragraph()
    out = io.BytesIO()
    word.save(out)
    export = exporting.Export(out.getvalue(), f"{name}.docx", DOCX)
    if notes:
        export.note = (
            f"{notes} line{'s' if notes > 1 else ''} in the Word file {'are' if notes > 1 else 'is'} highlighted in yellow: "
            f"the PDF stores some of {'their' if notes > 1 else 'its'} letters wrongly. Check {'them' if notes > 1 else 'it'} against the page."
        )
    return export


def _shade_behind(fills, para: flow.Paragraph) -> str | None:
    """The color of a box behind the whole paragraph (a shaded note), not just some words."""
    x0, y0, x1, y1 = para.bbox
    behind = [(r.width * r.height, c) for r, c in fills if r.x0 <= x0 + 1 and r.x1 >= x1 - 1 and r.y0 <= y0 + 1 and r.y1 >= y1 - 1 and c != "ffffff" and r.height < 6 * (y1 - y0 + 4)]
    return min(behind)[1] if behind else None
