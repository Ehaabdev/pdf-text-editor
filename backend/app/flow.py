"""Reading order and paragraphs, for Download as Word and Excel.

A PDF keeps lines, not paragraphs. Lines side by side on one row (a stretched line can
be read as several pieces) are read in their direction: left to right, or right to left
for Arabic. Consecutive rows are one paragraph when the first was wrapped: when the next
row's first word wouldn't have fitted at its end, as a word processor wraps text. A
larger gap, a change of size, or a list marker starts a new paragraph.
"""

import re
import statistics
from dataclasses import dataclass

from .textlayout import Line

_MARKER = re.compile(r"^\s*(?:[•●○■□▪▫◦‣∙·\-–—*]|\(?[0-9٠-٩]{1,3}[.)\-]|\(?[a-zA-Zأ-ي][.)]|[ivxIVX]{1,4}[.)])\s")


def rtl(lines: list[Line]) -> bool:
    """Mostly right-to-left text?"""
    r = sum(len(l.text) for l in lines if l.rtl)
    return r > sum(len(l.text) for l in lines) / 2


def bands(lines: list[Line]) -> list[list[Line]]:
    """Lines side by side, in rows from the top; each row in reading order."""
    rows: list[list[Line]] = []
    for line in sorted(lines, key=lambda l: (l.bbox[1], l.bbox[0])):
        mid = (line.bbox[1] + line.bbox[3]) / 2
        for row in rows:
            if min(l.bbox[1] for l in row) <= mid <= max(l.bbox[3] for l in row):
                row.append(line)
                break
        else:
            rows.append([line])
    for row in rows:
        row.sort(key=lambda l: l.bbox[0], reverse=rtl(row))
    return rows


def band_text(band: list[Line]) -> str:
    return " ".join(t for t in (l.text.strip() for l in band) if t)


def _height(band: list[Line]) -> float:
    return max(l.bbox[3] for l in band) - min(l.bbox[1] for l in band)


def _size(band: list[Line]) -> float:
    sizes = [s.size for l in band for _, s in l.spans] or [_height(band)]
    return statistics.median(sizes)


def _first_word(band: list[Line]) -> float:
    """How wide the row's first word is (points), with the space before it."""
    line = band[0]
    along = []  # glyph extents along the text's direction (pages can be turned)
    for i in line.order:
        g = line.glyphs[i]
        if g.is_space and along:
            break
        if not g.is_space:
            dx, dy = g.direction
            x0, y0, x1, y1 = g.bbox
            along += [x * dx + y * dy for x in (x0, x1) for y in (y0, y1)]
    width = max(along) - min(along) if along else 0.0
    return width + 0.3 * _size(band)


@dataclass
class Paragraph:
    bands: list[list[Line]]
    # Where its text wraps: the edges of its column, box or cell (points).
    wrap: tuple[float, float] = (0.0, 0.0)

    @property
    def lines(self) -> list[Line]:
        return [l for b in self.bands for l in b]

    @property
    def text(self) -> str:
        return " ".join(band_text(b) for b in self.bands)

    @property
    def rtl(self) -> bool:
        return rtl(self.lines)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        ls = self.lines
        return min(l.bbox[0] for l in ls), min(l.bbox[1] for l in ls), max(l.bbox[2] for l in ls), max(l.bbox[3] for l in ls)


def edges(lines: list[Line]) -> tuple[float, float]:
    """Where text starts and ends in a column: the edges most long lines reach."""
    if not lines:
        return 0.0, 0.0
    widest = max(l.bbox[2] - l.bbox[0] for l in lines)
    long = [l for l in lines if l.bbox[2] - l.bbox[0] >= 0.5 * widest] or lines
    rights = sorted(l.bbox[2] for l in long)
    lefts = sorted(l.bbox[0] for l in long)
    return lefts[len(lefts) // 10], rights[-1 - len(rights) // 10]


def paragraphs(lines: list[Line], left: float | None = None, right: float | None = None) -> list[Paragraph]:
    """The lines (of one column or one table cell) as paragraphs, top to bottom."""
    rows = bands(lines)
    if not rows:
        return []
    if left is None or right is None:
        left, right = edges(lines)
    gaps = [b[0].bbox[1] - max(l.bbox[3] for l in a) for a, b in zip(rows, rows[1:])]
    # The gap between lines of one paragraph: the smallest gap that comes up more than
    # once (between paragraphs there's usually more).
    # Bigger text has bigger gaps: measured for each size of text, when it comes up.
    def smallest_repeated(values: list[float]) -> float | None:
        seen = sorted(g for g in values if g >= -1)
        repeated = [g for g in seen if sum(1 for h in seen if abs(h - g) <= 1.0) >= 2]
        return repeated[0] if repeated else None

    by_size: dict[float, list[float]] = {}
    for a, b, g in zip(rows, rows[1:], gaps):
        if abs(_size(a) - _size(b)) <= 0.15 * _size(a):
            by_size.setdefault(round(_size(a)), []).append(g)
    overall = smallest_repeated(gaps)
    if overall is None:
        overall = min((g for g in gaps if g >= -1), default=0.0)
    usual = {k: v if (v := smallest_repeated(gs)) is not None else overall for k, gs in by_size.items()}
    # Text in a box or a column narrower than the page wraps at the box's edges, less its
    # inner margin (as much as the text keeps from its left edge).
    # A column is trusted when it's a drawn box (the text keeps a margin inside it) or
    # when other text runs beside it (a page in two columns); not when it's only as wide
    # as a few short lines happen to be.
    inner: dict[tuple[float, float], float] = {}
    members: dict[tuple[float, float], list[Line]] = {}
    for l in lines:
        if l.column:
            inner[l.column] = min(inner.get(l.column, l.bbox[0]), l.bbox[0])
            members.setdefault(l.column, []).append(l)

    def beside(c) -> bool:
        top = min(l.bbox[1] for l in members[c])
        bottom = max(l.bbox[3] for l in members[c])
        return any((l.bbox[0] >= c[1] or l.bbox[2] <= c[0]) and l.bbox[1] < bottom and l.bbox[3] > top for l in lines)

    def shared(values: list[float], at_least: float, nearest_high: bool) -> float | None:
        """An edge at least three nearby lines end on (justified text, a narrow column):
        the nearest one past this line's own end."""
        ends = sorted(values)
        found = []
        for v in ends:
            if sum(1 for w in ends if abs(w - v) <= 1.0) >= 3:
                found.append(v)
        found = [v for v in found if (v >= at_least - 1.5 if nearest_high else v <= at_least + 1.5)]
        if not found:
            return None
        return min(found) if nearest_high else max(found)

    def sides(band: list[Line]) -> tuple[float, float]:
        cols = {l.column for l in band}
        if len(cols) == 1 and (c := cols.pop()) and c[1] - c[0] < 0.9 * (right - left):
            pad = inner[c] - c[0]
            if pad > 2:
                return c[0] + pad, c[1] - pad
            if beside(c):
                return c
        top = band[0].bbox[1]
        # Evidence of an edge comes from lines of some length, not short labels or numbers.
        near = [l for l in lines if abs(l.bbox[1] - top) < 300 and l.bbox[2] - l.bbox[0] >= (right - left) / 3]
        x0, x1 = min(l.bbox[0] for l in band), max(l.bbox[2] for l in band)
        r = shared([l.bbox[2] for l in near], x1, True)
        l_ = shared([l.bbox[0] for l in near], x0, False)
        a = l_ if l_ is not None and l_ > left else left
        b = r if r is not None and r < right else right
        # Short lines centered one under another share their ends too: a column narrower
        # than a third of the page is taken for that, not for a column.
        return (a, b) if b - a >= (right - left) / 3 else (left, right)

    out = [Paragraph([rows[0]], sides(rows[0]))]
    for prev, row, gap in zip(rows, rows[1:], gaps):
        if _continues(prev, row, gap, usual.get(round(_size(prev)), overall), *sides(prev)):
            out[-1].bands.append(row)
        else:
            out.append(Paragraph([row], sides(row)))
    return out


def _continues(prev, row, gap, usual, left, right) -> bool:
    size = _size(prev)
    if abs(_size(row) - size) > 0.15 * size:
        return False
    if gap > max(usual, 0) + max(2.0, 0.25 * size) or gap > 1.2 * size:
        return False
    if _MARKER.match(band_text(row)):
        return False
    backwards = rtl(prev)
    if backwards != rtl(row):
        return False
    # Wrapped: the next row's first word wouldn't have fitted at the end of this one,
    # and this one starts near the column's start (a first line may be indented). A
    # short line pushed to the far side (right-aligned) wasn't wrapped.
    x0, x1 = min(l.bbox[0] for l in prev), max(l.bbox[2] for l in prev)
    end, start = (x0 - left, right - x1) if backwards else (right - x1, x0 - left)
    word = _first_word(row)
    return end < word + 2.0 and start < word + 40.0
