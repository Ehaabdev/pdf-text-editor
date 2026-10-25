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
MAX_RENDER_WIDTH = 3200
MAX_RENDER_PIXELS = 20_000_000

# Limits that keep a small public server usable (limits.py, storage.py).
RATE_UPLOADS_PER_HOUR = int(os.environ.get("PDFEDIT_RATE_UPLOADS_PER_HOUR", "30"))
RATE_CHANGES_PER_HOUR = int(os.environ.get("PDFEDIT_RATE_CHANGES_PER_HOUR", "300"))
RATE_READS_PER_MINUTE = int(os.environ.get("PDFEDIT_RATE_READS_PER_MINUTE", "900"))
RATE_EXPORTS_PER_HOUR = int(os.environ.get("PDFEDIT_RATE_EXPORTS_PER_HOUR", "60"))
HEAVY_JOBS = int(os.environ.get("PDFEDIT_HEAVY_JOBS", "2"))  # changes and uploads at once
MAX_VERSIONS = int(os.environ.get("PDFEDIT_MAX_VERSIONS", "60"))  # stored changes per file
MAX_DOC_BYTES = int(os.environ.get("PDFEDIT_MAX_DOC_MB", "300")) * 1024 * 1024  # all its versions
MAX_TOTAL_BYTES = int(os.environ.get("PDFEDIT_MAX_TOTAL_MB", "4000")) * 1024 * 1024  # every file stored

# Cached data that can be rebuilt (font index).
CACHE_DIR = Path(os.environ.get("PDFEDIT_CACHE_DIR", BASE_DIR / ".cache"))
