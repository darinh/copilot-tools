#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# migrate-operator-state.sh — Move operator/handoff state out of
# ~/.copilot/projects/ (where it collides with the Copilot CLI's own
# home) into ~/.operator/projects/.
#
# Safe to source repeatedly: each move skips if the target already
# exists. Refuses to run if:
#   • an operator tmux session is currently active (the running
#     instance still reads from the legacy path);
#   • both catalogs exist with conflicting rows for the same project
#     path (manual reconciliation needed).
#
# Migrates ONLY:
#   • ~/.copilot/projects/catalog.csv
#   • ~/.copilot/projects/<guid-shaped-dir>/    (UUID format)
#
# Leaves anything else under ~/.copilot/projects/ alone and warns
# about it.
#
# Sourced by setup.sh and upgrade.sh. Can also be run directly:
#   bash lib/migrate-operator-state.sh
# ═══════════════════════════════════════════════════════════════════

# This file is sourced, so don't enable set -e at file scope — the parent
# script controls that. Functions return non-zero on error and the parent
# decides how to react.

OPERATOR_HOME="${COPILOT_OPERATOR_HOME:-${HOME}/.operator}"
LEGACY_PROJECTS_DIR="${HOME}/.copilot/projects"
NEW_PROJECTS_DIR="${OPERATOR_HOME}/projects"
LEGACY_CATALOG="${LEGACY_PROJECTS_DIR}/catalog.csv"
NEW_CATALOG="${NEW_PROJECTS_DIR}/catalog.csv"
MIGRATE_LOCK="${OPERATOR_HOME}/.migrate.lock"

# UUID regex: 8-4-4-4-12 hex. Matches all the GUIDs the catalog generates.
_GUID_RE='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'

_mig_info() { echo "  ✅ [migrate] $*"; }
_mig_warn() { echo "  ⚠️  [migrate] $*"; }
_mig_err()  { echo "  ❌ [migrate] $*" >&2; }

# Check if any operator-managed tmux session is currently running. If yes,
# migrating their state out from under them risks losing in-flight handoffs.
_mig_operators_running() {
    command -v tmux >/dev/null 2>&1 || return 1
    local restart_dir="${OPERATOR_HOME}/restart"
    [[ -d "$restart_dir" ]] || return 1
    local sess
    while IFS= read -r sess; do
        [[ -n "$sess" ]] || continue
        if [[ -e "${restart_dir}/${sess}.managed" || -e "${restart_dir}/${sess}.state" ]]; then
            echo "$sess"
            return 0
        fi
    done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null)
    return 1
}

# Detect conflicting rows: same project path, different GUID, in legacy vs new.
# Returns 0 if a conflict exists (and prints details), 1 if none.
_mig_catalog_conflicts() {
    [[ -f "$LEGACY_CATALOG" && -f "$NEW_CATALOG" ]] || return 1
    local conflict=0 path id new_id
    while IFS=',' read -r path id; do
        path="${path#\"}"; path="${path%\"}"
        id="${id#\"}"; id="${id%\"}"
        [[ -n "$path" && -n "$id" ]] || continue
        # Look up the same path in the new catalog.
        new_id=$(awk -F',' -v p="\"$path\"" '$1==p {gsub(/^"|"$/,"",$2); print $2; exit}' "$NEW_CATALOG" 2>/dev/null)
        if [[ -n "$new_id" && "$new_id" != "$id" ]]; then
            echo "  $path: legacy=$id  new=$new_id"
            conflict=1
        fi
    done < "$LEGACY_CATALOG"
    return $(( conflict == 1 ? 0 : 1 ))
}

# Merge legacy catalog rows into the new catalog. Skips rows whose path is
# already present in the new catalog (regardless of GUID — those are handled
# by the conflict check above which gates the whole migration). Also skips
# rows whose GUID is listed in $_MIG_FAILED_GUIDS — the legacy directory for
# that GUID failed to move, so we must NOT advertise it in the new catalog;
# handoff.sh's per-lookup legacy fallback will still find the stranded state.
_mig_merge_catalogs() {
    [[ -f "$LEGACY_CATALOG" ]] || return 0
    mkdir -p "$NEW_PROJECTS_DIR"
    touch "$NEW_CATALOG"
    local appended=0 skipped_failed=0 path id
    : "${_MIG_FAILED_GUIDS:=$'\n'}"
    while IFS=',' read -r path id; do
        path="${path#\"}"; path="${path%\"}"
        id="${id#\"}"; id="${id%\"}"
        [[ -n "$path" && -n "$id" ]] || continue
        # Skip rows whose dir-move failed (still recoverable via legacy fallback).
        if [[ "$_MIG_FAILED_GUIDS" == *$'\n'"${id}"$'\n'* ]]; then
            (( skipped_failed++ )) || true
            continue
        fi
        if ! awk -F',' -v p="\"$path\"" '$1==p {found=1} END{exit !found}' "$NEW_CATALOG" 2>/dev/null; then
            echo "\"$path\",$id" >> "$NEW_CATALOG"
            (( appended++ )) || true
        fi
    done < "$LEGACY_CATALOG"
    (( appended > 0 ))       && _mig_info "Merged $appended catalog row(s) into $NEW_CATALOG"
    (( skipped_failed > 0 )) && _mig_warn "Held back $skipped_failed catalog row(s) tied to failed-move project dirs; legacy fallback will resolve them."
    return 0
}

# Move GUID-shaped project subdirs from legacy → new, skipping any that
# already exist at the destination. Warns about non-GUID entries it leaves
# behind. Returns 0 on full success, 2 if any move FAILED (so the caller
# knows not to nuke the legacy catalog).
#
# Populates two globals consumed by _mig_merge_catalogs:
#   _MIG_SAFE_GUIDS   — GUIDs whose dir is safely at the new location
#                       (moved or already there). Their catalog rows are
#                       safe to merge.
#   _MIG_FAILED_GUIDS — GUIDs whose legacy dir FAILED to move. Their
#                       catalog rows must NOT be merged — otherwise the
#                       new catalog would point handoff.sh at a missing
#                       directory and the legacy fallback in handoff.sh
#                       could never find the stranded state.
_mig_move_projects() {
    _MIG_SAFE_GUIDS=$'\n'
    _MIG_FAILED_GUIDS=$'\n'
    [[ -d "$LEGACY_PROJECTS_DIR" ]] || return 0
    mkdir -p "$NEW_PROJECTS_DIR"
    local moved=0 skipped=0 unknown=0 failed=0 base
    shopt -s nullglob dotglob
    for entry in "$LEGACY_PROJECTS_DIR"/*; do
        [[ -e "$entry" ]] || continue
        base="$(basename "$entry")"
        # Don't touch the catalog file here — handled by _mig_merge_catalogs.
        [[ "$base" == "catalog.csv" ]] && continue
        if [[ -d "$entry" && "$base" =~ $_GUID_RE ]]; then
            if [[ -e "${NEW_PROJECTS_DIR}/${base}" ]]; then
                (( skipped++ )) || true
                _MIG_SAFE_GUIDS+="${base}"$'\n'
            else
                if mv "$entry" "${NEW_PROJECTS_DIR}/${base}" 2>/dev/null; then
                    (( moved++ )) || true
                    _MIG_SAFE_GUIDS+="${base}"$'\n'
                else
                    _mig_err "Failed to move project dir: $entry → ${NEW_PROJECTS_DIR}/${base}"
                    (( failed++ )) || true
                    _MIG_FAILED_GUIDS+="${base}"$'\n'
                fi
            fi
        else
            _mig_warn "Leaving non-project entry in legacy dir: $entry"
            (( unknown++ )) || true
        fi
    done
    shopt -u nullglob dotglob
    (( moved > 0 ))   && _mig_info "Moved $moved project dir(s) to $NEW_PROJECTS_DIR"
    (( skipped > 0 )) && _mig_info "Skipped $skipped project dir(s) (target already present)"
    (( failed > 0 ))  && _mig_err  "FAILED to move $failed project dir(s); legacy catalog will NOT be removed."
    (( failed > 0 )) && return 2
    return 0
}

# Public entry point. Returns:
#   0 on success (including no-op when nothing to migrate)
#   1 on hard refusal (running operators, catalog conflicts)
#   2 on partial failure (some moves failed)
migrate_operator_state() {
    # Nothing legacy to migrate? Done.
    if [[ ! -d "$LEGACY_PROJECTS_DIR" && ! -f "$LEGACY_CATALOG" ]]; then
        return 0
    fi

    mkdir -p "$OPERATOR_HOME"

    # Acquire a non-blocking exclusive lock so parallel setup.sh / upgrade.sh
    # invocations don't race. `flock` is in util-linux; if it's not installed
    # we fall back to a best-effort O_EXCL pidfile (rare on the platforms we
    # target, but worth a graceful degrade).
    local lock_fd
    if command -v flock >/dev/null 2>&1; then
        exec {lock_fd}>"$MIGRATE_LOCK" || { _mig_err "Cannot open lock: $MIGRATE_LOCK"; return 1; }
        if ! flock -n -x "$lock_fd"; then
            _mig_err "Another migration is in progress (lock held: $MIGRATE_LOCK). Wait for it to finish."
            exec {lock_fd}>&-
            return 1
        fi
    else
        # Best-effort pidfile lock
        if ( set -o noclobber; echo "$$" > "$MIGRATE_LOCK" ) 2>/dev/null; then
            : # acquired
        else
            _mig_err "Lock present: $MIGRATE_LOCK. If no migration is running, remove it and retry."
            return 1
        fi
        trap "rm -f '$MIGRATE_LOCK'" RETURN
    fi

    # Refuse while operator instances are running.
    local running
    if running=$(_mig_operators_running); then
        _mig_err "Operator instance '$running' is still running. Stop it first:"
        _mig_err "    operator stop $running"
        [[ -n "${lock_fd:-}" ]] && exec {lock_fd}>&- || true
        return 1
    fi

    # Refuse on catalog conflicts (same path, different GUID).
    if _mig_catalog_conflicts; then
        _mig_err "Legacy and new catalogs disagree on the GUID for one or more projects (see above)."
        _mig_err "Reconcile manually: pick the GUID you want, edit both catalogs to match,"
        _mig_err "then re-run this migration."
        [[ -n "${lock_fd:-}" ]] && exec {lock_fd}>&- || true
        return 1
    fi

    # Do the work. Order matters: move project dirs FIRST so we know which
    # GUIDs are safely at the destination, then merge catalog rows that
    # correspond to safe (or catalog-only) GUIDs. If any move fails, the
    # catalog merger skips those rows AND we keep the legacy catalog so
    # handoff.sh's per-lookup legacy fallback can resolve the stranded state.
    _mig_move_projects
    local move_rc=$?
    _mig_merge_catalogs

    if (( move_rc != 0 )); then
        _mig_warn "Migration completed with partial failures. Legacy catalog kept at: $LEGACY_CATALOG"
        _mig_warn "Re-run after resolving the underlying issue (permissions, disk space, etc.)."
        [[ -n "${lock_fd:-}" ]] && exec {lock_fd}>&- || true
        return $move_rc
    fi

    # Remove the legacy catalog only after a successful merge AND successful
    # project moves. Leave the empty parent dir alone in case the Copilot CLI
    # itself ever uses it.
    if [[ -f "$LEGACY_CATALOG" ]]; then
        rm -f "$LEGACY_CATALOG" && _mig_info "Removed merged legacy catalog: $LEGACY_CATALOG"
    fi

    # Remove the legacy projects dir if it's now empty.
    if [[ -d "$LEGACY_PROJECTS_DIR" ]]; then
        rmdir "$LEGACY_PROJECTS_DIR" 2>/dev/null && _mig_info "Removed empty legacy dir: $LEGACY_PROJECTS_DIR" || true
    fi

    [[ -n "${lock_fd:-}" ]] && exec {lock_fd}>&- || true
    return 0
}

# Allow direct execution as well as sourcing.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    migrate_operator_state
    exit $?
fi
