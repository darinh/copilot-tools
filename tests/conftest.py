"""Shared fixtures for the copilot-tools test suite."""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import sys
import warnings
from contextlib import contextmanager
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

# The user's real project catalog, watched by CONTENT rather than by name.
#
# The directory guard above compares the set of names before and after a test,
# which cannot see a file that is overwritten in place: the name is there both
# times. That is precisely how this file was destroyed. A test suite rewrote
# the real ~/.copilot/projects/catalog.csv with a single fixture row, six real
# project registrations were lost, and nothing failed. It surfaced only because
# `handoff` refused to start minutes later, by which point nothing connected the
# two events.
#
# It is watched even though the enclosing directory is not, and the reason the
# directory is excluded does not fully apply here. That reason is concurrency:
# peer agents write handoff files under ~/.copilot/projects constantly, so
# blaming the running test for a new name there would be a fabricated
# accusation. Writes to the catalog itself are rare by comparison -- no
# production code writes it at all; handoff_tool and copilot_operator only read
# it -- so the false-positive rate is low enough to be worth the cover.
#
# Low is not zero, and it is worth being exact about that, because the first
# draft of this guard restored the old bytes on the strength of "nothing but a
# test writes this file". That premise was falsified within the hour: an agent
# appended two recovered rows BY HAND while this was being written. Registration
# is a manual act and it is not announced. So the guard reports and preserves;
# it never puts anything back. See _catalog_complaint.
_REAL_CATALOG = Path.home() / ".copilot" / "projects" / "catalog.csv"

# Distinct from None, which means "checked, and the file is not there". A read
# that fails establishes nothing at all, and the two must not be conflated:
# install_manifest already spells this rule as "a destination that cannot be
# examined is UNREADABLE, never ABSENT".
_UNREADABLE = object()


def _catalog_state():
    """The catalog's exact bytes, ``None`` if absent, ``_UNREADABLE`` if unknown."""
    try:
        return _REAL_CATALOG.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return _UNREADABLE


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


def _bank(before: bytes) -> Path | None:
    """Copy the pre-test bytes beside the catalog, never overwriting anything."""
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    for n in range(20):
        suffix = f".pre-test-{stamp}" + (f"-{n}" if n else "")
        dest = _REAL_CATALOG.with_name(_REAL_CATALOG.name + suffix)
        try:
            # O_EXCL: a banked copy that overwrites a banked copy is a
            # preserver that destroys.
            with open(dest, "xb") as fh:
                fh.write(before)
            return dest
        except FileExistsError:
            continue
        except OSError:
            return None
    return None


def _catalog_complaint(before, nodeid: str) -> str | None:
    """Bank the catalog's pre-test bytes if this test changed it, and say so.

    Returns ``None`` when nothing changed or when nothing could be established.

    It deliberately does NOT put the old contents back. Restoring assumes the
    new contents are the test's doing, and this file has exactly one other
    writer: a human or an agent registering a project, which can land at any
    moment and is not announced. Overwriting it to undo a suspected clobber
    would destroy a legitimate registration on a guess -- a preserver that
    destroys, which is the failure this whole guard exists to catch. So it
    copies the old bytes to a fresh name, touches nothing that already exists,
    and hands the decision to someone who can tell the two apart.
    """
    if before is _UNREADABLE:
        # Nothing was established going in, so nothing can be concluded coming
        # out. Staying silent beats inventing a verdict from an unknown.
        return None
    after = _catalog_state()
    if after is _UNREADABLE:
        # Symmetrically: the file could not be read on the way out, so nothing
        # about it has been established either. Falling through would compare
        # the sentinel against real bytes, find them unequal, and convict the
        # running test of a change nobody observed -- a sharing violation from
        # a peer's open handle is enough to do it.
        return None
    if after == before:
        return None
    banked = _bank(before) if before is not None else None
    where = (f"Its pre-test contents are banked at {banked}\n" if banked
             else "")
    # The bytes go in the message too: a bank can fail, and a report naming a
    # file whose contents nobody recorded is a report of an unrecoverable loss.
    original = "(did not exist)" if before is None else before.decode(
        "utf-8", errors="replace")
    return (
        f"{nodeid} modified the REAL project catalog at {_REAL_CATALOG}.\n"
        f"{where}"
        f"Its contents before this test were:\n{original}\n"
        "Nothing has been overwritten -- put it back by hand once you have "
        "checked that another agent did not legitimately register a project "
        "mid-run.\n"
        "A test must never touch the user's home. Point the code under test "
        "at a temporary root -- monkeypatch handoff_tool.CATALOG, or patch "
        "Path.home -- and remember that a subprocess inherits neither."
    )


@pytest.fixture(autouse=True)
def _real_catalog_is_never_rewritten(request: pytest.FixtureRequest):
    """Fail the test that changes the user's real catalog, and bank the old bytes.

    It does not put the file back. See _catalog_complaint for why not.
    """
    before = _catalog_state()
    yield
    complaint = _catalog_complaint(before, request.node.nodeid)
    if complaint:
        raise AssertionError(complaint)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "restart"
    d.mkdir(parents=True)
    return d


@contextmanager
def denied(monkeypatch, *paths, limit: int | None = None, counter=None):
    """Make every stat of ``paths`` raise EACCES, as a revoked directory does.

    Three call sites are patched, not one. ``Path.exists()`` reaches the
    filesystem through ``os.stat`` -- and on 3.10 through a private pathlib
    accessor that copies ``os.stat`` at import time -- while the tri-state
    probes in ``copilot_operator`` call ``os.lstat`` directly. Deny only
    ``os.lstat`` and code that still uses ``exists()`` sails through, so the
    test grades nothing; deny only ``os.stat`` and the probes never see the
    failure they exist to handle; miss the accessor and the whole thing is
    vacuous on 3.10.

    ``limit`` denies only the first N probes, which is how a transient failure
    (a scanner holding a file open on Windows) actually behaves: the next poll
    succeeds.
    """
    targets = {str(Path(p)) for p in paths}
    seen = counter if counter is not None else {"n": 0}
    real_stat, real_lstat = os.stat, os.lstat

    def guard(real):
        def probe(path, *args, **kwargs):
            try:
                key = str(Path(path))
            except TypeError:
                key = None
            if key in targets and (limit is None or seen["n"] < limit):
                seen["n"] += 1
                raise PermissionError(13, "Permission denied")
            return real(path, *args, **kwargs)
        return probe

    monkeypatch.setattr(os, "stat", guard(real_stat))
    monkeypatch.setattr(os, "lstat", guard(real_lstat))
    accessor = getattr(pathlib, "_NormalAccessor", None)
    saved = {}
    if accessor is not None:
        for name, real in (("stat", real_stat), ("lstat", real_lstat)):
            if hasattr(accessor, name):
                saved[name] = getattr(accessor, name)
                setattr(accessor, name, staticmethod(guard(real)))
    try:
        yield seen
    finally:
        for name, original in saved.items():
            setattr(accessor, name, original)
        monkeypatch.setattr(os, "stat", real_stat)
        monkeypatch.setattr(os, "lstat", real_lstat)


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
