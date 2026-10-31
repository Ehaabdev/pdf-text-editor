"""Drawing pieces of one page (text objects, shapes, pictures) again on another, and
taking pieces off a page: what flows onto the next page when a page is full.

A piece is drawn on the other page after that page's own drawing, which is first wrapped
in q ... Q so the piece starts from a clean state. Inside its own q ... Q it first sets
the state it was drawn in: every cm, color, line style, ExtGState and text state (font,
spacing, render mode) in effect where it was, found by walking its page from the start
(a q saves them, a Q restores them; text state set inside an earlier text object lasts
too), and the clipping in effect there, its points taken to page space (Chrome cuts
gaps in an underline where letters reach down through it).

Its fonts, pictures, color spaces, patterns, shadings and graphics states go with it into
the other page's resources: under the same name when that's free or already the same
object, else under a new name, its instructions rewritten. Word's tags (marked content)
are left out: copied, they'd say the same text is in two places.

Taking pieces off a page: their instructions go, what they set that lasts is set again
after them, and their tags stay (they draw nothing).
"""

import pikepdf

from . import moving
from .changes import EditRejected, write_page
from .moving import Piece
from .pdfcontent import Replay

# What sets state that the drawing of a piece can depend on.
_FILL = {"g", "rg", "k", "cs", "sc", "scn"}
_STROKE = {"G", "RG", "K", "CS", "SC", "SCN"}
_SINGLE = {"w", "J", "j", "M", "d", "ri", "i", "Tf", "Tc", "Tw", "Tz", "TL", "Ts", "Tr"}
_MARKED = {"BDC", "BMC", "EMC", "MP", "DP"}
# Instruction -> (resource kind, which operand names it).
_NAMED = {"Tf": ("/Font", 0), "Do": ("/XObject", 0), "gs": ("/ExtGState", 0), "cs": ("/ColorSpace", 0), "CS": ("/ColorSpace", 0), "sh": ("/Shading", 0)}
_DEVICE = {"/DeviceGray", "/DeviceRGB", "/DeviceCMYK", "/Pattern"}


def _op(operands, name: str) -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction(list(operands), pikepdf.Operator(name))


def _is(ins, *names) -> bool:
    return not isinstance(ins, pikepdf.ContentStreamInlineImage) and str(ins.operator) in names


class _State:
    """The state set so far at one level of q ... Q, as instructions that set it again:
    each cm and ExtGState in order, and the last of each other setting."""

    def __init__(self, other=None):
        self.cm = list(other.cm) if other else []
        self.gs = list(other.gs) if other else []
        self.fill = list(other.fill) if other else []
        self.stroke = list(other.stroke) if other else []
        self.single = dict(other.single) if other else {}

    def add(self, ins) -> None:
        op = str(ins.operator)
        if op == "cm":
            self.cm.append(ins)
        elif op == "gs":
            self.gs.append(ins)
        elif op in _FILL:
            # A new color replaces the old; a color in a color space keeps the space.
            self.fill = ([i for i in self.fill if str(i.operator) == "cs"] if op in ("sc", "scn") else []) + [ins]
        elif op in _STROKE:
            self.stroke = ([i for i in self.stroke if str(i.operator) == "CS"] if op in ("SC", "SCN") else []) + [ins]
        elif op in _SINGLE:
            self.single[op] = ins

    def instructions(self) -> list:
        return self.cm + self.gs + self.fill + self.stroke + list(self.single.values())


def states_at(rp: Replay, starts: set[int]) -> dict[int, list]:
    """For each instruction in `starts`: the instructions that set again the state in
    effect just before it."""
    out = {}
    stack = [_State()]
    for k, ins in enumerate(rp.instructions):
        if k in starts:
            out[k] = stack[-1].instructions()
        if isinstance(ins, pikepdf.ContentStreamInlineImage):
            continue
        op = str(ins.operator)
        if op == "q":
            stack.append(_State(stack[-1]))
        elif op == "Q":
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1].add(ins)
    return out


def _clips(rp: Replay, p: Piece, scopes: dict) -> list:
    """The clipping paths in effect where a piece is drawn (indexes into rp.paths), in order."""
    return [i for i, path in enumerate(rp.paths) if path.clip and path.end < p.start < scopes.get(i, len(rp.instructions))]


def _clip_on_page(rp: Replay, i: int) -> list:
    """A clipping path's instructions drawn again in page space: its points through the
    transformation it was built in (a rectangle as four corners), then W or W* and n."""
    path = rp.paths[i]
    m = path.ctm
    pt = lambda x, y: [x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5]]
    out = []
    for k in range(path.start, path.end + 1):
        ins = rp.instructions[k]
        if isinstance(ins, pikepdf.ContentStreamInlineImage):
            continue
        op = str(ins.operator)
        v = [float(x) for x in ins.operands]
        if op == "re":
            x, y, w, h = v
            corners = [pt(x, y), pt(x + w, y), pt(x + w, y + h), pt(x, y + h)]
            out += [_op(corners[0], "m"), *[_op(c, "l") for c in corners[1:]], _op([], "h")]
        elif op in ("m", "l"):
            out.append(_op(pt(*v), op))
        elif op == "c":
            out.append(_op(pt(v[0], v[1]) + pt(v[2], v[3]) + pt(v[4], v[5]), op))
        elif op in ("v", "y"):
            out.append(_op(pt(v[0], v[1]) + pt(v[2], v[3]), op))
        elif op in ("h", "W", "W*"):
            out.append(_op([], op))
    return out + [_op([], "n")]


def _clips_cut(rp: Replay, p: Piece, scopes: dict) -> bool:
    """Kept for callers that ask: clipping is now carried along, so nothing is cut."""
    return False


def _resources(pdf: pikepdf.Pdf, page_no: int) -> pikepdf.Dictionary:
    """A page's own resources (an inherited set is copied onto the page first)."""
    page = pdf.pages[page_no]
    res = page.obj.get("/Resources")
    if res is None:
        page.obj.Resources = pikepdf.Dictionary(page.Resources)
        res = page.obj.Resources
    return res


def _same(a, b) -> bool:
    if a.objgen != (0, 0) or b.objgen != (0, 0):
        return a.objgen == b.objgen
    return a.unparse() == b.unparse()


class _Names:
    """Resource names of one page, brought into another's: name -> name there."""

    def __init__(self, pdf: pikepdf.Pdf, src_no: int, dst_no: int):
        self.src = pdf.pages[src_no].obj.get("/Resources") or pdf.pages[src_no].Resources
        self.dst = _resources(pdf, dst_no)
        self.done: dict = {}

    def bring(self, kind: str, name) -> pikepdf.Name:
        key = (kind, str(name))
        if key in self.done:
            return self.done[key]
        group = self.src.get(kind)
        if group is None or name not in group:
            raise EditRejected("Something on this page uses a font or picture the page doesn't list, so it can't move to the next page.")
        obj = group[name]
        there = self.dst.get(kind)
        if there is None:
            self.dst[kind] = pikepdf.Dictionary()
            there = self.dst[kind]
        new = pikepdf.Name(str(name))
        n = 1
        while new in there and not _same(there[new], obj):
            new = pikepdf.Name(f"{name}_{n}")
            n += 1
        if new not in there:
            there[new] = obj
        self.done[key] = new
        return new

    def rewrite(self, ins):
        if isinstance(ins, pikepdf.ContentStreamInlineImage):
            cs = ins.iimage.obj.get("/ColorSpace") if hasattr(ins, "iimage") else None
            if cs is not None and isinstance(cs, pikepdf.Name) and str(cs) not in _DEVICE and str(cs) not in ("/G", "/RGB", "/CMYK", "/I"):
                raise EditRejected("A picture on this page can't move to the next page yet.")
            return ins
        op = str(ins.operator)
        operands = list(ins.operands)
        if op in _NAMED:
            kind, at = _NAMED[op]
            name = operands[at]
            if not (kind == "/ColorSpace" and str(name) in _DEVICE):
                operands[at] = self.bring(kind, name)
                return _op(operands, op)
        if op in ("scn", "SCN") and operands and isinstance(operands[-1], pikepdf.Name):
            operands[-1] = self.bring("/Pattern", operands[-1])
            return _op(operands, op)
        return ins


def copy_pieces(pdf: pikepdf.Pdf, rp: Replay, src_no: int, dst_no: int, pieces: list[Piece], dy: float, dst_rp: Replay) -> None:
    """Draw pieces of page `src_no` (as `rp` read it) again on page `dst_no`, `dy` points
    lower on the page (PDF; higher when negative), after that page's own drawing."""
    if dst_rp.stray_restore or dst_rp.open_text:
        raise EditRejected("The next page is built in a way that can't take more text yet.")
    scopes = moving._clip_scopes(rp)
    clips = {}
    for p in pieces:
        if p.kind == "clip":
            raise EditRejected("Clipping can't move to the next page.")
        clips[p.start] = _clips(rp, p, scopes)
    states = states_at(rp, {p.start for p in pieces})
    names = _Names(pdf, src_no, dst_no)
    out = [_op([], "q"), *dst_rp.instructions, *([_op([], "Q")] * dst_rp.open_states), _op([], "Q")]
    for p in sorted(pieces, key=lambda p: p.start):
        drawn = [ins for ins in rp.instructions[p.start : p.end + 1] if not _is(ins, *_MARKED)]
        out += [_op([], "q"), _op([1, 0, 0, 1, 0, -dy], "cm")]
        for i in clips[p.start]:
            out += _clip_on_page(rp, i)
        out += [names.rewrite(ins) for ins in states[p.start] + drawn]
        out.append(_op([], "Q"))
    write_page(pdf, dst_no, out)


def remove_pieces(pdf: pikepdf.Pdf, rp: Replay, page_no: int, pieces: list[Piece]) -> None:
    """Take pieces off a page: their instructions go (but their tags), and what they set
    that lasts is set again after them."""
    drop: set[int] = set()
    after: dict[int, list] = {}
    for p in pieces:
        lasting = moving._lasting(rp, p)
        if lasting is None:
            raise EditRejected("Some of this text is drawn in a way that can't move to the next page.")
        drop.update(range(p.start, p.end + 1))
        after[p.end] = lasting
    out = []
    for k, ins in enumerate(rp.instructions):
        if k in drop:
            if _is(ins, *_MARKED):
                out.append(ins)
        else:
            out.append(ins)
        out += after.get(k, [])
    write_page(pdf, page_no, out)
