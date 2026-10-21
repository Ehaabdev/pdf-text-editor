import asyncio
import logging
import re
import sys
from contextlib import asynccontextmanager
from pathlib import PurePath

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config
from .pdf import InvalidPdf, describe_file, inspect_upload, render_page_png
from .storage import DocumentMeta, DocumentNotFound, DocumentStore

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
