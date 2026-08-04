#!/usr/bin/env python3
"""Copilot CLI Operator — metrics-capturing wrapper for the GitHub Copilot CLI.

Cross-platform: Windows, Linux, WSL and macOS. Session management goes through
``operator_mux`` (tmux on POSIX, psmux on Windows) and each session is
supervised by ``operator_runner`` running inside the pane.

Why a runner rather than launching Copilot directly
---------------------------------------------------
The bash implementation relies on ``exec copilot`` so that the multiplexer's
pane PID *is* Copilot's PID. Windows has no ``exec``, so the pane PID identifies
the multiplexer's own shell instead and PID-based log lookup silently fails.
The runner spawns Copilot itself, records the real PID, and — because it lives
inside the pane — still captures metrics after the user detaches, which the
bash version never did.

State lives under ``~/.operator`` (override with ``COPILOT_OPERATOR_HOME``) and
never under ``~/.copilot``, which the Copilot CLI wholesale-deletes on startup.
"""
from __future__ import annotations

import atexit
import json
import os
import platform
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# An editable install freezes the module list into its import finder, so a
# module added to this directory after the last `pip install -e .` is invisible
# to the installed `operator` entry point even though the file sits right here.
# Making our own directory importable turns that stale-install failure into a
# no-op instead of a traceback on startup.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import operator_ingest                                       # noqa: E402
import operator_mail                                         # noqa: E402
import operator_trace                                        # noqa: E402
from install_manifest import (                                # noqa: E402
    dir_present,
    file_present,
    path_present,
)
from operator_console import enable_utf8_output               # noqa: E402
from operator_mux import (                                    # noqa: E402
    Mux, MuxError, MuxNotFoundError, safe_instance_id,
)
from project_paths import (                                   # noqa: E402
    catalog_rows,
    guid_is_usable,
    primary_repo_root,
    project_dir,
    projects_root,
)

__version__ = "1.0.0"

POLL_INTERVAL = 10
MAX_SESSIONS = 1000
MAX_LAUNCH_FAILURES = 5
LAUNCH_BACKOFF_BASE = 5
RESTART_PAUSE_SECONDS = 3
# A session that stayed up at least this long before dying did not fail to
# start, so it must not accumulate toward the consecutive-exit limit.
#
# That limit exists to stop a *hot* relaunch spin -- a session that dies on
# startup, every time, forever. It counted exits and never their spacing, so
# five unrelated deaths hours apart retired the supervisor exactly as fast as
# five in a minute. This machine's own logs are the case against it: on four
# separate occasions every instance died within seconds of every other,
# independent of when each was launched, having each run for minutes -- an
# external event, not a crash loop. Five such waves and the user came back to
# nothing running at all. Sessions that were healthy for minutes now reset the
# count, so only genuinely rapid failures can retire a loop.
HEALTHY_SESSION_SECONDS = 120
SESSION_ID_WAIT = 20
EXIT_GRACE_SECONDS = 20
RESERVED_WORDS = {"stop", "list", "report", "ingest", "help", "join", "reload",
                  "version", "forget", "logs", "tabs", "restore",
                  "stop-loop", "stop-session", "restart-loop", "menu",
                  "trace"}
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SESSION_ARG_RE = re.compile(r"^--(continue|resume|connect)(=.*)?$")

IS_WINDOWS = platform.system() == "Windows"

# Extra Popen/run kwargs for helper subprocesses that must never show a window.
#
# On Windows, a process that has no console of its own (for example the
# background loop supervisor) makes Windows allocate a brand new *visible*
# console for any console child it starts. CREATE_NO_WINDOW suppresses that.
#
# Constraint: CREATE_NO_WINDOW does not merely hide a window, it gives the
# child a *fresh* invisible console and rebinds its std handles to it. It is
# therefore safe only on calls that pass explicit pipes/handles
# (capture_output=True, stdout=DEVNULL, ...). Never apply it to a spawn that
# has to inherit the caller's terminal, such as an interactive attach or
# anything whose output the user is meant to read -- that output would be
# written into the hidden console and silently lost.
NO_WINDOW_KWARGS: dict = (
    {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    if IS_WINDOWS else {}
)


def is_wsl() -> bool:
    """True when running inside WSL (Windows Subsystem for Linux).

    ``platform.system()`` reports ``"Linux"`` for WSL, so this is a separate
    check for anything (like `operator restore`) that needs to know whether
    Windows Terminal / ``wt.exe`` might be reachable via interop.
    """
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(
            encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


# ── paths ───────────────────────────────────────────────────────
def operator_home() -> Path:
    override = os.environ.get("COPILOT_OPERATOR_HOME")
    return Path(override) if override else Path.home() / ".operator"


HOME = Path.home()
OPERATOR_HOME = operator_home()
RESTART_DIR = OPERATOR_HOME / "restart"
LOG_FILE = OPERATOR_HOME / "operator.log"
METRICS_DB = OPERATOR_HOME / "metrics.db"
BACKUPS_DIR = OPERATOR_HOME / "backups"
TABS_FILE = OPERATOR_HOME / "tabs.json"
COPILOT_LOG_DIR = Path(
    os.environ.get("COPILOT_LOG_DIR") or HOME / ".copilot" / "logs"
)

LEGACY_RESTART_DIR = HOME / ".copilot" / "restart"
LEGACY_LOG_FILE = HOME / ".copilot" / "operator.log"
LEGACY_METRICS_DB = HOME / ".copilot" / "operator-metrics.db"
LEGACY_BACKUPS_DIR = HOME / ".copilot" / "operator-backups"

MUX = Mux()


def log(msg: str) -> None:
    line = f"[operator {datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        OPERATOR_HOME.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr)


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(code)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── presence probes ─────────────────────────────────────────────
# Every *read* in this module already treats "could not read" as a
# non-answer (`except OSError: return None`). Presence checks were the
# exception: `Path.exists`/`is_dir`/`is_file` raise on anything outside
# pathlib's small ignore list -- EACCES, and on Windows a sharing violation
# from a scanner holding the file open -- so a probe that used to be a
# one-line question could end the process. In the supervisor's poll loop that
# is fatal in the literal sense: the loop only catches KeyboardInterrupt, so
# one unlucky stat on a marker file stops the unattended restarts this whole
# program exists to provide. See :func:`install_manifest.path_present` for
# which error codes lie in which direction.
#
# The tri-state answer is deliberate. "Cannot tell" must not share a return
# value with "absent", because absent is what licenses overwriting a file or
# concluding a session has ended.
#
# `dir_present`, `file_present` and `path_present` live in
# :mod:`install_manifest` so that this module, `handoff_tool` and the setup
# path all decide presence by the same rules. A second copy of a probe is a
# second place for the polarity to drift.

#: Paths already reported as unexaminable, so a permanent failure is logged
#: once per process rather than once per poll.
_PROBE_WARNED: set[str] = set()


class _Unplaceable:
    """Sentinel: the lookup failed, which is not the same as finding nothing.

    Handed back by ``_tracked_cwd_for_id`` so a caller deciding who is present
    cannot mistake "the records would not open" for "there is no record".
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unplaceable>"


UNPLACEABLE = _Unplaceable()


class _CatalogUnreadable:
    """Sentinel: the catalog could not be read, which is not "no entry".

    Handed back by ``project_handoff_file`` so a caller cannot mistake "the
    catalog would not open" for "this project was never registered". The two
    licence opposite statements to the agent: the first establishes nothing,
    while the second is used to explain why no handoff is expected here.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<catalog-unreadable>"


CATALOG_UNREADABLE = _CatalogUnreadable()


def remove_file(path: Path) -> bool:
    """Delete ``path`` if we can; report whether it is gone.

    ``unlink(missing_ok=True)`` only forgives a file that was already absent.
    Every caller here is cleaning up state it no longer wants, and a marker
    held open by a scanner or sitting on a denied path is a reason to move on,
    not to end the process with a traceback -- least of all the supervisor's
    shutdown path, which runs when something has already gone wrong.
    """
    try:
        path.unlink()
        return True
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError as exc:
        log(f"  Could not remove {path.name}: {exc}")
        return False


@contextmanager
def _exclusive_lock(path: Path):
    """Yield True when this process took ``path`` as a lock, False otherwise.

    ``O_CREAT|O_EXCL`` is the one creation primitive that is atomic on both
    POSIX and Windows, which is what makes it a lock rather than another
    check-then-act. A lock whose recorded pid is *readable* and dead is stale
    -- the holder crashed mid-operation -- and is reclaimed once. A lock whose
    owner cannot be read is not: ``os.open`` creates the file empty and the
    pid lands a moment later, so an unparseable lock is most likely one being
    taken right now, and deleting it would hand the same lock to two
    processes. Refusing there can jam a lock whose holder died inside that
    window; that is the trade, and a jam says so in the log while a double
    acquisition does not.
    """
    acquired = False
    try:
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    recorded = path.read_text(encoding="utf-8").strip()
                except OSError as exc:
                    log(f"  Lock {path.name} exists and could not be read "
                        f"({exc}) — treating it as held")
                    break
                try:
                    holder = int(recorded)
                except ValueError:
                    log(f"  Lock {path.name} names no owner — treating it as "
                        f"held. If nothing is running, remove {path}")
                    break
                if _pid_alive(holder):
                    break
                remove_file(path)
                continue
            except OSError:
                break
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(str(os.getpid()))
                acquired = True
                break
        yield acquired
    finally:
        if acquired:
            remove_file(path)


def marker_state(path: Path) -> bool | None:
    """Tri-state read of a supervisor signal file, warning once per path.

    None means the probe failed: the marker may or may not be set and the
    caller has to decide what to do about not knowing. Most callers can wait
    for the next poll; the one that cannot is crash recovery, which would
    otherwise read "cannot tell" as "nobody asked me to stop".
    """
    present = path_present(path)
    key = str(path)
    if present is None:
        if key not in _PROBE_WARNED:
            _PROBE_WARNED.add(key)
            log(f"  Could not examine {path.name} — treating it as unset and "
                f"re-checking next poll")
        return None
    _PROBE_WARNED.discard(key)
    return bool(present)


def marker_set(path: Path) -> bool:
    """True only when a marker file is definitely there.

    Used by the supervisor for signal files it polls. A probe that cannot
    answer reports "no signal yet" and lets the next poll ask again, which is
    the only outcome that keeps the loop alive; the alternatives are killing
    the supervisor with a traceback or acting on a signal nobody sent.

    Callers that would take an irreversible branch on the False must use
    ``marker_state`` instead: "no marker" and "no answer" only mean the same
    thing when the consequence of being wrong is one more poll.
    """
    return marker_state(path) is True


def set_tab_title(title: str) -> None:
    if sys.stdout.isatty():
        sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()


# Windows Terminal, ConEmu and a few others draw OSC 9;4 as a progress ring on
# the tab itself. State 3 is an animated indeterminate ring, which is as close
# to an animated tab icon as a terminal gets: custom icons are static images.
TAB_IDLE = 0
TAB_STEADY = 1
TAB_ERROR = 2
TAB_LOOPING = 3
TAB_WAITING = 4


def set_tab_progress(state: int, value: int = 100) -> None:
    """Show a state ring on the terminal tab. Terminals that do not know the
    sequence ignore it, so this is safe everywhere."""
    if os.environ.get("OPERATOR_NO_TAB_PROGRESS"):
        return
    if not sys.stdout.isatty():
        return
    sequence = f"\033]9;4;{state};{value}\007"
    if os.environ.get("TMUX"):
        # tmux drops sequences it does not implement unless they are wrapped in
        # its DCS passthrough (and allow-passthrough is on).
        sequence = "\033Ptmux;" + sequence.replace("\033", "\033\033") + "\033\\"
    try:
        sys.stdout.write(sequence)
        sys.stdout.flush()
    except OSError:
        pass


def clear_tab_progress() -> None:
    set_tab_progress(TAB_IDLE, 0)


atexit.register(clear_tab_progress)


def _move_legacy(src: Path, dest: Path) -> bool:
    """Move ``src`` onto ``dest``, gated and logged. True when it moved.

    The gate is the destination being *definitely* absent: a destination that
    merely cannot be examined must not be moved onto, because ``shutil.move``
    replaces a file it lands on and the one copy of the user's metrics or
    backups would be gone.

    Failures are logged rather than passed over. This function moves the
    user's data; "it silently did nothing" and "it silently did the wrong
    thing" are indistinguishable afterwards if neither says anything.
    """
    state = path_present(dest)
    if state is None:
        log(f"  Not migrating {src.name}: could not examine {dest}")
        return False
    if state:
        return False
    try:
        shutil.move(str(src), str(dest))
        return True
    except (OSError, shutil.Error) as exc:
        log(f"  Could not migrate {src} to {dest}: {exc}")
        return False


def migrate_legacy_state() -> None:
    """One-time move of state out of ~/.copilot, which the CLI deletes.

    Held under an exclusive lock, because probing a destination and then
    moving onto it are two syscalls and a tri-state probe only fixes the
    first one. A machine running a loop per project starts many operators at
    once; without the lock they interleave between the check and the move, and
    on POSIX a directory that appeared absent to both ends up nested inside
    itself rather than moved. The loser of the race finds the work already
    done, which is the correct outcome for a one-time migration.
    """
    RESTART_DIR.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(OPERATOR_HOME / "migrate.lock") as acquired:
        if not acquired:
            return
        moved = 0
        # Each probe is spent on three states, not two. ~/.copilot is deleted
        # by the CLI, so anything left behind here is lost for good -- and a
        # skip caused by an unexaminable source is indistinguishable, in the
        # log and in the outcome, from there having been nothing to move.
        legacy_restart = dir_present(LEGACY_RESTART_DIR)
        if legacy_restart is None:
            log(f"  Could not examine {LEGACY_RESTART_DIR} — any legacy state "
                f"there has been left in place, not migrated")
        elif legacy_restart and LEGACY_RESTART_DIR != RESTART_DIR:
            try:
                legacy_items = list(LEGACY_RESTART_DIR.iterdir())
            except OSError as exc:
                log(f"  Could not list {LEGACY_RESTART_DIR}: {exc}")
                legacy_items = []
            for src in legacy_items:
                moved += _move_legacy(src, RESTART_DIR / src.name)
        for src, dest in ((LEGACY_LOG_FILE, LOG_FILE),
                          (LEGACY_METRICS_DB, METRICS_DB)):
            state = file_present(src)
            if state is None:
                log(f"  Could not examine {src} — left in place, not migrated")
            elif state:
                moved += _move_legacy(src, dest)
        backups = dir_present(LEGACY_BACKUPS_DIR)
        if backups is None:
            log(f"  Could not examine {LEGACY_BACKUPS_DIR} — left in place, "
                f"not migrated")
        elif backups:
            moved += _move_legacy(LEGACY_BACKUPS_DIR, BACKUPS_DIR)
        if moved:
            log(f"Migrated {moved} legacy state item(s) into {OPERATOR_HOME}")


# ── instance ────────────────────────────────────────────────────
class Instance:
    """One named unit of work: a session plus its state files."""

    def __init__(self, display_name: str):
        self.display_name = display_name
        self.id = safe_instance_id(display_name)
        self.session = self.id
        RESTART_DIR.mkdir(parents=True, exist_ok=True)

    # -- file locations
    @property
    def restart_marker(self) -> Path:
        return RESTART_DIR / self.id

    @property
    def state_file(self) -> Path:
        return RESTART_DIR / f"{self.id}.state"

    @property
    def managed_file(self) -> Path:
        return RESTART_DIR / f"{self.id}.managed"

    @property
    def pid_file(self) -> Path:
        return RESTART_DIR / f"{self.id}.pid"

    @property
    def exit_file(self) -> Path:
        return RESTART_DIR / f"{self.id}.exit"

    @property
    def session_file(self) -> Path:
        return RESTART_DIR / f"{self.id}.session"

    @property
    def spec_file(self) -> Path:
        return RESTART_DIR / f"{self.id}.launch.json"

    @property
    def loop_pid_file(self) -> Path:
        """PID of the *background loop supervisor* process (not Copilot's)."""
        return RESTART_DIR / f"{self.id}.loop.pid"

    @property
    def loop_args_file(self) -> Path:
        """The arguments loop mode was started with.

        Recorded so a supervisor can be replaced (``operator restart-loop``)
        without having to reconstruct them from the launch spec, where they
        are already mixed with the preamble and the flags loop mode adds.
        """
        return RESTART_DIR / f"{self.id}.loopargs.json"

    @property
    def restart_lock_file(self) -> Path:
        """Held while a supervisor handoff is in progress.

        Two concurrent ``operator restart-loop`` runs would both retire the
        old supervisor and both spawn a replacement, leaving two supervisors
        fighting over one session — each relaunching what the other killed.
        """
        return RESTART_DIR / f"{self.id}.restartlock"

    @property
    def detach_marker(self) -> Path:
        """Touched to ask a running loop supervisor to exit but leave the
        Copilot session running (``operator stop-loop``)."""
        return RESTART_DIR / f"{self.id}.detach"

    @property
    def stop_marker(self) -> Path:
        """Touched to ask a running loop supervisor to shut down *and* stop
        the Copilot session, without racing a relaunch (``operator stop``)."""
        return RESTART_DIR / f"{self.id}.stopreq"

    # -- ownership
    def claim(self, token: str) -> None:
        """Record ownership of the *live* session.

        The record binds a token to the session as it exists now. Continuity
        state (``.state``) deliberately does **not** confer ownership: it
        outlives the session so a named loop can auto-continue, and treating it
        as proof of ownership would let a stale file authorize killing an
        unrelated session that later took the same name.
        """
        payload = {
            "token": token,
            "display_name": self.display_name,
            "session": self.session,
            "claimed_at": utcnow(),
            "pid": os.getpid(),
        }
        tmp = self.managed_file.with_suffix(".managed.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self.managed_file)

    def ownership(self) -> dict | None:
        # "Cannot examine" answers the same as "no claim": ownership is what
        # authorizes destroying a session, so anything short of a claim we can
        # actually read must refuse.
        if path_present(self.managed_file) is not True:
            return None
        try:
            return json.loads(self.managed_file.read_text(encoding="utf-8"))
        except ValueError:
            # A legacy or truncated marker: it read fine, it just says
            # nothing. Present but tokenless.
            return {"token": None, "display_name": self.display_name}
        except OSError:
            # Something is there but we could not read it — a dangling
            # symlink, a directory, a denied file. Returning the tokenless
            # dict here would hand out ownership on the strength of a claim
            # nobody managed to read, and ownership is what authorizes
            # killing a session.
            return None

    def owns_live_session(self) -> bool:
        """True only when this operator's claim matches a session that exists.

        Required before any destructive action. ``is_managed`` is about
        continuity, not authority.
        """
        owner = self.ownership()
        if owner is None:
            return False
        if owner.get("session") not in (None, self.session):
            return False
        return MUX.has_session(self.session)

    def is_managed(self) -> bool:
        """True when this instance has operator state of any kind.

        Used for listing and continuity only — never to authorize a kill, so
        state that cannot be examined counts as present: reporting "no such
        instance" for state that is really there is the misleading answer, and
        every destructive path re-checks ownership anyway.
        """
        return (path_present(self.managed_file) is not False
                or path_present(self.state_file) is not False)

    # -- persisted state
    def save_state(self, session_num: int, run_started: str, session_id: str = "") -> None:
        lines = [f"SESSION_NUM={session_num}", f"RUN_STARTED={run_started}"]
        if session_id:
            lines.append(f"COPILOT_SESSION_ID={session_id}")
        tmp = self.state_file.with_suffix(".state.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_file)

    def load_state(self) -> dict | None:
        if path_present(self.state_file) is False:
            return None
        state: dict[str, str] = {}
        try:
            for line in self.state_file.read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    state[k.strip()] = v.strip()
        except OSError:
            return None
        return state

    def read_session_id(self) -> str:
        try:
            value = self.session_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return value if UUID_RE.match(value) else ""

    def copilot_pid(self) -> int | None:
        try:
            return int(self.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def cleanup_files(self) -> None:
        for path in (self.restart_marker, self.managed_file, self.spec_file,
                     self.pid_file, self.exit_file, self.session_file,
                     self.loop_pid_file, self.detach_marker, self.stop_marker,
                     self.loop_args_file, self.restart_lock_file):
            remove_file(path)


def read_managed_instances() -> dict[str, dict] | None:
    """Managed instances, or None when the state directory could not be read.

    The distinction matters to anything deciding *who is present*. An empty
    map and a failed listing look identical to a caller and mean opposite
    things, and one of them is a licence to act as though nobody else is
    here.
    """
    found: dict[str, dict] = {}
    present = dir_present(RESTART_DIR)
    if present is None:
        return None
    if not present:
        return found
    try:
        entries = list(RESTART_DIR.iterdir())
    except OSError:
        return None
    for path in entries:
        if path.suffix == ".managed":
            ident = path.name[: -len(".managed")]
        elif path.suffix == ".state":
            ident = path.name[: -len(".state")]
        else:
            continue
        meta = found.setdefault(ident, {})
        if path.suffix == ".managed":
            try:
                meta.update(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
    return found


def managed_instances() -> dict[str, dict]:
    """Managed instances as far as they can be listed; unreadable reads empty.

    Only for callers that display or look up a known id. Anything deciding
    whether somebody *else* is here must use :func:`read_managed_instances`
    and refuse on None.
    """
    found = read_managed_instances()
    return {} if found is None else found


# ── tab registry ────────────────────────────────────────────────
# Windows Terminal (and most terminal emulators) expose no API to list their
# own tabs, so the operator keeps its own record of which named instances were
# started from a terminal tab, in which directory, and with which arguments.
# After a reboot or crash every process is gone, but this file survives, and
# `operator restore` replays each entry in a fresh tab — the existing
# auto-continue/--resume logic then picks the Copilot session back up.
def read_tabs() -> dict | None:
    """The tab registry, or None when it exists but could not be read.

    The distinction matters because the registry is rewritten whole. Treating
    an unreadable file as an empty one would let the next ``register_tab``
    replace every other tab's restore record with a single entry.
    """
    if path_present(TABS_FILE) is False:
        return {}
    try:
        data = json.loads(TABS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else {}


def load_tabs() -> dict[str, dict]:
    """The tab registry as far as it can be read; unreadable reads as empty.

    Only for callers that display or filter. Anything that writes the file
    back must use :func:`read_tabs` and refuse the write on None.
    """
    entries = read_tabs()
    return {} if entries is None else entries


def save_tabs(entries: dict[str, dict]) -> None:
    OPERATOR_HOME.mkdir(parents=True, exist_ok=True)
    tmp = TABS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, TABS_FILE)


def register_tab(instance: Instance, loop_mode: bool,
                 copilot_args: list[str], cwd: Path) -> None:
    """Remember how to relaunch this instance's tab after a crash.

    Only runs when a Windows Terminal session id is present (``$WT_SESSION``),
    since that's the signal an interactive tab — as opposed to a one-off or
    CI invocation — started this instance. ``--fresh`` is deliberately never
    persisted here: a restore should always try to resume, never reset
    numbering, even if the tab that crashed had just been started fresh.
    """
    if not os.environ.get("WT_SESSION"):
        return
    argv = ["--loop"] if loop_mode else []
    argv += ["--name", instance.display_name, *copilot_args]
    entries = read_tabs()
    if entries is None:
        # Rewriting the file from what we could not read would drop every
        # other tab. Losing one tab's restore record is the smaller loss.
        log(f"  Could not read {TABS_FILE.name} — not recording this tab, so "
            f"the tabs already in it survive")
        return
    entries[instance.id] = {
        "display_name": instance.display_name,
        "cwd": str(cwd),
        "argv": argv,
        "wsl_distro": os.environ.get("WSL_DISTRO_NAME", ""),
        "updated_at": utcnow(),
    }
    save_tabs(entries)


def remove_tab(instance_id: str) -> None:
    entries = load_tabs()
    if instance_id in entries:
        del entries[instance_id]
        save_tabs(entries)


def manage_tabs(args: list[str]) -> int:
    """Inspect or edit the tab registry used by `operator restore`."""
    entries = load_tabs()
    if not args or args[0] == "list":
        if not entries:
            print("No tracked tabs. Tabs are recorded automatically when a "
                  "named instance (--name/--loop) is started inside a "
                  "Windows Terminal tab.")
            return 0
        print("═══ Tracked Tabs ═══\n")
        for ident, meta in sorted(entries.items()):
            kind = f"wsl:{meta['wsl_distro']}" if meta.get("wsl_distro") else "native"
            print(f"  {meta.get('display_name', ident)}  [{kind}]")
            print(f"    cwd:  {meta.get('cwd', '?')}")
            print(f"    argv: operator {' '.join(meta.get('argv', []))}")
        print("\nRestore: operator restore")
        print("Remove:  operator tabs remove <name>")
        return 0
    if args[0] == "remove":
        if len(args) < 2:
            print("Usage: operator tabs remove NAME", file=sys.stderr)
            return 1
        target = Instance(args[1]).id
        if target not in entries:
            print(f"No tracked tab for '{args[1]}'.", file=sys.stderr)
            return 1
        del entries[target]
        save_tabs(entries)
        print(f"Removed tracked tab '{args[1]}'.")
        return 0
    if args[0] == "clear":
        save_tabs({})
        print("Cleared all tracked tabs.")
        return 0
    print("Usage: operator tabs [list|remove NAME|clear]", file=sys.stderr)
    return 1


def _wsl_distros() -> list[str]:
    """List installed WSL distros, oldest/most-reliable enumeration first."""
    wsl = shutil.which("wsl.exe") or shutil.which("wsl")
    if not wsl:
        return []
    try:
        # stdin=DEVNULL: wsl.exe otherwise inherits our stdin and can consume
        # bytes meant for the interactive restore picker's input() call.
        out = subprocess.run([wsl, "-l", "-q"], capture_output=True,
                             stdin=subprocess.DEVNULL, timeout=15, check=False,
                             **NO_WINDOW_KWARGS)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    # wsl -l -q emits UTF-16LE with a BOM on stock Windows builds.
    try:
        text = out.stdout.decode("utf-16-le")
    except UnicodeDecodeError:
        text = out.stdout.decode("utf-8", errors="ignore")
    names = [line.strip().strip("\x00") for line in text.splitlines()]
    return [n for n in names if n]


def _read_remote_tabs(distro: str) -> dict[str, dict]:
    """Read another distro's tab registry via `wsl.exe -d <distro>`.

    Reading through the WSL command (rather than guessing a \\\\wsl.localhost
    UNC path) works regardless of each distro's actual $HOME layout and
    whether COPILOT_OPERATOR_HOME is overridden there.
    """
    wsl = shutil.which("wsl.exe") or shutil.which("wsl")
    if not wsl:
        return {}
    try:
        # stdin=DEVNULL for the same reason as _wsl_distros above.
        out = subprocess.run(
            [wsl, "-d", distro, "--", "cat", "$HOME/.operator/tabs.json"],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=15,
            check=False, shell=False, **NO_WINDOW_KWARGS,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0 or not out.stdout:
        return {}
    try:
        data = json.loads(out.stdout.decode("utf-8", errors="ignore"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# Characters a PowerShell command-mode token may contain and still be passed
# through verbatim. The set is deliberately small: `#` starts a comment that
# swallows every later argument, a leading `@` is splatting, `$` interpolates,
# and a comma builds an array literal -- `x,` reaches the process as `x` and
# `a, b` reaches it as two arguments. Anything outside this set is quoted,
# which is never wrong, only noisier.
_PS_BARE = re.compile(r"\A[A-Za-z0-9_+=:./\\-]+\Z")


def _ps_quote(arg: str) -> str:
    """Quote one argument for a PowerShell ``-Command`` string.

    Single quotes make PowerShell treat the token as a literal, so a Windows
    path's backslashes and a `$` in an argument survive intact; the only
    escape inside them is a doubled `''`.

    One class of argument cannot be fixed here: Windows PowerShell
    re-serialises arguments into a command line before handing them to a
    native executable, and that layer drops an empty argument and the
    stop-parsing token ``--%``, and mishandles an embedded double quote.
    Escaping the quote as ``\\"`` repairs the simple case and corrupts
    ``a\\"b`` -- backslashes before a quote follow a separate doubling rule --
    so these are known, deliberate limitations rather than bugs to patch.
    """
    if _PS_BARE.match(arg):
        return arg
    return "'" + arg.replace("'", "''") + "'"


def _as_text(value, default: str) -> str:
    """A registry field as a string, falling back when it is anything else."""
    return value if isinstance(value, str) else default


def _relaunch_command(argv: list[str], quote) -> str:
    """The `operator ...` command line that restores one tab.

    The registry stores argv as a list, so rebuilding it with a plain
    ``" ".join`` silently loses every argument boundary: a tab started as
    ``--name "my project"`` comes back as ``--name my project``, which restores
    a differently-named instance, and an argument containing an apostrophe
    produces an unbalanced quote that kills the shell before `operator` runs.
    """
    return " ".join(["operator", *(quote(str(a)) for a in argv)])


def _wsl_escape(command: str) -> str:
    """Neutralise the expansion WSL performs before bash ever parses the string.

    ``wsl.exe -- bash -lic <string>`` does not hand the string to bash intact:
    WSL rebuilds a command line on the Linux side and runs it through
    ``/bin/bash -c`` -- a bare ``wsl.exe -d D -- /usr/bin/printf ...`` still
    fails with a *bash* syntax error, which is how you can tell. Word structure
    survives that pass, so `shlex.quote` alone looks like it works, but `$` and
    backticks are expanded inside what should be a single-quoted literal. A
    tracked argument of ``$(rm -rf ~)`` would therefore *run* during restore,
    and one containing a lone backtick aborts the tab with an unmatched-quote
    error. Escaping these three characters makes the pass a no-op; the real
    parse then sees exactly the quoted string `shlex.quote` produced.

    Verified empirically against Ubuntu-24.04 over 25 hostile arguments,
    including ``$(echo PWNED)``, ```echo PWNED```, ``$HOME``, ``\\$HOME``,
    ``$(``, ``a\\`b`` and a trailing backslash.
    """
    return command.replace("\\", "\\\\").replace("$", "\\$").replace("`", "\\`")


def _build_wt_command(entries: list[tuple[str, dict]]) -> list[str]:
    """Build a single `wt.exe` argv that opens one tab per entry."""
    wt = shutil.which("wt.exe") or shutil.which("wt")
    if not wt:
        die("Windows Terminal ('wt.exe') was not found on PATH.")
    cmd = [wt]
    for i, (_ident, meta) in enumerate(entries):
        if i:
            cmd += [";", "new-tab"]
        else:
            cmd += ["new-tab"]
        # Every field here comes from a file the user can hand-edit, so none of
        # it can be trusted to be the type it should be: a dict reaching
        # subprocess raises TypeError, which the launch path does not expect.
        title = _as_text(meta.get("display_name"), "operator")
        cmd += ["--title", title]
        argv = meta.get("argv", [])
        if not isinstance(argv, list):
            # A hand-edited registry could hold a string here; iterating that
            # would relaunch the tab one character per argument.
            argv = []
        distro = _as_text(meta.get("wsl_distro"), "")
        cwd = _as_text(meta.get("cwd"), "")
        if distro:
            cmd += ["-d", cwd or "~", "wsl.exe", "-d", distro]
            if cwd:
                cmd += ["--cd", cwd]
            cmd += ["--", "bash", "-lic",
                    _wsl_escape(_relaunch_command(argv, shlex.quote))]
        else:
            if cwd:
                cmd += ["-d", cwd]
            cmd += ["powershell", "-NoExit", "-Command",
                    _relaunch_command(argv, _ps_quote)]
    return cmd


def _collect_tab_entries() -> list[tuple[str, dict]]:
    """Gather tab registry entries from the local machine and every WSL distro."""
    local = load_tabs()
    combined: list[tuple[str, dict]] = [(f"local:{k}", v) for k, v in local.items()]

    # When this process is itself running inside a WSL distro, that distro's
    # own registry is already included above via `local` -- querying it again
    # through `wsl.exe -d <this-distro>` would duplicate every entry. This is
    # a no-op on native Windows, where $WSL_DISTRO_NAME is never set.
    current_distro = os.environ.get("WSL_DISTRO_NAME", "")
    for distro in _wsl_distros():
        if current_distro and distro == current_distro:
            continue
        remote = _read_remote_tabs(distro)
        for ident, meta in remote.items():
            meta = dict(meta)
            meta.setdefault("wsl_distro", distro)
            combined.append((f"{distro}:{ident}", meta))
    return combined


def _describe_entry(meta: dict) -> str:
    kind = f"wsl:{meta['wsl_distro']}" if meta.get("wsl_distro") else "native"
    return f"{meta.get('display_name', '?')}  [{kind}]  {meta.get('cwd', '?')}"


def _parse_selection(selection: str, n: int) -> list[int]:
    """Parse a picker response like '1,3' or 'all' into 0-based indices."""
    selection = selection.strip().lower()
    if not selection:
        return []
    if selection == "all":
        return list(range(n))
    indices: list[int] = []
    for part in selection.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            idx = int(part) - 1
        except ValueError:
            continue
        if 0 <= idx < n:
            indices.append(idx)
    return indices


def _prompt_selection(n: int) -> str:
    try:
        return input(f"Select tab(s) to restore [1-{n}, comma-separated, or "
                     "'all'] (blank to cancel): ")
    except EOFError:
        return ""


def restore_tabs(args: list[str]) -> int:
    """Reopen a Windows Terminal window with a tab per selected instance.

    A full reboot kills every multiplexer server and Copilot process, so there
    is nothing left to reattach to — this simply replays each instance's
    original `operator` command line in a fresh tab. Loop mode's own
    auto-continue logic (session numbering + saved `--resume=<uuid>`) takes it
    from there, so each Copilot session picks back up rather than starting
    over.

    With no arguments, prompts for which tracked tab(s) to restore. `--all`
    restores every tracked tab without prompting. Display names given as
    positional arguments restore exactly those, without prompting.
    """
    if not IS_WINDOWS and not is_wsl():
        die("operator restore needs Windows Terminal (wt.exe), which is only "
            "reachable from native Windows or from within WSL (with Windows "
            "interop enabled).")
    if is_wsl() and not IS_WINDOWS:
        print("(Running inside WSL — this only sees this machine's local and "
              "sibling-WSL-distro tab registries, not a native Windows-side "
              "registry. Run `operator restore` from Windows PowerShell for "
              "that.)\n")
    dry_run = "--dry-run" in args or "--list" in args
    restore_all = "--all" in args
    names = [a for a in args if not a.startswith("--")]

    combined = _collect_tab_entries()
    if not combined:
        print("No tracked tabs to restore. Tabs are recorded automatically "
              "when a named instance (--name/--loop) is started inside a "
              "Windows Terminal tab.")
        return 0

    if names:
        wanted = set(names)
        selected = [(ident, meta) for ident, meta in combined
                    if meta.get("display_name") in wanted]
        missing = wanted - {meta.get("display_name") for _, meta in selected}
        if missing:
            print(f"No tracked tab(s): {', '.join(sorted(missing))}", file=sys.stderr)
        if not selected:
            return 1
    elif restore_all:
        selected = combined
    else:
        print("═══ Tracked Tabs ═══\n")
        for i, (_ident, meta) in enumerate(combined, 1):
            print(f"  [{i}] {_describe_entry(meta)}")
        print()
        response = _prompt_selection(len(combined))
        indices = _parse_selection(response, len(combined))
        if not indices:
            print("Nothing selected. Cancelled.")
            return 0
        selected = [combined[i] for i in indices]

    print(f"\nRestoring {len(selected)} tab(s):")
    for _ident, meta in selected:
        print(f"  {_describe_entry(meta)}")

    cmd = _build_wt_command(selected)
    if dry_run:
        print("\n--dry-run: not launching. Command that would run:")
        print("  " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
        return 0

    try:
        subprocess.Popen(cmd, close_fds=True)
    except (OSError, TypeError) as exc:
        die(f"Failed to launch Windows Terminal: {exc}")
    print("\nLaunched. Each tab resumes its own Copilot session as it starts.")
    return 0


# ── metrics presentation ────────────────────────────────────────
def _table(rows, headers) -> str:
    if not rows:
        return "(no data)"
    data = [list(headers)] + [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in data) for i in range(len(headers))]
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(data[0])).rstrip(),
           "  ".join("-" * widths[i] for i in range(len(headers)))]
    for row in data[1:]:
        out.append("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))).rstrip())
    return "\n".join(out)


def _query(sql: str, params=()) -> tuple[list, list[str]]:
    with operator_ingest.connect(METRICS_DB) as conn:
        cur = conn.execute(sql, params)
        headers = [d[0] for d in cur.description]
        return cur.fetchall(), headers


def _fmt_duration_sql(col: str) -> str:
    return (
        f"CASE WHEN {col} >= 3600 THEN ({col}/3600) || 'h ' || (({col}%3600)/60) || 'm' "
        f"ELSE ({col}/60) || 'm ' || ({col}%60) || 's' END"
    )


# AI credits are stored as integer nano-AIU to avoid float drift. These
# fragments convert on read. See operator_ingest for the billing constants.
_NANO = operator_ingest.NANO_AIU_PER_CREDIT
_USD = operator_ingest.USD_PER_CREDIT
_LEGACY_USD = operator_ingest.USD_PER_PREMIUM_REQUEST


def _credits(expr: str = "nano_aiu") -> str:
    return f"COALESCE(SUM({expr}),0) / {_NANO}.0"


def _legacy_usd_term(predicate: str = "1") -> str:
    """Dollars from legacy premium requests, for rows with no AI credits."""
    return (
        f"COALESCE(SUM(CASE WHEN ({predicate}) AND COALESCE(nano_aiu,0) = 0 "
        f"THEN premium_requests ELSE 0 END),0) * {_LEGACY_USD}"
    )


def _usd(predicate: str = "1") -> str:
    """Dollars over an optional window, combining both billing models.

    Rows predating the 2026-06-01 change have no ``nano_aiu``, so their cost is
    still derived from premium requests. The same window must apply to both
    terms — costing credits within a period but legacy usage only all-time
    would report $0.00 for a legacy user's real spend.
    """
    return (
        f"(COALESCE(SUM(CASE WHEN ({predicate}) THEN nano_aiu ELSE 0 END),0) "
        f"/ {_NANO}.0) * {_USD} + {_legacy_usd_term(predicate)}"
    )


def report_metrics(subcmd: str = "summary") -> int:
    try:
        return _report_metrics(subcmd)
    except sqlite3.Error as exc:
        # A file is there but sqlite cannot make sense of it: truncated,
        # locked, or not a database at all. That is a bad report, not a
        # crashed CLI.
        print(f"Could not read the metrics database at {METRICS_DB}: {exc}",
              file=sys.stderr)
        return 1


def _report_metrics(subcmd: str = "summary") -> int:
    state = file_present(METRICS_DB)
    if state is None:
        # Connecting anyway would create a database at whatever the path
        # really points at -- through a dangling symlink, that is a brand new
        # empty file somewhere else and every query below fails on it.
        print(f"Could not examine the metrics database at {METRICS_DB}.",
              file=sys.stderr)
        return 1
    if not state:
        print(f"No metrics database found at {METRICS_DB}")
        print("Run the operator first to start collecting metrics.")
        return 1

    home = str(HOME)
    if subcmd == "summary":
        print("═══ Usage Summary ═══\n")
        rows, headers = _query(f"""
            SELECT
              printf('%.1f', COALESCE(SUM(CASE WHEN date(ended_at,'localtime')=date('now','localtime')
                                THEN nano_aiu ELSE 0 END),0) / {_NANO}.0) AS today_credits,
              printf('%.1f', COALESCE(SUM(CASE WHEN ended_at >= datetime('now','-7 days')
                                THEN nano_aiu ELSE 0 END),0) / {_NANO}.0) AS week_credits,
              printf('%.1f', {_credits()}) AS all_time_credits,
              COALESCE(SUM(premium_requests),0) AS legacy_premium,
              COUNT(*) AS sessions
            FROM sessions WHERE no_op = 0
        """)
    elif subcmd == "sessions":
        print("═══ Recent Sessions ═══\n")
        rows, headers = _query(f"""
            SELECT session_num AS '#', substr(started_at,1,16) AS started,
                   printf('%.1f', COALESCE(nano_aiu,0) / {_NANO}.0) AS credits,
                   COALESCE(premium_requests,0) AS legacy_pr,
                   COALESCE(api_time_seconds || 's','—') AS api_time,
                   COALESCE({_fmt_duration_sql('session_time_seconds')},'—') AS sess_time,
                   COALESCE('+' || lines_added || ' -' || lines_removed,
                            '—') AS changes,
                   COALESCE(substr(git_branch,1,20),'—') AS branch,
                   COALESCE(replace(work_dir, ?, '~'),'—') AS project
            FROM sessions WHERE no_op = 0 ORDER BY id DESC LIMIT 20
        """, (home,))
    elif subcmd == "models":
        print("═══ Per-Model Usage ═══\n")
        rows, headers = _query(f"""
            SELECT model_name AS model,
                   printf('%.1f', {_credits()}) AS credits,
                   COALESCE(SUM(premium_requests),0) AS legacy_pr,
                   COUNT(*) AS appearances
            FROM model_usage GROUP BY model_name
            ORDER BY SUM(nano_aiu) DESC, SUM(premium_requests) DESC
        """)
    elif subcmd == "projects":
        print("═══ Per-Project Usage ═══\n")
        rows, headers = _query(f"""
            SELECT COALESCE(replace(work_dir, ?, '~'),'—') AS project,
                   printf('%.1f', {_credits()}) AS credits,
                   printf('$%.2f', {_usd()}) AS est_cost,
                   COUNT(*) AS sessions,
                   COALESCE(SUM(api_time_seconds) || 's','—') AS total_api_time
            FROM sessions WHERE no_op = 0 GROUP BY work_dir
            ORDER BY SUM(nano_aiu) DESC
        """, (home,))
    elif subcmd == "costs":
        today = "date(ended_at,'localtime')=date('now','localtime')"
        week = "ended_at >= datetime('now','-7 days')"
        month = "strftime('%Y-%m', ended_at)=strftime('%Y-%m','now')"
        print("═══ Cost Estimates ═══\n")
        rows, headers = _query(f"""
            SELECT
              printf('%.1f', COALESCE(SUM(CASE WHEN {today}
                                THEN nano_aiu ELSE 0 END),0) / {_NANO}.0) AS today_cr,
              printf('$%.2f', {_usd(today)}) AS today_cost,
              printf('%.1f', COALESCE(SUM(CASE WHEN {week}
                                THEN nano_aiu ELSE 0 END),0) / {_NANO}.0) AS week_cr,
              printf('$%.2f', {_usd(week)}) AS week_cost,
              printf('%.1f', COALESCE(SUM(CASE WHEN {month}
                                THEN nano_aiu ELSE 0 END),0) / {_NANO}.0) AS month_cr,
              printf('$%.2f', {_usd(month)}) AS month_cost,
              printf('%.1f', {_credits()}) AS all_cr,
              printf('$%.2f', {_usd()}) AS all_cost
            FROM sessions WHERE no_op = 0
        """)
    elif subcmd == "tokens":
        print("═══ Token Usage ═══\n")
        rows, headers = _query(f"""
            SELECT COALESCE(SUM(tokens_input),0) AS input,
                   COALESCE(SUM(tokens_output),0) AS output,
                   COALESCE(SUM(tokens_cache_read),0) AS cache_read,
                   COALESCE(SUM(tokens_cache_write),0) AS cache_write,
                   COALESCE(SUM(tokens_input+tokens_output+tokens_cache_read+tokens_cache_write),0) AS total,
                   printf('%.1f', {_credits()}) AS credits
            FROM sessions WHERE no_op = 0
        """)
    else:
        print("Usage: operator report "
              "[summary|sessions|models|projects|costs|tokens]\n")
        print("  summary   — AI credit totals (today, week, all time)")
        print("  sessions  — Last 20 sessions with details")
        print("  models    — Usage breakdown by AI model")
        print("  projects  — Usage breakdown by project directory")
        print("  costs     — Cost estimates in USD")
        print("  tokens    — Token counts by type")
        return 1

    print(_table(rows, headers))
    if subcmd == "costs":
        print(f"\nNote: billed in GitHub AI credits at ${_USD:.2f}/credit.")
        print("      Each plan includes a monthly credit allowance; these")
        print("      figures are gross usage, not net of that allowance.")
        print(f"      Rows predating 2026-06-01 are costed at "
              f"${_LEGACY_USD:.2f}/premium request.")
    return 0


def show_run_summary(run_started: str) -> None:
    if not run_started or file_present(METRICS_DB) is not True:
        return
    print("\n═══ Operator Run Summary ═══\n")
    try:
        rows, headers = _query(f"""
            SELECT COUNT(*) AS sessions,
                   printf('%.1f', {_credits()}) AS credits,
                   printf('$%.2f', {_usd()}) AS est_cost,
                   COALESCE(SUM(api_time_seconds) || 's','—') AS total_api_time,
                   COALESCE({_fmt_duration_sql('SUM(session_time_seconds)')},'—') AS total_sess_time,
                   COALESCE('+' || SUM(lines_added) || ' -' || SUM(lines_removed),
                            '—') AS total_changes
            FROM sessions WHERE no_op = 0 AND ended_at >= ?
        """, (run_started,))
    except sqlite3.Error as exc:
        # This runs on every shutdown path. A summary that cannot be read is
        # not a reason to end a clean shutdown with a traceback.
        print(f"  (metrics unavailable: {exc})")
        return
    print(_table(rows, headers))
    try:
        rows, headers = _query(f"""
            SELECT m.model_name AS model,
                   printf('%.1f', COALESCE(SUM(m.nano_aiu),0) / {_NANO}.0) AS credits,
                   COUNT(*) AS uses
            FROM model_usage m JOIN sessions s ON m.session_id = s.id
            WHERE s.no_op = 0 AND s.ended_at >= ?
            GROUP BY m.model_name ORDER BY SUM(m.nano_aiu) DESC
        """, (run_started,))
    except sqlite3.Error:
        return
    if rows:
        print()
        print(_table(rows, headers))


# ── launching ───────────────────────────────────────────────────
def project_catalog_path() -> Path:
    return projects_root() / "catalog.csv"


def project_handoff_file(cwd: Path) -> "Path | None | _CatalogUnreadable":
    """Resolve the handoff (``next-session.md``) path for a project directory.

    Looks the directory up in ``~/.copilot/projects/catalog.csv`` (the same
    catalog ``handoff``/``handoff_tool.py`` use) and returns the path the
    handoff file *would* live at, regardless of whether it currently exists.
    Returns None if the directory has no catalog entry at all, and
    :data:`CATALOG_UNREADABLE` if the catalog could not be read, which is a
    different answer and must not share a return value with the first.

    The lookup is keyed on the primary checkout, so running from a worktree
    finds the project's real entry instead of reporting it unregistered.

    The presence probe is spent on ``is False`` and only ``is False``, for the
    reason :func:`handoff_tool.resolve_guid` spells out against this same file:
    a denied *stat* does not imply a denied *read*, so a catalog sitting behind
    an unsearchable parent still gets opened. Gating the read on the stat could
    only ever subtract a lookup that would have succeeded -- measured: the stat
    raises EACCES while ``open`` hands the bytes over.
    """
    catalog = project_catalog_path()
    if file_present(catalog) is False:
        return None
    # "No row matched" is only an answer if every row was actually compared.
    undecided = False
    try:
        target = str(primary_repo_root(cwd).resolve())
    except (OSError, ValueError, RuntimeError):
        # Nothing can be compared against a target that will not resolve, so
        # every row below is undecided rather than unmatched. Reporting "not
        # registered" here would tell a restarting session its project has no
        # handoff, which is the one thing this must never say on a guess.
        return CATALOG_UNREADABLE
    if IS_WINDOWS:
        target = target.lower()
    try:
        with open(catalog, "r", encoding="utf-8", errors="replace", newline="") as fh:
            for row in catalog_rows(fh):
                if row is None:
                    # The line would not parse at all. Same reasoning as an
                    # unresolvable row below: it is a row not compared, not a
                    # row that failed to match.
                    undecided = True
                    continue
                if len(row) < 2:
                    continue
                path, guid = row[0].strip().strip('"'), row[1].strip().strip('"')
                # The same predicate the writer uses, imported rather than
                # copied: two definitions of "valid project id" that drift
                # apart is the very bug this rejects. A row the writer refuses
                # to create must not be one the reader will happily open --
                # `../../elsewhere` resolved two levels outside the projects
                # root, and on Windows `victim.` is `victim`, another
                # project's handoff.
                if not path or not guid_is_usable(guid):
                    continue
                try:
                    resolved = str(Path(path).resolve())
                except (OSError, ValueError, RuntimeError):
                    # This row could not be compared. Skipping it is right, but
                    # it means the "not registered" verdict below is no longer
                    # established for this catalog. All three arrive here: the
                    # catalog is a hand-edited CSV, so a row can name a symlink
                    # loop (RuntimeError, or OSError(ELOOP) on newer
                    # interpreters) or carry an embedded NUL (ValueError) just
                    # as easily as it can name a denied path (OSError).
                    undecided = True
                    continue
                if IS_WINDOWS:
                    resolved = resolved.lower()
                if resolved == target:
                    return project_dir(guid) / "next-session.md"
    except OSError:
        return CATALOG_UNREADABLE
    return CATALOG_UNREADABLE if undecided else None


def build_preamble(agent_name: str, instance: Instance, crash_recovery: bool = False) -> str:
    text = (
        "You are running under an automated operator wrapper that a human set up. "
        "Key facts: (1) You have blanket human approval for ALL decisions — tool calls, "
        "file edits, git operations, architectural choices. Do not ask for direction or "
        "confirmation. Make your best judgment call and proceed. If you are genuinely "
        "uncertain between approaches that have very different consequences, state your "
        "reasoning and pick one. (2) Session restart: when context gets heavy or a task is "
        "complete with next steps, use the handoff command: handoff --instance "
        f"{instance.display_name} --status \"what you completed\" --next \"what to do next\" "
        "--context \"key decisions and gotchas\" — this atomically writes the handoff file "
        "and triggers the restart. It works the same on every platform. (3) On startup: "
        "always check for a session handoff file to resume work. (4) You are the "
        f"@{agent_name} agent with --yolo permissions (all tools/files/URLs auto-approved). "
        f"(5) Operator instance: {instance.display_name}. "
        "Now: check for your session handoff and get to work."
    )
    if crash_recovery:
        text += (
            " (6) This session is being resumed because a handoff file could not be "
            "found for this project. Either a crash occurred or the previous session "
            "ended without the handoff being written. If you intended to end the "
            "session, please make sure you write a handoff first next time."
        )
    return text


def write_launch_spec(instance: Instance, argv: list[str], cwd: Path,
                      session_num: int) -> Path:
    spec = {
        "instance": instance.id,
        "display_name": instance.display_name,
        "argv": argv,
        "cwd": str(cwd),
        "session_num": session_num,
        "state_dir": str(RESTART_DIR),
        "metrics_db": str(METRICS_DB),
        "copilot_log_dir": str(COPILOT_LOG_DIR),
    }
    tmp = instance.spec_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    os.replace(tmp, instance.spec_file)
    return instance.spec_file


def runner_argv(spec_path: Path) -> list[str]:
    """Command the multiplexer runs inside the pane.

    Passed as an explicit argv list (never a shell string) so arguments keep
    their exact spelling regardless of platform quoting rules.
    """
    runner = Path(__file__).resolve().parent / "operator_runner.py"
    return [sys.executable, str(runner), str(spec_path)]


def copilot_executable() -> str | None:
    return shutil.which("copilot")


def _ensure_usage_logging(argv: list[str]) -> list[str]:
    """Ensure Copilot logs the data the metrics pipeline depends on.

    Since the move to AI credits, usage is reported in the chat-completion
    response bodies (``copilot_usage.total_nano_aiu``), and those bodies are
    only written at debug log level. At the default level the process log
    contains no usage data at all, so metrics would silently be empty.

    Set ``COPILOT_OPERATOR_NO_DEBUG_LOG=1`` to opt out — sessions will run with
    smaller logs but will record no usage.
    """
    if os.environ.get("COPILOT_OPERATOR_NO_DEBUG_LOG"):
        return argv
    if any(a == "--log-level" or a.startswith("--log-level=") for a in argv):
        return argv
    return [*argv, "--log-level", "debug"]


def start_session(instance: Instance, copilot_args: list[str], session_num: int,
                  remain_on_exit: bool, preamble: str = "") -> None:
    cwd = Path.cwd()
    exe = copilot_executable()
    if not exe:
        die("GitHub Copilot CLI ('copilot') was not found on PATH.\n"
            "  Install it: https://docs.github.com/en/copilot/how-tos/copilot-cli")

    argv = [exe, *copilot_args]
    if preamble:
        argv += ["-i", preamble]
    argv = _ensure_usage_logging(argv)

    remove_file(instance.restart_marker)
    remove_file(instance.exit_file)
    remove_file(instance.session_file)

    spec = write_launch_spec(instance, argv, cwd, session_num)

    log(f"Session #{session_num}: launching copilot")
    log(f"  Work dir: {cwd}")

    if MUX.has_session(instance.session):
        MUX.kill_session(instance.session)
        time.sleep(0.5)

    MUX.new_session(instance.session, str(cwd), runner_argv(spec))
    MUX.set_remain_on_exit(instance.session, remain_on_exit)

    instance.claim(uuid.uuid4().hex)

    # Wait briefly for the runner to publish Copilot's real PID.
    for _ in range(30):
        if instance.copilot_pid() is not None:
            break
        if path_present(instance.exit_file) is True:
            break
        time.sleep(0.2)

    pid = instance.copilot_pid()
    log(f"  Session #{session_num} running (copilot pid={pid or 'pending'}) — "
        f"attach with: operator join {instance.display_name}")


def _pid_alive(pid: int) -> bool:
    """Cross-platform "is this OS process still alive" check.

    Used for the background loop supervisor's own PID, which is a plain
    Python process — not a mux session or a Copilot child — so none of the
    mux/pane helpers apply.
    """
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _save_loop_args(instance: Instance, user_args: list[str]) -> None:
    """Record how loop mode was invoked so it can be reproduced later."""
    payload = {"user_args": list(user_args), "cwd": str(Path.cwd())}
    tmp = instance.loop_args_file.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, instance.loop_args_file)
    except OSError as exc:
        # Losing this costs a faithful restart-loop, never the running
        # session, so it must not take the supervisor down with it.
        log(f"  Warning: could not record loop args: {exc}")


def _load_loop_args(instance: Instance) -> tuple[list[str], str | None]:
    """The args loop mode was started with, plus its working directory.

    Returns ``([], None)`` when nothing was recorded — the caller decides
    whether that is fatal.
    """
    try:
        payload = json.loads(instance.loop_args_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None
    args = payload.get("user_args")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return [], None
    cwd = payload.get("cwd")
    return args, cwd if isinstance(cwd, str) else None


def _running_loop_pid(instance: Instance) -> int | None:
    """PID of instance's background loop supervisor, if one is alive.

    Prunes the pid file when it points at a dead process, so callers never
    have to special-case a stale record.
    """
    try:
        pid = int(instance.loop_pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if _pid_alive(pid):
        return pid
    remove_file(instance.loop_pid_file)
    return None


def is_copilot_running(instance: Instance) -> bool:
    """True while the session's Copilot process is still alive.

    Three signals, because any one alone can lie:

    * the runner's ``.exit`` marker is authoritative when present, but the
      runner writes it last and may be killed before it does;
    * ``has_session`` stays true after the program exits when
      ``remain-on-exit`` is on, which loop mode sets;
    * ``pane_dead`` catches exactly that case.

    Omitting ``pane_dead`` lets loop mode poll forever when the runner dies
    without writing its marker.

    Only a marker we can actually see ends the session. A probe that fails
    says nothing about whether Copilot is alive, and answering "exited" would
    make the supervisor tear down and relaunch a perfectly healthy session.
    """
    if path_present(instance.exit_file) is True:
        return False
    if not MUX.has_session(instance.session):
        return False
    return not MUX.pane_dead(instance.session)


def wait_for_exit(instance: Instance, timeout: int = EXIT_GRACE_SECONDS) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_copilot_running(instance):
            return True
        time.sleep(1)
    return False


def stop_session_gracefully(instance: Instance) -> None:
    """Ask Copilot to exit, wait, then force the session down.

    The bash version captured metrics while Copilot was still running, so the
    shutdown telemetry frequently did not exist yet and the record landed as a
    no-op. Here the runner writes metrics once Copilot has actually exited, so
    the only requirement is to end the process cleanly and wait for it.
    """
    if not MUX.has_session(instance.session):
        return
    try:
        MUX.send_keys(instance.session, "/exit")
    except MuxError as exc:
        # Asking politely is best-effort: the session can die between the
        # check above and the keystroke, and a backend that refuses the
        # keystroke has told us nothing except that this route is closed.
        # Fall through to the wait-then-kill path, which is what already
        # happened when the failure went unreported.
        log(f"  Could not send /exit ({exc}) — falling back to the kill path")
    if wait_for_exit(instance, EXIT_GRACE_SECONDS):
        return
    log("  Copilot did not exit within the grace period — terminating session")
    MUX.kill_session(instance.session)
    # Give the runner a moment to finish writing metrics.
    for _ in range(10):
        if path_present(instance.exit_file) is True:
            break
        time.sleep(0.5)


# ── argument helpers ────────────────────────────────────────────
def extract_agent_from_args(args: list[str]) -> str:
    for i, arg in enumerate(args):
        if arg.startswith("--agent="):
            return arg.split("=", 1)[1]
        if arg == "--agent" and i + 1 < len(args):
            return args[i + 1]
    return "anvil:anvil"


def args_have_explicit_session(args: list[str]) -> bool:
    return any(SESSION_ARG_RE.match(a) for a in args)


def has_agent_flag(args: list[str]) -> bool:
    return any(a == "--agent" or a.startswith("--agent=") for a in args)


def with_experimental(defaults: list[str]) -> list[str]:
    """Append `--experimental` to the operator's injected defaults.

    Runtime extensions -- `checkout-guard` among them -- load ONLY when the
    CLI is in experimental mode, and the CLI persists the last spelling it was
    given into `~/.copilot/settings.json`. So the flag is sticky global state
    that any other session, on any project, can flip; and when it is off,
    every extension silently does not load. There is no error and no missing
    output, because an extension that never loaded cannot report its own
    absence. That was measured on this machine: agent sessions ran for over an
    hour with no checkout-guard at all, in the shared primary checkout it
    exists to protect, and nothing inside those sessions could have told.

    Passing it explicitly on every launch is what makes the guard's silence
    mean "scanned and found nothing" rather than "was never there".

    It is added UNCONDITIONALLY, and callers must place the result BEFORE the
    user's own arguments. A user who really wants `--no-experimental` still
    gets it, because the CLI resolves conflicting spellings last-wins -- both
    orders were measured against CLI 1.0.77:

        copilot --experimental --no-experimental ...  -> experimental: false
        copilot --no-experimental --experimental ...  -> experimental: true

    Deciding by *inspecting* the user's arguments instead is what the earlier
    version of this function did, and it was wrong: it could not tell a flag
    from a value, so `-p --no-experimental` -- a prompt that merely looks like
    a ruling -- suppressed the injected flag and put the session straight back
    into the silent, guardless state this exists to prevent. Any such check
    needs a list of which options take values, and that list goes stale every
    time the CLI grows one. Ordering needs no list.
    """
    return [*defaults, "--experimental"]


def handle_existing_session(instance: Instance) -> None:
    if not MUX.has_session(instance.session):
        return
    owner = instance.ownership()
    if owner is None:
        die(f"A session named '{instance.session}' already exists but was not created "
            f"by the operator. Refusing to touch it.\n"
            f"  Choose another name with --name, or stop that session yourself.")
    print(f"Session '{instance.display_name}' is already running.", file=sys.stderr)
    try:
        answer = input("Stop it and start a new one? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer in ("y", "yes"):
        log(f"Stopping existing session '{instance.display_name}' at user request")
        MUX.kill_session(instance.session)
        instance.cleanup_files()
        time.sleep(1)
    else:
        print("Aborted.", file=sys.stderr)
        sys.exit(1)


# ── commands ────────────────────────────────────────────────────
def list_instances() -> int:
    """Non-interactive listing — the scriptable counterpart to bare
    ``operator``, which lists the same instances and then lets you act on
    one."""
    print("═══ Running Operator Instances ═══\n")
    instances = active_instances()
    for inst in instances:
        print(f"  {_instance_summary(instance_snapshot(inst))}")
    if not instances:
        print("  (none)")
    print("\nInspect: operator             (interactive: stats, join, stop)")
    print("Attach:  operator join <name>")
    print("Stop:    operator stop <name>")
    return 0


def _request_supervisor_stop(instance: Instance, timeout: float = 20.0) -> None:
    """If a background loop supervisor is running for instance, ask it to
    shut down (and take the session with it) before we touch anything else.

    This avoids a race where we kill the mux session ourselves while an
    unrelated background loop is still polling — without this, the
    supervisor would see the session vanish with no stop/restart marker and
    (correctly, in the crash case) relaunch a fresh one right underneath us.
    """
    pid = _running_loop_pid(instance)
    if pid is None:
        return
    instance.stop_marker.touch()
    log(f"  Stop signal sent to loop supervisor for '{instance.display_name}' (pid {pid})")
    deadline = time.time() + timeout
    while time.time() < deadline and _running_loop_pid(instance) is not None:
        time.sleep(0.5)


def stop_operator(target: str | None = None) -> int:
    log(f"Stop requested{f' for {target}' if target else ''}")
    if target:
        instance = Instance(target)
        if not instance.is_managed():
            print(f"No operator instance '{target}' found.", file=sys.stderr)
            print(file=sys.stderr)
            list_instances()
            return 1
        _request_supervisor_stop(instance)
        if MUX.has_session(instance.session):
            # A live session is only ours to kill if we hold a matching claim.
            # Continuity state alone must never authorize destroying a session
            # that merely shares the name.
            if not instance.owns_live_session():
                print(f"A session named '{instance.session}' is running but was not "
                      f"started by this operator. Refusing to stop it.", file=sys.stderr)
                print("  Remove the stale state with: operator forget "
                      f"{instance.display_name}", file=sys.stderr)
                return 1
            if not MUX.kill_session(instance.session):
                print(f"Failed to stop session '{instance.session}'.", file=sys.stderr)
                return 1
        instance.cleanup_files()
        remove_file(instance.state_file)
        remove_tab(instance.id)
        log(f"Stopped: {target}")
        return 0

    managed = managed_instances()
    live = set(MUX.list_sessions()) if MUX.available() else set()
    count = 0
    for ident in sorted(managed):
        if ident not in live:
            continue
        inst = Instance(managed[ident].get("display_name", ident))
        if not inst.owns_live_session():
            log(f"  Skipping {ident}: live session is not owned by this operator")
            continue
        _request_supervisor_stop(inst)
        if not MUX.kill_session(ident):
            log(f"  Failed to stop {ident}")
            continue
        inst.cleanup_files()
        remove_file(inst.state_file)
        remove_tab(inst.id)
        log(f"Stopped: {ident}")
        count += 1
    if count == 0:
        print("No running operator instances found.")
    else:
        log(f"Stopped {count} instance(s)")
    return 0


def stop_loop_only(target: str | None) -> int:
    """Stop just the background loop supervisor, leaving its Copilot session
    (if any) running untouched. Counterpart to stop_session_only."""
    if not target:
        print("Usage: operator stop-loop NAME", file=sys.stderr)
        return 1
    instance = Instance(target)
    pid = _running_loop_pid(instance)
    if pid is None:
        print(f"No background loop supervisor is running for '{target}'.", file=sys.stderr)
        return 1
    instance.detach_marker.touch()
    log(f"Detach requested for loop '{target}' (pid {pid})")
    deadline = time.time() + 20
    while time.time() < deadline:
        if _running_loop_pid(instance) is None:
            print(f"Loop supervisor for '{instance.display_name}' stopped; "
                  f"session left running.")
            print(f"  Re-attach: operator join {instance.display_name}")
            return 0
        time.sleep(0.5)
    print(f"Loop supervisor for '{target}' did not stop within 20s.", file=sys.stderr)
    return 1


@contextmanager
def _restart_handoff_lock(instance: Instance):
    """Serialise supervisor handoffs for one instance.

    Yields True when the lock was taken, False when another handoff is
    already in progress.
    """
    with _exclusive_lock(instance.restart_lock_file) as acquired:
        yield acquired


def restart_loop(target: str | None) -> int:
    """Replace an instance's loop supervisor, leaving its session running.

    The supervisor is a long-lived process that imported the operator's code
    at startup, so it keeps running the code it started with — `operator stop`
    would pick up new code but takes the Copilot session down with it. This
    swaps only the supervisor: the old one is asked to detach, and a new one
    adopts the still-running session.
    """
    if not target:
        print("Usage: operator restart-loop NAME", file=sys.stderr)
        print("Replaces the loop supervisor (picking up new operator code) "
              "without stopping the Copilot session.", file=sys.stderr)
        return 1
    instance = Instance(target)

    if not MUX.has_session(instance.session):
        print(f"No running session '{target}'. Nothing to keep alive — "
              f"start it with: operator --loop --name {target}", file=sys.stderr)
        return 1
    if not instance.owns_live_session():
        print(f"A session named '{instance.session}' is running but was not "
              f"started by this operator. Refusing to touch it.", file=sys.stderr)
        print(f"  Drop stale state with: operator forget {instance.display_name}",
              file=sys.stderr)
        return 1

    # Everything that can be known to be wrong is checked *before* the old
    # supervisor is retired, so a rejected restart leaves the instance exactly
    # as it found it.
    user_args, recorded_cwd = _load_loop_args(instance)
    if recorded_cwd is None:
        print(f"No recorded loop arguments for '{target}'. This instance was "
              f"started by an operator that predates restart-loop.",
              file=sys.stderr)
        print("  Its next session would lose its original arguments, so it is "
              "safer to restart it yourself. Run this from "
              f"{instance.display_name}'s working directory — --adopt is what "
              "keeps the running session alive:", file=sys.stderr)
        print(f"    operator stop-loop {instance.display_name}", file=sys.stderr)
        print(f"    operator --loop --headless --adopt "
              f"--name {instance.display_name} [original args]", file=sys.stderr)
        print("  That records the arguments, so future restarts can just use: "
              f"operator restart-loop {instance.display_name}", file=sys.stderr)
        return 1
    if dir_present(Path(recorded_cwd)) is False:
        # Spawning from the caller's cwd instead would silently point the
        # instance at a different project. A directory that merely cannot be
        # examined is not known to be gone, so it does not earn this refusal.
        print(f"The directory '{target}' was started in no longer exists:",
              file=sys.stderr)
        print(f"  {recorded_cwd}", file=sys.stderr)
        print("  Refusing to restart it somewhere else. Restore the directory, "
              "or stop the instance and start it where you want it.",
              file=sys.stderr)
        return 1

    with _restart_handoff_lock(instance) as acquired:
        if not acquired:
            print(f"Another restart of '{target}' is already in progress.",
                  file=sys.stderr)
            return 1
        return _do_restart_loop(instance, user_args, recorded_cwd)


def _do_restart_loop(instance: Instance, user_args: list[str],
                     recorded_cwd: str) -> int:
    """The handoff itself. Runs holding the per-instance restart lock."""
    target = instance.display_name
    pid = _running_loop_pid(instance)
    if pid is not None:
        instance.detach_marker.touch()
        log(f"Restart requested for loop '{target}' (pid {pid})")
        # Budget derived from how long the supervisor can take to look at the
        # marker, so tuning the poll interval cannot silently break this.
        budget = SESSION_ID_WAIT + POLL_INTERVAL * 2 + 15
        deadline = time.time() + budget
        while time.time() < deadline:
            if _running_loop_pid(instance) is None:
                break
            time.sleep(0.5)
        else:
            remove_file(instance.detach_marker)
            print(f"Loop supervisor for '{target}' did not stop within "
                  f"{budget}s. Session left untouched; no new supervisor "
                  f"started.", file=sys.stderr)
            return 1

        # The supervisor is gone, but *why* it went matters. If the detach
        # marker is still sitting there it never consumed our request, so it
        # exited for its own reasons — most likely a concurrent `operator
        # stop`, which also takes the session down. Spawning an adopting
        # supervisor now would resurrect a session the user just stopped.
        # A marker we cannot examine is not proof it was consumed, and the
        # cost of guessing wrong here is a session coming back from the dead,
        # so anything but a definite absence refuses.
        if path_present(instance.detach_marker) is not False:
            remove_file(instance.detach_marker)
            print(f"The supervisor for '{target}' exited without taking the "
                  f"restart request — something else stopped it.",
                  file=sys.stderr)
            print("  Not starting a replacement.", file=sys.stderr)
            return 1
        print(f"Old supervisor (pid {pid}) stopped.")
    else:
        print(f"No supervisor was running for '{target}' — starting one.")

    # Re-check rather than trust the check from before the handoff: `operator
    # stop` may have killed the session while we waited.
    if not MUX.has_session(instance.session):
        print(f"Session '{target}' disappeared during the restart — it was "
              f"stopped by something else. Not starting a replacement.",
              file=sys.stderr)
        return 1

    try:
        _spawn_background_loop(instance, user_args, is_fresh=False, adopt=True,
                               cwd=recorded_cwd)
    except OSError as exc:
        # The old supervisor is already gone, so failing here is the one
        # outcome worth shouting about: the session is alive and unwatched.
        print(f"Could not start a replacement supervisor for '{target}': {exc}",
              file=sys.stderr)
        print(f"  The Copilot session is still running but is NOT supervised.",
              file=sys.stderr)
        print(f"  Retry: operator restart-loop {target}", file=sys.stderr)
        return 1

    # Confirm it actually came up: a supervisor that died on startup would
    # otherwise leave the session unsupervised while we report success. Wait
    # for the pid *file*, not the spawned pid — on Windows sys.executable is
    # often a launcher shim that exits once the real interpreter is running,
    # so the pid Popen hands back may be dead while the supervisor is fine.
    deadline = time.time() + 20
    while time.time() < deadline:
        new_pid = _running_loop_pid(instance)
        if new_pid is not None:
            print(f"✅ Loop supervisor for '{target}' replaced "
                  f"(pid {new_pid}); session kept running.")
            print(f"  Attach: operator join {target}")
            return 0
        time.sleep(0.5)
    print(f"New supervisor for '{target}' did not come up. The Copilot session "
          f"is still running but is no longer supervised.", file=sys.stderr)
    print(f"  Check the log for details: {LOG_FILE}", file=sys.stderr)
    print(f"  Retry: operator restart-loop {target}", file=sys.stderr)
    return 1


def stop_session_only(target: str | None) -> int:
    """Stop just the Copilot session, leaving its loop supervisor (if any)
    running so it can relaunch a fresh session. Counterpart to stop_loop_only."""
    if not target:
        print("Usage: operator stop-session NAME", file=sys.stderr)
        return 1
    instance = Instance(target)
    if not MUX.has_session(instance.session):
        print(f"No running session '{target}'.", file=sys.stderr)
        return 1
    if not instance.owns_live_session():
        print(f"A session named '{instance.session}' is running but was not "
              f"started by this operator. Refusing to stop it.", file=sys.stderr)
        print(f"  Remove the stale state with: operator forget {instance.display_name}",
              file=sys.stderr)
        return 1
    if not MUX.kill_session(instance.session):
        print(f"Failed to stop session '{instance.session}'.", file=sys.stderr)
        return 1
    pid = _running_loop_pid(instance)
    if pid:
        print(f"Session '{instance.display_name}' stopped; "
              f"loop supervisor (pid {pid}) will relaunch it shortly.")
    else:
        print(f"Session '{instance.display_name}' stopped. "
              f"No loop supervisor is running, so it will not restart automatically.")
        remove_tab(instance.id)
    log(f"Stopped session only: {target}")
    return 0


def forget_instance(target: str | None) -> int:
    """Delete an instance's operator state without touching any session."""
    if not target:
        print("Usage: operator forget NAME", file=sys.stderr)
        return 1
    instance = Instance(target)
    if not instance.is_managed():
        print(f"No operator state for '{target}'.", file=sys.stderr)
        return 1
    instance.cleanup_files()
    remove_file(instance.state_file)
    remove_tab(instance.id)
    print(f"Removed operator state for '{instance.display_name}'.")
    print("Any running session with that name was left untouched.")
    return 0


def join_instance(target: str | None) -> int:
    if not target:
        return list_instances()
    instance = Instance(target)
    if MUX.has_session(instance.session):
        set_tab_title(f"terminal - {instance.display_name}")
        set_tab_progress(TAB_LOOPING if _running_loop_pid(instance) else TAB_STEADY)
        MUX.attach(instance.session)
        return 0
    print(f"No instance '{target}' found.", file=sys.stderr)
    print(file=sys.stderr)
    list_instances()
    return 1


def reload_instance(target: str | None) -> int:
    if not target:
        print("Usage: operator reload NAME", file=sys.stderr)
        print("Re-generates the launch spec for an instance using the current operator.",
              file=sys.stderr)
        return 1
    instance = Instance(target)
    if path_present(instance.spec_file) is False:
        die(f"No launch spec found for '{target}' at {instance.spec_file}")
    try:
        spec = json.loads(instance.spec_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # The spec is about to be rewritten from what was read. Reading it as
        # "empty" would replace a working launch spec with one that launches
        # nothing, so a failed read has to stop the command instead.
        die(f"Could not read the launch spec at {instance.spec_file}: {exc}")
    if not isinstance(spec, dict):
        die(f"The launch spec at {instance.spec_file} is not a JSON object.")
    argv = list(spec.get("argv", []))
    agent = extract_agent_from_args(argv)
    preamble = build_preamble(agent, instance)
    if "-i" in argv:
        idx = argv.index("-i")
        argv = argv[:idx]
    if "--effort" not in argv:
        argv += ["--effort", "high"]
    argv += ["-i", preamble]
    spec["argv"] = argv
    tmp = instance.spec_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    os.replace(tmp, instance.spec_file)
    log(f"Reloaded launch spec for {target}")
    print(f"✅ Launch spec updated: {instance.spec_file}")
    print("   Changes take effect on next copilot restart.")
    return 0


def ingest_all_logs(force: bool = False) -> int:
    operator_ingest.init_db(METRICS_DB)
    results = operator_ingest.ingest_all(COPILOT_LOG_DIR, METRICS_DB, force=force)
    if results is None:
        print(f"Cannot examine {COPILOT_LOG_DIR} — no logs were ingested, and "
              f"whether any are there is unknown")
        return 1
    if not results:
        print(f"No Copilot logs found in {COPILOT_LOG_DIR}")
        return 0
    for line in results:
        print(line)
    return 0


def _log_files() -> "list[Path] | None":
    """The process logs, or None when the directory could not be examined.

    An empty list is a census: it says the directory was read and found to
    hold nothing. A denied stat or a failed glob establishes no such thing,
    and the caller spends the empty list as "No Copilot logs found" -- a
    failed read arriving as a confident statement about the filesystem, which
    is the empty-population bug this module keeps having to unlearn.
    """
    present = dir_present(COPILOT_LOG_DIR)
    if present is None:
        return None
    if present is False:
        return []
    try:
        return sorted(COPILOT_LOG_DIR.glob("process-*.log"))
    except OSError:
        return None


def manage_logs(args: list[str]) -> int:
    """Report on, and optionally prune, Copilot's process logs.

    The operator runs Copilot at debug level so usage data exists at all, which
    makes logs considerably larger. Copilot does not rotate them, so this gives
    a way to see and reclaim the space.

    Pruning is never automatic: logs are the only record of usage, and deleting
    them silently would destroy data the user may not have ingested yet.
    """
    prune = "--prune" in args
    days = 30
    for i, a in enumerate(args):
        if a == "--days" and i + 1 < len(args):
            try:
                days = int(args[i + 1])
            except ValueError:
                die("--days requires a whole number")
        elif a.startswith("--days="):
            try:
                days = int(a.split("=", 1)[1])
            except ValueError:
                die("--days requires a whole number")

    files = _log_files()
    if files is None:
        print(f"Could not examine {COPILOT_LOG_DIR} — not reporting whether "
              f"logs are present there.")
        return 1
    if not files:
        print(f"No Copilot logs found in {COPILOT_LOG_DIR}")
        return 0

    total = sum(f.stat().st_size for f in files)
    print(f"Copilot logs in {COPILOT_LOG_DIR}")
    print(f"  files: {len(files)}")
    print(f"  size:  {total / 1_048_576:.1f} MB")

    cutoff = time.time() - days * 86400
    old = [f for f in files if f.stat().st_mtime < cutoff]
    old_size = sum(f.stat().st_size for f in old)

    if not prune:
        print(f"\n  older than {days} days: {len(old)} files "
              f"({old_size / 1_048_576:.1f} MB)")
        if old:
            print(f"  remove them with: operator logs --prune --days {days}")
        return 0

    if not old:
        print(f"\nNothing older than {days} days to prune.")
        return 0

    # Only prune what has already been recorded, so usage is never lost.
    operator_ingest.init_db(METRICS_DB)
    with operator_ingest.connect(METRICS_DB) as conn:
        known = {r["log_file"] for r in
                 conn.execute("SELECT log_file FROM sessions WHERE log_file IS NOT NULL")}

    removed = skipped = 0
    freed = 0
    for f in old:
        # The full path only. `known` also holds bare basenames, from rows
        # written before a log was keyed by path, and accepting those here
        # would delete a log this database has no record of: a legacy row
        # names a file in a directory nobody wrote down, so a same-named log
        # in the current one matches it without being it. That is the loss
        # this function exists to prevent -- the log is the only record of the
        # session, and it would be gone before it could ever be ingested.
        # A legacy row is re-keyed the next time its log is ingested, so the
        # cost of the strict test is that an old database prunes nothing until
        # `operator ingest` has run once, which is what the line below tells
        # the user to do.
        if operator_ingest.log_key(f) not in known:
            skipped += 1
            continue
        freed += f.stat().st_size
        try:
            f.unlink()
            removed += 1
        except OSError as exc:
            print(f"  could not remove {f.name}: {exc}", file=sys.stderr)

    print(f"\nRemoved {removed} ingested log(s), freed {freed / 1_048_576:.1f} MB.")
    if skipped:
        print(f"Kept {skipped} log(s) not yet ingested — run 'operator ingest' first.")
    return 0


# ── agent-to-agent messaging ────────────────────────────────────
def _instance_is_known(instance: Instance) -> bool:
    """True when this name refers to an instance the operator has seen.

    Used to reject a mistyped recipient. A message addressed to a name nobody
    answers to would sit in a directory forever with nothing to report it, so
    this fails closed and lists the real names instead.
    """
    if instance.is_managed():
        return True
    if instance.id in load_tabs():
        return True
    try:
        return MUX.has_session(instance.session)
    except MuxError:
        return False


def _can_receive_live(instance: Instance) -> bool:
    """True when there is a running Copilot session to type into.

    All three conditions matter: between sessions a loop keeps its state and
    (with remain-on-exit) even its mux session, but the pane is dead and
    anything typed into it would be swallowed silently.
    """
    try:
        if not MUX.has_session(instance.session):
            return False
        if MUX.pane_dead(instance.session):
            return False
    except MuxError:
        return False
    return is_copilot_running(instance)


SEND_FLAGS = ("--from", "--to", "--force", "--queue")
INBOX_FLAGS = ("--peek", "--history", "--json")
HELP_FLAGS = ("-h", "--help", "-?")


def _send_usage(stream=None) -> None:
    stream = sys.stderr if stream is None else stream
    print('Usage: operator send --from NAME --to NAME "message"', file=stream)
    print("  --from and --to are both required so the recipient knows who "
          "wrote and who to reply to.", file=stream)
    print("  --queue  leave it for the next session even if one is running",
          file=stream)
    print("  --force  send to a name the operator does not recognize",
          file=stream)
    print("  --       everything after it is message text, flags and all",
          file=stream)


def send_message(args: list[str]) -> int:
    """``operator send --from NAME --to NAME "message"``.

    An option this function does not recognize is refused rather than folded
    into the message body. Silently treating ``--dry-run`` as text delivers a
    message to a sender who believes nothing was sent, which is worse than
    not sending at all. Use ``--`` when the text really does start with a
    dash.
    """
    if args[:1] and args[0] in HELP_FLAGS:
        _send_usage(sys.stdout)
        return 0

    sender = ""
    recipient = ""
    force = False
    queue_only = False
    body: list[str] = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            body.extend(args[i + 1:])
            break
        if arg.startswith("-") and not (
                arg in SEND_FLAGS or arg.startswith(("--from=", "--to="))):
            print(f"operator send: unknown option '{arg}'", file=sys.stderr)
            print("  If it belongs to the message, put it after --:",
                  file=sys.stderr)
            print('    operator send --from a --to b -- '
                  f'"{arg} ..."', file=sys.stderr)
            print("  Nothing was sent.", file=sys.stderr)
            _send_usage()
            return 2
        if arg in ("--from", "--to"):
            if i + 1 >= len(args) or not args[i + 1]:
                print(f"{arg} requires a value", file=sys.stderr)
                return 2
            if arg == "--from":
                sender = args[i + 1]
            else:
                recipient = args[i + 1]
            i += 1
        elif arg.startswith("--from="):
            sender = arg.split("=", 1)[1]
        elif arg.startswith("--to="):
            recipient = arg.split("=", 1)[1]
        elif arg == "--force":
            force = True
        elif arg == "--queue":
            queue_only = True
        else:
            body.append(arg)
        i += 1

    text = " ".join(body).strip()
    if not sender or not recipient or not text:
        _send_usage()
        return 2

    target = Instance(recipient)
    if not _instance_is_known(target) and not force:
        print(f"No operator instance '{recipient}' found — not sending.",
              file=sys.stderr)
        print("  Use --force to queue it anyway for an instance that has not "
              "started yet.\n", file=sys.stderr)
        list_instances()
        return 1

    msg = operator_mail.new_message(sender, target.display_name, target.id, text)

    if not queue_only and _can_receive_live(target):
        try:
            MUX.send_keys(target.session, operator_mail.render_line(msg))
        except MuxError as exc:
            print(f"Live delivery failed ({exc}) — queueing instead.",
                  file=sys.stderr)
        else:
            operator_mail.record_delivered(OPERATOR_HOME, msg)
            log(f"Message delivered live: {sender} -> {target.display_name}")
            print(f"Delivered to '{target.display_name}' (session is live).")
            return 0

    operator_mail.queue(OPERATOR_HOME, msg)
    log(f"Message queued: {sender} -> {target.display_name}")
    print(f"Queued for '{target.display_name}' — it will be delivered at the "
          f"start of its next session.")
    print(f"  Pending: {operator_mail.pending_count(OPERATOR_HOME, target.id)}")
    return 0


def _inbox_usage(stream=None) -> None:
    stream = sys.stderr if stream is None else stream
    print("Usage: operator inbox [NAME] [--peek|--history|--json]",
          file=stream)
    print("  --peek     show unread mail without marking it read",
          file=stream)
    print("  --history  show mail that was already read", file=stream)
    print("  --json     machine-readable output", file=stream)
    print("  --         the next argument is a mailbox name, dash or not",
          file=stream)
    print("  With no NAME, the mailbox is named after this directory — which "
          "is\n  nobody in particular. A destructive read is refused when any "
          "other\n  instance is live here; pass your own name.", file=stream)


def _dir_matches(child: str | None, parent: Path) -> bool | None:
    """True when ``child`` is ``parent`` or lives beneath it, None when the
    two could not be compared at all.

    Path comparison follows the convention used elsewhere in this file:
    resolve first, then lowercase on Windows, where ``C:\\Repo`` and
    ``c:\\repo`` are the same directory.

    Three answers rather than two, for the reason
    :func:`install_manifest.path_present` gives about the presence probes:
    ``False`` here does not mean "could not tell", it means *somewhere else*,
    and that is a placement this comparison is in no position to make. The
    only caller is the census behind a destructive mail read, where an
    instance reported elsewhere is an instance that does not stop the read.

    ``Path.resolve`` declines to answer in three different ways, and the
    handler this replaced caught one of them:

    * a **symlink loop** raises ``RuntimeError`` on the interpreters this
      project supports and ``OSError(ELOOP)`` on newer ones -- so both are
      caught, and which one arrives is a version detail, not a behaviour;
    * an **embedded NUL** raises ``ValueError``. The recorded directory comes
      out of a hand-editable launch-spec JSON, and JSON carries ``\\u0000``
      happily;
    * everything else -- a denial, a disconnected network home, WINERROR 21 --
      raises ``OSError``, which *was* caught and answered ``False``.

    The first two were not caught at all, so ``operator inbox`` ended in a
    traceback rather than a decision. ``parent`` is resolved here too, so a
    parent that will not resolve made *every* comparison answer "elsewhere"
    and the census came back confidently empty.
    """
    if not child:
        return False
    try:
        cp = str(Path(child).resolve())
        pp = str(parent.resolve())
    except (OSError, ValueError, RuntimeError):
        return None
    if IS_WINDOWS:
        cp, pp = cp.lower(), pp.lower()
    if cp == pp:
        return True
    try:
        Path(cp).relative_to(Path(pp))
        return True
    except ValueError:
        return False


def live_instance_ids_under(cwd: Path) -> list[str] | None:
    """Ids of live sessions whose work is in or under ``cwd``.

    Returns **ids, never display names**. The two are not interchangeable:
    ``safe_instance_id`` appends a digest when sanitizing changes a name and
    is therefore not idempotent, so an id passed back through it becomes a
    third, non-existent instance. Identity is decided in id space and display
    names are used only for the message a human reads.

    "Under" and not "equal to" because a git worktree lives *inside* the
    primary checkout: an agent in ``.worktrees/fix-thing`` is working on the
    same project as one sitting at the repo root, and from the root it is
    exactly the peer whose mail must not be eaten.

    Returns ``None`` when the census could not be taken at all — a backend
    that errors or is missing its binary tells us nothing about who is here,
    and "I could not look" must not read the same as "nobody is here".
    Note that an *unavailable* multiplexer is not uncertainty: with no
    backend there are no sessions to miss, so that answers the empty list.
    The same rule applies one record down: an unreadable launch spec, tab
    registry or state directory refuses the census rather than quietly
    dropping the instance it could not place. So does a directory that will
    not compare — a path the backend reported happily and :func:`_dir_matches`
    cannot resolve places an instance nowhere, which is not the same as
    placing it elsewhere.
    """
    try:
        if not MUX.available():
            return []
        live = sorted(MUX.list_sessions())
    except (MuxError, OSError):
        return None

    # The population itself has to be readable. An unexaminable state
    # directory or an unreadable tab registry would otherwise shrink `known`
    # silently, and `known` is what decides whether an unplaceable instance
    # is one of ours.
    managed = read_managed_instances()
    if managed is None:
        return None
    tabs = read_tabs()
    if tabs is None:
        return None

    known = set(managed) | set(tabs)
    found: list[str] = []
    for ident in live:
        try:
            pane = MUX.pane_current_path(ident)
        except (MuxError, OSError):
            pane = None
            pane_failed = True
        else:
            pane_failed = False
        recorded = _tracked_cwd_for_id(ident)
        pane_here = _dir_matches(pane, cwd)
        if pane_here:
            found.append(ident)
            continue
        if recorded is UNPLACEABLE:
            # The launch spec or the tab registry is there and would not be
            # read. The pane did not place this instance here either, so the
            # only honest answer is that we do not know who is in this
            # directory — the same refusal as a failed pane lookup, one
            # record further down.
            return None
        recorded_here = _dir_matches(recorded, cwd)
        if recorded_here:
            found.append(ident)
        elif (pane_here is None or recorded_here is None) and ident in known:
            # The backend answered, and the answer was a path that cannot be
            # compared to this directory — a symlink loop, an embedded NUL, a
            # denial part-way down. `pane_failed` is False, so without this
            # the instance would fall past every branch below and simply not
            # be in the list: the same hole in the census as a failed pane
            # lookup, arriving one step later.
            return None
        elif pane_failed and ident in known:
            # The backend refused to say where this instance is and the
            # recorded directory does not place it here either. That is not
            # evidence of absence, and a census with a hole in it must not be
            # returned as though it were complete.
            return None
        elif pane is None and recorded is None and ident in known:
            # An operator session nobody can place. Counting it costs the
            # caller one explicit name; missing it costs somebody their mail,
            # so an instance of unknown location is treated as present.
            found.append(ident)
    return found


def _name_has_live_session(instance: Instance) -> bool | None:
    """Whether a session by this name is running. ``None`` if unanswerable.

    An *absent* multiplexer answers False rather than None: with no backend
    installed nothing can be running under it, so this is knowledge, not
    uncertainty. Anything else that goes wrong is uncertainty.
    """
    try:
        if not MUX.available():
            return False
        return MUX.has_session(instance.session)
    except (MuxError, OSError):
        return None


def _display_name_for_id(ident: str) -> str:
    """A human label for a session id. Never used to decide identity."""
    meta = managed_instances().get(ident, {})
    name = meta.get("display_name")
    if not name:
        spec = RESTART_DIR / f"{ident}.launch.json"
        try:
            name = json.loads(spec.read_text(encoding="utf-8")).get("display_name")
        except (OSError, ValueError):
            name = None
    return name or ident


def show_inbox(args: list[str]) -> int:
    """``operator inbox [NAME]`` — read messages addressed to an instance.

    Reading is destructive by default: it archives everything it shows. So an
    argument this function does not understand is refused rather than
    ignored — a typo'd ``--peek`` that fell through to the default would
    consume the mailbox it was asked to leave alone, and the next reader
    could not tell that from an empty inbox.

    Nothing stops an instance being named ``-beta``, so ``--`` ends the
    options and makes what follows a name.

    With no NAME the mailbox is named after the working directory, and that
    name answers "what would a session started here be called", not "who am
    I". In a checkout shared by several agents it names *nobody* — it is just
    the folder — so consuming it takes whichever peer happens to hold that
    name. A destructive read of a directory-derived name is therefore refused
    while any other instance is live here. ``--peek`` and ``--history`` change
    nothing and stay available.
    """
    flags: list[str] = []
    names: list[str] = []
    end_of_options = False
    for arg in args:
        if end_of_options:
            names.append(arg)
        elif arg == "--":
            end_of_options = True
        elif arg.startswith("-"):
            flags.append(arg)
        else:
            names.append(arg)

    if any(f in HELP_FLAGS for f in flags):
        _inbox_usage(sys.stdout)
        return 0

    unknown = [f for f in flags if f not in INBOX_FLAGS]
    if unknown:
        print(f"operator inbox: unknown option '{unknown[0]}'",
              file=sys.stderr)
        if unknown[0].startswith("-") and not unknown[0].startswith("--"):
            print("  If it is a mailbox name, put it after --:",
                  file=sys.stderr)
            print(f"    operator inbox -- {unknown[0]}", file=sys.stderr)
        print("  No mail was read.", file=sys.stderr)
        _inbox_usage()
        return 2

    peek = "--peek" in flags
    as_json = "--json" in flags
    want_history = "--history" in flags

    if len(names) > 1:
        print("operator inbox reads one mailbox at a time, got: "
              f"{', '.join(repr(n) for n in names)}", file=sys.stderr)
        print("  No mail was read.", file=sys.stderr)
        _inbox_usage()
        return 2
    if names and not names[0].strip():
        print("operator inbox: the mailbox name is empty.", file=sys.stderr)
        print("  No mail was read.", file=sys.stderr)
        _inbox_usage()
        return 2

    if names:
        name = names[0]
        derived = False
    else:
        name = default_instance_name()
        derived = True

    instance = Instance(name)

    # Keyed on destructiveness, not on output format: consume() runs before
    # the --json branch below, so `operator inbox --json` archives too.
    destructive = not peek and not want_history
    if derived and destructive:
        cwd = Path.cwd()
        here = live_instance_ids_under(cwd)
        name_is_live = _name_has_live_session(instance)

        def _refuse(reason: str) -> int:
            print(f"operator inbox: refusing to consume mail for "
                  f"'{instance.display_name}'.", file=sys.stderr)
            print("  You did not name a mailbox, so this one was named after "
                  "the directory —", file=sys.stderr)
            print(f"  which is not the same thing as naming you. {reason}",
                  file=sys.stderr)
            print("  Reading archives what it shows, so a wrong guess eats a "
                  "peer's mail and", file=sys.stderr)
            print("  leaves an emptied mailbox that looks exactly like an "
                  "empty one.", file=sys.stderr)
            print("  Read yours:      operator inbox <your-name>",
                  file=sys.stderr)
            print("  Look, keep mail: operator inbox --peek", file=sys.stderr)
            print("  No mail was read.", file=sys.stderr)
            return 2

        if here is None or name_is_live is None:
            # Cannot tell who is working here, so cannot tell whose mailbox
            # this is. Refusing costs a name; guessing costs somebody's mail.
            return _refuse("The multiplexer could not be asked who is live "
                           "here, so this cannot be checked.")
        if any(ident != instance.id for ident in here):
            # Report every live instance, the derived one included: telling a
            # caller "live here: beta" while a session named after the folder
            # is also running reads as the operator having lost track of it.
            live_here = ", ".join(_display_name_for_id(i) for i in here)
            return _refuse(f"Live here: {live_here}.")
        if name_is_live and instance.id not in here:
            return _refuse(f"'{instance.display_name}' is live, but is not "
                           "working in this directory — the folder and the "
                           "mailbox belong to different agents.")

    if want_history:
        msgs = operator_mail.history(OPERATOR_HOME, instance.id)
    elif peek:
        msgs = operator_mail.pending(OPERATOR_HOME, instance.id)
    else:
        msgs = operator_mail.consume(OPERATOR_HOME, instance.id)

    if as_json:
        print(json.dumps(msgs, indent=2))
        return 0

    label = "history" if want_history else "messages"
    print(f"═══ Inbox for '{instance.display_name}' ({len(msgs)} {label}) ═══\n")
    print(operator_mail.render_for_terminal(msgs))
    if msgs and not peek and not want_history:
        print("\n(These are now marked read.)")
    return 0


def run_single_session(instance: Instance, copilot_args: list[str],
                       headless: bool = False) -> int:
    # `--yolo` waives every approval prompt for the life of the session, and
    # whether that is right here turns entirely on whether a human is watching.
    #
    # ATTACHED (the default): no `--yolo`. Your terminal is attached, so you
    # are sitting there to answer. `operator.sh` never injected it in this
    # mode and the Python operator used to, which meant the same command
    # granted an agent blanket approval on one platform and not the other --
    # a difference nobody reads the source to discover. Converged on the lower
    # authority. `operator.sh` has no headless mode at all, so the branch
    # below is Python-only by construction and cannot re-open that gap.
    #
    # HEADLESS: `--yolo` AND `--no-ask-user`, and the `headless` condition is
    # load-bearing -- do not fold this back into one list. Nothing attaches a
    # terminal here; `operator join` is an invitation the user may never
    # accept. Without them the session does not degrade to "more prompts", it
    # blocks on the first question forever, and it does so while looking
    # exactly like a session doing long work: live process, live pane, no
    # error anywhere. The safer-looking option is the one that fails silently
    # and unrecoverably, which is why the asymmetry decides it the other way
    # round from the attached case.
    #
    # The two flags close two different mouths and both are needed. `--yolo`
    # waives approval prompts the CLI raises before acting; `--no-ask-user`
    # stops the agent choosing to ask a question of its own accord. Granting
    # only the first leaves the identical hang reachable through `ask_user`,
    # which is exactly why loop mode -- the other unattended mode -- has
    # always injected both.
    #
    # Either way a user who passes these themselves is honoured: their
    # arguments land after these, and the CLI resolves last-wins.
    grants = ["--yolo", "--no-ask-user"] if headless else []
    args = [*with_experimental([*grants, "--autopilot", "--effort", "high"]),
            *copilot_args]
    handle_existing_session(instance)
    operator_ingest.init_db(METRICS_DB)
    run_started = utcnow()
    log(f"Starting single session: {instance.display_name}")

    start_session(instance, args, 1, remain_on_exit=False)
    if headless:
        print(f"Session '{instance.display_name}' started headless.")
        print(f"  Attach:  operator join {instance.display_name}")
        print(f"  Message: operator send --from <you> --to {instance.display_name} \"...\"")
        print("  Metrics are captured by the session supervisor when copilot exits.")
        return 0
    set_tab_title(f"operator - {instance.display_name}")
    set_tab_progress(TAB_STEADY)
    MUX.attach(instance.session)

    if MUX.has_session(instance.session) and path_present(instance.exit_file) is not True:
        print()
        print("Detached from copilot session.")
        print(f"  Re-attach: operator join {instance.display_name}")
        print("  Metrics are captured by the session supervisor when copilot exits.")
        return 0

    # Session finished; the runner has already ingested metrics.
    wait_for_exit(instance, 10)
    show_run_summary(run_started)
    instance.cleanup_files()
    return 0


def run_loop_mode(instance: Instance, user_args: list[str], is_fresh: bool,
                  adopt: bool = False) -> int:
    """Supervise an instance, restarting Copilot until asked to stop.

    ``adopt`` takes over a session that is already running instead of
    launching one. That is what lets a supervisor be replaced — to pick up new
    operator code, say — without disturbing the Copilot session it was
    watching. Everything after the initial launch is identical either way.
    """
    copilot_args = with_experimental(
        ["--yolo", "--autopilot", "--no-ask-user", "--effort", "high"])
    agent = extract_agent_from_args(user_args)
    if not has_agent_flag(user_args):
        copilot_args += ["--agent", agent]
    copilot_args += user_args

    operator_ingest.init_db(METRICS_DB)

    start_session_num = 1
    run_started = utcnow()
    resume_id = ""
    if not is_fresh:
        state = instance.load_state()
        if state:
            # Adoption joins the session that is already running, so it keeps
            # that session's number. Only a launch moves to the next one.
            start_session_num = int(state.get("SESSION_NUM", 0) or 0) + (0 if adopt else 1)
            run_started = state.get("RUN_STARTED", run_started)
            candidate = state.get("COPILOT_SESSION_ID", "")
            if UUID_RE.match(candidate or ""):
                resume_id = candidate
                log(f"  Will resume Copilot CLI session: {resume_id}")
            log(f"Continuing from session #{start_session_num} (run started {run_started})")
    if adopt:
        start_session_num = max(1, start_session_num)
        # Nothing is being launched, so there is nothing to resume into.
        resume_id = ""

    # A resume id with no handoff file for this project means the previous
    # session ended without ever calling `handoff` — most likely a crash
    # (operator itself dying, Windows rebooting, etc.) rather than a clean
    # stop. Tell the agent so it can act accordingly.
    #
    # An *unregistered* project is a different situation entirely: no catalog
    # entry means no handoff file could ever have been written there, so the
    # absence proves nothing and must not be reported to the agent as a crash.
    crash_recovery = False
    if resume_id:
        handoff_file = project_handoff_file(Path.cwd())
        if handoff_file is CATALOG_UNREADABLE:
            # The catalog would not open. That establishes nothing about
            # whether this project is registered, so it must not be reported
            # as either a missing handoff or an unregistered project.
            log("  Could not read the project catalog — not reporting this as "
                "crash recovery")
        elif handoff_file is None:
            log("  Project is not registered in the catalog — no handoff file "
                "is expected here")
        elif path_present(handoff_file) is False:
            crash_recovery = True
            log("  No handoff file found for this project — treating this as "
                "crash recovery")
        elif path_present(handoff_file) is None:
            # Telling the agent a handoff is missing is a claim about the last
            # session. A probe that failed has not established anything.
            log(f"  Could not examine {handoff_file} — not reporting this as "
                f"crash recovery")

    preamble = build_preamble(agent, instance, crash_recovery=crash_recovery)

    if adopt:
        # Refuse to "adopt" anything we do not own or that is not there: the
        # supervisor would otherwise sit polling a session it cannot manage,
        # or immediately relaunch over somebody else's.
        if not MUX.has_session(instance.session):
            die(f"No running session '{instance.display_name}' to adopt.")
        if not instance.owns_live_session():
            die(f"A session named '{instance.session}' is running but was not "
                f"started by this operator. Refusing to adopt it.\n"
                f"  Drop stale state with: operator forget {instance.display_name}")
        # Last line of defence against two supervisors watching one session:
        # they would relaunch over each other's sessions indefinitely. The
        # handoff lock makes this unlikely; this makes it survivable.
        other = _running_loop_pid(instance)
        if other is not None and other != os.getpid():
            die(f"Another loop supervisor (pid {other}) is already running for "
                f"'{instance.display_name}'. Refusing to start a second one.")
    else:
        handle_existing_session(instance)

    shutdown = {"requested": False}

    def _on_signal(signum, _frame):
        # Handlers only flag intent; blocking work happens on the main path.
        shutdown["requested"] = True

    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_signal)

    def _sleep(total: float) -> None:
        """Sleep in slices so a shutdown request is noticed promptly.

        The handler sets a flag rather than raising, so a single long sleep
        would delay Ctrl+C by up to a full poll interval.
        """
        end = time.time() + total
        while time.time() < end:
            if shutdown["requested"]:
                return
            time.sleep(min(0.25, max(0.0, end - time.time())))

    log("═══════════════════════════════════════════")
    log("Copilot CLI Operator starting (loop mode)")
    log(f"  Instance: {instance.display_name}")
    log(f"  Agent: {agent}")
    log(f"  Starting session: #{start_session_num}")
    log(f"  Poll interval: {POLL_INTERVAL}s")
    log(f"  Restart signal: {instance.restart_marker}")
    log(f"  Attach: operator join {instance.display_name}")
    log("═══════════════════════════════════════════")
    set_tab_title(f"operator - {instance.display_name}")
    set_tab_progress(TAB_LOOPING)

    session_num = start_session_num
    last_launched = 0
    launch_failures = 0
    crash_failures = 0
    # When the session now being watched went up. None until one is launched
    # or adopted; used to tell a session that died young from one that ran.
    session_started_at: float | None = None
    unknown_markers = 0
    resume_id_used = ""
    adopting = adopt
    instance.loop_pid_file.write_text(str(os.getpid()), encoding="utf-8")
    # Recorded so this supervisor can be replaced later without guessing how
    # it was started. Written every time, so it tracks the live invocation.
    _save_loop_args(instance, user_args)
    try:
        try:
            while session_num <= MAX_SESSIONS:
                if adopting:
                    # Take over the session already running: no launch, no
                    # preamble, no resume. Only the first pass adopts; every
                    # session after this one is launched normally.
                    adopting = False
                    log(f"Session #{session_num}: adopting the running session")
                    last_launched = session_num
                    # An adopted session was already up for an unknown time,
                    # which is strictly longer than nothing. Treating it as
                    # started now is the conservative reading: it can only
                    # delay the healthy-uptime reset, never trigger it early.
                    session_started_at = time.time()
                else:
                    launch_args = list(copilot_args)
                    if resume_id:
                        if args_have_explicit_session(launch_args):
                            log("  Skipping automatic --resume; user args already choose a session")
                        else:
                            launch_args.append(f"--resume={resume_id}")
                            resume_id_used = resume_id
                        resume_id = ""

                    # Messages that arrived while no session was running are
                    # handed over here, per launch rather than once: the base
                    # preamble is built before the loop starts, so mail that
                    # arrives during session #3 must still reach session #4.
                    # Read now, archive only once the session is really up.
                    launch_preamble = preamble
                    waiting = operator_mail.pending(OPERATOR_HOME, instance.id)
                    if waiting:
                        senders = ", ".join(operator_mail.sender_names(waiting))
                        log(f"  Delivering {len(waiting)} queued message(s) from {senders}")
                        launch_preamble += operator_mail.render_for_agent(waiting)

                    # Persist the pending resume id too: if the launch fails or the
                    # process dies here, the id must survive on disk rather than being
                    # cleared by a pre-launch write.
                    instance.save_state(session_num, run_started, resume_id_used)
                    try:
                        start_session(instance, launch_args, session_num,
                                      remain_on_exit=True, preamble=launch_preamble)
                    except MuxError as exc:
                        # A launch failure must not kill an unattended loop. Back off
                        # and retry the same session number rather than exiting.
                        launch_failures += 1
                        log(f"  Launch failed ({exc}) — attempt {launch_failures}")
                        if launch_failures >= MAX_LAUNCH_FAILURES:
                            log(f"  Giving up after {launch_failures} consecutive launch failures")
                            raise
                        if resume_id_used:
                            # Put the resume id back so a failed launch does not lose it.
                            resume_id = resume_id_used
                        backoff = min(60, LAUNCH_BACKOFF_BASE * launch_failures)
                        log(f"  Retrying in {backoff}s...")
                        _sleep(backoff)
                        if shutdown["requested"]:
                            raise KeyboardInterrupt
                        continue
                    launch_failures = 0
                    if waiting:
                        operator_mail.archive(OPERATOR_HOME, instance.id,
                                              [m["id"] for m in waiting])
                    resume_id_used = ""
                    last_launched = session_num
                    session_started_at = time.time()

                # Record the CLI session id once the runner discovers it.
                for _ in range(SESSION_ID_WAIT):
                    sid = instance.read_session_id()
                    if sid:
                        instance.save_state(session_num, run_started, sid)
                        break
                    if not is_copilot_running(instance):
                        break
                    if marker_set(instance.detach_marker) or marker_set(instance.stop_marker):
                        # A stop/detach request must not wait out session-id
                        # discovery: `operator restart-loop` blocks on this
                        # supervisor exiting, and a session that never reports
                        # an id would hold it for the full SESSION_ID_WAIT on
                        # top of the poll interval.
                        break
                    _sleep(1)
                    if shutdown["requested"]:
                        raise KeyboardInterrupt

                restart_requested = False
                while True:
                    if shutdown["requested"]:
                        raise KeyboardInterrupt
                    # Checked before sleeping, not after: `operator stop` and
                    # `operator restart-loop` both block waiting for this
                    # supervisor to act, so a whole poll interval of latency
                    # is paid by a human (or an agent) every time.
                    if marker_set(instance.stop_marker):
                        # `operator stop NAME` asked us to shut down and take the
                        # session with us — same as Ctrl+C, just triggered
                        # remotely since this loop now runs in the background.
                        remove_file(instance.stop_marker)
                        log(f"Session #{session_num}: stop requested — shutting down")
                        stop_session_gracefully(instance)
                        show_run_summary(run_started)
                        if MUX.has_session(instance.session):
                            MUX.kill_session(instance.session)
                        instance.cleanup_files()
                        return 0
                    if marker_set(instance.detach_marker):
                        # `operator stop-loop NAME` asked us to stop supervising
                        # but leave the session running untouched. Also how
                        # `operator restart-loop` retires the old supervisor.
                        remove_file(instance.detach_marker)
                        sid = instance.read_session_id()
                        instance.save_state(session_num, run_started, sid)
                        log(f"Session #{session_num}: detach requested — leaving "
                            f"session running, supervisor exiting")
                        return 0
                    _sleep(POLL_INTERVAL)
                    if shutdown["requested"]:
                        raise KeyboardInterrupt
                    if not is_copilot_running(instance):
                        stop_state = marker_state(instance.stop_marker)
                        detach_state = marker_state(instance.detach_marker)
                        if stop_state is None or detach_state is None:
                            # The session is gone and we cannot tell whether a
                            # human asked for that. Relaunching would resurrect
                            # a session someone stopped; assuming a stop would
                            # abandon one that crashed. Re-poll instead and let
                            # a readable marker settle it.
                            unknown_markers += 1
                            log(f"Session #{session_num}: copilot is not running but "
                                f"the stop/detach markers cannot be examined "
                                f"({unknown_markers}/{MAX_LAUNCH_FAILURES}) — "
                                f"waiting rather than relaunching")
                            if unknown_markers >= MAX_LAUNCH_FAILURES:
                                log(f"  Giving up after {unknown_markers} consecutive "
                                    f"unreadable checks — leaving the session alone")
                                show_run_summary(run_started)
                                return 1
                            continue
                        unknown_markers = 0
                        if marker_set(instance.restart_marker):
                            log(f"Session #{session_num}: restart signal detected!")
                            crash_failures = 0
                        else:
                            uptime = (None if session_started_at is None
                                      else time.time() - session_started_at)
                            if uptime is not None and uptime >= HEALTHY_SESSION_SECONDS:
                                # Healthy run, then death: whatever killed it,
                                # it is not the startup failure the limit is
                                # counting. Start the count over at this one.
                                if crash_failures:
                                    log(f"  Previous session stayed up "
                                        f"{int(uptime)}s — not a crash loop, "
                                        f"resetting the exit count")
                                crash_failures = 0
                            crash_failures += 1
                            _record_session_exit(instance, session_num,
                                                 stop_state, detach_state,
                                                 crash_failures, uptime=uptime)
                            ran_for = ("" if uptime is None
                                       else f" after {int(uptime)}s")
                            log(f"Session #{session_num}: copilot exited unexpectedly"
                                f"{ran_for} "
                                f"({crash_failures}/{MAX_LAUNCH_FAILURES}) — relaunching")
                            if crash_failures >= MAX_LAUNCH_FAILURES:
                                log(f"  Giving up after {crash_failures} consecutive "
                                    f"unexpected exits")
                                show_run_summary(run_started)
                                instance.cleanup_files()
                                return 1
                        restart_requested = True
                        break
                    if marker_set(instance.restart_marker):
                        log(f"Session #{session_num}: restart signal detected!")
                        crash_failures = 0
                        restart_requested = True
                        break

                if restart_requested:
                    log("Restarting copilot...")
                    remove_file(instance.restart_marker)
                    stop_session_gracefully(instance)
                    instance.save_state(session_num, run_started)
                    session_num += 1
                    log(f"Pausing before session #{session_num}...")
                    _sleep(RESTART_PAUSE_SECONDS)
                    if shutdown["requested"]:
                        raise KeyboardInterrupt
        except KeyboardInterrupt:
            print(file=sys.stderr)
            log("Signal received — shutting down")
            stop_session_gracefully(instance)
            # Record the last session actually launched, not one that never
            # started, and keep whichever resume id is still pending so an
            # interrupted retry does not lose it or skip a number.
            discovered = instance.read_session_id()
            instance.save_state(
                last_launched or start_session_num - 1 or 1,
                run_started,
                discovered or resume_id or resume_id_used,
            )
            show_run_summary(run_started)
            if MUX.has_session(instance.session):
                MUX.kill_session(instance.session)
            instance.cleanup_files()
            return 0

        show_run_summary(run_started)
        if MUX.has_session(instance.session):
            MUX.kill_session(instance.session)
        instance.cleanup_files()
        log("Operator shut down")
        return 0
    finally:
        remove_file(instance.loop_pid_file)


# ── help ────────────────────────────────────────────────────────
HELP = """operator — Metrics-capturing wrapper for GitHub Copilot CLI

USAGE
    operator                                                    Interactive menu
    operator [--name NAME] [copilot-args...]                   Single session
    operator --loop [--name NAME] [--fresh] [copilot-args...]  Loop mode (backgrounded, auto-attaches)
    operator --loop --headless [--name NAME] [copilot-args...] Loop mode without attaching
    operator send --from NAME --to NAME "message"              Message another instance
    operator inbox [NAME] [--peek|--history|--json]            Read messages sent to an instance
    operator NAME                                              Join a running instance
    operator join [NAME]                                       Join (explicit form)
    operator reload NAME                                       Hot-reload launch spec
    operator list                                              Show running instances
    operator stop [NAME]                                       Stop instance(s) — loop + session
    operator stop-loop NAME                                    Stop only the background loop
    operator restart-loop NAME                                 Replace the loop supervisor (new code), keep session
    operator stop-session NAME                                 Stop only the Copilot session
    operator forget NAME                                       Drop operator state only
    operator report [type]                                     View usage reports
    operator ingest [--force]                                  Process copilot logs
    operator logs [--prune] [--days N]                         Inspect/prune copilot logs
    operator trace [-n N] [--kind K] [--json] [--all]          Who invoked the operator, and how it ended
    operator tabs [list|remove NAME|clear]                     Manage tracked terminal tabs
    operator restore [NAME...|--all] [--dry-run]               Reopen tracked tabs after a crash
    operator help                                              Show this help

OPTIONS
    --name NAME     Set instance name (default: current directory name)
    --loop          Enable autonomous loop mode
    --fresh         Reset session numbering (ignore prior state)
    --headless      Start without attaching, and return. Use this when one
                    agent starts another agent's loop: the caller's terminal
                    is left alone instead of being taken over by a TUI.
                    (--detached is accepted as a synonym.)

OWNERSHIP
    The operator acts only on sessions it started. A session is owned when a
    claim record matches a session that is currently running; continuity state
    alone never confers ownership, so a leftover state file cannot authorize
    stopping an unrelated session that later took the same name. Use
    `operator forget NAME` to drop stale state without touching any session.

MODES
    Single session (default)
        Launches copilot in a multiplexer session and auto-attaches. A
        supervisor inside the session captures usage metrics when copilot
        exits — including when you have detached. Adds --autopilot --effort
        high --experimental. Not --yolo: your terminal is attached, so you
        are there to approve. Pass --yolo yourself if you want it. With
        --headless, --yolo and --no-ask-user ARE added, because nothing
        attaches and an unanswerable question would hang the session
        silently.

    Loop mode (--loop)
        Adds --yolo --autopilot --no-ask-user --effort high --experimental
        automatically.
        Runs the polling supervisor in the *background* (not in your
        terminal) and then attaches you to the Copilot session directly in
        the same tab — you never have to babysit raw loop logs or dedicate a
        second tab to it. If a supervisor is already running for this
        instance, `operator --loop --name X` just attaches to it instead of
        starting a second one. Sends a preamble for autonomous operation and
        restarts copilot when the agent raises the instance restart marker,
        or when the session ends unexpectedly (crash) — either way the loop
        keeps going. Named instances auto-continue when restarted: session
        numbering, run summary scope, and the last Copilot CLI session id
        carry over, and that session is resumed once with --resume. Use
        --fresh to reset. If a resumed session has no handoff file for the
        project, the preamble notes that this looks like crash recovery
        rather than a clean handoff.

LOOP VS. SESSION
    Loop mode has two independent lifecycles: the background supervisor
    (which watches for crashes/restarts) and the Copilot session itself
    (the multiplexer pane running Copilot). You can stop either one without
    the other:

        operator stop-loop NAME       Stop the supervisor; session keeps running.
                                      Re-attach any time with `operator join NAME`.
        operator restart-loop NAME    Replace the supervisor with a fresh one,
                                      leaving the session running. The supervisor
                                      is a long-lived process that imported the
                                      operator's code when it started, so this is
                                      how a running instance picks up an updated
                                      operator without losing its session.
        operator stop-session NAME    Stop the session; if a supervisor is still
                                      running it relaunches a fresh one shortly.
        operator stop NAME            Stop both, cleanly, with no relaunch.

MESSAGING
    Operator instances are separate processes and cannot see each other's
    context, so they talk by mail:

        operator send --from alpha --to beta "the schema is frozen"
        operator inbox alpha           Read alpha's mail (marks it read)
        operator inbox beta --peek     Read without marking read
        operator inbox beta --history  What was already delivered

    Pass your own name. With no NAME the mailbox is named after the working
    directory, which is nobody in particular -- in a checkout two agents
    share it resolves to the same name for both, and reading archives what
    it shows. A nameless destructive read is refused when another instance
    is live here (or in a worktree under it), when the derived name is live
    somewhere else, or when the multiplexer cannot say who is live.

    --from and --to are both required: the recipient has to know who wrote
    and where to send an answer, and every delivered message carries the
    exact reply command. A --to that names no known instance is refused
    rather than queued into a mailbox nobody reads (--force overrides, for
    an instance that has not started yet).

    An option neither command recognizes is refused, never ignored. Reading
    an inbox archives what it shows, so a typo'd --peek that fell through to
    the default would eat the mail it was meant to leave alone; and a typo'd
    flag on send would be delivered as message text by a sender who believed
    nothing was sent. Put message text that starts with a dash after --:

        operator send --from alpha --to beta -- "--force is the flag you want"

    Delivery depends on what the recipient is doing. If its Copilot session
    is running, the message is typed straight into it. If it is between
    sessions, the message waits and is handed over in the next session's
    preamble. --queue forces the second path. Read messages are archived,
    not deleted, so --history is an audit trail.

MENU
    Running `operator` with no arguments at all opens an interactive menu.
    Pick "Sessions" to see everything that is running, select one, and read
    its stats — which loop session it is on, how long the run has been going,
    the loop supervisor and Copilot pids, its directory and recorded usage.
    From there you can join it, stop just its loop, stop just its session, or
    stop it completely. The menu stays open until you choose Exit.

REPORTS
    operator report summary       AI credit totals (today, week, all time)
    operator report sessions      Last 20 sessions with details
    operator report models        Usage breakdown by AI model
    operator report projects      Usage breakdown by project directory
    operator report costs         Cost estimates in USD
    operator report tokens        Token counts by type

BILLING
    GitHub replaced premium requests with AI credits on 2026-06-01. Usage is
    metered on token consumption; 1 AI credit = $0.01 USD. Legacy annual plans
    still bill in premium requests, and both are reported.

    Usage figures come from Copilot's chat-completion response bodies, which
    are only written at debug log level. The operator therefore adds
    --log-level debug when launching Copilot. Set COPILOT_OPERATOR_NO_DEBUG_LOG=1
    to opt out; sessions will then produce smaller logs but record no usage.

    Debug logs are large and Copilot does not rotate them. `operator logs`
    reports how much space they use; `operator logs --prune --days N` removes
    logs older than N days, and only those already ingested.

TAB RESTORE
    Terminal apps expose no API to list their own tabs, so the operator keeps
    its own record: whenever a named instance (--name/--loop) is started
    inside a Windows Terminal tab ($WT_SESSION set), it upserts an entry in
    ~/.operator/tabs.json recording the directory and the exact `operator`
    command line used. `operator stop`/`forget` drop the entry again.

    `operator restore` needs Windows Terminal (`wt.exe`) reachable on PATH,
    so run it from native Windows PowerShell or from within a WSL distro
    (Windows interop must be enabled). It reads the local machine's registry
    plus every installed WSL distro's registry (via `wsl.exe -d <distro>`),
    then opens one Windows Terminal window with a tab per selected instance,
    replaying each command line. When run from inside WSL, only this
    machine's WSL registries are visible — a native Windows-side registry
    can't be seen from there. A reboot kills every multiplexer server, so
    there is nothing to reattach to — restore simply relaunches, and the
    existing auto-continue/--resume logic picks each Copilot session back up
    rather than starting fresh.

    With no arguments, `operator restore` lists tracked tabs and prompts for
    which to restore. `operator restore --all` restores every tracked tab
    without prompting. `operator restore NAME [NAME...]` restores exactly the
    named instance(s). Add --dry-run to any form to preview the wt.exe command
    without launching anything. Use `operator tabs` to inspect or edit the
    registry directly.

FILES
    ~/.operator/                        State directory (override with
                                        COPILOT_OPERATOR_HOME)
    ~/.operator/metrics.db              SQLite metrics database
    ~/.operator/operator.log            Operator log file
    ~/.operator/restart/                Per-instance markers and state
    ~/.operator/tabs.json               Tracked terminal tabs (for `operator restore`)
    ~/.operator/backups/                Backups of the operator script
    ~/.copilot/logs/process-*.log       Copilot process logs (source data)

PLATFORMS
    Windows          psmux   (winget install --id marlocarlo.psmux)
    Linux / WSL      tmux
    macOS            tmux

DEPENDENCIES
    A terminal multiplexer (see above), python3 >= 3.10, copilot, git
"""


def show_help() -> int:
    print(HELP)
    return 0


# ── entry point ─────────────────────────────────────────────────
def _tracked_cwd_for_id(ident: str) -> str | None | _Unplaceable:
    """The directory a tracked/managed *instance id* is bound to, if known.

    Keyed on the id rather than the display name because the two are not
    interchangeable: ``safe_instance_id`` is not idempotent, so re-deriving an
    id from something that is already an id produces a different, non-existent
    instance whose files are always missing — a lookup that fails silently and
    reports "location unknown" for an instance whose location is on disk.

    Three answers, not two: a directory, ``None`` for "nothing on disk binds
    this id anywhere", and ``UNPLACEABLE`` for "the records exist and would
    not be read". Collapsing the third into the second is what lets a census
    return a curated population as though it were complete.
    """
    spec = RESTART_DIR / f"{ident}.launch.json"
    state = file_present(spec)
    if state is None:
        return UNPLACEABLE
    if state:
        try:
            cwd = json.loads(spec.read_text(encoding="utf-8")).get("cwd")
        except ValueError:
            cwd = None
        except OSError:
            return UNPLACEABLE
        if cwd:
            return cwd
    tabs = read_tabs()
    if tabs is None:
        return UNPLACEABLE
    entry = tabs.get(ident)
    if entry:
        return entry.get("cwd")
    return None


def _tracked_cwd_for(name: str) -> str | None | _Unplaceable:
    """Best-effort lookup of the directory a tracked/managed instance name is
    already bound to, so a same-named directory elsewhere doesn't collide."""
    return _tracked_cwd_for_id(Instance(name).id)


def _name_conflicts(name: str, cwd: Path) -> bool:
    """True when `name` is already a live session bound to a different cwd.

    A name is only a real conflict when something is actually running under
    it right now; a stale state/tab entry for a directory that no longer has
    a live session poses no risk of talking to the wrong project.
    """
    inst = Instance(name)
    if not (MUX.available() and MUX.has_session(inst.session)):
        return False
    bound_to = _tracked_cwd_for(name)
    if bound_to is UNPLACEABLE:
        # Something is running under this name and we cannot show it is this
        # directory. Sharing a name with an instance we cannot place is the
        # expensive mistake here; suffixing the name is the cheap one.
        return True
    return bound_to is not None and bound_to != str(cwd)


def default_instance_name() -> str:
    """Derive an instance name from the working directory.

    A filesystem root has no directory name, so falling back to a generic
    label would silently run Copilot over the entire drive. Bash refuses
    this; so do we.

    Two different directories that happen to share a folder name (e.g. two
    checkouts both named "backend") would otherwise collide if both are
    running at once. When the plain name is already a live session bound to
    a different directory, append -1, -2, ... until a free name is found.
    """
    cwd = Path.cwd()
    base = cwd.name
    if not base:
        die(f"Refusing to start in the filesystem root ({cwd}).\n"
            "  Run the operator from a project directory, or pass --name NAME.")

    candidate = base
    suffix = 0
    while suffix <= MAX_SESSIONS:
        if not _name_conflicts(candidate, cwd):
            return candidate
        suffix += 1
        candidate = f"{base}-{suffix}"
    die(f"Could not find a free instance name derived from '{base}' after "
        f"{MAX_SESSIONS} attempts. Pass --name NAME explicitly.")


def _spawn_background_loop(instance: Instance, copilot_args: list[str],
                           is_fresh: bool, adopt: bool = False,
                           cwd: str | None = None) -> int:
    """Launch the loop supervisor as a detached background OS process.

    Re-execs this same script with --_supervise so the child runs
    run_loop_mode directly instead of recursing into this function again.

    Windows note: use CREATE_NO_WINDOW, *not* DETACHED_PROCESS. Both detach
    the child from the parent terminal's console, but DETACHED_PROCESS leaves
    the child with no console at all -- so the moment it (or any descendant)
    starts another console program, Windows allocates a brand new *visible*
    console window for it. That bites immediately here because `sys.executable`
    is typically a venv/Store shim that re-execs the real python.exe as a
    child process. CREATE_NO_WINDOW instead gives the supervisor its own
    console that has no window, and every descendant inherits that invisible
    console, so nothing ever pops up.
    """
    cmd = [sys.executable, str(Path(__file__).resolve()),
           "--_supervise", "--loop", "--name", instance.display_name]
    if is_fresh:
        cmd.append("--fresh")
    if adopt:
        cmd.append("--adopt")
    cmd += copilot_args
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, close_fds=True,
                       cwd=cwd or str(Path.cwd()))
    if IS_WINDOWS:
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)  # decode-ok: every stream is DEVNULL
    return proc.pid


def start_and_attach_loop(instance: Instance, copilot_args: list[str],
                          is_fresh: bool) -> int:
    """Ensure a background loop supervisor is running for instance, then
    attach to its Copilot session in the current tab.

    This is what `operator --loop` does today: the supervisor never blocks
    the invoking terminal, and there is only ever one tab involved, not one
    for the loop's logs and a second for the session.
    """
    existing_pid = _running_loop_pid(instance)
    if existing_pid is not None:
        log(f"Loop supervisor already running for '{instance.display_name}' "
            f"(pid {existing_pid}) — attaching")
        print(f"Loop already running for '{instance.display_name}' — attaching...")
    else:
        pid = _spawn_background_loop(instance, copilot_args, is_fresh)
        log(f"Started background loop supervisor for '{instance.display_name}' (pid {pid})")
        print(f"Loop started in the background for '{instance.display_name}' "
              f"(pid {pid}) — attaching...")

    deadline = time.time() + SESSION_ID_WAIT
    while time.time() < deadline:
        if MUX.has_session(instance.session):
            break
        time.sleep(0.5)
    else:
        print(f"Timed out waiting for session '{instance.display_name}' to start.",
              file=sys.stderr)
        print(f"Check the log for details: {LOG_FILE}", file=sys.stderr)
        return 1

    set_tab_title(f"operator - {instance.display_name}")
    set_tab_progress(TAB_LOOPING)
    MUX.attach(instance.session)
    return 0


def start_loop_headless(instance: Instance, copilot_args: list[str],
                        is_fresh: bool) -> int:
    """Start a background loop supervisor and return without attaching.

    Same supervisor as ``operator --loop``, minus the attach — for starting a
    loop from somewhere that must not be taken over by a full-screen TUI, such
    as one agent starting another agent's loop.

    It still waits for the session to appear rather than returning the moment
    the process is spawned: a caller that never attaches would otherwise have
    no way to learn that the launch failed.
    """
    existing_pid = _running_loop_pid(instance)
    if existing_pid is not None:
        log(f"Loop supervisor already running for '{instance.display_name}' "
            f"(pid {existing_pid}) — nothing to start")
        print(f"Loop already running for '{instance.display_name}' (pid {existing_pid}).")
        print(f"  Attach: operator join {instance.display_name}")
        return 0

    pid = _spawn_background_loop(instance, copilot_args, is_fresh)
    log(f"Started headless loop supervisor for '{instance.display_name}' (pid {pid})")

    deadline = time.time() + SESSION_ID_WAIT
    while time.time() < deadline:
        if MUX.has_session(instance.session):
            break
        time.sleep(0.5)
    else:
        print(f"Timed out waiting for session '{instance.display_name}' to start.",
              file=sys.stderr)
        print(f"Check the log for details: {LOG_FILE}", file=sys.stderr)
        return 1

    print(f"Loop '{instance.display_name}' started headless (supervisor pid {pid}).")
    print(f"  Attach:  operator join {instance.display_name}")
    print(f"  Message: operator send --from <you> --to {instance.display_name} \"...\"")
    print(f"  Stop:    operator stop {instance.display_name}")
    return 0


def _prompt_line(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


# ── interactive session browser ─────────────────────────────────
def _parse_utc(stamp: str) -> datetime | None:
    """Parse the operator's own ``%Y-%m-%dT%H:%M:%SZ`` timestamps."""
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _fmt_elapsed(seconds: float) -> str:
    total = int(max(0, seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _age_since(stamp: str) -> str:
    started = _parse_utc(stamp)
    if started is None:
        return "unknown"
    return _fmt_elapsed((datetime.now(timezone.utc) - started).total_seconds())


def _shorten_home(path: str) -> str:
    home = str(HOME)
    return "~" + path[len(home):] if path.startswith(home) else path


def instance_snapshot(instance: Instance) -> dict:
    """Everything the browser needs in order to describe one instance.

    Reads only state that already exists on disk, so it is safe to call
    repeatedly — refreshing the view never disturbs a running session.
    """
    state = instance.load_state() or {}
    owner = instance.ownership() or {}
    try:
        session_num = int(state.get("SESSION_NUM", 0) or 0)
    except ValueError:
        session_num = 0
    spec: dict = {}
    try:
        loaded = json.loads(instance.spec_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        loaded = None
    if isinstance(loaded, dict):
        spec = loaded
    cwd = spec.get("cwd") or (load_tabs().get(instance.id) or {}).get("cwd") or ""
    session_live = MUX.available() and MUX.has_session(instance.session)
    return {
        "instance": instance,
        "name": instance.display_name,
        "id": instance.id,
        "session_live": session_live,
        # Ownership gates every destructive action, so the browser has to
        # surface it: a same-named session we did not start is look-only.
        "owned": session_live and instance.owns_live_session(),
        "loop_pid": _running_loop_pid(instance),
        "session_num": session_num,
        "run_started": state.get("RUN_STARTED", "") or owner.get("claimed_at", ""),
        "copilot_session_id": (state.get("COPILOT_SESSION_ID", "")
                               or instance.read_session_id()),
        "copilot_pid": instance.copilot_pid(),
        "cwd": cwd,
        "argv": list(spec.get("argv") or []),
    }


def _status_label(snap: dict) -> str:
    if snap["loop_pid"] and snap["session_live"]:
        return f"looping · session #{snap['session_num'] or 1}"
    if snap["loop_pid"]:
        return "looping · starting next session"
    if snap["session_live"]:
        return "single session · no loop"
    return "stopped"


def _instance_summary(snap: dict) -> str:
    parts = [snap["name"], _status_label(snap)]
    if snap["run_started"]:
        parts.append(f"up {_age_since(snap['run_started'])}")
    if snap["cwd"]:
        parts.append(_shorten_home(snap["cwd"]))
    if snap["session_live"] and not snap["owned"]:
        parts.append("[unowned session]")
    return "  ·  ".join(parts)


def active_instances() -> list[Instance]:
    """Managed instances with a live session and/or a live loop supervisor.

    A loop between sessions has no session for a few seconds, and a session
    whose loop was stopped has no supervisor. Both are exactly the states a
    user needs to act on, so neither one alone may exclude an instance.
    """
    live = set(MUX.list_sessions()) if MUX.available() else set()
    found: list[Instance] = []
    for ident, meta in sorted(managed_instances().items()):
        inst = Instance(meta.get("display_name", ident))
        if inst.id in live or _running_loop_pid(inst) is not None:
            found.append(inst)
    return found


def _recorded_usage(cwd: str) -> str:
    """Spend already recorded for this directory, as one line.

    Metrics are keyed by working directory rather than by instance name, so
    this is the project's total across every run — not just this one.
    """
    if not cwd or file_present(METRICS_DB) is not True:
        return ""
    try:
        rows, _ = _query(f"""
            SELECT COUNT(*) AS n,
                   printf('%.1f', {_credits()}) AS credits,
                   printf('$%.2f', {_usd()}) AS est_cost
            FROM sessions WHERE no_op = 0 AND work_dir = ?
        """, (cwd,))
    except sqlite3.Error:
        return ""
    if not rows or not rows[0][0]:
        return ""
    count, credits, cost = rows[0][0], rows[0][1], rows[0][2]
    return f"{count} recorded session(s) · {credits} credits · {cost}"


def _print_instance_detail(snap: dict) -> None:
    print(f"\n═══ {snap['name']} ═══\n")
    rows: list[tuple[str, str]] = [("Status", _status_label(snap))]
    if snap["run_started"]:
        rows.append(("Running for", f"{_age_since(snap['run_started'])} "
                                    f"(since {snap['run_started']})"))
    if snap["session_num"]:
        rows.append(("Loop session", f"#{snap['session_num']}"))
    rows.append(("Loop supervisor",
                 f"pid {snap['loop_pid']}" if snap["loop_pid"] else "not running"))
    if snap["session_live"]:
        rows.append(("Session", f"{snap['id']} — live"
                                + ("" if snap["owned"] else
                                   " (NOT started by this operator)")))
    else:
        rows.append(("Session", "not running"))
    if snap["copilot_pid"]:
        rows.append(("Copilot pid", str(snap["copilot_pid"])))
    if snap["copilot_session_id"]:
        rows.append(("Copilot session", snap["copilot_session_id"]))
    if snap["cwd"]:
        rows.append(("Directory", _shorten_home(snap["cwd"])))
    usage = _recorded_usage(snap["cwd"])
    if usage:
        rows.append(("Usage", usage))
    if snap["argv"]:
        # The preamble is a paragraph of prose; showing it in full buries
        # everything else, so keep the flags and elide the rest.
        flags = []
        for arg in snap["argv"]:
            if arg == "-i":
                flags.append("-i <preamble>")
                break
            flags.append(arg)
        rows.append(("Command", " ".join(flags)))
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"  {label.ljust(width)}   {value}")


def _pick_instance(purpose: str) -> Instance | None:
    """List what is running and return the one the user picked."""
    instances = active_instances()
    if not instances:
        print("\nNo operator instances are running.")
        print("  Start one with: operator --loop --name <name>")
        return None
    snaps = [instance_snapshot(inst) for inst in instances]
    print(f"\n═══ {purpose} ═══\n")
    for i, snap in enumerate(snaps, 1):
        print(f"  {i}) {_instance_summary(snap)}")
    print()
    choice = _prompt_line(f"Instance [1-{len(snaps)}] (blank to cancel): ")
    if not choice:
        return None
    try:
        idx = int(choice)
    except ValueError:
        print("Not a number.", file=sys.stderr)
        return None
    if not 1 <= idx <= len(snaps):
        print("Out of range.", file=sys.stderr)
        return None
    return instances[idx - 1]


def _join_snapshot(snap: dict) -> int:
    """Attach to a picked instance without a second name lookup."""
    if not snap["session_live"]:
        print(f"'{snap['name']}' has no live session to join.", file=sys.stderr)
        if snap["loop_pid"]:
            print("  Its loop is between sessions — try again in a moment.",
                  file=sys.stderr)
        return 1
    return join_instance(snap["name"])


def show_instance_detail(instance: Instance) -> int:
    """Stats for one instance plus the actions that apply to its state.

    Only offers what the instance can actually do right now: stopping a loop
    that is not running, or a session that is not live, is not an option the
    user should have to discover is a no-op.
    """
    while True:
        snap = instance_snapshot(instance)
        _print_instance_detail(snap)
        if not snap["session_live"] and not snap["loop_pid"]:
            print("\n  This instance is no longer running.")
            return 0

        options: list[tuple[str, Callable[[], int]]] = []
        if snap["session_live"]:
            options.append(("Join this session (attach the terminal)",
                            lambda: _join_snapshot(snap)))
        if snap["loop_pid"]:
            options.append(("Stop the loop, leave the session running",
                            lambda: stop_loop_only(snap["name"])))
        if snap["session_live"]:
            label = ("Stop the session, let the loop restart it"
                     if snap["loop_pid"] else "Stop the session")
            options.append((label, lambda: stop_session_only(snap["name"])))
        options.append(("Stop everything (loop and session)",
                        lambda: stop_operator(snap["name"])))
        options.append(("Refresh", None))
        options.append(("Back", None))

        print()
        for i, (label, _) in enumerate(options, 1):
            print(f"  {i}) {label}")
        print()
        choice = _prompt_line(f"Action [1-{len(options)}] (blank to go back): ")
        if not choice:
            return 0
        try:
            idx = int(choice)
        except ValueError:
            print("Not a number.", file=sys.stderr)
            continue
        if not 1 <= idx <= len(options):
            print("Out of range.", file=sys.stderr)
            continue
        label, action = options[idx - 1]
        if label == "Back":
            return 0
        if action is None:      # Refresh
            continue
        rc = action()
        if label.startswith("Join"):
            return rc
        # Any stop changes the instance's state; loop round and re-read it so
        # the next menu reflects what is actually left running.


def browse_instances() -> int:
    """List running instances, pick one, then act on it."""
    instance = _pick_instance("Running Operator Instances")
    if instance is None:
        return 0
    return show_instance_detail(instance)


def show_menu() -> int:
    """Bare ``operator`` (no arguments): an interactive action picker.

    Every session action funnels through the browser rather than asking the
    user to retype a name the operator already knows. The menu loops so a
    single action is not the end of the program.
    """
    while True:
        running = len(active_instances())
        print("\n═══ Copilot Operator ═══\n")
        options: list[tuple[str, Callable[[], int] | None]] = [
            (f"Sessions — inspect, join or stop  ({running} running)",
             browse_instances),
            ("Restore tabs (pick which)", lambda: restore_tabs([])),
            ("Restore all tracked tabs", lambda: restore_tabs(["--all"])),
            ("View usage report", lambda: report_metrics("summary")),
            ("Exit", None),
        ]
        for i, (label, _) in enumerate(options, 1):
            print(f"  {i}) {label}")
        print()
        choice = _prompt_line("Choose an action: ")
        if not choice:
            return 0
        try:
            idx = int(choice)
        except ValueError:
            print("Not a number.", file=sys.stderr)
            return 1
        if idx < 1 or idx > len(options):
            print("Out of range.", file=sys.stderr)
            return 1
        _, action = options[idx - 1]
        if action is None:
            return 0
        rc = action()
        if rc != 0:
            return rc


def _record_session_exit(instance, session_num: int,
                         stop_state, detach_state, consecutive: int,
                         uptime: float | None = None) -> None:
    """Trace a session found gone, with the evidence the decision was made on.

    The supervisor polls liveness rather than waiting on the child, so it has
    never had an exit *code* to log -- but the runner writes one to the exit
    file, and that is the difference between "copilot crashed" and "copilot
    shut down cleanly and nobody asked us to expect it". Reading it here costs
    one file read on a path that only runs when a session has already ended.
    """
    try:
        code: "int | None" = None
        try:
            raw = instance.exit_file.read_text(encoding="utf-8").strip()
            code = int(raw) if raw else None
        except (OSError, ValueError):
            code = None
        try:
            pid = instance.copilot_pid()
        except Exception:
            pid = None
        operator_trace.record_session_exit(
            OPERATOR_HOME,
            instance=instance.display_name,
            session=session_num,
            pid=pid,
            markers={"stop": stop_state, "detach": detach_state,
                     "restart": marker_state(instance.restart_marker),
                     "exit_code": code,
                     "uptime_s": None if uptime is None else int(uptime)},
            consecutive=consecutive,
            limit=MAX_LAUNCH_FAILURES,
        )
    except Exception:
        return


def show_trace(args: list[str]) -> int:
    """Print the invocation trace: who ran the operator, and how it ended.

    The trace answers the question ``operator.log`` cannot: a line there says
    a session was relaunched, but not whether a person asked for it, an agent
    did, or something outside the toolkit did.
    """
    limit = 25
    kind = None
    as_json = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-n", "--limit"):
            if i + 1 >= len(args):
                die("--limit requires a value")
            try:
                limit = int(args[i + 1])
            except ValueError:
                die(f"--limit wants a number, got {args[i + 1]!r}")
            i += 1
        elif arg == "--kind":
            if i + 1 >= len(args):
                die("--kind requires a value")
            kind = args[i + 1]
            i += 1
        elif arg == "--json":
            as_json = True
        elif arg in ("--all",):
            limit = 0
        else:
            die(f"unknown option for `operator trace`: {arg}")
        i += 1

    records = operator_trace.read_records(OPERATOR_HOME, limit=0, kind=None)
    if records is None:
        # Not the same as an empty trace, and saying "no invocations" here
        # would be the exact substitution this trace exists to catch.
        print(f"Error: could not read the trace at "
              f"{operator_trace.trace_path(OPERATOR_HOME)}", file=sys.stderr)
        return 1

    exits = {r.get("trace_id"): r for r in records if r.get("event") == "exit"}
    all_invocations = [r for r in records if r.get("event") == "invoke"]
    invocations = [
        r for r in all_invocations
        if not kind or (r.get("source") or {}).get("kind") == kind
    ]
    if limit:
        invocations = invocations[-limit:]

    session_exits = [r for r in records if r.get("event") == "session_exit"]
    if limit:
        session_exits = session_exits[-limit:]

    if as_json:
        print(json.dumps(
            {"invocations": invocations, "session_exits": session_exits},
            indent=2))
        return 0

    if not invocations and not session_exits:
        # An empty *result* and an empty *trace* are different findings, and
        # printing one sentence for both would hide a filter that matched
        # nothing behind a file that recorded nothing.
        if all_invocations:
            kinds = sorted({(r.get("source") or {}).get("kind") or "?"
                            for r in all_invocations})
            print(f"No invocation matched --kind {kind}. "
                  f"{len(all_invocations)} traced so far; "
                  f"kinds present: {', '.join(kinds)}.")
        else:
            print("No operator invocations have been traced yet.")
        print(f"  Trace file: {operator_trace.trace_path(OPERATOR_HOME)}")
        return 0

    print(f"═══ Operator invocations ({len(invocations)} shown) ═══\n")
    for rec in invocations:
        source = rec.get("source") or {}
        done = exits.get(rec.get("trace_id"))
        if done is None:
            outcome = "running/unknown"
        else:
            outcome = f"rc={done.get('rc')} {done.get('ms')}ms"
        argv = " ".join(rec.get("argv") or []) or "(no arguments)"
        print(f"{rec.get('ts', '?'):20} {str(source.get('kind', '?')):11} "
              f"{argv[:60]:60} {outcome}")
        why = source.get("why")
        if why:
            print(f"{'':20} └─ {why}")

    if session_exits:
        # Printed separately because they are a different kind of fact: not a
        # command someone ran, but a supervised session found gone. These are
        # the events a mass die-off consists of, and no operator command is
        # invoked during one -- which is why an invocation log alone could not
        # explain the seven simultaneous deaths this trace was written after.
        print(f"\n═══ Supervised sessions found gone "
              f"({len(session_exits)} shown) ═══\n")
        for rec in session_exits:
            markers = rec.get("markers") or {}
            code = markers.get("exit_code")
            # "Exited unexpectedly" only ever meant unexplained. A recorded
            # exit code of 0 is a clean shutdown nobody asked us to expect,
            # and reading it as a crash is how five of them end a loop.
            if code is None:
                verdict = "no exit code recorded"
            elif code == 0:
                verdict = "clean exit (rc=0), unexplained by any marker"
            else:
                verdict = f"rc={code}"
            gave_up = " GIVING UP" if rec.get("giving_up") else ""
            print(f"{rec.get('ts', '?'):20} {str(rec.get('instance', '?')):18} "
                  f"#{rec.get('session', '?'):<5} "
                  f"{rec.get('consecutive', '?')}/{rec.get('limit', '?')}"
                  f"{gave_up}")
            print(f"{'':20} └─ {verdict}; "
                  f"copilot pid={rec.get('session_pid')}, markers "
                  f"stop={markers.get('stop')} detach={markers.get('detach')} "
                  f"restart={markers.get('restart')}")

    print(f"\nTrace file: {operator_trace.trace_path(OPERATOR_HOME)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point: trace the invocation, then dispatch it.

    The trace brackets the entire command so a record shows both what was
    asked and how it ended. Most error paths in this file leave through
    ``die()``, which is a ``SystemExit``, and a crash leaves through an
    exception -- ``operator.log`` renders those two identically today, and
    they are not the same thing at all.
    """
    enable_utf8_output()
    args = list(sys.argv[1:] if argv is None else argv)
    traced = operator_trace.record_invocation(OPERATOR_HOME, args)
    try:
        rc = _dispatch_command(args)
    except SystemExit as exc:
        code = exc.code
        operator_trace.record_exit(
            traced,
            code if isinstance(code, int) else (0 if code is None else 1))
        raise
    except BaseException:
        # -1 is not a code this program can return, which is exactly why it is
        # used: it marks a command that left through an exception rather than
        # through a decision, and keeps the two distinguishable in the trace.
        operator_trace.record_exit(traced, -1)
        raise
    operator_trace.record_exit(traced, rc)
    return rc


def _dispatch_command(args: list[str]) -> int:
    migrate_legacy_state()

    if not args:
        return show_menu()

    head = args[0]
    if head in ("help", "-h", "--help", "-?"):
        return show_help()
    if head in ("version", "--version", "-V"):
        print(f"operator {__version__}")
        return 0
    if head == "list":
        return list_instances()
    if head == "menu":
        return show_menu()
    if head == "report":
        return report_metrics(args[1] if len(args) > 1 else "summary")
    if head == "ingest":
        return ingest_all_logs(force="--force" in args[1:])
    if head == "stop":
        return stop_operator(args[1] if len(args) > 1 else None)
    if head == "stop-loop":
        return stop_loop_only(args[1] if len(args) > 1 else None)
    if head == "restart-loop":
        return restart_loop(args[1] if len(args) > 1 else None)
    if head == "stop-session":
        return stop_session_only(args[1] if len(args) > 1 else None)
    if head == "join":
        return join_instance(args[1] if len(args) > 1 else None)
    if head == "reload":
        return reload_instance(args[1] if len(args) > 1 else None)
    if head == "forget":
        return forget_instance(args[1] if len(args) > 1 else None)
    if head == "send":
        return send_message(args[1:])
    if head == "inbox":
        return show_inbox(args[1:])
    if head == "logs":
        return manage_logs(args[1:])
    if head == "trace":
        return show_trace(args[1:])
    if head == "tabs":
        return manage_tabs(args[1:])
    if head == "restore":
        return restore_tabs(args[1:])

    # Positional shortcut: `operator foo` joins a running instance named foo.
    if len(args) == 1 and not head.startswith("-") and head not in RESERVED_WORDS:
        candidate = Instance(head)
        if MUX.available() and MUX.has_session(candidate.session):
            set_tab_title(f"terminal - {candidate.display_name}")
            set_tab_progress(TAB_LOOPING if _running_loop_pid(candidate) else TAB_STEADY)
            MUX.attach(candidate.session)
            return 0

    return run_dispatch(args)


def run_dispatch(args: list[str]) -> int:
    if not MUX.available():
        print(_missing_mux_message(), file=sys.stderr)
        return 1

    loop_mode = False
    is_fresh = False
    supervise = False
    headless = False
    adopt = False
    name = ""
    copilot_args: list[str] = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--loop":
            loop_mode = True
        elif arg == "--fresh":
            is_fresh = True
        elif arg in ("--headless", "--detached"):
            headless = True
        elif arg == "--adopt":
            # Internal: set by `operator restart-loop` on the replacement
            # supervisor so it takes over the running session instead of
            # launching a new one. Not for interactive use.
            adopt = True
        elif arg == "--_supervise":
            # Internal: marks the re-exec'd background supervisor process so
            # it runs run_loop_mode directly instead of spawning yet another
            # background process. Not documented; not for interactive use.
            supervise = True
        elif arg == "--name":
            if i + 1 >= len(args) or not args[i + 1]:
                die("--name requires a value")
            name = args[i + 1]
            i += 1
        elif arg.startswith("--name="):
            name = arg.split("=", 1)[1]
            if not name:
                die("--name requires a value")
        else:
            copilot_args.append(arg)
        i += 1

    instance = Instance(name or default_instance_name())
    register_tab(instance, loop_mode, copilot_args, Path.cwd())
    try:
        if loop_mode:
            if supervise:
                return run_loop_mode(instance, copilot_args, is_fresh, adopt=adopt)
            if headless:
                return start_loop_headless(instance, copilot_args, is_fresh)
            return start_and_attach_loop(instance, copilot_args, is_fresh)
        return run_single_session(instance, copilot_args, headless=headless)
    except MuxError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _missing_mux_message() -> str:
    try:
        MUX.binary
    except MuxNotFoundError as exc:
        return f"Error: {exc}"
    return "Error: no terminal multiplexer available."


if __name__ == "__main__":
    sys.exit(main())
