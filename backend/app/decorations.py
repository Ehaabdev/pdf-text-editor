"""Underlines, strikethroughs and highlights: shapes that belong to the text above them.

Word and Chrome draw each one as a filled rectangle of its own, next to the text, not
as part of the font. When text is deleted its decorations must go too, and when words
change the decorations must follow them. Shapes that only look similar belong to the
page and must stay: a paragraph's bottom border, a table border right under a cell's
text, a cell's shading, the footnote separator. What tells them apart is that a
decoration starts and ends exactly at letter edges of one line, at a set height
relative to its baseline.
"""

from dataclasses import dataclass

from .pdfcontent import DrawnPath, Replay
from .textlayout import Line

EDGE = 1.0  # how close (pt) a decoration's ends sit to letter edges


@dataclass
class Decoration:
    path: int  # index into Replay.paths
    kind: str  # "underline", "strike" or "highlight"
    x0: float  # PDF x of its ends
    x1: float
    traces: frozenset[int]  # the line's glyphs it runs under or over
    shared: bool = False  # drawn in one path together with other shapes


def _extent(rp: Replay, t: int) -> tuple[float, float]:
    g = rp.glyphs[t]
    width = g.advance / 1000 * g.paint.size * g.paint.Tz * g.paint.trm2[0]
    return g.origin[0], g.origin[0] + max(width, 0.0)


def _vertical_rules(rp: Replay) -> list[tuple[float, float, float]]:
    out = []
    for p in rp.paths:
        if p.clip or not (p.fills or p.strokes):
            continue
        for s in p.subpaths:
            x0, y0, x1, y1 = s.bbox
            if x1 - x0 <= 2.0 and y1 - y0 >= 4.0:
                out.append(((x0 + x1) / 2, y0, y1))
    return out


def for_line(line: Line, rp: Replay) -> list[Decoration]:
    """The decorations drawn on this line, in drawing order."""
    members = [t for t in line.members]
    if not members:
        return []
    main = rp.glyphs[members[0]]
    for t in members:
        if rp.glyphs[t].advance > 0:
            main = rp.glyphs[t]
            break
    size = main.paint.size * main.paint.trm2[3]
    base = main.origin[1]
    if size <= 0:
        return []
    extents = {t: _extent(rp, t) for t in members}
    lefts = [a for a, _ in extents.values()]
    rights = [b for _, b in extents.values()]
    u0, u1 = min(lefts), max(rights)
    rules = _vertical_rules(rp)
    first_op = {t: rp.glyphs[t].op for t in members}

    found = []
    for index, p in enumerate(rp.paths):
        if p.clip or not (p.fills or p.strokes):
            continue
        for s in p.subpaths:
            kind = _kind(p, s.bbox, base, size)
            if kind is None:
                continue
            x0, x1 = s.bbox[0], s.bbox[2]
            if x1 - x0 < 0.2 * size or x0 < u0 - EDGE or x1 > u1 + EDGE:
                continue
            # Both ends at letter edges: shading and borders reach past the text on one side.
            if not any(abs(x0 - a) <= EDGE for a in lefts) or not any(abs(x1 - b) <= EDGE for b in rights):
                continue
            y0, y1 = s.bbox[1], s.bbox[3]
            if any(abs(x - e) <= EDGE and a < y1 + 1 and b > y0 - 1 for x, a, b in rules for e in (x0, x1)):
                continue  # meets a table's vertical border
            covered = frozenset(t for t, (a, b) in extents.items() if x0 - 0.1 <= (a + b) / 2 <= x1 + 0.1)
            if not covered:
                continue
            if kind == "highlight" and p.end > min(first_op[t] for t in covered):
                continue  # a highlight lies behind its text, so it's drawn first
            found.append(Decoration(index, kind, x0, x1, covered, shared=len(p.subpaths) > 1))
        bullet = _bullet(p, base, size, u0, u1, line.rtl)
        if bullet:
            found.append(Decoration(index, "bullet", p.bbox[0], p.bbox[2], frozenset()))
    return found


def _bullet(p: DrawnPath, base: float, size: float, u0: float, u1: float, rtl: bool) -> bool:
    """A list bullet drawn as a shape (Chrome draws them as small filled circles): small
    and roundish, at the height of the middle of small letters, just before the text."""
    if not p.fills or p.strokes or len(p.subpaths) != 1:
        return False
    x0, y0, x1, y1 = p.bbox
    w, h = x1 - x0, y1 - y0
    if not (0.15 * size <= w <= 0.6 * size and 0.15 * size <= h <= 0.6 * size and abs(w - h) < 0.3 * size):
        return False
    if not base + 0.1 * size <= (y0 + y1) / 2 <= base + 0.6 * size:
        return False
    if rtl:
        return u1 + 0.2 * size <= x0 <= u1 + 2.5 * size
    return u0 - 2.5 * size <= x1 <= u0 - 0.2 * size


def _kind(p: DrawnPath, bbox, base: float, size: float) -> str | None:
    x0, y0, x1, y1 = bbox
    height = y1 - y0
    middle = (y0 + y1) / 2
    thickness = height
    if p.strokes and not p.fills:
        scale = (abs(p.ctm[1]) ** 2 + abs(p.ctm[3]) ** 2) ** 0.5
        thickness = height + p.line_width * scale
    if thickness <= 0.15 * size:
        if base - 0.4 * size <= middle <= base + 0.05 * size:
            return "underline"
        if base + 0.1 * size < middle <= base + 0.5 * size:
            return "strike"
        return None
    if p.fills and not p.strokes and 0.8 * size <= height <= 1.8 * size and y0 <= base - 0.1 * size and y1 >= base + 0.55 * size:
        return "highlight"
    return None
