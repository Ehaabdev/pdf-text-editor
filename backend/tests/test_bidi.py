"""app.bidi against python-bidi (a full UAX #9 implementation) as the oracle."""

import json
import random
from pathlib import Path

import pytest
from bidi import get_display

from app.bidi import levels, visual_order

BRACKETS = str.maketrans({c: "|" for c in "()[]{}<>«»"})

PIECES = ["مرحبا", "الأفق", "رقم", "Q-2026", "WhatsApp", "Team", "+966", "55", "123", "4567", "21/10/2026",
          "٢٠/١١/٢٠٢٦", "15%", "-10%", "4,542.50", "(", ")", "[", "]", ":", ".", "،", "/", "-", "@", "sales@example.com",
          "https://support.example.com/help", "قَالَ", "Mr.", "خالد", "3.2", "Floor", "12"]


def shown(text: str, rtl: bool) -> str:
    lv = levels(text, rtl)
    return "".join(text[i] for i in visual_order(lv))


def oracle(text: str, rtl: bool) -> str:
    return get_display(text, base_dir="R" if rtl else "L")


def comparable(s: str) -> str:
    # python-bidi mirrors brackets in its output; positions are what matter here.
    return s.translate(BRACKETS)


def random_lines(count: int):
    rng = random.Random(7)
    for _ in range(count):
        words = [rng.choice(PIECES) for _ in range(rng.randint(1, 9))]
        seps = [rng.choice([" ", " ", " ", ""]) for _ in words]
        yield "".join(w + s for w, s in zip(words, seps)).strip(), rng.random() < 0.6


@pytest.mark.parametrize("text, rtl", list(random_lines(3000)))
def test_matches_python_bidi(text, rtl):
    assert comparable(shown(text, rtl)) == comparable(oracle(text, rtl))


def test_matches_python_bidi_on_the_test_documents():
    root = Path(__file__).parent / "fixtures"
    for source in ("word", "chrome"):
        for p in json.loads((root / source / "expected.json").read_text(encoding="utf-8")):
            for rtl in (True, False):
                assert comparable(shown(p["text"], rtl)) == comparable(oracle(p["text"], rtl)), p["text"]
