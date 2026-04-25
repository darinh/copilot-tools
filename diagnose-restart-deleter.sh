#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# diagnose-restart-deleter.sh
#
# Captures the PID, executable, and command line of whatever process
# wholesale-deletes ~/.copilot/restart. Use this when operator.sh's
# WARN line "$RESTART_DIR vanished" is firing.
#
# Why this script exists:
#   inotifywait shows WHAT was deleted but not WHO deleted it.
#   On WSL2 the kernel audit subsystem isn't available, so we use
#   fanotify (via the `fatrace` tool) which works on WSL2 and reports
#   the offending PID and comm for every filesystem event.
#
# Usage:
#   sudo ./diagnose-restart-deleter.sh           # waits indefinitely
#   sudo ./diagnose-restart-deleter.sh --once    # exit after first capture
#
# Requires: sudo, fatrace (apt-get install fatrace)
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "This script must run as root (fatrace needs CAP_SYS_ADMIN)." >&2
    echo "Re-run: sudo $0 $*" >&2
    exit 1
fi

if ! command -v fatrace >/dev/null 2>&1; then
    echo "fatrace not installed. Install with: sudo apt-get install -y fatrace" >&2
    exit 1
fi

# Resolve the real user's home (we're running as root via sudo).
REAL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo "$USER")}"
if ! [[ "$REAL_USER" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
    echo "Refusing to run: SUDO_USER value '$REAL_USER' is not a safe username." >&2
    exit 1
fi
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
if [[ -z "$REAL_HOME" || ! -d "$REAL_HOME" ]]; then
    echo "Could not resolve a real home directory for $REAL_USER." >&2
    exit 1
fi
RESTART_DIR="${REAL_HOME}/.copilot/restart"
RESTART_PARENT="${REAL_HOME}/.copilot"
ONCE=false
[[ "${1:-}" == "--once" ]] && ONCE=true

# Refuse to operate if either path is a symlink — a malicious symlink
# could redirect chown / mkdir at arbitrary system locations.
for _p in "$RESTART_PARENT" "$RESTART_DIR"; do
    if [[ -L "$_p" ]]; then
        echo "Refusing to run: $_p is a symlink. Resolve it first." >&2
        exit 1
    fi
done

# Confirm the parent is owned by REAL_USER before touching anything inside.
PARENT_OWNER=$(stat -c '%U' "$RESTART_PARENT" 2>/dev/null || echo "")
if [[ "$PARENT_OWNER" != "$REAL_USER" ]]; then
    echo "Refusing to run: $RESTART_PARENT is owned by '$PARENT_OWNER', expected '$REAL_USER'." >&2
    exit 1
fi

mkdir -p "$RESTART_DIR"
chown -h "$REAL_USER:$REAL_USER" "$RESTART_DIR"

LOG_FILE="$(mktemp -t fatrace-restart.XXXXXX.log)"
# Intentionally NOT chowning the log to REAL_USER — on WSL2 that caused
# the subsequent root-side redirect to fail with EACCES. The file stays
# root-owned; we'll print the path at the end so the user can sudo-cat it.
FATRACE_PID=""

cleanup() {
    if [[ -n "$FATRACE_PID" ]] && kill -0 "$FATRACE_PID" 2>/dev/null; then
        kill "$FATRACE_PID" 2>/dev/null || true
        wait "$FATRACE_PID" 2>/dev/null || true
    fi
    echo
    echo "Full fatrace log retained at: $LOG_FILE"
}
trap cleanup EXIT INT TERM

echo "Watching $RESTART_DIR via fatrace"
echo "Log file: $LOG_FILE"

# fatrace -c keeps comm names short; -t adds timestamps.
# It can't filter by path, so we capture everything and grep.
fatrace -t -c >"$LOG_FILE" 2>&1 &
FATRACE_PID=$!

# Give fatrace a moment to set up its fanotify mark.
sleep 1
if ! kill -0 "$FATRACE_PID" 2>/dev/null; then
    echo "fatrace died immediately. Output:" >&2
    cat "$LOG_FILE" >&2
    exit 1
fi

echo
echo "Waiting for the next vanish event…"
echo "  (Trigger one by running 'operator --loop' in another window and answering 'y',"
echo "   or just wait — your live operators restart on their own.)"
echo

REPORT_AND_RECREATE() {
    echo "════════════════════════════════════════════════════════════════"
    echo "VANISH DETECTED at $(date)"
    echo "════════════════════════════════════════════════════════════════"
    # Let fatrace flush.
    sleep 1
    echo
    echo "── fatrace events touching .copilot/restart (last 50) ──"
    grep -E '\.copilot(/restart)?(/|$| )' "$LOG_FILE" 2>/dev/null \
        | tail -50 || echo "(nothing matched yet — see full log)"
    echo
    echo "── Delete events specifically (D = delete) ──"
    # fatrace D events look like: 'comm(pid): D /full/path'
    grep -E '\): D[+ ]' "$LOG_FILE" 2>/dev/null \
        | grep -E '\.copilot(/restart)?' \
        | tail -20 || echo "(no delete events captured — try a longer wait)"
    echo
    echo "── Resolving exe paths for PIDs seen above ──"
    # Pull unique PIDs from the matching lines and resolve /proc/<pid>/exe
    # while we still can (some may have exited).
    pids=$(grep -E '\): [CDOWR][+ ]' "$LOG_FILE" 2>/dev/null \
        | grep -E '\.copilot(/restart)?' \
        | sed -nE 's/.*\(([0-9]+)\):.*/\1/p' \
        | sort -u | tail -20)
    if [[ -n "$pids" ]]; then
        for pid in $pids; do
            exe=$(readlink -f "/proc/$pid/exe" 2>/dev/null || echo "(gone)")
            comm=$(cat "/proc/$pid/comm" 2>/dev/null || echo "(gone)")
            cmdline=$(tr '\0' ' ' </proc/"$pid"/cmdline 2>/dev/null || echo "(gone)")
            ppid=$(awk '/^PPid:/ {print $2}' "/proc/$pid/status" 2>/dev/null || echo "?")
            echo "  pid=$pid ppid=$ppid comm=$comm"
            echo "    exe=$exe"
            echo "    cmd=$cmdline"
        done
    else
        echo "(no PIDs to resolve)"
    fi
    echo
    echo "── Process tree right now ──"
    ps -ef --forest 2>/dev/null | grep -E "copilot|operator|tmux|node|bash" | head -40
    echo "════════════════════════════════════════════════════════════════"

    # Re-validate paths/ownership before recreating.
    if [[ -L "$RESTART_PARENT" || -L "$RESTART_DIR" ]]; then
        echo "ABORT: path became a symlink during watch — not recreating." >&2
        exit 1
    fi
    _parent_owner_now=$(stat -c '%U' "$RESTART_PARENT" 2>/dev/null || echo "")
    if [[ "$_parent_owner_now" != "$REAL_USER" ]]; then
        echo "ABORT: $RESTART_PARENT ownership changed to '$_parent_owner_now' — not recreating." >&2
        exit 1
    fi
    mkdir -p "$RESTART_DIR"
    chown -h "$REAL_USER:$REAL_USER" "$RESTART_DIR"
}

while true; do
    sleep 2
    if [[ ! -d "$RESTART_DIR" ]]; then
        REPORT_AND_RECREATE
        if [[ "$ONCE" == true ]]; then
            echo "Captured. Exiting."
            exit 0
        fi
        echo "Continuing to watch (Ctrl+C to stop)…"
        echo
    fi
done
