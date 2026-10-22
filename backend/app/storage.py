"""On-disk store for uploaded documents.

Each document lives in its own folder named by an unguessable id:

    data/<id>/meta.json   name, timestamps, current version, history of changes
    data/<id>/v0.pdf      the original upload
    data/<id>/v1.pdf ...  one file per change; `version` says which is current

Undo, redo and "start over" move `version` between the files: a change that was undone
is kept (and can be redone) until a new change is made.

Folders are deleted once they have not been used for DOC_TTL_SECONDS.
"""

import json
import re
import secrets
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")


class DocumentNotFound(Exception):
    pass


class StaleRevision(Exception):
    """The document changed since the client last saw it (another tab, a double click)."""


class NothingToUndo(Exception):
    pass


class NoSuchVersion(Exception):
    pass


@dataclass
class DocumentMeta:
    id: str
    name: str
    created: float
    last_access: float
    version: int  # index of the current vN.pdf (goes back down on undo)
    revision: int = 0  # bumped on every change and never reused; keys cached page images
    # One entry per change: history[k] made v{k+1}.pdf from v{k}.pdf. What it was, and
    # where on which pages it changed anything (see EditResult.areas).
    history: list[dict] = field(default_factory=list)


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

    def version_path(self, meta: DocumentMeta, version: int) -> Path:
        """Any stored version: 0 is the original."""
        path = self._dir(meta.id) / f"v{version}.pdf"
        if version < 0 or not path.exists():
            raise NoSuchVersion(version)
        return path

    def save_version(self, doc_id: str, expected_revision: int, data: bytes, entry: dict | None = None) -> DocumentMeta:
        """Store an edited copy as the new current version, with what the change was.
        Versions after the current one (undone changes) are dropped, files and history
        together. Fails if someone changed the document meanwhile."""
        with self._lock:
            meta = self._read_meta(doc_id)
            if meta.revision != expected_revision:
                raise StaleRevision(doc_id)
            folder = self._dir(doc_id)
            for old in folder.glob("v*.pdf"):
                if int(old.stem[1:]) > meta.version:
                    old.unlink(missing_ok=True)
            # Documents saved before changes were listed have no entries for their versions.
            known = meta.history[: meta.version]
            known += [{}] * (meta.version - len(known))
            meta.history = known + [entry or {}]
            meta.version += 1
            meta.revision += 1
            meta.last_access = time.time()
            (folder / f"v{meta.version}.pdf").write_bytes(data)
            self._write_meta(meta)
            return meta

    def go_to(self, doc_id: str, expected_revision: int, version: int) -> DocumentMeta:
        """Make a stored version current: an earlier one (undo, start over) or a later
        one that was undone (redo). Nothing is deleted."""
        with self._lock:
            meta = self._read_meta(doc_id)
            if meta.revision != expected_revision:
                raise StaleRevision(doc_id)
            self.version_path(meta, version)
            meta.version = version
            meta.revision += 1
            meta.last_access = time.time()
            self._write_meta(meta)
            return meta

    def undo(self, doc_id: str, expected_revision: int) -> DocumentMeta:
        """Go back to the previous version. The undone version is kept until a new edit."""
        meta = self._read_meta(doc_id)
        if meta.version == 0:
            if meta.revision != expected_revision:
                raise StaleRevision(doc_id)
            raise NothingToUndo(doc_id)
        return self.go_to(doc_id, expected_revision, meta.version - 1)

    def redo(self, doc_id: str, expected_revision: int) -> DocumentMeta:
        """Bring back the change undone last."""
        meta = self._read_meta(doc_id)
        try:
            return self.go_to(doc_id, expected_revision, meta.version + 1)
        except NoSuchVersion:
            if meta.revision != expected_revision:
                raise StaleRevision(doc_id)
            raise

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
