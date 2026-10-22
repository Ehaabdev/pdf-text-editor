import asyncio
import logging
import re
import sys
from contextlib import asynccontextmanager
from pathlib import PurePath

from fastapi import Body, FastAPI, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config
from .pdf import InvalidPdf, describe_file, inspect_upload, render_page_png
from .adding import add_line
from .deleting import delete_lines
from .editing import EditRejected, replace_line
from .storage import DocumentMeta, DocumentNotFound, DocumentStore, NothingToUndo, StaleRevision
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
def page_image(doc_id: str, page_no: int, width: int = Query(1200)):
    meta = _get_meta(doc_id)
    try:
        png = render_page_png(store.current_path(meta), page_no, width)
    except IndexError:
        raise HTTPException(404, "That page doesn't exist.")
    # Image URLs carry the document revision, so a given URL never changes.
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/documents/{doc_id}/pages/{page_no}/lines")
def page_text_lines(doc_id: str, page_no: int):
    meta = _get_meta(doc_id)
    try:
        width, height, lines = lines_for(store.current_path(meta), page_no)
    except IndexError:
        raise HTTPException(404, "That page doesn't exist.")
    return JSONResponse(
        {"width": width, "height": height, "lines": [line_json(l) for l in lines]},
        headers={"Cache-Control": "private, max-age=3600"},
    )


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


class RevisionRequest(BaseModel):
    revision: int


_STALE = "This document changed in another tab or window. Reload the page to see the latest version."


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
    except EditRejected as e:
        raise HTTPException(422, str(e))
    if result.pdf == src:
        return {"document": _document_json(meta, describe_file(store.current_path(meta))), "warnings": result.warnings}
    try:
        meta = await run_in_threadpool(store.save_version, doc_id, edit.revision, result.pdf)
    except StaleRevision:
        raise HTTPException(409, _STALE)
    return {"document": _document_json(meta, describe_file(store.current_path(meta))), "warnings": result.warnings}


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
    except EditRejected as e:
        raise HTTPException(422, str(e))
    try:
        meta = await run_in_threadpool(store.save_version, doc_id, body.revision, result.pdf)
    except StaleRevision:
        raise HTTPException(409, _STALE)
    return {"document": _document_json(meta, describe_file(store.current_path(meta))), "warnings": result.warnings}


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
        result = await run_in_threadpool(add_line, src, body.page, body.text, body.lineId, body.oldText, body.where, body.at)
    except EditRejected as e:
        raise HTTPException(422, str(e))
    try:
        meta = await run_in_threadpool(store.save_version, doc_id, body.revision, result.pdf)
    except StaleRevision:
        raise HTTPException(409, _STALE)
    return {"document": _document_json(meta, describe_file(store.current_path(meta))), "warnings": result.warnings}


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


@app.get("/api/documents/{doc_id}/download")
def download_document(doc_id: str):
    meta = _get_meta(doc_id)
    name = meta.name
    if meta.version > 0:
        name = name[:-4] + "-edited.pdf"
    return FileResponse(store.current_path(meta), media_type="application/pdf", filename=name)


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
