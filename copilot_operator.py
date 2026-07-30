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

import csv
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import operator_ingest
from operator_console import enable_utf8_output
from operator_mux import Mux, MuxError, MuxNotFoundError, safe_instance_id

__version__ = "1.0.0"

POLL_INTERVAL = 10
MAX_SESSIONS = 1000
MAX_LAUNCH_FAILURES = 5
LAUNCH_BACKOFF_BASE = 5
RESTART_PAUSE_SECONDS = 3
SESSION_ID_WAIT = 20
EXIT_GRACE_SECONDS = 20
RESERVED_WORDS = {"stop", "list", "report", "ingest", "help", "join", "reload",
                  "version", "forget", "logs", "tabs", "restore",
                  "stop-loop", "stop-session", "menu"}
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


def set_tab_title(title: str) -> None:
    if sys.stdout.isatty():
        sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()


def migrate_legacy_state() -> None:
    """One-time move of state out of ~/.copilot, which the CLI deletes."""
    RESTART_DIR.mkdir(parents=True, exist_ok=True)
    moved = 0
    if LEGACY_RESTART_DIR.is_dir() and LEGACY_RESTART_DIR != RESTART_DIR:
        for src in list(LEGACY_RESTART_DIR.iterdir()):
            dest = RESTART_DIR / src.name
            if not dest.exists():
                try:
                    shutil.move(str(src), str(dest))
                    moved += 1
                except (OSError, shutil.Error):
                    pass
    for src, dest in ((LEGACY_LOG_FILE, LOG_FILE), (LEGACY_METRICS_DB, METRICS_DB)):
        if src.is_file() and not dest.exists():
            try:
                shutil.move(str(src), str(dest))
                moved += 1
            except (OSError, shutil.Error):
                pass
    if LEGACY_BACKUPS_DIR.is_dir() and not BACKUPS_DIR.exists():
        try:
            shutil.move(str(LEGACY_BACKUPS_DIR), str(BACKUPS_DIR))
            moved += 1
        except (OSError, shutil.Error):
            pass
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
        if not self.managed_file.exists():
            return None
        try:
            return json.loads(self.managed_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A legacy or truncated marker: present but tokenless.
            return {"token": None, "display_name": self.display_name}

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

        Used for listing and continuity only — never to authorize a kill.
        """
        return self.managed_file.exists() or self.state_file.exists()

    # -- persisted state
    def save_state(self, session_num: int, run_started: str, session_id: str = "") -> None:
        lines = [f"SESSION_NUM={session_num}", f"RUN_STARTED={run_started}"]
        if session_id:
            lines.append(f"COPILOT_SESSION_ID={session_id}")
        tmp = self.state_file.with_suffix(".state.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_file)

    def load_state(self) -> dict | None:
        if not self.state_file.exists():
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
                     self.loop_pid_file, self.detach_marker, self.stop_marker):
            path.unlink(missing_ok=True)


def managed_instances() -> dict[str, dict]:
    """Map instance id -> ownership metadata for every managed instance."""
    found: dict[str, dict] = {}
    if not RESTART_DIR.is_dir():
        return found
    for path in RESTART_DIR.iterdir():
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


# ── tab registry ────────────────────────────────────────────────
# Windows Terminal (and most terminal emulators) expose no API to list their
# own tabs, so the operator keeps its own record of which named instances were
# started from a terminal tab, in which directory, and with which arguments.
# After a reboot or crash every process is gone, but this file survives, and
# `operator restore` replays each entry in a fresh tab — the existing
# auto-continue/--resume logic then picks the Copilot session back up.
def load_tabs() -> dict[str, dict]:
    if not TABS_FILE.exists():
        return {}
    try:
        data = json.loads(TABS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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
    entries = load_tabs()
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
        title = meta.get("display_name", "operator")
        cmd += ["--title", title]
        argv = meta.get("argv", [])
        distro = meta.get("wsl_distro", "")
        cwd = meta.get("cwd", "")
        if distro:
            inner = "operator " + " ".join(argv) if argv else "operator"
            cmd += ["-d", cwd or "~", "wsl.exe", "-d", distro]
            if cwd:
                cmd += ["--cd", cwd]
            cmd += ["--", "bash", "-lic", inner]
        else:
            if cwd:
                cmd += ["-d", cwd]
            cmd += ["powershell", "-NoExit", "-Command", "operator " + " ".join(argv)]
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
    except OSError as exc:
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
    if not METRICS_DB.exists():
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
                   '+' || COALESCE(lines_added,0) || ' -' || COALESCE(lines_removed,0) AS changes,
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
                   COALESCE(SUM(api_time_seconds),0) || 's' AS total_api_time
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
    if not run_started or not METRICS_DB.exists():
        return
    print("\n═══ Operator Run Summary ═══\n")
    rows, headers = _query(f"""
        SELECT COUNT(*) AS sessions,
               printf('%.1f', {_credits()}) AS credits,
               printf('$%.2f', {_usd()}) AS est_cost,
               COALESCE(SUM(api_time_seconds),0) || 's' AS total_api_time,
               COALESCE({_fmt_duration_sql('SUM(session_time_seconds)')},'0s') AS total_sess_time,
               '+' || COALESCE(SUM(lines_added),0) || ' -' || COALESCE(SUM(lines_removed),0) AS total_changes
        FROM sessions WHERE no_op = 0 AND ended_at >= ?
    """, (run_started,))
    print(_table(rows, headers))
    rows, headers = _query(f"""
        SELECT m.model_name AS model,
               printf('%.1f', COALESCE(SUM(m.nano_aiu),0) / {_NANO}.0) AS credits,
               COUNT(*) AS uses
        FROM model_usage m JOIN sessions s ON m.session_id = s.id
        WHERE s.no_op = 0 AND s.ended_at >= ?
        GROUP BY m.model_name ORDER BY SUM(m.nano_aiu) DESC
    """, (run_started,))
    if rows:
        print()
        print(_table(rows, headers))


# ── launching ───────────────────────────────────────────────────
def project_catalog_path() -> Path:
    return Path.home() / ".copilot" / "projects" / "catalog.csv"


def project_handoff_file(cwd: Path) -> Path | None:
    """Resolve the handoff (``next-session.md``) path for a project directory.

    Looks the directory up in ``~/.copilot/projects/catalog.csv`` (the same
    catalog ``handoff``/``handoff_tool.py`` use) and returns the path the
    handoff file *would* live at, regardless of whether it currently exists.
    Returns None if the directory has no catalog entry at all.
    """
    catalog = project_catalog_path()
    if not catalog.is_file():
        return None
    target = str(cwd.resolve())
    if IS_WINDOWS:
        target = target.lower()
    try:
        with open(catalog, "r", encoding="utf-8", errors="replace", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 2:
                    continue
                path, guid = row[0].strip().strip('"'), row[1].strip().strip('"')
                if not path or not guid:
                    continue
                try:
                    resolved = str(Path(path).resolve())
                except OSError:
                    continue
                if IS_WINDOWS:
                    resolved = resolved.lower()
                if resolved == target:
                    return Path.home() / ".copilot" / "projects" / guid / "next-session.md"
    except OSError:
        return None
    return None


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

    instance.restart_marker.unlink(missing_ok=True)
    instance.exit_file.unlink(missing_ok=True)
    instance.session_file.unlink(missing_ok=True)

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
        if instance.exit_file.exists():
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
    instance.loop_pid_file.unlink(missing_ok=True)
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
    """
    if instance.exit_file.exists():
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
    MUX.send_keys(instance.session, "/exit")
    if wait_for_exit(instance, EXIT_GRACE_SECONDS):
        return
    log("  Copilot did not exit within the grace period — terminating session")
    MUX.kill_session(instance.session)
    # Give the runner a moment to finish writing metrics.
    for _ in range(10):
        if instance.exit_file.exists():
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
    print("═══ Running Operator Instances ═══\n")
    managed = managed_instances()
    live = set(MUX.list_sessions()) if MUX.available() else set()
    found = False
    for ident in sorted(managed):
        if ident not in live:
            continue
        meta = managed[ident]
        display = meta.get("display_name", ident)
        inst = Instance(display)
        # A live session with only continuity state behind it is not ours.
        owned = inst.owns_live_session()
        label = display if display == ident else f"{display} (session: {ident})"
        if not owned:
            label += "  [name in use by an unowned session]"
        print(f"  {label}")
        found = True
    if not found:
        print("  (none)")
    print("\nAttach: operator join <name>")
    print("Stop:   operator stop <name>")
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
        instance.state_file.unlink(missing_ok=True)
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
        inst.state_file.unlink(missing_ok=True)
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
    instance.state_file.unlink(missing_ok=True)
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
    if not instance.spec_file.exists():
        die(f"No launch spec found for '{target}' at {instance.spec_file}")
    spec = json.loads(instance.spec_file.read_text(encoding="utf-8"))
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
    if not results:
        print(f"No Copilot logs found in {COPILOT_LOG_DIR}")
        return 0
    for line in results:
        print(line)
    return 0


def _log_files() -> list[Path]:
    if not COPILOT_LOG_DIR.is_dir():
        return []
    return sorted(COPILOT_LOG_DIR.glob("process-*.log"))


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
        if f.name not in known:
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


def run_single_session(instance: Instance, copilot_args: list[str]) -> int:
    args = ["--yolo", "--autopilot", "--effort", "high", *copilot_args]
    handle_existing_session(instance)
    operator_ingest.init_db(METRICS_DB)
    run_started = utcnow()
    log(f"Starting single session: {instance.display_name}")

    start_session(instance, args, 1, remain_on_exit=False)
    set_tab_title(f"operator - {instance.display_name}")
    MUX.attach(instance.session)

    if MUX.has_session(instance.session) and not instance.exit_file.exists():
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


def run_loop_mode(instance: Instance, user_args: list[str], is_fresh: bool) -> int:
    copilot_args = ["--yolo", "--autopilot", "--no-ask-user", "--effort", "high"]
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
            start_session_num = int(state.get("SESSION_NUM", 0) or 0) + 1
            run_started = state.get("RUN_STARTED", run_started)
            candidate = state.get("COPILOT_SESSION_ID", "")
            if UUID_RE.match(candidate or ""):
                resume_id = candidate
                log(f"  Will resume Copilot CLI session: {resume_id}")
            log(f"Continuing from session #{start_session_num} (run started {run_started})")

    # A resume id with no handoff file for this project means the previous
    # session ended without ever calling `handoff` — most likely a crash
    # (operator itself dying, Windows rebooting, etc.) rather than a clean
    # stop. Tell the agent so it can act accordingly.
    crash_recovery = False
    if resume_id:
        handoff_file = project_handoff_file(Path.cwd())
        if handoff_file is None or not handoff_file.exists():
            crash_recovery = True
            log("  No handoff file found for this project — treating this as "
                "crash recovery")

    preamble = build_preamble(agent, instance, crash_recovery=crash_recovery)

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

    session_num = start_session_num
    last_launched = 0
    launch_failures = 0
    crash_failures = 0
    resume_id_used = ""
    instance.loop_pid_file.write_text(str(os.getpid()), encoding="utf-8")
    try:
        try:
            while session_num <= MAX_SESSIONS:
                launch_args = list(copilot_args)
                if resume_id:
                    if args_have_explicit_session(launch_args):
                        log("  Skipping automatic --resume; user args already choose a session")
                    else:
                        launch_args.append(f"--resume={resume_id}")
                        resume_id_used = resume_id
                    resume_id = ""

                # Persist the pending resume id too: if the launch fails or the
                # process dies here, the id must survive on disk rather than being
                # cleared by a pre-launch write.
                instance.save_state(session_num, run_started, resume_id_used)
                try:
                    start_session(instance, launch_args, session_num,
                                  remain_on_exit=True, preamble=preamble)
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
                resume_id_used = ""
                last_launched = session_num

                # Record the CLI session id once the runner discovers it.
                for _ in range(SESSION_ID_WAIT):
                    sid = instance.read_session_id()
                    if sid:
                        instance.save_state(session_num, run_started, sid)
                        break
                    if not is_copilot_running(instance):
                        break
                    _sleep(1)
                    if shutdown["requested"]:
                        raise KeyboardInterrupt

                restart_requested = False
                while True:
                    if shutdown["requested"]:
                        raise KeyboardInterrupt
                    _sleep(POLL_INTERVAL)
                    if shutdown["requested"]:
                        raise KeyboardInterrupt
                    if instance.stop_marker.exists():
                        # `operator stop NAME` asked us to shut down and take the
                        # session with us — same as Ctrl+C, just triggered
                        # remotely since this loop now runs in the background.
                        instance.stop_marker.unlink(missing_ok=True)
                        log(f"Session #{session_num}: stop requested — shutting down")
                        stop_session_gracefully(instance)
                        show_run_summary(run_started)
                        if MUX.has_session(instance.session):
                            MUX.kill_session(instance.session)
                        instance.cleanup_files()
                        return 0
                    if instance.detach_marker.exists():
                        # `operator stop-loop NAME` asked us to stop supervising
                        # but leave the session running untouched.
                        instance.detach_marker.unlink(missing_ok=True)
                        sid = instance.read_session_id()
                        instance.save_state(session_num, run_started, sid)
                        log(f"Session #{session_num}: detach requested — leaving "
                            f"session running, supervisor exiting")
                        return 0
                    if not is_copilot_running(instance):
                        if instance.restart_marker.exists():
                            log(f"Session #{session_num}: restart signal detected!")
                            crash_failures = 0
                        else:
                            crash_failures += 1
                            log(f"Session #{session_num}: copilot exited unexpectedly "
                                f"({crash_failures}/{MAX_LAUNCH_FAILURES}) — relaunching")
                            if crash_failures >= MAX_LAUNCH_FAILURES:
                                log(f"  Giving up after {crash_failures} consecutive "
                                    f"unexpected exits")
                                show_run_summary(run_started)
                                instance.cleanup_files()
                                return 1
                        restart_requested = True
                        break
                    if instance.restart_marker.exists():
                        log(f"Session #{session_num}: restart signal detected!")
                        crash_failures = 0
                        restart_requested = True
                        break

                if restart_requested:
                    log("Restarting copilot...")
                    instance.restart_marker.unlink(missing_ok=True)
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
        instance.loop_pid_file.unlink(missing_ok=True)


# ── help ────────────────────────────────────────────────────────
HELP = """operator — Metrics-capturing wrapper for GitHub Copilot CLI

USAGE
    operator                                                    Interactive menu
    operator [--name NAME] [copilot-args...]                   Single session
    operator --loop [--name NAME] [--fresh] [copilot-args...]  Loop mode (backgrounded, auto-attaches)
    operator NAME                                              Join a running instance
    operator join [NAME]                                       Join (explicit form)
    operator reload NAME                                       Hot-reload launch spec
    operator list                                              Show running instances
    operator stop [NAME]                                       Stop instance(s) — loop + session
    operator stop-loop NAME                                    Stop only the background loop
    operator stop-session NAME                                 Stop only the Copilot session
    operator forget NAME                                       Drop operator state only
    operator report [type]                                     View usage reports
    operator ingest [--force]                                  Process copilot logs
    operator logs [--prune] [--days N]                         Inspect/prune copilot logs
    operator tabs [list|remove NAME|clear]                     Manage tracked terminal tabs
    operator restore [NAME...|--all] [--dry-run]               Reopen tracked tabs after a crash
    operator help                                              Show this help

OPTIONS
    --name NAME     Set instance name (default: current directory name)
    --loop          Enable autonomous loop mode
    --fresh         Reset session numbering (ignore prior state)

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
        exits — including when you have detached. Always runs with --yolo.

    Loop mode (--loop)
        Adds --yolo --autopilot --no-ask-user --effort high automatically.
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
        operator stop-session NAME    Stop the session; if a supervisor is still
                                      running it relaunches a fresh one shortly.
        operator stop NAME            Stop both, cleanly, with no relaunch.

MENU
    Running `operator` with no arguments at all shows an interactive menu:
    list instances, join a session, restore tabs (all or picked), stop a
    loop only, stop a session only, or stop an instance completely.

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
def _tracked_cwd_for(name: str) -> str | None:
    """Best-effort lookup of the directory a tracked/managed instance name is
    already bound to, so a same-named directory elsewhere doesn't collide."""
    inst = Instance(name)
    spec = inst.spec_file
    if spec.exists():
        try:
            return json.loads(spec.read_text(encoding="utf-8")).get("cwd")
        except (OSError, ValueError):
            pass
    entry = load_tabs().get(inst.id)
    if entry:
        return entry.get("cwd")
    return None


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
                           is_fresh: bool) -> int:
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
    cmd += copilot_args
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, close_fds=True, cwd=str(Path.cwd()))
    if IS_WINDOWS:
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
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
    MUX.attach(instance.session)
    return 0


def _prompt_line(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def show_menu() -> int:
    """Bare `operator` (no arguments): an interactive action picker.

    Exists so juggling loops and sessions never requires memorizing exact
    subcommand names — list what's running, then pick an action.
    """
    print("═══ Copilot Operator ═══\n")
    options: list[tuple[str, Callable[[], int]]] = [
        ("List running instances", list_instances),
        ("Join a session", lambda: join_instance(_prompt_line("Instance name: "))),
        ("Restore tabs (pick which)", lambda: restore_tabs([])),
        ("Restore all tracked tabs", lambda: restore_tabs(["--all"])),
        ("Stop a loop only (leave its session running)",
         lambda: stop_loop_only(_prompt_line("Instance name: "))),
        ("Stop a session only (leave its loop running)",
         lambda: stop_session_only(_prompt_line("Instance name: "))),
        ("Stop an instance completely (loop + session)",
         lambda: stop_operator(_prompt_line("Instance name: "))),
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
    return action()


def main(argv: list[str] | None = None) -> int:
    enable_utf8_output()
    args = list(sys.argv[1:] if argv is None else argv)
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
    if head == "stop-session":
        return stop_session_only(args[1] if len(args) > 1 else None)
    if head == "join":
        return join_instance(args[1] if len(args) > 1 else None)
    if head == "reload":
        return reload_instance(args[1] if len(args) > 1 else None)
    if head == "forget":
        return forget_instance(args[1] if len(args) > 1 else None)
    if head == "logs":
        return manage_logs(args[1:])
    if head == "tabs":
        return manage_tabs(args[1:])
    if head == "restore":
        return restore_tabs(args[1:])

    # Positional shortcut: `operator foo` joins a running instance named foo.
    if len(args) == 1 and not head.startswith("-") and head not in RESERVED_WORDS:
        candidate = Instance(head)
        if MUX.available() and MUX.has_session(candidate.session):
            set_tab_title(f"terminal - {candidate.display_name}")
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
    name = ""
    copilot_args: list[str] = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--loop":
            loop_mode = True
        elif arg == "--fresh":
            is_fresh = True
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
                return run_loop_mode(instance, copilot_args, is_fresh)
            return start_and_attach_loop(instance, copilot_args, is_fresh)
        return run_single_session(instance, copilot_args)
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
