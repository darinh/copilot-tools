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
import hashlib
import json
import os
import ntpath
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
import mail_affiliation                                      # noqa: E402
import operator_mail                                         # noqa: E402
import operator_session                                      # noqa: E402
import operator_trace                                        # noqa: E402
import operator_work                                         # noqa: E402
import operator_worktree                                     # noqa: E402
import operator_ownership                                    # noqa: E402
import work_claims                                           # noqa: E402
import conversation_log                                      # noqa: E402
import conversation_viewer                                   # noqa: E402
import backlog_tool                                          # noqa: E402
import handoff_tool                                          # noqa: E402
import install_manifest                                      # noqa: E402
import project_features                                      # noqa: E402
import project_instructions                                  # noqa: E402
from copilot_tools_version import __version__ as TOOLKIT_VERSION  # noqa: E402
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
    catalog_guid,
    catalog_rows,
    guid_is_usable,
    operator_home as _operator_home,
    primary_repo_root,
    project_dir,
    projects_root,
)

__version__ = "1.0.0"

POLL_INTERVAL = 10
#: How often the supervisor refreshes the work claim of a running session.
#:
#: The claim's staleness window is measured in minutes and the poll interval
#: in seconds, so writing on every poll would buy no extra evidence and cost
#: a database write per tick for the lifetime of every loop on the machine.
HEARTBEAT_INTERVAL = 60
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
# How long a supervisor's startup record may be believed on its age alone,
# once the pid it names is no longer alive. It bounds one specific unknown:
# on Windows `sys.executable` is often a launcher shim that re-execs the real
# interpreter and exits, so the pid the spawning parent recorded can be dead
# while the supervisor is starting normally. Generous against a measured
# 105 ms floor for interpreter-plus-import, because everything this bound is
# wrong about costs a bounded wait, while everything it is wrong about in the
# other direction costs a destroyed session.
SUPERVISOR_STARTUP_GRACE = 30.0
# How far *ahead* of the clock a record's mtime may sit and still be read as
# "written just now". A record written microseconds ago routinely reads as
# microseconds in the future: `time.time()` and a filesystem timestamp are not
# the same clock, and on Windows they differ by more than the coarser one's
# tick. Seconds rather than milliseconds so that a filesystem whose timestamps
# round to the nearest second cannot defeat it, and deliberately far below
# SUPERVISOR_STARTUP_GRACE, because this tolerance *adds* to how long a record
# can be believed and every wait that must outlast one pays for it.
#
# Not a way of tolerating a badly wrong clock. A record dated further ahead
# than this is pruned, on purpose: `age < grace` alone stays true of a
# future-dated record for as long as the skew lasts, which is hours, and an
# unbounded refusal is worse than a wrong one here. Declining to start a
# second supervisor is *reported as success*, so it becomes a launch that
# silently starts nothing.
CLOCK_SKEW_TOLERANCE = 2.0
# The longest a startup record can still be believed, measured from now: one
# dated CLOCK_SKEW_TOLERANCE ahead is believed until it is
# SUPERVISOR_STARTUP_GRACE behind. Every wait that has to outlast a record
# adds *this*, never the grace alone.
#
# Derived rather than written out because the two got out of step twice while
# this was being written, and the failure is quiet: a wait shorter than the
# window cannot do anything but time out in exactly the case the window
# exists for, and it strands the marker it laid down when it does.
SUPERVISOR_STARTUP_ALLOWANCE = SUPERVISOR_STARTUP_GRACE + CLOCK_SKEW_TOLERANCE
# The point past which a startup record is not believed even though the pid it
# names is alive. That belief is otherwise unbounded, and a pid is not an
# identity: a supervisor hard-killed inside its startup window -- the one exit
# that runs neither its atexit hook nor its `finally` -- leaves a record behind,
# and the operating system is free to hand that number to something unrelated.
# From then on the record names a live process forever, is never pruned because
# liveness is checked before age, and every later launch declines to start a
# supervisor and *reports success*. A rare crash becomes a permanent, silent
# refusal to run, recoverable only by deleting a file nobody knows exists.
#
# Ten minutes rather than something near the grace because this bound is not
# for slowness -- the grace and a live pid already cover a startup taking its
# time, which is the whole reason liveness is a ground for belief at all. It
# exists so that a phantom expires at all, and a startup still unpublished ten
# minutes in is not one this record should keep speaking for.
#
# Deliberately NOT added to any wait budget. A record with a live process
# behind it cannot be waited out on principle, and a caller that runs into one
# should time out and say so rather than block for ten minutes; the budgets are
# sized for SUPERVISOR_STARTUP_ALLOWANCE, which covers every record whose
# process is gone -- the only kind a wait can outlast.
SUPERVISOR_STARTUP_CEILING = 600.0
# Consecutive sessions that may change nothing before the loop gives up.
MAX_NOCHANGE_SESSIONS = 3
# Consecutive sessions that may end *unaccounted for* -- neither by a restart
# request nor by an exit the runner saw -- and change nothing, before the loop
# gives up.
#
# A separate allowance, and a more patient one, because it is answering a
# different question. Changing nothing is evidence of idleness only when the
# session ended the way the loop expects; a session that was killed at four
# minutes has usually not committed yet, so folding it into the idleness
# streak retires the loops being killed *fastest* -- exactly the ones whose
# failure has nothing to do with the agent. It is still bounded: an
# unattended loop that cannot keep a session alive long enough to produce
# anything burns credits either way, and the healthy-uptime reset means
# MAX_LAUNCH_FAILURES can never bound it. Five matches the tolerance
# MAX_LAUNCH_FAILURES gives deaths, rather than the three idleness gets.
MAX_UNACCOUNTED_SESSIONS = 5
# Seconds any single git probe may take before its answer is "unknown".
GIT_PROBE_TIMEOUT = 30
# run_loop_mode's exit code when the progress circuit breaker stopped it.
EXIT_NO_PROGRESS = 3
# run_loop_mode's exit code when the loop was stopped by sessions that kept
# ending unaccounted for. Distinct from EXIT_NO_PROGRESS because the two carry
# opposite diagnoses -- "the agent has run out of work" versus "something is
# killing the sessions" -- and a reader who cannot tell them apart will act on
# the wrong one.
EXIT_UNACCOUNTED = 4

# Every word `_dispatch_command` answers to itself, rather than reading as an
# instance name or handing on to copilot. This tuple is the single source of
# truth: `RESERVED_WORDS` is derived from it and the did-you-mean suggestion
# is measured against it.
#
# It is one list because the hand-maintained second copy had already drifted:
# `send` and `inbox` are dispatched here and were missing from the set that
# decides what is not an instance name. Nothing broke, because both are
# matched before the shortcut is reached -- which is exactly the kind of
# silence that lets the next omission be a real one.
SUBCOMMANDS = ("help", "version", "list", "menu", "projects", "report",
               "ingest", "stop", "stop-loop", "restart-loop", "stop-session",
               "join", "reload", "forget", "send", "reply", "inbox", "logs",
               "trace",
               "tabs", "restore", "session", "work", "backlog", "worktree",
               "ownership", "conversations")

RESERVED_WORDS = set(SUBCOMMANDS)

#: Words that mean a subcommand here but are spelled for a different tool.
#:
#: These are not typos and no edit distance reaches them: somebody typing
#: ``ls`` has spelled what they meant correctly, in the wrong language. That
#: makes them the one part of the typo guard that has to be enumerated by
#: hand, and the list is deliberately short -- an alias is a guess about
#: intent, and a wrong guess sends the reader somewhere that does not do what
#: they asked.
#:
#: Two rules for adding one, both learned from entries that were removed:
#:
#: - The target must *do the thing the other tool's word names*. ``cat`` and
#:   ``tail`` were here pointing at ``logs``, which reports sizes and prunes
#:   old files and cannot display a log at all. An alias that misses by that
#:   much is worse than no suggestion, because it is confident.
#: - The target must not be *more destructive than the word*. ``quit`` and
#:   ``exit`` were here pointing at ``stop``, and bare ``operator stop`` kills
#:   every managed instance on the machine without asking. Somebody typing
#:   ``quit`` means "let me out of this one", and answering it with a command
#:   that stops everybody else's agents too is the exact harm this guard was
#:   built to prevent, arriving by the front door.
SUBCOMMAND_ALIASES = {
    "ls": "list",          # every unix shell
    "ll": "list",
    "ps": "list",          # what is running
    "dir": "list",         # cmd.exe
    "sessions": "list",
    "status": "list",      # `list` is the "what is running" view
    "kill": "stop",        # a synonym, not an escalation: both take a target
}
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SESSION_ARG_RE = re.compile(r"^--(continue|resume|connect)(=.*)?$")

IS_WINDOWS = platform.system() == "Windows"

# Verdicts of the supervisor-staleness check. Defined up here, away from the
# machinery in `loop_code_state`, because `build_preamble` takes one as a
# default argument and a default is evaluated when the `def` runs -- so the
# name has to exist above the first function that mentions it, not merely
# above the first that calls it.
CODE_CURRENT = "current"
CODE_STALE = "stale"
CODE_UNKNOWN = "unknown"
CODE_UNRECORDED = "unrecorded"

#: How many numbered clauses the unconditional part of the preamble already
#: spends, so the optional ones know where to start counting.
#:
#: This is an assumption about prose that lives somewhere else, which is the
#: shape that reads as fact and is checked by nobody. Editing the base text to
#: add a "(6)" would silently make the first optional clause a duplicate,
#: because a wrong number here is still a number and every clause after it
#: stays self-consistently wrong. `test_the_base_clause_count_matches_the_text`
#: counts the clauses in the rendered preamble instead of trusting this, so the
#: assumption is falsified by the text rather than restated by it.
BASE_CLAUSES = 5

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
    """This toolkit's own state directory.

    Defined in ``project_paths`` and re-exported here. It used to live in this
    module, which meant :func:`projects_root` -- one import away, in a module
    this one depends on -- could not reach it and spelled its own parent
    directory out by hand instead. That is how the project catalog ended up
    somewhere the rest of the toolkit's state had already left.
    """
    return _operator_home()


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
#: The project catalog and the retired-instructions archive were this
#: toolkit's own state kept in the Copilot CLI's configuration directory.
#: ``~/.copilot`` belongs to the CLI -- it is where the CLI keeps its
#: extensions, skills, settings and session store -- so anything of ours in
#: there is squatting, and is subject to whatever the CLI does to its own
#: directory. The catalog is the file that maps a project to its id; losing
#: it does not lose a preference, it loses every project's identity and with
#: it every handoff and `superseded/` file keyed to that id.
LEGACY_PROJECTS_DIR = HOME / ".copilot" / "projects"

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
        moved += _migrate_legacy_projects()
        if moved:
            log(f"Migrated {moved} legacy state item(s) into {OPERATOR_HOME}")


def _migrate_legacy_projects() -> int:
    """Move the project catalog and per-project directories out of ~/.copilot.

    Merged entry by entry rather than moved as a directory. The destination
    can already exist -- a project registered after this version shipped
    writes straight to the new location -- and moving a directory onto an
    existing one either fails or, on POSIX, nests it inside itself. Entry by
    entry also means a single unreadable project does not strand the other
    seven.

    An entry already present at the destination is left alone rather than
    overwritten. The new location is the live one by the time this runs, so
    its copy is the newer of the two; a legacy `next-session.md` written
    before the move must not be allowed to overwrite a handoff written after
    it.
    """
    legacy = projects_root()
    if LEGACY_PROJECTS_DIR == legacy:
        return 0
    present = dir_present(LEGACY_PROJECTS_DIR)
    if present is None:
        log(f"  Could not examine {LEGACY_PROJECTS_DIR} — the project catalog "
            f"there has been left in place, not migrated")
        return 0
    if not present:
        return 0
    try:
        entries = list(LEGACY_PROJECTS_DIR.iterdir())
    except OSError as exc:
        log(f"  Could not list {LEGACY_PROJECTS_DIR}: {exc}")
        return 0
    legacy.mkdir(parents=True, exist_ok=True)
    moved = 0
    for src in entries:
        dest = legacy / src.name
        occupied = path_present(dest)
        if occupied is None:
            log(f"  Could not examine {dest} — {src} left in place")
            continue
        if occupied:
            log(f"  {dest} already exists — {src} left in place, not merged")
            continue
        moved += _move_legacy(src, dest)
    return moved


# ── instance ────────────────────────────────────────────────────
class Instance:
    """One named unit of work: a session plus its state files."""

    def __init__(self, display_name: str):
        self.display_name = display_name
        self.id = safe_instance_id(display_name)
        self.session = self.id
        # Whether the last launch managed to clear the previous session's
        # exit code. Only `start_session` can know, and only the loop asks;
        # anything that never launches a session has nothing stale to read,
        # which is why the optimistic value is the right default here.
        self.exit_file_cleared = True
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
    def loop_startup_file(self) -> Path:
        """A supervisor exists for this instance but has not published its pid.

        The loop pid file cannot answer that question, because it is written
        near the *end* of a startup that takes upwards of 105 ms — and it has
        to stay that way, since every reader of the code record treats it as
        the commit point. So liveness during startup gets its own record,
        written by the spawning parent the instant ``Popen`` returns and
        removed once the pid file exists.

        The file holds one pid, and its mtime bounds how long it may be
        believed without one: on Windows ``sys.executable`` is often a
        launcher shim that re-execs the real interpreter and exits, so the
        pid the parent records can be dead while the supervisor it started is
        perfectly healthy. The child overwrites the record with its own pid as
        its first act, which closes that gap for everything after the import.
        """
        return RESTART_DIR / f"{self.id}.loopstarting"

    @property
    def loop_args_file(self) -> Path:
        """The arguments loop mode was started with.

        Recorded so a supervisor can be replaced (``operator restart-loop``)
        without having to reconstruct them from the launch spec, where they
        are already mixed with the preamble and the flags loop mode adds.
        """
        return RESTART_DIR / f"{self.id}.loopargs.json"

    @property
    def loop_code_file(self) -> Path:
        """Which operator source the running supervisor actually loaded.

        A supervisor is long-lived and imported its code once, at startup, so
        a fix landing afterwards does not reach it (that is what
        ``restart-loop`` is for). Nothing recorded *which* code it started
        with, so neither a person nor the trace could tell a supervisor
        running today's fix from one running last week's — and the records
        both produce are byte-identical in shape.
        """
        return RESTART_DIR / f"{self.id}.loopcode.json"

    @property
    def nochange_file(self) -> Path:
        """Consecutive sessions that left the project's git state untouched.

        On disk rather than in memory because a supervisor can be replaced
        mid-run (``operator restart-loop``), and a breaker that forgets its
        count every time the supervisor is swapped would never trip.
        """
        return RESTART_DIR / f"{self.id}.nochange"

    @property
    def unaccounted_file(self) -> Path:
        """Consecutive sessions that ended unaccounted for and changed nothing.

        Kept apart from ``nochange_file`` rather than sharing its count: two
        killed sessions and one idle one are not three of anything, and
        summing them is what let a loop be retired for idleness it never
        showed. On disk for the same reason as the streak beside it.
        """
        return RESTART_DIR / f"{self.id}.unaccounted"

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

    def read_nochange_count(self) -> int | None:
        """Consecutive no-change sessions recorded so far.

        ``None`` means the count could not be established, which is not the
        same as zero: silently reading an unreadable counter as "no evidence
        of stalling yet" is how a circuit breaker ends up permanently off
        without anyone noticing.
        """
        return self._read_streak(self.nochange_file)

    def read_unaccounted_count(self) -> int | None:
        """Consecutive unaccounted-for endings recorded so far.

        Same tri-state as ``read_nochange_count`` and for the same reason.
        """
        return self._read_streak(self.unaccounted_file)

    def _read_streak(self, path: Path) -> int | None:
        present = path_present(path)
        if present is False:
            return 0
        if present is None:
            return None
        try:
            value = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        return value if value >= 0 else None

    def save_nochange_count(self, count: int) -> bool:
        """Persist the streak. ``False`` when it could not be written.

        Losing this costs the breaker its memory across a supervisor swap,
        never the running session, so — like ``_save_loop_args`` — it must not
        take an unattended supervisor down with it.
        """
        return self._save_streak(self.nochange_file, count, "no-change")

    def save_unaccounted_count(self, count: int) -> bool:
        """Persist the unaccounted-ending streak. ``False`` when it could not
        be written, on the same terms as ``save_nochange_count``."""
        return self._save_streak(self.unaccounted_file, count, "unaccounted")

    def _save_streak(self, path: Path, count: int, label: str) -> bool:
        tmp = RESTART_DIR / f"{path.name}.tmp"
        try:
            tmp.write_text(f"{count}\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log(f"  Warning: could not record the {label} count: {exc}")
            return False
        return True

    def cleanup_files(self) -> None:
        for path in (self.restart_marker, self.managed_file, self.spec_file,
                     self.pid_file, self.exit_file, self.session_file,
                     self.loop_pid_file, self.loop_startup_file,
                     self.detach_marker, self.stop_marker,
                     self.loop_args_file, self.loop_code_file,
                     self.restart_lock_file,
                     self.nochange_file, self.unaccounted_file):
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


def _git_output(args: list[str], cwd: Path) -> str | None:
    """Run a read-only git command. ``None`` when it could not be answered.

    Every failure mode collapses to ``None`` on purpose: git missing, the
    directory not being a repository, a lock held by whoever is working in
    there, a timeout. The caller must treat that as "unknown", never as
    "nothing changed".
    """
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=GIT_PROBE_TIMEOUT, **NO_WINDOW_KWARGS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _git_output_with_input(args: list[str], cwd: Path,
                           stdin_text: str) -> str | None:
    """``_git_output`` for a command that reads paths on stdin.

    Same contract: every failure mode collapses to ``None``, meaning
    "unknown", never "nothing changed".
    """
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), input=stdin_text,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=GIT_PROBE_TIMEOUT, **NO_WINDOW_KWARGS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _worktree_paths(cwd: Path) -> list[Path] | None:
    """Every checkout attached to this repository, primary first."""
    out = _git_output(["worktree", "list", "--porcelain"], cwd)
    if out is None:
        return None
    paths = [line[len("worktree "):].strip()
             for line in out.splitlines() if line.startswith("worktree ")]
    if not paths:
        # A repository always has at least its primary checkout, so an empty
        # list means the output was not what we think it was.
        return None
    return [Path(p) for p in paths]


def _uncommitted_content(path: Path) -> str | None:
    """The *content* of everything uncommitted in one worktree.

    ``git status`` names the paths that changed and how, but not what is in
    them: a file edited twice produces the identical ``" M app.py"`` line
    both times. A loop iterating on the same uncommitted file would therefore
    fingerprint identically session after session and be stopped for making
    no progress, which is the most expensive way this breaker can be wrong.

    ``git diff`` (worktree against index) and ``git diff --cached`` (index
    against HEAD) together cover every tracked byte, and neither needs HEAD to
    exist — in a repository with no commits yet ``--cached`` diffs against the
    empty tree instead of failing, so no unborn-HEAD special case is needed.
    Untracked files are in neither diff, so their contents are hashed
    separately. ``--exclude-standard`` applies the ignore rules, which is what
    keeps build output and ``__pycache__`` from churning the fingerprint on
    every run and silently disarming the breaker. Entries ending in ``/`` are
    skipped: git does not descend into a nested repository, so it reports one
    collapsed directory entry that ``hash-object`` cannot read. A linked
    worktree is the common case and loses nothing, because every worktree is
    fingerprinted in its own right; anything else still registers through the
    ``git status`` line that names it.

    ``None`` means the question could not be answered.
    """
    parts = []
    for args in (["diff"], ["diff", "--cached"]):
        out = _git_output(args, path)
        if out is None:
            return None
        parts.append(out)
    untracked = _git_output(
        ["ls-files", "--others", "--exclude-standard", "-z"], path)
    if untracked is None:
        return None
    names = [n for n in untracked.split("\0") if n and not n.endswith("/")]
    # ``ls-files -z`` is NUL-delimited precisely because a filename may
    # contain a newline, but ``hash-object --stdin-paths`` is newline-
    # delimited and has no NUL equivalent. Feeding it such a name splits it
    # into two paths that do not exist, so the call fails, the fingerprint
    # collapses to "unknown", and the breaker is off for the rest of the run
    # — silently, because "unknown" is indistinguishable from an unreadable
    # repository. The rare name goes through argv, where no delimiter exists
    # to be confused, and the common case keeps its single batched call.
    batched = [n for n in names if "\n" not in n]
    individually = [n for n in names if "\n" in n]
    if batched:
        # One batched call: the file count is unbounded, and a process per
        # file would make the probe's cost scale with someone else's mess.
        hashed = _git_output_with_input(
            ["hash-object", "--stdin-paths"], path, "\n".join(batched) + "\n")
        if hashed is None:
            return None
        parts.append(hashed)
    for name in individually:
        hashed = _git_output(["hash-object", "--", name], path)
        if hashed is None:
            return None
        # The name is digested alongside its content: two such files swapping
        # contents would otherwise produce the same unordered set of hashes.
        parts.append(f"{name}\0{hashed}")
    return "\0".join(parts)


def workspace_fingerprint(cwd: Path) -> str | None:
    """Digest of everything in this repository a session could have changed.

    ``None`` means the question could not be answered, which is deliberately
    distinct from "nothing changed".

    Scope is the whole repository, not ``cwd``. Work here happens on branches
    in linked worktrees under ``.worktrees/``, so a session can commit an
    entire feature without the primary checkout's HEAD or ``git status``
    moving at all. Fingerprinting only the current directory would therefore
    report a productive session as idle and eventually stop a loop that was
    working perfectly. Every local ref and every worktree's uncommitted state
    is included instead.

    ``refs/remotes`` is included for the same reason: a session that commits,
    pushes, then deletes its local branch and worktree leaves local state
    exactly as it found it, and only the remote-tracking ref still records
    that the work happened.

    A detached HEAD is covered by each worktree's ``# branch.oid`` line
    rather than by the refs, because a commit made while detached advances no
    ref at all.
    """
    refs = _git_output(
        ["for-each-ref", "--format=%(objectname) %(refname)",
         "refs/heads", "refs/tags", "refs/stash", "refs/remotes"], cwd)
    if refs is None:
        return None
    worktrees = _worktree_paths(cwd)
    if worktrees is None:
        return None

    digest = hashlib.sha256()
    digest.update(refs.encode("utf-8", "replace"))
    for path in worktrees:
        present = dir_present(path)
        if present is False:
            # git still lists a worktree whose directory has been removed. It
            # cannot be holding changes, so it is recorded as absent rather
            # than making the whole fingerprint unknown.
            digest.update(f"\0{path}\0<absent>".encode("utf-8", "replace"))
            continue
        if present is None:
            return None
        # ``--branch`` in the v2 format is what makes a detached HEAD
        # visible. A commit on a detached checkout advances no ref, so
        # ``for-each-ref`` above cannot see it and the tree is clean
        # afterwards — the whole repository would fingerprint identically
        # across a session that committed real work, and the breaker would
        # stop a loop that was being productive. ``# branch.oid`` carries the
        # commit itself. v2 is used rather than a second ``rev-parse`` probe
        # because it costs no extra process and, unlike ``rev-parse HEAD``,
        # it still exits 0 in a repository with no commits yet (reporting
        # ``(initial)``) rather than failing and reading as "unknown".
        status = _git_output(
            ["status", "--porcelain=v2", "--branch",
             "--untracked-files=all"], path)
        if status is None:
            # The worktree we cannot read is exactly the one that might hold
            # the change, so no verdict is available for the repository.
            return None
        content = _uncommitted_content(path)
        if content is None:
            return None
        digest.update(f"\0{path}\0".encode("utf-8", "replace"))
        digest.update(status.encode("utf-8", "replace"))
        digest.update(content.encode("utf-8", "replace"))
    return digest.hexdigest()


def evaluate_progress(count: int | None, before: str | None,
                      after: str | None, *,
                      ending_accounted_for: bool) -> tuple[int | None, str]:
    """Fold one finished session into the no-change counter.

    Returns ``(count, verdict)`` where verdict is ``changed``, ``unchanged``,
    ``unaccounted`` or ``unknown``. An unknown session leaves the counter
    exactly as it was: it neither counts toward stopping the loop nor clears
    what came before.

    Progress is judged before the counter is consulted, so a session that
    demonstrably changed something resets the streak to zero even when the
    previous count was unreadable. Reporting known progress as "unknown"
    would leave a corrupt counter file corrupt forever, and a breaker that
    can never be re-armed is one that has silently switched itself off.

    An unreadable count is healed the same way when the session demonstrably
    changed *nothing*, by restarting the streak at one. The argument is the
    same one, and leaving it out was a real hole: a stuck agent is exactly
    the case where no session ever writes the counter, so a file that went
    corrupt would stay corrupt and the breaker would be off for the rest of
    the run — precisely when it was needed. Restarting at one can only ever
    undercount (the streak really is at least this session), so the error it
    can make is letting the loop run longer, never stopping a healthy one.

    ``ending_accounted_for`` is what keeps this counter about *idleness*.
    Changing nothing is evidence that an agent had nothing to do only if the
    session ended the way the loop expects — a handoff, or an exit the runner
    saw. A session killed from outside has usually not committed at the point
    it dies, so it is indistinguishable from an idle one by fingerprint
    alone, and charging it here retires the loops being killed fastest. Such
    a session is not evidence *either way*: it neither advances this streak
    nor clears it, exactly like an unmeasurable one. It is counted separately
    by ``evaluate_unaccounted``, so the loop stays bounded.

    There is deliberately no default. The caller must decide, because the
    silent version of this parameter is the bug it exists to fix.
    """
    if before is None or after is None:
        return count, "unknown"
    if before != after:
        return 0, "changed"
    if not ending_accounted_for:
        # Not healed to 1 here as an unreadable count would be for a genuine
        # no-change session: there is nothing to heal *from*. This session
        # says nothing about idleness, so inventing a streak length from it
        # would be entering a guess as an observation.
        return count, "unaccounted"
    if count is None:
        return 1, "unchanged"
    return count + 1, "unchanged"


def evaluate_unaccounted(count: int | None, verdict: str) -> int | None:
    """Fold the same session into the unaccounted-ending streak.

    Takes ``evaluate_progress``'s verdict rather than re-deciding, so the two
    counters cannot disagree about what the session was.

    A session that changed something clears this streak as well: whatever
    ended it, work landed, and the loop is worth continuing. Anything the
    fingerprint could not settle leaves it alone, for the same reason the
    other counter is left alone — "could not tell" is not "ended badly".
    """
    if verdict == "changed":
        return 0
    if verdict != "unaccounted":
        return count
    if count is None:
        return 1
    return count + 1


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


def project_handoff_file(cwd: Path,
                         instance_id: str = "") -> "Path | None | _CatalogUnreadable":
    """Resolve the handoff path for a project directory.

    Looks the directory up in ``~/.operator/projects/catalog.csv`` (the same
    catalog ``handoff``/``handoff_tool.py`` use) and returns the path the
    handoff file *would* live at, regardless of whether it currently exists.
    Returns None if the directory has no catalog entry at all, and
    :data:`CATALOG_UNREADABLE` if the catalog could not be read, which is a
    different answer and must not share a return value with the first.

    Handoffs are keyed by **instance**: ``handoff/{instance_id}.md``. An empty
    ``instance_id`` yields the project directory's legacy ``next-session.md``,
    which is what a pre-migration project still has on disk and what a caller
    with no instance in hand can meaningfully ask about.

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
                    base = project_dir(guid)
                    if instance_id:
                        return base / "handoff" / f"{instance_id}.md"
                    return base / "next-session.md"
    except OSError:
        return CATALOG_UNREADABLE
    return CATALOG_UNREADABLE if undecided else None


def crash_recovery_verdict(workdir: Path, instance_id: str = "") -> bool:
    """Did the session before this launch end without leaving a handoff?

    A missing handoff file means the previous session never reached `handoff`
    — most likely a crash (operator itself dying, Windows rebooting, an
    external kill mid-turn) rather than a clean stop. Telling the agent lets
    it act accordingly.

    **This is a claim about one moment and must be re-decided at every
    launch.** It used to be decided once, before the supervisor's loop
    started, and the answer was then baked into a preamble reused by every
    session of the run. A run is long-lived — `copilot-tools` reached session
    #223 on a run started 25 days earlier — so one verdict taken at loop start
    was still being reported to sessions hundreds of handoffs later. It failed
    in both directions: a loop that started with no handoff told every later
    session its predecessor had crashed, contradicting the handoff sitting on
    disk that the agent had just been told to read; and a loop that started
    with one never reported a genuine mid-turn kill afterwards, which is
    precisely the event this note exists to surface. Queued mail was moved to
    per-launch delivery for the same reason and this was left behind.

    An *unregistered* project is a different situation entirely: no catalog
    entry means no handoff file could ever have been written there, so the
    absence proves nothing and must not be reported to the agent as a crash.

    The project-keyed ``next-session.md`` is consulted as a fallback because
    it is what a project that has not yet been through
    ``handoff_tool.migrate_project_handoff`` still has on disk. Migration
    happens on the next *write*, so between this change shipping and this
    instance's next handoff, the instance file legitimately does not exist
    while a real handoff sits beside it. Reporting that as a crash would tell
    the agent its predecessor died in the one situation where the predecessor
    demonstrably did not.
    """
    handoff_file = project_handoff_file(workdir, instance_id)
    if handoff_file is CATALOG_UNREADABLE:
        # The catalog would not open. That establishes nothing about whether
        # this project is registered, so it must not be reported as either a
        # missing handoff or an unregistered project.
        log("  Could not read the project catalog — not reporting this as "
            "crash recovery")
        return False
    if handoff_file is None:
        log("  Project is not registered in the catalog — no handoff file "
            "is expected here")
        return False
    # Probed once and held: asking twice invites the two answers to disagree,
    # and the tri-state exists so the unknown case can be decided deliberately.
    present = path_present(handoff_file)
    if present is None:
        # Telling the agent a handoff is missing is a claim about the last
        # session. A probe that failed has not established anything.
        log(f"  Could not examine {handoff_file} — not reporting this as "
            f"crash recovery")
        return False
    if present:
        return False
    if instance_id:
        legacy = handoff_file.parent.parent / "next-session.md"
        legacy_present = path_present(legacy)
        if legacy_present is None:
            log(f"  Could not examine {legacy} — not reporting this as "
                f"crash recovery")
            return False
        if legacy_present:
            log(f"  No handoff at {handoff_file}, but an unmigrated one is "
                f"at {legacy} — not reporting this as crash recovery")
            return False
    log("  No handoff file found for this project — treating this as "
        "crash recovery")
    return True


def _loop_work_db(workdir: Path):
    """The claim/session database for the project being supervised, or ``None``.

    Quiet and total, unlike its CLI equivalent ``_session_db``: the loop must
    launch a session whether or not this project is registered, so every
    failure here becomes ``None`` and a log line rather than an exception.
    Resolved from the *primary* checkout so a loop running inside a worktree
    finds the project's real entry instead of minting a second one.
    """
    try:
        found = catalog_guid(primary_repo_root(workdir))
        if found.guid is None:
            return None
        return operator_session.db_path(project_dir(found.guid))
    except Exception as exc:                                # noqa: BLE001
        log(f"  Could not resolve this project's work database ({exc})")
        return None


def _loop_start_session(db, instance: "Instance", session_num: int):
    """Open the session log and settle what this instance is to work on.

    FR-2 wants the assignment resolved before the agent's first token, and the
    only party that can do that is the one launching it. An agent left to work
    it out for itself pays for the reasoning on every session, can still get
    it wrong, and needs the rules in its context permanently to get it right.
    Here it is one query whose answer is already in the preamble.

    Total for the same reason as :func:`_loop_work_db`: a missing assignment
    costs the agent a hint, and must not cost it a session.
    """
    if db is None:
        return None
    try:
        operator_session.init_db(db)
        return operator_session.start_session(
            db, instance=instance.id, session=session_num)
    except Exception as exc:                                # noqa: BLE001
        log(f"  Could not resolve this session's assignment ({exc})")
        return None


def _loop_heartbeat(db, instance_id: str) -> None:
    """Refresh whatever claim this instance currently holds.

    The supervisor heartbeats, not the agent. It is the only party that knows
    the session is alive from the process table rather than from the agent's
    opinion of its own progress -- an agent asked to report its own liveness
    reports it right up to the moment it stops being able to, which is the
    only moment the answer mattered.

    The claim is re-read rather than remembered from the assignment, because
    an agent can take one mid-session; caching the item resolved at launch
    would leave exactly those claims un-refreshed until they went stale, and
    the whole point of the cascade is that a stale claim gets taken away.
    """
    if db is None:
        return
    try:
        held = work_claims.claim_for_instance(db, instance_id)
        if held is not None:
            work_claims.heartbeat(db, item=held.item, instance=instance_id)
    except Exception as exc:                                # noqa: BLE001
        log(f"  Could not refresh this instance's work claim ({exc})")


def build_preamble(agent_name: str, instance: Instance, crash_recovery: bool = False,
                   assignment=None, code_state: str = CODE_CURRENT) -> str:
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
    clauses: list[str] = []
    if crash_recovery:
        clauses.append(
            "This session is being resumed because a handoff file could not be "
            "found for this project. Either a crash occurred or the previous session "
            "ended without the handoff being written. If you intended to end the "
            "session, please make sure you write a handoff first next time."
        )
    notice = _code_state_notice(code_state, instance, crash_recovery)
    if notice:
        clauses.append(notice)
    # The assignment is resolved by `operator session start` before the agent's
    # first token (FR-2), and reaches it here. Nothing is said when there is
    # nothing to say: `describe` returns "" for an unassigned session, and an
    # always-present line reading "you have no assignment" would be paid for on
    # every token of every session that has none.
    if assignment is not None:
        described = operator_session.describe(assignment)
        if described:
            clauses.append(described)
    # Numbered from the clauses actually collected, rather than from a counter
    # incremented alongside them. Both spellings produce the same text today;
    # they differ in what they make *possible*. A counter is two statements --
    # bump it, append the text -- and nothing ties them together, so it can be
    # bumped without appending (leaving a gap in the numbering) or appended to
    # without bumping (using a number twice). The literal "(7)" that this
    # replaced was the second of those, and it survived because at the time it
    # was written only one optional clause could precede it.
    #
    # Numbering a list at render time makes both unrepresentable: a clause that
    # is not appended cannot consume a number, because the number *is* its
    # index. This is deliberately not a test -- a guard that has to fire is
    # weaker than a shape that cannot fail, and this one previously cost a
    # surviving mutant that could only be argued equivalent by reasoning about
    # which clause happened to be last.
    for offset, body in enumerate(clauses):
        text += f" ({BASE_CLAUSES + 1 + offset}) {body}"
    return text


def _code_state_notice(code_state: str, instance: Instance,
                       crash_recovery: bool) -> str:
    """The staleness caveat for the preamble, or ``""`` when there is none.

    Why this belongs in the preamble at all, when `operator list` already
    reports the same fact: they have different readers. `operator list` is
    read by a human at a terminal who went looking. The preamble is read by
    an agent that did not, and the preamble is where the misinformation
    lands -- 355 launches across the fleet were told a handoff could not be
    found, by supervisors running code from before the verdict was decided
    per launch, and not one of them had any reason to go and check whether
    its wrapper was current. Making staleness legible at the command line
    did not make it legible to the party being lied to.

    Scoped deliberately to *this preamble's own claims* rather than issued as
    a general warning about the repository. The agent cannot act on "some
    code is old"; it can act on "the sentence above about your predecessor
    may have been written by code that no longer exists".

    Silent when the code is current, which is the overwhelmingly common case
    -- a caveat attached to every session is one that stops being read, and
    this instrument exists because the previous one said nothing.
    """
    if code_state == CODE_CURRENT:
        return ""
    # Named so the agent can quote it back, and so the two verdicts are not
    # reported in the same words: one is an observed difference, the other is
    # an absence of evidence, and collapsing them would overstate the second.
    claims = ("the claim above that a handoff could not be found"
              if crash_recovery else "anything above that it decided per launch")
    if code_state == CODE_STALE:
        return (
            "CAUTION — this operator wrapper is running OUT-OF-DATE code. The operator "
            "source on disk has changed since the supervisor that launched you imported "
            f"it, and a supervisor keeps its code for the whole run, so {claims} was "
            "produced by a version that is no longer in the tree. Treat it as "
            "unverified rather than false, and verify anything you would otherwise "
            "have taken on this wrapper's word before acting on it — in particular, "
            "check for a handoff file yourself rather than trusting a claim that none "
            f"exists. `operator list` names the changed files; `operator restart-loop "
            f"{instance.display_name}` picks up the current code, but that restarts "
            "the supervisor you are running under, so raise it with the human rather "
            "than doing it as a side effect of some other task."
        )
    return (
        "CAUTION — this operator wrapper CANNOT SHOW that it is running current code. "
        "The supervisor that launched you either recorded nothing about the operator "
        "source it imported, or that record could not be compared against the tree "
        f"now. This is an absence of evidence, not evidence of staleness: {claims} may "
        "be perfectly correct. But it cannot be confirmed, so verify anything load-"
        "bearing — in particular, check for a handoff file yourself rather than "
        "trusting a claim that none exists. `operator list` reports the same state for "
        "every instance on this machine."
    )


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
    # `remove_file` already logs the failure; what is recorded here is the
    # consequence — an exit code surviving this point belongs to the session
    # that just ended, not the one about to start.
    instance.exit_file_cleared = remove_file(instance.exit_file)
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


class _CodeUnknown:
    """The running code could not be compared against what is on disk."""
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "CODE_UNKNOWN"


class _FileAbsent:
    """A definite answer: the recorded source file is no longer there."""
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "FILE_ABSENT"


FILE_ABSENT = _FileAbsent()

_RUNNING_CODE: "dict | None" = None


def _digest_file(path: Path) -> "str | _FileAbsent | None":
    """sha256 of a file's bytes; ``FILE_ABSENT`` if gone, ``None`` if unknown.

    Three answers, not two. A digest that quietly became a constant when the
    file could not be read would compare equal to itself forever and report
    code it never saw as unchanged -- which is the failure this whole
    fingerprint exists to make impossible, reproduced inside it.

    Absence is separated from unreadability because they support opposite
    conclusions: a module the supervisor loaded that is no longer on disk has
    *definitely* changed, while one behind a denied read has not been
    examined at all.
    """
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except FileNotFoundError:
        return FILE_ABSENT
    except (OSError, ValueError):
        return None


def _loaded_operator_sources(modules: "dict | None" = None) -> list[Path]:
    """The operator's own ``.py`` files that *this process* imported.

    Deliberately not a glob of the directory. The question a staleness check
    has to answer is "has the code I am running changed", which is about the
    files this process loaded -- not about every file that happens to sit
    beside them. A glob would mark a supervisor stale because an unrelated
    tool in the same checkout was edited, and in a repository under active
    development that fires constantly. A notice that always fires is one
    nobody reads, which would leave the instrument no better off than the
    silence it replaced.

    ``modules`` exists so a test can supply the module table instead of
    mutating the real ``sys.modules``. Asserting the negative any other way
    means naming a file and hoping nothing imported it, and the first version
    of that test named a file that does not exist -- which no implementation
    can return, so it passed against a globbing one too.
    """
    here = Path(__file__).resolve().parent
    found: dict[str, Path] = {}
    for module in list((sys.modules if modules is None else modules).values()):
        origin = getattr(module, "__file__", None)
        if not origin:
            continue
        try:
            resolved = Path(origin).resolve()
        except (OSError, ValueError, RuntimeError):
            continue
        if resolved.suffix != ".py":
            continue
        try:
            if here not in resolved.parents:
                continue
        except (OSError, ValueError):
            continue
        found[str(resolved)] = resolved
    return [found[key] for key in sorted(found)]


def _combined_digest(entries: "list[dict]") -> str:
    """One short digest over a set of per-file digests.

    Unreadable and absent files contribute their *state* rather than being
    skipped, so two fingerprints cannot come out equal because a file
    dropped out of both.
    """
    hasher = hashlib.sha256()
    for entry in sorted(entries, key=lambda e: e.get("path") or ""):
        hasher.update((entry.get("path") or "").encode("utf-8", "replace"))
        hasher.update(b"\0")
        hasher.update(str(entry.get("sha256")).encode("utf-8", "replace"))
        hasher.update(b"\n")
    return hasher.hexdigest()[:16]


def running_code_fingerprint() -> dict:
    """Digest of the operator source this process is running. Computed once.

    The cache is the point, not an optimisation. This has to keep answering
    for the code the process *loaded*; recomputing it later would hash
    whatever is on disk by then and report a supervisor as running code it
    has never executed -- stating the confusion the fingerprint exists to
    end, in the fingerprint's own voice.

    Honest about its own resolution: the bytes are read from disk moments
    after import rather than captured by the import itself, so a file edited
    inside that window is recorded as the newer bytes. That is a millisecond
    at startup, and it is the direction that under-reports staleness rather
    than inventing it.
    """
    global _RUNNING_CODE
    if _RUNNING_CODE is None:
        entries = []
        for path in _loaded_operator_sources():
            digest = _digest_file(path)
            entries.append({
                "path": str(path),
                "sha256": None if digest is None
                          else ("absent" if digest is FILE_ABSENT else digest),
            })
        _RUNNING_CODE = {
            "version": TOOLKIT_VERSION,
            "digest": _combined_digest(entries),
            "files": entries,
        }
    return _RUNNING_CODE


def _save_loop_code(instance: Instance) -> None:
    """Record which operator source this supervisor started with.

    Losing this costs a staleness verdict, never the running session, so it
    warns and carries on for the same reason ``_save_loop_args`` does.
    """
    payload = dict(running_code_fingerprint())
    payload["pid"] = os.getpid()
    payload["recorded"] = utcnow()
    tmp = instance.loop_code_file.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, instance.loop_code_file)
    except OSError as exc:
        log(f"  Warning: could not record the running operator code: {exc}")


def _publish_supervisor_records(instance: Instance, user_args: list[str]) -> None:
    """Write this supervisor's startup records, pid file last.

    The order is the point, which is why these three writes live in one named
    function instead of inline where they cannot be tested. Every reader of
    the *code record* gates on the loop pid file first — `_instance_summary`
    and `list_instances` both require `snap["loop_pid"]` before they will say
    anything about `loop_code` — so among those readers the pid file is the
    commit point: once it exists, the record describing that supervisor
    already does.

    Written the other way round -- which is how it was -- a concurrent
    ``operator ls`` lands between the pid file and the code record and sees a
    live supervisor that has recorded nothing, which is now a reportable
    state. It would tell a perfectly healthy supervisor, running the newest
    code there is, to restart. The window is short and the consequence is
    only a printed line, but a notice that is sometimes wrong is the kind
    that stops being read, and this one exists precisely because the previous
    one said nothing.

    The args record has a different reader: `restart_loop` gates on the live
    session rather than on the pid file, and refuses when no args are
    recorded. Writing args first shrinks that window too, so the reordering
    is an improvement there rather than a trade.

    What this ordering does **not** do is make a starting supervisor visible.
    The pid file is written near the end of a startup that already takes
    upwards of 105 ms, and until it exists `_running_loop_pid` reports that
    nothing is running. That window was backlog item 0010, and it was not
    fixable by ordering these three writes: it is closed instead by a
    separate record written before this function is reached and removed
    after it -- see `Instance.loop_startup_file` and `_supervisor_present`.
    Anything acting destructively on "is a supervisor running" must ask
    `_supervisor_present`, not `_running_loop_pid`.
    """
    # Recorded so this supervisor can be replaced later without guessing how
    # it was started. Written every time, so it tracks the live invocation.
    _save_loop_args(instance, user_args)
    # ...and which operator source it is actually running. A supervisor keeps
    # the code it imported for the whole run, so this is the only place the
    # answer is still knowable.
    _save_loop_code(instance)
    instance.loop_pid_file.write_text(str(os.getpid()), encoding="utf-8")
    # The pid file now answers the liveness question, so the startup record
    # has nothing left to say. Removed after the pid file exists, never
    # before: the two together are what make the supervisor continuously
    # visible from `Popen` to exit, and a gap between them is the whole bug.
    remove_file(instance.loop_startup_file)


def loop_code_state(instance: Instance) -> "tuple[str, list[str]]":
    """Is the supervisor running the code that is on disk now?

    Returns ``(verdict, changed_paths)`` where verdict is ``CODE_CURRENT``,
    ``CODE_STALE``, ``CODE_UNRECORDED`` or ``CODE_UNKNOWN``.

    A supervisor imported its code at startup and keeps it for the whole run,
    so an operator fix is inert for every instance already running when it
    landed. That was not a hypothetical: the fix that made ``session_exit``
    record handoff endings landed at 19:36 on 2026-08-04, and every
    supervisor on the machine had started at 13:28 -- so the trace kept
    producing pre-fix records, dated after the fix, with nothing in them
    saying so. Backlog 0001 tells its next reader to scope a re-measurement
    to records "at or after 2026-08-05", and that instruction was already
    false when it was written.

    One observed difference is enough to say stale, even when other files
    could not be read: staleness is established by a single changed file,
    whereas *currency* is a claim about all of them and so cannot survive a
    file nobody could examine.

    A record that is *observed absent* is a fourth answer, not the third one.
    The record is written by the same change that reads it, so a supervisor
    that is running and has left none started before that change existed --
    or could not write one, which ``_save_loop_code`` warns about and
    survives. Either way its verdict is unavailable until it restarts, and
    the remedy is the same as for a stale one. Collapsing that into "cannot
    tell" is what made this instrument silent for the entire population it
    was built for: measured 2026-08-05T11:35Z, every one of the six running
    supervisors predated the record, so all six read ``unknown``, ``operator
    ls`` said nothing, and the output was byte-identical to a machine on
    which every supervisor was current.
    """
    payload, unusable = _read_loop_record(instance)
    if payload is None:
        return unusable, []
    return _compare_recorded_files(payload.get("files"))


def _read_loop_record(instance: Instance) -> "tuple[dict | None, str]":
    """The supervisor's startup record, or why it could not be had.

    Returns ``(payload, "")`` when the record was read, and
    ``(None, verdict)`` otherwise, where the verdict distinguishes
    ``CODE_UNRECORDED`` -- observed absent -- from ``CODE_UNKNOWN``, which is
    "nobody could look". Keeping those apart is the whole reason this
    function exists rather than a ``try/except`` at each call site: collapsing
    them is the defect that made `operator ls` silent for the entire
    population it was built for, and a second reader that re-derived the
    distinction would be free to get it wrong again.

    Two questions are asked of this one record -- *which code did the
    supervisor load* (`loop_code_state`) and *when did it start*
    (`loop_started_at`) -- and they are printed on the same row, so they must
    not disagree about whether the record exists.
    """
    try:
        raw = instance.loop_code_file.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        # Definite: nothing is there, and nothing can be under a path whose
        # parent is a file. Distinguished from the denial below because they
        # support different claims -- this one says the supervisor never
        # recorded, that one says nobody could look.
        return None, CODE_UNRECORDED
    except (OSError, ValueError):
        # Something is there and could not be read (a denial, a directory in
        # its place, bytes that are not UTF-8). "Cannot tell" is the only
        # honest answer, and it must not borrow the confidence of the branch
        # above.
        return None, CODE_UNKNOWN
    try:
        payload = json.loads(raw)
    except ValueError:
        return None, CODE_UNKNOWN
    if not isinstance(payload, dict):
        # Valid JSON that is not an object -- `null`, `[]`, a bare string.
        # `json.loads` raises nothing for these, so the guard above lets them
        # through and `.get` would raise AttributeError out of `operator ls`,
        # taking down the status command for every instance over one damaged
        # file belonging to one. A record we cannot read is the same answer as
        # a record that is not there.
        return None, CODE_UNKNOWN
    return payload, ""


def loop_started_at(instance: Instance) -> "str | None":
    """When the *running supervisor process* started, or ``None``.

    Not the same question as ``RUN_STARTED``, and that difference is the
    point. ``RUN_STARTED`` is persisted in the state file and deliberately
    carried across supervisor restarts (`launch_loop` reads it back with
    ``state.get("RUN_STARTED", run_started)``), so it describes the *run*.
    The supervisor is a process, and a process that died and was relaunched
    is a new one with the same run behind it.

    `_save_loop_code` already stamps ``recorded`` at startup, so this needs no
    new state -- only a reader. It was measurable all along and nothing looked.

    Returns ``None`` when the record is absent or unreadable, which the caller
    must treat as "cannot tell" rather than "did not restart". A missing
    record is already reported in its own right by `loop_code_state`.
    """
    payload, _ = _read_loop_record(instance)
    if payload is None:
        return None
    recorded = payload.get("recorded")
    return recorded if isinstance(recorded, str) and recorded else None


def _compare_recorded_files(files: object) -> "tuple[str, list[str]]":
    """Compare recorded per-file digests against what is on disk now.

    Shared by the two callers that ask the staleness question from opposite
    ends: `loop_code_state` reads another process's record off disk, and
    `own_code_state` hands over this process's own in-memory fingerprint.
    They must not drift, because they are quoted side by side -- `operator
    list` prints one and the session preamble carries the other, and two
    verdicts that disagree about the same supervisor would discredit both.
    """
    if not isinstance(files, list) or not files:
        return CODE_UNKNOWN, []

    changed: list[str] = []
    undecided = False
    for entry in files:
        if not isinstance(entry, dict):
            undecided = True
            continue
        path, recorded = entry.get("path"), entry.get("sha256")
        if not isinstance(path, str) or not path:
            undecided = True
            continue
        if not isinstance(recorded, str):
            # Nothing was known about this file when the supervisor started,
            # so nothing can be concluded about it now.
            undecided = True
            continue
        now = _digest_file(Path(path))
        if now is None:
            undecided = True
            continue
        current = "absent" if now is FILE_ABSENT else now
        if current != recorded:
            changed.append(path)

    if changed:
        return CODE_STALE, sorted(changed)
    if undecided:
        return CODE_UNKNOWN, []
    return CODE_CURRENT, []


def own_code_state() -> "tuple[str, list[str]]":
    """Has the operator source moved on since *this* process imported it?

    The same question `loop_code_state` answers about somebody else, asked
    from inside the process that is actually running the code -- which is
    strictly better evidence, and the reason this does not simply reuse the
    record on disk. Three things stop being possible:

    - The record could have failed to be written. `_save_loop_code` warns and
      carries on, by design, so a supervisor can be running with a
      `loop_code` file belonging to a *previous* supervisor of the same
      instance. Compared against disk that stale record can read
      ``current`` -- a confident all-clear sourced from a process that no
      longer exists.
    - The record could be unreadable, which costs a verdict this process
      never needed to go to disk for.
    - The record has no owner stamped into it that a reader is obliged to
      check, so nothing distinguishes those two cases from a good one.

    `running_code_fingerprint` is cached for the life of the process, so the
    left-hand side here is what was really imported, not a re-read of disk.
    That cache is what makes the comparison mean anything: recomputing both
    sides would compare disk against disk and always say ``current``.

    Never returns ``CODE_UNRECORDED``: an in-memory fingerprint always
    exists, so "nobody wrote it down" is not one of the available answers.
    """
    return _compare_recorded_files(running_code_fingerprint().get("files"))


def _launch_code_state() -> str:
    """`own_code_state` for the per-launch preamble, which may not raise.

    This runs inside an unattended supervisor's launch loop, where an
    unhandled exception ends the run and takes every future session with it.
    A staleness verdict is never worth that, so anything unexpected degrades
    to ``CODE_UNKNOWN``.

    Degrading to ``CODE_UNKNOWN`` rather than ``CODE_CURRENT`` is the whole
    point: the failure directions are not symmetric. ``CODE_UNKNOWN`` prints
    a caveat the agent may not need, which costs a few lines. ``CODE_CURRENT``
    prints a clean bill of health nobody checked, which is exactly the silent
    all-clear this instrument was built to stop -- and it would be
    indistinguishable from the healthy case, so nothing downstream could ever
    catch it.
    """
    try:
        return own_code_state()[0]
    except Exception as exc:  # pragma: no cover - defensive
        log(f"  Warning: could not check whether this supervisor is current: {exc}")
        return CODE_UNKNOWN


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


def _record_supervisor_starting(instance: Instance, pid: int) -> None:
    """Record that a supervisor for ``instance`` exists but is still starting.

    Written twice on purpose. The spawning parent writes it the instant
    ``Popen`` returns, which is the earliest moment anybody knows a
    supervisor exists — earlier than the child can say so itself, because the
    child cannot run a single instruction until the interpreter has started
    and this module has imported. The child then overwrites it with its own
    pid, which is the only pid that stays meaningful on Windows.
    """
    try:
        instance.loop_startup_file.write_text(str(pid), encoding="utf-8")
    except OSError as exc:
        log(f"  Warning: could not record the starting supervisor: {exc}")


def _starting_loop_pid(instance: Instance) -> int | None:
    """PID of a supervisor that exists but has not published its pid file yet.

    ``None`` means no supervisor is starting. Any int means one is, and
    callers must treat it as "present" rather than as a usable pid: ``0`` is
    returned when a supervisor is definitely there but its pid is not
    knowable, which is the honest answer for a record that cannot be read and
    the only one that keeps a destructive caller from concluding absence.

    Two independent grounds for believing the record, because either alone
    fails: the recorded process being alive covers a startup that is taking
    its time, and the record being younger than ``SUPERVISOR_STARTUP_GRACE``
    covers the pid being dead while the supervisor is not — the Windows
    launcher shim exits the moment it has re-execed the real interpreter.
    Requiring both would reopen the window this file exists to close; the
    cost of either is that stop and restart-loop wait, which is bounded and
    reversible, where the cost of concluding absence is a session destroyed
    or a second supervisor started.

    Both grounds are bounded, and neither bound is the other's.
    ``SUPERVISOR_STARTUP_CEILING`` is what stops a live pid being believed
    forever, because a pid is not an identity and the one it names may have
    been reused; see that constant for what an unbounded belief costs.
    """
    path = instance.loop_startup_file
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return None
    except OSError:
        # Not "no supervisor" — "no answer". Pruning is not available either
        # (the same path is unexaminable), so this refuses until it clears.
        return 0
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = 0
    if -CLOCK_SKEW_TOLERANCE <= age:
        # Two independent upper bounds, because the two grounds for belief are
        # worth different amounts of time. A live process is evidence for as
        # long as it lives, up to the ceiling that stops a reused pid speaking
        # forever. An mtime alone is evidence only for the grace.
        if pid > 0 and age < SUPERVISOR_STARTUP_CEILING and _pid_alive(pid):
            return pid
        if age < SUPERVISOR_STARTUP_GRACE:
            return pid
    # Three ways to be outside all of that, and they are the same statement: this
    # record's mtime is no longer evidence that a supervisor is on its way.
    # Above the grace with nothing alive behind it, it is simply old -- one that
    # was going to publish a pid would have by now. Above the ceiling, the pid
    # is alive but the record is far too old to still be about that process.
    # Below the skew tolerance it is dated further ahead
    # than any clock disagreement explains, and believing it would cost an
    # unbounded refusal rather than a bounded one. See the three constants.
    #
    # The future side is inclusive because the tolerance is sized off a
    # filesystem's timestamp granularity, and a filesystem that rounds to 2 s
    # produces an age of *exactly* -2.0 rather than approximately it. A strict
    # bound there would prune every record this instance ever writes, on that
    # filesystem, every time -- reopening the window for the one class of user
    # who cannot see it happening. Costs nothing: the invariant is
    # `believed for <= SUPERVISOR_STARTUP_ALLOWANCE`, and -2.0 is exactly the
    # case that reaches it.
    remove_file(path)
    return None


def _supervisor_status(instance: Instance) -> tuple[int | None, bool]:
    """``(pid, still_starting)`` for this instance, from one pass.

    ``_running_loop_pid`` answers a narrower question — has a supervisor
    finished starting — and every caller that acts destructively on the
    answer needs this one instead. The two were the same function until a
    supervisor's first ~105 ms turned out to be invisible to it, which let
    ``operator stop`` kill a session that a starting supervisor then
    relaunched underneath the user, and ``operator restart-loop`` start a
    second supervisor over the first.

    Both halves come from one pass because they are read from the same two
    files and every caller uses them together. Asking separately means a
    supervisor that publishes its pid, or exits, between the two reads is
    described by one and sized for by the other: a published supervisor that
    exits mid-question gets reported as "still starting" and handed a
    startup-sized budget it has no use for.

    ``still_starting`` is only meaningful when ``pid is not None``; with no
    supervisor at all there is nothing for it to describe.

    Callers that are *confirming a supervisor came up* must keep using
    ``_running_loop_pid``: this is satisfied by the record its own spawn
    wrote, so it would report success before anything had started.
    """
    pid = _running_loop_pid(instance)
    if pid is not None:
        return pid, False
    return _starting_loop_pid(instance), True


def _supervisor_present(instance: Instance) -> int | None:
    """Is *any* supervisor for this instance running, including a starting one?

    The half of ``_supervisor_status`` that most callers need on its own.
    """
    return _supervisor_status(instance)[0]


def _supervisor_where(pid: int | None, still_starting: bool) -> str:
    """How to describe a supervisor whose pid may not mean what it says.

    The pid from ``_supervisor_status`` is only a *running* supervisor's pid
    when it came from the pid file. From a startup record it can be a live
    pid, ``0`` for "not yet knowable", or a launcher shim's pid that is
    already dead — and the last of those is truthy, so
    ``f'pid {pid}' if pid`` reports a dead process as the one in charge,
    for exactly the case the startup record exists to cover.

    Takes both halves rather than re-reading, so what it says cannot
    contradict what its caller decided.
    """
    if not still_starting:
        return f"pid {pid}"
    if not pid:
        return "still starting; pid not yet known"
    return f"still starting; spawned as pid {pid}"


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
    snaps = [instance_snapshot(inst) for inst in instances]
    for snap in snaps:
        print(f"  {_instance_summary(snap)}")
    if not instances:
        print("  (none)")
    # Named individually rather than counted: the remedy is per-instance, and
    # a bare count would leave the reader to work out which ones it meant.
    stale = [s["name"] for s in snaps
             if s["loop_pid"] and s.get("loop_code") == CODE_STALE]
    if stale:
        print("\nThese supervisors are running operator code that has since "
              "changed on disk.")
        print("A supervisor imports its code once and keeps it for the whole "
              "run, so a fix that")
        print("landed afterwards is not running in them, and the records they "
              "write still describe")
        print("the older code. Pick it up without stopping the session:")
        for name in stale:
            print(f"    operator restart-loop {name}")
    # A separate group with the same remedy, because the reason differs and
    # merging them would say something false about one of the two. Reported
    # at all because silence here is indistinguishable from every supervisor
    # being current -- which is how this check came to say nothing on a
    # machine where no supervisor could be checked at all.
    unrecorded = [s["name"] for s in snaps
                  if s["loop_pid"] and s.get("loop_code") == CODE_UNRECORDED]
    if unrecorded:
        print("\nThese supervisors did not record which operator code they "
              "loaded, so whether")
        print("they are up to date cannot be determined. They started before "
              "the record existed,")
        print("or could not write one. The same restart fixes it and picks "
              "up the current code:")
        for name in unrecorded:
            print(f"    operator restart-loop {name}")
    # No remedy line, deliberately: this reports something that has already
    # happened rather than a state to correct, and offering a command would
    # imply the restart is the problem instead of its evidence. What the
    # reader needs is the meaning -- a supervisor that restarted took the
    # session it was running with it, unfinished and unhanded-off.
    restarted = [s["name"] for s in snaps
                 if s["loop_pid"] and supervisor_restarted_after(
                     s["run_started"], s.get("loop_started"))]
    if restarted:
        print("\nThese supervisors started later than the run they are "
              "running, so they are not")
        print("the process that began it. Whatever session each was running "
              "at that moment died")
        print("with it, mid-turn and without a handoff. `up` above dates the "
              "run, which is kept")
        print("across the restart, so it does not show this:")
        for name in restarted:
            print(f"    {name}")
        print("Several at once means something killed them together — check "
              "whether the logon")
        print("session was replaced (Windows: TerminalServices-"
              "LocalSessionManager event 21).")
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

    The marker goes down *before* the check, so a supervisor that becomes
    visible in between still finds it. That ordering is only safe paired with
    the removal below: this function is also called for instances with no
    supervisor at all, and two of ``stop_operator``'s paths return without
    running ``cleanup_files``, so a marker left behind would sit in
    ``RESTART_DIR`` until some future supervisor started and immediately
    stopped itself. The invariant on return is that either a supervisor holds
    the marker, or it is gone because we removed it.
    """
    instance.stop_marker.touch()
    pid, starting = _supervisor_status(instance)
    if pid is None:
        remove_file(instance.stop_marker)
        return
    log(f"  Stop signal sent to loop supervisor for '{instance.display_name}' "
        f"({_supervisor_where(pid, starting)})")
    # A supervisor that has not published yet cannot look at the marker, so a
    # wait shorter than the whole window a record can be believed for is
    # guaranteed to expire before an orphaned one is even eligible to be
    # pruned.
    if starting:
        timeout += SUPERVISOR_STARTUP_ALLOWANCE
    deadline = time.time() + timeout
    while time.time() < deadline and _supervisor_present(instance) is not None:
        time.sleep(0.5)
    # What upholds the invariant in the docstring. Removing unconditionally
    # would reinstate the bug this function exists to fix: a supervisor that
    # is merely slow is still going to read that marker, and the caller is
    # about to kill its session — without the marker it reads that as a crash
    # and relaunches. So the marker is only withdrawn once nothing is there
    # to honour it, which is also the only case where leaving it would strand
    # it for the next supervisor to trip over.
    if _supervisor_present(instance) is None:
        remove_file(instance.stop_marker)


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
    pid, starting = _supervisor_status(instance)
    if pid is None:
        print(f"No background loop supervisor is running for '{target}'.", file=sys.stderr)
        return 1
    instance.detach_marker.touch()
    log(f"Detach requested for loop '{target}' "
        f"({_supervisor_where(pid, starting)})")
    # Same budget reasoning as _do_restart_loop: a supervisor that has not
    # finished starting has to finish before it polls, and an orphaned
    # startup record is only resolved by ageing out of the whole window.
    budget = 20.0 + (SUPERVISOR_STARTUP_ALLOWANCE if starting else 0.0)
    deadline = time.time() + budget
    while time.time() < deadline:
        if _supervisor_present(instance) is None:
            # Gone is not the same as gone *because of us*. A marker still
            # sitting there was never consumed — the supervisor we saw was an
            # orphaned record that aged out, or it exited for its own reasons
            # — and leaving it would make the next supervisor for this
            # instance detach the moment it started.
            unconsumed = path_present(instance.detach_marker) is not False
            remove_file(instance.detach_marker)
            if unconsumed:
                # Reported as a failure, like _do_restart_loop does, and for
                # the same reason: nothing acted on the request. The end state
                # happens to be the one asked for, but saying so would claim
                # an event that never happened, and this function already
                # returns 1 when it finds no supervisor at the top.
                print(f"The supervisor for '{target}' exited without taking "
                      f"the detach request — nothing acted on it.",
                      file=sys.stderr)
                return 1
            print(f"Loop supervisor for '{instance.display_name}' stopped; "
                  f"session left running.")
            print(f"  Re-attach: operator join {instance.display_name}")
            return 0
        time.sleep(0.5)
    remove_file(instance.detach_marker)
    print(f"Loop supervisor for '{target}' did not stop within {budget:g}s.",
          file=sys.stderr)
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
    pid, starting = _supervisor_status(instance)
    if pid is not None:
        instance.detach_marker.touch()
        where = _supervisor_where(pid, starting)
        log(f"Restart requested for loop '{target}' ({where})")
        # Budget derived from how long the supervisor can take to look at the
        # marker, so tuning the poll interval cannot silently break this. A
        # supervisor still starting has to finish starting before it polls at
        # all, so the whole window a startup record can be believed for is
        # part of the wait too.
        budget = (SESSION_ID_WAIT + POLL_INTERVAL * 2 + 15
                  + (SUPERVISOR_STARTUP_ALLOWANCE if starting else 0))
        deadline = time.time() + budget
        while time.time() < deadline:
            if _supervisor_present(instance) is None:
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
        # Described from what was observed *before* the wait: by now there is
        # no supervisor to describe, and a dead shim pid would otherwise be
        # printed as the one that stopped.
        print(f"Old supervisor ({where}) stopped.")
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
    # `_running_loop_pid` and not `_supervisor_present` for the same reason
    # the check is here at all: the startup record is written by the spawn
    # itself, so asking whether one exists would answer yes before the
    # supervisor had executed a single instruction.
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
    # The question here is "will anything relaunch this session?", and a
    # supervisor that is still starting certainly will — it comes up, finds
    # the session gone with no marker, and reads that as a crash. Answering
    # from the pid file alone told the user their session would stay down and
    # then relaunched it seconds later. `is not None`, not truthiness: 0 is a
    # supervisor whose pid is not knowable yet, not the absence of one.
    pid, starting = _supervisor_status(instance)
    if pid is not None:
        print(f"Session '{instance.display_name}' stopped; loop supervisor "
              f"({_supervisor_where(pid, starting)}) will relaunch it shortly.")
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

    # Affiliation is recorded, never enforced. Two of the three 0025 council
    # seats rejected gating delivery on it: no wrong outcome has been traced
    # to a cross-project message, two of the four cross-project threads
    # improved this repository, and a refusal built on an *unknown*
    # affiliation would drop work the sender believed was sent. So every
    # failure below is a blank field, and the send proceeds.
    origin = mail_affiliation.describe_path(Path.cwd())
    destination = mail_affiliation.describe_instance(
        target.id, RESTART_DIR, read_tabs)
    mail_affiliation.attach(msg, origin, destination)
    relation = mail_affiliation.relationship(origin, destination)
    if relation == mail_affiliation.CROSS_PROJECT:
        # stderr, exit 0, message still sent. For the human reading a
        # transcript later, not a gate: the recipient is told the same thing
        # in the delivered line, which is where it can still change a
        # decision.
        print(f"Note: cross-project send — your project "
              f"{origin.project} → recipient project {destination.project}.",
              file=sys.stderr)

    if not queue_only and _can_receive_live(target):
        try:
            MUX.send_keys(target.session, operator_mail.render_line(msg))
        except MuxError as exc:
            print(f"Live delivery failed ({exc}) — queueing instead.",
                  file=sys.stderr)
        else:
            try:
                operator_mail.record_delivered(OPERATOR_HOME, msg)
            except operator_mail.MailError as exc:
                # The message is already on the recipient's screen. This
                # record only feeds `inbox --history`, and failing the send
                # over it would invite a resend of a message they have read.
                print(f"Delivered, but not recorded in history ({exc}).",
                      file=sys.stderr)
            log(f"Message delivered live: {sender} -> {target.display_name}")
            print(f"Delivered to '{target.display_name}' (session is live).")
            return 0

    try:
        operator_mail.queue(OPERATOR_HOME, msg)
    except operator_mail.MailError as exc:
        # Nothing was stored, so this has to be a failure the sender can see.
        # The fault is at the far end -- their mailbox, not ours -- so say
        # whose it is rather than leaving a traceback to be read as our bug.
        print(f"Could not queue for '{target.display_name}': {exc}",
              file=sys.stderr)
        return 1
    log(f"Message queued: {sender} -> {target.display_name}")
    print(f"Queued for '{target.display_name}' — it will be delivered at the "
          f"start of its next session.")
    try:
        print(f"  Pending: {operator_mail.pending_count(OPERATOR_HOME, target.id)}")
    except operator_mail.MailError as exc:
        # The message is already queued; this line is a courtesy. Reporting
        # "Pending: 0" would be false and reporting failure would be worse --
        # the caller would resend a message that is already sitting there.
        print(f"  Pending: unknown ({exc})")
    return 0


REPLY_FLAGS = ("--instance", "--to", "--queue", "--force")


def _reply_usage(stream=None) -> None:
    stream = sys.stderr if stream is None else stream
    print('Usage: operator reply [--instance NAME] [--to NAME] "message"',
          file=stream)
    print("  --instance  who is replying. Defaults to $OPERATOR_INSTANCE.",
          file=stream)
    print("  --to        who to answer. Defaults to whoever wrote to you "
          "most recently.", file=stream)
    print("  --queue     leave it for the next session even if one is running",
          file=stream)
    print("  --force     send to a name the operator does not recognize",
          file=stream)
    print("  --          everything after it is message text, flags and all",
          file=stream)


def reply_message(args: list[str]) -> int:
    """``operator reply "message"`` — answer without restating the addresses.

    This is deliberately sugar over `send_message` rather than a second
    delivery path. Live-versus-queued, the unknown-recipient refusal and the
    archive record are all decisions with earned comments on them in `send`,
    and a parallel implementation would be a second place for them to drift.
    What is genuinely new here is only the two lookups: who is replying, and
    to whom.

    Both lookups refuse rather than guess. An unresolved sender could be
    defaulted to the directory name -- `operator inbox` used to do exactly
    that -- but a reply carries an assertion the recipient will act on, and
    signing it with a name nobody chose puts words in another agent's mouth.
    """
    if args[:1] and args[0] in HELP_FLAGS:
        _reply_usage(sys.stdout)
        return 0

    instance = ""
    recipient = ""
    passthrough: list[str] = []
    body: list[str] = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            body.extend(args[i + 1:])
            break
        if arg.startswith("-") and not (
                arg in REPLY_FLAGS
                or arg.startswith(("--instance=", "--to="))):
            print(f"operator reply: unknown option '{arg}'", file=sys.stderr)
            print("  If it belongs to the message, put it after --:",
                  file=sys.stderr)
            print(f'    operator reply -- "{arg} ..."', file=sys.stderr)
            print("  Nothing was sent.", file=sys.stderr)
            _reply_usage()
            return 2
        if arg in ("--instance", "--to"):
            if i + 1 >= len(args) or not args[i + 1]:
                print(f"{arg} requires a value", file=sys.stderr)
                return 2
            if arg == "--instance":
                instance = args[i + 1]
            else:
                recipient = args[i + 1]
            i += 1
        elif arg.startswith("--instance=") or arg.startswith("--to="):
            # An explicitly empty inline value is a mistake, not an omission.
            # Falling through to the defaults here would sign the reply with
            # $OPERATOR_INSTANCE, or send it to the last correspondent, for a
            # caller who named neither -- which is the misrouting this
            # command exists to refuse.
            flag, value = arg.split("=", 1)
            if not value:
                print(f"{flag} requires a value", file=sys.stderr)
                return 2
            if flag == "--instance":
                instance = value
            else:
                recipient = value
        elif arg in ("--queue", "--force"):
            passthrough.append(arg)
        else:
            body.append(arg)
        i += 1

    text = " ".join(body).strip()
    if not text:
        print("operator reply: no message text.", file=sys.stderr)
        _reply_usage()
        return 2

    if not instance:
        instance = os.environ.get("OPERATOR_INSTANCE", "").strip()
    if not instance:
        print("operator reply: could not tell who is replying.",
              file=sys.stderr)
        print("  Pass --instance NAME, or set OPERATOR_INSTANCE.",
              file=sys.stderr)
        print("  Your instance name is in your session preamble.",
              file=sys.stderr)
        print("  Nothing was sent.", file=sys.stderr)
        return 2

    if not recipient:
        try:
            recipient = operator_mail.last_correspondent(
                OPERATOR_HOME, Instance(instance).id) or ""
        except operator_mail.MailError as exc:
            # The mailbox is unreadable, so "nobody has written to you" and
            # "we could not look" are indistinguishable from here -- and only
            # one of them means the reply should not be sent. Say which, and
            # exit differently: a caller that retries on one of these must
            # not retry on the other, and a shared code makes that
            # undecidable for anything driving this command.
            print(f"operator reply: could not read mail for '{instance}' to "
                  f"find who to answer: {exc}", file=sys.stderr)
            print("  Pass --to NAME to answer anyway. Nothing was sent.",
                  file=sys.stderr)
            return 3
    if not recipient:
        print(f"operator reply: nobody has written to '{instance}', so there "
              "is nothing to reply to.", file=sys.stderr)
        print('  Use: operator send --from NAME --to NAME "message"',
              file=sys.stderr)
        print("  Nothing was sent.", file=sys.stderr)
        return 1

    # `--` guarantees the reply text is never re-parsed as flags, whatever it
    # starts with. The caller already had one chance to say `--`; this second
    # one is ours, and it is why the body is passed as separate words rather
    # than re-joined into a single quoted string.
    return send_message(["--from", instance, "--to", recipient]
                        + passthrough + ["--"] + body)


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

    try:
        if want_history:
            msgs = operator_mail.history(OPERATOR_HOME, instance.id)
        elif peek:
            msgs = operator_mail.pending(OPERATOR_HOME, instance.id)
        else:
            msgs = operator_mail.consume(OPERATOR_HOME, instance.id)
    except operator_mail.MailError as exc:
        # The mailbox is there but could not be read. Printing "No messages."
        # here would be the worst available answer: it is what a healthy empty
        # inbox prints, so an agent that has mail waiting would be told, in the
        # ordinary words, that nobody wrote to it -- and would stop looking.
        print(f"operator inbox: could not read mail for "
              f"'{instance.display_name}'.", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        print("  This is not the same as an empty inbox: messages may be "
              "waiting and unreadable.", file=sys.stderr)
        # `consume` archives one message at a time, so a fault part way
        # through the batch leaves the earlier ones already read. Claiming
        # "nothing has been marked read" on that path was this module's own
        # defect reached through its error handler: a sentence asserting an
        # outcome nobody checked, and the messages it was wrong about had
        # been archived unread and shown to nobody. Ask, then say.
        if exc.consumed:
            print(f"  {len(exc.consumed)} message(s) HAD already been marked "
                  "read before the failure. They are printed below, because "
                  "this is the only time they will ever be offered.",
                  file=sys.stderr)
            print(operator_mail.render_for_terminal(exc.consumed))
        else:
            print("  Nothing has been marked read, so anything there survives "
                  "for the next attempt.", file=sys.stderr)
        return 1

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
    # First act, before any work: the pid the spawning parent recorded may be
    # a launcher shim that has already exited, and only this process knows
    # the pid that will still be alive in a second's time. Overwriting also
    # refreshes the record's mtime, so a supervisor that crashes later in
    # startup stops being believed promptly rather than for the full grace.
    _record_supervisor_starting(instance, os.getpid())
    # Registered rather than left to the `finally` below, because the two
    # startup checks that can end this process -- adoption refusing a session
    # it does not own, and refusing to be a second supervisor -- both call
    # `die()` before that `try` is entered. Without this, a supervisor that
    # correctly refused to start would leave a record making every caller
    # wait out `SUPERVISOR_STARTUP_GRACE` for a process that is already gone,
    # so the obvious retry of `operator restart-loop` would refuse for 30s.
    atexit.register(remove_file, instance.loop_startup_file)
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

    # Whether the *previous* session left a handoff behind is a question about
    # a moment, so it is re-asked before every launch rather than answered once
    # here. See `crash_recovery_verdict`. What is fixed for the whole run is
    # only whether there *was* a predecessor to ask about: at loop start that
    # is exactly "we are continuing an earlier run", and every session this
    # supervisor watches end adds one thereafter.
    #
    # Continuation is read off the session number, not off `resume_id`. A
    # resume id is written only when the previous session reported one and it
    # parses as a UUID, so keying on it would call a run with five sessions
    # behind it a first launch the moment that id went missing -- and the
    # question here is whether a predecessor *existed*, not whether we can
    # resume into it.
    had_predecessor = bool(resume_id) or start_session_num > 1

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
        #
        # `_running_loop_pid` and not `_supervisor_present`, but not because
        # the wider reader would be wrong here -- because by this point it
        # would answer the same thing. This process overwrote the startup
        # record with its own pid as its first act, so the record can only
        # name *us*, and a check against it can never fire. What catches a
        # peer that is merely starting is the spawning caller
        # (`restart_loop`, `start_and_attach_loop`, `start_loop_headless`),
        # which consults `_supervisor_present` before deciding to spawn at
        # all. If that claim ever moved to after this guard, the wider reader
        # here would start seeing the record the *parent* wrote for this very
        # child -- a launcher shim's pid on Windows -- and every supervisor
        # would refuse to start itself, on one platform only.
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
    _publish_supervisor_records(instance, user_args)
    operator_trace.record_supervisor_start(
        OPERATOR_HOME, instance=instance.display_name,
        session=start_session_num, code=running_code_fingerprint())

    # Progress circuit breaker. A fresh run starts a fresh count: --fresh
    # means "forget the previous run", and inheriting its stalled counter
    # would stop the new one after fewer sessions than it is owed.
    workdir = Path.cwd()
    if is_fresh:
        # The count is reset in memory whether or not the file could be
        # removed. Deleting it is disk hygiene; if that fails, reading the
        # stale streak back would let a run started with --fresh stop early,
        # which is exactly what --fresh promises will not happen.
        remove_file(instance.nochange_file)
        nochange = 0
        remove_file(instance.unaccounted_file)
        unaccounted = 0
    else:
        nochange = instance.read_nochange_count()
        unaccounted = instance.read_unaccounted_count()
    if adopt:
        # This supervisor arrived part-way through a session it did not
        # start, so the repository state that session began with is not
        # knowable. Measuring its end against a baseline taken now would read
        # work it had already finished as no work at all, and could stop a
        # loop that had just been productive. The adopted session is
        # unmeasurable by construction; the baseline re-arms from its end.
        baseline = None
    else:
        baseline = workspace_fingerprint(workdir)
    if nochange is None:
        log(f"  Progress breaker: re-arms from the next measurable session "
            f"— cannot read {instance.nochange_file}")
    elif adopt:
        log(f"  Progress breaker: re-arms after the adopted session "
            f"(currently {nochange})")
    elif baseline is None:
        log("  Progress breaker: inactive — no readable git state in "
            f"{workdir}")
    else:
        log(f"  Progress breaker: stops the loop after "
            f"{MAX_NOCHANGE_SESSIONS} consecutive sessions that change "
            f"nothing (currently {nochange})")
        log(f"  Unaccounted endings: stops the loop after "
            f"{MAX_UNACCOUNTED_SESSIONS} consecutive sessions that change "
            f"nothing and end without a handoff or an observed exit "
            f"(currently {'unknown' if unaccounted is None else unaccounted})")
    work_db = None
    last_heartbeat = 0.0
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
                    # An adopted session gets a log row and a heartbeat like
                    # any other. What it does not get is a preamble: nothing
                    # is being launched to read one, so the assignment is
                    # resolved for the record and the claim, not to be said.
                    work_db = _loop_work_db(workdir)
                    _loop_start_session(work_db, instance, session_num)
                    last_heartbeat = 0.0
                else:
                    if marker_set(instance.stop_marker):
                        # A stop request that landed while this supervisor was
                        # still starting. Honoured *before* the launch, not on
                        # the first poll after it: the harm `operator stop`
                        # was reported for is not that the supervisor survives
                        # but that a brand-new agent session gets launched
                        # under someone who asked for everything to stop, and
                        # an agent that runs for two seconds can still commit.
                        remove_file(instance.stop_marker)
                        log(f"Session #{session_num}: stop requested before "
                            f"launch — shutting down without starting one")
                        if MUX.has_session(instance.session):
                            MUX.kill_session(instance.session)
                        instance.cleanup_files()
                        return 0
                    if marker_set(instance.detach_marker):
                        # Same for `operator stop-loop` / the retiring half of
                        # `operator restart-loop`: leave the session alone —
                        # here there is not even one to leave — and exit, so
                        # the caller waiting on this supervisor to go is not
                        # made to wait out a session launch first.
                        remove_file(instance.detach_marker)
                        log(f"Session #{session_num}: detach requested before "
                            f"launch — supervisor exiting")
                        return 0
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
                    # preamble is built per launch too, so mail that arrives
                    # during session #3 must still reach session #4.
                    # Read now, archive only once the session is really up.
                    # The assignment is settled here, before the preamble is
                    # built, so the agent's first token already knows whether
                    # it is resuming an item, being offered one, or free.
                    work_db = _loop_work_db(workdir)
                    assignment = _loop_start_session(work_db, instance,
                                                     session_num)
                    last_heartbeat = 0.0
                    launch_preamble = build_preamble(
                        agent, instance,
                        crash_recovery=(had_predecessor
                                        and crash_recovery_verdict(
                                            workdir, instance.id)),
                        assignment=assignment,
                        code_state=_launch_code_state())
                    try:
                        waiting = operator_mail.pending(OPERATOR_HOME, instance.id)
                    except operator_mail.MailError as exc:
                        # An unreadable mailbox must not kill an unattended
                        # loop, so this session goes ahead without a mail
                        # preamble -- but it goes ahead having said so. The
                        # messages are neither read nor archived, so they are
                        # offered again at the next launch: a jam that is
                        # announced every session, rather than a delivery that
                        # silently never happens.
                        log(f"  Could not read queued mail ({exc})")
                        log("  Continuing without it; nothing was marked read")
                        waiting = []
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
                        try:
                            operator_mail.archive(OPERATOR_HOME, instance.id,
                                                  [m["id"] for m in waiting])
                        except operator_mail.MailError as exc:
                            # The mail was delivered into the session; only the
                            # bookkeeping failed. Left pending, it is delivered
                            # again next launch -- a duplicate the agent can
                            # see, which is the better failure of the two.
                            log(f"  Delivered mail could not be marked read ({exc})")
                            log("  It will be offered again at the next launch")
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
                # How the session that is about to end finished, carried to the
                # converged progress check below rather than re-probed there:
                # by then `remove_file` has cleared the restart marker, so the
                # question is no longer answerable from disk. "Accounted for"
                # means a handoff asked for the restart, or the runner survived
                # to write an exit code — either way something explains the
                # ending. A session that simply vanished explains nothing, and
                # a fingerprint that did not move says nothing about idleness.
                ending_accounted_for = False
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
                        uptime = (None if session_started_at is None
                                  else time.time() - session_started_at)
                        # Probed as a tri-state and recorded as one. `marker_set`
                        # answers False for "not there" and for "could not
                        # look", which is the right call for the *branch* -- one
                        # more poll is cheap -- but writing that False into the
                        # trace would enter a guess as an observation, and the
                        # postmortem reading it has no way to tell them apart.
                        restart_probe = marker_state(instance.restart_marker)
                        if restart_probe is True:
                            log(f"Session #{session_num}: restart signal detected!")
                            crash_failures = 0
                            ending_accounted_for = True
                            _record_session_exit(instance, session_num,
                                                 stop_state, detach_state,
                                                 restart_probe,
                                                 crash_failures, uptime=uptime)
                        else:
                            # No restart was asked for, so the only thing that
                            # can still account for this ending is an exit code:
                            # the runner outlived copilot and wrote one down.
                            # With neither, nobody saw the session end — the
                            # signature of the whole pane being killed — and it
                            # is not chargeable evidence of an idle agent.
                            ending_accounted_for = ending_was_observed(instance)
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
                                                 restart_probe,
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
                    # Copilot is confirmed up, so the claim is provably still
                    # being worked. Throttled: the poll interval is seconds and
                    # the staleness window is minutes, so one write per minute
                    # is as much evidence as the cascade can use.
                    if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                        _loop_heartbeat(work_db, instance.id)
                        last_heartbeat = time.time()
                    if marker_set(instance.restart_marker):
                        log(f"Session #{session_num}: restart signal detected!")
                        crash_failures = 0
                        ending_accounted_for = True
                        # The handoff path arrives here, not above: `handoff`
                        # touches the marker while copilot is still up, so the
                        # supervisor sees the request before it sees the exit.
                        # Recording only the branch above is why every
                        # `session_exit` in the trace carried `restart=False`
                        # -- not because no session ever ended by handoff, but
                        # because the ones that did were never written down.
                        #
                        # Recorded here rather than after the session is
                        # actually torn down, deliberately. If
                        # `stop_session_gracefully` and the kill behind it both
                        # fail, the supervisor dies -- and a record written
                        # after that point is the one that would never exist.
                        # A trace saying "a restart was requested" when the
                        # teardown then failed is recoverable by whoever reads
                        # it next; silence about the last thing that happened
                        # before the supervisor died is not.
                        _record_session_exit(
                            instance, session_num,
                            marker_state(instance.stop_marker),
                            marker_state(instance.detach_marker), True,
                            crash_failures,
                            uptime=(None if session_started_at is None
                                    else time.time() - session_started_at),
                            session_gone=False)
                        restart_requested = True
                        break

                if restart_requested:
                    # Something has now ended under this supervisor's watch, so
                    # from here on there is always a predecessor to ask about.
                    had_predecessor = True
                    log("Restarting copilot...")
                    remove_file(instance.restart_marker)
                    stop_session_gracefully(instance)
                    instance.save_state(session_num, run_started)

                    # The session is over and its writes have landed, so this
                    # is the only honest moment to ask whether it changed
                    # anything.
                    current = workspace_fingerprint(workdir)
                    nochange, verdict = evaluate_progress(
                        nochange, baseline, current,
                        ending_accounted_for=ending_accounted_for)
                    unaccounted = evaluate_unaccounted(unaccounted, verdict)
                    if verdict == "unknown":
                        log(f"Session #{session_num}: cannot tell whether "
                            f"anything changed — progress breaker not advanced")
                    elif verdict == "changed":
                        instance.save_nochange_count(0)
                        instance.save_unaccounted_count(0)
                    elif verdict == "unaccounted":
                        # Deliberately not charged to the idleness streak. A
                        # session nobody saw end had usually not committed yet,
                        # so its unchanged fingerprint is a fact about when it
                        # died and not about what the agent was doing.
                        instance.save_unaccounted_count(unaccounted)
                        log(f"Session #{session_num}: changed nothing in "
                            f"{workdir} and ended with no handoff and no "
                            f"observed exit "
                            f"({unaccounted}/{MAX_UNACCOUNTED_SESSIONS}) — not "
                            f"counted as idleness")
                        if unaccounted >= MAX_UNACCOUNTED_SESSIONS:
                            log(f"Loop stopped: {unaccounted} consecutive "
                                f"sessions ended unaccounted for and changed "
                                f"nothing. That is not idleness — something is "
                                f"ending these sessions. Stopping instead of "
                                f"starting session #{session_num + 1}.")
                            log(f"  What ended them: operator trace "
                                f"--kind session_exit")
                            log(f"  Resume with: operator --loop --name "
                                f"{instance.display_name}")
                            show_run_summary(run_started)
                            instance.cleanup_files()
                            return EXIT_UNACCOUNTED
                    else:
                        instance.save_nochange_count(nochange)
                        log(f"Session #{session_num}: changed nothing in "
                            f"{workdir} ({nochange}/{MAX_NOCHANGE_SESSIONS})")
                        if nochange >= MAX_NOCHANGE_SESSIONS:
                            log(f"Progress breaker tripped: {nochange} "
                                f"consecutive sessions changed nothing. "
                                f"Stopping instead of starting session "
                                f"#{session_num + 1}.")
                            log(f"  Resume with: operator --loop --name "
                                f"{instance.display_name}")
                            show_run_summary(run_started)
                            instance.cleanup_files()
                            return EXIT_NO_PROGRESS
                    # A session whose end could not be measured keeps the old
                    # baseline, so its work is still counted against the next
                    # comparison rather than being lost between two unknowns.
                    if current is not None:
                        baseline = current

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
        remove_file(instance.loop_startup_file)


# ── help ────────────────────────────────────────────────────────
HELP = """operator — Metrics-capturing wrapper for GitHub Copilot CLI

USAGE
    operator                                                    Interactive menu
    operator [--name NAME] [copilot-args...]                   Single session
    operator --loop [--name NAME] [--fresh] [copilot-args...]  Loop mode (backgrounded, auto-attaches)
    operator --loop --headless [--name NAME] [copilot-args...] Loop mode without attaching
    operator send --from NAME --to NAME "message"              Message another instance
    operator reply [--instance NAME] [--to NAME] "message"     Answer whoever wrote to you last
    operator inbox [NAME] [--peek|--history|--json]            Read messages sent to an instance
    operator session start --instance NAME [--project SUB]     Resolve this instance's work assignment, deliver queued mail
    operator session end --instance NAME --status T --next T   Handoff, close the session log, dispose of the claim
    operator work list                                         Who holds which work item, and whether they are running
    operator work request --instance NAME --item REF           Claim a work item
    operator work reclaim --instance NAME --item REF           Take an item whose owner is provably gone (preserves their work)
    operator backlog ready [--explain]                         The items an agent may work, and why the rest are not
    operator backlog close ID [--commit REV|--reject]          End an item's life: shipped, or considered and declined
    operator ownership check [--project SUB]                   Refuse a branch that changed files outside its subproject
    operator NAME                                              Join a running instance
    operator join [NAME]                                       Join (explicit form)
    operator reload NAME                                       Hot-reload launch spec
    operator list                                              Show running instances
    operator projects                                          Per-project feature configuration
    operator projects retire [--yes]                           Move conventions into each project's AGENTS.md
    operator stop [NAME]                                       Stop instance(s) — loop + session
    operator stop-loop NAME                                    Stop only the background loop
    operator restart-loop NAME                                 Replace the loop supervisor (new code), keep session
    operator stop-session NAME                                 Stop only the Copilot session
    operator forget NAME                                       Drop operator state only
    operator report [type]                                     View usage reports
    operator conversations [seed|serve|stats]                  Browse what was said to agents and back
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
        --fresh to reset. Before every launch, if the previous session left
        no handoff file for the project, the preamble notes that this looks
        like crash recovery rather than a clean handoff.

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
    # The earliest anyone can know this supervisor exists. The child cannot
    # say so for itself until the interpreter has started and this module has
    # imported -- a measured 105 ms floor -- and until something says so,
    # `operator stop` and `operator restart-loop` both act as if no
    # supervisor were running. See `Instance.loop_startup_file`.
    _record_supervisor_starting(instance, proc.pid)
    return proc.pid


def start_and_attach_loop(instance: Instance, copilot_args: list[str],
                          is_fresh: bool) -> int:
    """Ensure a background loop supervisor is running for instance, then
    attach to its Copilot session in the current tab.

    This is what `operator --loop` does today: the supervisor never blocks
    the invoking terminal, and there is only ever one tab involved, not one
    for the loop's logs and a second for the session.
    """
    existing_pid, starting = _supervisor_status(instance)
    if existing_pid is not None:
        where = _supervisor_where(existing_pid, starting)
        log(f"Loop supervisor already running for '{instance.display_name}' "
            f"({where}) — attaching")
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
    existing_pid, starting = _supervisor_status(instance)
    if existing_pid is not None:
        where = _supervisor_where(existing_pid, starting)
        log(f"Loop supervisor already running for '{instance.display_name}' "
            f"({where}) — nothing to start")
        print(f"Loop already running for '{instance.display_name}' ({where}).")
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


#: How much later than the run a supervisor must have started before the
#: listing will call it a restart.
#:
#: A supervisor publishes its records *before* the first session of a run is
#: launched, and ``RUN_STARTED`` is stamped by that launch -- so on a healthy
#: fresh run the supervisor is a few seconds *older* than the run, never
#: younger. Any positive margin therefore only has to clear write ordering,
#: and the direction of the error is deliberate: too wide misses a restart
#: that happened within five minutes of a run beginning, too narrow calls a
#: perfectly ordinary startup a restart. The second is the one that matters,
#: because a notice that is sometimes wrong stops being read -- which is how
#: backlog 0001 got where it is.
SUPERVISOR_RESTART_MARGIN = 5 * 60


def supervisor_restarted_after(run_started: str, loop_started: "str | None") -> bool:
    """Did this supervisor start materially later than the run it is running?

    If so it is not the process that began the run: the original was replaced,
    and every session in flight at that moment died with it.

    Tri-state inputs collapse to ``False`` on purpose *and only* where ``False``
    means "no claim made". An unreadable or absent record, or a run with no
    recorded start, yields nothing to compare -- and the honest answer there is
    silence, not "it did not restart". `loop_code_state` already reports a
    missing record in its own right, so the reader is not left with nothing.
    """
    began = _parse_utc(run_started)
    started = _parse_utc(loop_started or "")
    if began is None or started is None:
        return False
    return (started - began).total_seconds() > SUPERVISOR_RESTART_MARGIN


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
        "loop_code": loop_code_state(instance)[0],
        "loop_started": loop_started_at(instance) or "",
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
    # Said next to `up`, which describes the *run* and survives a supervisor
    # being killed and relaunched. Without this the row for a machine whose
    # every supervisor had just been destroyed was byte-identical to the row
    # for a ten-day run that nothing had touched -- measured 2026-08-09T17:27
    # local, when `operator list` reported all eight instances "up 10d 13h"
    # ninety seconds after every one of their supervisors had died with the
    # logon session and been started afresh. That restart is the observable
    # of the event backlog 0001 is about, and nothing surfaced it.
    if snap["loop_pid"] and supervisor_restarted_after(
            snap["run_started"], snap.get("loop_started")):
        parts.append("[supervisor restarted "
                     f"{_age_since(snap['loop_started'])} ago]")
    # Only ever said about a supervisor that is actually running: a stopped
    # instance has no loaded code to be stale, and saying so anyway would
    # attach the notice to every row it cannot act on.
    if snap["loop_pid"] and snap.get("loop_code") == CODE_STALE:
        parts.append("[supervisor running older code]")
    elif snap["loop_pid"] and snap.get("loop_code") == CODE_UNRECORDED:
        # Worded as the observation, not the conclusion. The supervisor did
        # not record what it loaded, so what it is running is genuinely not
        # known -- but that it cannot be checked is itself the finding, and
        # the fix is the same restart.
        parts.append("[supervisor code unrecorded]")
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


# ── project configurations ──────────────────────────────────────
def catalog_projects(catalog=None):
    """Every project the catalog registers, plus the rows that would not read.

    Returns ``(projects, problems)``, or :data:`CATALOG_UNREADABLE` when the
    file itself could not be opened. Those are three answers and not two: an
    absent catalog means nothing is registered yet and the right thing to say
    is "run setup"; an unreadable one establishes nothing at all, and printing
    an empty project list for it would report every project on the machine as
    unregistered on the strength of one permission error.

    ``problems`` carries the rows that were skipped, for the same reason. A
    line that will not parse, or one whose second column is not a usable
    project id, is a row that was *not compared* -- it is not evidence that the
    project it names is absent, and silently dropping it would let a user look
    at this screen, not see their project, and conclude it was never set up.
    """
    catalog = Path(catalog) if catalog is not None else project_catalog_path()
    if file_present(catalog) is False:
        return [], []
    projects: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    try:
        with open(catalog, "r", encoding="utf-8", errors="replace",
                  newline="") as fh:
            for number, row in enumerate(catalog_rows(fh), 1):
                if row is None:
                    problems.append(f"line {number}: could not be parsed as CSV")
                    continue
                if not row or not any(cell.strip() for cell in row):
                    continue
                if len(row) < 2:
                    problems.append(f"line {number}: no project id column")
                    continue
                path = row[0].strip().strip('"')
                guid = row[1].strip().strip('"')
                if not path:
                    problems.append(f"line {number}: no project path")
                    continue
                if not guid_is_usable(guid):
                    problems.append(
                        f"line {number}: {path} has an unusable project id "
                        f"{guid!r}")
                    continue
                if guid in seen:
                    problems.append(
                        f"line {number}: {path} repeats project id {guid}")
                    continue
                seen.add(guid)
                projects.append({"path": path, "guid": guid,
                                 "label": _project_label(path)})
    except OSError:
        return CATALOG_UNREADABLE
    return projects, problems


def _project_label(path: str) -> str:
    """The last component of a catalog path, for a compact listing.

    ``ntpath`` rather than ``os.path``. The catalog stores each path in the
    native form of the platform that *created* the entry, so a Linux machine
    reading a catalog synced from Windows meets ``C:\\Users\\dev\\repos\\app``
    -- and ``posixpath.basename`` returns that whole string, because a
    backslash is an ordinary filename character there. ``ntpath`` is pure
    syntax with no platform dependence and understands drive prefixes, UNC
    paths and both separators, so it is the union of the two rather than a
    guess at which one this row came from.
    """
    trimmed = path.rstrip("/\\")
    return ntpath.basename(trimmed) or path


def _feature_config(project: dict):
    """``(document, values, problem)`` for one project.

    ``problem`` is a message when the configuration exists and could not be
    read, and in that case ``document`` is None and ``values`` is None -- not
    the defaults. Resolving an unreadable file to the defaults would render a
    complete, confident screen about a project whose real choices nobody
    managed to look at, and then write those invented defaults back over the
    file the moment the user touched anything.
    """
    path = project_features.config_path(project["guid"])
    try:
        document = project_features.read_config(path)
    except project_features.FeatureConfigError as exc:
        return None, None, str(exc)
    return document, project_features.resolved_values(document), None


def browse_project_configurations() -> int:
    """List catalogued projects, pick one, then change its features."""
    while True:
        found = catalog_projects()
        if found is CATALOG_UNREADABLE:
            print(f"\nCannot read the project catalog "
                  f"{project_catalog_path()}.", file=sys.stderr)
            print("  Nothing can be listed until it opens; this is not the "
                  "same as no projects being registered.", file=sys.stderr)
            return 1
        projects, problems = found
        print("\n═══ Project Configurations ═══\n")
        if problems:
            print(f"  {project_catalog_path()}:")
            for problem in problems:
                print(f"    ! {problem}")
            print("  Those rows were skipped, so any project they name is "
                  "missing from this list.\n")
        if not projects:
            print(f"  No projects registered in {project_catalog_path()}.")
            print("  A project is registered the first time an agent sets it "
                  "up in that directory.")
            return 0

        rows = []
        for project in projects:
            _, values, problem = _feature_config(project)
            if problem is not None:
                summary = "unreadable configuration"
            else:
                enabled = project_features.enabled_slugs(values)
                summary = (f"{len(enabled)} of "
                           f"{len(project_features.FEATURES)} enabled")
            rows.append((project["label"], summary, project["path"]))

        width = max(len(row[0]) for row in rows)
        for i, (label, summary, path) in enumerate(rows, 1):
            print(f"  {i:>2}) {label:<{width}}  {summary}")
            print(f"      {path}")
        retire_index = len(projects) + 1
        offer_retirement = user_instructions_present()
        if offer_retirement:
            print(f"\n  {retire_index:>2}) Retire {global_instructions_path()}")
            print(f"      Give each project above its own "
                  f"{project_instructions.AGENTS_NAME} instead. That file is "
                  "read by")
            print("      every session on this machine, project or not.")
        print()
        upper = retire_index if offer_retirement else len(projects)
        choice = _prompt_line(
            f"Choose a project [1-{upper}] (blank to go back): ")
        if not choice:
            return 0
        try:
            index = int(choice)
        except ValueError:
            print("Not a number.", file=sys.stderr)
            continue
        if offer_retirement and index == retire_index:
            retire_user_instructions()
            continue
        if not 1 <= index <= len(projects):
            print("Out of range.", file=sys.stderr)
            continue
        show_project_config(projects[index - 1])


def show_project_config(project: dict) -> int:
    """The feature list for one project. Each change is written immediately.

    Written on each change rather than on the way out, because the way out
    includes Ctrl-C and a closed terminal, and a screen that shows a feature
    as off while the file still says on is worse than no screen at all.
    """
    path = project_features.config_path(project["guid"])
    while True:
        document, values, problem = _feature_config(project)
        print(f"\n═══ {project['label']} ═══\n")
        print(f"  {project['path']}")
        if problem is not None:
            print(f"\n  ! {problem}", file=sys.stderr)
            print("  Refusing to show or change features that could not be "
                  "read.", file=sys.stderr)
            return 1
        print(f"  {path}"
              f"{'' if document is not None else '  (not written yet — showing defaults)'}")
        unknown = project_features.unknown_entries(document)
        if unknown:
            print(f"\n  ! Settings this build has no feature for: "
                  f"{', '.join(unknown)}")
            print("    They are left untouched by anything changed here.")

        features = project_features.FEATURES
        width = max(len(f.name) for f in features)
        print()
        for i, feature in enumerate(features, 1):
            shown = project_features.describe_value(feature.slug,
                                                    values[feature.slug])
            print(f"  {i:>2}) {feature.name:<{width}}  {shown}")
        # Offered only while nothing has been written, because that is exactly
        # the state ``project_instructions._values_for`` refuses. The flags
        # ship off, so answering that refusal by hand costs one toggle per
        # feature per project; on a machine with eight registered projects
        # that is dozens of keystrokes to record the answer somebody already
        # has. A refusal is only defensible when saying "yes, these" is cheap.
        record = len(features) + 1 if document is None else None
        if record is not None:
            print(f"  {record:>2}) Record these as chosen "
                  f"(changes nothing; stops regeneration refusing)")
        back = (record or len(features)) + 1
        print(f"  {back:>2}) Back")
        print()
        choice = _prompt_line(
            f"Choose a feature [1-{back}] (blank to go back): ")
        if not choice:
            return 0
        try:
            index = int(choice)
        except ValueError:
            print("Not a number.", file=sys.stderr)
            continue
        if index == back:
            return 0
        if record is not None and index == record:
            try:
                project_features.write_config(
                    path, {f.slug: values[f.slug] for f in features},
                    document=document)
            except project_features.FeatureConfigError as exc:
                print(f"Not saved: {exc}", file=sys.stderr)
                continue
            print(f"  Recorded in {path}")
            continue
        if not 1 <= index <= len(features):
            print("Out of range.", file=sys.stderr)
            continue

        feature = features[index - 1]
        chosen = _pick_feature_value(feature, values[feature.slug])
        if chosen is None or chosen == values[feature.slug]:
            continue
        try:
            project_features.write_config(path, {feature.slug: chosen},
                                          document=document)
        except project_features.FeatureConfigError as exc:
            print(f"Not saved: {exc}", file=sys.stderr)
            continue
        print(f"  {feature.name} → "
              f"{project_features.describe_value(feature.slug, chosen)}")


def _pick_feature_value(feature, current: str) -> "str | None":
    """The value the user wants for ``feature``, or None to leave it alone.

    A two-valued feature is toggled rather than submenued -- asking someone to
    pick "on" from a list of {on, off} is a menu that exists to be dismissed.
    Anything with more options gets the list, which is what makes a choice of
    one backend expressible at all: ``tracked-backlog`` is not a boolean, and
    a screen built only from toggles could not offer it.
    """
    if feature.is_flag:
        return project_features.OFF if current == project_features.ON \
            else project_features.ON
    print(f"\n  {feature.name}: {feature.description}\n")
    for i, option in enumerate(feature.options, 1):
        marker = "*" if option.value == current else " "
        print(f"  {i:>2}){marker} {option.value:<14} {option.label}")
    print()
    choice = _prompt_line(
        f"Choose [1-{len(feature.options)}] (blank to leave unchanged): ")
    if not choice:
        return None
    try:
        index = int(choice)
    except ValueError:
        print("Not a number.", file=sys.stderr)
        return None
    if not 1 <= index <= len(feature.options):
        print("Out of range.", file=sys.stderr)
        return None
    return feature.options[index - 1].value


# ── retiring the user-scope instructions file ───────────────────
def global_instructions_path() -> Path:
    """The user-scope instructions file, loaded into every session anywhere."""
    return HOME / ".copilot" / project_instructions.GLOBAL_NAME


def instructions_archive_dir() -> Path:
    """Where a retired user-scope instructions file is kept.

    Under ``~/.operator`` for the reason :func:`projects_root` is: archiving a
    file into the directory whose contents this toolkit does not own is not
    archiving it.
    """
    return operator_home() / project_instructions.ARCHIVE_DIRNAME


def _repo_template_path() -> Path:
    """The template shipped beside this module.

    ``_HERE`` rather than a package resource: this toolkit is installed from a
    checkout, editable or otherwise, and every other path in this file is
    derived the same way.
    """
    return Path(_HERE) / "templates" / project_instructions.TEMPLATE_NAME


def user_instructions_present() -> bool:
    """Whether the retired user-scope file is still sitting there.

    ``is not False`` rather than ``is True``: a file that cannot be *examined*
    is still being loaded into every session, and reporting it absent because
    a stat failed would hide the exact thing this is for.
    """
    return path_present(global_instructions_path()) is not False


def _combine_prompt(project: dict, existing: str) -> bool:
    """Ask before writing into a repository's own ``AGENTS.md``."""
    path = Path(project["path"]) / project_instructions.AGENTS_NAME
    lines = existing.strip().splitlines()
    print(f"\n  {path} already exists ({len(existing)} bytes, "
          f"{len(lines)} lines). First lines:")
    for line in lines[:5]:
        print(f"    | {line}")
    if len(lines) > 5:
        print("    | ...")
    print("\n  Combining appends this project's conventions below what is "
          "there. Nothing already in the file is changed or removed.")
    answer = _prompt_line("  Combine? [y/N]: ").lower()
    return answer in ("y", "yes")


def _catalog_fingerprint():
    """Enough of the catalog's state to notice it changed under us.

    Bytes rather than mtime: a second write within the same filesystem
    timestamp granularity is exactly the case a coarse stamp misses, and two
    agents registering projects a moment apart is the scenario. Unreadable
    reads back as the exception text, so "it went away" also counts as a
    change rather than as "no projects".
    """
    path = project_catalog_path()
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        return f"unreadable: {exc}"


# ── telling git about the AGENTS.md files retirement writes ──────
#: What happened to one repository's ``AGENTS.md`` once git was told about it.
AGENTS_COMMITTED = "committed"
AGENTS_STAGED = "staged"
AGENTS_TRACKED = "already tracked"
AGENTS_NO_REPO = "not a git repository"
AGENTS_FAILED = "could not be staged"


class AgentsTracking:
    """One repository's ``AGENTS.md``, and what git was told about it.

    A plain class rather than a ``@dataclass``, which is what this was until
    `tests/test_entry_points.py` objected. That test executes this module
    straight from its path, with no entry in ``sys.modules`` -- the way the
    installed console script is loaded -- and ``@dataclass`` resolves its
    field annotations through ``sys.modules[cls.__module__]``, which is
    ``None`` there. It raises at import time, so the cost is not a broken
    dataclass but a `operator` command that cannot start at all.
    """
    __slots__ = ("label", "root", "state", "detail")

    def __init__(self, label: str, root: Path, state: str, detail: str = ""):
        self.label = label
        self.root = root
        self.state = state
        self.detail = detail

    def __repr__(self) -> str:
        return (f"AgentsTracking({self.label!r}, {self.root!r}, "
                f"{self.state!r}, {self.detail!r})")



def _git_write(args: list[str], cwd: Path) -> "tuple[bool, str]":
    """Run a git command that changes state. ``(succeeded, why not)``.

    Unlike :func:`_git_output` the failure text is kept rather than collapsed
    into ``None``. Every caller here has to name the repository that refused
    and say why: a write whose failure is indistinguishable from success
    leaves somebody believing a file is staged when it is not, which is the
    exact belief this feature exists to stop being wrong.
    """
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=GIT_PROBE_TIMEOUT, **NO_WINDOW_KWARGS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        text = (proc.stderr or proc.stdout).strip()
        lines = text.splitlines()
        return False, (lines[-1] if lines else f"git exited {proc.returncode}")
    return True, ""


def _commit_would_be_unsafe(root: Path) -> "str | None":
    """Why a commit in ``root`` must not be attempted, or ``None``.

    A commit is refused wherever it would land somewhere nobody is looking.
    A detached HEAD takes the commit and leaves no branch pointing at it. An
    interrupted merge, rebase, cherry-pick or revert has a half-built tree
    whose next git command expects to *finish* that operation, not to find an
    unrelated commit on top of it.

    Staging stays correct in every one of these -- only the commit is
    refused -- which is why this answers about the commit alone.
    """
    if _git_output(["symbolic-ref", "-q", "HEAD"], root) is None:
        return "HEAD is detached"
    for name, why in (
            ("MERGE_HEAD", "a merge is in progress"),
            ("CHERRY_PICK_HEAD", "a cherry-pick is in progress"),
            ("REVERT_HEAD", "a revert is in progress"),
            ("rebase-merge", "a rebase is in progress"),
            ("rebase-apply", "a rebase is in progress")):
        located = _git_output(["rev-parse", "--git-path", name], root)
        if located is None:
            return "the git directory could not be examined"
        # `.exists()` answers False for a path that is there but unexaminable,
        # and raises on a permission denial. Both wrong answers here say "no
        # operation in progress" and let a commit land in a half-built tree,
        # so an unexaminable git directory refuses the commit rather than
        # assuming the best of it.
        try:
            interrupted = (root / located.strip()).exists()
        except OSError as exc:
            return f"the git directory could not be examined ({exc})"
        if interrupted:
            return why
    return None


def _managed_names(root) -> "list[str]":
    """The generated files in *root* that this tool actually manages.

    Built from what is on disk rather than from a fixed pair. ``git add`` and
    ``git commit`` both treat a pathspec that matches nothing as fatal, so a
    project that declined its ``AGENTS.md`` -- and therefore never got a
    ``CLAUDE.md`` either -- would have its staging reported as a git failure
    for naming a file that was correctly never written.

    Existence alone is not the test, and the difference is the whole point.
    ``project_instructions._place_claude`` deliberately leaves a ``CLAUDE.md``
    that carries no managed block alone: it is the user's own file, and
    Claude Code users commonly keep one. Naming it here on the strength of
    its existence would hand that file to ``git add`` and then to a commit
    whose message says ``AGENTS.md`` and whose consent prompt never mentioned
    it -- and if it was already tracked, its uncommitted working-tree edits
    would go in too. That is precisely the failure ``commit_agents_files``
    passes a pathspec to avoid; reading it off disk reintroduced it one
    argument further down. A file is named here only when it carries a
    managed block, which is the same rule that decides whether we may write
    to it at all.
    """
    names = []
    for name in (project_instructions.AGENTS_NAME,
                 project_instructions.CLAUDE_NAME):
        try:
            text = (Path(root) / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if project_instructions.managed_block_present(text):
            names.append(name)
    return names


def stage_agents_files(outcomes) -> "list[AgentsTracking]":
    """Stage every ``AGENTS.md`` that was just written.

    Writing the file and saying nothing to git leaves a repository's own
    conventions in the one state that `git clean -fd` deletes, that never
    reaches a clone, and that is indistinguishable from the scratch checkout
    hygiene exists to sweep away. Staged, it survives all three and reads in
    `git status` as something somebody meant to be there.

    ``CLAUDE.md`` is staged with it, for exactly the same reason: an import
    file that never reaches a clone is worse than no import file, because the
    repository it was written into still reads as though Claude had been
    catered for.

    Staging is unconditional because it creates no commit and destroys
    nothing; committing is the part that has to be asked about.
    """
    tracked: "list[AgentsTracking]" = []
    for outcome in outcomes:
        if outcome.agents_path is None:
            continue
        root = Path(outcome.path)
        names = _managed_names(root)
        if not names:
            continue
        if _git_output(["rev-parse", "--git-dir"], root) is None:
            tracked.append(AgentsTracking(outcome.label, root, AGENTS_NO_REPO))
            continue
        # Tracked-and-unmodified is the one case with nothing to do. It is
        # asked as two questions because `git status` alone cannot tell it
        # apart from a file some .gitignore has swallowed, which reports
        # clean while being entirely absent from the repository.
        is_tracked = _git_output(
            ["ls-files", "--error-unmatch", "--", *names], root) is not None
        pending = _git_output(["status", "--porcelain", "--", *names], root)
        if is_tracked and pending is not None and not pending.strip():
            tracked.append(AgentsTracking(outcome.label, root, AGENTS_TRACKED))
            continue
        ok, why = _git_write(["add", "--", *names], root)
        tracked.append(AgentsTracking(
            outcome.label, root,
            AGENTS_STAGED if ok else AGENTS_FAILED, "" if ok else why))
    return tracked


def commit_agents_files(tracked, origin: str) -> "list[AgentsTracking]":
    """Commit the staged ``AGENTS.md`` files, one commit per repository.

    ``git commit -- <path>`` rather than a bare ``git commit``, so a
    repository carrying unrelated staged work contributes only this file.
    Sweeping somebody's in-progress index into a commit they did not write is
    the failure this feature must not become, and it is the reason a bare
    commit is wrong here even though it is shorter.

    Anything that was not staged is passed through untouched, so the returned
    list still describes every repository.
    """
    name = project_instructions.AGENTS_NAME
    message = (f"docs: add {name} project conventions\n\n"
               f"Written by `operator projects retire` from {origin}.")
    settled: "list[AgentsTracking]" = []
    for entry in tracked:
        if entry.state != AGENTS_STAGED:
            settled.append(entry)
            continue
        unsafe = _commit_would_be_unsafe(entry.root)
        if unsafe:
            settled.append(AgentsTracking(entry.label, entry.root,
                                          AGENTS_STAGED, f"not committed — {unsafe}"))
            continue
        names = _managed_names(entry.root)
        if not names:
            settled.append(entry)
            continue
        ok, why = _git_write(["commit", "-m", message, "--", *names], entry.root)
        settled.append(AgentsTracking(
            entry.label, entry.root,
            AGENTS_COMMITTED if ok else AGENTS_STAGED,
            "" if ok else f"not committed — {why}"))
    return settled


def retire_user_instructions(assume_yes: bool = False) -> int:
    """Give every catalogued project an ``AGENTS.md``, then retire the global file.

    The offer this screen exists to make. A user-scope instructions file is
    read by every Copilot session on the machine, including ones in
    directories that are not projects, so the conventions move to where the
    consent is: the repositories that were actually registered.
    """
    global_path = global_instructions_path()
    print("\n═══ Retire the user-scope instructions file ═══\n")
    print(f"  {global_path}")
    if not user_instructions_present():
        print("\n  Already retired — nothing at that path.")
    found = catalog_projects()
    if found is CATALOG_UNREADABLE:
        print(f"\nCannot read {project_catalog_path()}; nothing can be "
              "written and nothing will be removed.", file=sys.stderr)
        return 1
    projects, problems = found
    if problems:
        for problem in problems:
            print(f"    ! {problem}")
        print(f"\n  Those rows name projects that cannot be given an "
              f"{project_instructions.AGENTS_NAME}, so {global_path} stays.",
              file=sys.stderr)
        print("  A row that will not parse is not a row naming no project. "
              "Removing the file while one of them is unreadable would take "
              "the conventions away from a project that never got them.",
              file=sys.stderr)
        return 1
    if not projects:
        print(f"\n  No projects registered in {project_catalog_path()}. "
              "Removing the file now would take the conventions off this "
              "machine entirely, so it stays.")
        return 1

    manifest = install_manifest.load(OPERATOR_HOME)
    try:
        source, origin = project_instructions.resolve_source(
            _repo_template_path(), global_path, manifest)
    except project_instructions.InstructionsError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    print(f"\n  Conventions taken from {origin}")
    print(f"  Each project gets an {project_instructions.AGENTS_NAME} with "
          "only the sections its own features turned on.\n")
    for project in projects:
        print(f"    {project['label']}  {project['path']}")
    if not assume_yes:
        if _prompt_line(f"\n  Write {project_instructions.AGENTS_NAME} into "
                        f"{len(projects)} repositories? [y/N]: "
                        ).lower() not in ("y", "yes"):
            print("  Nothing written.")
            return 0
    print()
    snapshot = _catalog_fingerprint()

    def recheck():
        if _catalog_fingerprint() == snapshot:
            return None
        return (f"{project_catalog_path()} changed while this was running, so "
                "the list of projects it was given may be out of date.")

    result = project_instructions.retire(
        projects,
        source=source,
        source_origin=origin,
        global_path=global_path,
        archive_dir=instructions_archive_dir(),
        projects_root=projects_root(),
        home=HOME,
        version=TOOLKIT_VERSION,
        decide=(lambda project, existing: True) if assume_yes else _combine_prompt,
        log=print,
        recheck=recheck,
    )
    tracking = stage_agents_files(result.placed)
    staged = [entry for entry in tracking if entry.state == AGENTS_STAGED]
    # `--yes` answers "write into my repositories", which is not the same
    # consent as "make commits in them". Unattended, the conservative half is
    # taken: everything is staged and nothing is committed.
    if staged and not assume_yes:
        suffix = "y" if len(staged) == 1 else "ies"
        if _prompt_line(
                f"\n  Commit {project_instructions.AGENTS_NAME} in "
                f"{len(staged)} repositor{suffix}? [y/N]: "
                ).lower() in ("y", "yes"):
            tracking = commit_agents_files(tracking, result.source_origin)
    return _report_retirement(result, global_path, tracking)


def _report_retirement(result, global_path: Path, tracking=()) -> int:
    print()
    if tracking:
        print(f"  {project_instructions.AGENTS_NAME} in each repository:")
        for entry in tracking:
            print(f"    {entry.label}: {entry.state}"
                  + (f" — {entry.detail}" if entry.detail else ""))
        if any(entry.state == AGENTS_STAGED for entry in tracking):
            print("    Staged, not committed — commit them or they are one "
                  "`git clean -fd` from gone.")
        print()
    if result.user_agents:
        print("  ! A user-scope AGENTS.md is also loaded into every session:")
        for path in result.user_agents:
            print(f"      {path}")
        print("    It is not this toolkit's file and was not touched.")
    if result.removed:
        if result.archived:
            print(f"  Retired {global_path}")
            print(f"  A copy is kept at {result.archived} — nothing prunes "
                  "that directory.")
        else:
            print(f"  {global_path} was already gone.")
        return 0
    for problem in result.problems:
        print(f"  ! {problem}", file=sys.stderr)
    blockers = result.blockers
    if blockers:
        print("\n  Blocked by:", file=sys.stderr)
        for outcome in blockers:
            print(f"    {outcome.label}: {outcome.state}"
                  + (f" — {outcome.detail}" if outcome.detail else ""),
                  file=sys.stderr)
        print("\n  The conventions are now in two places rather than none. "
              "Resolve the above and run this again.", file=sys.stderr)
    return 1


def show_menu() -> int:
    """Bare ``operator`` (no arguments): an interactive action picker.

    Every session action funnels through the browser rather than asking the
    user to retype a name the operator already knows. The menu loops so a
    single action is not the end of the program.
    """
    while True:
        running = len(active_instances())
        print("\n═══ Copilot Operator ═══\n")
        if user_instructions_present():
            print(f"  ! {global_instructions_path()} is read by every Copilot")
            print("    session on this machine, including directories that "
                  "are not projects.")
            print("    Retire it from the projects screen below to give each "
                  f"repository its own {project_instructions.AGENTS_NAME}.\n")
        options: list[tuple[str, Callable[[], int] | None]] = [
            (f"Sessions — inspect, join or stop  ({running} running)",
             browse_instances),
            ("Project configurations — features per project",
             browse_project_configurations),
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


def read_exit_code(instance) -> int | None:
    """The exit code the runner recorded for a session, or ``None``.

    ``None`` covers three things that are worth keeping apart in principle
    and are the same answer here: no file, an empty file, and a file that
    could not be read. What every one of them means to a caller is that
    *nobody observed copilot terminate*. That is the signature of a session
    killed wholesale — the runner dies with it and never gets to write a code
    — as opposed to one whose process ended under a runner that survived to
    write it down.

    Only call this once the session is gone. `start_session` clears the file
    at launch, but a clearing that failed would let a previous session's code
    be read against a live one, so anything deciding whether *this* session's
    ending was observed must go through :func:`ending_was_observed`.
    """
    try:
        raw = instance.exit_file.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def ending_was_observed(instance) -> bool:
    """Whether anything outlived this session far enough to record its end.

    An exit code is only evidence about *this* session if the launch managed
    to clear whatever the last one left behind. `start_session` clears the
    file, but the clear is best-effort — a file held open or on a path that
    has gone read-only survives it — and a stale code read against the next
    session would say "somebody watched this end" about the exact case where
    nobody did. Deciding it in the other direction costs a killed session
    being counted against the wrong allowance; deciding it this way costs an
    orderly exit being counted against the unaccounted one, which is bounded
    too. Under a failure that already means the state directory is not
    writable, the second is the one to take.
    """
    if not instance.exit_file_cleared:
        return False
    return read_exit_code(instance) is not None


def _session_usage(stream) -> None:
    print("Usage:\n"
          "  operator session start --instance NAME [--session N] "
          "[--project SUB] [--json]\n"
          "  operator session end   --instance NAME [--session N] "
          "--status TEXT --next TEXT [--done] [--json]\n"
          "\n"
          "start  resolves this instance's assignment before its first token:\n"
          "       a claim it already holds (resume), else claims whose owners\n"
          "       are provably gone (offered, oldest first), else nothing.\n"
          "end    writes the handoff, closes the session log and disposes of\n"
          "       the claim in one call. The claim is kept unless --done.",
          file=stream)


def _session_db(cwd: Path):
    """The claim/session database for the project ``cwd`` belongs to.

    Resolved from the *primary* checkout, so a session started inside a
    worktree finds the project's real entry instead of minting a second one.
    """
    root = primary_repo_root(cwd)
    found = catalog_guid(root)
    if found.guid is None:
        print(f"No catalog entry for {root} — this project is not registered.",
              file=sys.stderr)
        return None
    return operator_session.db_path(project_dir(found.guid))


def _parse_flagged_args(args: list[str], *, takes_value: dict,
                        flags: dict) -> "dict | None":
    """``--key value`` / ``--key=value`` / bare flags, or ``None`` on refusal.

    Shared by ``session`` and ``work`` rather than written twice. The two
    behaviours worth keeping identical are both ones a second copy would drift
    on: an unrecognised option is *refused* rather than ignored -- a caller who
    typed ``--relase`` and saw a success message believes an effect happened
    that did not -- and a value that looks like an option is refused too.

    That second rule was bought with a real defect. ``session end --status ok
    --next --done`` bound ``next="--done"`` and left ``done`` false: the caller
    asked for the claim to be released, was told the session ended, and it was
    not. A value that genuinely starts with a dash is still expressible as
    ``--next=-x``, which is unambiguous by construction.

    ``flags`` is matched against the whole argument, not its ``=``-prefix, so
    ``--json=true`` is an unknown option rather than a flag with a silently
    discarded value.
    """
    opts: dict = {}

    def separate_value(key: str, i: int) -> "str | None":
        if i + 1 >= len(args):
            print(f"Missing value for {key}", file=sys.stderr)
            return None
        value = args[i + 1]
        if value.startswith("-") and value != "-":
            print(f"Missing value for {key}: {value!r} looks like an option. "
                  f"Write {key}={value} if that really is the value.",
                  file=sys.stderr)
            return None
        return value

    i = 0
    while i < len(args):
        arg = args[i]
        key, _, inline = arg.partition("=")
        if arg in flags:
            opts[flags[arg]] = True
        elif key in takes_value:
            if "=" in arg:
                opts[takes_value[key]] = inline
            else:
                value = separate_value(key, i)
                if value is None:
                    return None
                opts[takes_value[key]] = value
                i += 1
        else:
            print(f"Unknown option: {arg}", file=sys.stderr)
            return None
        i += 1
    return opts


def _parse_session_args(args: list[str]) -> "dict | None":
    """Options for ``session start``/``end``."""
    opts: dict = {"instance": "", "session": None, "project": None,
                  "json": False, "done": False, "status": "", "next": "",
                  "context": "", "prompt": "", "in_progress": ""}
    parsed = _parse_flagged_args(
        args,
        takes_value={"--instance": "instance", "--project": "project",
                     "--status": "status", "--next": "next",
                     "--context": "context", "--prompt": "prompt",
                     "--in-progress": "in_progress", "--session": "session"},
        flags={"--json": "json", "--done": "done", "--release": "done"})
    if parsed is None:
        return None
    raw = parsed.pop("session", None)
    opts.update(parsed)
    if raw is not None:
        try:
            opts["session"] = int(raw)
        except ValueError:
            print(f"--session takes a number, not {raw!r}", file=sys.stderr)
            return None
        if opts["session"] < 0:
            print(f"--session takes a session number, not {raw!r}",
                  file=sys.stderr)
            return None
    if not opts["instance"]:
        print("Missing required: --instance NAME", file=sys.stderr)
        return None
    return opts


def _session_start(opts: dict, db) -> int:
    instance = safe_instance_id(opts["instance"])
    session_num = opts["session"]
    if session_num is None:
        session_num = _read_session_number(instance)
    operator_session.init_db(db)
    assignment = operator_session.start_session(
        db, instance=instance, session=session_num,
        subproject=opts["project"])
    # Mail is delivered here rather than waited for. A sleeping instance
    # still needs a queue, but the agent should not have to remember a
    # command to drain it -- that is the polling model this replaces, and the
    # failure it had was silent: an agent that never ran `operator inbox`
    # was indistinguishable from one with no mail.
    #
    # Consuming (rather than peeking) is what makes this a delivery. The
    # messages are rendered below in the same breath, so the archive and the
    # agent's context agree; a peek here would show them again next session
    # and read as a second message rather than the same one.
    # `instance` is already a sanitized id, so it is passed straight through.
    # Wrapping it in `Instance(...)` again re-sanitizes an id that has been
    # sanitized once: `beta.test` becomes `beta-test-2e02bd` and then
    # `beta-test-2e02bd-1ac43e`, a mailbox nothing ever writes to -- so mail
    # for every name needing sanitization would be silently undeliverable.
    try:
        delivered = operator_mail.consume(OPERATOR_HOME, instance)
    except operator_mail.MailError as exc:
        # A jammed mailbox must not stop a session starting, but it must not
        # pass for an empty one either.
        print(f"Could not read queued mail: {exc}", file=sys.stderr)
        print("  This is not an empty mailbox: messages may be waiting and "
              "unreadable.", file=sys.stderr)
        delivered = exc.consumed
        if delivered:
            # `consume` archives one at a time, so a fault part way through
            # leaves the earlier ones already read. Saying "nothing was marked
            # read" here while printing them below is a sentence asserting an
            # outcome nobody checked -- the same defect `show_inbox` already
            # had, reached through a second door. Ask, then say.
            print(f"  {len(delivered)} message(s) HAD already been marked "
                  "read before the failure. They are printed below, because "
                  "this is the only time they will ever be offered.",
                  file=sys.stderr)
        else:
            print("  Nothing was marked read, so anything there survives for "
                  "the next attempt; try `operator inbox --peek`.",
                  file=sys.stderr)
    if opts["json"]:
        print(json.dumps({
            "kind": assignment.kind,
            "instance": assignment.instance,
            "session": session_num,
            **operator_session.assignment_values(assignment),
            "messages": delivered,
            "offers": [{"item": o.item, "instance": o.claim.instance,
                        "reason": o.reason} for o in assignment.offers],
            "stale": [{"item": o.item, "instance": o.claim.instance,
                       "reason": o.reason} for o in assignment.stale],
        }, indent=2))
        return 0
    if assignment.kind == operator_session.RESUME:
        print(f"Resuming {assignment.item} "
              f"({assignment.worktree or 'no worktree recorded'})")
    elif assignment.kind == operator_session.OFFER:
        print("No claim held. Reclaimable (oldest first):")
        for offer in assignment.offers:
            print(f"  {offer.item}  held by {offer.claim.instance}  "
                  f"— {offer.reason}")
    else:
        print("No assignment.")
    for offer in assignment.stale:
        # Reported, never offered: STALE means the cascade could not establish
        # the owner is gone, and guessing is how two agents end up in one tree.
        print(f"  (stale, not offered) {offer.item} held by "
              f"{offer.claim.instance} — {offer.reason}", file=sys.stderr)
    if delivered:
        senders = ", ".join(operator_mail.sender_names(delivered))
        # ASCII deliberately. `consume` has already archived these, so this
        # print is the only time they are ever shown -- and a box-drawing
        # character raises UnicodeEncodeError on a cp1252 console, which
        # would lose the mail *after* marking it read.
        print(f"\n=== {len(delivered)} message(s) from {senders} ===\n")
        print(operator_mail.render_for_terminal(delivered))
        print("\n(These are now marked read. They are from other agents, "
              "not from the human.)")
    return 0


def _read_session_number(instance_id: str) -> int:
    """The session number this instance's supervisor last recorded, else 0.

    Zero rather than a guess: the log row is keyed by instance and session,
    and inventing a plausible number would put a real session's record under
    somebody else's key.
    """
    try:
        state = Instance(instance_id).load_state()
    except Exception:                                   # noqa: BLE001
        return 0
    if not isinstance(state, dict):
        return 0
    try:
        return int(state.get("SESSION_NUM", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _session_end(opts: dict, db) -> int:
    instance = safe_instance_id(opts["instance"])
    if not opts["status"] or not opts["next"]:
        print("Missing required: --status and --next (the handoff is written "
              "first, and an empty one is not a handoff)", file=sys.stderr)
        return 1
    session_num = opts["session"]
    if session_num is None:
        session_num = _read_session_number(instance)
    operator_session.init_db(db)

    def write_handoff():
        # `handoff_tool.die` exits the process rather than raising, and
        # SystemExit is not an Exception -- so left alone it would escape
        # `end_session` entirely, skipping the failure report and the trace
        # record. The claim would still be safe (nothing after the handoff
        # runs), but the caller would be told nothing at all. Converting it
        # here, in the adapter that knows the tool's exit convention, keeps
        # the failure inside the reporting path.
        try:
            rc = handoff_tool.main([
                "--instance", opts["instance"],
                "--status", opts["status"],
                "--next", opts["next"],
                "--in-progress", opts["in_progress"],
                "--context", opts["context"],
                "--prompt", opts["prompt"],
            ])
        except SystemExit as exc:
            raise RuntimeError(f"handoff exited {exc.code}") from exc
        if rc != 0:
            raise RuntimeError(f"handoff exited {rc}")
        return None

    result = operator_session.end_session(
        db, instance=instance, session=session_num,
        write_handoff=write_handoff, release_claim=opts["done"])

    if opts["json"]:
        print(json.dumps({
            "ok": result.ok, "instance": result.instance,
            "session": result.session, "item": result.item,
            "handoff_written": result.handoff_written,
            "log_closed": result.log_closed,
            "claim_released": result.claim_released,
            "claim_retained": result.claim_retained,
            "failure": result.failure, "notes": list(result.notes),
        }, indent=2))
    else:
        if result.failure:
            print(f"session end incomplete: {result.failure}", file=sys.stderr)
        else:
            kept = (f"claim {result.item} kept" if result.claim_retained
                    else (f"claim {result.item} released" if result.claim_released
                          else "no claim held"))
            print(f"Handoff written, session log closed, {kept}.")
        for note in result.notes:
            print(f"  note: {note}", file=sys.stderr)
    return 0 if result.ok else 1


#: The verbs ``operator session`` answers to.
#:
#: A named tuple rather than a literal in the dispatch, so the template
#: conformance test can measure the document against the code instead of
#: against a second copy of the vocabulary.
SESSION_VERBS = ("start", "end")


def manage_session(args: list[str]) -> int:
    """``operator session start|end`` — the two ends of one session (FR-2, FR-5)."""
    if not args or args[0] in HELP_FLAGS:
        _session_usage(sys.stdout if args else sys.stderr)
        return 0 if args else 1
    verb = args[0]
    if verb not in SESSION_VERBS:
        print(f"Unknown subcommand: operator session {verb}", file=sys.stderr)
        print("Did you mean `operator session start` or "
              "`operator session end`?", file=sys.stderr)
        return 1
    opts = _parse_session_args(args[1:])
    if opts is None:
        return 1
    db = _session_db(Path.cwd())
    if db is None:
        return 1
    return _session_start(opts, db) if verb == "start" else _session_end(opts, db)


WORK_VERBS = ("request", "release", "list", "heartbeat", "reclaim")


def _work_usage(stream) -> None:
    print("Usage:\n"
          "  operator work request   --instance NAME --item REF "
          "[--project SUB] [--worktree PATH] [--branch NAME] [--json]\n"
          "  operator work release   --instance NAME [--item REF] [--json]\n"
          "  operator work heartbeat --instance NAME [--item REF] [--json]\n"
          "  operator work list      [--project SUB] [--json]\n"
          "  operator work reclaim   --instance NAME --item REF [--json]\n"
          "\n"
          "One work item per instance, one owner per item.\n"
          "reclaim refuses an owner that is live, and one the liveness\n"
          "cascade could not decide about. It commits a dead owner's\n"
          "uncommitted changes to wip/ITEM-INSTANCE before the item moves,\n"
          "and never runs git stash, reset, clean, checkout or restore.",
          file=stream)


def _parse_work_args(args: list[str]) -> "dict | None":
    """Options for the ``operator work`` verbs.

    ``--instance`` is not required here as it is for ``session``: ``work
    list`` is a question about the project, not about one agent, and demanding
    a name to ask it would make the natural reading -- what is everybody
    working on -- unavailable.
    """
    opts: dict = {"instance": "", "item": "", "project": None,
                  "worktree": "", "branch": "", "json": False}
    parsed = _parse_flagged_args(
        args,
        takes_value={"--instance": "instance", "--item": "item",
                     "--project": "project", "--worktree": "worktree",
                     "--branch": "branch"},
        flags={"--json": "json"})
    if parsed is None:
        return None
    opts.update(parsed)
    return opts


def _agent_pid(instance) -> "int | None":
    """The pid that stands for this agent, or ``None`` when none is running.

    Never this process. ``operator work request`` exits within a second of
    writing the claim, so recording its own pid would leave a claim whose
    second liveness signal says the owner is gone -- reclaimable by anyone,
    immediately, and by the cascade's own evidence.

    The copilot session is preferred over the supervisor because it is the
    process actually doing the work; the supervisor is the fallback for a
    session that has not started or has just ended between two of its own
    restarts.
    """
    pid = instance.copilot_pid()
    if pid and _pid_alive(pid):
        return pid
    return _running_loop_pid(instance)


def _claimed_item(db, opts: dict, instance: str) -> "str | None":
    """``--item`` if given, else whatever this instance already holds.

    Defaulting is what makes ``release`` and ``heartbeat`` usable from an
    agent that was handed its assignment rather than choosing it: FR-2's whole
    point is that the agent does not have to know the item's name to work it.
    """
    if opts["item"]:
        return opts["item"]
    held = work_claims.claim_for_instance(db, instance)
    return None if held is None else held.item


def _work_request(opts: dict, db) -> int:
    instance = safe_instance_id(opts["instance"])
    if not opts["item"]:
        print("Missing required: --item REF", file=sys.stderr)
        return 1
    worktree = opts["worktree"] or str(Path.cwd())
    branch = opts["branch"] or operator_work.current_branch(worktree) or None
    operator_session.init_db(db)
    try:
        held = operator_work.request(
            db, item=opts["item"], instance=instance,
            subproject=opts["project"] or "", worktree=worktree, branch=branch,
            mux_session=Instance(opts["instance"]).session,
            pid=_agent_pid(Instance(opts["instance"])))
    except work_claims.ClaimRefused as exc:
        if opts["json"]:
            print(json.dumps({"ok": False, "reason": exc.reason,
                              "item": exc.item, "instance": exc.instance,
                              "held_by": (None if exc.holder is None
                                          else exc.holder.instance),
                              "detail": str(exc)}, indent=2))
        else:
            print(f"Refused: {exc}", file=sys.stderr)
            if exc.reason == work_claims.ITEM_HELD:
                print("Use `operator work list` to see whether its owner is "
                      "still running, and `operator work reclaim` if it is "
                      "provably gone.", file=sys.stderr)
        return 1
    if opts["json"]:
        print(json.dumps(_claim_json(held), indent=2))
    else:
        print(f"{held.item} claimed by {held.instance} "
              f"({held.worktree or 'no worktree'}"
              f"{', ' + held.branch if held.branch else ''})")
    return 0


def _claim_json(held) -> dict:
    return {"ok": True, "item": held.item, "instance": held.instance,
            "subproject": held.subproject, "worktree": held.worktree,
            "branch": held.branch, "claimed_at": held.claimed_at,
            "heartbeat_at": held.heartbeat_at, "pid": held.pid,
            "mux_session": held.mux_session}


def _work_release(opts: dict, db) -> int:
    instance = safe_instance_id(opts["instance"])
    operator_session.init_db(db)
    item = _claimed_item(db, opts, instance)
    if item is None:
        print(f"{instance} holds no work item.", file=sys.stderr)
        return 1
    ok = operator_work.release(db, item=item, instance=instance)
    if opts["json"]:
        print(json.dumps({"ok": ok, "item": item, "instance": instance},
                         indent=2))
    elif ok:
        print(f"{item} released by {instance}.")
    else:
        print(f"{instance} does not hold {item}; nothing released.",
              file=sys.stderr)
    return 0 if ok else 1


def _work_heartbeat(opts: dict, db) -> int:
    instance = safe_instance_id(opts["instance"])
    operator_session.init_db(db)
    item = _claimed_item(db, opts, instance)
    if item is None:
        print(f"{instance} holds no work item.", file=sys.stderr)
        return 1
    ok = operator_work.heartbeat(db, item=item, instance=instance)
    if opts["json"]:
        print(json.dumps({"ok": ok, "item": item, "instance": instance},
                         indent=2))
    elif ok:
        print(f"{item}: heartbeat refreshed.")
    else:
        print(f"{instance} does not hold {item}; heartbeat not refreshed.",
              file=sys.stderr)
    return 0 if ok else 1


def _work_list(opts: dict, db) -> int:
    operator_session.init_db(db)
    rows = operator_work.listing(db, subproject=opts["project"])
    if opts["json"]:
        print(json.dumps([{**_claim_json(held), "verdict": verdict.verdict,
                           "reason": verdict.reason,
                           "reclaimable": verdict.reclaimable}
                          for held, verdict in rows], indent=2))
        return 0
    if not rows:
        print("No work items are claimed.")
        return 0
    for held, verdict in rows:
        print(f"{held.item}  {held.instance}  {verdict.verdict}")
        print(f"{'':4}{verdict.reason}")
        if held.worktree:
            print(f"{'':4}{held.worktree}"
                  f"{' (' + held.branch + ')' if held.branch else ''}")
    return 0


def _work_reclaim(opts: dict, db) -> int:
    instance = safe_instance_id(opts["instance"])
    if not opts["item"]:
        print("Missing required: --item REF", file=sys.stderr)
        return 1
    operator_session.init_db(db)
    result = operator_work.reclaim(
        db, item=opts["item"], to_instance=instance,
        mux_session=Instance(opts["instance"]).session,
        pid=_agent_pid(Instance(opts["instance"])))
    preserved = result.preservation
    if opts["json"]:
        print(json.dumps({
            "ok": result.ok, "item": result.item,
            "instance": result.to_instance,
            "refused": result.refused, "detail": result.detail,
            "previous_owner": (None if result.previous is None
                               else result.previous.instance),
            "verdict": (None if result.liveness is None
                        else result.liveness.verdict),
            "preserved_branch": (None if preserved is None
                                 else preserved.branch),
            "preserved_commit": (None if preserved is None
                                 else preserved.commit),
            "notes": [] if preserved is None else list(preserved.notes),
        }, indent=2))
        return 0 if result.ok else 1
    if not result.ok:
        print(f"Refused: {result.detail}", file=sys.stderr)
        if result.refused == operator_work.OWNER_STALE:
            print("STALE is not a verdict this tool acts on. Confirm the "
                  "agent has stopped, then release the claim from that "
                  "instance.", file=sys.stderr)
        return 1
    previous = result.previous.instance if result.previous else "?"
    print(f"{result.item}: {previous} -> {result.to_instance}")
    if preserved is not None and preserved.branch:
        print(f"  {previous}'s uncommitted work is on {preserved.branch} "
              f"({(preserved.commit or '')[:12]}). The working tree was not "
              f"touched.")
    for note in (preserved.notes if preserved else ()):
        print(f"  note: {note}")
    return 0


def manage_work(args: list[str]) -> int:
    """``operator work request|release|list|heartbeat|reclaim`` (FR-3, FR-4)."""
    if not args or args[0] in HELP_FLAGS:
        _work_usage(sys.stdout if args else sys.stderr)
        return 0 if args else 1
    verb = args[0]
    if verb not in WORK_VERBS:
        print(f"Unknown subcommand: operator work {verb}", file=sys.stderr)
        print(f"Expected one of: {', '.join(WORK_VERBS)}", file=sys.stderr)
        return 1
    opts = _parse_work_args(args[1:])
    if opts is None:
        return 1
    if verb != "list" and not opts["instance"]:
        print("Missing required: --instance NAME", file=sys.stderr)
        return 1
    db = _session_db(Path.cwd())
    if db is None:
        return 1
    return {"request": _work_request, "release": _work_release,
            "heartbeat": _work_heartbeat, "list": _work_list,
            "reclaim": _work_reclaim}[verb](opts, db)


WORKTREE_VERBS = ("new", "finish", "recover")


def _worktree_usage(stream) -> None:
    print("Usage:\n"
          "  operator worktree new     --instance NAME --item REF "
          "[--project SUB] [--branch NAME] [--path PATH] [--json]\n"
          "  operator worktree finish  --instance NAME [--item REF] "
          "[--into REF] [--json]\n"
          "  operator worktree recover [--preserve] [--json]\n"
          "\n"
          "A checkout is 1:1 with a work item: `new` takes the claim and\n"
          "creates the tree together, and releases the claim again if the\n"
          "tree cannot be made.\n"
          "`finish` refuses a tree with uncommitted changes rather than\n"
          "tidying it, and deletes the branch only when --into already\n"
          "contains it.\n"
          "`recover` reports; it removes nothing. --preserve commits the\n"
          "uncommitted work of an unclaimed tree, or one whose owner is\n"
          "provably gone, to a wip/ branch.",
          file=stream)


def _parse_worktree_args(args: list[str]) -> "dict | None":
    opts: dict = {"instance": "", "item": "", "project": None, "branch": "",
                  "path": "", "into": operator_worktree.DEFAULT_INTEGRATION,
                  "preserve": False, "json": False}
    parsed = _parse_flagged_args(
        args,
        takes_value={"--instance": "instance", "--item": "item",
                     "--project": "project", "--branch": "branch",
                     "--path": "path", "--into": "into"},
        flags={"--preserve": "preserve", "--json": "json"})
    if parsed is None:
        return None
    opts.update(parsed)
    return opts


def _worktree_new(opts: dict, db, root) -> int:
    instance = safe_instance_id(opts["instance"])
    if not opts["item"]:
        print("Missing required: --item REF", file=sys.stderr)
        return 1
    operator_session.init_db(db)
    result = operator_worktree.new(
        db, root, item=opts["item"], instance=instance,
        subproject=opts["project"] or "", branch=opts["branch"] or None,
        path=opts["path"] or None,
        mux_session=Instance(opts["instance"]).session,
        pid=_agent_pid(Instance(opts["instance"])))
    if opts["json"]:
        print(json.dumps(_worktree_json(result), indent=2))
        return 0 if result.ok else 1
    if not result.ok:
        print(f"Refused: {result.detail}", file=sys.stderr)
        for note in result.notes:
            print(f"  note: {note}", file=sys.stderr)
        return 1
    print(f"{result.item}: {result.path} ({result.branch})")
    for note in result.notes:
        print(f"  note: {note}")
    return 0


def _worktree_json(result) -> dict:
    return {"ok": result.ok, "verb": result.verb, "item": result.item,
            "instance": result.instance, "path": result.path,
            "branch": result.branch, "branch_deleted": result.branch_deleted,
            "refused": result.refused, "detail": result.detail,
            "notes": list(result.notes)}


def _worktree_finish(opts: dict, db, root) -> int:
    instance = safe_instance_id(opts["instance"])
    operator_session.init_db(db)
    item = _claimed_item(db, opts, instance)
    if item is None:
        print(f"{instance} holds no work item.", file=sys.stderr)
        return 1
    result = operator_worktree.finish(db, root, item=item, instance=instance,
                                      into=opts["into"])
    if opts["json"]:
        print(json.dumps(_worktree_json(result), indent=2))
        return 0 if result.ok else 1
    if not result.ok:
        print(f"Refused: {result.detail}", file=sys.stderr)
        if result.refused == operator_worktree.WORKTREE_DIRTY:
            print("Nothing here commits, stages or discards them for you: "
                  "that is the one thing this command must never be a faster "
                  "way to do.", file=sys.stderr)
        return 1
    print(f"{result.item}: {result.path} removed, claim released"
          f"{', branch ' + result.branch + ' deleted' if result.branch_deleted else ''}.")
    for note in result.notes:
        print(f"  note: {note}")
    return 0


def _worktree_recover(opts: dict, db, root) -> int:
    operator_session.init_db(db)
    try:
        rows = operator_worktree.survey(db, root, preserve=opts["preserve"])
    except operator_work.GitUnavailable as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    if opts["json"]:
        print(json.dumps([{
            "path": row.path, "branch": row.branch, "state": row.state,
            "item": None if row.claim is None else row.claim.item,
            "instance": None if row.claim is None else row.claim.instance,
            "verdict": None if row.liveness is None else row.liveness.verdict,
            "reason": None if row.liveness is None else row.liveness.reason,
            "preserved_branch": (None if row.preserved is None
                                 else row.preserved.branch),
            "preserved_commit": (None if row.preserved is None
                                 else row.preserved.commit),
            "note": row.note,
        } for row in rows], indent=2))
        return 0
    for row in rows:
        owner = f"  {row.claim.instance} ({row.claim.item})" if row.claim else ""
        print(f"{row.state:<12}{row.path}{owner}")
        if row.branch:
            print(f"{'':4}{row.branch}")
        if row.liveness is not None:
            print(f"{'':4}{row.liveness.reason}")
        if row.note:
            print(f"{'':4}{row.note}")
        if row.preserved is not None and row.preserved.branch:
            print(f"{'':4}uncommitted work preserved on "
                  f"{row.preserved.branch} "
                  f"({(row.preserved.commit or '')[:12]})")
    if not opts["preserve"] and any(
            row.state in (operator_worktree.UNCLAIMED, operator_worktree.DEAD)
            for row in rows):
        print("\nRe-run with --preserve to commit the uncommitted work in "
              "those trees to a wip/ branch. Nothing is removed either way.")
    return 0


def manage_worktree(args: list[str]) -> int:
    """``operator worktree new|finish|recover`` — the checkout of a work item.

    The verbs are asymmetric on purpose. ``new`` and ``finish`` both write on
    the assumption that the agent running them knows what is in the tree;
    ``recover`` is what runs when nobody does, so it reports and preserves and
    removes nothing at all.
    """
    if not args or args[0] in HELP_FLAGS:
        _worktree_usage(sys.stdout if args else sys.stderr)
        return 0 if args else 1
    verb = args[0]
    if verb not in WORKTREE_VERBS:
        print(f"Unknown subcommand: operator worktree {verb}", file=sys.stderr)
        print(f"Expected one of: {', '.join(WORKTREE_VERBS)}", file=sys.stderr)
        return 1
    opts = _parse_worktree_args(args[1:])
    if opts is None:
        return 1
    if verb != "recover" and not opts["instance"]:
        print("Missing required: --instance NAME", file=sys.stderr)
        return 1
    db = _session_db(Path.cwd())
    if db is None:
        return 1
    # The *primary* checkout, never `Path.cwd()`: every one of these verbs
    # runs from inside a worktree at least half the time, and git's worktree
    # commands addressed at a linked worktree operate on the same repository
    # but the layout is anchored on the primary checkout's `.worktrees/`.
    root = primary_repo_root(Path.cwd())
    return {"new": _worktree_new, "finish": _worktree_finish,
            "recover": _worktree_recover}[verb](opts, db, root)


#: The verbs ``operator ownership`` answers to. See :data:`SESSION_VERBS`.
OWNERSHIP_VERBS = ("check",)


def manage_ownership(args: list[str]) -> int:
    """``operator ownership check`` — may this branch be pushed?

    The gate the peer-agents skill calls the only isolation that can fail a
    build. The decision lives in :mod:`operator_ownership`, which touches
    neither git nor the filesystem beyond reading the declaration; this
    function is the part that has to talk to git, and it is deliberately the
    only part that does.

    Exit codes are the interface, because the caller is a pre-push hook or a
    CI step and neither reads prose. ``0`` allowed, ``1`` refused, ``2``
    could not tell. The third is not folded into the second: a hook that
    treats "the declaration would not parse" as "this branch is fine" is the
    failure this whole module is written against, and a hook author who
    wants that has to write ``|| true`` where somebody can see it.
    """
    if not args or args[0] in HELP_FLAGS:
        _ownership_usage(sys.stdout if args else sys.stderr)
        return 0 if args else 1
    if args[0] not in OWNERSHIP_VERBS:
        print(f"Unknown subcommand: operator ownership {args[0]}",
              file=sys.stderr)
        print(f"Expected: {', '.join(OWNERSHIP_VERBS)}", file=sys.stderr)
        return 1
    opts: dict = {"project": None, "against": operator_worktree.
                  DEFAULT_INTEGRATION, "contracts": False, "json": False}
    parsed = _parse_flagged_args(
        args[1:],
        takes_value={"--project": "project", "--against": "against"},
        flags={"--allow-contracts": "contracts", "--json": "json"})
    if parsed is None:
        return 1
    opts.update(parsed)
    root = primary_repo_root(Path.cwd())
    try:
        declaration = operator_ownership.read_declaration(root)
    except operator_ownership.OwnershipError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    changed = _changed_paths(opts["against"])
    if changed is None:
        print(f"Could not list the files this branch changed against "
              f"{opts['against']}. Refusing rather than reporting it clean.",
              file=sys.stderr)
        return 2
    verdict = operator_ownership.check(
        declaration, changed, subproject=opts["project"],
        allow_contracts=opts["contracts"])
    if opts["json"]:
        print(json.dumps({"ok": verdict.ok, "code": verdict.code,
                          "subproject": verdict.subproject,
                          "offending": list(verdict.offending),
                          "candidates": list(verdict.candidates),
                          "detail": verdict.detail}, indent=2))
    else:
        stream = sys.stdout if verdict.ok else sys.stderr
        print(f"{verdict.code}: {verdict.detail}"
              if verdict.detail else verdict.code, file=stream)
        for path in verdict.offending:
            print(f"  {path}", file=stream)
        if verdict.candidates and not verdict.ok:
            print(f"  declared subprojects: "
                  f"{', '.join(verdict.candidates)}", file=stream)
    return 0 if verdict.ok else 1


def _ownership_usage(stream) -> None:
    print("Usage:\n"
          "  operator ownership check [--project SUB] [--against REF] "
          "[--allow-contracts] [--json]\n"
          "\n"
          "Refuses a branch that changed files outside the subproject it is\n"
          "working. --project names it; left out, the branch must resolve to\n"
          "exactly one. Contract paths are refused even to a subproject that\n"
          "owns them, because they are the interface between subprojects --\n"
          "--allow-contracts waives that one rule and nothing else.\n"
          "\n"
          "Exit 0 allowed, 1 refused, 2 could not tell. The third is not the\n"
          "second: a hook treating 'the declaration would not parse' as\n"
          "'this branch is fine' is what this check exists to prevent.\n"
          "\n"
          "Declared in .operator/subprojects.json at the repository root:\n"
          '  {"subprojects": {"api": {"owns": ["services/api"]}},\n'
          '   "contracts": ["specs/contracts"]}',
          file=stream)


def _changed_paths(against: str) -> "list | None":
    """Repository-relative paths this branch changed, or None if git refused.

    ``main...HEAD`` -- three dots -- is the merge base, so a branch that has
    not been rebased is not blamed for what landed on ``main`` behind it.
    Two dots would report every file changed on the integration branch since
    the fork as though this branch had touched them, and the check would
    then refuse work nobody did.

    ``None`` on failure, never ``[]``. An empty list is a real answer -- a
    branch with no changes passes -- and returning it for "git would not
    run" is the collapse that turns this gate into a decoration.
    """
    proc = _run_git(["diff", "--name-only", f"{against}...HEAD"])
    if proc is None or proc.returncode != 0:
        return None
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _run_git(args: list[str]):
    try:
        return subprocess.run(["git", *args], capture_output=True,
                              encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None


def manage_backlog(args: list[str]) -> int:
    """``operator backlog …`` — the tracked backlog, from the operator CLI.

    A delegation and not a reimplementation. ``backlog_tool`` already owns the
    vocabulary, the approval gate and every rule ``check`` enforces; a second
    argument parser here would be a second copy of all three, and the copy is
    the thing that drifts. What this adds is discoverability: an agent reads
    ``operator …`` in its instructions, and a verb reachable only through a
    separate console script is a verb it will not find -- and an agent that
    cannot find `close` edits the status field by hand.

    ``argparse`` exits rather than returning, so ``--help`` and a malformed
    argument arrive here as :class:`SystemExit`. Letting one escape would take
    the operator down through a path that has nothing to do with operator
    state; its code is the exit code, which is what this call would have
    returned anyway.
    """
    try:
        return backlog_tool.main(args, prog="operator backlog")
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if not exc.code
                                                           else 1)


def _record_session_exit(instance, session_num: int,
                         stop_state, detach_state, restart_state,
                         consecutive: int, uptime: float | None = None,
                         session_gone: bool = True) -> None:
    """Trace a session ending, with the evidence the decision was made on.

    The supervisor polls liveness rather than waiting on the child, so it has
    never had an exit *code* to log -- but the runner writes one to the exit
    file, and that is the difference between "copilot crashed" and "copilot
    shut down cleanly and nobody asked us to expect it". Reading it here costs
    one file read on a path that only runs when a session has already ended.

    ``restart_state`` is passed in rather than probed here. It used to be
    re-read off disk, and the only call site was the branch that had *already*
    established the restart marker was absent -- so the field could not carry
    ``True`` in any record, over 979 recorded exits. A field that cannot vary
    records nothing, and this one was read as proof that no session had ever
    ended by handoff when all it showed was where the call sat.

    It takes the caller's tri-state probe, not a ``bool``. ``marker_set``
    collapses "not there" and "could not look" into one answer, which is the
    right trade for deciding a branch and the wrong one for a record somebody
    will later read as an observation.

    ``session_gone`` is False on the one path that fires while copilot is still
    up (a restart requested mid-session, which is what `handoff` does). No exit
    code can belong to a live process, so none is read: `start_session` clears
    the exit file, but a clearing that failed would otherwise let a previous
    session's code be recorded against this one.
    """
    try:
        code: "int | None" = read_exit_code(instance) if session_gone else None
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
                     "restart": restart_state,
                     "exit_code": code,
                     "uptime_s": None if uptime is None else int(uptime)},
            consecutive=consecutive,
            limit=MAX_LAUNCH_FAILURES,
            code=running_code_fingerprint().get("digest"),
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

    supervisor_starts = [r for r in records
                         if r.get("event") == "supervisor_start"]
    if limit:
        supervisor_starts = supervisor_starts[-limit:]

    if as_json:
        print(json.dumps(
            {"invocations": invocations, "session_exits": session_exits,
             "supervisor_starts": supervisor_starts},
            indent=2))
        return 0

    if not invocations and not session_exits and not supervisor_starts:
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
        # command someone ran, but a supervised session ending. These are the
        # events a mass die-off consists of, and no operator command is
        # invoked during one -- which is why an invocation log alone could not
        # explain the seven simultaneous deaths this trace was written after.
        print(f"\n═══ Supervised session endings "
              f"({len(session_exits)} shown) ═══\n")
        for rec in session_exits:
            markers = rec.get("markers") or {}
            code = markers.get("exit_code")
            # "Exited unexpectedly" only ever meant unexplained. A recorded
            # exit code of 0 is a clean shutdown nobody asked us to expect,
            # and reading it as a crash is how five of them end a loop.
            #
            # A set restart marker is the one explanation available here, so
            # it is reported ahead of the code: `handoff` ends a session that
            # way, and such a session has no exit code *by construction* --
            # rendering it as "no exit code recorded" would give it the exact
            # signature of the externally killed sessions it must be told
            # apart from.
            if markers.get("restart") is True:
                verdict = "ended by restart request (handoff or operator restart)"
                if code is not None:
                    verdict += f", rc={code}"
            elif code is None:
                verdict = "no exit code recorded"
            elif code == 0:
                verdict = "clean exit (rc=0), unexplained by any marker"
            else:
                verdict = f"rc={code}"
            gave_up = " GIVING UP" if rec.get("giving_up") else ""
            # Which supervisor code wrote this record. Printed on every line
            # because the alternative a reader falls back on is the date, and
            # a date cannot see a supervisor that started before a fix and
            # kept running -- which is how 979 records written by an
            # instrument that could not vary were read as a finding about the
            # world. "unrecorded" is its own answer: those are the records
            # from before this was stamped, and they are exactly the ones no
            # conclusion should rest on.
            digest = rec.get("code") or "unrecorded"
            print(f"{rec.get('ts', '?'):20} {str(rec.get('instance', '?')):18} "
                  f"#{rec.get('session', '?'):<5} "
                  f"{rec.get('consecutive', '?')}/{rec.get('limit', '?')}"
                  f"{gave_up}")
            print(f"{'':20} └─ {verdict}; "
                  f"copilot pid={rec.get('session_pid')}, markers "
                  f"stop={markers.get('stop')} detach={markers.get('detach')} "
                  f"restart={markers.get('restart')}; code={digest}")

    if supervisor_starts:
        # The boundary every other record is read relative to. A supervisor
        # keeps the code it imported for its whole run, so this line is where
        # one instrument stops and the next begins -- without it the only
        # available boundary is the clock, which does not know that a
        # supervisor started before a fix is still running afterwards.
        print(f"\n═══ Supervisor starts ({len(supervisor_starts)} shown) ═══\n")
        for rec in supervisor_starts:
            print(f"{rec.get('ts', '?'):20} {str(rec.get('instance', '?')):18} "
                  f"from session #{rec.get('session', '?'):<5} "
                  f"code={rec.get('code') or 'unrecorded'} "
                  f"v{rec.get('toolkit_version') or '?'}")

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


#: The shortest word the prefix rule will act on.
#:
#: Two characters is not evidence of intent. ``in``, ``re``, ``he`` and ``me``
#: are all prefixes of a subcommand and all ordinary first words of a sentence,
#: so a prefix rule without a floor refuses ``operator in the parser, rename
#: x`` -- a working invocation, taken away to guess at a typo nobody made.
#: Three costs nothing here: the two-letter mistake this guard exists for,
#: ``ls``, is not a prefix of ``list`` at all and is caught by the alias table.
MIN_PREFIX_LENGTH = 3


def _one_edit_apart(word: str, candidate: str) -> bool:
    """True when at most one insertion, deletion, substitution or transposition
    of adjacent characters turns ``word`` into ``candidate``.

    At *most* one, so an identical pair is also true. Nothing calls it that
    way -- the dispatcher answers an exact subcommand long before this -- but
    the name says "apart" and a future caller would be entitled to read that
    as "and not equal", so it is written down rather than left to be found.

    Damerau-Levenshtein rather than plain Levenshtein because a transposition
    is one slip of the fingers and two ordinary edits, and transposition is
    the single most common typing mistake there is: ``jion``, ``sedn`` and
    ``verison`` are all one flipped pair from a real subcommand and would
    otherwise need a threshold of two, which is wide enough to swallow real
    words (``test`` is two edits from ``list``).
    """
    if abs(len(word) - len(candidate)) > 1:
        return False
    previous: dict[tuple[int, int], int] = {}
    for i in range(-1, len(word)):
        previous[(i, -1)] = i + 1
    for j in range(-1, len(candidate)):
        previous[(-1, j)] = j + 1
    for i, a in enumerate(word):
        for j, b in enumerate(candidate):
            cost = 0 if a == b else 1
            best = min(previous[(i - 1, j)] + 1,
                       previous[(i, j - 1)] + 1,
                       previous[(i - 1, j - 1)] + cost)
            if i and j and a == candidate[j - 1] and word[i - 1] == b:
                best = min(best, previous[(i - 2, j - 2)] + cost)
            previous[(i, j)] = best
    return previous[(len(word) - 1, len(candidate) - 1)] <= 1


def _subcommand_suggestions(word: str) -> list[str]:
    """The subcommands ``word`` is plausibly a mistyping of, in declared order.

    Three rules, and the narrowness of all three is the point. This predicate
    decides whether a word is refused instead of being handed to copilot as a
    prompt, and ``operator [copilot-args...]`` is documented -- so every word
    it claims wrongly is a working invocation taken away.

    - **A prefix, of at least MIN_PREFIX_LENGTH characters.** ``sto`` and
      ``log`` are truncations of something real. A prefix is necessarily no
      longer than what it prefixes, which is what keeps instance names safe:
      ``list-view`` and ``report-gen`` are longer than every subcommand they
      resemble, so no name longer than the thing it looks like can be refused
      by this rule at all.
    - **One edit away**, the classic single-slip model, and the reason a count
      is right here where a similarity ratio was not. It cannot span a length
      gap of two, so it protects longer names for the same reason.
    - **A word from another tool.** ``ls`` is not a typo of ``list``; it is
      correct spelling from a different program, and no distance measure will
      ever connect them. Those are enumerated in SUBCOMMAND_ALIASES rather
      than guessed at, and consulted only when the other two rules found
      nothing.

    Together those two length properties are the whole safety argument for
    project names: **a word two or more characters longer than a subcommand
    can never be refused because of it.** Almost every real instance name has
    that shape.

    An earlier version scored ``difflib.SequenceMatcher`` ratios with a 0.6
    cutoff, and three reviewers independently rejected it: a ratio measures
    shared characters in any order, so it refused ``refactor`` (``restore``),
    ``read`` (``reload``), ``hello`` (``help``), ``test`` (``ingest``) and --
    worst -- ``myproject``, the documented quick-join, against ``projects``.
    Ten of thirty ordinary one-word prompts were refused. These rules refuse
    two, ``lint`` and ``end``, each genuinely one keystroke from a subcommand.

    All matches are returned rather than one winner. ``sto`` truncates three
    different subcommands and ``difflib``'s own tie-break is alphabetical,
    which would have answered ``logs`` to ``ls`` -- the exact mistake this was
    written for. There is no limit because there is no honest way to choose
    which of a set of equally good answers to hide.
    """
    word = word.lower()
    if not word:
        return []
    matches = [candidate for candidate in SUBCOMMANDS
               if (len(word) >= MIN_PREFIX_LENGTH and candidate.startswith(word))
               or _one_edit_apart(word, candidate)]
    if not matches and word in SUBCOMMAND_ALIASES:
        matches = [SUBCOMMAND_ALIASES[word]]
    return matches


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
    if head == "projects":
        if len(args) > 1:
            if args[1] != "retire":
                print(f"Unknown subcommand: operator projects {args[1]}",
                      file=sys.stderr)
                print("Did you mean `operator projects retire`?",
                      file=sys.stderr)
                return 1
            return retire_user_instructions(assume_yes="--yes" in args[2:])
        return browse_project_configurations()
    if head == "report":
        return report_metrics(args[1] if len(args) > 1 else "summary")
    if head == "conversations":
        return conversations_command(args[1:])
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
    if head == "reply":
        return reply_message(args[1:])
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
    if head == "session":
        return manage_session(args[1:])
    if head == "work":
        return manage_work(args[1:])
    if head == "backlog":
        return manage_backlog(args[1:])
    if head == "worktree":
        return manage_worktree(args[1:])
    if head == "ownership":
        return manage_ownership(args[1:])

    # Positional shortcut: `operator foo` joins a running instance named foo.
    if len(args) == 1 and not head.startswith("-") and head not in RESERVED_WORDS:
        candidate = Instance(head)
        if MUX.available() and MUX.has_session(candidate.session):
            set_tab_title(f"terminal - {candidate.display_name}")
            set_tab_progress(TAB_LOOPING if _running_loop_pid(candidate) else TAB_STEADY)
            MUX.attach(candidate.session)
            return 0

    # Everything above has returned, so `head` is not a subcommand and does
    # not name a running instance. `run_dispatch` would read it as a copilot
    # argument and start a session named after the current directory -- so
    # `operator ls` does not report that `ls` is spelled `list`, it offers to
    # restart whatever is running here, and against a name that is *not*
    # running it starts a real session with no prompt at all.
    #
    # An unknown subcommand is a typing mistake, and the answer to a typing
    # mistake is a message rather than a state change. Only a head that is
    # close to a real subcommand is refused: `operator [copilot-args...]` is
    # documented, so an unrecognisable word is still passed through as a
    # prompt. This mirrors the refusal `operator projects <typo>` already
    # gives one level down.
    if not head.startswith("-"):
        suggestions = _subcommand_suggestions(head)
        if suggestions:
            names = " or ".join(f"`operator {name}`" for name in suggestions)
            print(f"Unknown subcommand: operator {head}", file=sys.stderr)
            print(f"Did you mean {names}?", file=sys.stderr)
            print(f"(To pass it to copilot instead, name the instance: "
                  f"`operator --name NAME {head}`.)", file=sys.stderr)
            return 1

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




#: The verbs ``operator conversations`` answers to. See :data:`SESSION_VERBS`.
CONVERSATIONS_VERBS = ("seed", "serve", "stats")


def _flag_value(args: list[str], flag: str) -> str:
    """The value following ``flag``, or ``""``.

    ``--port`` with nothing after it returns ``""`` rather than consuming the
    next flag: ``operator conversations serve --port --no-browser`` is a typo,
    and reading ``--no-browser`` as a port number turns it into a confusing
    ``ValueError`` about something the user never typed.
    """
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            candidate = args[i + 1]
            return "" if candidate.startswith("--") else candidate
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return ""


def _flag_values(args: list[str], flag: str) -> list[str]:
    """Every value given for a repeatable flag.

    ``--allow-host`` is repeatable because a machine reached from a LAN has
    more than one name browsers may use, and collapsing them to the last one
    given would refuse the others with the same 403 as an attack.
    """
    found: list[str] = []
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            candidate = args[i + 1]
            if not candidate.startswith("--"):
                found.append(candidate)
        elif arg.startswith(flag + "="):
            found.append(arg.split("=", 1)[1])
    return found


def _conversations_usage(stream) -> None:
    print("Usage: operator conversations <seed|serve|stats>", file=stream)
    print(file=stream)
    print("  seed [--source S]   Copy existing messages into the store",
          file=stream)
    print("  serve [--port N] [--host H] [--allow-host H] [--no-browser]",
          file=stream)
    print("                      Browse them at http://127.0.0.1:8765/",
          file=stream)
    print("  stats               What is stored, and where it came from",
          file=stream)
    print(file=stream)
    print("The store is per machine, at ~/.operator/conversations.db.",
          file=stream)
    print("Seeding is idempotent — run it as often as you like.",
          file=stream)
    print(file=stream)
    print("Future messages are captured by the conversation-capture "
          "extension,", file=stream)
    print("which setup installs along with every other one in extensions/.",
          file=stream)


def _seed_sources(conn, wanted: str) -> list:
    """Run the seeders the caller asked for.

    Every root is passed explicitly rather than left to each seeder's own
    default. They resolve identically today, but the first draft did leave
    them independent, and a test home wrote its rows into the *real*
    ``~/.operator/conversations.db`` while reading mail from the temporary one
    -- a store half from one machine's state and half from another's, which no
    error could report because each half was individually correct.

    ``--source`` exists because the three read very different things -- one
    opens somebody else's live database, one walks a mail directory, one folds
    in whatever the capture hook has spooled -- and when one of them is what is
    being debugged, running the other two is noise.
    """
    available = {
        conversation_log.SOURCE_SESSION_STORE:
            lambda c: conversation_log.seed_session_store(c),
        conversation_log.SOURCE_OPERATOR_MAIL:
            lambda c: conversation_log.seed_operator_mail(
                c, OPERATOR_HOME / "messages"),
        conversation_log.SOURCE_HOOK:
            lambda c: conversation_log.ingest_spool(
                c, conversation_log.spool_dir(OPERATOR_HOME)),
        conversation_log.SOURCE_HANDOFF:
            lambda c: conversation_log.seed_handoffs(
                c, OPERATOR_HOME / "projects"),
    }
    if not wanted:
        return [fn(conn) for fn in available.values()]
    if wanted not in available:
        die(f"Unknown source: {wanted}\n"
            f"Expected one of: {', '.join(available)}")
    return [available[wanted](conn)]


def conversations_command(args: list[str]) -> int:
    """``operator conversations`` — the record of what was said, and to whom.

    Sessions are ephemeral by design and ``git log`` answers only for work that
    landed. What a human asked an agent, and what came back, has until now been
    held in two places that are not queryable as a conversation and in one --
    the session database -- that does not outlive the session.
    """
    if not args or args[0] in HELP_FLAGS:
        _conversations_usage(sys.stdout if args else sys.stderr)
        return 0 if args else 1
    verb, rest = args[0], args[1:]
    if verb not in CONVERSATIONS_VERBS:
        print(f"Unknown subcommand: operator conversations {verb}",
              file=sys.stderr)
        print(f"Expected one of: {', '.join(CONVERSATIONS_VERBS)}",
              file=sys.stderr)
        return 1

    path = conversation_log.db_path(OPERATOR_HOME)
    try:
        if verb == "serve":
            raw = _flag_value(rest, "--port")
            if raw and not (raw.isascii() and raw.isdigit()):
                # Checked before int(), which accepts `80_80`, ` 8765 `,
                # `+8765` and unicode digits like `８７６５` -- none of them a
                # port anyone typed on purpose, and every one of them passes
                # the range check below.
                die(f"--port wants a number, not {raw!r}")
            port = int(raw) if raw else 8765
            if not 1 <= port <= 65535:
                die(f"--port must be between 1 and 65535, not {port}")
            return conversation_viewer.serve(
                path,
                host=_flag_value(rest, "--host") or "127.0.0.1",
                port=port,
                allow_hosts=_flag_values(rest, "--allow-host"),
                open_browser="--no-browser" not in rest)

        conn = conversation_log.connect(path)
    except conversation_log.ConversationError as exc:
        die(str(exc))
    except OSError as exc:
        die(f"Could not serve the conversation store: {exc}")

    try:
        if verb == "seed":
            reports = _seed_sources(conn, _flag_value(rest, "--source") or "")
            for report in reports:
                print("  " + report.describe())
                for problem in report.errors[:5]:
                    print(f"    ! {problem}", file=sys.stderr)
            total = conversation_log.summary(conn)["messages"]
            print(f"{total} message(s) in {path}")
            print("Run `operator conversations serve` to read them.")
            # A source that was *present and unreadable* fails the run even
            # though the others may have succeeded: a seeder that exits 0
            # having silently dropped a whole source is one nobody re-runs.
            # A source that is simply not on this machine is not a failure --
            # see SeedReport.absent.
            return 1 if any(r.failed for r in reports) else 0

        stats = conversation_log.summary(conn)
        print(f"{path}")
        print(f"  {stats['messages'] or 0} message(s), "
              f"{stats['projects'] or 0} project(s), "
              f"{stats['sessions'] or 0} session(s)")
        if stats["first_day"]:
            print(f"  {stats['first_day']} .. {stats['last_day']}")
        print(f"  search: {stats['search_mode']}")
        for row in sorted(stats["breakdown"], key=lambda r: -r["n"]):
            print(f"  {row['n']:>6}  {row['channel']:<12} {row['actor']}")
        for project in conversation_log.projects(conn)[:12]:
            print(f"  {project['messages']:>6}  {project['project']} "
                  f"({project['first_day']} .. {project['last_day']})")
        return 0
    finally:
        conn.close()

if __name__ == "__main__":
    sys.exit(main())
