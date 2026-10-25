"""Pictures: listed, deleted, replaced (fit, fill, stretch; on one page or every page they
are on), added, moved and resized, with the rest of the page left exactly as it was."""

import io
from pathlib import Path

import pikepdf
import pymupdf
import pytest
from PIL import Image

from app import images
from app.editing import EditRejected
from app.textlayout import page_lines

FIXTURES = Path(__file__).parent / "fixtures" / "images"
SOURCES = {p.stem.split("-")[-1]: p.read_bytes() for p in sorted(FIXTURES.glob("images-*.pdf"))}
PICTURE = {name: (FIXTURES / name).read_bytes() for name in ("stamp.png", "logo.png", "photo.jpg", "icon.png")}


def find(src: bytes, page_no: int, width: int):
    return next(i for i in images.page_images(src, page_no) if i.width == width)


def texts(pdf: bytes, page_no: int = 0):
    return [l.text for l in page_lines(pymupdf.open("pdf", pdf)[page_no])]


@pytest.mark.parametrize("source", SOURCES)
def test_the_pictures_on_a_page(source):
    found = images.page_images(SOURCES[source], 0)
    widths = sorted(i.width for i in found)
    assert {480, 900, 600, 300}.issubset(widths)  # logo, photo, signature, stamp
    signature = find(SOURCES[source], 0, 600)
    assert signature.pages == 2  # the same signature is on the second page too
    assert all(not i.background for i in found)


@pytest.mark.parametrize("source", SOURCES)
def test_deleting_a_picture_leaves_the_rest(source):
    src = SOURCES[source]
    photo = find(src, 0, 900)
    result = images.delete_image(src, 0, photo.id, photo.bbox)
    assert all(i.width != 900 for i in images.page_images(result.pdf, 0))
    assert texts(result.pdf) == texts(src)


@pytest.mark.parametrize("mode", ["fit", "fill", "stretch"])
def test_replacing_a_picture(mode):
    src = SOURCES["chrome"]
    photo = find(src, 0, 900)
    result = images.replace_image(src, 0, photo.id, photo.bbox, PICTURE["stamp.png"], mode)
    now = [i for i in images.page_images(result.pdf, 0) if i.width == 300]
    new = max(now, key=lambda i: (i.bbox[2] - i.bbox[0]) * (i.bbox[3] - i.bbox[1]))
    old = pymupdf.Rect(photo.bbox)
    if mode == "fit":  # the whole square stamp, as tall as the place, centered
        assert abs((new.bbox[3] - new.bbox[1]) - old.height) < 0.1
        assert abs((new.bbox[0] + new.bbox[2]) / 2 - (old.x0 + old.x1) / 2) < 0.1
    elif mode == "stretch":
        assert all(abs(a - b) < 0.1 for a, b in zip(new.bbox, photo.bbox))
    assert texts(result.pdf) == texts(src)


def test_replacing_a_picture_on_every_page_it_is_on():
    src = SOURCES["chrome"]
    signature = find(src, 0, 600)
    one = images.replace_image(src, 0, signature.id, signature.bbox, PICTURE["icon.png"], "fit")
    assert {a[0] for a in one.areas} == {0}
    every = images.replace_image(src, 0, signature.id, signature.bbox, PICTURE["icon.png"], "fit", everywhere=True)
    assert {a[0] for a in every.areas} == {0, 1}
    assert all(i.width != 600 for n in (0, 1) for i in images.page_images(every.pdf, n))


def test_adding_a_picture_keeps_its_see_through_parts():
    src = SOURCES["chrome"]
    box = (300, 120, 380, 200)
    result = images.add_image(src, 1, PICTURE["stamp.png"], box)
    added = images.page_images(result.pdf, 1)[-1]
    assert all(abs(a - b) < 0.05 for a, b in zip(added.bbox, box))
    with pikepdf.open(io.BytesIO(result.pdf)) as pdf:
        xo = pdf.pages[1].Resources.XObject
        assert any("/SMask" in xo[k] for k in xo.keys() if str(k).startswith("/Pic"))


def test_a_big_photo_is_kept_at_print_quality():
    src = SOURCES["chrome"]
    big = io.BytesIO()
    Image.new("RGB", (4000, 3000), (200, 120, 40)).save(big, "JPEG")
    result = images.add_image(src, 1, big.getvalue(), (100, 100, 172, 154))  # one inch wide
    added = images.page_images(result.pdf, 1)[-1]
    assert added.width <= 300 + 1  # 300 dots per inch


def test_moving_and_resizing_a_picture():
    src = SOURCES["chrome"]
    photo = find(src, 0, 900)
    box = (photo.bbox[0] + 40, photo.bbox[1] + 20, photo.bbox[0] + 160, photo.bbox[1] + 100)
    result = images.move_image(src, 0, photo.id, photo.bbox, box)
    moved = find(result.pdf, 0, 900)
    assert all(abs(a - b) < 0.05 for a, b in zip(moved.bbox, box))
    assert texts(result.pdf) == texts(src)


def test_a_picture_that_changed_is_refused():
    src = SOURCES["chrome"]
    photo = find(src, 0, 900)
    with pytest.raises(EditRejected, match="changed since"):
        images.delete_image(src, 0, photo.id, (0, 0, 10, 10))


def test_a_file_that_isnt_a_picture_is_refused():
    src = SOURCES["chrome"]
    photo = find(src, 0, 900)
    with pytest.raises(EditRejected, match="isn't a picture"):
        images.replace_image(src, 0, photo.id, photo.bbox, b"%PDF-1.7 not a picture", "fit")


def test_a_turned_page():
    """On a page shown turned, a new picture is upright as shown."""
    doc = pymupdf.open("pdf", SOURCES["chrome"])
    doc[1].set_rotation(90)
    src = doc.tobytes()
    box = (100, 100, 180, 140)
    result = images.add_image(src, 1, PICTURE["logo.png"], box)
    added = images.page_images(result.pdf, 1)[-1]
    assert all(abs(a - b) < 0.05 for a, b in zip(added.bbox, box))


def _with_drawn_logo() -> bytes:
    """A page with a logo made of shapes, kept as a form (as some programs save logos)."""
    logo = pymupdf.open()
    lp = logo.new_page(width=200, height=100)
    lp.draw_rect((10, 10, 190, 90), color=(0.8, 0.1, 0.1), fill=(0.9, 0.3, 0.3))
    lp.draw_circle((100, 50), 30, color=(0, 0, 0.6), fill=(0.2, 0.2, 0.8))
    doc = pymupdf.open()
    page = doc.new_page()
    page.draw_line((72, 72), (400, 72), color=(0, 0, 0))
    page.show_pdf_page(pymupdf.Rect(100, 100, 300, 200), logo, 0)
    page.draw_line((72, 260), (400, 260), color=(0, 0, 0))
    return doc.tobytes()


def test_replacing_a_drawn_logo_fills_its_place():
    src = _with_drawn_logo()
    (logo,) = images.page_images(src, 0)
    assert logo.kind == "drawing"
    result = images.replace_image(src, 0, logo.id, logo.bbox, PICTURE["logo.png"], "stretch")
    (new,) = images.page_images(result.pdf, 0)
    assert new.kind == "image"
    assert all(abs(a - b) < 0.1 for a, b in zip(new.bbox, logo.bbox))
    assert texts(result.pdf) == texts(src)


def test_a_picture_alone_for_dragging():
    src = SOURCES["chrome"]
    signature = find(src, 0, 600)
    png = Image.open(io.BytesIO(images.picture_png(src, 0, signature.id, 400)))
    assert png.mode == "RGBA" and png.width == 400
    # See-through around the signature: nothing else of the page (the table) comes along.
    alpha = png.getchannel("A")
    assert alpha.getextrema()[0] == 0


def test_pictures_through_the_website(client):
    """What the website does: list a page's pictures, show one alone, change them, undo."""
    from conftest import upload

    doc = upload(client, SOURCES["chrome"], "images.pdf").json()
    base = f"/api/documents/{doc['id']}"
    found = client.get(f"{base}/pages/0/images").json()["images"]
    photo = next(i for i in found if i["width"] == 900)
    alone = client.get(f"{base}/pages/0/images/{photo['id']}/picture?width=300")
    assert alone.status_code == 200 and alone.headers["content-type"] == "image/png"
    ref = {"revision": doc["revision"], "page": 0, "imageId": photo["id"], "bbox": photo["bbox"]}

    moved = client.post(f"{base}/images/move", json={**ref, "box": [100, 400, 250, 500]})
    assert moved.status_code == 200, moved.text
    doc = moved.json()["document"]
    assert doc["history"][-1]["summary"] == "Resized a picture"

    # The picture as seen before the move is refused now: it isn't there any more.
    stale = client.post(f"{base}/images/delete", json={**ref, "revision": doc["revision"]})
    assert stale.status_code == 422 and "Select it again" in stale.json()["detail"]

    found = client.get(f"{base}/pages/0/images").json()["images"]
    photo = next(i for i in found if i["width"] == 900)
    form = {"revision": doc["revision"], "page": 0, "imageId": photo["id"], "bbox": ",".join(map(str, photo["bbox"])), "mode": "fit", "everywhere": "false"}
    replaced = client.post(f"{base}/images/replace", data=form, files={"file": ("stamp.png", PICTURE["stamp.png"], "image/png")})
    assert replaced.status_code == 200, replaced.text
    doc = replaced.json()["document"]

    added = client.post(
        f"{base}/images/add",
        data={"revision": doc["revision"], "page": 1, "box": "300,120,380,200"},
        files={"file": ("logo.png", PICTURE["logo.png"], "image/png")},
    )
    assert added.status_code == 200, added.text
    doc = added.json()["document"]
    assert [c["summary"] for c in doc["history"]] == ["Resized a picture", "Replaced a picture", "Added a picture"]

    bad = client.post(
        f"{base}/images/add",
        data={"revision": doc["revision"], "page": 1, "box": "300,120,380,200"},
        files={"file": ("notes.txt", b"not a picture", "text/plain")},
    )
    assert bad.status_code == 422 and "JPEG or PNG" in bad.json()["detail"]

    undone = client.post(f"{base}/undo", json={"revision": doc["revision"]})
    assert undone.status_code == 200
    assert len(client.get(f"{base}/pages/1/images").json()["images"]) == len(images.page_images(SOURCES["chrome"], 1))


def _encoded(name: str, fmt: str) -> bytes:
    out = io.BytesIO()
    Image.open(io.BytesIO(PICTURE[name])).save(out, fmt)
    return out.getvalue()


def test_a_webp_picture_from_a_phone_is_added():
    result = images.add_image(SOURCES["chrome"], 1, _encoded("stamp.png", "WEBP"), (300, 120, 380, 200))
    assert len(images.page_images(result.pdf, 1)) == len(images.page_images(SOURCES["chrome"], 1)) + 1


@pytest.mark.parametrize("fmt", ["GIF", "BMP", "TIFF"])
def test_only_jpeg_png_and_webp_are_read(fmt):
    with pytest.raises(EditRejected, match="JPEG or PNG"):
        images.add_image(SOURCES["chrome"], 1, _encoded("stamp.png", fmt), (300, 120, 380, 200))
