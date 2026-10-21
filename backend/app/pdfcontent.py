"""Read and change a page's drawing instructions (its content stream).

`replay` runs the page's instructions the way a viewer does and records, for every
glyph drawn, which instruction drew it, where it lands and with which text state,
color and font. It must agree with MuPDF on every glyph of the page (`matches_trace`)
before anything is changed; otherwise the page uses something this module does not
model (text in form XObjects, Type3 or vertical fonts) and editing is refused.

`remove_glyphs` takes glyphs out of their instructions and puts an equal spacing
number in their place, so everything else stays exactly where it was. `draw_block`
makes a self-contained block of instructions that draws glyphs at given positions.
All other instructions are left as they are: their bytes are written back unchanged.
"""

import re
from dataclasses import dataclass, field

import pikepdf
import pymupdf

Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


class Unsupported(Exception):
    """The page uses something editing doesn't handle. The message is for the user."""


def mul(a, b) -> Matrix:
    return (
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4],
        a[4] * b[1] + a[5] * b[3] + b[5],
    )


def inverse(m) -> Matrix:
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        raise Unsupported("This text uses a drawing transformation that can't be edited.")
    return (d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det)


_HEX = re.compile(rb"<([0-9A-Fa-f]*)>")


def _hex(value: bytes) -> bytes:
    """A PDF hex string's bytes; an odd last digit counts as followed by 0 (PDF 7.3.4.3)."""
    digits = value.decode("ascii")
    return bytes.fromhex(digits + "0" if len(digits) % 2 else digits)


def _parse_to_unicode(stream) -> dict[int, str]:
    """A font's ToUnicode table: code -> the text viewers copy for that glyph."""
    if stream is None:
        return {}
    try:
        data = stream.read_bytes()
    except Exception:
        return {}

    def text(raw: bytes) -> str:
        return raw.decode("utf-16-be", errors="replace")

    out: dict[int, str] = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        values = _HEX.findall(block)
        for src, dst in zip(values[0::2], values[1::2]):
            try:
                out[int(src, 16)] = text(_hex(dst))
            except ValueError:
                continue
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for line in re.findall(rb"<[0-9A-Fa-f]+>\s*<[0-9A-Fa-f]+>\s*(?:<[0-9A-Fa-f]*>|\[[^\]]*\])", block):
            try:
                parts = _HEX.findall(line)
                lo, hi = int(parts[0], 16), int(parts[1], 16)
                if b"[" in line:
                    for k, dst in enumerate(parts[2:]):
                        out[lo + k] = text(_hex(dst))
                else:
                    first = _hex(parts[2])
                    base = int.from_bytes(first, "big")
                    for k in range(min(hi - lo + 1, 65536)):
                        out[lo + k] = text((base + k).to_bytes(len(first), "big"))
            except (ValueError, OverflowError, IndexError):
                continue
    return out


class FontInfo:
    """What the content stream needs to know about a font resource."""

    def __init__(self, name: str, font):
        self.name = name
        self.obj = font
        subtype = str(font.get("/Subtype", ""))
        if subtype == "/Type3":
            raise Unsupported("This text uses a Type 3 font, which can't be edited yet.")
        self.two_byte = subtype == "/Type0"
        self.widths: dict[int, float] = {}
        if self.two_byte:
            encoding = str(font.get("/Encoding", ""))
            if encoding != "/Identity-H":
                raise Unsupported("This text uses a font encoding that can't be edited yet.")
            d = font.DescendantFonts[0]
            self.default = float(d.get("/DW", 1000))
            w = d.get("/W", [])
            i = 0
            while i < len(w):
                c = int(w[i])
                if isinstance(w[i + 1], pikepdf.Array):
                    for k, v in enumerate(w[i + 1]):
                        self.widths[c + k] = float(v)
                    i += 2
                else:
                    for cid in range(c, int(w[i + 1]) + 1):
                        self.widths[cid] = float(w[i + 2])
                    i += 3
        else:
            first = int(font.get("/FirstChar", 0))
            for k, v in enumerate(font.get("/Widths", [])):
                self.widths[first + k] = float(v)
            fd = font.get("/FontDescriptor")
            self.default = float(fd.get("/MissingWidth", 0)) if fd is not None else 0.0
        self.to_unicode = _parse_to_unicode(font.get("/ToUnicode"))

    def codes(self, s: bytes) -> list[int]:
        if self.two_byte:
            return [(s[i] << 8) | s[i + 1] for i in range(0, len(s) - 1, 2)]
        return list(s)

    def encode(self, code: int) -> bytes:
        return bytes([code >> 8, code & 0xFF]) if self.two_byte else bytes([code])

    def width(self, code: int) -> float:
        return self.widths.get(code, self.default)


@dataclass
class Paint:
    """The state a glyph was drawn with, enough to draw it again identically."""

    font: str  # resource name, e.g. "/F3"
    size: float
    Tz: float  # horizontal scaling, 1.0 = 100%
    Tr: int  # render mode
    trm2: tuple[float, float, float, float]  # text space -> user space, size excluded
    fill: tuple = ()  # instructions that set the fill color, as (operands, operator)
    stroke: tuple = ()
    extgstates: tuple = ()  # ExtGState names applied, in order


@dataclass
class DrawnGlyph:
    op: int  # index of the instruction that drew it
    item: int  # index inside a TJ array (0 for Tj)
    pos: int  # index of the code inside that string
    code: int
    origin: tuple[float, float]  # PDF user space
    advance: float  # displacement in text space units of 1/1000 em, spacing included
    paint: Paint
    ctm: Matrix


@dataclass
class Replay:
    instructions: list
    glyphs: list[DrawnGlyph]
    fonts: dict[str, FontInfo]
    # For each instruction index, the CTM in effect (only recorded for text objects' ET).
    et_after: dict[int, int] = field(default_factory=dict)  # text-showing op -> index of its ET


def _number(x) -> float:
    return float(x)


def replay(pdf: pikepdf.Pdf, page_index: int) -> Replay:
    page = pdf.pages[page_index]
    resources = page.obj.get("/Resources") or page.Resources
    fonts: dict[str, FontInfo] = {}
    for name, font in (resources.get("/Font") or {}).items():
        try:
            fonts[str(name)] = FontInfo(str(name), font)
        except Unsupported:
            fonts[str(name)] = None  # only a problem if text is drawn with it

    xobjects = resources.get("/XObject") or {}
    instructions = list(pikepdf.parse_content_stream(page))

    ctm: Matrix = IDENTITY
    stack = []
    st = dict(Tc=0.0, Tw=0.0, Tz=1.0, TL=0.0, font=None, size=0.0, Ts=0.0, Tr=0, fill=(), stroke=(), gs=())
    tm = tlm = IDENTITY
    glyphs: list[DrawnGlyph] = []
    open_text_ops: list[int] = []
    et_after: dict[int, int] = {}

    for k, ins in enumerate(instructions):
        if isinstance(ins, pikepdf.ContentStreamInlineImage):
            continue
        operands, op = ins.operands, str(ins.operator)
        if op == "q":
            stack.append((ctm, dict(st)))
        elif op == "Q":
            if stack:
                ctm, st = stack.pop()
        elif op == "cm":
            ctm = mul(tuple(_number(x) for x in operands), ctm)
        elif op == "gs":
            st["gs"] = st["gs"] + (str(operands[0]),)
        elif op in ("g", "rg", "k"):
            st["fill"] = ((tuple(operands), op),)
        elif op == "cs":
            st["fill"] = ((tuple(operands), op),)
        elif op in ("sc", "scn"):
            st["fill"] = tuple(x for x in st["fill"] if x[1] == "cs") + ((tuple(operands), op),)
        elif op in ("G", "RG", "K"):
            st["stroke"] = ((tuple(operands), op),)
        elif op == "CS":
            st["stroke"] = ((tuple(operands), op),)
        elif op in ("SC", "SCN"):
            st["stroke"] = tuple(x for x in st["stroke"] if x[1] == "CS") + ((tuple(operands), op),)
        elif op == "BT":
            tm = tlm = IDENTITY
            open_text_ops = []
        elif op == "ET":
            for t in open_text_ops:
                et_after[t] = k
            open_text_ops = []
        elif op == "Tf":
            st["font"], st["size"] = str(operands[0]), _number(operands[1])
        elif op in ("Tc", "Tw", "Ts", "TL"):
            st[op] = _number(operands[0])
        elif op == "Tz":
            st["Tz"] = _number(operands[0]) / 100
        elif op == "Tr":
            st["Tr"] = int(operands[0])
        elif op == "Tm":
            tm = tlm = tuple(_number(x) for x in operands)
        elif op in ("Td", "TD"):
            tx, ty = _number(operands[0]), _number(operands[1])
            if op == "TD":
                st["TL"] = -ty
            tlm = mul((1, 0, 0, 1, tx, ty), tlm)
            tm = tlm
        elif op == "T*":
            tlm = mul((1, 0, 0, 1, 0, -st["TL"]), tlm)
            tm = tlm
        elif op == "Do":
            xo = xobjects.get(operands[0]) if operands else None
            if xo is not None and str(xo.get("/Subtype", "")) == "/Form" and _has_text(xo):
                raise Unsupported("Some text on this page is inside an embedded object, which can't be edited yet.")

        if op in ("Tj", "TJ", "'", '"'):
            if op in ("'", '"'):
                if op == '"':
                    st["Tw"], st["Tc"] = _number(operands[0]), _number(operands[1])
                tlm = mul((1, 0, 0, 1, 0, -st["TL"]), tlm)
                tm = tlm
            font = fonts.get(st["font"])
            if font is None:
                raise Unsupported("This text uses a font that can't be edited yet.")
            open_text_ops.append(k)
            fs, th = st["size"], st["Tz"]
            items = list(operands[0]) if op == "TJ" else [operands[-1]]
            m2 = mul(tm, ctm)
            paint = Paint(
                font=st["font"], size=fs, Tz=th, Tr=st["Tr"],
                trm2=(m2[0], m2[1], m2[2], m2[3]),
                fill=st["fill"], stroke=st["stroke"], extgstates=st["gs"],
            )
            for item_index, item in enumerate(items):
                if not isinstance(item, (pikepdf.String, bytes)):
                    tm = mul((1, 0, 0, 1, -_number(item) / 1000 * fs * th, 0), tm)
                    continue
                for pos, code in enumerate(font.codes(bytes(item))):
                    trm = mul(mul((fs * th, 0, 0, fs, 0, st["Ts"]), tm), ctm)
                    tw = st["Tw"] if (not font.two_byte and code == 32) else 0.0
                    adv = font.width(code) + (1000 * (st["Tc"] + tw) / fs if fs else 0.0)
                    glyphs.append(DrawnGlyph(k, item_index, pos, code, (trm[4], trm[5]), adv, paint, ctm))
                    tm = mul((1, 0, 0, 1, adv / 1000 * fs * th, 0), tm)
    return Replay(instructions, glyphs, fonts, et_after)


def _has_text(xobject) -> bool:
    try:
        for ins in pikepdf.parse_content_stream(xobject):
            if not isinstance(ins, pikepdf.ContentStreamInlineImage) and str(ins.operator) in ("Tj", "TJ", "'", '"'):
                return True
    except Exception:
        return True
    return False


def trace_origins(page: pymupdf.Page) -> list[tuple[float, float]]:
    """MuPDF's glyph origins in drawing order, in PDF user space."""
    to_pdf = ~page.transformation_matrix
    return [tuple(pymupdf.Point(c[2]) * to_pdf) for s in page.get_texttrace() for c in s["chars"] if c[1] != -1]


def matches_trace(rp: Replay, page: pymupdf.Page, tolerance: float = 0.25) -> bool:
    """Do the replayed glyphs agree with MuPDF on count and every position? Viewers
    differ among themselves by up to about 0.25pt on long lines (font file widths vs
    the PDF's); a modelling error puts glyphs a whole letter or more apart."""
    theirs = trace_origins(page)
    if len(theirs) != len(rp.glyphs):
        return False
    return all(abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance for a, b in zip((g.origin for g in rp.glyphs), theirs))


# Changing instructions ------------------------------------------------------------------


def remove_glyphs(rp: Replay, remove: set[int]) -> list:
    """The page's instructions with the given glyphs (indices into rp.glyphs) taken out.
    Each removed glyph becomes a spacing number of exactly its advance, so every other
    glyph stays where it was. Instructions without removed glyphs are unchanged."""
    by_op: dict[int, dict[tuple[int, int], bool]] = {}
    for i in remove:
        g = rp.glyphs[i]
        by_op.setdefault(g.op, {})[(g.item, g.pos)] = True
    advances = {(g.op, g.item, g.pos): g.advance for g in rp.glyphs if g.op in by_op}

    font_of = {g.op: rp.fonts[g.paint.font] for g in rp.glyphs if g.op in by_op}
    out = []
    for k, ins in enumerate(rp.instructions):
        if k not in by_op:
            out.append(ins)
            continue
        operands, op = ins.operands, str(ins.operator)
        font = font_of[k]
        items = list(operands[0]) if op == "TJ" else [operands[-1]]
        new_items: list = []

        def push_number(n: float):
            if new_items and not isinstance(new_items[-1], pikepdf.String):
                new_items[-1] = new_items[-1] + n
            else:
                new_items.append(n)

        for item_index, item in enumerate(items):
            if not isinstance(item, (pikepdf.String, bytes)):
                push_number(_number(item))
                continue
            kept = b""
            for pos, code in enumerate(font.codes(bytes(item))):
                if by_op[k].get((item_index, pos)):
                    if kept:
                        new_items.append(pikepdf.String(kept))
                        kept = b""
                    push_number(-advances[(k, item_index, pos)])
                else:
                    kept += font.encode(code)
            if kept:
                new_items.append(pikepdf.String(kept))
        array = pikepdf.Array([x if isinstance(x, pikepdf.String) else round(x, 4) for x in new_items])
        if op == "'":
            out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("T*")))
        elif op == '"':
            out.append(pikepdf.ContentStreamInstruction([operands[0]], pikepdf.Operator("Tw")))
            out.append(pikepdf.ContentStreamInstruction([operands[1]], pikepdf.Operator("Tc")))
            out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("T*")))
        out.append(pikepdf.ContentStreamInstruction([array], pikepdf.Operator("TJ")))
    return out


@dataclass
class PlacedGlyph:
    code: int
    origin: tuple[float, float]  # PDF user space


@dataclass
class PlacedRun:
    """Glyphs to draw with one font and paint, each at its own position."""

    font: str  # resource name
    font_info: FontInfo
    paint: Paint
    glyphs: list[PlacedGlyph]


def _ins(operands, op):
    return pikepdf.ContentStreamInstruction(operands, pikepdf.Operator(op))


def draw_block(runs: list[PlacedRun], ctm: Matrix, actual_text: str | None) -> list:
    """Instructions that draw the runs and leave the graphics state as they found it.
    `ctm` is the transformation in effect where the block is inserted; it is undone
    so positions can be given in the page's own coordinates."""
    out = [_ins([], "q"), _ins(list(inverse(ctm)), "cm")]
    if actual_text is not None:
        out.append(_ins([pikepdf.Name("/Span"), pikepdf.Dictionary(ActualText=pikepdf.String(actual_text))], "BDC"))
    # One text object for the whole block, glyphs left to right, so text extraction in
    # other programs reads the line as one line. Color and font change inside it as needed.
    out.append(_ins([], "BT"))
    current = None
    for run in sorted(runs, key=lambda r: r.glyphs[0].origin[0] if r.glyphs else 0):
        p = run.paint
        state = (run.font, p.size, p.Tz, p.Tr, p.fill, p.stroke, p.extgstates)
        if state != current:
            for name in p.extgstates:
                if current is None or name not in current[6]:
                    out.append(_ins([pikepdf.Name(name)], "gs"))
            for operands, op in p.fill + p.stroke:
                out.append(_ins(list(operands), op))
            out.append(_ins([pikepdf.Name(run.font), p.size], "Tf"))
            out.append(_ins([p.Tz * 100], "Tz"))
            out.append(_ins([p.Tr], "Tr"))
            current = state
        a, b, c, d = p.trm2
        for g in run.glyphs:
            out.append(_ins([a, b, c, d, round(g.origin[0], 4), round(g.origin[1], 4)], "Tm"))
            out.append(_ins([pikepdf.String(run.font_info.encode(g.code))], "Tj"))
    out.append(_ins([], "ET"))
    if actual_text is not None:
        out.append(_ins([], "EMC"))
    out.append(_ins([], "Q"))
    return out


def write_contents(pdf: pikepdf.Pdf, page_index: int, instructions: list) -> None:
    page = pdf.pages[page_index]
    page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(instructions))
