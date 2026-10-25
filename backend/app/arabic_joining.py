"""How Arabic letters join their neighbours (Unicode ArabicShaping-18.0.0.txt, Joining_Type), to tell
which shape a letter takes: alone, at the start, in the middle or at the end of a word.

D joins on both sides, R only to the letter before it (alef, dal, ra, waw...), L only to
the one after, C causes joining (tatweel), T is transparent (vowel marks: the letters on
either side join through it), U doesn't join (hamza, digits, everything else). Code
points not listed are T when they're marks or format characters, else U.
"""

import unicodedata

_RANGES = {
    "C": "0640,07FA,0883-0885,200D",
    "D": "0620,0626,0628,062A-062E,0633-063F,0641-0647,0649-064A,066E-066F,0678-0687,069A-06BF,06C1-06C2,06CC,06CE,06D0-06D1,06FA-06FC,06FF,0712-0714,071A-071D,071F-0727,0729,072B,072D-072E,074E-0758,075C-076A,076D-0770,0772,0775-0777,077A-077F,07CA-07EA,0841-0845,0848,084A-0853,0855,0860,0862-0865,0868,0886,0889-088D,088F,08A0-08A9,08AF-08B0,08B3-08B8,08BA-08C8,10EC3-10EC4,10EC6-10EC7",
    "L": "10ED9-10EEE",
    "R": "0622-0625,0627,0629,062F-0632,0648,0671-0673,0675-0677,0688-0699,06C0,06C3-06CB,06CD,06CF,06D2-06D3,06D5,06EE-06EF,0710,0715-0719,071E,0728,072A,072C,072F,074D,0759-075B,076B-076C,0771,0773-0774,0778-0779,0840,0846-0847,0849,0854,0856-0858,0867,0869-086A,0870-0882,088E,08AA-08AC,08AE,08B1-08B2,08B9,10EC2",
    "T": "070F",
}


def _table() -> dict[int, str]:
    out = {}
    for kind, spec in _RANGES.items():
        for part in spec.split(","):
            a, _, b = part.partition("-")
            for c in range(int(a, 16), int(b or a, 16) + 1):
                out[c] = kind
    return out


_TYPES = _table()


def joining_type(ch: str) -> str:
    kind = _TYPES.get(ord(ch))
    if kind is not None:
        return kind
    return "T" if unicodedata.category(ch) in ("Mn", "Me", "Cf") else "U"


def forms(text: str) -> list[str]:
    """The shape each character of a word takes: "isol", "init", "medi" or "fina" ("" for
    transparent ones). Joining passes through transparent characters."""
    kinds = [joining_type(c) for c in text]
    out = [""] * len(text)
    for i, k in enumerate(kinds):
        if k == "T":
            continue
        prev = next((kinds[j] for j in range(i - 1, -1, -1) if kinds[j] != "T"), "U")
        nxt = next((kinds[j] for j in range(i + 1, len(text)) if kinds[j] != "T"), "U")
        before = k in ("R", "D", "C") and prev in ("D", "L", "C")
        after = k in ("D", "L", "C") and nxt in ("R", "D", "C")
        out[i] = "medi" if before and after else "fina" if before else "init" if after else "isol"
    return out
