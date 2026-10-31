import asyncio
import base64
import logging
import re
import sys
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path, PurePath
from urllib.parse import quote

import pymupdf
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import columns, config, dragging, exporting, images, limits, rows
from .pdf import InvalidPdf, describe_file, inspect_upload, render_page_png
from .adding import add_line
from .deleting import delete_lines
from .editing import EditRejected, replace_line
from .lists import add_item, markers, renumber_after_delete
from .spots import plan as plan_spot
from .storage import DocumentMeta, DocumentNotFound, DocumentStore, NoSuchVersion, NothingToUndo, StaleRevision, StorageFull, TooManyChanges
from .textlayout import line_json, lines_for

# Arabic and other non-Latin text must never crash logging on a Windows console.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

log = logging.getLogger("pdfedit")
store = DocumentStore(config.DATA_DIR)


async def _cleanup_loop():
    while True:
        try:
            removed = await run_in_threadpool(store.cleanup_expired)
            if removed:
                log.info("deleted %d expired documents", removed)
        except Exception:
            log.exception("cleanup failed")
        await asyncio.sleep(config.CLEANUP_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()


app = FastAPI(title="PDF Editor", lifespan=lifespan)


@app.middleware("http")
async def reject_oversized_uploads(request: Request, call_next):
    # Refuse before the body is read. Small allowance for multipart overhead.
    length = request.headers.get("content-length")
    if request.method == "POST" and length and length.isdigit():
        if int(length) > config.MAX_UPLOAD_BYTES + 64 * 1024:
            return JSONResponse({"detail": _too_large_message()}, status_code=413)
    return await call_next(request)


@app.middleware("http")
async def limit_visitors(request: Request, call_next):
    """How often one visitor may upload, change and look at pages; heavy jobs one or two
    at a time (limits.py)."""
    kind = limits.kind_of(request.method, request.url.path)
    if kind is None:
        return await call_next(request)
    visitor = request.client.host if request.client else "unknown"
    if not limits.limiter.allow(visitor, kind):
        return JSONResponse({"detail": limits.MESSAGES[kind]}, status_code=429, headers={"Retry-After": "60"})
    if kind not in limits.HEAVY:
        return await call_next(request)
    async with limits.heavy_gate():
        return await call_next(request)


@app.exception_handler(TooManyChanges)
async def _too_many_changes(request: Request, exc: TooManyChanges):
    return JSONResponse(
        {"detail": "This file has had as many changes as it can keep. Download it, then open the downloaded file to keep editing."},
        status_code=409,
    )


@app.exception_handler(StorageFull)
async def _storage_full(request: Request, exc: StorageFull):
    return JSONResponse(
        {"detail": "The server is full right now. Files are deleted an hour after their last use, so try again a little later."},
        status_code=503,
    )


def _too_large_message() -> str:
    return f"This file is larger than {config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB."


def _clean_filename(raw: str | None) -> str:
    name = PurePath((raw or "").replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip() or "document.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    if len(name) > 120:
        name = name[:116] + ".pdf"
    return name


def _get_meta(doc_id: str) -> DocumentMeta:
    try:
        return store.get(doc_id)
    except DocumentNotFound:
        raise HTTPException(404, "This document no longer exists. Files are deleted an hour after they were last used.")


def _document_json(meta: DocumentMeta, info) -> dict:
    return {
        "id": meta.id,
        "name": meta.name,
        "revision": meta.revision,
        "edited": meta.version > 0,
        "canUndo": meta.version > 0,
        "canRedo": meta.version < len(meta.history),
        "version": meta.version,
        # Every change, in order; those after the current version were undone (and can
        # be redone until the next change).
        "history": [{**entry, "applied": k < meta.version} for k, entry in enumerate(meta.history)],
        "pageCount": info.page_count,
        "pages": [{"width": p.width, "height": p.height} for p in info.pages],
        "isSigned": info.is_signed,
    }


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/documents", status_code=201)
async def upload_document(file: UploadFile = File(...)):
    chunks, total = [], 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > config.MAX_UPLOAD_BYTES:
            raise HTTPException(413, _too_large_message())
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(400, "The file is empty.")
    try:
        info = await run_in_threadpool(inspect_upload, data)
    except InvalidPdf as e:
        raise HTTPException(400, str(e))
    meta = await run_in_threadpool(store.create, data, _clean_filename(file.filename))
    return _document_json(meta, info)


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str):
    meta = _get_meta(doc_id)
    return _document_json(meta, describe_file(store.current_path(meta)))


@app.get("/api/documents/{doc_id}/pages/{page_no}/image")
def page_image(doc_id: str, page_no: int, width: int = Query(1200), version: int | None = None, clip: str | None = None):
    """The page as an image: the current version, or any stored one (0 is the original);
    the whole page, or only `clip` ("x0,y0,x1,y1" in points, as displayed)."""
    meta = _get_meta(doc_id)
    try:
        path = store.current_path(meta) if version is None else store.version_path(meta, version)
    except NoSuchVersion:
        raise HTTPException(404, "That version doesn't exist.")
    area = None
    if clip is not None:
        try:
            area = tuple(float(v) for v in clip.split(","))
            if len(area) != 4:
                raise ValueError
        except ValueError:
            raise HTTPException(400, "clip must be four numbers: x0,y0,x1,y1.")
    try:
        png = render_page_png(path, page_no, width, area)
    except IndexError:
        raise HTTPException(404, "That page doesn't exist.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Image URLs carry the document revision, so a given URL never changes.
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/documents/{doc_id}/pages/{page_no}/lines")
def page_text_lines(doc_id: str, page_no: int):
    meta = _get_meta(doc_id)
    path = store.current_path(meta)
    try:
        width, height, lines = lines_for(path, page_no)
    except IndexError:
        raise HTTPException(404, "That page doesn't exist.")
    items = _list_markers(str(path), path.stat().st_mtime_ns, page_no)
    return JSONResponse(
        # "item": the marker a new list item would get below or above the line, if it's in a list.
        {"width": width, "height": height, "lines": [{**line_json(l), "item": items.get(l.id)} for l in lines]},
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.get("/api/documents/{doc_id}/pages/{page_no}/spot")
def spot_plan(doc_id: str, page_no: int, x: float, y: float):
    """What adding text at (x, y), in points as displayed, would do: a new list item, a new
    line (the text below moving down), text joining a line, or text where clicked."""
    meta = _get_meta(doc_id)
    src = store.current_path(meta).read_bytes()
    try:
        return plan_spot(src, page_no, x, y).json()
    except IndexError:
        raise HTTPException(404, "That page doesn't exist.")


@lru_cache(maxsize=64)
def _list_markers(path: str, mtime_ns: int, page_no: int) -> dict:
    return markers(Path(path).read_bytes(), page_no, list(lines_for(path, page_no)[2]))


class EditRequest(BaseModel):
    revision: int
    page: int
    lineId: str = Field(max_length=32)
    oldText: str = Field(max_length=5000)
    newText: str = Field(max_length=5000)


class LineRef(BaseModel):
    page: int
    lineId: str = Field(max_length=32)
    oldText: str = Field(max_length=5000)


class DeletionRequest(BaseModel):
    revision: int
    lines: list[LineRef] = Field(min_length=1, max_length=500)


class AdditionRequest(BaseModel):
    revision: int
    page: int
    lineId: str = Field(max_length=32)  # the line whose style and place the new line follows
    oldText: str = Field(max_length=5000)
    text: str = Field(min_length=1, max_length=2000)
    where: str = Field(default="below", pattern="^(below|above)$")
    # Or a point on the page as displayed, in points: the text starts there.
    at: tuple[float, float] | None = None
    # As a new item of the line's list: numbered on, with the items after it renumbered.
    item: bool = False


class RowRequest(BaseModel):
    revision: int
    page: int
    lineId: str = Field(max_length=32)  # a line in the row
    oldText: str = Field(max_length=5000)
    where: str = Field(default="below", pattern="^(below|above)$")
    cells: list[str] = Field(default_factory=list, max_length=40)  # the new row's text, in the table's order


class ColumnRequest(BaseModel):
    revision: int
    page: int
    lineId: str = Field(max_length=32)  # a line in the column
    oldText: str = Field(max_length=5000)
    where: str = Field(default="right", pattern="^(left|right)$")  # on the page
    cells: list[str] = Field(default_factory=list, max_length=200)  # the new column's text, top to bottom


class ResizeRequest(BaseModel):
    revision: int
    page: int
    lineId: str = Field(max_length=32)  # a line in the table
    oldText: str = Field(max_length=5000)
    edge: int = Field(ge=0, le=200)  # which column edge, left to right (0: the table's left edge)
    x: float = Field(ge=-10000, le=10000)  # where it goes, as displayed


class MovedLine(BaseModel):
    lineId: str = Field(max_length=32)
    oldText: str = Field(max_length=5000)


class MoveRequest(BaseModel):
    revision: int
    page: int
    lines: list[MovedLine] = Field(min_length=1, max_length=300)  # on one page
    table: bool = False  # the whole table the first line is in
    dx: float = Field(default=0.0, ge=-5000, le=5000)  # points as displayed: right
    dy: float = Field(default=0.0, ge=-5000, le=5000)  # down


class RevisionRequest(BaseModel):
    revision: int


_STALE = "This document changed in another tab or window. Reload the page to see the latest version."


def _short(text: str, n: int = 40) -> str:
    """Quoted text in a change's summary: shortened, and isolated (FSI ... PDI) so an
    Arabic quote inside an English sentence keeps its own direction."""
    text = " ".join(text.split())
    text = text if len(text) <= n else text[: n - 1] + "…"
    return "\u2068" + text + "\u2069"


def _outcome(meta, result) -> dict:
    """The reply to a change: the document now, and what to tell the user about it."""
    return {
        "document": _document_json(meta, describe_file(store.current_path(meta))),
        "warnings": result.warnings,
        "lookalikes": result.lookalikes,
        "moved": result.moved,  # lines that moved down (to make room) or up (into the space)
    }


def _entry(kind: str, summary: str, result) -> dict:
    """What a change was, for the list of changes and the before/after view."""
    return {
        "kind": kind,
        "summary": summary,
        "pages": sorted({a[0] for a in result.areas}),
        "areas": [[round(v, 2) for v in a] for a in result.areas],  # [page, x0, y0, x1, y1]
        "marks": [[round(v, 2) for v in a] for a in result.marks],  # the same, drawn tight
        "warnings": result.warnings,
        "lookalikes": result.lookalikes,  # fonts that stood in for missing ones: to check
    }


@app.post("/api/documents/{doc_id}/edits")
async def edit_line(doc_id: str, edit: EditRequest):
    meta = _get_meta(doc_id)
    if meta.revision != edit.revision:
        raise HTTPException(409, _STALE)
    if any(c in edit.newText for c in ("\n", "\r", "\u2028", "\u2029")):
        raise HTTPException(422, "A line can't contain a line break.")
    src = store.current_path(meta).read_bytes()
    try:
        result = await run_in_threadpool(replace_line, src, edit.page, edit.lineId, edit.oldText, edit.newText)
        if not edit.newText.strip():  # emptied: deleted, so a list counts on without it
            result = await run_in_threadpool(renumber_after_delete, src, result, [(edit.page, edit.lineId, edit.oldText)])
    except EditRejected as e:
        raise HTTPException(422, str(e))
    if result.pdf == src:
        return _outcome(meta, result)
    if edit.newText.strip():
        entry = _entry("edit", f"Changed “{_short(edit.oldText)}” to “{_short(result.text)}”", result)
    else:
        entry = _entry("delete", f"Deleted “{_short(edit.oldText)}”", result)
    try:
        meta = await run_in_threadpool(store.save_version, doc_id, edit.revision, result.pdf, entry)
    except StaleRevision:
        raise HTTPException(409, _STALE)
    return _outcome(meta, result)


@app.post("/api/documents/{doc_id}/deletions")
async def delete_document_lines(doc_id: str, body: DeletionRequest):
    """Delete lines, on one page or several, as one change (one Undo brings them back)."""
    meta = _get_meta(doc_id)
    if meta.revision != body.revision:
        raise HTTPException(409, _STALE)
    src = store.current_path(meta).read_bytes()
    targets = [(l.page, l.lineId, l.oldText) for l in body.lines]
    try:
        result = await run_in_threadpool(delete_lines, src, targets)
        result = await run_in_threadpool(renumber_after_delete, src, result, targets)
    except EditRejected as e:
        raise HTTPException(422, str(e))
    n = len(body.lines)
    entry = _entry("delete", f"Deleted “{_short(body.lines[0].oldText)}”" if n == 1 else f"Deleted {n} lines", result)
    try:
        meta = await run_in_threadpool(store.save_version, doc_id, body.revision, result.pdf, entry)
    except StaleRevision:
        raise HTTPException(409, _STALE)
    return _outcome(meta, result)


@app.post("/api/documents/{doc_id}/additions")
async def add_document_line(doc_id: str, body: AdditionRequest):
    """Add a new line below or above a line, in its style, or at a point on the page."""
    meta = _get_meta(doc_id)
    if meta.revision != body.revision:
        raise HTTPException(409, _STALE)
    if any(c in body.text for c in ("\n", "\r", "\u2028", "\u2029")):
        raise HTTPException(422, "Add one line at a time.")
    src = store.current_path(meta).read_bytes()
    try:
        if body.item and body.at is None:
            result = await run_in_threadpool(add_item, src, body.page, body.text, body.lineId, body.oldText, body.where)
        else:
            result = await run_in_threadpool(add_line, src, body.page, body.text, body.lineId, body.oldText, body.where, body.at)
    except EditRejected as e:
        raise HTTPException(422, str(e))
    entry = _entry("add", f"Added “{_short(result.text)}”", result)
    try:
        meta = await run_in_threadpool(store.save_version, doc_id, body.revision, result.pdf, entry)
    except StaleRevision:
        raise HTTPException(409, _STALE)
    return _outcome(meta, result)


# Table rows -------------------------------------------------------------------------------


@app.get("/api/documents/{doc_id}/pages/{page_no}/tables")
def page_tables(doc_id: str, page_no: int):
    """The page's tables with borders: each row's cells, their text and their lines."""
    meta = _get_meta(doc_id)
    path = store.current_path(meta)
    try:
        found = _tables(str(path), path.stat().st_mtime_ns, page_no)
    except IndexError:
        raise HTTPException(404, "That page doesn't exist.")
    return JSONResponse({"tables": found}, headers={"Cache-Control": "private, max-age=3600"})


@lru_cache(maxsize=64)
def _tables(path: str, mtime_ns: int, page_no: int) -> list[dict]:
    lines = list(lines_for(Path(path), page_no)[2])
    with pymupdf.open(path) as doc:
        return rows.table_json(doc[page_no], lines)


@app.post("/api/documents/{doc_id}/rows/add")
async def add_table_row(doc_id: str, body: RowRequest):
    """A new row below or above the row a line is in, looking like it, with its cells' text."""
    meta = _get_meta(doc_id)
    if meta.revision != body.revision:
        raise HTTPException(409, _STALE)
    if any(len(c) > 500 or any(b in c for b in ("\n", "\r", "\u2028", "\u2029")) for c in body.cells):
        raise HTTPException(422, "A cell's text goes on one line.")
    src = store.current_path(meta).read_bytes()
    try:
        result = await run_in_threadpool(rows.add_row, src, body.page, body.lineId, body.oldText, body.where, body.cells)
    except EditRejected as e:
        raise HTTPException(422, str(e))
    entry = _entry("add", "Added a table row", result)
    try:
        meta = await run_in_threadpool(store.save_version, doc_id, body.revision, result.pdf, entry)
    except StaleRevision:
        raise HTTPException(409, _STALE)
    return _outcome(meta, result)


@app.post("/api/documents/{doc_id}/rows/delete")
async def delete_table_row(doc_id: str, body: RowRequest):
    """The row a line is in taken out of its table; what's below moves up."""
    meta = _get_meta(doc_id)
    if meta.revision != body.revision:
        raise HTTPException(409, _STALE)
    src = store.current_path(meta).read_bytes()
    try:
        result = await run_in_threadpool(rows.delete_row, src, body.page, body.lineId, body.oldText)
    except EditRejected as e:
        raise HTTPException(422, str(e))
    entry = _entry("delete", "Deleted a table row", result)
    try:
        meta = await run_in_threadpool(store.save_version, doc_id, body.revision, result.pdf, entry)
    except StaleRevision:
        raise HTTPException(409, _STALE)
    return _outcome(meta, result)


@app.post("/api/documents/{doc_id}/columns/add")
async def add_table_column(doc_id: str, body: ColumnRequest):
    """A new column left or right of the column a line is in, looking like it, with its cells' text."""
    meta = _get_meta(doc_id)
    if meta.revision != body.revision:
        raise HTTPException(409, _STALE)
    if any(len(c) > 500 or any(b in c for b in ("\n", "\r", "\u2028", "\u2029")) for c in body.cells):
        raise HTTPException(422, "A cell's text goes on one line.")
    src = store.current_path(meta).read_bytes()
    try:
        result = await run_in_threadpool(columns.add_column, src, body.page, body.lineId, body.oldText, body.where, body.cells)
    except EditRejected as e:
        raise HTTPException(422, str(e))
    entry = _entry("add", "Added a table column", result)
    try:
        meta = await run_in_threadpool(store.save_version, doc_id, body.revision, result.pdf, entry)
    except StaleRevision:
        raise HTTPException(409, _STALE)
    return _outcome(meta, result)


@app.post("/api/documents/{doc_id}/columns/delete")
async def delete_table_column(doc_id: str, body: RowRequest):
    """The column a line is in taken out of its table; the table gets narrower."""
    meta = _get_meta(doc_id)
    if meta.revision != body.revision:
        raise HTTPException(409, _STALE)
    src = store.current_path(meta).read_bytes()
    try:
        result = await run_in_threadpool(columns.delete_column, src, body.page, body.lineId, body.oldText)
    except EditRejected as e:
        raise HTTPException(422, str(e))
    entry = _entry("delete", "Deleted a table column", result)
    try:
        meta = await run_in_threadpool(store.save_version, doc_id, body.revision, result.pdf, entry)
    except StaleRevision:
        raise HTTPException(409, _STALE)
    return _outcome(meta, result)


@app.post("/api/documents/{doc_id}/columns/resize")
async def resize_table_column(doc_id: str, body: ResizeRequest):
    """A column edge of the table a line is in dragged to a new place."""
    meta = _get_meta(doc_id)
    if meta.revision != body.revision:
        raise HTTPException(409, _STALE)
    src = store.current_path(meta).read_bytes()
    try:
        result = await run_in_threadpool(columns.resize_column, src, body.page, body.lineId, body.oldText, body.edge, body.x)
    except EditRejected as e:
        raise HTTPException(422, str(e))
    entry = _entry("move", "Resized a table column", result)
    try:
        meta = await run_in_threadpool(store.save_version, doc_id, body.revision, result.pdf, entry)
    except StaleRevision:
        raise HTTPException(409, _STALE)
    return _outcome(meta, result)


# Moving lines or a table -------------------------------------------------------------------


@app.post("/api/documents/{doc_id}/moves/preview")
async def move_preview(doc_id: str, body: MoveRequest):
    """What would move, as a picture on a see-through background with the box it covers
    on the page; or the lines it can only move with."""
    meta = _get_meta(doc_id)
    if meta.revision != body.revision:
        raise HTTPException(409, _STALE)
    src = store.current_path(meta).read_bytes()
    targets = [(l.lineId, l.oldText) for l in body.lines]
    try:
        png, box, ids = await run_in_threadpool(dragging.preview, src, body.page, targets, body.table)
    except dragging.Together as e:
        return {"together": [l.id for l in e.lines], "message": str(e)}
    except EditRejected as e:
        raise HTTPException(422, str(e))
    return {"box": box, "lines": ids, "image": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}


@app.post("/api/documents/{doc_id}/moves")
async def move_lines(doc_id: str, body: MoveRequest):
    """The lines (or the table) moved by dx, dy on their page; their old place stays empty."""
    meta = _get_meta(doc_id)
    if meta.revision != body.revision:
        raise HTTPException(409, _STALE)
    src = store.current_path(meta).read_bytes()
    targets = [(l.lineId, l.oldText) for l in body.lines]
    try:
        result = await run_in_threadpool(dragging.move, src, body.page, targets, body.dx, body.dy, body.table)
    except EditRejected as e:
        raise HTTPException(422, str(e))
    n = result.moved
    summary = "Moved a table" if body.table else f"Moved “{_short(body.lines[0].oldText)}”" if n == 1 else f"Moved {n} lines"
    entry = _entry("move", summary, result)
    try:
        meta = await run_in_threadpool(store.save_version, doc_id, body.revision, result.pdf, entry)
    except StaleRevision:
        raise HTTPException(409, _STALE)
    return _outcome(meta, result)


# Pictures ---------------------------------------------------------------------------------


@app.get("/api/documents/{doc_id}/pages/{page_no}/images")
def page_pictures(doc_id: str, page_no: int):
    """The pictures on a page: where, how big, and on how many pages each one is."""
    meta = _get_meta(doc_id)
    path = store.current_path(meta)
    try:
        found = _pictures(str(path), path.stat().st_mtime_ns, page_no)
    except IndexError:
        raise HTTPException(404, "That page doesn't exist.")
    return JSONResponse({"images": found}, headers={"Cache-Control": "private, max-age=3600"})


@lru_cache(maxsize=64)
def _pictures(path: str, mtime_ns: int, page_no: int) -> list[dict]:
    return [i.json() for i in images.page_images(Path(path).read_bytes(), page_no)]


@app.get("/api/documents/{doc_id}/pages/{page_no}/images/{image_id}/picture")
def page_picture(doc_id: str, page_no: int, image_id: str, width: int = Query(600)):
    """One picture alone, see-through around it: shown while it's being moved."""
    meta = _get_meta(doc_id)
    path = store.current_path(meta)
    try:
        png = images.picture_png(path.read_bytes(), page_no, image_id, width)
    except EditRejected as e:
        raise HTTPException(404, str(e))
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


class PictureRef(BaseModel):
    revision: int
    page: int
    imageId: str = Field(max_length=32)
    bbox: tuple[float, float, float, float]  # where it was, as the person saw it


class PictureMove(PictureRef):
    box: tuple[float, float, float, float]  # where it goes, as displayed


def _box(text: str) -> tuple[float, float, float, float]:
    try:
        values = tuple(float(v) for v in text.split(","))
    except ValueError:
        values = ()
    if len(values) != 4:
        raise HTTPException(400, "A box is four numbers: x0,y0,x1,y1.")
    return values  # type: ignore[return-value]


async def _read_picture(file: UploadFile) -> bytes:
    data = await file.read(images.MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(400, "The picture is empty.")
    return data


async def _save_change(doc_id: str, meta, revision: int, result, summary: str) -> dict:
    if meta.revision != revision:
        raise HTTPException(409, _STALE)
    try:
        meta = await run_in_threadpool(store.save_version, doc_id, revision, result.pdf, _entry("image", summary, result))
    except StaleRevision:
        raise HTTPException(409, _STALE)
    return _outcome(meta, result)


@app.post("/api/documents/{doc_id}/images/delete")
async def delete_picture(doc_id: str, body: PictureRef):
    meta = _get_meta(doc_id)
    if meta.revision != body.revision:
        raise HTTPException(409, _STALE)
    src = store.current_path(meta).read_bytes()
    try:
        result = await run_in_threadpool(images.delete_image, src, body.page, body.imageId, body.bbox)
    except EditRejected as e:
        raise HTTPException(422, str(e))
    return await _save_change(doc_id, meta, body.revision, result, "Deleted a picture")


@app.post("/api/documents/{doc_id}/images/replace")
async def replace_picture(
    doc_id: str,
    file: UploadFile = File(...),
    revision: int = Form(...),
    page: int = Form(...),
    imageId: str = Form(..., max_length=32),
    bbox: str = Form(...),
    mode: str = Form("fit"),
    everywhere: bool = Form(False),
):
    meta = _get_meta(doc_id)
    if meta.revision != revision:
        raise HTTPException(409, _STALE)
    src = store.current_path(meta).read_bytes()
    data = await _read_picture(file)
    try:
        result = await run_in_threadpool(images.replace_image, src, page, imageId, _box(bbox), data, mode, everywhere)
    except EditRejected as e:
        raise HTTPException(422, str(e))
    pages = len({a[0] for a in result.areas})
    summary = "Replaced a picture" + (f" on {pages} pages" if pages > 1 else "")
    return await _save_change(doc_id, meta, revision, result, summary)


@app.post("/api/documents/{doc_id}/images/add")
async def add_picture(
    doc_id: str,
    file: UploadFile = File(...),
    revision: int = Form(...),
    page: int = Form(...),
    box: str = Form(...),
):
    meta = _get_meta(doc_id)
    if meta.revision != revision:
        raise HTTPException(409, _STALE)
    src = store.current_path(meta).read_bytes()
    data = await _read_picture(file)
    try:
        result = await run_in_threadpool(images.add_image, src, page, data, _box(box))
    except EditRejected as e:
        raise HTTPException(422, str(e))
    return await _save_change(doc_id, meta, revision, result, "Added a picture")


@app.post("/api/documents/{doc_id}/images/move")
async def move_picture(doc_id: str, body: PictureMove):
    meta = _get_meta(doc_id)
    if meta.revision != body.revision:
        raise HTTPException(409, _STALE)
    src = store.current_path(meta).read_bytes()
    try:
        result = await run_in_threadpool(images.move_image, src, body.page, body.imageId, body.bbox, body.box)
    except EditRejected as e:
        raise HTTPException(422, str(e))
    old, new = body.bbox, body.box
    resized = abs((old[2] - old[0]) - (new[2] - new[0])) > 0.5 or abs((old[3] - old[1]) - (new[3] - new[1])) > 0.5
    return await _save_change(doc_id, meta, body.revision, result, "Resized a picture" if resized else "Moved a picture")


@app.post("/api/documents/{doc_id}/undo")
def undo_edit(doc_id: str, body: RevisionRequest):
    _get_meta(doc_id)
    try:
        meta = store.undo(doc_id, body.revision)
    except StaleRevision:
        raise HTTPException(409, _STALE)
    except NothingToUndo:
        raise HTTPException(409, "There is nothing to undo.")
    return _document_json(meta, describe_file(store.current_path(meta)))


@app.post("/api/documents/{doc_id}/redo")
def redo_edit(doc_id: str, body: RevisionRequest):
    _get_meta(doc_id)
    try:
        meta = store.redo(doc_id, body.revision)
    except StaleRevision:
        raise HTTPException(409, _STALE)
    except NoSuchVersion:
        raise HTTPException(409, "There is nothing to redo.")
    return _document_json(meta, describe_file(store.current_path(meta)))


@app.post("/api/documents/{doc_id}/versions/{version}")
def go_to_version(doc_id: str, version: int, body: RevisionRequest):
    """Make a stored version current: 0 is the original ("start over"). Later versions
    are kept, so this can be redone."""
    _get_meta(doc_id)
    try:
        meta = store.go_to(doc_id, body.revision, version)
    except StaleRevision:
        raise HTTPException(409, _STALE)
    except NoSuchVersion:
        raise HTTPException(404, "That version doesn't exist.")
    return _document_json(meta, describe_file(store.current_path(meta)))


@app.get("/api/documents/{doc_id}/download")
def download_document(doc_id: str):
    meta = _get_meta(doc_id)
    name = meta.name
    if meta.version > 0:
        name = name[:-4] + "-edited.pdf"
    return FileResponse(store.current_path(meta), media_type="application/pdf", filename=name)


@app.get("/api/documents/{doc_id}/export")
def export_document(doc_id: str, format: str = Query(..., max_length=20)):
    """Download as…: the current PDF as pictures, PowerPoint, Word or Excel (exporting.py)."""
    meta = _get_meta(doc_id)
    name = meta.name[:-4] + "-edited.pdf" if meta.version > 0 else meta.name  # as Download names it
    try:
        out = exporting.export(store.current_path(meta).read_bytes(), name, format)
    except exporting.UnknownFormat as e:
        raise HTTPException(400, str(e))
    quoted = quote(out.filename)
    disposition = f"attachment; filename*=utf-8''{quoted}" if quoted != out.filename else f'attachment; filename="{out.filename}"'
    headers = {"Content-Disposition": disposition, "Cache-Control": "no-store"}
    if out.note:
        headers["X-Export-Note"] = quote(out.note)
    return Response(out.data, media_type=out.media_type, headers=headers)


@app.delete("/api/documents/{doc_id}", status_code=204)
def delete_document(doc_id: str):
    try:
        store.delete(doc_id)
    except DocumentNotFound:
        raise HTTPException(404, "This document no longer exists.")
    return Response(status_code=204)


# The built website, when present (production). In development Vite serves it.
if config.FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=config.FRONTEND_DIST, html=True), name="site")
