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
# It adds --autopilot --effort high --experimental.
#
# Loop mode (--loop) adds --yolo --autopilot --no-ask-user --effort high --experimental, sends a
# preamble for autonomous operation, and restarts copilot when the
# agent signals via a restart marker file. Ctrl+C shows aggregate stats.
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

# Resolve the directory where this script lives, following symlinks.
#
# `readlink -f` is GNU-only. macOS ships BSD readlink, which has no `-f` at
# all -- setup.sh has said so in a comment since it was written, and this was
# the last script in the repo still relying on it.
#
# The failure it produced was not the abort you would hope for. The failing
# `readlink` sits inside `$(dirname "$(...)")`, so `set -e` never sees it: the
# assignment takes the status of the OUTER `$(cd ... && pwd)`, which succeeds.
# `dirname ""` is ".", so `cd .` works, and SCRIPT_DIR silently became the
# CALLER'S working directory. The two `operator-ingest.py` invocations below
# then looked for it wherever the user happened to be standing, and the only
# other symptom was one line of readlink usage text on stderr.
#
# The loop below is the portable equivalent: plain `readlink` with no `-f`
# exists on BSD and GNU alike, and resolving one link at a time arrives at the
# same place. CDPATH is emptied because a user's CDPATH can make `cd` print a
# directory and jump somewhere else entirely. The hop limit stands in for the
# ELOOP that `readlink -f` would have raised on a circular link; failing loudly
# there is the opposite of the bug being fixed, and deliberately so.
resolve_script_dir() {
    local src="$1" dir hops=0
    while [ -L "$src" ]; do
        # An assignment, not `(( hops++ ))` -- an arithmetic command whose
        # result is 0 returns status 1, which under `set -e` would end the
        # script on the first hop.
        hops=$((hops + 1))
        if [ "$hops" -gt 40 ]; then
            echo "operator.sh: too many symbolic links resolving $1" >&2
            return 1
        fi
        dir="$(CDPATH="" cd "$(dirname "$src")" && pwd)"
        src="$(readlink "$src")"
        case "$src" in
            /*) ;;
            *) src="$dir/$src" ;;
        esac
    done
    CDPATH="" cd "$(dirname "$src")" && pwd
}
SCRIPT_DIR="$(resolve_script_dir "${BASH_SOURCE[0]}")"

# ── Constants ───────────────────────────────────────────────────
# All operator state lives in ~/.operator/ (NOT ~/.copilot/).
# The copilot CLI itself wholesale-deletes ~/.copilot/restart/ on every
# startup (confirmed via fatrace: copilot's MainThread does
# open/readdir/unlink/rmdir of that path within ~3s of launch). To avoid
# the name collision, and to keep operator state cleanly separated from
# copilot's own state, everything operator-related now lives outside
# ~/.copilot.
OPERATOR_HOME="${COPILOT_OPERATOR_HOME:-${HOME}/.operator}"
RESTART_DIR="${OPERATOR_HOME}/restart"
LOG_FILE="${OPERATOR_HOME}/operator.log"
METRICS_DB="${OPERATOR_HOME}/metrics.db"
COPILOT_LOG_DIR="${HOME}/.copilot/logs"

# Legacy paths (for one-time migration). Anything found here on startup gets
# moved into the new locations under $OPERATOR_HOME.
LEGACY_RESTART_DIR="${HOME}/.copilot/restart"
LEGACY_LOG_FILE="${HOME}/.copilot/operator.log"
LEGACY_METRICS_DB="${HOME}/.copilot/operator-metrics.db"
LEGACY_BACKUPS_DIR="${HOME}/.copilot/operator-backups"
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
CURRENT_COPILOT_SESSION_ID=""
COPILOT_LAUNCH_STARTED_MS=""

# ── Logging ─────────────────────────────────────────────────────
mkdir -p "$OPERATOR_HOME" "$(dirname "$LOG_FILE")"

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

# Windows Terminal and ConEmu draw OSC 9;4 as a progress ring on the tab
# itself. State 3 is an animated indeterminate ring, which is as close to an
# animated tab icon as a terminal gets: custom icons are static images.
# States: 0 clear, 1 steady, 2 error, 3 animated (loop), 4 waiting.
set_tab_progress() {
    [[ -n "${OPERATOR_NO_TAB_PROGRESS:-}" ]] && return 0
    [[ -t 1 ]] || return 0
    local seq
    printf -v seq '\033]9;4;%s;%s\007' "$1" "${2:-100}"
    if [[ -n "${TMUX:-}" ]]; then
        # tmux drops sequences it does not implement unless they are wrapped
        # in its DCS passthrough (and allow-passthrough is on).
        printf '\033Ptmux;%s\033\\' "${seq//$'\033'/$'\033\033'}"
    else
        printf '%s' "$seq"
    fi
}

clear_tab_progress() {
    set_tab_progress 0 0
}
trap clear_tab_progress EXIT

derive_instance_paths() {
    local name="$1"
    INSTANCE_NAME="$name"
    TMUX_SESSION="$name"
    RESTART_MARKER="${RESTART_DIR}/${name}"
    STATE_FILE="${RESTART_DIR}/${name}.state"
    RUN_SCRIPT="${OPERATOR_HOME}/run-${name}.sh"
    mkdir -p "$RESTART_DIR"
}

# One-time migration: move operator state out of ~/.copilot/ (where it
# collides with the copilot CLI) into ~/.operator/. Safe to call repeatedly
# — only acts on legacy paths that still exist. The legacy ~/.copilot/restart
# directory itself gets nuked by copilot on its next startup, so we don't
# bother to rmdir it.
migrate_legacy_state() {
    local moved=0 f base

    # Ensure target directories exist before we try to mv into them.
    mkdir -p "$RESTART_DIR" "$OPERATOR_HOME"

    # 1. Restart markers / state files
    if [[ -d "$LEGACY_RESTART_DIR" && "$LEGACY_RESTART_DIR" != "$RESTART_DIR" ]]; then
        shopt -s nullglob
        for f in "$LEGACY_RESTART_DIR"/*.state "$LEGACY_RESTART_DIR"/*.managed "$LEGACY_RESTART_DIR"/*; do
            [[ -e "$f" ]] || continue
            base="$(basename "$f")"
            if [[ ! -e "${RESTART_DIR}/${base}" ]]; then
                mv "$f" "${RESTART_DIR}/${base}" 2>/dev/null && (( moved++ )) || true
            fi
        done
        shopt -u nullglob
    fi

    # 2. Per-instance run scripts (~/.copilot/operator-run-*.sh -> ~/.operator/run-*.sh)
    shopt -s nullglob
    for f in "${HOME}/.copilot/operator-run-"*.sh; do
        [[ -e "$f" ]] || continue
        base="$(basename "$f")"
        base="${base#operator-run-}"
        if [[ ! -e "${OPERATOR_HOME}/run-${base}" ]]; then
            mv "$f" "${OPERATOR_HOME}/run-${base}" 2>/dev/null && (( moved++ )) || true
        fi
    done
    shopt -u nullglob

    # 3. Metrics DB
    if [[ -f "$LEGACY_METRICS_DB" && ! -e "$METRICS_DB" ]]; then
        mv "$LEGACY_METRICS_DB" "$METRICS_DB" 2>/dev/null && (( moved++ )) || true
    fi

    # 4. Operator log
    if [[ -f "$LEGACY_LOG_FILE" && ! -e "$LOG_FILE" ]]; then
        mv "$LEGACY_LOG_FILE" "$LOG_FILE" 2>/dev/null && (( moved++ )) || true
    fi

    # 5. Backups dir
    if [[ -d "$LEGACY_BACKUPS_DIR" && ! -e "${OPERATOR_HOME}/backups" ]]; then
        mv "$LEGACY_BACKUPS_DIR" "${OPERATOR_HOME}/backups" 2>/dev/null && (( moved++ )) || true
    fi

    if (( moved > 0 )); then
        log "Migrated $moved legacy operator artifact(s) from ~/.copilot/ to $OPERATOR_HOME"
    fi

    # Auto-heal: any live tmux session that has a matching run-<name>.sh
    # script but no .managed marker (because the bug destroyed it earlier)
    # gets its marker recreated. Only restores markers we know are operator-
    # owned (run script must exist).
    if command -v tmux >/dev/null 2>&1; then
        local healed=0 sess
        while IFS= read -r sess; do
            [[ -z "$sess" ]] && continue
            if [[ -f "${OPERATOR_HOME}/run-${sess}.sh" && ! -e "${RESTART_DIR}/${sess}.managed" ]]; then
                touch "${RESTART_DIR}/${sess}.managed"
                (( healed++ )) || true
            fi
        done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null || true)
        if (( healed > 0 )); then
            log "Auto-healed $healed .managed marker(s) for live tmux session(s)"
        fi
    fi
}

save_instance_state() {
    [[ -z "$STATE_FILE" ]] && return
    {
        printf 'SESSION_NUM=%d\nRUN_STARTED=%s\n' "$CURRENT_SESSION_NUM" "$OPERATOR_RUN_STARTED"
        if [[ -n "$CURRENT_COPILOT_SESSION_ID" ]]; then
            printf 'COPILOT_SESSION_ID=%s\n' "$CURRENT_COPILOT_SESSION_ID"
        fi
    } > "$STATE_FILE"
}

load_instance_state() {
    [[ -z "$STATE_FILE" || ! -f "$STATE_FILE" ]] && return 1
    local line
    while IFS='=' read -r key val; do
        case "$key" in
            SESSION_NUM)  CURRENT_SESSION_NUM="$val" ;;
            RUN_STARTED)  OPERATOR_RUN_STARTED="$val" ;;
            COPILOT_SESSION_ID) CURRENT_COPILOT_SESSION_ID="$val" ;;
        esac
    done < "$STATE_FILE"
    return 0
}

# ── Sets, without associative arrays ────────────────────────────
#
# `/bin/bash` on macOS is 3.2 and always will be — Apple froze it at the last
# GPLv2 release — and bash 3.2 has no associative arrays at all. `local -A x`
# there is not a subtly different array, it is `declare: -A: invalid option`,
# and under this script's `set -e` that ends the run. Three places wanted a
# set, all of them for the same question: is this name one of the ones I
# collected?
#
# So membership is an exact scan of an indexed array, which is the one kind of
# array bash 3.2 does have. The obvious cheaper encoding — join the names with
# newlines and ask whether the string contains one — is what this used first,
# and it is wrong: `for f in "$RESTART_DIR"/*.managed` yields whatever
# filenames exist, and a filename may contain a newline. `operator --name` with
# a newline in it creates the marker with `touch` before tmux rejects the name,
# so the poisoned marker outlives the failed launch; one such marker splits
# into two logical members, and `stop_operator` passes every name it believes
# is a member to `tmux kill-session`. An exact comparison has no encoding to
# corrupt, and n here is the number of tmux sessions on the box.
#
# Usage: in_list "$needle" ${arr[@]+"${arr[@]}"}
in_list() {
    local needle="$1"
    shift
    local item
    for item in "$@"; do
        # `"$needle"` is quoted, so a name containing `*` or `?` is compared
        # literally instead of being used as a pattern against its neighbours.
        [[ "$item" == "$needle" ]] && return 0
    done
    return 1
}

list_instances() {
    echo "═══ Running Operator Instances ═══"
    echo
    local found=false
    # Collect names of sessions managed by operator (have a .managed or .state marker)
    local managed_sessions=()
    for f in "${RESTART_DIR}"/*.state "${RESTART_DIR}"/*.managed; do
        [[ -e "$f" ]] || continue
        local base
        base=$(basename "$f")
        base="${base%.state}"
        base="${base%.managed}"
        managed_sessions+=("$base")
    done
    while IFS= read -r line; do
        local name
        name=$(echo "$line" | cut -d: -f1)
        if in_list "$name" ${managed_sessions[@]+"${managed_sessions[@]}"}; then
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

is_uuid() {
    [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]
}

extract_copilot_session_id_from_log() {
    local logfile="$1"
    [[ -n "$logfile" && -f "$logfile" ]] || return 1

    local session_id
    session_id=$(grep -Eo '"session_id"[[:space:]]*:[[:space:]]*"[0-9a-fA-F-]{36}"' "$logfile" 2>/dev/null \
        | head -1 \
        | sed -E 's/.*"([0-9a-fA-F-]{36})".*/\1/' || true)
    if is_uuid "$session_id"; then
        echo "$session_id"
        return 0
    fi

    session_id=$(grep -Eo 'Workspace initialized: [0-9a-fA-F-]{36}' "$logfile" 2>/dev/null \
        | head -1 \
        | awk '{print $3}' || true)
    if is_uuid "$session_id"; then
        echo "$session_id"
        return 0
    fi

    return 1
}

find_copilot_log_for_current_launch() {
    [[ -n "$COPILOT_PANE_PID" && -n "$COPILOT_LAUNCH_STARTED_MS" ]] || return 1

    local logfile base started_ms newest="" newest_ms=0
    shopt -s nullglob
    for logfile in "${COPILOT_LOG_DIR}"/process-*-"${COPILOT_PANE_PID}".log; do
        base=$(basename "$logfile")
        started_ms="${base#process-}"
        started_ms="${started_ms%-${COPILOT_PANE_PID}.log}"
        if [[ "$started_ms" =~ ^[0-9]+$ ]] && (( started_ms >= COPILOT_LAUNCH_STARTED_MS && started_ms > newest_ms )); then
            newest="$logfile"
            newest_ms="$started_ms"
        fi
    done
    shopt -u nullglob

    [[ -n "$newest" ]] || return 1
    echo "$newest"
}

remember_current_copilot_session_id() {
    local timeout="${1:-15}"
    local elapsed=0 logfile session_id

    if [[ -z "$COPILOT_PANE_PID" ]]; then
        log "  Warning: cannot determine Copilot CLI session id without a pane pid"
        return 1
    fi

    while (( elapsed < timeout )); do
        logfile=$(find_copilot_log_for_current_launch || true)
        if session_id=$(extract_copilot_session_id_from_log "$logfile"); then
            CURRENT_COPILOT_SESSION_ID="$session_id"
            save_instance_state
            log "  Copilot CLI session id: $CURRENT_COPILOT_SESSION_ID"
            return 0
        fi
        sleep 1
        (( elapsed++ )) || true
    done

    log "  Warning: could not determine Copilot CLI session id (pid=${COPILOT_PANE_PID:-?})"
    return 1
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
                       COALESCE('+' || lines_added || ' -' || lines_removed, '—') AS changes,
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
                       COALESCE(SUM(api_time_seconds) || 's', '—') AS total_api_time
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
            COALESCE(SUM(api_time_seconds) || 's', '—') AS total_api_time,
            COALESCE(
                CASE
                    WHEN SUM(session_time_seconds) >= 3600 THEN
                        (SUM(session_time_seconds) / 3600) || 'h ' || ((SUM(session_time_seconds) % 3600) / 60) || 'm'
                    ELSE (SUM(session_time_seconds) / 60) || 'm ' || (SUM(session_time_seconds) % 60) || 's'
                END, '—') AS total_sess_time,
            COALESCE('+' || SUM(lines_added) || ' -' || SUM(lines_removed), '—') AS total_changes,
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

args_have_explicit_session() {
    local prev=""
    for arg in "$@"; do
        case "$arg" in
            --continue|--resume|--resume=*|--connect|--connect=*)
                return 0
                ;;
        esac
        if [[ "$prev" == "--resume" || "$prev" == "--connect" ]]; then
            return 0
        fi
        prev="$arg"
    done
    return 1
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
        Adds --autopilot --effort high --experimental automatically.
        When copilot exits, usage metrics are parsed from its process
        log and stored in the metrics database.

    Loop mode (--loop)
        Adds --yolo --autopilot --no-ask-user --effort high --experimental automatically.
        Sends a preamble for autonomous operation. Restarts copilot
        when the agent touches the instance-specific restart marker.
        Ctrl+C captures metrics and shows an aggregate run summary.

        Named instances auto-continue when restarted — session numbering,
        run summary scope, and the last Copilot CLI session ID carry over.
        If WSL crashes or Windows reboots, restarting the same loop resumes
        that CLI session with --resume=<session-id>. Use --fresh to reset.

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
    ~/.operator/                        Operator state directory (override
                                        with COPILOT_OPERATOR_HOME env var)
    ~/.operator/metrics.db              SQLite metrics database
    ~/.operator/operator.log            Operator log file
    ~/.operator/restart/                Per-instance restart marker files
    ~/.operator/run-<instance>.sh       Per-instance launch script
    ~/.operator/backups/                Backups of operator.sh
    operator-ingest.py                  Log parser (lives next to operator.sh)
    ~/.copilot/logs/process-*.log       Copilot process logs (source data)

DEPENDENCIES
    tmux, sqlite3, python3, copilot
HELP
    # Unquoted heredoc, so the word list is the same string the refusal in
    # main() consults. Written out rather than restated: a help text that
    # named these by hand is a second copy, and the whole reason the refusal
    # exists is that a word can be a real subcommand somewhere and silently
    # not one here.
    cat << HELP
ELSEWHERE
    These are subcommands of the Python operator (copilot_operator.py), not
    of this script. Asking for one here reports where it lives rather than
    starting a session:

        ${PYTHON_ONLY_SUBCOMMANDS}
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
        local managed_sessions=()
        for f in "${RESTART_DIR}"/*.managed "${RESTART_DIR}"/*.state; do
            [[ -e "$f" ]] || continue
            local base
            base=$(basename "$f")
            base="${base%.managed}"
            base="${base%.state}"
            managed_sessions+=("$base")
        done
        while IFS= read -r name; do
            if in_list "$name" ${managed_sessions[@]+"${managed_sessions[@]}"}; then
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
        # Guarded like the loop path above, though every caller today builds
        # this array from a non-empty defaults list. That non-emptiness is a
        # fact about those lists, not an invariant of this function -- and the
        # single-session list was shortened from six elements to four in
        # cb10f72, by the same hand that had just verified these expansions as
        # safe, without the coupling being visible from either end. This is the
        # innermost consumer, so it inherits emptiness from every caller,
        # including ones not written yet.
        for arg in ${copilot_args[@]+"${copilot_args[@]}"}; do
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

    # Snapshot the names of all sibling .managed markers BEFORE we launch
    # copilot. If the upcoming launch causes the wholesale dir-vanish (see
    # comment at recovery block below), this lets us restore exactly the
    # markers that existed — no more, no less. This is more reliable than
    # inferring liveness from leftover operator-run-*.sh files (which
    # `operator stop` does not delete).
    #
    # Known tradeoff: markers created by OTHER operators during our ~3s
    # launch window are not in this snapshot. If such a marker is lost to
    # the vanish, that operator's next natural file-write (within seconds)
    # will restore it. We deliberately do NOT fall back to bare
    # `tmux list-sessions` to populate the snapshot — any tmux session with
    # a colliding name would get a bogus .managed marker written.
    local -a PRE_LAUNCH_MARKERS=()
    if [[ -d "$RESTART_DIR" ]]; then
        local _f
        for _f in "$RESTART_DIR"/*.managed; do
            [[ -e "$_f" ]] || continue
            local _base
            _base=$(basename "$_f")
            _base="${_base%.managed}"
            [[ "$_base" == "$TMUX_SESSION" ]] && continue
            PRE_LAUNCH_MARKERS+=("$_base")
        done
    fi

    tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true

    COPILOT_LAUNCH_STARTED_MS=$(date +%s%3N)
    tmux new-session -d -s "$TMUX_SESSION" -c "$(pwd)" "$RUN_SCRIPT"
    tmux set-option -t "$TMUX_SESSION" remain-on-exit "$remain_on_exit"

    # Record the pane PID so we can find the right process log later
    COPILOT_PANE_PID=$(tmux display-message -t "$TMUX_SESSION" -p '#{pane_pid}' 2>/dev/null || echo "")

    log "  Waiting for copilot to start..."
    sleep 3

    log "  Session #${session_num} running (pid=$COPILOT_PANE_PID) — attach with: tmux attach -t $TMUX_SESSION"
    if [[ "$IS_LOOP_MODE" == true ]]; then
        remember_current_copilot_session_id 15 || true
    fi

    # Mark this session as operator-managed so list_instances can find it.
    #
    # Historical note: prior to moving RESTART_DIR out of ~/.copilot, the
    # copilot CLI itself would wholesale-delete ~/.copilot/restart on every
    # startup (confirmed via fatrace — copilot's MainThread does
    # open/readdir/unlink/rmdir within ~3s of launch). The path move
    # eliminates that collision and the snapshot/restore below should now
    # be a no-op in practice.
    #
    # Belt-and-suspenders: if anything ever DOES nuke RESTART_DIR mid-launch
    # (a future tool, a misbehaving cleanup script, etc.), we restore the
    # .managed markers we snapshotted BEFORE launch (PRE_LAUNCH_MARKERS) so
    # `operator list` and pending handoffs survive. Each restored marker is
    # cross-checked against a live tmux session — if the named session is no
    # longer running, we don't restore it (it was legitimately stopped
    # during our launch window).
    if [[ ! -d "$RESTART_DIR" ]]; then
        log "WARN: $RESTART_DIR vanished between startup and session launch — unexpected after the ~/.operator/ move. Recreating + restoring ${#PRE_LAUNCH_MARKERS[@]} sibling marker(s). If this fires repeatedly, run:"
        log "     ${SCRIPT_DIR:-<your copilot-tools checkout>}/diagnose-restart-deleter.sh"
        mkdir -p "$RESTART_DIR"
        if (( ${#PRE_LAUNCH_MARKERS[@]} > 0 )); then
            local _live_sessions=()
            local _s
            while IFS= read -r _s; do
                [[ -n "$_s" ]] && _live_sessions+=("$_s")
            done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null)
            local _name
            for _name in ${PRE_LAUNCH_MARKERS[@]+"${PRE_LAUNCH_MARKERS[@]}"}; do
                if in_list "$_name" ${_live_sessions[@]+"${_live_sessions[@]}"}; then
                    touch "${RESTART_DIR}/${_name}.managed"
                    log "  Restored marker for live instance: $_name"
                else
                    log "  Skipped marker for $_name (tmux session no longer exists)"
                fi
            done
        fi
    fi
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
    # `--experimental` always, and always ahead of "$@": runtime extensions
    # load only in experimental mode, and the flag is sticky global state in
    # ~/.copilot/settings.json that any other session can flip. The CLI
    # resolves conflicting spellings last-wins, so a user's own
    # `--no-experimental` still beats this. See with_experimental() in
    # copilot_operator.py for the full reasoning.
    local copilot_args=("--autopilot" "--effort" "high" "--experimental")
    copilot_args+=("$@")

    handle_existing_session

    init_metrics_db
    OPERATOR_RUN_STARTED=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

    log "Starting single session: $INSTANCE_NAME"

    SCRIPT_PREAMBLE=""
    generate_run_script ${copilot_args[@]+"${copilot_args[@]}"}
    start_copilot_in_tmux 1 off

    set_tab_title "operator - $INSTANCE_NAME"
    set_tab_progress 1
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

    local copilot_args=("--yolo" "--autopilot" "--no-ask-user" "--effort" "high" "--experimental")

    # `${a[@]+"${a[@]}"}` everywhere `user_args` is expanded, not `"${a[@]}"`.
    # macOS ships bash 3.2, where an empty array is *unset* rather than
    # set-and-empty, so `"${user_args[@]}"` under the `set -u` on line 28 is an
    # unbound-variable error rather than zero words. `operator --loop` with no
    # arguments of its own is the plainest way to reach that, and it died here
    # before touching anything. bash 4.4 and later have no such quirk, which is
    # why this only ever showed up on macOS.
    local agent_name
    agent_name=$(extract_agent_from_args ${user_args[@]+"${user_args[@]}"})
    local has_agent=false
    for arg in ${user_args[@]+"${user_args[@]}"}; do
        [[ "$arg" == "--agent" || "$arg" == --agent=* ]] && has_agent=true
    done
    if [[ "$has_agent" == false ]]; then
        copilot_args+=("--agent" "$agent_name")
    fi

    copilot_args+=(${user_args[@]+"${user_args[@]}"})

    SCRIPT_PREAMBLE=$(build_preamble "$agent_name")

    init_metrics_db

    # Auto-continue: load prior state for named instances
    local start_session=1
    local resume_session_id=""
    if [[ "$IS_FRESH" == false ]] && load_instance_state; then
        start_session=$(( CURRENT_SESSION_NUM + 1 ))
        log "Continuing from session #${start_session} (run started ${OPERATOR_RUN_STARTED})"
        if is_uuid "$CURRENT_COPILOT_SESSION_ID"; then
            resume_session_id="$CURRENT_COPILOT_SESSION_ID"
            log "  Will resume Copilot CLI session after operator restart: $resume_session_id"
        else
            CURRENT_COPILOT_SESSION_ID=""
        fi
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
    set_tab_progress 3

    for session_num in $(seq "$start_session" "$MAX_SESSIONS"); do
        CURRENT_SESSION_NUM=$session_num
        save_instance_state

        local launch_args=(${copilot_args[@]+"${copilot_args[@]}"})
        if [[ -n "$resume_session_id" ]]; then
            if args_have_explicit_session ${launch_args[@]+"${launch_args[@]}"}; then
                log "  Skipping automatic --resume; user args already choose a session"
            else
                launch_args+=("--resume=$resume_session_id")
            fi
            resume_session_id=""
        fi

        generate_run_script ${launch_args[@]+"${launch_args[@]}"}
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
            CURRENT_COPILOT_SESSION_ID=""
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

# Every word `main`'s `case` below answers itself.
#
# One list, read by both `is_reserved_word` and the typo guard, because the
# Python operator learned what two copies cost: its hand-maintained second
# copy had already drifted -- `send` and `inbox` were dispatched and missing
# from the set that decides what is not an instance name. Nothing broke, which
# is what let the omission sit there.
# `tests/test_operator_sh_typo_guard.py` checks this against the `case` arms
# so a subcommand added to one and not the other fails rather than drifts.
#
# Deliberately only what *this script* implements, not what the Python
# operator does. `send`, `inbox` and `restart-loop` have no arm here, so
# suggesting one of them would answer a typing mistake by naming a word that
# operator.sh also handles by starting a session -- the very behaviour this
# guard exists to stop, arriving via the fix for it.
SUBCOMMANDS="stop list report ingest help join reload"
RESERVED_WORDS="$SUBCOMMANDS"

#: Words the Python operator dispatches and this script does not.
#
# Backlog 9. These are not typos and the guard above cannot reach them: they
# are spelled correctly, for the *other* program that answers the word
# `operator`. Without an arm here they matched no `case`, named no running
# session, and reached the launch path -- so `operator inbox copilot-tools`
# on Linux or macOS started a copilot session named `inbox` and passed
# `copilot-tools` to it as a prompt. It did not read a mailbox and it did not
# report an error, which is the failure direction that costs the most: this
# repository's own conventions tell every agent to run `operator inbox` at the
# start of work, so the mailbox stays unread while looking exactly like an
# empty one.
#
# Refusing rather than forwarding. `exec python3 copilot_operator.py "$@"`
# would reach parity for free and is deliberately not done: it makes this
# script a proxy for the other one, which is the product decision backlog 9
# says should be made deliberately rather than by accretion, and it fails
# confusingly wherever `copilot_operator.py` is absent or its dependencies are
# not installed. A refusal that names the entry point turns a silent wrong
# action into a recoverable one and commits to nothing.
#
# Hand-transcribed from `copilot_operator.py`'s SUBCOMMANDS, which is exactly
# the arrangement that let that file's own second copy drift -- so
# `tests/test_operator_sh_typo_guard.py` derives this set as
# `python SUBCOMMANDS - shell SUBCOMMANDS` and fails when the two disagree. A
# subcommand added to the Python operator is then a red test here rather than
# a word that silently starts a session again.
PYTHON_ONLY_SUBCOMMANDS="version menu projects stop-loop restart-loop stop-session forget send reply inbox session work backlog worktree ownership logs trace tabs restore conversations"

#: Words that mean a subcommand here but are spelled for a different tool.
#
# Not typos, and no edit distance reaches them: somebody typing `ls` has
# spelled what they meant correctly, in the wrong language. That makes this
# the one part of the guard that must be enumerated by hand, and it is kept
# short -- an alias is a guess about intent, and a wrong guess sends the
# reader somewhere that does not do what they asked.
#
# Two rules for adding one, both learned from entries the Python operator
# removed after review:
#
# - The target must *do the thing the other tool's word names*. `cat` and
#   `tail` pointed at `logs`, which reports sizes and prunes old files and
#   cannot display a log at all. An alias that misses by that much is worse
#   than no suggestion, because it is confident.
# - The target must not be *more destructive than the word*. `quit` and `exit`
#   pointed at `stop`, and bare `operator stop` stops every managed instance.
#   Somebody typing `quit` means "let me out of this one".
#
# Two parallel indexed arrays rather than one associative array, for the bash
# 3.2 reason documented on `in_list`: `local -A` is a syntax error on the bash
# macOS ships. Index i of one lines up with index i of the other.
SUBCOMMAND_ALIAS_WORDS=(ls ll ps dir sessions status kill)
SUBCOMMAND_ALIAS_TARGETS=(list list list list list list stop)

#: Shortest prefix that may stand in for a subcommand.
#
# Two would refuse `ls`, `re` and `in` -- `operator [copilot-args...]` is
# documented, so each of those is a working invocation taken away to guess at
# a typo nobody made. Three costs nothing: the two-letter mistake this guard
# exists for, `ls`, is not a prefix of `list` at all and is caught by the
# alias table above.
MIN_PREFIX_LENGTH=3

# True when at most one insertion, deletion, substitution or transposition of
# adjacent characters turns $1 into $2 -- Damerau-Levenshtein, threshold 1.
#
# Damerau rather than plain Levenshtein because a transposition is one slip of
# the fingers and two ordinary edits: `jion`, `sedn` and `verison` are each one
# flipped pair from a real word and would otherwise need a threshold of two,
# which is wide enough to swallow real words (`test` is two edits from `list`).
#
# The matrix is one flat indexed array addressed as d[i * width + j]. bash 3.2
# has no associative arrays and no two-dimensional ones at any version, and the
# operands here are single subcommands, so the allocation is trivially small.
one_edit_apart() {
    local word="$1" candidate="$2"
    local n=${#word} m=${#candidate}
    local gap=$(( n > m ? n - m : m - n ))
    # One edit changes the length by at most one, so this is not merely a
    # shortcut: it is what stops a long instance name being refused because of
    # a short subcommand.
    if [ "$gap" -gt 1 ]; then
        return 1
    fi
    local width=$(( m + 1 ))
    local -a d=()
    local i j cost candidate_prev
    for (( i = 0; i <= n; i++ )); do
        d[$(( i * width ))]=$i
    done
    for (( j = 0; j <= m; j++ )); do
        d[$j]=$j
    done
    for (( i = 1; i <= n; i++ )); do
        for (( j = 1; j <= m; j++ )); do
            cost=1
            if [ "${word:i-1:1}" = "${candidate:j-1:1}" ]; then
                cost=0
            fi
            # Deletion, insertion, substitution. Assignments rather than
            # `(( ... ))` statements throughout: an arithmetic command whose
            # value is 0 returns 1, which under `set -e` would end the run the
            # first time two prefixes matched exactly.
            d[$(( i * width + j ))]=$(( d[(i - 1) * width + j] + 1 ))
            if [ "$(( d[i * width + j - 1] + 1 ))" -lt "${d[$(( i * width + j ))]}" ]; then
                d[$(( i * width + j ))]=$(( d[i * width + j - 1] + 1 ))
            fi
            if [ "$(( d[(i - 1) * width + j - 1] + cost ))" -lt "${d[$(( i * width + j ))]}" ]; then
                d[$(( i * width + j ))]=$(( d[(i - 1) * width + j - 1] + cost ))
            fi
            # Transposition of the adjacent pair ending at i, j.
            if [ "$i" -gt 1 ] && [ "$j" -gt 1 ]; then
                candidate_prev="${candidate:j-2:1}"
                if [ "${word:i-1:1}" = "$candidate_prev" ] \
                   && [ "${word:i-2:1}" = "${candidate:j-1:1}" ] \
                   && [ "$(( d[(i - 2) * width + j - 2] + cost ))" -lt "${d[$(( i * width + j ))]}" ]; then
                    d[$(( i * width + j ))]=$(( d[(i - 2) * width + j - 2] + cost ))
                fi
            fi
        done
    done
    [ "${d[$(( n * width + m ))]}" -le 1 ]
}

# The subcommands $1 is plausibly a mistyping of, one per line, in declared
# order. Prints nothing when it resembles none of them.
#
# Three rules, and the narrowness of all three is the point. This predicate
# decides whether a word is refused instead of being handed to copilot as a
# prompt, and `operator [copilot-args...]` is documented -- so every word it
# claims wrongly is a working invocation taken away.
#
# - A prefix of at least MIN_PREFIX_LENGTH characters. `sto` and `rep` are
#   truncations of something real. A prefix is never longer than what it
#   prefixes, which is what keeps instance names safe.
# - One edit away, the classic single-slip model, which cannot span a length
#   gap of two and so protects longer names for the same reason.
# - A word from another tool, consulted only when the first two found nothing.
#
# Together those two length properties are the whole safety argument for
# project names: *a word two or more characters longer than every subcommand
# can never be refused*. Almost every real instance name has that shape.
#
# The Python operator's first attempt scored `difflib` similarity ratios at a
# 0.6 cutoff and three reviewers rejected it independently: a ratio counts
# shared characters in any order, so it refused `refactor`, `read`, `hello`,
# `test` and -- worst -- `myproject`, the documented quick-join. Ten of thirty
# ordinary one-word prompts. These rules refuse two, `lint` and `end`, each
# genuinely one keystroke from a subcommand and both recoverable via the
# escape hatch the message names.
#
# Every match is printed rather than one winner. `sto` truncates nothing else
# here today, but a tie-break by taste is how `ls` would come to be answered
# with `logs`, and there is no honest way to choose between equally good
# answers.
subcommand_suggestions() {
    local word
    word="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
    if [ -z "$word" ]; then
        return 0
    fi
    local -a matches=()
    local candidate
    for candidate in $SUBCOMMANDS; do
        # `"$word"*` -- the variable is quoted so a name containing `*` or `?`
        # is compared literally rather than used as a pattern against the
        # subcommand it is being tested against.
        if [ "${#word}" -ge "$MIN_PREFIX_LENGTH" ] && [[ "$candidate" == "$word"* ]]; then
            matches+=("$candidate")
        elif one_edit_apart "$word" "$candidate"; then
            matches+=("$candidate")
        fi
    done
    if [ "${#matches[@]}" -eq 0 ]; then
        local i=0
        for candidate in ${SUBCOMMAND_ALIAS_WORDS[@]+"${SUBCOMMAND_ALIAS_WORDS[@]}"}; do
            if [ "$candidate" = "$word" ]; then
                matches+=("${SUBCOMMAND_ALIAS_TARGETS[$i]}")
                break
            fi
            i=$(( i + 1 ))
        done
    fi
    # `printf '%s\n'` with no operands still prints one empty line, which the
    # caller would read back as a suggestion of "".
    if [ "${#matches[@]}" -gt 0 ]; then
        printf '%s\n' ${matches[@]+"${matches[@]}"}
    fi
}

join_instance() {
    local target="${1:-}"
    if [[ -z "$target" ]]; then
        list_instances
        exit 0
    fi
    target="$(sanitize_session_name "$target")"
    if tmux has-session -t "$target" 2>/dev/null; then
        set_tab_title "terminal - $target"
        set_tab_progress 1
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
    migrate_legacy_state
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
            set_tab_progress 1
            exec tmux attach -t "$1"
        fi
    fi

    # Everything above has answered, so the first argument is not a subcommand
    # and does not name a running instance. The rest of main() would read it as
    # a copilot argument and start a session named after the current directory
    # -- so `operator ls` does not report that `ls` is spelled `list`, it
    # offers to restart whatever is running here, and where nothing is running
    # it starts a real session with `ls` as its prompt.
    #
    # An unknown subcommand is a typing mistake, and the answer to a typing
    # mistake is a message rather than a state change. Only a first argument
    # close to a real subcommand -- or spelled correctly for the Python
    # operator, which is backlog 9 and the first branch below -- is refused:
    # `operator [copilot-args...]` is documented, so an unrecognisable word is
    # still passed through as a prompt.
    #
    # Ahead of the tmux/sqlite3/python3 checks below on purpose. A typo is
    # answerable without any of them, and reporting a missing dependency to
    # somebody who typed `operator ls` names neither the thing they got wrong
    # nor the thing they wanted.
    #
    # Not gated on `$# -eq 1`, unlike the shortcut above: `operator ls -la` is
    # the same mistake as `operator ls`.
    #
    # `is_reserved_word` is re-checked rather than assumed. Every arm of the
    # `case` above happens to exit today -- `stop` exits inside
    # `stop_operator`, the rest exit at the arm -- so a real subcommand cannot
    # reach here. That is a fact about those seven handlers, not a property of
    # the dispatch, and the failure it would produce if one ever returned is
    # this guard refusing the exact command it had just run, with a "did you
    # mean" naming the word the user typed correctly.
    local first_arg="${1:-}"
    if [[ -n "$first_arg" && "$first_arg" != -* ]] && ! is_reserved_word "$first_arg"; then
        # Backlog 9. Checked before the "did you mean" rules, and by exact
        # match rather than by distance: a word this script does not implement
        # but the Python operator does is not a guess, it is a fact, and an
        # answer that names the entry point beats one that suggests the
        # nearest word spelled differently. No word is in both lists today --
        # `test_the_two_refusals_cannot_both_claim_a_word` is what keeps that
        # true -- so the order is a statement of precedence rather than a
        # behaviour anything currently depends on.
        if in_list "$first_arg" $PYTHON_ONLY_SUBCOMMANDS; then
            echo "operator.sh does not implement \`$first_arg\`;" \
                 "the Python operator does." >&2
            # Named only when it is actually there. operator.sh is often
            # installed on its own, and a command line quoting a path that
            # does not exist is worse than one that names the file to go and
            # find.
            if [[ -f "${SCRIPT_DIR:-}/copilot_operator.py" ]]; then
                echo "Run: python3 \"${SCRIPT_DIR}/copilot_operator.py\"" \
                     "$first_arg [arguments...]" >&2
            else
                echo "Run it with the Python operator (\`copilot_operator.py\`)" \
                     "instead of this script." >&2
            fi
            echo "(To pass it to copilot instead, name the instance:" \
                 "\`operator --name NAME $first_arg\`.)" >&2
            exit 1
        fi
        local suggestions
        # Word-split on purpose. Every subcommand is a single word of lowercase
        # letters, so there is nothing here to split wrongly or to glob.
        suggestions="$(subcommand_suggestions "$first_arg")"
        if [[ -n "$suggestions" ]]; then
            local names="" suggestion
            for suggestion in $suggestions; do
                [[ -n "$names" ]] && names="$names or "
                names="$names\`operator $suggestion\`"
            done
            echo "Unknown subcommand: operator $first_arg" >&2
            echo "Did you mean $names?" >&2
            echo "(To pass it to copilot instead, name the instance:" \
                 "\`operator --name NAME $first_arg\`.)" >&2
            exit 1
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

    # Guarded expansion, for the bash 3.2 reason documented in run_loop_mode().
    # `copilot_args` above starts empty and stays empty for a bare `operator`
    # or `operator --loop`, so on stock macOS bash both of these died here
    # before the session ever started.
    if [[ "$loop_mode" == true ]]; then
        run_loop_mode ${copilot_args[@]+"${copilot_args[@]}"}
    else
        run_single_session ${copilot_args[@]+"${copilot_args[@]}"}
    fi
}

main "$@"
