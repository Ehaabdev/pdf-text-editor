"""Numbered and bulleted lists: the marker a new item gets, and renumbering the items after
it, so a list never reads 1, 2, 2.

A line added below a list item's last line (or above its first line) can become an item
of the list itself: its number, letter or bullet in the list's own marker style, with
the items after it renumbered in the same change. Each renumbering is an ordinary checked
edit of that item's marker; if one can't be made exactly, nothing is changed.
"""

import io
import re
from dataclasses import dataclass

import pikepdf
import pymupdf

from .adding import _MARKER, _item_of, _lines_above, _lines_below, _old_words, add_line
from .editing import EditRejected, EditResult, comparable, replace_line
from .pdfcontent import Replay, replay
from .textlayout import Line, page_lines

_ROMAN = ["", "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii", "xiii", "xiv", "xv",
          "xvi", "xvii", "xviii", "xix", "xx"]
_DIGIT_SETS = ("0123456789", "٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹")


def _split(marker: str) -> tuple[str, str, str, bool] | None:
    """(before, value, after, reversed): "3." -> ("", "3", ".", False). An Arabic line's
    "1." can be stored as ".1"."""
    for text, rev in ((marker, False), (marker[::-1], True)):
        m = re.fullmatch(r"(\(?)([0-9٠-٩۰-۹]{1,3}|[a-zA-Z]|[ivxIVX]{1,5})([.)])", text)
        if m:
            return m.group(1), m.group(2), m.group(3), rev
    return None


def _number(value: str, roman: bool) -> tuple[int, str] | None:
    """The value as a number, and its kind: a digit set, "a"/"A" letters, "i"/"I" roman."""
    for digits in _DIGIT_SETS:
        if all(c in digits for c in value):
            return int("".join(str(digits.index(c)) for c in value)), digits
    low = value.lower()
    if roman and low in _ROMAN:
        return _ROMAN.index(low), "I" if value.isupper() else "i"
    if len(value) == 1 and value.isalpha():
        return ord(low) - ord("a") + 1, "A" if value.isupper() else "a"
    return None


def _write(n: int, kind: str) -> str | None:
    if kind in _DIGIT_SETS:
        return "".join(kind[int(c)] for c in str(n)) if n >= 0 else None
    if kind in ("a", "A"):
        return chr(ord(kind) + n - 1) if 1 <= n <= 26 else None
    if kind in ("i", "I"):
        return (_ROMAN[n].upper() if kind == "I" else _ROMAN[n]) if 1 <= n < len(_ROMAN) else None
    return None


def shifted(marker: str, step: int, roman: bool = False) -> str | None:
    """The marker `step` items on: "3." +1 -> "4.", "c)" +1 -> "d)", "iv." +1 -> "v.",
    "٣." -1 -> "٢.". A bullet or dash stays as it is. None when it can't be counted on."""
    if len(marker) == 1 and not marker.isalnum():
        return marker
    parts = _split(marker)
    if parts is None:
        return None
    before, value, after, rev = parts
    got = _number(value, roman or len(value) > 1)
    if got is None:
        return None
    n, kind = got
    new = _write(n + step, kind)
    if new is None:
        return None
    text = before + new + after
    return text[::-1] if rev else text


@dataclass
class Place:
    """Where a line sits in a list."""

    item_line: Line  # the first line of the line's item (with the marker)
    marker: str
    first: bool  # the line is its item's first line
    last: bool  # the line is its item's last line
    roman: bool  # the list counts in Roman numerals
    followers: list[tuple[Line, str]]  # the items after it in the same list, with their markers
    own: list[Line]  # the item's lines, the marker's first


def _start(line: Line, rp: Replay) -> float:
    words = _old_words(line, rp)
    return max(w.u1 for w in words) if line.rtl else min(w.u0 for w in words)


def place(line: Line, lines: list[Line], rp: Replay) -> Place | None:
    """The line's place in a list, or None when it isn't in one."""
    item_line, item = line, _item_of(line, rp, lines)
    if item is None:
        # A later line of an item: the lines above it, up to the one with the marker,
        # all start where the item's text starts.
        for above in _lines_above(line, lines):
            found = _item_of(above, rp, lines)
            if found is not None:
                item_line, item = above, found
                break
            if found is None and abs(_start(above, rp) - _start(line, rp)) > 1.5:
                return None
        if item is None or abs(_start(line, rp) - item.text_edge) > 1.5:
            return None
    marker = item.marker_text
    edge = item.marker_edges[1] if item_line.rtl else item.marker_edges[0]
    height = item_line.written[3] - item_line.written[1]
    # The lines under the item, straight down the list (not reading order: a table's
    # cells side by side would come in between).
    under = sorted(
        (l for l in lines if l.written[1] > item_line.written[1] + 0.5 and min(l.written[2], item_line.written[2]) > max(l.written[0], item_line.written[0])),
        key=lambda l: l.written[1],
    )

    def continues(prev: Line, nxt: Line, text_edge: float) -> bool:
        """A later line of the same item: close under it, starting where its text starts."""
        return 0 <= nxt.written[1] - prev.written[3] < 0.8 * height and _item_of(nxt, rp, lines) is None and abs(_start(nxt, rp) - text_edge) <= 1.5

    own, j = [item_line], 0
    while j < len(under) and continues(own[-1], under[j], item.text_edge):
        own.append(under[j])
        j += 1
    if line not in own:
        return None
    # The items after it: the same kind of marker, lined up, numbered on from this one.
    bullet = len(marker) == 1 and not marker.isalnum()
    roman = bool(re.fullmatch(r"\(?[ivxIVX]{2,5}[.)]|[.)][ivxIVX]{2,5}\)?", marker))
    expected = marker if bullet else shifted(marker, 1, roman)
    followers: list[tuple[Line, str]] = []
    prev, prev_edge = own[-1], item.text_edge
    skipped = 0.0  # height of the other language's text passed over since the last item
    while j < len(under) and expected is not None:
        other = under[j]
        theirs = _item_of(other, rp, lines)
        lined_up = theirs is not None and abs((theirs.marker_edges[1] if other.rtl else theirs.marker_edges[0]) - edge) <= 1.5
        if lined_up and theirs.marker_text == expected and other.written[1] - prev.written[3] <= 2.5 * height + skipped:
            followers.append((other, theirs.marker_text))
            roman = roman or bool(re.fullmatch(r"\(?[ivxIVX]{2,5}[.)]|[.)][ivxIVX]{2,5}\)?", theirs.marker_text))
            expected = marker if bullet else shifted(theirs.marker_text, 1, roman)
            prev, prev_edge, skipped = other, theirs.text_edge, 0.0
            j += 1
            continue
        if followers and not skipped and continues(prev, other, prev_edge):
            prev = other  # a later line of the item before
            j += 1
            continue
        if other.rtl != item_line.rtl:
            # The other language in between, as in a two-language contract where the
            # English and Arabic items take turns: the list goes on after it.
            skipped += (other.written[3] - other.written[1]) + 2.5 * height
            j += 1
            continue
        break
    return Place(item_line, marker, item_line is line, own[-1] is line, roman, followers, own)


def markers(pdf_bytes: bytes, page_no: int, lines: list[Line]) -> dict[str, dict]:
    """For each line in a list: the marker a new item gets below it (when it's its item's
    last line) and above it (when it's its item's first line)."""
    out = {}
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        try:
            rp = replay(pdf, page_no)
        except Exception:
            return {}
        for line in lines:
            try:
                p = place(line, lines, rp)
            except Exception:
                p = None
            if p is None:
                continue
            below = shifted(p.marker, 1, p.roman) if p.last else None
            above = p.marker if p.first else None
            if below or above:
                out[line.id] = {"below": below, "above": above}
    return out


def add_item(src: bytes, page_no: int, text: str, ref_id: str, ref_text: str, where: str = "below") -> EditResult:
    """Add `text` as a new item of the list the line is in, below (or above) it, and
    renumber the items after it. Refused when the line isn't in a list, or when an item's
    number can't be changed exactly."""
    doc = pymupdf.open("pdf", src)
    lines = page_lines(doc[page_no])
    ref = next((l for l in lines if l.id == ref_id), None)
    if ref is None or comparable(ref.text) != comparable(ref_text):
        raise EditRejected("This line changed since you selected it. Select it again.")
    with pikepdf.open(io.BytesIO(src)) as pdf:
        rp = replay(pdf, page_no)
        p = place(ref, lines, rp)
    if p is None or (where == "below" and not p.last) or (where == "above" and not p.first):
        raise EditRejected("This line isn't the end of a list item, so the new line can't be numbered as the next item.")
    marker = shifted(p.marker, 1, p.roman) if where == "below" else p.marker
    if marker is None:
        raise EditRejected("The list's numbering couldn't be continued.")
    numbered = len(p.marker) > 1 or p.marker.isalnum()
    # Renumbered: the items after it, and adding above, the line's own item too.
    renumber = ([(p.item_line, p.marker)] if where == "above" else []) + p.followers if numbered else []
    words = text.split()
    if len(words) > 1 and _MARKER.match(words[0]):
        words = words[1:]  # a number typed as well: the list's own is used
    result = add_line(src, page_no, f"{marker} {' '.join(words)}", ref_id, ref_text, where)
    pdf = result.pdf
    areas = list(result.areas)
    for old_line, old_marker in renumber:
        new_marker = shifted(old_marker, 1, p.roman)
        now = _find(pdf, page_no, old_line)
        if new_marker is None or now is None or old_marker not in now.text:
            raise EditRejected("The items after it couldn't be renumbered exactly, so nothing was changed. Add it as a plain line instead.")
        try:
            step = replace_line(pdf, page_no, now.id, now.text, now.text.replace(old_marker, new_marker, 1))
        except EditRejected:
            raise EditRejected("The items after it couldn't be renumbered exactly, so nothing was changed. Add it as a plain line instead.")
        pdf = step.pdf
        areas += step.areas
    return EditResult(pdf, result.warnings, result.text, areas, result.marks, result.lookalikes, result.moved)


def renumber_after_delete(src: bytes, result: EditResult, targets: list[tuple[int, str, str]]) -> EditResult:
    """After whole numbered items were deleted (`src` before, `result` after), count the
    items after them down, so the list reads on without a gap. Best effort: where that
    can't be done exactly, the deletion stands and the user is told."""
    doc = pymupdf.open("pdf", src)
    pdf, areas, warnings = result.pdf, list(result.areas), list(result.warnings)
    by_page: dict[int, set[str]] = {}
    for page_no, line_id, _text in targets:
        by_page.setdefault(page_no, set()).add(line_id)
    for page_no, ids in sorted(by_page.items()):
        lines = page_lines(doc[page_no])
        with pikepdf.open(io.BytesIO(src)) as p:
            rp = replay(p, page_no)
        # Each list with a whole item deleted: going down it, every item left counts down
        # by the deleted items above it (delete 1 and 3 of 5: 2, 4, 5 become 1, 2, 3).
        renumber: list[tuple[Line, str, str]] = []
        covered: set[str] = set()
        for line in lines:
            if line.id not in ids or line.id in covered:
                continue
            spot = place(line, lines, rp)
            if spot is None or not spot.first or not (len(spot.marker) > 1 or spot.marker.isalnum()):
                continue
            if not all(l.id in ids for l in spot.own):
                continue  # only part of the item: it stays an item
            items = [(spot.item_line, spot.marker, spot.own)]
            for l, m in spot.followers:
                theirs = place(l, lines, rp)
                items.append((l, m, theirs.own if theirs else [l]))
            gone = 0
            for item_line, marker, own in items:
                covered.add(item_line.id)
                if all(x.id in ids for x in own):
                    gone += 1
                elif gone:
                    new = shifted(marker, -gone, spot.roman)
                    if new is None:
                        renumber = []
                        break
                    renumber.append((item_line, marker, new))
        if not renumber:
            continue
        try:
            for old_line, old_marker, new_marker in renumber:
                now = _find(pdf, page_no, old_line)
                if new_marker is None or now is None or old_marker not in now.text:
                    raise EditRejected("not found")
                step = replace_line(pdf, page_no, now.id, now.text, now.text.replace(old_marker, new_marker, 1))
                pdf, areas = step.pdf, areas + step.areas
        except EditRejected:
            return EditResult(result.pdf, warnings + ["The numbers of the list items after it weren't updated."], result.text, result.areas, result.marks, result.lookalikes, result.moved)
    return EditResult(pdf, warnings, result.text, areas, result.marks, result.lookalikes, result.moved)


def _find(pdf: bytes, page_no: int, old: Line) -> Line | None:
    """The same line in a later version of the page: same text, same left and right
    edges (it may have moved down)."""
    for l in page_lines(pymupdf.open("pdf", pdf)[page_no]):
        if l.text == old.text and abs(l.bbox[0] - old.bbox[0]) < 0.5 and abs(l.bbox[2] - old.bbox[2]) < 0.5:
            return l
    return None
