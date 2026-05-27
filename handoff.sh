#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# handoff — Atomic session handoff for Copilot CLI agents
#
# Writes a next-session.md handoff file and touches the restart
# marker so the operator loop picks up the new session automatically.
#
# Usage:
#   handoff --instance NAME --status "..." --next "..."
#          [--in-progress "..."] [--context "..."] [--prompt "..."]
#          [--project-root DIR]
#
# The project GUID is resolved from ~/.operator/projects/catalog.csv
# using --project-root or the current working directory. The legacy
# ~/.copilot/projects/catalog.csv is consulted as a fallback for users
# mid-migration and the matching project gets moved into ~/.operator/.
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

# Operator state lives under ~/.operator/ (kept out of ~/.copilot/, which
# the Copilot CLI itself manages). Override with COPILOT_OPERATOR_HOME.
OPERATOR_HOME="${COPILOT_OPERATOR_HOME:-${HOME}/.operator}"
CATALOG="${OPERATOR_HOME}/projects/catalog.csv"
PROJECTS_DIR="${OPERATOR_HOME}/projects"
RESTART_DIR="${OPERATOR_HOME}/restart"

# Legacy paths (read-only fallback for users mid-migration). New writes always
# go to the locations above. On a legacy hit we opportunistically migrate just
# that one project so the migration completes incrementally without a big-bang
# move.
LEGACY_CATALOG="${HOME}/.copilot/projects/catalog.csv"
LEGACY_PROJECTS_DIR="${HOME}/.copilot/projects"

# ── Helpers ─────────────────────────────────────────────────────
die() { echo "Error: $*" >&2; exit 1; }
warn() { echo "Warning: $*" >&2; }

# Match a project root path against a catalog file. Echoes the matching GUID
# on success, empty string on miss.
lookup_in_catalog() {
    local catalog="$1" normalized="$2"
    [[ -f "$catalog" ]] || return 0
    local path id guid=""
    while IFS=',' read -r path id; do
        path="${path#\"}"; path="${path%\"}"
        id="${id#\"}"; id="${id%\"}"
        if [[ "$path" == "$normalized" ]]; then
            guid="$id"; break
        fi
    done < "$catalog"
    echo "$guid"
}

# Opportunistically migrate one project's state from the legacy ~/.copilot/projects/
# location into ~/.operator/projects/. Used when handoff finds the project in the
# legacy catalog only — avoids leaving handoff broken for users who haven't run
# `upgrade` yet. Safe to call repeatedly: each move skips if the target exists.
migrate_one_project() {
    local guid="$1" path="$2"
    mkdir -p "$PROJECTS_DIR"

    # Move the per-project directory if it exists in legacy but not new.
    local legacy_dir="${LEGACY_PROJECTS_DIR}/${guid}"
    local new_dir="${PROJECTS_DIR}/${guid}"
    if [[ -d "$legacy_dir" && ! -e "$new_dir" ]]; then
        mv "$legacy_dir" "$new_dir" 2>/dev/null || return 1
    fi

    # Append the catalog row to the new catalog if not already there.
    if [[ -f "$CATALOG" ]]; then
        if ! grep -Fq "\"$path\",$guid" "$CATALOG" 2>/dev/null && \
           ! grep -Fq "\"$path\",\"$guid\"" "$CATALOG" 2>/dev/null; then
            echo "\"$path\",$guid" >> "$CATALOG"
        fi
    else
        echo "\"$path\",$guid" > "$CATALOG"
    fi

    warn "Migrated project entry for '$path' from ~/.copilot/projects/ to ${PROJECTS_DIR}/. Run ./upgrade.sh in copilot-tools to migrate everything else."
}

resolve_guid() {
    local project_root="$1"
    local normalized
    normalized=$(cd "$project_root" 2>/dev/null && pwd) || die "Directory not found: $project_root"

    # 1. Try the canonical catalog first.
    local guid=""
    guid=$(lookup_in_catalog "$CATALOG" "$normalized")
    if [[ -n "$guid" ]]; then
        echo "$guid"; return 0
    fi

    # 2. Fall back to the legacy catalog. On hit, migrate just that one project
    #    so subsequent lookups go through the canonical path.
    if [[ -f "$LEGACY_CATALOG" ]]; then
        guid=$(lookup_in_catalog "$LEGACY_CATALOG" "$normalized")
        if [[ -n "$guid" ]]; then
            migrate_one_project "$guid" "$normalized" || true
            echo "$guid"; return 0
        fi
    fi

    die "No catalog entry for: $normalized
Add it with:
  echo '\"$normalized\",<guid>' >> $CATALOG"
}

resolve_instance() {
    # If no instance specified, try to find the one running in this project's cwd
    local project_root="$1"
    local normalized
    normalized=$(cd "$project_root" 2>/dev/null && pwd) || return 1

    # Collect operator-managed session names from markers
    local -A managed_sessions
    for f in "${RESTART_DIR}"/*.state "${RESTART_DIR}"/*.managed; do
        [[ -e "$f" ]] || continue
        local base
        base=$(basename "$f")
        base="${base%.state}"
        base="${base%.managed}"
        managed_sessions["$base"]=1
    done

    local matches=()
    while IFS= read -r session_name; do
        if [[ -n "${managed_sessions[$session_name]+x}" ]]; then
            # Check if tmux session's cwd matches our project root
            local session_cwd
            session_cwd=$(tmux display-message -t "$session_name" -p '#{pane_current_path}' 2>/dev/null || echo "")
            if [[ "$session_cwd" == "$normalized" || "$session_cwd" == "$normalized/"* ]]; then
                matches+=("$session_name")
            fi
        fi
    done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null)

    if (( ${#matches[@]} == 1 )); then
        echo "${matches[0]}"
        return 0
    elif (( ${#matches[@]} > 1 )); then
        echo "Multiple operator instances found for this project:" >&2
        for m in "${matches[@]}"; do
            echo "  $m" >&2
        done
        echo "Specify one with --instance NAME" >&2
        return 1
    else
        return 1
    fi
}

show_help() {
    cat << 'HELP'
handoff — Atomic session handoff for Copilot CLI agents

USAGE
    handoff --instance NAME --status "..." --next "..."
           [--in-progress "..."] [--context "..."] [--prompt "..."]
           [--project-root DIR]

REQUIRED
    --instance NAME     Operator instance name (short form, e.g. "agent-academy")
    --status TEXT       What was just completed
    --next TEXT         Prioritized next steps

OPTIONAL
    --in-progress TEXT  What was actively being worked on
    --context TEXT      Key decisions, gotchas, architectural notes
    --prompt TEXT       Ready-to-execute prompt for next session
    --project-root DIR  Project root (default: cwd, used for GUID lookup)

If --instance is omitted, handoff tries to infer it from running
operator sessions whose working directory matches the project root.

WHAT IT DOES
    1. Resolves project GUID from ~/.operator/projects/catalog.csv (with
       one-time fallback read of legacy ~/.copilot/projects/catalog.csv).
    2. Writes ~/.operator/projects/{guid}/next-session.md atomically.
    3. Touches ~/.operator/restart/{instance} to trigger operator restart
HELP
}

# ── Argument Parsing ────────────────────────────────────────────
instance=""
status=""
in_progress=""
next_steps=""
context=""
prompt=""
project_root="$(pwd)"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --instance)
            [[ $# -ge 2 ]] || die "--instance requires a value"
            instance="$2"; shift 2 ;;
        --instance=*)
            instance="${1#--instance=}"; shift ;;
        --status)
            [[ $# -ge 2 ]] || die "--status requires a value"
            status="$2"; shift 2 ;;
        --status=*)
            status="${1#--status=}"; shift ;;
        --in-progress)
            [[ $# -ge 2 ]] || die "--in-progress requires a value"
            in_progress="$2"; shift 2 ;;
        --in-progress=*)
            in_progress="${1#--in-progress=}"; shift ;;
        --next)
            [[ $# -ge 2 ]] || die "--next requires a value"
            next_steps="$2"; shift 2 ;;
        --next=*)
            next_steps="${1#--next=}"; shift ;;
        --context)
            [[ $# -ge 2 ]] || die "--context requires a value"
            context="$2"; shift 2 ;;
        --context=*)
            context="${1#--context=}"; shift ;;
        --prompt)
            [[ $# -ge 2 ]] || die "--prompt requires a value"
            prompt="$2"; shift 2 ;;
        --prompt=*)
            prompt="${1#--prompt=}"; shift ;;
        --project-root)
            [[ $# -ge 2 ]] || die "--project-root requires a value"
            project_root="$2"; shift 2 ;;
        --project-root=*)
            project_root="${1#--project-root=}"; shift ;;
        help|-h|--help)
            show_help; exit 0 ;;
        *)
            die "Unknown argument: $1" ;;
    esac
done

# ── Validation ──────────────────────────────────────────────────
[[ -n "$status" ]]     || die "Missing required: --status"
[[ -n "$next_steps" ]] || die "Missing required: --next"

# Resolve instance if not provided
if [[ -z "$instance" ]]; then
    instance=$(resolve_instance "$project_root") || die "Cannot infer instance. Use --instance NAME"
fi

# Warn if instance isn't a running tmux session (non-fatal)
if ! tmux has-session -t "$instance" 2>/dev/null; then
    echo "Warning: No tmux session '$instance' found. Handoff file will be written but restart may not trigger." >&2
fi

# ── Resolve GUID ────────────────────────────────────────────────
guid=$(resolve_guid "$project_root")
project_dir="${PROJECTS_DIR}/${guid}"
handoff_file="${project_dir}/next-session.md"
restart_marker="${RESTART_DIR}/${instance}"

mkdir -p "$project_dir" "$RESTART_DIR"

# ── Write Handoff (atomic) ─────────────────────────────────────
# Write to a tmpfile in the same directory, then atomic rename. This guarantees
# the next-session.md reader (the operator preamble) never sees a half-written
# file — a real concern when the agent's last action is `touch <restart>` which
# triggers an immediate operator restart.
tmpfile=$(mktemp "${project_dir}/.next-session.XXXXXX.md")
{
    echo "# Session Handoff"
    echo ""
    echo "## Status"
    echo "$status"
    echo ""
    if [[ -n "$in_progress" ]]; then
        echo "## In Progress"
        echo "$in_progress"
        echo ""
    fi
    echo "## Next Steps"
    echo "$next_steps"
    echo ""
    if [[ -n "$context" ]]; then
        echo "## Context"
        echo "$context"
        echo ""
    fi
    if [[ -n "$prompt" ]]; then
        echo "## Prompt"
        echo "$prompt"
        echo ""
    fi
} > "$tmpfile"
mv "$tmpfile" "$handoff_file"

# ── Touch Restart Marker ───────────────────────────────────────
touch "$restart_marker"

# Transitional: also touch the legacy path so operator instances that are
# still running pre-migration code (watching ~/.copilot/restart/) get the
# restart signal. Safe to keep — even after all operators are restarted,
# the legacy touch is a no-op (copilot CLI wipes that dir on its next
# startup anyway). Remove once you're confident no legacy operators remain.
LEGACY_RESTART_DIR="${HOME}/.copilot/restart"
if [[ "$LEGACY_RESTART_DIR" != "$RESTART_DIR" ]]; then
    mkdir -p "$LEGACY_RESTART_DIR" 2>/dev/null || true
    touch "${LEGACY_RESTART_DIR}/${instance}" 2>/dev/null || true
fi

echo "✅ Handoff written: $handoff_file"
echo "✅ Restart signal: $restart_marker"
