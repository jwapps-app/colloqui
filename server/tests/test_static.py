"""Static guards for the web client. The black-screen outage came from a
top-level `$('id').onclick = ...` referencing an element removed from the
HTML — that throws at load and blanks the whole app. This catches that class
of bug without a headless browser."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def _read(name):
    return (STATIC / name).read_text()


def test_wired_element_ids_exist():
    html = _read("index.html")
    js = _read("app.js")
    html_ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
    # IDs that get an event handler attached at module load must exist in HTML.
    wired = set(re.findall(
        r"\$\('([A-Za-z0-9_-]+)'\)\.(?:onclick|onsubmit|oninput|onchange|onscroll)\s*=",
        js,
    ))
    missing = sorted(wired - html_ids)
    assert not missing, f"app.js wires handlers to missing element id(s): {missing}"


def test_asset_versions_match():
    # style.css and app.js must share the same ?v= cache-bust number, or a
    # deploy ships new JS against stale CSS (or vice-versa).
    html = _read("index.html")
    css_v = re.search(r"style\.css\?v=(\d+)", html)
    js_v = re.search(r"app\.js\?v=(\d+)", html)
    assert css_v and js_v, "missing versioned asset references"
    assert css_v.group(1) == js_v.group(1), (
        f"asset version mismatch: style.css v{css_v.group(1)} vs app.js v{js_v.group(1)}"
    )


def test_no_native_dialogs():
    # We replaced native alert/confirm/prompt with themed dialogs; a stray one
    # would look out of place. (appAlert/appConfirm/appPrompt are fine.)
    js = _read("app.js")
    stray = re.findall(r"(?<![A-Za-z])(?:window\.)?(alert|confirm|prompt)\s*\(", js)
    assert not stray, f"native dialog call(s) snuck back in: {set(stray)}"
