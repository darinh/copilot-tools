"""The runtime extensions are code, and until now nothing checked them.

`extensions/` ships six JavaScript files that run inside every Copilot session
on the machine, and no CI job ever loaded one. All six could have been
syntactically invalid and all seven jobs would still have gone green, because
the pipeline is Python and shell only.

These tests close that gap from the Python suite, which is the one thing that
always runs. They skip rather than fail when node is unavailable, since node is
not a dependency of this package -- but the CI workflow runs them on a runner
where node is present, so the skip is a local-convenience path and not a way
for a broken extension to reach main unnoticed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTENSIONS_DIR = REPO_ROOT / "extensions"

node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _extension_dirs() -> list[Path]:
    return sorted(p for p in EXTENSIONS_DIR.iterdir()
                  if p.is_dir() and (p / "extension.mjs").is_file())


def _mjs_files() -> list[Path]:
    return sorted(EXTENSIONS_DIR.rglob("*.mjs"))


def test_extension_sources_are_discovered():
    """Guards the two collections below against silently going empty.

    Every other test here iterates one of them, and pytest passes an empty
    parametrisation without complaint -- so a wrong path would turn this whole
    module into a no-op that reports success.
    """
    dirs = _extension_dirs()
    files = _mjs_files()
    assert len(dirs) >= 6, f"expected the shipped extensions, found {dirs}"
    assert len(files) >= len(dirs)
    assert (EXTENSIONS_DIR / "checkout-guard" / "extension.mjs").is_file()


@node
@pytest.mark.parametrize("path", _mjs_files(), ids=lambda p: str(p.relative_to(EXTENSIONS_DIR)))
def test_extension_parses(path: Path):
    """`node --check` on every shipped .mjs file."""
    proc = subprocess.run(["node", "--check", str(path)],
                          capture_output=True, encoding="utf-8",
                          errors="replace", timeout=60)
    assert proc.returncode == 0, f"{path.name} does not parse:\n{proc.stderr}"


@node
def test_check_rejects_broken_javascript(tmp_path: Path):
    """Negative control for the test above.

    `node --check` exiting 0 on six files proves nothing unless it is known to
    exit non-zero on something. Without this, a `node` shim that always
    succeeded would leave the parse test permanently, invisibly green.
    """
    broken = tmp_path / "broken.mjs"
    broken.write_text("function ( { unclosed\n", encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(broken)],
                          capture_output=True, encoding="utf-8",
                          errors="replace", timeout=60)
    assert proc.returncode != 0


@node
def test_checkout_guard_unit_tests_pass():
    """Run the guard's own `node --test` suite from the Python suite.

    The guard decides whether a `git add` is allowed to proceed, so its logic
    needs real coverage rather than a syntax check. Running it from here means
    one command -- `python -m pytest` -- still exercises everything.
    """
    suite = EXTENSIONS_DIR / "checkout-guard" / "guard.test.mjs"
    assert suite.is_file()
    proc = subprocess.run(
        ["node", "--test", "--test-reporter=tap", str(suite)],
        capture_output=True, encoding="utf-8", errors="replace",
        timeout=300, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, f"guard tests failed:\n{proc.stdout}\n{proc.stderr}"
    assert "# fail 0" in proc.stdout
    # A suite that ran zero tests also reports zero failures.
    assert "# pass 0" not in proc.stdout, "the guard suite ran no tests"


@node
def test_checkout_guard_leaves_no_artifacts_in_the_checkout():
    """The litter guard must not litter.

    Its integration tests create real git repositories, and a test suite for
    this particular feature leaving a directory behind would be its own
    counterexample. The autouse fixture in conftest.py covers the repo root and
    skills/; this asserts it specifically for the subprocess, which runs
    outside the fixture's reach because it is a different process.
    """
    suite = EXTENSIONS_DIR / "checkout-guard" / "guard.test.mjs"
    before = {p.name for p in REPO_ROOT.iterdir()}
    subprocess.run(["node", "--test", str(suite)],
                   capture_output=True, encoding="utf-8", errors="replace",
                   timeout=300, cwd=str(REPO_ROOT))
    after = {p.name for p in REPO_ROOT.iterdir()}
    assert after - before == set()


def test_every_extension_is_documented():
    """A globbed install directory makes an undocumented extension invisible.

    `setup_tools._extension_sources` installs every subdirectory of
    `extensions/`, so a new one is deployed to every session on the machine
    whether or not anyone wrote it down.
    """
    readme = (EXTENSIONS_DIR / "README.md").read_text(encoding="utf-8")
    for src in _extension_dirs():
        assert f"`{src.name}`" in readme, f"extensions/README.md does not mention {src.name}"


def test_checkout_guard_knob_is_documented():
    """The disable switch has to be findable by whoever it is blocking."""
    readme = (EXTENSIONS_DIR / "README.md").read_text(encoding="utf-8")
    guard = (EXTENSIONS_DIR / "checkout-guard" / "extension.mjs").read_text(encoding="utf-8")
    assert "COPILOT_CHECKOUT_GUARD_DISABLE" in guard
    assert "COPILOT_CHECKOUT_GUARD_DISABLE" in readme


def test_ci_runs_the_extension_checks():
    """A gate nobody runs is documentation.

    These tests are only worth writing if the pipeline executes them, and the
    Python matrix runners have node preinstalled.
    """
    workflow = (REPO_ROOT / ".github" / "workflows").glob("*.yml")
    text = "\n".join(p.read_text(encoding="utf-8") for p in workflow)
    assert "tests/test_extensions.py" in text or "node --test" in text


def test_setup_deploys_the_new_extension():
    """`deployed_artifacts` is what the install manifest tracks."""
    import setup_tools

    keys = [key for key, kind, _src, _dest in setup_tools.deployed_artifacts()
            if kind == "extension"]
    assert "extensions/checkout-guard" in keys, json.dumps(keys)
