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

import operator_ingest
from operator_console import enable_utf8_output
from operator_mux import Mux, MuxError, MuxNotFoundError, safe_instance_id

__version__ = "1.0.0"

POLL_INTERVAL = 10
MAX_SESSIONS = 1000
EXIT_GRACE_SECONDS = 15
RESERVED_WORDS = {"stop", "list", "report", "ingest", "help", "join", "reload", "version"}
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SESSION_ARG_RE = re.compile(r"^--(continue|resume|connect)(=.*)?$")

IS_WINDOWS = platform.system() == "Windows"


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
COPILOT_LOG_DIR = Path(
    os.environ.get("COPILOT_LOG_DIR") or HOME / ".copilot" / "logs"
)

LEGACY_RESTART_DIR = HOME / ".copilot" / "restart"
LEGACY_LOG_FILE = HOME / ".copilot" / "operator.log"
LEGACY_METRICS_DB = HOME / ".copilot" / "operator-metrics.db"

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

    # -- ownership
    def claim(self, token: str) -> None:
        """Record ownership. An empty marker cannot prove which process owns a
        session, so the marker carries a token compared on stop/kill."""
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
            # A legacy empty marker: treat as owned but tokenless.
            return {"token": None, "display_name": self.display_name}

    def is_managed(self) -> bool:
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
                     self.pid_file, self.exit_file, self.session_file):
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


def report_metrics(subcmd: str = "summary") -> int:
    if not METRICS_DB.exists():
        print(f"No metrics database found at {METRICS_DB}")
        print("Run the operator first to start collecting metrics.")
        return 1

    home = str(HOME)
    if subcmd == "summary":
        print("═══ Usage Summary ═══\n")
        rows, headers = _query("""
            SELECT
              COALESCE(SUM(CASE WHEN date(ended_at,'localtime')=date('now','localtime')
                                THEN premium_requests ELSE 0 END),0) AS today,
              COALESCE(SUM(CASE WHEN ended_at >= datetime('now','-7 days')
                                THEN premium_requests ELSE 0 END),0) AS this_week,
              COALESCE(SUM(premium_requests),0) AS all_time,
              COUNT(*) AS sessions
            FROM sessions WHERE no_op = 0
        """)
    elif subcmd == "sessions":
        print("═══ Recent Sessions ═══\n")
        rows, headers = _query(f"""
            SELECT session_num AS '#', substr(started_at,1,16) AS started,
                   COALESCE(premium_requests,0) AS premium,
                   COALESCE(api_time_seconds || 's','—') AS api_time,
                   COALESCE({_fmt_duration_sql('session_time_seconds')},'—') AS sess_time,
                   '+' || COALESCE(lines_added,0) || ' -' || COALESCE(lines_removed,0) AS changes,
                   COALESCE(substr(git_branch,1,20),'—') AS branch,
                   COALESCE(replace(work_dir, ?, '~'),'—') AS project
            FROM sessions WHERE no_op = 0 ORDER BY id DESC LIMIT 20
        """, (home,))
    elif subcmd == "models":
        print("═══ Per-Model Usage ═══\n")
        rows, headers = _query("""
            SELECT model_name AS model,
                   COALESCE(SUM(premium_requests),0) AS total_premium,
                   COUNT(*) AS appearances
            FROM model_usage GROUP BY model_name ORDER BY total_premium DESC
        """)
    elif subcmd == "projects":
        print("═══ Per-Project Usage ═══\n")
        rows, headers = _query("""
            SELECT COALESCE(replace(work_dir, ?, '~'),'—') AS project,
                   COALESCE(SUM(premium_requests),0) AS total_premium,
                   COUNT(*) AS sessions,
                   COALESCE(SUM(api_time_seconds),0) || 's' AS total_api_time
            FROM sessions WHERE no_op = 0 GROUP BY work_dir ORDER BY total_premium DESC
        """, (home,))
    elif subcmd == "costs":
        print("═══ Cost Estimates (Enterprise @ $0.04/premium request) ═══\n")
        rows, headers = _query("""
            SELECT
              COALESCE(SUM(CASE WHEN date(ended_at,'localtime')=date('now','localtime')
                                THEN premium_requests ELSE 0 END),0) AS today_reqs,
              printf('$%.2f', COALESCE(SUM(CASE WHEN date(ended_at,'localtime')=date('now','localtime')
                                THEN premium_requests ELSE 0 END),0)*0.04) AS today_cost,
              COALESCE(SUM(CASE WHEN ended_at >= datetime('now','-7 days')
                                THEN premium_requests ELSE 0 END),0) AS week_reqs,
              printf('$%.2f', COALESCE(SUM(CASE WHEN ended_at >= datetime('now','-7 days')
                                THEN premium_requests ELSE 0 END),0)*0.04) AS week_cost,
              COALESCE(SUM(CASE WHEN strftime('%Y-%m', ended_at)=strftime('%Y-%m','now')
                                THEN premium_requests ELSE 0 END),0) AS month_reqs,
              printf('$%.2f', COALESCE(SUM(CASE WHEN strftime('%Y-%m', ended_at)=strftime('%Y-%m','now')
                                THEN premium_requests ELSE 0 END),0)*0.04) AS month_cost,
              COALESCE(SUM(premium_requests),0) AS all_time_reqs,
              printf('$%.2f', COALESCE(SUM(premium_requests),0)*0.04) AS all_time_cost
            FROM sessions WHERE no_op = 0
        """)
    else:
        print("Usage: operator report [summary|sessions|models|projects|costs]\n")
        print("  summary   — Premium request totals (today, week, all time)")
        print("  sessions  — Last 20 sessions with details")
        print("  models    — Usage breakdown by AI model")
        print("  projects  — Usage breakdown by project directory")
        print("  costs     — Cost estimates at enterprise overage rates")
        return 1

    print(_table(rows, headers))
    if subcmd == "costs":
        print("\nNote: Enterprise plan includes 1,000 premium requests/month.")
        print("      Costs above assume overage pricing ($0.04/request).")
        print("      Actual cost depends on your remaining monthly allowance.")
    return 0


def show_run_summary(run_started: str) -> None:
    if not run_started or not METRICS_DB.exists():
        return
    print("\n═══ Operator Run Summary ═══\n")
    rows, headers = _query(f"""
        SELECT COUNT(*) AS sessions,
               COALESCE(SUM(premium_requests),0) AS total_premium,
               COALESCE(SUM(api_time_seconds),0) || 's' AS total_api_time,
               COALESCE({_fmt_duration_sql('SUM(session_time_seconds)')},'0s') AS total_sess_time,
               '+' || COALESCE(SUM(lines_added),0) || ' -' || COALESCE(SUM(lines_removed),0) AS total_changes,
               printf('$%.2f', COALESCE(SUM(premium_requests),0)*0.04) AS est_cost
        FROM sessions WHERE no_op = 0 AND ended_at >= ?
    """, (run_started,))
    print(_table(rows, headers))
    rows, headers = _query("""
        SELECT m.model_name AS model,
               COALESCE(SUM(m.premium_requests),0) AS premium,
               COUNT(*) AS uses
        FROM model_usage m JOIN sessions s ON m.session_id = s.id
        WHERE s.no_op = 0 AND s.ended_at >= ?
        GROUP BY m.model_name ORDER BY premium DESC
    """, (run_started,))
    if rows:
        print()
        print(_table(rows, headers))


# ── launching ───────────────────────────────────────────────────
def build_preamble(agent_name: str, instance: Instance) -> str:
    return (
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


def is_copilot_running(instance: Instance) -> bool:
    if instance.exit_file.exists():
        return False
    return MUX.has_session(instance.session)


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
        label = display if display == ident else f"{display} (session: {ident})"
        print(f"  {label}")
        found = True
    if not found:
        print("  (none)")
    print("\nAttach: operator join <name>")
    print("Stop:   operator stop <name>")
    return 0


def stop_operator(target: str | None = None) -> int:
    log(f"Stop requested{f' for {target}' if target else ''}")
    if target:
        instance = Instance(target)
        if not instance.is_managed():
            print(f"No operator instance '{target}' found.", file=sys.stderr)
            print(file=sys.stderr)
            list_instances()
            return 1
        if MUX.has_session(instance.session):
            MUX.kill_session(instance.session)
        instance.cleanup_files()
        instance.state_file.unlink(missing_ok=True)
        log(f"Stopped: {target}")
        return 0

    managed = managed_instances()
    live = set(MUX.list_sessions()) if MUX.available() else set()
    count = 0
    for ident in sorted(managed):
        if ident not in live:
            continue
        MUX.kill_session(ident)
        inst = Instance(managed[ident].get("display_name", ident))
        inst.cleanup_files()
        inst.state_file.unlink(missing_ok=True)
        log(f"Stopped: {ident}")
        count += 1
    if count == 0:
        print("No running operator instances found.")
    else:
        log(f"Stopped {count} instance(s)")
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


def run_single_session(instance: Instance, copilot_args: list[str]) -> int:
    args = ["--autopilot", "--effort", "high", *copilot_args]
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

    preamble = build_preamble(agent, instance)
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
    try:
        while session_num <= MAX_SESSIONS:
            launch_args = list(copilot_args)
            if resume_id:
                if args_have_explicit_session(launch_args):
                    log("  Skipping automatic --resume; user args already choose a session")
                else:
                    launch_args.append(f"--resume={resume_id}")
                resume_id = ""

            instance.save_state(session_num, run_started)
            start_session(instance, launch_args, session_num,
                          remain_on_exit=True, preamble=preamble)

            # Record the CLI session id once the runner discovers it.
            for _ in range(20):
                sid = instance.read_session_id()
                if sid:
                    instance.save_state(session_num, run_started, sid)
                    break
                time.sleep(1)

            restart_requested = False
            while True:
                if shutdown["requested"]:
                    raise KeyboardInterrupt
                _sleep(POLL_INTERVAL)
                if shutdown["requested"]:
                    raise KeyboardInterrupt
                if not is_copilot_running(instance):
                    log(f"Session #{session_num}: copilot exited")
                    show_run_summary(run_started)
                    log("Operator shutting down")
                    instance.cleanup_files()
                    return 0
                if instance.restart_marker.exists():
                    log(f"Session #{session_num}: restart signal detected!")
                    restart_requested = True
                    break

            if restart_requested:
                log("Restarting copilot...")
                instance.restart_marker.unlink(missing_ok=True)
                stop_session_gracefully(instance)
                instance.save_state(session_num, run_started)
                session_num += 1
                log(f"Pausing before session #{session_num}...")
                time.sleep(3)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        log("Signal received — shutting down")
        stop_session_gracefully(instance)
        instance.save_state(session_num, run_started, instance.read_session_id())
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


# ── help ────────────────────────────────────────────────────────
HELP = """operator — Metrics-capturing wrapper for GitHub Copilot CLI

USAGE
    operator [--name NAME] [copilot-args...]                   Single session
    operator --loop [--name NAME] [--fresh] [copilot-args...]  Loop mode
    operator NAME                                              Join a running instance
    operator join [NAME]                                       Join (explicit form)
    operator reload NAME                                       Hot-reload launch spec
    operator list                                              Show running instances
    operator stop [NAME]                                       Stop instance(s)
    operator report [type]                                     View usage reports
    operator ingest [--force]                                  Process copilot logs
    operator help                                              Show this help

OPTIONS
    --name NAME     Set instance name (default: current directory name)
    --loop          Enable autonomous loop mode
    --fresh         Reset session numbering (ignore prior state)

MODES
    Single session (default)
        Launches copilot in a multiplexer session and auto-attaches. A
        supervisor inside the session captures usage metrics when copilot
        exits — including when you have detached.

    Loop mode (--loop)
        Adds --yolo --autopilot --no-ask-user --effort high automatically.
        Sends a preamble for autonomous operation and restarts copilot when
        the agent raises the instance restart marker. Named instances
        auto-continue when restarted: session numbering, run summary scope,
        and the last Copilot CLI session id carry over, and that session is
        resumed once with --resume. Use --fresh to reset.

REPORTS
    operator report summary       Premium request totals (today, week, all time)
    operator report sessions      Last 20 sessions with details
    operator report models        Usage breakdown by AI model
    operator report projects      Usage breakdown by project directory
    operator report costs         Cost estimates at $0.04/premium request

FILES
    ~/.operator/                        State directory (override with
                                        COPILOT_OPERATOR_HOME)
    ~/.operator/metrics.db              SQLite metrics database
    ~/.operator/operator.log            Operator log file
    ~/.operator/restart/                Per-instance markers and state
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
def default_instance_name() -> str:
    return Path.cwd().name or "operator"


def main(argv: list[str] | None = None) -> int:
    enable_utf8_output()
    args = list(sys.argv[1:] if argv is None else argv)
    migrate_legacy_state()

    if not args:
        return run_dispatch([])

    head = args[0]
    if head in ("help", "-h", "--help", "-?"):
        return show_help()
    if head in ("version", "--version", "-V"):
        print(f"operator {__version__}")
        return 0
    if head == "list":
        return list_instances()
    if head == "report":
        return report_metrics(args[1] if len(args) > 1 else "summary")
    if head == "ingest":
        return ingest_all_logs(force="--force" in args[1:])
    if head == "stop":
        return stop_operator(args[1] if len(args) > 1 else None)
    if head == "join":
        return join_instance(args[1] if len(args) > 1 else None)
    if head == "reload":
        return reload_instance(args[1] if len(args) > 1 else None)

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
    name = ""
    copilot_args: list[str] = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--loop":
            loop_mode = True
        elif arg == "--fresh":
            is_fresh = True
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
    try:
        if loop_mode:
            return run_loop_mode(instance, copilot_args, is_fresh)
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
