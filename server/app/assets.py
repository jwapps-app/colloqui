"""The web client's build id, and the one place it is stamped in.

Every static asset is served under /assets/<build>/..., where <build> is a
short hash of the static files themselves. A new release (or an edited file in
development) is therefore a new URL, so assets can be cached forever and a
client can tell it is stale by comparing its build with /healthz. Nothing in
the client or the HTML carries a hand-maintained version number: index.html
has a __BUILD__ placeholder and relative module imports inherit the path.
"""
import hashlib
import time
from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"
PLACEHOLDER = "__BUILD__"

# The directory is re-examined at most this often. In production the files
# never change; in development (source mounted into the container) an edit
# shows up as a new build within a second, with no restart.
_RECHECK_SECONDS = 1.0

_checked_at = 0.0
_signature: tuple = ()
_build = ""


def _files() -> list[Path]:
    return sorted(p for p in STATIC_DIR.rglob("*") if p.is_file())


def build_id() -> str:
    """Short content hash of the static directory."""
    global _checked_at, _signature, _build
    now = time.monotonic()
    if _build and now - _checked_at < _RECHECK_SECONDS:
        return _build
    _checked_at = now
    files = _files()
    signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    if signature != _signature or not _build:
        digest = hashlib.sha256()
        for p in files:
            digest.update(p.relative_to(STATIC_DIR).as_posix().encode())
            digest.update(b"\0")
            digest.update(p.read_bytes())
        _signature, _build = signature, digest.hexdigest()[:12]
    return _build


def asset_prefix() -> str:
    """URL prefix of the current build's assets, with a trailing slash."""
    return f"/assets/{build_id()}/"


def index_html() -> str:
    """index.html with the current build stamped in."""
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace(
        PLACEHOLDER, build_id()
    )
