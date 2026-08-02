"""Console entry points must survive a stale editable install.

``pip install -e .`` freezes the project's module list into a generated import
finder. A module added to the repository afterwards is invisible to the already
installed entry point: ``operator`` dies at import time with
``ModuleNotFoundError`` even though the missing file is sitting next to the one
that failed to import it, and the only cure is knowing to reinstall.

``python -I -S`` reproduces that condition faithfully — no site-packages means
no editable finder, so a sibling module is reachable only if the module being
run puts its own directory on ``sys.path``.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ENTRY_POINTS = ["copilot_operator.py", "handoff_tool.py", "operator_runner.py"]


def _run_isolated(code: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", "-S", "-c", code],
                          cwd=str(cwd), capture_output=True,
                          encoding="utf-8", errors="replace")


@pytest.mark.parametrize("module", ENTRY_POINTS)
def test_entry_point_imports_without_the_editable_finder(module, tmp_path):
    proc = _run_isolated(
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('probe', r'{REPO / module}')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "print('imported')\n",
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "imported" in proc.stdout


def test_the_isolation_really_does_hide_sibling_modules(tmp_path):
    """Guard the guard: if ``-I -S`` ever stopped hiding siblings, the tests
    above would keep passing for entirely the wrong reason."""
    proc = _run_isolated("import project_paths", tmp_path)
    assert proc.returncode != 0
    assert "No module named 'project_paths'" in proc.stderr
