"""Shared fixtures for the copilot-tools test suite."""
from __future__ import annotations

import datetime
import json
import ntpath
import os
import pathlib
import sys
import time
import warnings
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Imported after ROOT joins sys.path -- these live at the repo root.
import copilot_operator  # noqa: E402
import operator_mux  # noqa: E402

# Directories a test must never write into. The repo root and skills/ are
# tracked, so an artifact dropped there gets swept up by `git add -A`.
# Each entry is (directory, recursive, fatal). The repo root is scanned
# top-level only -- recursing it would walk .git, docs/ and sibling worktrees
# on every test -- while skills/ is small, tracked, and has actually been
# polluted, so it is walked in full.
#
# ``fatal`` says what a hit DOES, which is a separate question from what the
# guard can SEE. The repo root is shared: peer agents, reviewer subagents and
# anything not run in a worktree write into it while a suite is running, so a
# stray there is not evidence that the running test produced it. Failing on it
# accuses the innocent, and it does so intermittently, which reads as a flaky
# integration test rather than a guard problem -- so the guard gets switched
# off, and a disabled detector and a blind one produce the same silence except
# that the disabled one took a real finding with it. The root is therefore
# reported and not failed. It is still reported, because a stray there gets
# swept into somebody else's `git add -A` no matter who made it.
#
# skills/ is not shared in that way, so a hit there is still an error.
_GUARDED_DIRS: tuple[tuple[Path, bool, bool], ...] = (
    (ROOT, False, False),
    (ROOT / "skills", True, True),
)

# The user's real projects directory is deliberately NOT guarded by default.
# Peer agents and the operator itself legitimately write handoff files there
# (see handoff_tool.write_handoff), so a concurrent write would blame whichever
# test happened to be running -- manufacturing exactly the misattribution this
# guard exists to prevent. Opt in only for a run with no other live agents,
# which is also why opting in makes it fatal.
if os.environ.get("COPILOT_TOOLS_GUARD_HOME") == "1":
    _GUARDED_DIRS += ((Path.home() / ".copilot" / "projects", True, True),)

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


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_multiplexer: drives an actual tmux/psmux server rather than the "
        "in-memory FakeMux. Exempt from _no_real_multiplexer.",
    )


def _catalog_state():
    """The catalog's exact bytes, ``None`` if absent, ``_UNREADABLE`` if unknown."""
    try:
        return _REAL_CATALOG.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return _UNREADABLE


def _guarded_dirs() -> tuple[tuple[Path, bool, bool], ...]:
    """``_GUARDED_DIRS`` with duplicate directories merged.

    The snapshot is keyed by directory, so a repeated entry would otherwise be
    last-wins: listing a path twice, once recursive and once not, would leave
    the shallower scan in place and hide everything nested under it, reporting
    nothing at all. ``recursive`` and ``fatal`` are therefore OR-ed. A
    duplicate must never be able to reduce what the guard sees or how loudly
    it objects -- the only safe direction for a misconfiguration to fail is
    toward more visibility.
    """
    merged: dict[Path, tuple[bool, bool]] = {}
    for directory, recursive, fatal in _GUARDED_DIRS:
        was_recursive, was_fatal = merged.get(directory, (False, False))
        merged[directory] = (was_recursive or recursive, was_fatal or fatal)
    return tuple((d, recursive, fatal) for d, (recursive, fatal) in merged.items())


def _snapshot_guarded() -> dict[Path, tuple[bool, frozenset[str]]]:
    """Record whether each guarded directory exists and what it contains.

    Existence is tracked separately from contents so that a test which creates
    a guarded directory and leaves it empty is still caught -- an empty new
    directory and a missing one are not the same thing.
    """
    snapshot: dict[Path, tuple[bool, frozenset[str]]] = {}
    for directory, recursive, _fatal in _guarded_dirs():
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


def _find_strays_by_dir(
    before: dict[Path, tuple[bool, frozenset[str]]],
    after: dict[Path, tuple[bool, frozenset[str]]],
) -> dict[Path, list[str]]:
    """Strays keyed by the guarded directory they were found under.

    Keying by the guarded root rather than by path prefix keeps the severity
    lookup exact: skills/ lives inside the repo root, so a prefix match would
    have to guess which of two guarded directories a hit belongs to.
    """
    found: dict[Path, list[str]] = {}
    for directory, (exists, names) in after.items():
        existed, seen = before.get(directory, (False, frozenset()))
        strays: set[str] = set()
        if exists and not existed:
            strays.add(str(directory))
        strays.update(str(directory / name) for name in names - seen)
        if strays:
            found[directory] = sorted(strays)
    return found


def _find_strays(
    before: dict[Path, tuple[bool, frozenset[str]]],
    after: dict[Path, tuple[bool, frozenset[str]]],
) -> list[str]:
    """Return paths present in ``after`` that were absent from ``before``."""
    flat: set[str] = set()
    for strays in _find_strays_by_dir(before, after).values():
        flat.update(strays)
    return sorted(flat)


# Filesystem timestamps are recorded but never used to suppress a report.
# The before/after name diff already establishes that a path appeared during
# this test; mtime cannot strengthen that and can only subtract from it, since
# st_mtime is not a creation time -- shutil.copy2, os.utime, archive
# extraction, some git flows and NTFS timestamp tunnelling all produce a file
# that is genuinely new to the directory and carries an old mtime. Filtering on
# it would drop those silently, which is the one failure a detector may not
# have. So it is annotation: it tells the reader whether the artifact looks
# fresh, and decides nothing.
_MTIME_SLACK_SECONDS = 2.0


def _appeared_since(path: str, since: float) -> bool | None:
    """Whether ``path`` was last written after ``since``. None means unknown.

    ``lstat``, not ``stat``: a dangling symlink dropped into a guarded
    directory is exactly the kind of artifact worth naming, and ``stat`` would
    raise on it and report the mtime as unreadable.

    Three-valued because an mtime that cannot be read is not an old mtime.
    The slack absorbs coarse filesystem timestamps (FAT rounds to two
    seconds) so that a fresh file is not described as old.

    ``ValueError`` is caught alongside ``OSError`` because ``os.lstat``
    raises it for an embedded NUL (measured, not assumed -- a long path and a
    surrogate both give OSError, only NUL gives ValueError). No name from
    ``os.scandir`` can contain one, so this is unreachable from a real
    filesystem walk and reachable only from a malformed guarded-directory
    setting. It is caught anyway on cost: an exception escaping teardown
    fails an unrelated test, which is precisely the misattribution this
    module was changed to stop doing.
    """
    try:
        return os.lstat(path).st_mtime >= since - _MTIME_SLACK_SECONDS
    except (OSError, ValueError):
        return None


def _describe(path: str, recent: bool | None) -> str:
    """One reported line: the path, and what its mtime says about the window."""
    if recent is None:
        return f"{path} (mtime unreadable)"
    return f"{path} (mtime {'within' if recent else 'before'} this test)"


def _stray_report(nodeid: str, strays: list[str]) -> str:
    listed = "\n  ".join(strays)
    return (
        f"{nodeid} left files outside tmp_path:\n  {listed}\n"
        "Tests must write only into tmp_path. Point the code under test at "
        "an injected root (monkeypatch.chdir(tmp_path) or an explicit path "
        "argument) instead of letting it resolve against the current "
        "working directory."
    )


def _stray_notice(nodeid: str, described: list[str]) -> str:
    """The advisory wording for a shared directory.

    It states what was observed and not what it concludes. "A file appeared
    while this test was running" is true whoever wrote it; "this test left a
    file" is a guess that is wrong every time a peer agent is working in the
    same checkout, and a report that overstates its own evidence is one the
    reader learns to skip.
    """
    listed = "\n  ".join(described)
    return (
        f"a file appeared in a guarded shared directory while {nodeid} was "
        f"running:\n  {listed}\n"
        "This does not establish that the test wrote it -- the repo root is "
        "shared with peer agents and subagents. It is reported because a "
        "stray there is swept into the next `git add -A` regardless of who "
        "made it. If it was the test, point the code under test at tmp_path."
    )


@pytest.fixture(autouse=True)
def _no_stray_artifacts(request: pytest.FixtureRequest):
    """Report artifacts appearing in guarded directories, naming the test.

    Attribution is the whole point: an artifact discovered later cannot be
    traced back to a producer, which is how stray files survive cleanup and
    reappear. Recording it here pins it to a nodeid at the moment it happens.

    Whether that is a failure or a warning depends on whether the directory
    has other writers -- see ``_GUARDED_DIRS``. Nothing is filtered out: the
    before/after name diff is what establishes that a path appeared during
    this test, and mtime only annotates how fresh it looks. It is never a
    reason to stay quiet, because it does not answer the question the diff
    already answered.
    """
    started = time.time()
    before = _snapshot_guarded()
    yield
    by_dir = _find_strays_by_dir(before, _snapshot_guarded())
    fatal: list[str] = []
    advisory: list[str] = []
    for directory, _recursive, is_fatal in _guarded_dirs():
        found = by_dir.get(directory)
        if not found:
            continue
        if is_fatal:
            fatal.extend(found)
            continue
        advisory.extend(_describe(path, _appeared_since(path, started)) for path in found)
    if advisory:
        warnings.warn(_stray_notice(request.node.nodeid, advisory), stacklevel=2)
    if fatal:
        raise AssertionError(_stray_report(request.node.nodeid, sorted(fatal)))


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


# ── the multiplexer boundary ────────────────────────────────────
#
# `copilot_operator.MUX` is a module-level `Mux()` built at import time, so any
# test that calls into copilot_operator without replacing it drives the
# DEVELOPER'S OWN tmux/psmux server. Measured on 2026-08-01: 30 unit tests did
# exactly that, 134 real `tmux has-session` invocations per suite run, against
# whatever the machine happened to be running at the time.
#
# Two things follow, and the second is the expensive one:
#
#   * The tests are nondeterministic. Their answers depend on a live server, on
#     process-spawn latency and on what sessions exist right now. That is what
#     made test_unexpected_exit_without_marker_is_relaunched fail in a full run
#     and pass 3/3 alone -- five real subprocess spawns per run, none of them
#     visible in the test.
#   * They are also destructive in principle. The loop's stop path calls
#     `MUX.kill_session(instance.session)`, and its session names come from the
#     instance name in the test -- so a developer with a real session named
#     `relaunch-me` or `detach-me` loses it to a unit test.
#
# The double fakes the SUBPROCESS BOUNDARY ONLY: `_run` answers the tmux verbs
# from an in-memory session table and everything above it is the real `Mux`
# code. So new_session still verifies the session exists afterwards,
# kill_session still raises when one survives, and send_keys still splits
# literal text from Enter -- a test double built by reimplementing that surface
# would agree with whatever the implementation assumed rather than with what it
# does. An unrecognised verb raises rather than returning a plausible rc: a
# double that answers a question it does not understand is the failure this
# whole file keeps re-learning.
#
# Tests that want different behaviour still override `op.MUX` themselves; this
# runs first, so their own monkeypatch wins. Real-multiplexer coverage lives in
# test_integration.py, which builds its own `Mux()` and is untouched by this.
class FakeMux(operator_mux.Mux):
    """A `Mux` whose backend is a dict instead of a terminal multiplexer."""

    def __init__(self, sessions: tuple[str, ...] = ()):
        super().__init__(binary="fakemux")
        self.sessions: dict[str, dict] = {
            name: {"cwd": "", "argv": [], "remain_on_exit": False, "dead": False}
            for name in sessions
        }
        self.keys: list[tuple[str, str]] = []

    @property
    def binary(self) -> str:
        return "fakemux"

    def _run(self, *args: str, capture: bool = True) -> tuple[str, str, int]:
        verb = args[0] if args else ""
        if verb == "-V":
            return "fakemux 0.0", "", 0
        if verb == "has-session":
            return "", "", 0 if args[2] in self.sessions else 1
        if verb == "list-sessions":
            return "\n".join(self.sessions), "", 0
        if verb == "new-session":
            name = args[3]
            self.sessions[name] = {
                "cwd": args[5], "argv": list(args[7:]),
                "remain_on_exit": False, "dead": False,
            }
            return "", "", 0
        if verb == "kill-session":
            name = args[2]
            if name not in self.sessions:
                return "", f"can't find session: {name}", 1
            del self.sessions[name]
            return "", "", 0
        if verb == "set-option":
            name = args[2]
            if name not in self.sessions:
                return "", f"can't find session: {name}", 1
            self.sessions[name]["remain_on_exit"] = args[4] == "on"
            return "", "", 0
        if verb == "send-keys":
            name = args[2]
            if name not in self.sessions:
                return "", f"can't find session: {name}", 1
            # Three shapes reach here and only one of them is one keystroke:
            # ("-l", text) from the literal path, (text, "Enter") from the
            # non-literal path with enter=True, and ("Enter",) from the literal
            # path's separate submit. Recording args[-1] would drop `text` from
            # the middle shape and file the send as if only Enter were typed --
            # a double answering a narrower question than the caller asked.
            payload = list(args[3:])
            if payload[:1] == ["-l"]:
                # -l takes exactly one argument and refuses a trailing key name.
                payload = payload[1:2]
            for keystroke in payload:
                self.keys.append((name, keystroke))
            return "", "", 0
        if verb == "display-message":
            name = args[2]
            session = self.sessions.get(name)
            if session is None:
                return "", f"can't find session: {name}", 1
            fmt = args[4]
            if fmt == "#{pane_dead}":
                return "1" if session["dead"] else "0", "", 0
            if fmt == "#{pane_pid}":
                return "0", "", 0
            if fmt == "#{pane_current_path}":
                return session["cwd"], "", 0
            raise AssertionError(f"FakeMux does not model display format {fmt!r}")
        if verb == "attach":
            return "", "", 0 if args[2] in self.sessions else 1
        raise AssertionError(
            f"FakeMux does not model the {verb!r} verb (args: {args!r}). "
            "Teach it the verb rather than letting a test reach the real "
            "multiplexer -- see the comment above FakeMux."
        )


_MUX_BINARIES = frozenset({"tmux", "psmux", "pmux"})


def _is_a_multiplexer_spawn(cmd) -> bool:
    """True when ``cmd`` would start a real terminal multiplexer client.

    This ended in ``and False`` for the whole life of the branch that
    introduced it -- a debug stub committed by a session that said so in its
    own commit message ("committed as recovered, before verification") and was
    killed before the verification arrived. A predicate pinned to ``False``
    does not weaken the guard, it deletes it: ``guarded_run`` then delegates
    every argv, including the ones this file exists to refuse.

    What that costs is not hypothetical and not confined to the suite's
    accuracy. ``test_the_refusal_names_the_test_and_the_argv`` runs
    ``Mux(binary="tmux")._run("kill-server")`` in the expectation of being
    stopped here. Unstopped, it is a real ``tmux kill-server``: measured on
    this machine at the moment the branch was reviewed, seven live sessions --
    six peer agents and the reviewing session itself. The test asserting that
    the guard prevents destruction becomes the thing that destroys.

    So the failure mode ran both ways at once. The three positive controls in
    test_mux_isolation.py went red, which is the loud half; the quiet half is
    that every *other* test in the suite was free to reach the real server
    again, which is the exact condition a8575d7 and 20126d6 were written to
    end. Keep this a bare membership test.

    The name is extracted with ``ntpath.basename`` rather than
    ``os.path.basename``, because ``os.path`` is the *running* platform's path
    syntax and this guard is asked about argv that name the other one. On
    POSIX, ``os.path.basename(r"C:\\tools\\tmux.exe")`` is the whole string --
    backslash is an ordinary filename character there -- so the membership test
    saw ``c:\\tools\\tmux`` and the guard delegated a tmux invocation it exists
    to refuse. That is how the parametrised case for a full Windows path passed
    on the Windows legs and spawned on the four POSIX ones, which no amount of
    local Windows testing could show.

    ``ntpath`` is the right tool rather than splitting on both separators by
    hand, and the difference is not cosmetic: the hand-rolled version read
    ``C:tmux.exe`` -- a drive-*relative* path, which Windows accepts and
    resolves against the current directory on that drive -- as the name
    ``c:tmux``, missed, and delegated. That spelling is one ``os.path.basename``
    already handled, so the hand-rolled fix would have closed the POSIX hole by
    opening a Windows one. ``ntpath`` is pure syntax with no filesystem or
    platform dependence, and it understands drive prefixes, UNC paths and both
    separators, so it is the union of the two syntaxes rather than a guess at
    it. Over-refusing inside the test suite costs a loud error; under-refusing
    costs a real server.
    """
    head = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else cmd
    name = ntpath.basename(str(head)).lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name in _MUX_BINARIES


@pytest.fixture(autouse=True)
def _no_real_multiplexer(request: pytest.FixtureRequest):
    """Point `copilot_operator.MUX` at an empty in-memory multiplexer, and make
    any *other* route to a real one raise.

    An empty one, because that is what the leaking tests were already getting
    by accident: no session of theirs exists on the real server, so every
    `has_session` came back False. The behaviour they assert is unchanged; only
    its dependence on the machine goes away.

    Substituting `copilot_operator.MUX` is not on its own enough, and the gap
    is the kind that stays quiet. It closes the route the 30 leaking tests
    took; it does nothing about a test that builds its own `Mux()` -- which is
    exactly what test_integration.py does, deliberately, so the pattern is
    already in the file a newcomer copies from. A substitution cannot report
    what it did not intercept, so the second half poisons the spawn itself: any
    attempt to start a tmux/psmux/pmux client fails, loudly, naming the argv.
    Every other subprocess is delegated untouched -- the suite really does run
    Python child processes and must keep being able to.

    Tests that mean to drive a real server mark themselves `real_multiplexer`
    and are exempted from both halves.

    It saves and restores by hand rather than taking `monkeypatch`, and that is
    not a style choice. An autouse fixture that REQUESTS `monkeypatch` pulls
    monkeypatch's lifetime up to its own, so monkeypatch is finalised earlier
    than it used to be -- and `_no_stray_artifacts` above then runs its
    end-of-test scan with the test's patches STILL APPLIED. Doing that here
    turned all 26 tests in test_artifact_guard.py into teardown errors, because
    they patch `_GUARDED_DIRS` to their own tmp_path and the guard duly found
    their fixtures there. Depending on no ordinary fixture keeps this one first
    to set up and last to tear down, which is also the only order in which a
    test's own `monkeypatch.setattr(op, "MUX", ...)` is undone before this
    restores. `request` is exempt from that hazard: it is not finalised into
    the same stack.
    """
    if "real_multiplexer" in request.keywords:
        yield
        return

    real_mux = copilot_operator.MUX
    real_run = operator_mux.subprocess.run

    def guarded_run(cmd, *args, **kwargs):
        if _is_a_multiplexer_spawn(cmd):
            raise AssertionError(
                f"{request.node.nodeid} tried to start a real terminal "
                f"multiplexer: {cmd!r}. Unit tests must not drive this "
                f"machine's tmux/psmux server -- their answers then depend on "
                f"what happens to be running. Use conftest's FakeMux, or mark "
                f"the test `real_multiplexer` if it genuinely needs one."
            )
        return real_run(cmd, *args, **kwargs)

    copilot_operator.MUX = FakeMux()
    operator_mux.subprocess.run = guarded_run
    try:
        yield
    finally:
        operator_mux.subprocess.run = real_run
        copilot_operator.MUX = real_mux


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
