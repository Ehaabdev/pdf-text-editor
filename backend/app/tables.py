"""Tables on a page, for Download as Excel and Word: where their rows and columns are,
which cells are merged, and which lines of text are in each cell.

The text always comes from our own lines (textlayout.page_lines), which read Arabic and
mixed text correctly. The grid comes from the page's drawings:

- A ruled table is drawn with borders. Its columns are where vertical borders are, its
  rows where horizontal ones are; two neighbouring cells without a border between them
  are one merged cell. Word draws each cell's border as its own short piece, often a
  fraction of a point off its neighbour's, so pieces at nearly the same place count as one.
- A table without borders (common in pages printed from a browser) is found from the
  text: several rows in a row, each in two or more pieces, with gaps between the pieces
  at the same places in every row.
"""

from dataclasses import dataclass, field

import pymupdf

from .textlayout import Line

THIN = 2.5  # a rectangle this thin is a line
NEAR = 2.0  # borders this close are the same border
TOUCH = 3.0  # pieces of border this close are joined


@dataclass
class Cell:
    row: int
    col: int
    rowspan: int
    colspan: int
    bbox: tuple[float, float, float, float]
    lines: list[Line] = field(default_factory=list)
    fill: str | None = None  # "rrggbb" when the cell is shaded


@dataclass
class Table:
    bbox: tuple[float, float, float, float]
    rows: list[float]  # row edges, top to bottom
    cols: list[float]  # column edges, left to right
    cells: list[Cell]
    ruled: bool
    rtl: bool = False  # mostly right-to-left text: its first column is on the right

    @property
    def lines(self) -> list[Line]:
        return [l for c in self.cells for l in c.lines]


# Borders ---------------------------------------------------------------------------------


def _segments(page: pymupdf.Page):
    """Horizontal (y, x0, x1) and vertical (x, y0, y1) lines drawn on the page, as
    displayed, and filled rectangles (rect, color) that may shade cells."""
    m = page.rotation_matrix
    hs, vs, fills = [], [], []

    def edge(p: pymupdf.Point, q: pymupdf.Point):
        p, q = p * m, q * m
        if abs(p.y - q.y) < 0.5 and abs(p.x - q.x) > 1:
            hs.append(((p.y + q.y) / 2, min(p.x, q.x), max(p.x, q.x)))
        elif abs(p.x - q.x) < 0.5 and abs(p.y - q.y) > 1:
            vs.append(((p.x + q.x) / 2, min(p.y, q.y), max(p.y, q.y)))

    for d in page.get_drawings():
        stroked = d.get("color") is not None and d.get("type") in ("s", "fs")
        filled = d.get("fill") is not None and d.get("type") in ("f", "fs")
        for item in d["items"]:
            if item[0] == "l":
                edge(item[1], item[2])
            elif item[0] == "re":
                r = pymupdf.Rect(item[1]) * m
                if filled and r.height < THIN and r.width > 1:
                    hs.append(((r.y0 + r.y1) / 2, r.x0, r.x1))
                elif filled and r.width < THIN and r.height > 1:
                    vs.append(((r.x0 + r.x1) / 2, r.y0, r.y1))
                else:
                    if stroked:
                        for a, b in ((r.tl, r.tr), (r.bl, r.br), (r.tl, r.bl), (r.tr, r.br)):
                            hs.append((a.y, a.x, b.x)) if a.y == b.y else vs.append((a.x, a.y, b.y))
                    if filled and r.width >= THIN and r.height >= THIN:
                        fills.append((r, _hex(d["fill"])))
            elif item[0] == "qu":
                q = item[1]
                r = q.rect * m
                if filled and q.is_rectangular and r.width >= THIN and r.height >= THIN:
                    fills.append((r, _hex(d["fill"])))
    return _merge(hs), _merge(vs), fills


def _hex(color) -> str:
    return "".join(f"{round(max(0, min(1, c)) * 255):02x}" for c in (list(color) + [0, 0, 0])[:3])


def _merge(segments):
    """Pieces of the same line, end to end, made one."""
    out = []
    for pos, a, b in sorted(segments):
        for s in out:
            if abs(s[0] - pos) <= NEAR and a <= s[2] + TOUCH and b >= s[1] - TOUCH:
                s[1], s[2] = min(s[1], a), max(s[2], b)
                break
        else:
            out.append([pos, a, b])
    return [tuple(s) for s in out]


def _cluster(values: list[float]) -> list[float]:
    out: list[list[float]] = []
    for v in sorted(values):
        if out and v - out[-1][-1] <= NEAR:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


def _groups(hs, vs):
    """Borders that touch each other, in groups: each group a table's grid."""
    items = [("h", s) for s in hs] + [("v", s) for s in vs]
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, (ki, a) in enumerate(items):
        if ki != "h":
            continue
        for j, (kj, b) in enumerate(items):
            if kj != "v":
                continue
            y, x0, x1 = a
            x, y0, y1 = b
            if x0 - TOUCH <= x <= x1 + TOUCH and y0 - TOUCH <= y <= y1 + TOUCH:
                parent[find(i)] = find(j)
    groups: dict[int, tuple[list, list]] = {}
    for i, (k, s) in enumerate(items):
        g = groups.setdefault(find(i), ([], []))
        (g[0] if k == "h" else g[1]).append(s)
    return [g for g in groups.values() if g[0] and g[1]]


def _covers(segments, pos: float, a: float, b: float) -> bool:
    """Is there a border at pos running along the middle of a..b?"""
    mid = (a + b) / 2
    reach = max(1.0, (b - a) * 0.25)
    return any(abs(s[0] - pos) <= NEAR and s[1] <= mid - reach + TOUCH and s[2] >= mid + reach - TOUCH for s in segments)


def _ruled(hs, vs, fills, lines: list[Line]) -> list[Table]:
    tables = []
    for gh, gv in _groups(hs, vs):
        cols = _cluster([s[0] for s in gv])
        rows = _cluster([s[0] for s in gh])
        if len(cols) < 2 or len(rows) < 2:
            continue
        # The outer edges come from the border pieces' ends too: a table drawn with
        # only inner lines still ends where its lines do.
        x0, x1 = min(cols[0], min(s[1] for s in gh)), max(cols[-1], max(s[2] for s in gh))
        y0, y1 = min(rows[0], min(s[1] for s in gv)), max(rows[-1], max(s[2] for s in gv))
        cols = _cluster([x0, *cols, x1])
        rows = _cluster([y0, *rows, y1])
        nr, nc = len(rows) - 1, len(cols) - 1
        # Neighbouring grid cells with no border between them are one cell.
        right = [[_covers(gv, cols[c + 1], rows[r], rows[r + 1]) for c in range(nc - 1)] for r in range(nr)]
        down = [[_covers(gh, rows[r + 1], cols[c], cols[c + 1]) for c in range(nc)] for r in range(nr - 1)]
        cells = _merged_cells(rows, cols, right, down)
        if cells is None:
            continue
        table = Table((cols[0], rows[0], cols[-1], rows[-1]), rows, cols, cells, ruled=True)
        _fill(table, lines, fills)
        if len(cells) < 2 or not table.lines:
            continue
        tables.append(table)
    return tables


def _merged_cells(rows, cols, right, down) -> list[Cell] | None:
    nr, nc = len(rows) - 1, len(cols) - 1
    taken = [[False] * nc for _ in range(nr)]
    cells = []
    for r in range(nr):
        for c in range(nc):
            if taken[r][c]:
                continue
            # As wide as there's no border to the right, then as tall as the whole
            # width has no border below.
            w = 1
            while c + w < nc and not right[r][c + w - 1] and not taken[r][c + w]:
                w += 1
            h = 1
            while r + h < nr and all(not down[r + h - 1][c + k] for k in range(w)) and not any(taken[r + h][c + k] for k in range(w)):
                h += 1
            for i in range(r, r + h):
                for k in range(c, c + w):
                    taken[i][k] = True
            cells.append(Cell(r, c, h, w, (cols[c], rows[r], cols[c + w], rows[r + h])))
    return cells


def _center(line: Line) -> tuple[float, float]:
    x0, y0, x1, y1 = line.bbox
    return (x0 + x1) / 2, (y0 + y1) / 2


def _fill(table: Table, lines: list[Line], fills) -> None:
    for line in lines:
        x, y = _center(line)
        for cell in table.cells:
            x0, y0, x1, y1 = cell.bbox
            if x0 <= x <= x1 and y0 <= y <= y1:
                cell.lines.append(line)
                break
    for cell in table.cells:
        cell.lines.sort(key=lambda l: (l.bbox[1], l.bbox[0]))
        r = pymupdf.Rect(cell.bbox)
        shades = [(f.width * f.height, color) for f, color in fills if f.contains(pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)) and color != "ffffff" and f.width * f.height < r.width * r.height * 4]
        if shades:
            cell.fill = min(shades)[1]
    rtl = sum(len(l.text) for l in table.lines if l.rtl)
    table.rtl = rtl > sum(len(l.text) for l in table.lines) / 2


# Tables without borders ------------------------------------------------------------------


def _bands(lines: list[Line]) -> list[list[Line]]:
    """Lines side by side, in rows, top to bottom."""
    bands: list[list[Line]] = []
    for line in sorted(lines, key=lambda l: (l.bbox[1], l.bbox[0])):
        y = _center(line)[1]
        for band in bands:
            top = min(l.bbox[1] for l in band)
            bottom = max(l.bbox[3] for l in band)
            if top <= y <= bottom:
                band.append(line)
                break
        else:
            bands.append([line])
    for band in bands:
        band.sort(key=lambda l: l.bbox[0])
    return bands


def _gaps(band: list[Line]) -> list[tuple[float, float]]:
    return [(a.bbox[2], b.bbox[0]) for a, b in zip(band, band[1:]) if b.bbox[0] - a.bbox[2] > 4]


def _common(gaps, other) -> list[tuple[float, float]]:
    out = []
    for a0, a1 in gaps:
        for b0, b1 in other:
            lo, hi = max(a0, b0), min(a1, b1)
            if hi - lo >= 4:
                out.append((lo, hi))
    return out


def _unruled(lines: list[Line], fills) -> list[Table]:
    bands = _bands(lines)
    tables = []
    i = 0
    while i < len(bands):
        if len(bands[i]) < 2:
            i += 1
            continue
        run, gaps = [bands[i]], _gaps(bands[i])
        j = i + 1
        while j < len(bands) and gaps:
            prev, band = run[-1], bands[j]
            pitch = max(l.bbox[3] - l.bbox[1] for l in prev)
            if min(l.bbox[1] for l in band) - max(l.bbox[3] for l in prev) > 1.6 * pitch:
                break  # a paragraph's distance away: not the same table
            if len(band) < 2:
                # A cell's second line: it must keep within one column.
                (line,) = band
                if any(line.bbox[0] < g1 and line.bbox[2] > g0 for g0, g1 in gaps):
                    break
                run.append(band)
                j += 1
                continue
            common = _common(gaps, _gaps(band))
            if not common:
                break
            run, gaps = run + [band], common
            j += 1
        # Three rows in pieces at least: a stretched line of a paragraph can be read as
        # pieces too, but not three in a row with gaps at the same places.
        multi = [b for b in run if len(b) >= 2]
        if len(multi) >= 3 and any(g1 - g0 >= 8 for g0, g1 in gaps):
            tables.append(_from_bands(run, gaps, fills))
            i = j
        else:
            i += 1
    return tables


def _from_bands(run: list[list[Line]], gaps, fills) -> Table:
    all_lines = [l for b in run for l in b]
    x0 = min(l.bbox[0] for l in all_lines)
    x1 = max(l.bbox[2] for l in all_lines)
    cols = [x0, *[(g0 + g1) / 2 for g0, g1 in sorted(gaps)], x1]
    rows = [min(l.bbox[1] for l in run[0])]
    for a, b in zip(run, run[1:]):
        rows.append((max(l.bbox[3] for l in a) + min(l.bbox[1] for l in b)) / 2)
    rows.append(max(l.bbox[3] for l in run[-1]))
    cells = [Cell(r, c, 1, 1, (cols[c], rows[r], cols[c + 1], rows[r + 1])) for r in range(len(rows) - 1) for c in range(len(cols) - 1)]
    table = Table((x0, rows[0], x1, rows[-1]), rows, cols, cells, ruled=False)
    _fill(table, all_lines, fills)
    return table


# All tables ------------------------------------------------------------------------------


def find_tables(page: pymupdf.Page, lines: list[Line]) -> list[Table]:
    """The page's tables, top to bottom. A line belongs to one table at most."""
    hs, vs, fills = _segments(page)
    ruled = _ruled(hs, vs, fills, lines)
    used = {id(l) for t in ruled for l in t.lines}
    inside = [t.bbox for t in ruled]
    rest = [l for l in lines if id(l) not in used and not any(_within(_center(l), b) for b in inside)]
    tables = ruled + _unruled(rest, fills)
    return sorted(tables, key=lambda t: t.bbox[1])


def _within(p, box) -> bool:
    return box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]
