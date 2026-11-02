"""Whole pages: deleting, adding and moving one (pages.py), and through the website."""

from pathlib import Path

import pymupdf
import pytest

from app.editing import EditRejected
from app.pages import add_page, delete_page, move_page
from app.textlayout import page_lines
from conftest import upload

FIXTURES = Path(__file__).parent / "fixtures"
TWO = (FIXTURES / "flow" / "flow-chrome.pdf").read_bytes()  # header and "Page X of 2" footer


def linked() -> bytes:
    """Three pages: links on page 1 to pages 2 and 3, bookmarks to each (page 2's with a
    sub-bookmark to page 3), and a form field on pages 2 and 3."""
    doc = pymupdf.open()
    for n in range(3):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"{('First', 'Second', 'Third')[n]} page", fontsize=14)
    for target, y in ((1, 140), (2, 170)):
        doc[0].insert_link({"kind": pymupdf.LINK_GOTO, "page": target, "from": pymupdf.Rect(72, y - 12, 200, y + 4), "to": pymupdf.Point(72, 72)})
    doc.set_toc([[1, "One", 1], [1, "Two", 2], [2, "Two, part three", 3], [1, "Three", 3]])
    for n, name in ((1, "on_two"), (2, "on_three")):
        w = pymupdf.Widget()
        w.field_name, w.field_type, w.rect = name, pymupdf.PDF_WIDGET_TYPE_TEXT, pymupdf.Rect(72, 300, 250, 320)
        doc[n].add_widget(w)
    return doc.tobytes()


def texts(src: bytes) -> list[str]:
    return [" ".join(l.text for l in page_lines(p)) for p in pymupdf.open("pdf", src)]


def test_a_deleted_page_takes_what_points_at_it_with_it():
    src = linked()
    result = delete_page(src, 1)
    doc = pymupdf.open("pdf", result.pdf)
    assert texts(result.pdf) == ["First page", "Third page"]
    # The link to page 2 is gone; the one to page 3 still goes there (now page 2).
    assert [l["page"] for l in doc[0].get_links() if l["kind"] == pymupdf.LINK_GOTO] == [1]
    # Its bookmark is gone; the sub-bookmark to page 3 moved up a level.
    assert [(lvl, title, page) for lvl, title, page in doc.get_toc()] == [(1, "One", 1), (1, "Two, part three", 2), (1, "Three", 2)]
    # Its form field is gone; page 3's stays.
    assert [w.field_name for p in doc for w in p.widgets()] == ["on_three"]
    assert result.warnings == []  # no page numbers printed


def test_the_only_page_cant_be_deleted():
    one = pymupdf.open()
    one.new_page()
    with pytest.raises(EditRejected, match="at least one page"):
        delete_page(one.tobytes(), 0)


def test_deleting_says_to_check_printed_page_numbers():
    result = delete_page(TWO, 0)
    assert len(pymupdf.open("pdf", result.pdf)) == 1
    assert any("page numbers" in w for w in result.warnings)


@pytest.mark.parametrize("after", [0, 1])
def test_a_new_page_is_like_the_page_before_it(after):
    result = add_page(TWO, after)
    doc = pymupdf.open("pdf", result.pdf)
    assert doc.page_count == 3
    old = texts(TWO)
    got = texts(result.pdf)
    assert [got[n] for n in range(3) if n != after + 1] == old
    # The new page: the header and footer of the page before it, nothing else.
    lines = [l.text for l in page_lines(doc[after + 1])]
    assert lines == ["Harbor Supplies - Proposal HS-2026-07", f"Page {after + 1} of 2"]
    assert doc[after + 1].rect == doc[after].rect
    assert result.marks[0][0] == after + 1
    assert result.warnings == [f"It has the header and footer of page {after + 1}: check its page number, and the page count on the other pages."]


def test_a_moved_page_keeps_the_links_that_go_to_it():
    src = linked()
    result = move_page(src, 2, 0)
    doc = pymupdf.open("pdf", result.pdf)
    assert texts(result.pdf) == ["Third page", "First page", "Second page"]
    # Page 1 (now 2) still links to "page 2" and "page 3", wherever they are now.
    targets = [l["page"] for l in doc[1].get_links() if l["kind"] == pymupdf.LINK_GOTO]
    assert [texts(result.pdf)[t] for t in targets] == ["Second page", "Third page"]
    with pytest.raises(EditRejected, match="already there"):
        move_page(src, 1, 1)


def test_text_on_a_new_page_in_the_style_of_another_page():
    """A new page has no text to take a style from: the text comes in the style of a line
    on another page, and nothing else is left on it."""
    from app.adding import add_text_styled_from

    src = add_page(TWO, 1).pdf
    page = pymupdf.open("pdf", src)[0]
    ref = next(l for l in page_lines(page) if l.text.startswith("2. Prices are given"))
    result = add_text_styled_from(src, 2, "Typed on the new page", 0, ref.id, ref.text, (90.0, 400.0))
    doc = pymupdf.open("pdf", result.pdf)
    assert [l.text for l in page_lines(doc[2])] == ["Harbor Supplies - Proposal HS-2026-07", "Typed on the new page", "Page 2 of 2"]
    new = next(l for l in page_lines(doc[2]) if l.text == "Typed on the new page")
    assert abs(new.bbox[0] - 90) < 1 and abs((new.bbox[1] + new.bbox[3]) / 2 - 400) < 2
    assert (new.style.font, round(new.style.size, 1)) == (ref.style.font, round(ref.style.size, 1))
    assert texts(result.pdf)[:2] == texts(src)[:2]
    assert result.marks[0][0] == 2


def test_pages_through_the_website(client):
    doc = upload(client, linked(), name="linked.pdf").json()
    base = f"/api/documents/{doc['id']}"
    r = client.post(f"{base}/pages/move", json={"revision": doc["revision"], "page": 2, "to": 0})
    assert r.status_code == 200, r.text
    moved = r.json()["document"]
    assert moved["history"][-1]["summary"] == "Moved page 3 to page 1"
    r = client.post(f"{base}/pages/delete", json={"revision": moved["revision"], "page": 0})
    assert r.status_code == 200, r.text
    deleted = r.json()["document"]
    assert deleted["pageCount"] == 2 and deleted["originalPageCount"] == 3
    r = client.post(f"{base}/pages/add", json={"revision": deleted["revision"], "page": 1})
    assert r.status_code == 200, r.text
    added = r.json()["document"]
    assert added["pageCount"] == 3 and added["history"][-1]["pages"] == [2]
    assert client.get(f"{base}/pages/2/lines").json()["lines"] == []  # blank: no header or footer to copy
    assert client.post(f"{base}/pages/delete", json={"revision": 0, "page": 0}).status_code == 409
    back = client.post(f"{base}/undo", json={"revision": added["revision"]}).json()
    assert back["pageCount"] == 2
