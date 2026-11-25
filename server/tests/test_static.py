"""Static guards for the web client. The black-screen outage came from a
top-level `$('id').onclick = ...` referencing an element removed from the
HTML — that throws at load and blanks the whole app. This catches that class
of bug without a headless browser. (The modules' own behaviour is tested under
Node, see tests/js/.)"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def _read(name):
    return (STATIC / name).read_text()


def _client_sources() -> dict[str, str]:
    """Every first-party script: the entry file and the modules under js/."""
    files = [STATIC / "app.js", *sorted((STATIC / "js").glob("*.js"))]
    return {str(f.relative_to(STATIC)): f.read_text() for f in files}


def test_wired_element_ids_exist():
    html_ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', _read("index.html")))
    # IDs that get an event handler attached must exist in the HTML.
    missing = {}
    for name, js in _client_sources().items():
        wired = set(re.findall(
            r"\$\('([A-Za-z0-9_-]+)'\)\.(?:onclick|onsubmit|oninput|onchange|onscroll"
            r"|addEventListener)\b",
            js,
        ))
        if wired - html_ids:
            missing[name] = sorted(wired - html_ids)
    assert not missing, f"handlers wired to element ids missing from index.html: {missing}"


def test_no_hand_written_versions():
    # Assets are versioned by the server (app/assets.py): index.html references
    # them under /assets/__BUILD__/ and module imports are relative. A literal
    # `?v=` or a version constant means a number someone has to remember to bump.
    html = _read("index.html")
    assert "__BUILD__" in html
    assert re.search(r'<meta name="build" content="__BUILD__">', html)
    assert re.search(r'<script type="module" src="/assets/__BUILD__/app\.js">', html)
    offenders = [
        name for name, text in {**_client_sources(), "index.html": html}.items()
        if re.search(r"\?v=\d", text) or "APP_VERSION" in text
    ]
    assert not offenders, f"hand-written asset version in: {offenders}"


def test_local_assets_are_referenced_through_the_build_path():
    # A script, stylesheet or icon referenced at a bare path would bypass the
    # versioned URL and be served stale from caches after a release.
    html = _read("index.html")
    refs = re.findall(r'(?:src|href)="(/[^"]+\.(?:js|mjs|css|png))"', html)
    bare = [r for r in refs if not r.startswith("/assets/__BUILD__/")]
    assert not bare, f"unversioned asset reference(s) in index.html: {bare}"


def test_modules_import_only_relative_paths():
    # Relative imports inherit /assets/<build>/ from the importing module; an
    # absolute one ("/js/x.js") would load a second, unversioned copy.
    bad = {}
    for name, js in _client_sources().items():
        specs = re.findall(r"""(?:from|import)\s+['"]([^'"]+)['"]""", js)
        wrong = [s for s in specs if not s.startswith(("./", "../"))]
        if wrong:
            bad[name] = wrong
    assert not bad, f"non-relative module imports: {bad}"


def test_no_native_dialogs():
    # We replaced native alert/confirm/prompt with themed dialogs; a stray one
    # would look out of place. (appAlert/appConfirm/appPrompt are fine.)
    stray = {}
    for name, js in _client_sources().items():
        found = re.findall(r"(?<![A-Za-z.])(?:window\.)?(alert|confirm|prompt)\s*\(", js)
        if found:
            stray[name] = sorted(set(found))
    assert not stray, f"native dialog call(s) snuck back in: {stray}"
