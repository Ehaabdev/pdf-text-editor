"""One "Add text": a click on the page decides what adding there means, before anything
is written: a new list item, a new line (the text below moving down), text joining a line
on its row, or text right where clicked."""

from pathlib import Path

import pymupdf
import pytest

from app.editing import comparable
from app.spots import plan
from app.textlayout import page_lines

FIXTURES = Path(__file__).parent / "fixtures"
SRC = (FIXTURES / "additions" / "additions-word.pdf").read_bytes()
LINES = page_lines(pymupdf.open("pdf", SRC)[0])


def line(needle: str):
    return next(l for l in LINES if comparable(l.text).startswith(comparable(needle)))


def middle(l):
    return (l.bbox[1] + l.bbox[3]) / 2


@pytest.mark.parametrize(
    "where, expected",
    [
        (lambda: (line("3. Book").bbox[0] + 30, line("3. Book").bbox[3] - 1), ("item", "below", "4.", "3. Book")),
        (lambda: (line("3. Book").bbox[0] + 30, line("3. Book").bbox[3] + 4), ("item", "below", "4.", "3. Book")),
        (lambda: (line("2. Share").bbox[0] + 30, line("2. Share").bbox[1] + 1), ("item", "above", "2.", "2. Share")),
        (lambda: (line("3. Book").bbox[2] + 100, middle(line("3. Book"))), ("join", "below", None, "3. Book")),
        (
            lambda: (line("The team").bbox[0] + 50, (line("The team").bbox[3] + line("week ahead").bbox[1]) / 2),
            ("line", "below", None, "The team"),
        ),
        (lambda: (line("Status").bbox[0] + 20, middle(line("Planning"))), ("free", "below", None, None)),
        (lambda: (300, 780), ("free", "below", None, None)),
    ],
    ids=["on the list's last item", "just under it", "on an item's top half", "beside a line",
         "between a paragraph's lines", "an empty table cell", "empty space"],
)
def test_what_a_click_means(where, expected):
    x, y = where()
    p = plan(SRC, 0, x, y)
    kind, side, marker, needle = expected
    assert (p.kind, p.where, p.marker) == (kind, side, marker)
    assert (p.line is None) if needle is None else comparable(p.line.text).startswith(comparable(needle))
    assert p.label


def test_a_new_lines_band_is_where_it_will_go():
    """Under the list's last item: one line spacing lower, as wide as its paragraph."""
    ref, above = line("3. Book"), line("2. Share")
    p = plan(SRC, 0, ref.bbox[0] + 30, ref.bbox[3] + 4)
    step = ref.bbox[1] - above.bbox[1]
    assert abs(p.box[1] - (ref.bbox[1] + step)) < 0.5
    assert abs(p.box[0] - ref.bbox[0]) < 0.5
