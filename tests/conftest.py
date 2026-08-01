"""Shared fixtures for the copilot-tools test suite."""
from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Directories a test must never write into. The repo root and skills/ are
# tracked, so an artifact dropped there gets swept up by `git add -A`.
# Each entry is (directory, recursive). The repo root is scanned top-level
# only -- recursing it would walk .git, docs/ and sibling worktrees on every
# test -- while skills/ is small, tracked, and has actually been polluted, so
# it is walked in full.
_GUARDED_DIRS: tuple[tuple[Path, bool], ...] = (
    (ROOT, False),
    (ROOT / "skills", True),
)

# The user's real projects directory is deliberately NOT guarded by default.
# Peer agents and the operator itself legitimately write handoff files there
# (see handoff_tool.write_handoff), so a concurrent write would blame whichever
# test happened to be running -- manufacturing exactly the misattribution this
# guard exists to prevent. Opt in only for a run with no other live agents.
if os.environ.get("COPILOT_TOOLS_GUARD_HOME") == "1":
    _GUARDED_DIRS += ((Path.home() / ".copilot" / "projects", True),)

# Churn that is not a test artifact: tooling caches and developer worktrees.
_GUARD_IGNORED = frozenset({".git", ".pytest_cache", "__pycache__", ".worktrees"})


def _snapshot_guarded() -> dict[Path, tuple[bool, frozenset[str]]]:
    """Record whether each guarded directory exists and what it contains.

    Existence is tracked separately from contents so that a test which creates
    a guarded directory and leaves it empty is still caught -- an empty new
    directory and a missing one are not the same thing.
    """
    snapshot: dict[Path, tuple[bool, frozenset[str]]] = {}
    for directory, recursive in _GUARDED_DIRS:
        if not directory.is_dir():
            snapshot[directory] = (False, frozenset())
            continue
        try:
            snapshot[directory] = (True, frozenset(_entries(directory, recursive)))
        except OSError as exc:
            # An unreadable directory must not take the whole suite down, but a
            # blind spot that nobody is told about is how leaks survive in the
            # first place. Degrade to "exists, contents unknown" and say so.
            warnings.warn(
                f"artifact guard could not read {directory}: {exc}. "
                "Strays under it will not be detected for this test.",
                stacklevel=2,
            )
            snapshot[directory] = (True, frozenset())
    return snapshot


def _entries(directory: Path, recursive: bool) -> set[str]:
    """Names directly under ``directory``, or every path beneath it."""
    if not recursive:
        return {e.name for e in os.scandir(directory)} - _GUARD_IGNORED

    def _reraise(exc: OSError) -> None:
        raise exc

    found: set[str] = set()
    # os.walk swallows traversal errors by default, which would silently skip
    # an unreadable subtree and report it as clean.
    for parent, dirnames, filenames in os.walk(directory, onerror=_reraise):
        dirnames[:] = [d for d in dirnames if d not in _GUARD_IGNORED]
        base = Path(parent)
        for name in dirnames + [f for f in filenames if f not in _GUARD_IGNORED]:
            found.add((base / name).relative_to(directory).as_posix())
    return found


def _find_strays(
    before: dict[Path, tuple[bool, frozenset[str]]],
    after: dict[Path, tuple[bool, frozenset[str]]],
) -> list[str]:
    """Return paths present in ``after`` that were absent from ``before``."""
    strays: set[str] = set()
    for directory, (exists, names) in after.items():
        existed, seen = before.get(directory, (False, frozenset()))
        if exists and not existed:
            strays.add(str(directory))
        strays.update(str(directory / name) for name in names - seen)
    return sorted(strays)


def _stray_report(nodeid: str, strays: list[str]) -> str:
    listed = "\n  ".join(strays)
    return (
        f"{nodeid} left files outside tmp_path:\n  {listed}\n"
        "Tests must write only into tmp_path. Point the code under test at "
        "an injected root (monkeypatch.chdir(tmp_path) or an explicit path "
        "argument) instead of letting it resolve against the current "
        "working directory."
    )


@pytest.fixture(autouse=True)
def _no_stray_artifacts(request: pytest.FixtureRequest):
    """Fail the test that leaves files behind, naming it and the artifact.

    Attribution is the whole point: an artifact discovered later cannot be
    traced back to a producer, which is how stray files survive cleanup and
    reappear. Failing here pins the leak to a nodeid at the moment it happens.
    """
    before = _snapshot_guarded()
    yield
    strays = _find_strays(before, _snapshot_guarded())
    if strays:
        raise AssertionError(_stray_report(request.node.nodeid, strays))


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "restart"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "metrics.db"


def make_log(
    path: Path,
    *,
    premium_calls=((("claude-opus-4.6", 3.0),) * 1),
    lines_added: int = 10,
    lines_removed: int = 2,
    cwd: str = "/home/dev/project",
    extra_text: str = "",
) -> Path:
    """Write a synthetic Copilot process log containing a shutdown event."""
    header = (
        '2026-07-27T10:00:00.000Z [info] starting\n'
        f'2026-07-27T10:00:00.100Z [info] {{"cwd": "{cwd}"}}\n'
    )
    usage_blocks = []
    for model, cost in premium_calls:
        usage_blocks.append(
            '2026-07-27T10:01:00.000Z [telemetry] {\n'
            '  "kind": "assistant_usage",\n'
            f'  "model": "{model}",\n'
            f'  "cost": {cost}\n'
            '}\n'
        )
    shutdown = (
        '2026-07-27T10:30:00.000Z [telemetry] {\n'
        '  "kind": "session_shutdown",\n'
        '  "properties": {\n'
        '    "model_claude-opus-4.6_input_tokens": "1500000",\n'
        '    "model_claude-opus-4.6_output_tokens": "24000",\n'
        '    "model_claude-opus-4.6_cache_read_tokens": "900",\n'
        '    "model_claude-opus-4.6_request_count": "7"\n'
        '  },\n'
        '  "metrics": {\n'
        '    "total_premium_requests": 1,\n'
        '    "total_api_duration_ms": 120000,\n'
        '    "session_duration_ms": 1800000,\n'
        f'    "lines_added": {lines_added},\n'
        f'    "lines_removed": {lines_removed}\n'
        '  }\n'
        '}\n'
    )
    footer = '2026-07-27T10:30:05.000Z [info] done\n'
    path.write_text(
        header + extra_text + "".join(usage_blocks) + shutdown + footer,
        encoding="utf-8",
    )
    return path


@pytest.fixture
def launch_spec(tmp_path: Path, state_dir: Path, db_path: Path):
    def _make(argv, session_num=1, log_dir=None):
        spec = {
            "instance": "testinst",
            "argv": list(argv),
            "cwd": str(tmp_path),
            "session_num": session_num,
            "state_dir": str(state_dir),
            "metrics_db": str(db_path),
            "copilot_log_dir": str(log_dir or (tmp_path / "logs")),
        }
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    return _make
