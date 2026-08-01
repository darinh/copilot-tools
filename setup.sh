#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# copilot-tools setup — migrates any legacy bash operator/handoff install
# to the cross-platform Python implementation, then hands off to
# setup_tools.py, which provisions everything else (prerequisites, Anvil,
# MCP servers, Spec Kit, extensions, templates).
#
# Usage: ./setup.sh [setup_tools.py args...]   e.g. ./setup.sh --yes
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_BIN="${HOME}/.local/bin"
LEGACY_BACKUP_SUFFIX=".copilot-tools-legacy-bak"
FOREIGN_BACKUP_SUFFIX=".copilot-tools-preexisting-bak"

if ! echo "$PATH" | tr ':' '\n' | grep -qx "$LOCAL_BIN"; then
    export PATH="${LOCAL_BIN}:${PATH}"
fi

# ── Helpers ─────────────────────────────────────────────────────
info()  { echo "  ✅ $*"; }
warn()  { echo "  ⚠️  $*"; }
err()   { echo "  ❌ $*" >&2; }


echo ""
echo "═══ Copilot Tools Setup ═══"
echo ""

# ── Step 1: Locate (or install) Python 3.10+ ────────────────────
# Needed both to hand off to setup_tools.py and to canonicalize paths below
# (avoids relying on GNU-only `readlink -f`, which macOS's BSD readlink lacks).
echo "Locating Python..."

find_python() {
    local candidate ver major minor
    for candidate in python3 python; do
        if command -v "$candidate" &>/dev/null; then
            ver=$("$candidate" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo "0.0")
            major="${ver%%.*}"; minor="${ver##*.}"
            if [[ "$major" -gt 3 || ( "$major" -eq 3 && "$minor" -ge 10 ) ]]; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

# Setup installs what is missing rather than handing the user a homework
# list, so a machine without a usable Python gets one here.
install_python() {
    local sudo_cmd=()
    if [[ "$(id -u)" -ne 0 ]] && command -v sudo &>/dev/null; then
        sudo_cmd=(sudo)
    fi
    if command -v brew &>/dev/null; then
        brew install python@3.12 || brew install python3 || return 1
    elif command -v apt-get &>/dev/null; then
        "${sudo_cmd[@]}" apt-get update || true
        DEBIAN_FRONTEND=noninteractive "${sudo_cmd[@]}" apt-get install -y python3 python3-pip python3-venv || return 1
    elif command -v dnf &>/dev/null; then
        "${sudo_cmd[@]}" dnf install -y python3 python3-pip || return 1
    elif command -v pacman &>/dev/null; then
        "${sudo_cmd[@]}" pacman -S --noconfirm python python-pip || return 1
    elif command -v zypper &>/dev/null; then
        "${sudo_cmd[@]}" zypper install -y python3 python3-pip || return 1
    elif command -v apk &>/dev/null; then
        "${sudo_cmd[@]}" apk add python3 py3-pip || return 1
    else
        return 1
    fi
}

PYTHON_BIN="$(find_python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
    warn "Python 3.10+ not found — installing it..."
    if install_python; then
        PYTHON_BIN="$(find_python || true)"
    fi
fi
if [[ -z "$PYTHON_BIN" ]]; then
    err "Could not install Python 3.10+ automatically. Install it from"
    err "https://www.python.org/downloads/ (or your package manager) and re-run."
    exit 1
fi
info "Using $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"
echo ""

# ── Query-only modes: report, never migrate ─────────────────────
# --status, --check-only and --help ask setup_tools.py a question; they
# install nothing. Everything below this point is an install: it moves the
# user's ~/.local/bin/{operator,handoff} aside and only puts them back on
# evidence that an install happened. Running that machinery for a question
# is not merely noisy, it is destructive -- a --status on a machine whose
# report exits non-zero was relabelled "Python setup failed" and rolled
# back, and a --status on a machine that already had `operator` elsewhere on
# PATH DELETED ~/.local/bin/operator outright while reporting a successful
# migration.
#
# So the question is answered without touching anything, and setup_tools.py's
# exit code is forwarded verbatim: --status returns 1 for a machine that is
# merely out of date or has inert extensions, and that is a report, not a
# failure of setup -- which could not fix it in any case, since this toolkit
# never writes the CLI settings file that governs it.
QUERY_ONLY=0
# `$#` is checked first because bash 3.2 -- still the system bash on macOS --
# treats "$@" as unset under `set -u` when there are no positional parameters.
if (( $# > 0 )); then
    for arg in "$@"; do
        case "$arg" in
            --status|--check-only|--help|-h) QUERY_ONLY=1 ;;
        esac
    done
fi

if (( QUERY_ONLY )); then
    set +e
    "$PYTHON_BIN" "${SCRIPT_DIR}/setup_tools.py" "$@"
    status=$?
    set -e
    exit "$status"
fi

canon() { "$PYTHON_BIN" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null || true; }

# ── Step 2: Set aside anything currently at ~/.local/bin/{operator,handoff} ──
# `pip install -e .` (invoked below via setup_tools.py) will unconditionally
# create console scripts named `operator`/`handoff`, replacing whatever
# currently occupies those paths -- symlink or regular file -- with no
# backup of its own. So *anything* found there is moved aside first:
#   - a symlink resolving to *this checkout's* operator.sh/handoff.sh is
#     "legacy" -- its backup is deleted once the new install is confirmed
#     working (see Step 3).
#   - anything else (a symlink elsewhere, a plain file, another checkout)
#     is "foreign" -- it is never auto-deleted, only ever restored or left
#     as a permanent backup, so an unrelated pre-existing command a user
#     had is never silently destroyed.
# Either way the original is renamed (never deleted outright), so a failed
# or interrupted Python install below can never strand the user without a
# working `operator`/`handoff` command: see restore_legacy_links() and the
# INT/TERM trap, both of which are set up before the first rename happens.
echo "Checking ~/.local/bin/{operator,handoff} before installing..."
mkdir -p "$LOCAL_BIN"
OPERATOR_BACKUP=""; OPERATOR_KIND=""
HANDOFF_BACKUP=""; HANDOFF_KIND=""

stash_legacy_link() {
    # Records the backup path and kind into OPERATOR_BACKUP/OPERATOR_KIND or
    # HANDOFF_BACKUP/HANDOFF_KIND, or leaves them empty if there was nothing
    # at $link to begin with.
    local name="$1" script="$2" link="${LOCAL_BIN}/${1}"

    if [[ -L "$link" && ! -e "$link" ]]; then
        warn "${link} is a broken symlink — removing"
        rm -f "$link"
        return 0
    fi

    [[ -e "$link" ]] || return 0

    local kind="foreign" target=""
    if [[ -L "$link" ]]; then
        target="$(canon "$link")"
        local expected
        expected="$(canon "${SCRIPT_DIR}/${script}")"
        if [[ -n "$target" && -n "$expected" && "$target" == "$expected" ]]; then
            kind="legacy"
        fi
    fi

    local suffix backup
    if [[ "$kind" == "legacy" ]]; then
        suffix="$LEGACY_BACKUP_SUFFIX"
    else
        suffix="$FOREIGN_BACKUP_SUFFIX"
    fi
    backup="${link}${suffix}"
    if [[ "$kind" == "legacy" ]]; then
        rm -f "$backup"   # clear any stale backup from a prior interrupted run
    elif [[ -e "$backup" || -L "$backup" ]]; then
        # Never clobber a pre-existing foreign backup -- number it instead.
        local n=2
        while [[ -e "${backup}.${n}" || -L "${backup}.${n}" ]]; do n=$((n+1)); done
        backup="${backup}.${n}"
    fi
    mv "$link" "$backup"
    # Published in the same breath as the mv, and by the function rather than
    # by the caller. restore_legacy_links reads these globals, so a signal
    # arriving after the mv but before an assignment back in the caller would
    # find them empty and restore nothing -- leaving the user's only copy
    # sitting under a backup name with no record that it was ever moved. The
    # gap is small; the loss it produces is permanent and silent.
    if [[ "$name" == "operator" ]]; then
        OPERATOR_BACKUP="$backup"; OPERATOR_KIND="$kind"
    else
        HANDOFF_BACKUP="$backup"; HANDOFF_KIND="$kind"
    fi
    if [[ "$kind" == "legacy" ]]; then
        info "Set aside legacy symlink ${link} -> ${target} (finalized after install succeeds)"
    else
        warn "${link} doesn't point at this checkout's ${script} — moved existing '${name}' aside to ${backup} so it won't be silently overwritten (not auto-deleted)"
    fi
}

restore_legacy_links() {
    local reason="$1"
    if [[ -n "$OPERATOR_BACKUP" ]]; then
        mv "$OPERATOR_BACKUP" "${LOCAL_BIN}/operator"
        warn "Restored ${LOCAL_BIN}/operator ($reason)"
    fi
    if [[ -n "$HANDOFF_BACKUP" ]]; then
        mv "$HANDOFF_BACKUP" "${LOCAL_BIN}/handoff"
        warn "Restored ${LOCAL_BIN}/handoff ($reason)"
    fi
}

# Armed BEFORE the first stash, not after both of them. A Ctrl-C (or kill)
# between the stash below and the commit/rollback further down must not leave
# the user with neither the old command nor the new one -- and the interval
# that matters starts at the first `mv`, not once both have returned. Arming
# it here is safe because both backup variables are empty until a stash fills
# one in, and restore_legacy_links does nothing with an empty one.
trap 'restore_legacy_links "setup interrupted"; exit 130' INT TERM

stash_legacy_link operator operator.sh
stash_legacy_link handoff handoff.sh
if [[ -z "$OPERATOR_BACKUP" && -z "$HANDOFF_BACKUP" ]]; then
    info "Nothing at ~/.local/bin/{operator,handoff} yet"
fi
echo ""

# ── Step 3: Hand off to the cross-platform Python installer ────
# Installs the operator/handoff/operator-ingest console scripts, runtime
# extensions, and configuration templates -- everything the old Steps
# "operator symlink" / "runtime extensions" / "templates" used to do here.
echo "Running Python setup (package, extensions, templates)..."
set +e
"$PYTHON_BIN" "${SCRIPT_DIR}/setup_tools.py" "$@"
status=$?
set -e
echo ""

if (( status != 0 )); then
    err "Python setup failed (exit ${status})."
    restore_legacy_links "Python setup failed"
    exit "$status"
fi

missing=0
for name in operator handoff; do
    command -v "$name" &>/dev/null || { err "'${name}' does not resolve on PATH after setup."; (( missing++ )) || true; }
done

# `command -v` answers "does SOME `operator` resolve on PATH?", which is a
# narrower question than the one the finalization below turns on: "did this
# install put one where the original was set aside?". A user who already has
# an `operator` further along PATH answers the first question yes while
# ~/.local/bin/operator does not exist at all -- and the legacy branch of the
# finalization deletes the backup on that answer, destroying the only
# remaining copy while reporting a successful migration.
#
# So the stashed paths are checked directly. -e alone is not enough: a
# symlink whose target is gone is not -e, and it is still something that has
# to be treated as installed rather than silently overwritten.
for name in operator handoff; do
    if [[ "$name" == "operator" ]]; then backup="$OPERATOR_BACKUP"; else backup="$HANDOFF_BACKUP"; fi
    [[ -n "$backup" ]] || continue
    if [[ ! -e "${LOCAL_BIN}/${name}" && ! -L "${LOCAL_BIN}/${name}" ]]; then
        err "'${name}' was set aside but setup installed nothing at ${LOCAL_BIN}/${name}."
        (( missing++ )) || true
    fi
done

if (( missing > 0 )); then
    restore_legacy_links "the new install isn't in place yet"
    err "Fix the problem reported above, then re-run ./setup.sh."
    exit 1
fi

# Migration/rollback window is over -- operator/handoff are confirmed
# resolvable, so a later Ctrl-C (e.g. during Anvil/MCP/spec-kit steps below)
# has nothing left to protect.
trap - INT TERM

if [[ -n "$OPERATOR_BACKUP" ]]; then
    if [[ "$OPERATOR_KIND" == "legacy" ]]; then
        rm -f "$OPERATOR_BACKUP"
        info "Migrated 'operator' off the legacy bash script -> $(command -v operator)"
    else
        info "Your previous 'operator' command is preserved at ${OPERATOR_BACKUP}"
    fi
fi
if [[ -n "$HANDOFF_BACKUP" ]]; then
    if [[ "$HANDOFF_KIND" == "legacy" ]]; then
        rm -f "$HANDOFF_BACKUP"
        info "Migrated 'handoff' off the legacy bash script -> $(command -v handoff)"
    else
        info "Your previous 'handoff' command is preserved at ${HANDOFF_BACKUP}"
    fi
fi
echo ""

# ── Step 4: Everything else lives in setup_tools.py ───────────
# Anvil, the MCP servers, and spec-kit used to be installed by this script,
# which meant Windows users got none of them. They are now provisioned by
# setup_tools.py above, on every platform, along with the prerequisites
# (multiplexer, git, Copilot CLI) that this script previously only checked
# for before giving up.

# ── Done ─────────────────────────────────────────────────────
echo "═══ Setup Complete ═══"
echo ""
echo "Next steps:"
echo "  1. Run: operator help"
echo "  2. Copy code-intelligence skill into your project:"
echo "       cp -r ${SCRIPT_DIR}/skills/code-intelligence your-project/.github/skills/"
echo "  3. Review ~/.copilot/copilot-instructions.md and customize"
echo "  4. Start a session: operator --agent=anvil:anvil --yolo"
echo "  5. Start an autonomous loop: operator --loop --name myproject"
echo ""
