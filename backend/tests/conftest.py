import os
import sys
import tempfile
from pathlib import Path

import pymupdf
import pytest

# Point the app at a throwaway data folder before it is imported.
os.environ["PDFEDIT_DATA_DIR"] = tempfile.mkdtemp(prefix="pdfedit-test-")
os.environ["PDFEDIT_MAX_PAGES"] = "5"
os.environ["PDFEDIT_FRONTEND_DIST"] = str(Path(tempfile.gettempdir()) / "pdfedit-no-frontend")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def make_pdf(pages: int = 1, rotate: int = 0, cropbox=None, password: str | None = None) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"Page {i + 1}", fontsize=24)
        if cropbox:
            page.set_cropbox(pymupdf.Rect(cropbox))
        if rotate:
            page.set_rotation(rotate)
    if password:
        return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password)
    return doc.tobytes()


def upload(client, data: bytes, name: str = "test.pdf"):
    return client.post("/api/documents", files={"file": (name, data, "application/pdf")})
