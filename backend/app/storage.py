"""On-disk store for uploaded documents.

Each document lives in its own folder named by an unguessable id:

    data/<id>/meta.json   name, timestamps, version count
    data/<id>/v0.pdf      the original upload
    data/<id>/v1.pdf ...  one file per saved edit (later steps), newest is current

Folders are deleted once they have not been used for DOC_TTL_SECONDS.
"""

import json
import re
import secrets
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")


class DocumentNotFound(Exception):
    pass


@dataclass
class DocumentMeta:
    id: str
    name: str
    created: float
    last_access: float
    version: int  # index of the current vN.pdf (goes back down on undo)
    revision: int = 0  # bumped on every change and never reused; keys cached page images


class DocumentStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _dir(self, doc_id: str) -> Path:
        # The id is used as a folder name, so reject anything that is not one of ours.
        if not _ID_RE.match(doc_id):
            raise DocumentNotFound(doc_id)
        return self.root / doc_id

    def _read_meta(self, doc_id: str) -> DocumentMeta:
        try:
            raw = (self._dir(doc_id) / "meta.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            raise DocumentNotFound(doc_id)
        return DocumentMeta(**json.loads(raw))

    def _write_meta(self, meta: DocumentMeta) -> None:
        path = self._dir(meta.id) / "meta.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(meta), ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def create(self, data: bytes, name: str) -> DocumentMeta:
        doc_id = secrets.token_urlsafe(16)
        folder = self._dir(doc_id)
        folder.mkdir()
        (folder / "v0.pdf").write_bytes(data)
        now = time.time()
        meta = DocumentMeta(id=doc_id, name=name, created=now, last_access=now, version=0, revision=0)
        with self._lock:
            self._write_meta(meta)
        return meta

    def get(self, doc_id: str) -> DocumentMeta:
        """Return the document's metadata and mark it as recently used."""
        with self._lock:
            meta = self._read_meta(doc_id)
            meta.last_access = time.time()
            self._write_meta(meta)
        return meta

    def current_path(self, meta: DocumentMeta) -> Path:
        return self._dir(meta.id) / f"v{meta.version}.pdf"

    def delete(self, doc_id: str) -> None:
        folder = self._dir(doc_id)
        if not folder.exists():
            raise DocumentNotFound(doc_id)
        shutil.rmtree(folder, ignore_errors=True)

    def cleanup_expired(self) -> int:
        """Delete documents not used within the TTL. Returns how many were removed."""
        cutoff = time.time() - config.DOC_TTL_SECONDS
        removed = 0
        for folder in self.root.iterdir():
            if not folder.is_dir() or not _ID_RE.match(folder.name):
                continue
            try:
                meta = self._read_meta(folder.name)
                expired = meta.last_access < cutoff
            except (DocumentNotFound, ValueError, TypeError):
                # Half-written or corrupt folder: fall back to its modification time.
                expired = folder.stat().st_mtime < cutoff
            if expired:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        return removed
