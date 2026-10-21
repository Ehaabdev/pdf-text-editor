"""The Unicode bidirectional algorithm (UAX #9) for one line of plain text.

Gives each character its embedding level (even: left-to-right, odd: right-to-left)
and the left-to-right display order, the way browsers and Chrome's PDF output lay
text out. Explicit embedding and isolate controls are not supported: edited text is
stored without them. tests/test_bidi.py checks the result against python-bidi.
"""

import unicodedata

# Bracket pairs (Bidi_Paired_Bracket) for rule N0.
_PAIRS = {"(": ")", "[": "]", "{": "}", "༺": "༻", "༼": "༽", "᚛": "᚜",
          "⁅": "⁆", "⁽": "⁾", "₍": "₎", "⌈": "⌉", "⌊": "⌋",
          "〈": "〉", "❨": "❩", "❪": "❫", "❬": "❭", "❮": "❯",
          "❰": "❱", "❲": "❳", "❴": "❵", "⟅": "⟆", "⟦": "⟧",
          "⟨": "⟩", "⟪": "⟫", "⦃": "⦄", "⦅": "⦆", "〈": "〉",
          "《": "》", "「": "」", "『": "』", "【": "】", "（": "）",
          "［": "］", "｛": "｝"}
_CLOSERS = {v: k for k, v in _PAIRS.items()}
_CANON = {"〈": "〈", "〉": "〉"}  # canonically equivalent brackets

_NEUTRAL = {"B", "S", "WS", "ON"}


def _strong(t: str) -> str | None:
    """Strong direction for neutrals and brackets: numbers count as R."""
    if t == "L":
        return "L"
    if t in ("R", "AL", "EN", "AN"):
        return "R"
    return None


def levels(text: str, rtl: bool) -> list[int]:
    base = 1 if rtl else 0
    e = "R" if rtl else "L"
    n = len(text)
    types = [unicodedata.bidirectional(c) or "L" for c in text]
    # Explicit controls are not expected; treat any that slipped through as neutral.
    types = ["ON" if t in ("LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN") else t for t in types]
    sos = eos = e

    # W1: NSM takes the type of the previous character.
    for i, t in enumerate(types):
        if t == "NSM":
            types[i] = sos if i == 0 else types[i - 1]
    # W2: EN after AL becomes AN.
    last = sos
    for i, t in enumerate(types):
        if t in ("L", "R", "AL"):
            last = t
        elif t == "EN" and last == "AL":
            types[i] = "AN"
    # W3: AL becomes R.
    types = ["R" if t == "AL" else t for t in types]
    # W4: a single separator between two numbers of the same kind.
    for i in range(1, n - 1):
        if types[i] == "ES" and types[i - 1] == "EN" and types[i + 1] == "EN":
            types[i] = "EN"
        elif types[i] == "CS" and types[i - 1] == types[i + 1] and types[i - 1] in ("EN", "AN"):
            types[i] = types[i - 1]
    # W5: terminators next to European numbers become EN.
    i = 0
    while i < n:
        if types[i] == "ET":
            j = i
            while j < n and types[j] == "ET":
                j += 1
            if (i > 0 and types[i - 1] == "EN") or (j < n and types[j] == "EN"):
                for k in range(i, j):
                    types[k] = "EN"
            i = j
        else:
            i += 1
    # W6: remaining separators and terminators are neutral.
    types = ["ON" if t in ("ES", "ET", "CS") else t for t in types]
    # W7: EN after L (or at the start of an LTR line) becomes L.
    last = sos
    for i, t in enumerate(types):
        if t in ("L", "R"):
            last = t
        elif t == "EN" and last == "L":
            types[i] = "L"

    # N0: bracket pairs.
    original = [unicodedata.bidirectional(c) for c in text]
    pairs, stack = [], []
    for i, c in enumerate(text):
        if types[i] != "ON":
            continue
        c = _CANON.get(c, c)
        if c in _PAIRS:
            if len(stack) >= 63:
                break
            stack.append((c, i))
        elif c in _CLOSERS:
            for depth in range(len(stack) - 1, -1, -1):
                if stack[depth][0] == _CLOSERS[c]:
                    pairs.append((stack[depth][1], i))
                    del stack[depth:]
                    break
    for o, c in sorted(pairs):
        inside = {_strong(types[k]) for k in range(o + 1, c)} - {None}
        direction = None
        if e in inside:
            direction = e
        elif inside:
            opposite = "L" if e == "R" else "R"
            before = sos
            for k in range(o - 1, -1, -1):
                s = _strong(types[k])
                if s:
                    before = s
                    break
            direction = opposite if before == opposite else e
        if direction:
            types[o] = types[c] = direction
            for k in (o, c):  # marks after a bracket follow it
                j = k + 1
                while j < n and original[j] == "NSM":
                    types[j] = direction
                    j += 1

    # N1, N2: runs of neutrals take the direction around them, else the line's.
    i = 0
    while i < n:
        if types[i] in _NEUTRAL:
            j = i
            while j < n and types[j] in _NEUTRAL:
                j += 1
            left = sos if i == 0 else _strong(types[i - 1]) or e
            right = eos if j == n else _strong(types[j]) or e
            direction = left if left == right else e
            for k in range(i, j):
                types[k] = direction
            i = j
        else:
            i += 1

    # I1, I2: levels.
    out = []
    for t in types:
        if base % 2 == 0:
            out.append(base + (1 if t == "R" else 2 if t in ("AN", "EN") else 0))
        else:
            out.append(base + (1 if t in ("L", "EN", "AN") else 0))
    # L1: trailing whitespace returns to the line's level.
    k = n - 1
    while k >= 0 and original[k] in ("WS", "B", "S", "BN", "LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"):
        out[k] = base
        k -= 1
    for k in range(n):
        if original[k] in ("S", "B"):
            out[k] = base
            j = k - 1
            while j >= 0 and original[j] == "WS":
                out[j] = base
                j -= 1
    return out


def visual_order(lv: list[int]) -> list[int]:
    """L2: character indices in left-to-right display order."""
    order = list(range(len(lv)))
    if not lv:
        return order
    highest = max(lv)
    lowest_odd = min((l for l in lv if l % 2), default=highest + 1)
    for level in range(highest, lowest_odd - 1, -1):
        i = 0
        while i < len(order):
            if lv[order[i]] >= level:
                j = i
                while j < len(order) and lv[order[j]] >= level:
                    j += 1
                order[i:j] = order[i:j][::-1]
                i = j
            else:
                i += 1
    return order
