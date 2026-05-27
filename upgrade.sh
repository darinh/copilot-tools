#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# copilot-tools upgrade — Pull the latest changes and re-run setup.
#
# Usage: ./upgrade.sh
#
# Steps:
#   1. git fetch + fast-forward (refuses if uncommitted changes or
#      diverged history would lose work).
#   2. Re-run setup.sh, which:
#        - re-symlinks operator/handoff (or refreshes NTFS wrappers),
#        - resyncs extensions,
#        - smart-upgrades templates (auto-upgrade if unmodified,
#          prompt if user has local edits),
#        - migrates any leftover ~/.copilot/projects/ state into
#          ~/.operator/projects/.
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

info() { echo "  ✅ $*"; }
warn() { echo "  ⚠️  $*"; }
err()  { echo "  ❌ $*" >&2; }

echo ""
echo "═══ Copilot Tools Upgrade ═══"
echo ""

# ── 1. Sanity: must be a git checkout ─────────────────────────
if ! git -C "$SCRIPT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    err "$SCRIPT_DIR is not a git checkout. Clone the repo and re-run from there."
    exit 1
fi

# Refuse to pull on top of uncommitted changes — would clobber unsaved work.
if [[ -n "$(git -C "$SCRIPT_DIR" status --porcelain)" ]]; then
    err "Uncommitted changes detected in $SCRIPT_DIR. Commit or stash them first."
    git -C "$SCRIPT_DIR" status --short
    exit 1
fi

# ── 2. Fetch + fast-forward ───────────────────────────────────
branch="$(git -C "$SCRIPT_DIR" rev-parse --abbrev-ref HEAD)"
info "On branch: $branch"

if ! git -C "$SCRIPT_DIR" fetch --quiet; then
    err "git fetch failed. Check your network / remote."
    exit 1
fi

before="$(git -C "$SCRIPT_DIR" rev-parse HEAD)"
if ! git -C "$SCRIPT_DIR" pull --ff-only --quiet; then
    err "Cannot fast-forward. Your branch has diverged from origin."
    err "Resolve manually (rebase/merge) and re-run."
    exit 1
fi
after="$(git -C "$SCRIPT_DIR" rev-parse HEAD)"

if [[ "$before" == "$after" ]]; then
    info "Already up to date."
else
    info "Updated $before → $after"
    git -C "$SCRIPT_DIR" --no-pager log --oneline "${before}..${after}" | sed 's/^/    /'
fi
echo ""

# ── 3. Re-run setup ───────────────────────────────────────────
exec bash "$SCRIPT_DIR/setup.sh"
