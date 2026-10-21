import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("PDFEDIT_DATA_DIR", BASE_DIR / "data"))
FRONTEND_DIST = Path(os.environ.get("PDFEDIT_FRONTEND_DIST", BASE_DIR.parent / "frontend" / "dist"))

MAX_UPLOAD_BYTES = int(os.environ.get("PDFEDIT_MAX_UPLOAD_MB", "20")) * 1024 * 1024
MAX_PAGES = int(os.environ.get("PDFEDIT_MAX_PAGES", "50"))

# Documents are deleted this long after they were last used.
DOC_TTL_SECONDS = int(os.environ.get("PDFEDIT_DOC_TTL_MINUTES", "60")) * 60
CLEANUP_INTERVAL_SECONDS = 300

# Page image rendering limits.
MIN_RENDER_WIDTH = 200
MAX_RENDER_WIDTH = 2400
MAX_RENDER_PIXELS = 12_000_000
