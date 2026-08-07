#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# handoff — Atomic session handoff for Copilot CLI agents
#
# Writes a per-instance handoff file and touches the restart
# marker so the operator loop picks up the new session automatically.
#
# Usage:
#   handoff --instance NAME --status "..." --next "..."
#          [--in-progress "..."] [--context "..."] [--prompt "..."]
#          [--project-root DIR]
#
# The project GUID is resolved from ~/.operator/projects/catalog.csv
# using --project-root or the current working directory.
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

CATALOG="${HOME}/.operator/projects/catalog.csv"
RESTART_DIR="${COPILOT_OPERATOR_HOME:-${HOME}/.operator}/restart"

# ── Helpers ─────────────────────────────────────────────────────
die() { echo "Error: $*" >&2; exit 1; }

# ── Set membership, without associative arrays ──────────────────
#
# `/bin/bash` on macOS is 3.2 and always will be — Apple froze it at the last
# GPLv2 release — and 3.2 has no associative arrays at all. `local -A x` there
# is not a subtly different array, it is `declare: -A: invalid option`, and
# under the `set -e` above that ends the run. `resolve_instance` had one, and
# it is reached on every `handoff` invoked without `--instance`.
#
# What made it survive is the shape of the call site: the abort happened
# inside `$(resolve_instance ...)`, so it arrived as a non-zero status and
# `die "Cannot infer instance. Use --instance NAME"` — the same message a
# genuine no-match produces. On macOS the inference had never once worked, and
# it said so in the words of a feature declining to guess.
#
# Membership is now an exact scan of an indexed array, which is the one kind
# of array bash 3.2 does have. See `in_list` in operator.sh for why the
# cheaper encoding — join the names and ask whether the string contains one —
# is wrong: these names come from marker filenames, and a filename may contain
# a newline.
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

resolve_guid() {
    local project_root="$1"
    [[ -f "$CATALOG" ]] || die "Catalog not found: $CATALOG"

    # Normalize the path for matching
    local normalized
    normalized=$(cd "$project_root" 2>/dev/null && pwd) || die "Directory not found: $project_root"

    local guid=""
    while IFS=',' read -r path id; do
        # Strip surrounding quotes
        path="${path#\"}"
        path="${path%\"}"
        id="${id#\"}"
        id="${id%\"}"
        if [[ "$path" == "$normalized" ]]; then
            guid="$id"
            break
        fi
    done < "$CATALOG"

    [[ -n "$guid" ]] || die "No catalog entry for: $normalized
Add it with:
  echo '\"$normalized\",<guid>' >> $CATALOG"

    echo "$guid"
}

# ── The instance name has to be a filename, and the same one ───
#
# Sets `instance_id`, the name every path here is built from. A name
# containing `/` or `..` would write outside the project's handoff directory,
# and a name the Python writer would mangle gets written where the Python
# reader will not look. `operator` and the installed `handoff` both address an
# instance through `safe_instance_id`, which replaces `.:\/*?"<>|` and control
# characters, strips outer dashes, case-folds where the filesystem is
# case-insensitive, and appends a sha1 digest of the original whenever it
# changed anything -- so `a.b` and `a:b` stay two instances rather than
# collapsing into one.
#
# Reimplementing that here would mean reimplementing sha1 in bash 3.2 across
# two different checksum binaries, and a *nearly* matching id is the worst
# outcome available: it writes a real handoff to a path nothing reads, and
# reports success. So this refuses the names it cannot address identically
# instead of guessing. What is left is the set `safe_instance_id` returns
# unchanged, which is what `operator` generates.
addressable_instance_id() {
    local name="$1"
    case "$name" in
        *[!a-zA-Z0-9-]* | -* | *- | "")
            die "Instance name '${name}' is not addressable by this script (letters, digits and inner dashes only). Use the installed \`handoff\` command, which handles any name." ;;
    esac

    # Folded on macOS only, because that is where the id is folded: APFS is
    # case-insensitive, so `Build` and `build` are one file and therefore one
    # instance. On Linux they are two, and folding here would merge them.
    instance_id="$name"
    if [ "$(uname -s)" = "Darwin" ]; then
        instance_id=$(printf '%s' "$name" | tr 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz')
    fi

    case "$instance_id" in
        con | prn | aux | nul | com[1-9] | lpt[1-9] | CON | PRN | AUX | NUL | COM[1-9] | LPT[1-9])
            die "Instance name '${name}' is a reserved device name, which the installed \`handoff\` gives a digest suffix. Use that command instead." ;;
    esac

    case "$instance_id" in
        *-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f])
            die "Instance name '${name}' ends in something the installed \`handoff\` reads as a generated digest, so the two would disagree about its file. Use that command instead." ;;
    esac
}

resolve_instance() {
    # If no instance specified, try to find the one running in this project's cwd
    local project_root="$1"
    local normalized
    normalized=$(cd "$project_root" 2>/dev/null && pwd) || return 1

    # Collect operator-managed session names from markers
    local managed_sessions=()
    for f in "${RESTART_DIR}"/*.state "${RESTART_DIR}"/*.managed; do
        [[ -e "$f" ]] || continue
        local base
        base=$(basename "$f")
        base="${base%.state}"
        base="${base%.managed}"
        managed_sessions+=("$base")
    done

    local matches=()
    while IFS= read -r session_name; do
        if in_list "$session_name" ${managed_sessions[@]+"${managed_sessions[@]}"}; then
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
        for m in ${matches[@]+"${matches[@]}"}; do
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
    1. Resolves project GUID from ~/.operator/projects/catalog.csv
    2. Writes ~/.operator/projects/{guid}/handoff/{instance}.md
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

addressable_instance_id "$instance"

# ── Resolve GUID ────────────────────────────────────────────────
guid=$(resolve_guid "$project_root")
project_dir="${HOME}/.operator/projects/${guid}"
handoff_dir="${project_dir}/handoff"
handoff_file="${handoff_dir}/${instance_id}.md"
restart_marker="${RESTART_DIR}/${instance_id}"

mkdir -p "$handoff_dir" "$RESTART_DIR"

# ── Bank an unread predecessor ─────────────────────────────────
#
# The reader deletes this file once it has consumed it, so finding one here
# means a session of THIS instance ended without picking up what the one
# before it left. One slot, replaced each time: a second consecutive miss
# means the read side is broken, and keeping the older of two undelivered
# handoffs does not fix that.
if [[ -f "$handoff_file" ]]; then
    prev_file="${handoff_dir}/${instance_id}.prev.md"
    if mv -f "$handoff_file" "$prev_file"; then
        echo "Warning: '${instance}' had an unread handoff at ${handoff_file};" >&2
        echo "         a session ended without reading it. Moved to ${prev_file}" >&2
    else
        die "A handoff is already waiting at ${handoff_file} and could not be moved aside. Refusing to overwrite it."
    fi
fi

# ── Write Handoff ───────────────────────────────────────────────
{
    echo "# Session Handoff"
    echo ""
    echo "*Written by operator instance: \`${instance}\`*"
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
} > "$handoff_file"

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
    touch "${LEGACY_RESTART_DIR}/${instance_id}" 2>/dev/null || true
fi

echo "✅ Handoff written: $handoff_file"
echo "✅ Restart signal: $restart_marker"
