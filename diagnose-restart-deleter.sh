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
#   The Linux audit subsystem (auditd) records the offending PID,
#   comm, and exe. This script wires up a temporary audit watch,
#   waits for the next deletion, captures the culprit, then cleans up.
#
# Usage:
#   sudo ./diagnose-restart-deleter.sh           # waits indefinitely
#   sudo ./diagnose-restart-deleter.sh --once    # exit after first capture
#
# Requires: sudo, auditctl, ausearch (apt-get install auditd)
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "This script must run as root (auditctl requires CAP_AUDIT_CONTROL)." >&2
    echo "Re-run: sudo $0 $*" >&2
    exit 1
fi

if ! command -v auditctl >/dev/null 2>&1; then
    echo "auditctl not installed. Install with: sudo apt-get install -y auditd" >&2
    exit 1
fi

# Resolve the real user's home (we're running as root via sudo)
REAL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo "$USER")}"
# Validate REAL_USER to a safe charset before letting it flow into paths/chown
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
KEY="copilot_restart_kill_$$"
ONCE=false
[[ "${1:-}" == "--once" ]] && ONCE=true

# Refuse to operate if either path is a symlink — a malicious symlink could
# point chown / mkdir at arbitrary system locations.
for _p in "$RESTART_PARENT" "$RESTART_DIR"; do
    if [[ -L "$_p" ]]; then
        echo "Refusing to run: $_p is a symlink. Resolve it first." >&2
        exit 1
    fi
done

# Confirm the parent is owned by REAL_USER before touching anything inside it.
PARENT_OWNER=$(stat -c '%U' "$RESTART_PARENT" 2>/dev/null || echo "")
if [[ "$PARENT_OWNER" != "$REAL_USER" ]]; then
    echo "Refusing to run: $RESTART_PARENT is owned by '$PARENT_OWNER', expected '$REAL_USER'." >&2
    exit 1
fi

# Create the dir if missing, then chown without following symlinks.
mkdir -p "$RESTART_DIR"
chown -h "$REAL_USER:$REAL_USER" "$RESTART_DIR"

cleanup() {
    echo
    echo "Removing audit watches…"
    auditctl -d -w "$RESTART_DIR"  -p w -k "$KEY" 2>/dev/null || true
    auditctl -d -w "$RESTART_PARENT" -p w -k "$KEY" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Watching $RESTART_DIR (key=$KEY)"
auditctl -w "$RESTART_DIR"  -p w -k "$KEY"
auditctl -w "$RESTART_PARENT" -p w -k "$KEY"

LAST_TS_FILE="$(mktemp)"
date '+%H:%M:%S' > "$LAST_TS_FILE"

echo "Waiting for the next vanish event…"
echo "  (Trigger one by running 'operator --loop' in another window and answering 'y',"
echo "   or just wait — your live operators restart roughly every few minutes.)"
echo

while true; do
    sleep 2
    if [[ ! -d "$RESTART_DIR" ]]; then
        echo "════════════════════════════════════════════════════════════════"
        echo "VANISH DETECTED at $(date)"
        echo "════════════════════════════════════════════════════════════════"
        # Give audit a moment to flush
        sleep 1
        echo
        echo "── Audit records (last 30 seconds) ──"
        ausearch -k "$KEY" --start recent -i 2>/dev/null | tail -200 || true
        echo
        echo "── Specifically: rmdir / unlink / rename syscalls on the dir ──"
        ausearch -k "$KEY" --start recent -i 2>/dev/null \
            | grep -E "syscall=(rmdir|unlink|unlinkat|rename|renameat)" -B1 -A4 || true
        echo
        echo "── Process tree at this moment ──"
        ps -ef --forest 2>/dev/null | grep -E "copilot|operator|tmux|node|bash" | head -40
        echo "════════════════════════════════════════════════════════════════"
        # Recreate so subsequent operators don't get hosed.
        # Re-validate BOTH path components are not symlinks and that the
        # parent is still owned by REAL_USER before touching anything. A
        # symlink swap during the wait loop could otherwise misdirect
        # mkdir / chown as root.
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
        if [[ "$ONCE" == true ]]; then
            echo "Captured. Exiting."
            exit 0
        fi
        echo "Continuing to watch (Ctrl+C to stop)…"
        echo
    fi
done
