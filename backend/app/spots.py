"""What adding text at a clicked spot means: one "Add text" for everything.

The person clicks where the new text should go; the editor works out how, and says so
before anything is written:
  - under a list item's last line (or above its first): the list's next item, with its
    bullet or number;
  - on a line, between two lines, or just under one: a new line there, the text below
    moving down to make room;
  - beside a line, on its row: the text joins that line;
  - in empty space (an empty table cell, a margin, a blank area): the text goes where
    clicked.
Nothing here writes to the file: adding.py and lists.py do, with their own checks.
Positions are worked out in PDF space (y up), where table borders are.
"""

import io
from dataclasses import dataclass

import pikepdf
import pymupdf

from .adding import _lines_above, _lines_below
from .changes import to_display, to_pdf
from .lists import place, shifted
from .moving import _crop, column_of
from .pdfcontent import page_matrix, replay
from .textlayout import Line, page_lines


@dataclass
class Plan:
    kind: str  # "item", "line", "join" or "free"
    line: Line | None  # the line the new one goes below or above (item, line) or joins (join)
    where: str  # "below" or "above"
    marker: str | None  # a new item's bullet or number
    box: tuple[float, float, float, float]  # where it goes, as displayed (for item and line: the new line's band)
    label: str  # what will happen, in a few plain words

    def json(self) -> dict:
        return {
            "kind": self.kind,
            "lineId": self.line.id if self.line else None,
            "oldText": self.line.text if self.line else None,
            "where": self.where,
            "marker": self.marker,
            "box": [round(v, 2) for v in self.box],
            "label": self.label,
        }


def _pitch(line: Line, lines: list[Line]) -> float:
    """Top to top, from one line of its paragraph to the next."""
    below = _lines_below(line, lines)
    if below:
        return below[0].written[1] - line.written[1]
    above = _lines_above(line, lines)
    if above:
        return line.written[1] - above[0].written[1]
    return 1.2 * (line.written[3] - line.written[1])


def plan(src: bytes, page_no: int, x: float, y: float) -> Plan:
    """What adding text at (x, y) on the page, as displayed, would do."""
    doc = pymupdf.open("pdf", src)
    page = doc[page_no]
    lines = [l for l in page_lines(page) if l.text.strip()]
    with pikepdf.open(io.BytesIO(src)) as pdf:
        rp = replay(pdf, page_no)
        crop = _crop(pdf, page_no)
    tap = pymupdf.Point(x, y) * (page.derotation_matrix * ~page_matrix(page))
    rect = {l.id: to_pdf(page, l.bbox) for l in lines}  # x0, y0 (bottom), x1, y1 (top)
    caret = (x - 1, y - 6, x + 1, y + 6)

    def in_column(l: Line) -> bool:
        """The click is in the line's column: its table cell, or the page's width."""
        cx0, cx1, _cell = column_of(rect[l.id], rp, crop)
        return cx0 - 1 <= tap.x <= cx1 + 1

    # Table borders and other horizontal lines: text doesn't reach across them.
    rules = [
        s.bbox for p in rp.paths if not p.clip and (p.fills or p.strokes)
        for s in p.subpaths if s.bbox[3] - s.bbox[1] <= 3 and s.bbox[2] - s.bbox[0] >= 20
    ]

    def apart(low: float, high: float) -> bool:
        return any(low < (r[1] + r[3]) / 2 < high and r[0] - 1 <= tap.x <= r[2] + 1 for r in rules)

    def band(line: Line, where: str) -> tuple:
        """The new line's place: its paragraph's width, one line spacing under the line
        (adding above: where the line is now)."""
        para = [line, *_lines_above(line, lines), *_lines_below(line, lines)]
        x0, x1 = min(rect[l.id][0] for l in para), max(rect[l.id][2] for l in para)
        _, y0, _, y1 = rect[line.id]
        step = _pitch(line, lines) if where == "below" else 0.0
        return tuple(to_display(page, (x0, y0 - step, x1, y1 - step)))

    def item_or_line(line: Line, where: str) -> Plan:
        box = band(line, where)
        spot = place(line, lines, rp)
        if spot is not None and (spot.last if where == "below" else spot.first):
            marker = shifted(spot.marker, 1, spot.roman) if where == "below" else spot.marker
            if marker:
                text = f"A new item ({marker}) {'under' if where == 'below' else 'above'} this one."
                return Plan("item", line, where, marker, box, text)
        return Plan("line", line, where, None, box, "A new line here. The text below moves down to make room.")

    # On a line: a new line just there, under it or above it.
    for l in lines:
        x0, y0, x1, y1 = rect[l.id]
        if x0 - 1 <= tap.x <= x1 + 1 and y0 - 1 <= tap.y <= y1 + 1:
            return item_or_line(l, "below" if tap.y <= (y0 + y1) / 2 else "above")
    # Beside a line, on its row, in its column: it joins the line.
    for l in lines:
        x0, y0, x1, y1 = rect[l.id]
        h = y1 - y0
        if y0 - 0.25 * h <= tap.y <= y1 + 0.25 * h and (tap.x > x1 + 1 or tap.x < x0 - 1) and in_column(l):
            side = "after its last word" if (tap.x > x1) != l.rtl else "before its first word"
            return Plan("join", l, "below", None, caret, f"Your text goes on this line, {side}.")
    # Between lines, or just under or over one: a new line there. Not across a border.
    above = [l for l in lines if rect[l.id][1] >= tap.y and in_column(l) and not apart(tap.y, rect[l.id][1])]
    below = [l for l in lines if rect[l.id][3] <= tap.y and in_column(l) and not apart(rect[l.id][3], tap.y)]
    a = min(above, key=lambda l: rect[l.id][1], default=None)
    b = max(below, key=lambda l: rect[l.id][3], default=None)
    if a is not None and rect[a.id][1] - tap.y <= 1.5 * _pitch(a, lines):
        return item_or_line(a, "below")
    if b is not None and tap.y - rect[b.id][3] <= 0.8 * _pitch(b, lines):
        return item_or_line(b, "above")
    return Plan("free", None, "below", None, caret, "Your text goes where you clicked.")
