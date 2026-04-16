#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# Copilot CLI Operator — Metrics-capturing wrapper for GitHub Copilot CLI
#
# Wraps the copilot CLI to capture usage metrics (premium requests,
# API time, session time, per-model breakdown) into a SQLite database.
# Supports single-session mode (default) and autonomous loop mode.
#
# Usage:
#   ./operator.sh [copilot-args...]               # single session
#   ./operator.sh --loop [copilot-args...]         # autonomous loop mode
#   ./operator.sh report [summary|sessions|models|projects|costs]
#   ./operator.sh ingest                          # process all copilot logs
#   ./operator.sh stop                            # stop loop mode
#
# Examples:
#   ./operator.sh --agent anvil:anvil --yolo
#   ./operator.sh --loop --agent anvil:anvil --model claude-opus-4.6-1m
#   ./operator.sh report costs
#
# Single-session mode launches copilot in tmux and auto-attaches.
# When copilot exits, metrics are captured and a brief summary shown.
#
# Loop mode (--loop) adds --yolo --autopilot --no-ask-user, sends a
# preamble for autonomous operation, and restarts copilot when the
# agent signals via a restart marker file. Ctrl+C shows aggregate stats.
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

# Resolve the directory where this script lives (follows symlinks)
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

# ── Constants ───────────────────────────────────────────────────
RESTART_DIR="${HOME}/.copilot/restart"
LOG_FILE="${HOME}/.copilot/operator.log"
METRICS_DB="${HOME}/.copilot/operator-metrics.db"
COPILOT_LOG_DIR="${HOME}/.copilot/logs"
POLL_INTERVAL=10
MAX_SESSIONS=1000

# Instance-specific (set by derive_instance_paths)
INSTANCE_NAME=""
TMUX_SESSION=""
RESTART_MARKER=""
STATE_FILE=""
RUN_SCRIPT=""

# Runtime state
OPERATOR_RUN_STARTED=""
CURRENT_SESSION_NUM=0
IS_LOOP_MODE=false
IS_FRESH=false
COPILOT_PANE_PID=""

# ── Logging ─────────────────────────────────────────────────────
mkdir -p "$(dirname "$LOG_FILE")"

log() {
    local msg="[operator $(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg" >> "$LOG_FILE"
    echo "$msg" >&2
}

die() { echo "Error: $*" >&2; exit 1; }

# ── Instance Management ────────────────────────────────────────

# Sanitize a name for use as a tmux session name.
# tmux silently replaces '.' and ':' with '_', which causes all
# subsequent -t lookups to fail. We replace them with '-' upfront.
sanitize_session_name() {
    local name="$1"
    name="${name//./-}"
    name="${name//:/-}"
    echo "$name"
}

# Set the terminal tab/window title via OSC 0. Windows Terminal honors this
# from WSL. Only emits when stdout is a TTY so it doesn't pollute logs/pipes.
# tmux default config has set-titles off, so this persists through tmux attach.
set_tab_title() {
    if [[ -t 1 ]]; then
        printf '\033]0;%s\007' "$1"
    fi
}

derive_instance_paths() {
    local name="$1"
    INSTANCE_NAME="$name"
    TMUX_SESSION="$name"
    RESTART_MARKER="${RESTART_DIR}/${name}"
    STATE_FILE="${RESTART_DIR}/${name}.state"
    RUN_SCRIPT="${HOME}/.copilot/operator-run-${name}.sh"
    mkdir -p "$RESTART_DIR"
}

save_instance_state() {
    [[ -z "$STATE_FILE" ]] && return
    printf 'SESSION_NUM=%d\nRUN_STARTED=%s\n' \
        "$CURRENT_SESSION_NUM" "$OPERATOR_RUN_STARTED" > "$STATE_FILE"
}

load_instance_state() {
    [[ -z "$STATE_FILE" || ! -f "$STATE_FILE" ]] && return 1
    local line
    while IFS='=' read -r key val; do
        case "$key" in
            SESSION_NUM)  CURRENT_SESSION_NUM="$val" ;;
            RUN_STARTED)  OPERATOR_RUN_STARTED="$val" ;;
        esac
    done < "$STATE_FILE"
    return 0
}

list_instances() {
    echo "═══ Running Operator Instances ═══"
    echo
    local found=false
    # Collect names of sessions managed by operator (have a .managed or .state marker)
    local -A managed_sessions
    for f in "${RESTART_DIR}"/*.state "${RESTART_DIR}"/*.managed; do
        [[ -e "$f" ]] || continue
        local base
        base=$(basename "$f")
        base="${base%.state}"
        base="${base%.managed}"
        managed_sessions["$base"]=1
    done
    while IFS= read -r line; do
        local name
        name=$(echo "$line" | cut -d: -f1)
        if [[ -n "${managed_sessions[$name]+x}" ]]; then
            echo "  $line"
            found=true
        fi
    done < <(tmux list-sessions 2>/dev/null)
    if [[ "$found" == false ]]; then
        echo "  (none)"
    fi
    echo
    echo "Attach: tmux attach -t <name>"
    echo "Stop:   $0 stop <name>"
}

handle_existing_session() {
    if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        return 0
    fi
    echo "Session '$TMUX_SESSION' is already running." >&2
    printf "Stop it and start a new one? [y/N] " >&2
    local answer=""
    read -r answer < /dev/tty 2>/dev/null || answer=""
    case "$answer" in
        [Yy]|[Yy][Ee][Ss])
            log "Stopping existing session '$TMUX_SESSION' at user request"
            tmux kill-session -t "$TMUX_SESSION"
            rm -f "${RESTART_DIR}/${TMUX_SESSION}" "${RESTART_DIR}/${TMUX_SESSION}.state" "${RESTART_DIR}/${TMUX_SESSION}.managed"
            sleep 1
            ;;
        *)
            echo "Aborted." >&2
            exit 1
            ;;
    esac
}

# ── Metrics Database ────────────────────────────────────────────
init_metrics_db() {
    sqlite3 "$METRICS_DB" <<'SCHEMA'
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
SCHEMA
    # Migration: add log_file_mtime for existing databases
    sqlite3 "$METRICS_DB" "ALTER TABLE sessions ADD COLUMN log_file_mtime TEXT;" 2>/dev/null || true
}

# ── Metrics Helpers ─────────────────────────────────────────────
time_str_to_seconds() {
    local s="$1" h=0 m=0 sec=0
    [[ "$s" =~ ([0-9]+)h ]] && h="${BASH_REMATCH[1]}" || true
    [[ "$s" =~ ([0-9]+)m ]] && m="${BASH_REMATCH[1]}" || true
    [[ "$s" =~ ([0-9]+)s ]] && sec="${BASH_REMATCH[1]}" || true
    echo $(( h * 3600 + m * 60 + sec ))
}

sql_escape() {
    printf '%s' "${1//\'/\'\'}"
}

find_copilot_log() {
    # Find the copilot process log file by PID.
    # Log files are named: process-{startTimeMs}-{pid}.log
    local pid="$1"
    if [[ -n "$pid" ]]; then
        local logfile
        logfile=$(ls -t "${COPILOT_LOG_DIR}"/process-*-"${pid}".log 2>/dev/null | head -1)
        if [[ -n "$logfile" ]]; then
            echo "$logfile"
            return 0
        fi
    fi
    # Fallback: most recently modified log file
    ls -t "${COPILOT_LOG_DIR}"/process-*.log 2>/dev/null | head -1
}

capture_and_store_metrics() {
    local session_num="$1"
    local copilot_pid="${2:-$COPILOT_PANE_PID}"

    # Best-effort — never crash the operator
    (
        set +e

        local logfile
        logfile=$(find_copilot_log "$copilot_pid")
        if [[ -z "$logfile" ]] || [[ ! -f "$logfile" ]]; then
            log "  Metrics: no copilot process log found (pid=${copilot_pid:-?})"
            return 0
        fi

        log "  Metrics: ingesting $(basename "$logfile")..."

        local result
        result=$(python3 "${SCRIPT_DIR}/operator-ingest.py" \
            "$logfile" "$METRICS_DB" \
            --session-num "$session_num" \
            --work-dir "$(pwd)" 2>&1)

        log "  Metrics: $result"
    ) || log "  Warning: metrics capture failed (non-fatal)"
}

ingest_all_logs() {
    init_metrics_db

    local force_flag=""
    [[ "${1:-}" == "--force" ]] && force_flag="--force"

    local total=0 ingested=0 skipped=0 empty=0

    echo "Scanning ${COPILOT_LOG_DIR} for unprocessed logs..."

    for logfile in "${COPILOT_LOG_DIR}"/process-*.log; do
        [[ -f "$logfile" ]] || continue
        (( total++ )) || true

        local result rc
        result=$(python3 "${SCRIPT_DIR}/operator-ingest.py" \
            "$logfile" "$METRICS_DB" $force_flag 2>&1) && rc=0 || rc=$?

        if [[ "$result" == SKIP* ]]; then
            (( skipped++ )) || true
        elif [[ $rc -eq 2 ]]; then
            (( empty++ )) || true
        elif [[ $rc -eq 0 ]]; then
            (( ingested++ )) || true
            echo "  $result"
        else
            echo "  ERROR: $result" >&2
        fi
    done

    echo ""
    echo "Done: ${total} logs scanned, ${ingested} ingested, ${empty} no usage, ${skipped} already processed"

    if (( ingested > 0 )); then
        echo ""
        report_metrics summary
    fi
}

# ── Reports ─────────────────────────────────────────────────────
report_metrics() {
    if [[ ! -f "$METRICS_DB" ]]; then
        echo "No metrics database found at $METRICS_DB"
        echo "Run the operator first to start collecting metrics."
        exit 1
    fi

    local subcmd="${1:-summary}"
    local home_esc
    home_esc=$(sql_escape "$HOME")

    case "$subcmd" in
        summary)
            echo "═══ Usage Summary ═══"
            echo
            sqlite3 -header -column "$METRICS_DB" "
                SELECT
                    COALESCE(SUM(CASE WHEN date(ended_at,'localtime') = date('now','localtime') THEN premium_requests ELSE 0 END), 0) AS today,
                    COALESCE(SUM(CASE WHEN ended_at >= datetime('now','-7 days') THEN premium_requests ELSE 0 END), 0) AS this_week,
                    COALESCE(SUM(premium_requests), 0) AS all_time,
                    COUNT(*) AS sessions
                FROM sessions WHERE no_op = 0;
            "
            ;;
        sessions)
            echo "═══ Recent Sessions ═══"
            echo
            sqlite3 -header -column "$METRICS_DB" "
                SELECT session_num AS '#',
                       substr(started_at, 1, 16) AS started,
                       COALESCE(premium_requests, 0) AS premium,
                       COALESCE(api_time_seconds || 's', '—') AS api_time,
                       COALESCE(
                           CASE
                               WHEN session_time_seconds >= 3600 THEN
                                   (session_time_seconds / 3600) || 'h ' || ((session_time_seconds % 3600) / 60) || 'm'
                               ELSE (session_time_seconds / 60) || 'm ' || (session_time_seconds % 60) || 's'
                           END, '—') AS sess_time,
                       '+' || COALESCE(lines_added,0) || ' -' || COALESCE(lines_removed,0) AS changes,
                       COALESCE(substr(git_branch,1,20),'—') AS branch,
                       COALESCE(replace(work_dir, '${home_esc}', '~'), '—') AS project
                FROM sessions WHERE no_op = 0 ORDER BY id DESC LIMIT 20;
            "
            ;;
        models)
            echo "═══ Per-Model Usage ═══"
            echo
            sqlite3 -header -column "$METRICS_DB" "
                SELECT model_name AS model,
                       COALESCE(SUM(premium_requests), 0) AS total_premium,
                       COUNT(*) AS appearances
                FROM model_usage
                GROUP BY model_name
                ORDER BY total_premium DESC;
            "
            ;;
        projects)
            echo "═══ Per-Project Usage ═══"
            echo
            sqlite3 -header -column "$METRICS_DB" "
                SELECT COALESCE(replace(work_dir, '${home_esc}', '~'), '—') AS project,
                       COALESCE(SUM(premium_requests), 0) AS total_premium,
                       COUNT(*) AS sessions,
                       COALESCE(SUM(api_time_seconds), 0) || 's' AS total_api_time
                FROM sessions WHERE no_op = 0
                GROUP BY work_dir
                ORDER BY total_premium DESC;
            "
            ;;
        costs)
            echo "═══ Cost Estimates (Enterprise @ \$0.04/premium request) ═══"
            echo
            sqlite3 -header -column "$METRICS_DB" "
                SELECT
                    COALESCE(SUM(CASE WHEN date(ended_at,'localtime') = date('now','localtime') THEN premium_requests ELSE 0 END), 0) AS today_reqs,
                    printf('\$%.2f', COALESCE(SUM(CASE WHEN date(ended_at,'localtime') = date('now','localtime') THEN premium_requests ELSE 0 END), 0) * 0.04) AS today_cost,
                    COALESCE(SUM(CASE WHEN ended_at >= datetime('now','-7 days') THEN premium_requests ELSE 0 END), 0) AS week_reqs,
                    printf('\$%.2f', COALESCE(SUM(CASE WHEN ended_at >= datetime('now','-7 days') THEN premium_requests ELSE 0 END), 0) * 0.04) AS week_cost,
                    COALESCE(SUM(CASE WHEN strftime('%%Y-%%m', ended_at) = strftime('%%Y-%%m', 'now') THEN premium_requests ELSE 0 END), 0) AS month_reqs,
                    printf('\$%.2f', COALESCE(SUM(CASE WHEN strftime('%%Y-%%m', ended_at) = strftime('%%Y-%%m', 'now') THEN premium_requests ELSE 0 END), 0) * 0.04) AS month_cost,
                    COALESCE(SUM(premium_requests), 0) AS all_time_reqs,
                    printf('\$%.2f', COALESCE(SUM(premium_requests), 0) * 0.04) AS all_time_cost
                FROM sessions WHERE no_op = 0;
            "
            echo
            echo "Note: Enterprise plan includes 1,000 premium requests/month."
            echo "      Costs above assume overage pricing (\$0.04/request)."
            echo "      Actual cost depends on your remaining monthly allowance."
            ;;
        *)
            echo "Usage: $0 report [summary|sessions|models|projects|costs]"
            echo
            echo "  summary   — Premium request totals (today, week, all time)"
            echo "  sessions  — Last 20 sessions with details"
            echo "  models    — Usage breakdown by AI model"
            echo "  projects  — Usage breakdown by project directory"
            echo "  costs     — Cost estimates at enterprise overage rates"
            exit 1
            ;;
    esac
}

show_run_summary() {
    if [[ -z "$OPERATOR_RUN_STARTED" ]] || [[ ! -f "$METRICS_DB" ]]; then
        return
    fi

    echo ""
    echo "═══ Operator Run Summary ═══"
    echo ""
    sqlite3 -header -column "$METRICS_DB" "
        SELECT
            COUNT(*) AS sessions,
            COALESCE(SUM(premium_requests), 0) AS total_premium,
            COALESCE(SUM(api_time_seconds), 0) || 's' AS total_api_time,
            COALESCE(
                CASE
                    WHEN SUM(session_time_seconds) >= 3600 THEN
                        (SUM(session_time_seconds) / 3600) || 'h ' || ((SUM(session_time_seconds) % 3600) / 60) || 'm'
                    ELSE COALESCE((SUM(session_time_seconds) / 60) || 'm ' || (SUM(session_time_seconds) % 60) || 's', '0s')
                END, '0s') AS total_sess_time,
            '+' || COALESCE(SUM(lines_added), 0) || ' -' || COALESCE(SUM(lines_removed), 0) AS total_changes,
            printf('\$%.2f', COALESCE(SUM(premium_requests), 0) * 0.04) AS est_cost
        FROM sessions
        WHERE no_op = 0 AND ended_at >= '${OPERATOR_RUN_STARTED}';
    "
    local model_count
    model_count=$(sqlite3 "$METRICS_DB" "
        SELECT COUNT(DISTINCT m.model_name)
        FROM model_usage m JOIN sessions s ON m.session_id = s.id
        WHERE s.no_op = 0 AND s.ended_at >= '${OPERATOR_RUN_STARTED}';
    ")
    if (( model_count > 0 )); then
        echo ""
        sqlite3 -header -column "$METRICS_DB" "
            SELECT m.model_name AS model,
                   COALESCE(SUM(m.premium_requests), 0) AS premium,
                   COUNT(*) AS uses
            FROM model_usage m
            JOIN sessions s ON m.session_id = s.id
            WHERE s.no_op = 0 AND s.ended_at >= '${OPERATOR_RUN_STARTED}'
            GROUP BY m.model_name
            ORDER BY premium DESC;
        "
    fi
}

# ── Argument Helpers ────────────────────────────────────────────
extract_agent_from_args() {
    local prev=""
    for arg in "$@"; do
        if [[ "$arg" == --agent=* ]]; then
            echo "${arg#--agent=}"
            return
        fi
        if [[ "$prev" == "--agent" ]]; then
            echo "$arg"
            return
        fi
        prev="$arg"
    done
    echo "anvil:anvil"
}

build_preamble() {
    local agent_name="$1"
    printf '%s' "You are running under an automated operator wrapper that a human set up. Key facts: (1) You have blanket human approval for ALL decisions — tool calls, file edits, git operations, architectural choices. Do not ask for direction or confirmation. Make your best judgment call and proceed. If you are genuinely uncertain between approaches that have very different consequences, state your reasoning and pick one. (2) Session restart: when context gets heavy or a task is complete with next steps, use the handoff command: handoff --instance ${INSTANCE_NAME} --status \"what you completed\" --next \"what to do next\" --context \"key decisions and gotchas\" — this atomically writes the handoff file and triggers the restart. (3) On startup: always check for a session handoff file to resume work. (4) You are the @${agent_name} agent with --yolo permissions (all tools/files/URLs auto-approved). (5) Operator instance: ${INSTANCE_NAME}. Now: check for your session handoff and get to work."
}

# ── Commands ────────────────────────────────────────────────────
cleanup() {
    echo "" >&2
    log "Signal received — shutting down"

    if [[ "$IS_LOOP_MODE" == true ]] && (( CURRENT_SESSION_NUM > 0 )); then
        capture_and_store_metrics "$CURRENT_SESSION_NUM"
        save_instance_state
        show_run_summary
    fi

    if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        tmux kill-session -t "$TMUX_SESSION"
        log "Session ended"
    fi
    rm -f "$RUN_SCRIPT" "$RESTART_MARKER" "${RESTART_DIR}/${TMUX_SESSION}.managed"
    exit 0
}

show_help() {
    cat << 'HELP'
operator — Metrics-capturing wrapper for GitHub Copilot CLI

USAGE
    operator [--name NAME] [copilot-args...]                  Single session
    operator --loop [--name NAME] [--fresh] [copilot-args...]  Loop mode
    operator NAME                                              Join a running instance
    operator join [NAME]                                       Join (explicit form)
    operator reload NAME                                       Hot-reload run script
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
    name, or let the operator use the current directory name by default.

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
    operator myproject                    # quick join
    operator join myproject               # explicit join
    operator reload myproject             # hot-reload run script
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
    operator-ingest.py                  Log parser (lives next to operator.sh)
    ~/.copilot/logs/process-*.log       Copilot process logs (source data)
    ~/.copilot/restart/                 Per-instance restart marker files
    ~/.copilot/operator-backups/        Backups of operator.sh

DEPENDENCIES
    tmux, sqlite3, python3, copilot
HELP
}

stop_operator() {
    local target="${1:-}"
    [[ -n "$target" ]] && target="$(sanitize_session_name "$target")"
    log "Stop requested${target:+ for $target}"

    if [[ -n "$target" ]]; then
        if tmux has-session -t "$target" 2>/dev/null; then
            tmux kill-session -t "$target"
            rm -f "${RESTART_DIR}/${target}" "${RESTART_DIR}/${target}.state" "${RESTART_DIR}/${target}.managed"
            log "Stopped: $target"
        else
            echo "No instance '$target' found." >&2
            echo
            list_instances
            exit 1
        fi
    else
        local count=0
        # Find all operator-managed sessions via .managed and .state markers
        local -A managed_sessions
        for f in "${RESTART_DIR}"/*.managed "${RESTART_DIR}"/*.state; do
            [[ -e "$f" ]] || continue
            local base
            base=$(basename "$f")
            base="${base%.managed}"
            base="${base%.state}"
            managed_sessions["$base"]=1
        done
        while IFS= read -r name; do
            if [[ -n "${managed_sessions[$name]+x}" ]]; then
                tmux kill-session -t "$name"
                rm -f "${RESTART_DIR}/${name}" "${RESTART_DIR}/${name}.state" "${RESTART_DIR}/${name}.managed"
                log "Stopped: $name"
                (( count++ )) || true
            fi
        done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null)
        if (( count == 0 )); then
            echo "No running operator instances found."
        else
            log "Stopped $count instance(s)"
        fi
    fi
    exit 0
}

# ── Run Script Generation ──────────────────────────────────────
# Global: set SCRIPT_PREAMBLE before calling to include a preamble.
SCRIPT_PREAMBLE=""

generate_run_script() {
    local copilot_args=("$@")

    {
        printf '#!/usr/bin/env bash\n'
        if [[ -n "$SCRIPT_PREAMBLE" ]]; then
            printf 'PREAMBLE=%q\n' "$SCRIPT_PREAMBLE"
        fi
        printf 'exec copilot'
        for arg in "${copilot_args[@]}"; do
            printf ' %q' "$arg"
        done
        if [[ -n "$SCRIPT_PREAMBLE" ]]; then
            printf ' -i "$PREAMBLE"'
        fi
        printf '\n'
    } > "$RUN_SCRIPT"
    chmod +x "$RUN_SCRIPT"
}

# ── Session Lifecycle ───────────────────────────────────────────
start_copilot_in_tmux() {
    local session_num=$1
    local remain_on_exit="${2:-off}"

    log "Session #${session_num}: launching copilot"
    log "  Work dir: $(pwd)"

    rm -f "$RESTART_MARKER"

    tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true

    tmux new-session -d -s "$TMUX_SESSION" -c "$(pwd)" "$RUN_SCRIPT"
    tmux set-option -t "$TMUX_SESSION" remain-on-exit "$remain_on_exit"

    # Record the pane PID so we can find the right process log later
    COPILOT_PANE_PID=$(tmux display-message -t "$TMUX_SESSION" -p '#{pane_pid}' 2>/dev/null || echo "")

    log "  Waiting for copilot to start..."
    sleep 3

    log "  Session #${session_num} running (pid=$COPILOT_PANE_PID) — attach with: tmux attach -t $TMUX_SESSION"

    # Mark this session as operator-managed so list_instances can find it
    touch "${RESTART_DIR}/${TMUX_SESSION}.managed"
}

check_for_restart_signal() {
    [[ -f "$RESTART_MARKER" ]]
}

is_copilot_running() {
    if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        return 1
    fi
    local pane_dead
    pane_dead=$(tmux display-message -t "$TMUX_SESSION" -p '#{pane_dead}' 2>/dev/null || echo "1")
    [[ "$pane_dead" == "0" ]]
}

wait_for_copilot_exit() {
    local timeout=${1:-15} elapsed=0
    while (( elapsed < timeout )); do
        if ! is_copilot_running; then
            return 0
        fi
        sleep 1
        (( elapsed++ )) || true
    done
    return 1
}

restart_copilot() {
    local session_num=$1
    log "Restarting copilot..."

    rm -f "$RESTART_MARKER"

    tmux send-keys -t "$TMUX_SESSION" "/exit" Enter 2>/dev/null || true

    wait_for_copilot_exit 15 || log "  Copilot didn't exit within 15s — capturing anyway"

    # Parse metrics from copilot's process log (reliable, no terminal capture)
    capture_and_store_metrics "$session_num"

    if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        log "  Tmux session closed — will recreate"
    fi

    sleep 2
}

# ── Single Session Mode ────────────────────────────────────────
run_single_session() {
    local copilot_args=("--autopilot" "--effort" "high" "$@")

    handle_existing_session

    init_metrics_db
    OPERATOR_RUN_STARTED=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

    log "Starting single session: $INSTANCE_NAME"

    SCRIPT_PREAMBLE=""
    generate_run_script "${copilot_args[@]}"
    start_copilot_in_tmux 1 off

    set_tab_title "operator - $INSTANCE_NAME"
    tmux attach -t "$TMUX_SESSION" 2>/dev/null || true

    if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        echo ""
        echo "Detached from copilot session."
        echo "  Re-attach: tmux attach -t $TMUX_SESSION"
        echo "  Metrics will be captured when copilot exits."
    else
        # Session ended — parse metrics from copilot's process log
        capture_and_store_metrics 1
        show_run_summary
    fi

    rm -f "$RUN_SCRIPT" "${RESTART_DIR}/${TMUX_SESSION}.managed"
}

# ── Loop Mode ──────────────────────────────────────────────────
run_loop_mode() {
    local user_args=("$@")
    IS_LOOP_MODE=true

    trap cleanup SIGINT SIGTERM

    local copilot_args=("--yolo" "--autopilot" "--no-ask-user" "--effort" "high")

    local agent_name
    agent_name=$(extract_agent_from_args "${user_args[@]}")
    local has_agent=false
    for arg in "${user_args[@]}"; do
        [[ "$arg" == "--agent" || "$arg" == --agent=* ]] && has_agent=true
    done
    if [[ "$has_agent" == false ]]; then
        copilot_args+=("--agent" "$agent_name")
    fi

    copilot_args+=("${user_args[@]}")

    SCRIPT_PREAMBLE=$(build_preamble "$agent_name")

    init_metrics_db

    # Auto-continue: load prior state for named instances
    local start_session=1
    if [[ "$IS_FRESH" == false ]] && load_instance_state; then
        start_session=$(( CURRENT_SESSION_NUM + 1 ))
        log "Continuing from session #${start_session} (run started ${OPERATOR_RUN_STARTED})"
    else
        OPERATOR_RUN_STARTED=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    fi

    handle_existing_session

    log "═══════════════════════════════════════════"
    log "Copilot CLI Operator starting (loop mode)"
    log "  Instance: $INSTANCE_NAME"
    log "  Agent: $agent_name"
    log "  Starting session: #${start_session}"
    log "  Max sessions: $MAX_SESSIONS"
    log "  Poll interval: ${POLL_INTERVAL}s"
    log "  Restart signal: touch $RESTART_MARKER"
    log "  Attach: tmux attach -t $TMUX_SESSION"
    log "═══════════════════════════════════════════"

    set_tab_title "operator - $INSTANCE_NAME"

    for session_num in $(seq "$start_session" "$MAX_SESSIONS"); do
        CURRENT_SESSION_NUM=$session_num
        save_instance_state

        generate_run_script "${copilot_args[@]}"
        start_copilot_in_tmux "$session_num" on

        local restart_requested=false
        while true; do
            sleep "$POLL_INTERVAL"

            if ! is_copilot_running; then
                log "Session #${session_num}: copilot exited"
                capture_and_store_metrics "$session_num"
                save_instance_state
                show_run_summary
                log "Operator shutting down"
                rm -f "$RUN_SCRIPT" "$RESTART_MARKER" "${RESTART_DIR}/${TMUX_SESSION}.managed"
                exit 0
            fi

            if check_for_restart_signal; then
                log "Session #${session_num}: restart signal detected!"
                restart_requested=true
                break
            fi
        done

        if [[ "$restart_requested" == true ]]; then
            restart_copilot "$session_num"
            save_instance_state

            if (( session_num >= MAX_SESSIONS )); then
                log "Max sessions ($MAX_SESSIONS) reached — stopping"
                break
            fi

            log "Pausing before session #$((session_num + 1))..."
            sleep 3
        fi
    done

    show_run_summary
    if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        tmux kill-session -t "$TMUX_SESSION"
    fi
    rm -f "$RUN_SCRIPT" "$RESTART_MARKER" "${RESTART_DIR}/${TMUX_SESSION}.managed"
    log "Operator shut down"
}

# ── Main ────────────────────────────────────────────────────────
RESERVED_WORDS="stop list report ingest help join reload"

join_instance() {
    local target="${1:-}"
    if [[ -z "$target" ]]; then
        list_instances
        exit 0
    fi
    target="$(sanitize_session_name "$target")"
    if tmux has-session -t "$target" 2>/dev/null; then
        set_tab_title "terminal - $target"
        exec tmux attach -t "$target"
    else
        echo "No instance '$target' found." >&2
        echo
        list_instances
        exit 1
    fi
}

reload_instance() {
    local target="${1:-}"
    if [[ -z "$target" ]]; then
        echo "Usage: operator reload NAME" >&2
        echo "Re-generates the run script for an instance using the current operator.sh." >&2
        exit 1
    fi
    target="$(sanitize_session_name "$target")"
    derive_instance_paths "$target"
    if [[ ! -f "$RUN_SCRIPT" ]]; then
        die "No run script found for '$target' at $RUN_SCRIPT"
    fi
    # Extract the current copilot command from the existing run script (line starting with exec)
    local exec_line
    exec_line=$(grep '^exec copilot' "$RUN_SCRIPT" 2>/dev/null || echo "")
    if [[ -z "$exec_line" ]]; then
        die "Cannot parse run script: $RUN_SCRIPT"
    fi
    # Rebuild the preamble with current operator.sh logic
    local agent_name
    agent_name=$(sed -n 's/.*--agent[= ]\([^ ]*\).*/\1/p' "$RUN_SCRIPT" | head -1)
    agent_name="${agent_name:-anvil:anvil}"
    SCRIPT_PREAMBLE=$(build_preamble "$agent_name")
    # Extract copilot args from exec line (strip 'exec copilot ')
    local args_str="${exec_line#exec copilot }"
    # Remove the old -i "$PREAMBLE" from args
    args_str=$(echo "$args_str" | sed 's/ -i "\$PREAMBLE"$//')
    # Ensure --effort high is present
    if [[ "$args_str" != *"--effort"* ]]; then
        args_str="--effort high ${args_str}"
    fi
    # Rebuild the run script
    {
        printf '#!/usr/bin/env bash\n'
        printf 'PREAMBLE=%q\n' "$SCRIPT_PREAMBLE"
        printf '%s' "exec copilot ${args_str}"
        printf ' -i "$PREAMBLE"\n'
    } > "$RUN_SCRIPT"
    chmod +x "$RUN_SCRIPT"
    log "Reloaded run script for $target"
    echo "✅ Run script updated: $RUN_SCRIPT"
    echo "   Changes take effect on next copilot restart."
}

is_reserved_word() {
    local word="$1"
    local w
    for w in $RESERVED_WORDS; do
        [[ "$w" == "$word" ]] && return 0
    done
    return 1
}

main() {
    case "${1:-}" in
        stop)
            stop_operator "${2:-}"
            ;;
        list)
            list_instances
            exit 0
            ;;
        report)
            report_metrics "${2:-summary}"
            exit 0
            ;;
        ingest)
            ingest_all_logs "${2:-}"
            exit 0
            ;;
        join)
            join_instance "${2:-}"
            exit 0
            ;;
        reload)
            reload_instance "${2:-}"
            exit 0
            ;;
        help|-h|--help|-\?)
            show_help
            exit 0
            ;;
    esac

    # Positional shortcut: operator foo → join running instance named "foo"
    if [[ $# -eq 1 && "${1:-}" != --* && -n "${1:-}" ]] && ! is_reserved_word "${1:-}"; then
        if tmux has-session -t "$1" 2>/dev/null; then
            set_tab_title "terminal - $1"
            exec tmux attach -t "$1"
        fi
    fi

    if ! command -v tmux &>/dev/null; then
        echo "Error: tmux is required but not found." >&2
        exit 1
    fi
    if ! command -v sqlite3 &>/dev/null; then
        echo "Error: sqlite3 is required but not found." >&2
        exit 1
    fi
    if ! command -v python3 &>/dev/null; then
        echo "Error: python3 is required but not found." >&2
        exit 1
    fi

    local loop_mode=false
    local instance_name=""
    local copilot_args=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --loop)
                loop_mode=true
                shift
                ;;
            --fresh)
                IS_FRESH=true
                shift
                ;;
            --name)
                if [[ $# -lt 2 || -z "${2:-}" ]]; then
                    echo "Error: --name requires a value" >&2
                    exit 1
                fi
                instance_name="$2"
                shift 2
                ;;
            --name=*)
                instance_name="${1#--name=}"
                shift
                ;;
            *)
                copilot_args+=("$1")
                shift
                ;;
        esac
    done

    if [[ -z "$instance_name" ]]; then
        instance_name="$(basename "$(pwd)")"
        if [[ -z "$instance_name" || "$instance_name" == "/" ]]; then
            die "--name is required when running from the filesystem root"
        fi
    fi

    instance_name="$(sanitize_session_name "$instance_name")"

    derive_instance_paths "$instance_name"

    if [[ "$IS_FRESH" == true ]]; then
        rm -f "$STATE_FILE"
    fi

    if [[ "$loop_mode" == true ]]; then
        run_loop_mode "${copilot_args[@]}"
    else
        run_single_session "${copilot_args[@]}"
    fi
}

main "$@"
