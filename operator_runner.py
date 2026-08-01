#!/usr/bin/env python3
"""In-pane supervisor for a single Copilot session.

The multiplexer launches this module, not Copilot directly. It exists to solve
two defects that cannot be fixed from the operator process:

1. **Process identity.** On POSIX the generated run script ends in
   `exec copilot`, so the multiplexer's pane PID *is* Copilot's PID. Windows has
   no `exec`: the measured process tree is
   `pane_pid -> pwsh -> run script -> copilot`, so the pane PID identifies the
   multiplexer's own shell. Because Copilot names its telemetry log
   `process-{startMs}-{pid}.log`, PID-based lookup silently fails there, which
   both disables `--resume` and lets concurrent instances attribute each other's
   usage. The runner spawns Copilot itself, so it knows the real PID.

   Even the spawned PID is not always the right one: on Windows the launcher is
   often a *shim* that re-execs the real binary as a child under a different
   pid. WinGet installs `copilot.exe` as such a shim, and virtualenv
   `python.exe` behaves the same way. The runner therefore matches the log
   against the whole process tree it created, and pins the file while that tree
   is still alive.

2. **Supervision across detach.** The operator prints "metrics will be captured
   when copilot exits" and then exits, so on detach nothing remained to capture
   them. The runner lives inside the pane and outlives detach, so it performs
   the capture itself.

State written to the instance state directory:

``{id}.pid``      Copilot's real process id, removed on exit
``{id}.session``  Copilot CLI session UUID, once discovered
``{id}.exit``     Exit code, written after metrics capture completes

A malformed launch spec exits ``EXIT_BAD_SPEC`` and is still reported through
those files: see :func:`_load_spec`.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path, PurePath, PurePosixPath

# `operator_ingest` is imported lazily below. When this module runs as the
# installed `operator-runner` console script rather than by path, an editable
# install's frozen module list is the only thing that can resolve it, so make
# our own directory importable as a fallback.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

SESSION_ID_TIMEOUT = 20
LOG_PIN_TIMEOUT = 30
TREE_SETTLE_SECONDS = 1.5

# Distinct from any code Copilot itself can return, so the parent can tell
# "I was launched wrong" from "the session failed". 78 is sysexits' EX_CONFIG.
EXIT_BAD_SPEC = 78

# Keys the operator always writes into the spec, and the type each must have.
# `argv` is checked against `list` rather than "is iterable" deliberately: a
# spec holding a bare string would otherwise pass through `list()` and be
# spawned one character per argument.
_REQUIRED_SPEC_KEYS = ("instance", "argv", "cwd",
                       "state_dir", "copilot_log_dir", "metrics_db")

# The spec the operator writes lives at ``{state_dir}/{instance}.launch.json``
# (see ``Instance.spec_file``), so its own path names both values we need to
# report a failure -- even when nothing inside the file can be read.
_SPEC_SUFFIX = ".launch.json"


def _log(state_dir: Path, instance: str, msg: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(state_dir / f"{instance}.runner.log", "a", encoding="utf-8") as fh:
            fh.write(f"[runner {stamp}] {msg}\n")
    except OSError:
        pass


def _process_parents() -> dict[int, int]:
    """Snapshot of pid -> parent pid for every visible process."""
    if sys.platform == "win32":
        return _process_parents_windows()
    return _process_parents_posix()


def _process_parents_windows() -> dict[int, int]:
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return {}
    parents: dict[int, int] = {}
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if kernel32.Process32First(snapshot, ctypes.byref(entry)):
            while True:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    return parents


def _process_parents_posix() -> dict[int, int]:
    parents: dict[int, int] = {}
    proc_root = Path("/proc")
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
                # comm may contain spaces/parens; fields follow the final ')'.
                tail = stat[stat.rfind(")") + 1 :].split()
                parents[int(entry.name)] = int(tail[1])
            except (OSError, ValueError, IndexError):
                continue
        return parents
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            bits = line.split()
            if len(bits) >= 2:
                parents[int(bits[0])] = int(bits[1])
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return parents


def _process_tree(root_pid: int) -> set[int]:
    """The root pid plus every descendant currently alive.

    Needed because ``Popen.pid`` is not always the process that ends up writing
    the telemetry log. On Windows the launcher is frequently a shim — WinGet
    installs ``copilot.exe`` as one, and virtualenv ``python.exe`` behaves the
    same way — which re-execs the real binary as a child under a different pid.
    Matching the whole tree keeps attribution correct without ever guessing.
    """
    pids = {root_pid}
    parents = _process_parents()
    if not parents:
        return pids
    children: dict[int, list[int]] = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    queue = [root_pid]
    while queue:
        current = queue.pop()
        for child in children.get(current, ()):
            if child not in pids:
                pids.add(child)
                queue.append(child)
    return pids



def _find_log(log_dir: Path, pids, started_ms: int, window_ms: int = 600_000) -> Path | None:
    """Locate the Copilot process log for a set of candidate PIDs.

    Never falls back to "newest log in the directory": that is precisely what
    causes one instance to record another's usage when several run at once.

    Bounded on both sides and resolved to the launch time rather than the
    newest match, because the OS can recycle a PID and a later Copilot run
    would otherwise be attributed to this session.
    """
    if isinstance(pids, int):
        pids = {pids}
    best: Path | None = None
    best_delta = None
    for pid in pids:
        try:
            candidates = list(log_dir.glob(f"process-*-{pid}.log"))
        except OSError:
            continue
        for path in candidates:
            stem = path.name[len("process-") : -len(f"-{pid}.log")]
            if not stem.isdigit():
                continue
            ms = int(stem)
            # Allow a small negative skew: the log may be stamped just before
            # the parent observes the spawn.
            if ms < started_ms - 5000 or ms > started_ms + window_ms:
                continue
            delta = abs(ms - started_ms)
            if best_delta is None or delta < best_delta:
                best, best_delta = path, delta
    return best


def _extract_session_id(path: Path) -> str | None:
    import re

    uuid_re = re.compile(
        r'"session_id"\s*:\s*"([0-9a-fA-F-]{36})"'
        r"|Workspace initialized:\s*([0-9a-fA-F-]{36})"
    )
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(4000):
                line = fh.readline()
                if not line:
                    break
                m = uuid_re.search(line)
                if m:
                    return m.group(1) or m.group(2)
    except OSError:
        return None
    return None


class SpecError(Exception):
    """The launch spec cannot be used. Carries a human-readable diagnostic.

    ``spec`` holds whatever was successfully parsed before the failure, so the
    caller can still recover the instance name from a spec that is merely
    incomplete rather than unreadable.
    """

    def __init__(self, message: str, spec: object = None) -> None:
        super().__init__(message)
        self.spec = spec


def _is_safe_component(name: object) -> bool:
    """True when ``name`` can only ever name a file *inside* a directory.

    The instance name is interpolated straight into `{instance}.exit`,
    `{instance}.pid` and `{instance}.runner.log`, so a corrupt or hand-edited
    spec carrying `..\\escaped` would otherwise write outside the state
    directory the parent is watching. Legitimate ids are already single path
    components -- the operator writes the spec itself as `{id}.launch.json` --
    so requiring that here rejects nothing real. `safe_instance_id` is not
    usable for this: it *rewrites* a name and appends a digest, which would
    silently address different files than the operator does.
    """
    if not isinstance(name, str) or not name.strip():
        return False
    if "\x00" in name or name in (".", ".."):
        return False
    return PurePath(name).name == name and PurePosixPath(name).name == name


def _fallback_identity(spec_path: Path, spec: object) -> tuple[Path, str]:
    """Best guess at ``(state_dir, instance)`` when the spec is unusable.

    Derived from the spec's own path, never from the spec's contents. The
    operator writes the spec to ``{state_dir}/{instance}.launch.json`` and
    polls ``{state_dir}/{instance}.exit``, so the path it handed us is by
    construction the one directory it is known to be watching. A spec that
    already failed validation has no authority to redirect the report
    somewhere the parent will never look -- which would hide the failure just
    as effectively as the traceback this function exists to replace.
    """
    instance = spec_path.name
    if instance.endswith(_SPEC_SUFFIX):
        instance = instance[: -len(_SPEC_SUFFIX)]
    else:
        instance = spec_path.stem
    if not _is_safe_component(instance):
        instance = "unknown"
    return spec_path.parent, instance


def _report_bad_spec(spec_path: Path, spec: object, message: str) -> int:
    """Make an unusable spec observable instead of a bare traceback.

    The supervisor dying silently is the worst case for the parent: the loop
    polls for ``{id}.exit`` and would otherwise have no record of why the pane
    went away. So write the marker and the runner log on the way out, and only
    then fail.

    Nothing in here may raise. It is the last-resort reporter, and a crash
    here restores exactly the failure it was written to prevent -- so the
    filesystem calls catch ``ValueError`` as well as ``OSError``: a path
    carrying an embedded NUL raises the former, not the latter.
    """
    state_dir, instance = _fallback_identity(spec_path, spec)
    detail = f"invalid launch spec {spec_path}: {message}"
    print(f"operator-runner: {detail}", file=sys.stderr)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        _log(state_dir, instance, detail)
        (state_dir / f"{instance}.exit").write_text(str(EXIT_BAD_SPEC),
                                                    encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"operator-runner: cannot write exit marker under {state_dir}: {exc}",
              file=sys.stderr)
    return EXIT_BAD_SPEC


def _load_spec(spec_path: Path) -> dict:
    """Read and validate the launch spec, or raise :class:`SpecError`.

    Every failure names the offending file and the offending key, because the
    reader of this diagnostic is looking at a pane that closed.
    """
    try:
        text = spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(f"cannot read spec: {exc}") from exc
    except UnicodeDecodeError as exc:
        # Not an OSError. Reading binary garbage must fail like every other
        # malformed spec, not as a bare traceback.
        raise SpecError(f"not valid UTF-8: {exc}") from exc
    try:
        spec = json.loads(text)
    except ValueError as exc:
        raise SpecError(f"not valid JSON: {exc}") from exc
    if not isinstance(spec, dict):
        raise SpecError(f"expected a JSON object, got {type(spec).__name__}")

    missing = [key for key in _REQUIRED_SPEC_KEYS if key not in spec]
    if missing:
        raise SpecError(f"missing required key(s): {', '.join(missing)}", spec)

    for key in ("instance", "cwd", "state_dir", "copilot_log_dir", "metrics_db"):
        value = spec[key]
        if not isinstance(value, str) or not value.strip():
            raise SpecError(f"key {key!r} must be a non-empty string, "
                            f"got {value!r}", spec)
        # An embedded NUL survives JSON but makes every filesystem call and
        # `Popen` raise ValueError -- which is not an OSError, so it would
        # escape the guards around the spawn and crash after validation
        # "passed", with no marker written.
        if "\x00" in value:
            raise SpecError(f"key {key!r} contains an embedded NUL", spec)

    if not _is_safe_component(spec["instance"]):
        raise SpecError(f"key 'instance' must be a single path component, "
                        f"got {spec['instance']!r}", spec)

    argv = spec["argv"]
    if not isinstance(argv, list):
        raise SpecError(f"key 'argv' must be a list, got {type(argv).__name__}",
                        spec)
    if not argv:
        raise SpecError("key 'argv' is empty; nothing to launch", spec)
    bad = [item for item in argv if not isinstance(item, str)]
    if bad:
        raise SpecError(f"key 'argv' must contain only strings; offending "
                        f"entries: {bad!r}", spec)
    if any("\x00" in item for item in argv):
        raise SpecError("key 'argv' contains an embedded NUL", spec)

    session_num = spec.get("session_num", 0)
    if isinstance(session_num, bool) or not isinstance(session_num, int):
        raise SpecError(f"key 'session_num' must be an integer, "
                        f"got {session_num!r}", spec)
    return spec


def run(spec_path: Path) -> int:
    spec_path = Path(spec_path)
    try:
        spec = _load_spec(spec_path)
    except SpecError as exc:
        return _report_bad_spec(spec_path, exc.spec, str(exc))

    instance: str = spec["instance"]
    argv: list[str] = list(spec["argv"])
    cwd: str = spec["cwd"]
    state_dir = Path(spec["state_dir"])
    log_dir = Path(spec["copilot_log_dir"])
    metrics_db = Path(spec["metrics_db"])
    session_num = int(spec.get("session_num", 0))

    state_dir.mkdir(parents=True, exist_ok=True)
    pid_file = state_dir / f"{instance}.pid"
    exit_file = state_dir / f"{instance}.exit"
    session_file = state_dir / f"{instance}.session"

    # A stale exit marker from the previous session would make the operator
    # think this one already finished.
    exit_file.unlink(missing_ok=True)

    started_ms = int(time.time() * 1000)
    _log(state_dir, instance, f"launching: {' '.join(argv)}")

    try:
        # No stream redirection: Copilot is a full-screen TUI and must inherit
        # the pane's terminal directly.
        proc = subprocess.Popen(argv, cwd=cwd)
    except FileNotFoundError:
        _log(state_dir, instance, f"executable not found: {argv[0]}")
        exit_file.write_text("127", encoding="utf-8")
        print(f"operator: cannot find {argv[0]!r} on PATH", file=sys.stderr)
        return 127
    except OSError as exc:
        _log(state_dir, instance, f"spawn failed: {exc}")
        exit_file.write_text("126", encoding="utf-8")
        return 126
    except ValueError as exc:
        # Belt and braces: validation rejects the argument shapes known to
        # reach this (an embedded NUL), but ValueError is not an OSError, so
        # anything missed here would escape both handlers above and kill the
        # supervisor without a marker.
        _log(state_dir, instance, f"invalid launch arguments: {exc}")
        exit_file.write_text(str(EXIT_BAD_SPEC), encoding="utf-8")
        print(f"operator-runner: invalid launch arguments: {exc}", file=sys.stderr)
        return EXIT_BAD_SPEC

    pid_file.write_text(str(proc.pid), encoding="utf-8")
    _log(state_dir, instance, f"launcher pid={proc.pid}")

    # Snapshot the process tree immediately: if the launcher is a shim it
    # re-execs the real binary as a child, and a short-lived process would
    # otherwise vanish before we could learn its pid.
    candidate_pids: set[int] = _process_tree(proc.pid)

    # Pin the log file while the tree is still alive, and pick up the CLI
    # session id so the operator can resume it later.
    pinned: Path | None = None
    found_session = False
    started_at = time.time()
    deadline = started_at + LOG_PIN_TIMEOUT
    # Sample the tree eagerly for a moment even if the process exits at once:
    # a shim may not have spawned the real binary by the first snapshot, and a
    # pid that is never observed can never be attributed.
    settle_until = started_at + TREE_SETTLE_SECONDS

    while True:
        now = time.time()
        alive = proc.poll() is None
        if alive or now < settle_until:
            candidate_pids |= _process_tree(proc.pid)

        if pinned is None:
            pinned = _find_log(log_dir, candidate_pids, started_ms)
            if pinned is not None:
                _log(state_dir, instance,
                     f"log pinned: {pinned.name} (pids={sorted(candidate_pids)})")

        if pinned is not None and not found_session:
            sid = _extract_session_id(pinned)
            if sid:
                session_file.write_text(sid, encoding="utf-8")
                _log(state_dir, instance, f"session id={sid}")
                found_session = True

        if not alive and now >= settle_until:
            break
        if found_session and pinned is not None:
            break
        if now > deadline:
            break
        time.sleep(0.25 if not alive else 0.5)

    if pinned is None:
        _log(state_dir, instance, "no log pinned during startup window")
    if not found_session:
        _log(state_dir, instance, "session id not discovered within timeout")

    returncode = proc.wait()
    _log(state_dir, instance, f"copilot exited rc={returncode}")
    pid_file.unlink(missing_ok=True)

    # Give Copilot a moment to flush its shutdown telemetry before parsing.
    time.sleep(2)

    logfile = pinned or _find_log(log_dir, candidate_pids, started_ms)
    if logfile is None:
        _log(state_dir, instance, "no matching log found; metrics not recorded")
    else:
        try:
            import operator_ingest

            result = operator_ingest.ingest_file(
                logfile,
                metrics_db,
                session_num=session_num,
                work_dir=cwd,
            )
            _log(state_dir, instance, f"metrics: {result}")
        except Exception as exc:  # pragma: no cover - defensive
            _log(state_dir, instance, f"metrics capture failed: {exc}")

    exit_file.write_text(str(returncode), encoding="utf-8")
    return returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="operator-runner",
        description="In-pane supervisor for a Copilot session (internal).",
    )
    parser.add_argument("spec", help="Path to the JSON launch spec")
    args = parser.parse_args(argv)
    return run(Path(args.spec))


if __name__ == "__main__":
    sys.exit(main())
