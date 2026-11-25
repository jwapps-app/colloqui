"""The web client's own tests run under Node (no browser, no npm packages):
every module must parse, and the pure modules (formatting, the task grammar,
the message renderer, the DOM helpers) have behaviour tests in tests/js/.

Running them from here keeps `pytest` the one command that checks everything.
Without Node installed they are skipped locally; CI sets REQUIRE_NODE=1 so a
missing Node there is a failure, not a silent skip."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent
STATIC = SERVER / "app" / "static"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None and not os.environ.get("REQUIRE_NODE"),
    reason="Node.js is not installed",
)


def _node(*args: str) -> subprocess.CompletedProcess:
    assert NODE, "Node.js is required (REQUIRE_NODE is set) but was not found"
    return subprocess.run(
        [NODE, *args], cwd=SERVER, capture_output=True, text=True, timeout=120
    )


def test_every_client_script_parses():
    scripts = [STATIC / "app.js", STATIC / "sw.js", STATIC / "theme.js",
               *sorted((STATIC / "js").glob("*.js"))]
    assert len(scripts) > 20
    for script in scripts:
        r = _node("--check", str(script))
        assert r.returncode == 0, f"{script.name} does not parse:\n{r.stderr}"


def test_client_unit_tests_pass():
    tests = sorted(str(p) for p in (SERVER / "tests" / "js").glob("*.test.mjs"))
    assert tests
    r = _node("--test", *tests)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
