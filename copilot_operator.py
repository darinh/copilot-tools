#!/usr/bin/env python3
"""Copilot CLI Operator — Metrics-capturing wrapper for GitHub Copilot CLI.

Cross-platform replacement for operator.sh. Uses tmux (Linux/macOS) or
psmux (Windows — install via `winget install psmux`) for session management.

Usage:
    operator [copilot-args...]                    # single session
    operator --loop [copilot-args...]             # autonomous loop mode
    operator report [summary|sessions|models|projects|costs]
    operator ingest [--force]                     # process all copilot logs
    operator stop [NAME]                          # stop loop mode
    operator list                                 # show running instances
    operator help                                 # show this help
"""
import argparse
import os
import platform
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Constants ───────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
COPILOT_DIR = Path.home() / ".copilot"
RESTART_DIR = COPILOT_DIR / "restart"
LOG_FILE = COPILOT_DIR / "operator.log"
METRICS_DB = COPILOT_DIR / "operator-metrics.db"
COPILOT_LOG_DIR = COPILOT_DIR / "logs"
POLL_INTERVAL = 10
MAX_SESSIONS = 1000

# ── Tmux Detection ─────────────────────────────────────────────
TMUX_CMD = None


def find_tmux():
    """Find tmux or psmux binary."""
    global TMUX_CMD
    if TMUX_CMD is not None:
        return TMUX_CMD
    for cmd in ['tmux', 'psmux', 'pmux']:
        if shutil.which(cmd):
            TMUX_CMD = cmd
            return cmd
    return None


def tmux(*args, check=False, capture=True):
    """Run a tmux/psmux command."""
    cmd = find_tmux()
    if not cmd:
        print("Error: tmux (or psmux on Windows) is required but not found.", file=sys.stderr)
        if platform.system() == 'Windows':
            print("Install psmux: winget install psmux", file=sys.stderr)
        else:
            print("Install tmux via your package manager.", file=sys.stderr)
        sys.exit(1)
    full_cmd = [cmd] + list(args)
    if capture:
        r = subprocess.run(full_cmd, capture_output=True, text=True)
        return r.stdout.strip(), r.returncode
    else:
        return subprocess.run(full_cmd).returncode


# ── Logging ─────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[operator {ts}] {msg}"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')
    print(line, file=sys.stderr)


# ── Instance Management ────────────────────────────────────────
class Instance:
    def __init__(self, name):
        if not name.startswith('operator-copilot-'):
            name = f'operator-copilot-{name}'
        self.name = name
        self.tmux_session = name
        self.restart_marker = RESTART_DIR / name
        self.state_file = RESTART_DIR / f'{name}.state'
        self.run_script = COPILOT_DIR / f'operator-run-{name}.sh'
        RESTART_DIR.mkdir(parents=True, exist_ok=True)

    def save_state(self, session_num, run_started):
        self.state_file.write_text(
            f'SESSION_NUM={session_num}\nRUN_STARTED={run_started}\n')

    def load_state(self):
        if not self.state_file.exists():
            return None
        state = {}
        for line in self.state_file.read_text().splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                state[k] = v
        return state

    def cleanup(self):
        for f in [self.run_script, self.restart_marker]:
            f.unlink(missing_ok=True)


def next_instance_number():
    """Find the next available instance number from tmux sessions."""
    out, rc = tmux('list-sessions', '-F', '#{session_name}')
    if rc != 0:
        return 1
    max_n = 0
    import re
    for line in out.splitlines():
        m = re.match(r'^operator-copilot-(\d+)$', line)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return max_n + 1


def list_instances():
    """List running operator tmux sessions."""
    print("═══ Running Operator Instances ═══")
    print()
    out, rc = tmux('list-sessions')
    found = False
    if rc == 0:
        for line in out.splitlines():
            name = line.split(':')[0]
            if name.startswith('operator-copilot-') or name == 'copilot-operator':
                print(f"  {line}")
                found = True
    if not found:
        print("  (none)")
    mux = find_tmux() or 'tmux'
    print()
    print(f"Attach: {mux} attach -t <name>")
    print(f"Stop:   operator stop <name>")


# ── Metrics Database ────────────────────────────────────────────
def init_metrics_db():
    COPILOT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(METRICS_DB))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_num INTEGER NOT NULL,
            log_file TEXT UNIQUE,
            log_file_mtime TEXT,
            no_op INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            work_dir TEXT,
            git_branch TEXT,
            premium_requests INTEGER,
            api_time_seconds INTEGER,
            session_time_seconds INTEGER,
            lines_added INTEGER,
            lines_removed INTEGER,
            raw_metrics TEXT
        );
        CREATE TABLE IF NOT EXISTS model_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id),
            model_name TEXT NOT NULL,
            tokens_in TEXT,
            tokens_out TEXT,
            tokens_cached TEXT,
            premium_requests INTEGER
        );
    """)
    # Migration: add log_file_mtime for existing databases
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN log_file_mtime TEXT")
    except sqlite3.OperationalError:
        pass
    conn.close()


def sql_escape(s):
    return str(s).replace("'", "''")


# ── Metrics Helpers ─────────────────────────────────────────────
def find_copilot_log(pid=None):
    """Find the copilot process log file by PID."""
    log_dir = COPILOT_LOG_DIR
    if not log_dir.exists():
        return None
    if pid:
        matches = sorted(log_dir.glob(f'process-*-{pid}.log'),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if matches:
            return matches[0]
    # Fallback: most recently modified
    all_logs = sorted(log_dir.glob('process-*.log'),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    return all_logs[0] if all_logs else None


def capture_and_store_metrics(session_num, copilot_pid=None):
    """Parse metrics from copilot log and store in database."""
    try:
        logfile = find_copilot_log(copilot_pid)
        if not logfile:
            log(f"  Metrics: no copilot process log found (pid={copilot_pid or '?'})")
            return
        log(f"  Metrics: ingesting {logfile.name}...")
        ingest_py = SCRIPT_DIR / 'operator_ingest.py'
        r = subprocess.run(
            [sys.executable, str(ingest_py), str(logfile), str(METRICS_DB),
             '--session-num', str(session_num), '--work-dir', str(Path.cwd())],
            capture_output=True, text=True, timeout=60)
        output = (r.stdout + r.stderr).strip()
        log(f"  Metrics: {output}")
    except Exception as e:
        log(f"  Warning: metrics capture failed ({e})")


def ingest_all_logs(force=False):
    """Process all copilot process logs."""
    init_metrics_db()
    total = ingested = skipped = empty = 0
    print(f"Scanning {COPILOT_LOG_DIR} for unprocessed logs...")
    if not COPILOT_LOG_DIR.exists():
        print("No log directory found.")
        return
    ingest_py = SCRIPT_DIR / 'operator_ingest.py'
    force_args = ['--force'] if force else []
    for logfile in sorted(COPILOT_LOG_DIR.glob('process-*.log')):
        total += 1
        r = subprocess.run(
            [sys.executable, str(ingest_py), str(logfile), str(METRICS_DB)] + force_args,
            capture_output=True, text=True, timeout=120)
        output = (r.stdout + r.stderr).strip()
        if output.startswith('SKIP'):
            skipped += 1
        elif r.returncode == 2:
            empty += 1
        elif r.returncode == 0:
            ingested += 1
            print(f"  {output}")
        else:
            print(f"  ERROR: {output}", file=sys.stderr)
    print()
    print(f"Done: {total} logs scanned, {ingested} ingested, {empty} no usage, {skipped} already processed")
    if ingested > 0:
        print()
        report_metrics('summary')


# ── Reports ─────────────────────────────────────────────────────
def report_metrics(subcmd='summary'):
    if not METRICS_DB.exists():
        print(f"No metrics database found at {METRICS_DB}")
        print("Run the operator first to start collecting metrics.")
        sys.exit(1)

    conn = sqlite3.connect(str(METRICS_DB))
    home_esc = sql_escape(str(Path.home()))

    def query(sql):
        """Execute query and return formatted table."""
        cursor = conn.execute(sql)
        cols = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
        if not rows:
            print("  (no data)")
            return
        # Calculate column widths
        widths = [len(c) for c in cols]
        str_rows = []
        for row in rows:
            sr = [str(v) if v is not None else '—' for v in row]
            str_rows.append(sr)
            for i, v in enumerate(sr):
                widths[i] = max(widths[i], len(v))
        # Print header
        header = '  '.join(c.ljust(w) for c, w in zip(cols, widths))
        print(header)
        print('  '.join('-' * w for w in widths))
        for sr in str_rows:
            print('  '.join(v.ljust(w) for v, w in zip(sr, widths)))

    if subcmd == 'summary':
        print("═══ Usage Summary ═══")
        print()
        query("""
            SELECT
                COALESCE(SUM(CASE WHEN date(ended_at,'localtime') = date('now','localtime')
                    THEN premium_requests ELSE 0 END), 0) AS today,
                COALESCE(SUM(CASE WHEN ended_at >= datetime('now','-7 days')
                    THEN premium_requests ELSE 0 END), 0) AS this_week,
                COALESCE(SUM(premium_requests), 0) AS all_time,
                COUNT(*) AS sessions
            FROM sessions WHERE no_op = 0
        """)

    elif subcmd == 'sessions':
        print("═══ Recent Sessions ═══")
        print()
        query(f"""
            SELECT session_num AS '#',
                   substr(started_at, 1, 16) AS started,
                   COALESCE(premium_requests, 0) AS premium,
                   COALESCE(api_time_seconds || 's', '—') AS api_time,
                   COALESCE(
                       CASE WHEN session_time_seconds >= 3600 THEN
                           (session_time_seconds / 3600) || 'h ' ||
                           ((session_time_seconds % 3600) / 60) || 'm'
                       ELSE (session_time_seconds / 60) || 'm ' ||
                           (session_time_seconds % 60) || 's'
                       END, '—') AS sess_time,
                   '+' || COALESCE(lines_added,0) || ' -' || COALESCE(lines_removed,0) AS changes,
                   COALESCE(substr(git_branch,1,20),'—') AS branch,
                   COALESCE(replace(work_dir, '{home_esc}', '~'), '—') AS project
            FROM sessions WHERE no_op = 0 ORDER BY id DESC LIMIT 20
        """)

    elif subcmd == 'models':
        print("═══ Per-Model Usage ═══")
        print()
        query("""
            SELECT model_name AS model,
                   COALESCE(SUM(premium_requests), 0) AS total_premium,
                   COUNT(*) AS appearances
            FROM model_usage GROUP BY model_name ORDER BY total_premium DESC
        """)

    elif subcmd == 'projects':
        print("═══ Per-Project Usage ═══")
        print()
        query(f"""
            SELECT COALESCE(replace(work_dir, '{home_esc}', '~'), '—') AS project,
                   COALESCE(SUM(premium_requests), 0) AS total_premium,
                   COUNT(*) AS sessions,
                   COALESCE(SUM(api_time_seconds), 0) || 's' AS total_api_time
            FROM sessions WHERE no_op = 0
            GROUP BY work_dir ORDER BY total_premium DESC
        """)

    elif subcmd == 'costs':
        print("═══ Cost Estimates (Enterprise @ $0.04/premium request) ═══")
        print()
        query("""
            SELECT
                COALESCE(SUM(CASE WHEN date(ended_at,'localtime') = date('now','localtime')
                    THEN premium_requests ELSE 0 END), 0) AS today_reqs,
                printf('$%.2f', COALESCE(SUM(CASE WHEN date(ended_at,'localtime') = date('now','localtime')
                    THEN premium_requests ELSE 0 END), 0) * 0.04) AS today_cost,
                COALESCE(SUM(CASE WHEN ended_at >= datetime('now','-7 days')
                    THEN premium_requests ELSE 0 END), 0) AS week_reqs,
                printf('$%.2f', COALESCE(SUM(CASE WHEN ended_at >= datetime('now','-7 days')
                    THEN premium_requests ELSE 0 END), 0) * 0.04) AS week_cost,
                COALESCE(SUM(CASE WHEN strftime('%Y-%m', ended_at) = strftime('%Y-%m', 'now')
                    THEN premium_requests ELSE 0 END), 0) AS month_reqs,
                printf('$%.2f', COALESCE(SUM(CASE WHEN strftime('%Y-%m', ended_at) = strftime('%Y-%m', 'now')
                    THEN premium_requests ELSE 0 END), 0) * 0.04) AS month_cost,
                COALESCE(SUM(premium_requests), 0) AS all_time_reqs,
                printf('$%.2f', COALESCE(SUM(premium_requests), 0) * 0.04) AS all_time_cost
            FROM sessions WHERE no_op = 0
        """)
        print()
        print("Note: Enterprise plan includes 1,000 premium requests/month.")
        print("      Costs above assume overage pricing ($0.04/request).")
        print("      Actual cost depends on your remaining monthly allowance.")

    else:
        print("Usage: operator report [summary|sessions|models|projects|costs]")
        print()
        print("  summary   — Premium request totals (today, week, all time)")
        print("  sessions  — Last 20 sessions with details")
        print("  models    — Usage breakdown by AI model")
        print("  projects  — Usage breakdown by project directory")
        print("  costs     — Cost estimates at enterprise overage rates")
        sys.exit(1)

    conn.close()


def show_run_summary(run_started):
    if not run_started or not METRICS_DB.exists():
        return
    conn = sqlite3.connect(str(METRICS_DB))
    print()
    print("═══ Operator Run Summary ═══")
    print()
    row = conn.execute(f"""
        SELECT COUNT(*) AS sessions,
               COALESCE(SUM(premium_requests), 0) AS total_premium,
               COALESCE(SUM(api_time_seconds), 0) AS total_api_time,
               COALESCE(SUM(session_time_seconds), 0) AS total_sess_time,
               COALESCE(SUM(lines_added), 0) AS lines_added,
               COALESCE(SUM(lines_removed), 0) AS lines_removed
        FROM sessions WHERE no_op = 0 AND ended_at >= '{run_started}'
    """).fetchone()
    sessions, premium, api_s, sess_s, added, removed = row
    if sess_s >= 3600:
        h, rem = divmod(sess_s, 3600)
        m, s = divmod(rem, 60)
        sess_str = f"{h}h {m}m"
    else:
        m, s = divmod(sess_s, 60)
        sess_str = f"{m}m {s}s"
    cost = premium * 0.04
    print(f"  Sessions: {sessions}")
    print(f"  Premium requests: {premium}")
    print(f"  API time: {api_s}s")
    print(f"  Session time: {sess_str}")
    print(f"  Code changes: +{added} -{removed}")
    print(f"  Est. cost: ${cost:.2f}")

    # Per-model breakdown
    models = conn.execute(f"""
        SELECT m.model_name, COALESCE(SUM(m.premium_requests), 0) AS premium, COUNT(*) AS uses
        FROM model_usage m JOIN sessions s ON m.session_id = s.id
        WHERE s.no_op = 0 AND s.ended_at >= '{run_started}'
        GROUP BY m.model_name ORDER BY premium DESC
    """).fetchall()
    if models:
        print()
        for model, prem, uses in models:
            print(f"  {model}: {prem} premium ({uses} uses)")
    conn.close()


# ── Argument Helpers ────────────────────────────────────────────
def extract_agent_from_args(args):
    prev = None
    for arg in args:
        if arg.startswith('--agent='):
            return arg.split('=', 1)[1]
        if prev == '--agent':
            return arg
        prev = arg
    return 'anvil:anvil'


def build_preamble(agent_name, instance):
    return (
        f"You are running under an automated operator wrapper that a human set up. "
        f"Key facts: (1) You have blanket human approval for ALL decisions — tool calls, "
        f"file edits, git operations, architectural choices. Do not ask for direction or "
        f"confirmation. Make your best judgment call and proceed. If you are genuinely "
        f"uncertain between approaches that have very different consequences, state your "
        f"reasoning and pick one. (2) Session restart: when context gets heavy or a task "
        f"is complete with next steps, first write your session handoff file, then run this "
        f"bash command: touch {instance.restart_marker} — the operator watches for this file "
        f"and will restart copilot CLI and deliver this same preamble. (3) On startup: always "
        f"check for a session handoff file to resume work. (4) You are the @{agent_name} agent "
        f"with --yolo permissions (all tools/files/URLs auto-approved). (5) Operator instance: "
        f"{instance.name}. Now: check for your session handoff and get to work."
    )


# ── Session Lifecycle ───────────────────────────────────────────
def generate_run_script(instance, copilot_args, preamble=None):
    """Generate a shell script that launches copilot with the given args."""
    lines = ['#!/usr/bin/env bash' if platform.system() != 'Windows' else '@echo off']
    if preamble:
        if platform.system() == 'Windows':
            # Windows batch file — use a temp file for preamble
            lines.append(f'set PREAMBLE={preamble}')
            cmd_parts = ['copilot'] + [_shell_quote(a) for a in copilot_args]
            lines.append(' '.join(cmd_parts) + ' -i "%PREAMBLE%"')
        else:
            escaped = preamble.replace("'", "'\\''")
            lines.append(f"PREAMBLE='{escaped}'")
            cmd_parts = ['exec copilot'] + [_shell_quote(a) for a in copilot_args]
            lines.append(' '.join(cmd_parts) + ' -i "$PREAMBLE"')
    else:
        if platform.system() == 'Windows':
            cmd_parts = ['copilot'] + [_shell_quote(a) for a in copilot_args]
            lines.append(' '.join(cmd_parts))
        else:
            cmd_parts = ['exec copilot'] + [_shell_quote(a) for a in copilot_args]
            lines.append(' '.join(cmd_parts))

    instance.run_script.write_text('\n'.join(lines) + '\n')
    if platform.system() != 'Windows':
        instance.run_script.chmod(0o755)


def _shell_quote(s):
    """Quote a string for shell usage."""
    if not s or any(c in s for c in ' \t\n"\'$`\\!#&|;(){}[]<>?*~'):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


def start_copilot_in_tmux(instance, session_num, remain_on_exit='off'):
    """Launch copilot in a tmux session."""
    log(f"Session #{session_num}: launching copilot")
    log(f"  Work dir: {Path.cwd()}")

    instance.restart_marker.unlink(missing_ok=True)

    # Kill any existing session
    tmux('kill-session', '-t', instance.tmux_session)
    # Create new session
    tmux('new-session', '-d', '-s', instance.tmux_session,
         '-c', str(Path.cwd()), str(instance.run_script))
    tmux('set-option', '-t', instance.tmux_session, 'remain-on-exit', remain_on_exit)

    # Get pane PID
    out, _ = tmux('display-message', '-t', instance.tmux_session, '-p', '#{pane_pid}')
    pane_pid = out.strip() or None

    log("  Waiting for copilot to start...")
    time.sleep(3)

    mux = find_tmux()
    log(f"  Session #{session_num} running (pid={pane_pid or '?'}) — "
        f"attach with: {mux} attach -t {instance.tmux_session}")

    return pane_pid


def is_copilot_running(instance):
    """Check if copilot is still running in the tmux session."""
    out, rc = tmux('has-session', '-t', instance.tmux_session)
    if rc != 0:
        return False
    out, _ = tmux('display-message', '-t', instance.tmux_session, '-p', '#{pane_dead}')
    return out.strip() == '0'


def check_for_restart_signal(instance):
    return instance.restart_marker.exists()


def wait_for_copilot_exit(instance, timeout=15):
    elapsed = 0
    while elapsed < timeout:
        if not is_copilot_running(instance):
            return True
        time.sleep(1)
        elapsed += 1
    return False


def restart_copilot(instance, session_num, pane_pid):
    """Restart copilot after a restart signal."""
    log("Restarting copilot...")
    instance.restart_marker.unlink(missing_ok=True)

    tmux('send-keys', '-t', instance.tmux_session, '/exit', 'Enter')

    if not wait_for_copilot_exit(instance, 15):
        log("  Copilot didn't exit within 15s — capturing anyway")

    capture_and_store_metrics(session_num, pane_pid)

    _, rc = tmux('has-session', '-t', instance.tmux_session)
    if rc != 0:
        log("  Tmux session closed — will recreate")

    time.sleep(2)


# ── Single Session Mode ────────────────────────────────────────
def run_single_session(instance, copilot_args):
    _, rc = tmux('has-session', '-t', instance.tmux_session)
    if rc == 0:
        mux = find_tmux()
        print(f"Error: instance '{instance.name}' already exists.", file=sys.stderr)
        print(f"  Attach: {mux} attach -t {instance.tmux_session}", file=sys.stderr)
        print(f"  Stop:   operator stop {instance.name}", file=sys.stderr)
        sys.exit(1)

    init_metrics_db()
    run_started = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    log(f"Starting single session: {instance.name}")

    generate_run_script(instance, copilot_args)
    pane_pid = start_copilot_in_tmux(instance, 1, 'off')

    mux = find_tmux()
    # Attach — this blocks until user detaches or session ends
    tmux('attach', '-t', instance.tmux_session, capture=False)

    _, rc = tmux('has-session', '-t', instance.tmux_session)
    if rc == 0:
        print()
        print("Detached from copilot session.")
        print(f"  Re-attach: {mux} attach -t {instance.tmux_session}")
        print("  Metrics will be captured when copilot exits.")
    else:
        capture_and_store_metrics(1, pane_pid)
        show_run_summary(run_started)

    instance.cleanup()


# ── Loop Mode ──────────────────────────────────────────────────
def run_loop_mode(instance, user_args, is_fresh=False):
    copilot_args = ['--yolo', '--autopilot', '--no-ask-user']

    agent_name = extract_agent_from_args(user_args)
    has_agent = any(a == '--agent' or a.startswith('--agent=') for a in user_args)
    if not has_agent:
        copilot_args.extend(['--agent', agent_name])

    copilot_args.extend(user_args)
    preamble = build_preamble(agent_name, instance)

    init_metrics_db()

    # Auto-continue: load prior state for named instances
    start_session = 1
    run_started = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    if not is_fresh:
        state = instance.load_state()
        if state:
            start_session = int(state.get('SESSION_NUM', 0)) + 1
            run_started = state.get('RUN_STARTED', run_started)
            log(f"Continuing from session #{start_session} (run started {run_started})")

    _, rc = tmux('has-session', '-t', instance.tmux_session)
    if rc == 0:
        mux = find_tmux()
        log(f"Instance '{instance.name}' already exists. "
            f"Attach with: {mux} attach -t {instance.tmux_session}")
        log(f"To stop: operator stop {instance.name}")
        sys.exit(1)

    log("═══════════════════════════════════════════")
    log("Copilot CLI Operator starting (loop mode)")
    log(f"  Instance: {instance.name}")
    log(f"  Agent: {agent_name}")
    log(f"  Starting session: #{start_session}")
    log(f"  Max sessions: {MAX_SESSIONS}")
    log(f"  Poll interval: {POLL_INTERVAL}s")
    log(f"  Restart signal: touch {instance.restart_marker}")
    mux = find_tmux()
    log(f"  Attach: {mux} attach -t {instance.tmux_session}")
    log("═══════════════════════════════════════════")

    current_session = start_session
    pane_pid = None

    def handle_signal(signum, frame):
        print("", file=sys.stderr)
        log("Signal received — shutting down")
        if current_session > 0:
            capture_and_store_metrics(current_session, pane_pid)
            instance.save_state(current_session, run_started)
            show_run_summary(run_started)
        _, rc = tmux('has-session', '-t', instance.tmux_session)
        if rc == 0:
            tmux('kill-session', '-t', instance.tmux_session)
            log("Session ended")
        instance.cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, handle_signal)

    for session_num in range(start_session, MAX_SESSIONS + 1):
        current_session = session_num
        instance.save_state(session_num, run_started)

        generate_run_script(instance, copilot_args, preamble)
        pane_pid = start_copilot_in_tmux(instance, session_num, 'on')

        restart_requested = False
        while True:
            time.sleep(POLL_INTERVAL)
            if not is_copilot_running(instance):
                log(f"Session #{session_num}: copilot exited")
                capture_and_store_metrics(session_num, pane_pid)
                instance.save_state(session_num, run_started)
                show_run_summary(run_started)
                log("Operator shutting down")
                instance.cleanup()
                sys.exit(0)

            if check_for_restart_signal(instance):
                log(f"Session #{session_num}: restart signal detected!")
                restart_requested = True
                break

        if restart_requested:
            restart_copilot(instance, session_num, pane_pid)
            instance.save_state(session_num, run_started)

            if session_num >= MAX_SESSIONS:
                log(f"Max sessions ({MAX_SESSIONS}) reached — stopping")
                break

            log(f"Pausing before session #{session_num + 1}...")
            time.sleep(3)

    show_run_summary(run_started)
    _, rc = tmux('has-session', '-t', instance.tmux_session)
    if rc == 0:
        tmux('kill-session', '-t', instance.tmux_session)
    instance.cleanup()
    log("Operator shut down")


# ── Commands ────────────────────────────────────────────────────
def stop_operator(target=None):
    """Stop one or all operator instances."""
    log(f"Stop requested{f' for {target}' if target else ''}")

    if target:
        full_name = target
        if not target.startswith('operator-copilot-'):
            full_name = f'operator-copilot-{target}'
        found = False
        for name in [full_name, target]:
            _, rc = tmux('has-session', '-t', name)
            if rc == 0:
                tmux('kill-session', '-t', name)
                (RESTART_DIR / name).unlink(missing_ok=True)
                log(f"Stopped: {name}")
                found = True
                break
        if not found:
            print(f"No instance '{target}' found.", file=sys.stderr)
            print()
            list_instances()
            sys.exit(1)
    else:
        out, rc = tmux('list-sessions', '-F', '#{session_name}')
        count = 0
        if rc == 0:
            for name in out.splitlines():
                if name.startswith('operator-copilot-') or name == 'copilot-operator':
                    tmux('kill-session', '-t', name)
                    (RESTART_DIR / name).unlink(missing_ok=True)
                    log(f"Stopped: {name}")
                    count += 1
        if count == 0:
            print("No running operator instances found.")
        else:
            log(f"Stopped {count} instance(s)")
    sys.exit(0)


def show_help():
    help_text = """\
operator — Metrics-capturing wrapper for GitHub Copilot CLI

USAGE
    operator [--name NAME] [copilot-args...]                  Single session
    operator --loop [--name NAME] [--fresh] [copilot-args...]  Loop mode
    operator list                                              Show running instances
    operator stop [NAME]                                       Stop instance(s)
    operator report [type]                                     View usage reports
    operator ingest [--force]                                  Process copilot logs
    operator help                                              Show this help

OPTIONS
    --name NAME     Set instance name (default: operator-copilot-{N}, auto-incremented)
    --loop          Enable autonomous loop mode
    --fresh         Reset session numbering (ignore prior state)

MODES
    Single session (default)
        Launches copilot in tmux with your args, auto-attaches.
        When copilot exits, usage metrics are parsed from its process
        log and stored in the metrics database.

    Loop mode (--loop)
        Adds --yolo --autopilot --no-ask-user automatically.
        Sends a preamble for autonomous operation. Restarts copilot
        when the agent touches the instance-specific restart marker.
        Ctrl+C captures metrics and shows an aggregate run summary.

        Named instances auto-continue when restarted — session numbering
        and run summary scope carry over. Use --fresh to reset.

MULTI-INSTANCE
    Multiple operator instances can run concurrently. Each gets its own
    tmux session and restart marker file. Use --name to assign a specific
    name, or let the operator auto-assign operator-copilot-1, -2, etc.

    operator --loop --name matrix --agent anvil:anvil
    operator --loop --name academy --agent anvil:anvil
    operator list                     # see all running
    operator stop matrix              # stop one
    operator stop                     # stop all

REPORTS
    operator report summary       Premium request totals (today, week, all time)
    operator report sessions      Last 20 sessions with details
    operator report models        Usage breakdown by AI model
    operator report projects      Usage breakdown by project directory
    operator report costs         Cost estimates at $0.04/premium request

EXAMPLES
    operator --agent=anvil:anvil --yolo
    operator --loop --agent=anvil:anvil --model=claude-opus-4.6-1m
    operator --loop --name myproject --agent=anvil:anvil
    operator report costs
    operator ingest
    operator ingest --force

INGEST
    Scans ~/.copilot/logs/ for copilot process logs and stores usage
    metrics in the database. Logs already processed are skipped unless
    the file has been modified since last ingestion (detected via mtime).

    --force     Reprocess ALL log files, updating existing records.

FILES
    ~/.copilot/operator-metrics.db      SQLite metrics database
    ~/.copilot/operator.log             Operator log file
    operator_ingest.py                  Log parser (lives next to copilot_operator.py)
    ~/.copilot/logs/process-*.log       Copilot process logs (source data)
    ~/.copilot/restart/                 Per-instance restart marker files

DEPENDENCIES
    tmux (Linux/macOS) or psmux (Windows), python3, copilot

CROSS-PLATFORM
    On Windows, install psmux (native tmux for Windows):
        winget install psmux
    psmux ships a `tmux` binary alias, so all operator commands work identically.
"""
    print(help_text)


# ── Main ────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        show_help()
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd in ('help', '-h', '--help', '-?'):
        show_help()
        sys.exit(0)
    elif cmd == 'stop':
        stop_operator(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == 'list':
        list_instances()
        sys.exit(0)
    elif cmd == 'report':
        report_metrics(sys.argv[2] if len(sys.argv) > 2 else 'summary')
        sys.exit(0)
    elif cmd == 'ingest':
        ingest_all_logs(force='--force' in sys.argv)
        sys.exit(0)

    # Check prerequisites
    if not find_tmux():
        if platform.system() == 'Windows':
            print("Error: psmux is required. Install: winget install psmux", file=sys.stderr)
        else:
            print("Error: tmux is required but not found.", file=sys.stderr)
        sys.exit(1)
    if not shutil.which('copilot'):
        print("Error: copilot CLI is required but not found.", file=sys.stderr)
        sys.exit(1)

    # Parse operator-specific args, pass rest to copilot
    loop_mode = False
    is_fresh = False
    instance_name = None
    copilot_args = []

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == '--loop':
            loop_mode = True
        elif args[i] == '--fresh':
            is_fresh = True
        elif args[i] == '--name':
            if i + 1 >= len(args):
                print("Error: --name requires a value", file=sys.stderr)
                sys.exit(1)
            instance_name = args[i + 1]
            i += 1
        elif args[i].startswith('--name='):
            instance_name = args[i].split('=', 1)[1]
        else:
            copilot_args.append(args[i])
        i += 1

    if not instance_name:
        instance_name = f'operator-copilot-{next_instance_number()}'

    instance = Instance(instance_name)

    if is_fresh:
        instance.state_file.unlink(missing_ok=True)

    if loop_mode:
        run_loop_mode(instance, copilot_args, is_fresh)
    else:
        run_single_session(instance, copilot_args)


if __name__ == '__main__':
    main()
