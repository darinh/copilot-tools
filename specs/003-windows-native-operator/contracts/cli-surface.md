# CLI Surface Contract

**Feature**: `003-windows-native-operator`

The parity checklist for **SC-002**: every row must behave equivalently on Windows and Linux. The
authority for expected behavior is `operator.sh` and `handoff.sh` on `main` (spec Assumptions).

## `operator`

### Invocation forms

| Form | Behavior |
|---|---|
| `operator [--name NAME] [copilot-args...]` | Single session; auto-attaches. |
| `operator --loop [--name NAME] [--fresh] [copilot-args...]` | Autonomous loop mode. |
| `operator NAME` | Positional shortcut: joins running instance `NAME` **only if that session exists**. If it does not, `NAME` is not an instance reference — it falls through and is passed to `copilot` as an argument, starting a session named after the current directory. |
| `operator join [NAME]` | Explicit join. Bare form lists instances. |
| `operator reload NAME` | Regenerate the instance run script. |
| `operator list` | Show running instances. |
| `operator stop [NAME]` | Stop one instance, or all when omitted. Refuses to stop a running session this operator does not own. |
| `operator forget NAME` | Delete an instance's operator state without touching any running session. |
| `operator report [TYPE]` | Usage report. Default `summary`. |
| `operator ingest [--force]` | Process Copilot logs. |
| `operator logs [--prune] [--days N]` | Inspect Copilot's process logs; prune only those already ingested. |
| `operator help` \| `-h` \| `--help` \| `-?` | Help text. |

Reserved words that are never treated as an instance name: `stop`, `list`, `report`, `ingest`, `help`,
`join`, `reload`.

### Options

| Option | Meaning |
|---|---|
| `--name NAME` | Instance name. Default: current directory name. |
| `--loop` | Autonomous loop mode. |
| `--fresh` | Ignore saved state; reset session numbering. |

All unrecognized arguments pass through to `copilot` unchanged, including values with spaces, quotes,
and non-ASCII characters (FR-007).

### Injected Copilot arguments

| Mode | Injected |
|---|---|
| Single session | `--autopilot --effort high --experimental`, plus `--yolo --no-ask-user` when `--headless` (`--detached`) |
| Loop mode | `--yolo --autopilot --no-ask-user --effort high --experimental`, plus `--agent <name>` when absent, plus the autonomous preamble via `-i` |
| Both | `--log-level debug`, unless the user set `--log-level` or `COPILOT_OPERATOR_NO_DEBUG_LOG=1`. Required because Copilot only writes usage data at debug level. |

`--experimental` is injected **unconditionally**, and always ahead of the user's own arguments.
Runtime extensions load only in experimental mode. A user who passes `--no-experimental` still
gets it, because the CLI resolves conflicting spellings last-wins (measured, both orders, CLI
1.0.77). The operator deliberately does **not** inspect the user's arguments to decide, because it
cannot tell a flag from a value: `-p --no-experimental` is a prompt, not a ruling.

In loop mode with saved state, `--resume=<session-id>` is appended **exactly once**, and is suppressed
when the user already specified a session argument (FR-012).

`--yolo` is granted only where nobody can be asked, and it never travels alone: it is always paired
with `--no-ask-user`, because the two close different mouths. `--yolo` waives the approvals the CLI
raises before acting; `--no-ask-user` stops the agent asking a question of its own accord. Granting
only the first still leaves an unattended session able to hang forever on `ask_user`.

The question in every mode is whether a human is watching, and both implementations must answer it
the same way. **Loop mode**: granted — unattended, so a question would hang forever. **Attached
single session**: not granted — the invoking terminal is attached and the human who typed the command
is sitting at it. **Headless single session** (`--headless`, or its synonym `--detached`): granted,
because nothing attaches; `operator join` is an invitation the user may never accept, and a blocked
session is indistinguishable from a working one (live process, live pane, no error).

`operator.sh` has no headless mode, so that branch exists only in `copilot_operator.py`. Note what
that does and does not mean. The shell's single session attaches best-effort (`tmux attach ... ||
true`), so where there is no TTY — a wrapper, CI, a nested tmux — the attach fails and the session
keeps running unattended. That is the environment removing the terminal, not a mode anyone asked
for, and `copilot_operator.py` does the identical thing on the identical path; neither grants
`--yolo` there, so the two still agree. What would be a genuine divergence is the shell gaining a
*deliberate* unattended launch, which is why the test suite asserts, by running the real shell
function, both that it always reaches its attach and that only loop mode grants blanket approval.

Where the two operators disagreed — `copilot_operator.py` injected `--yolo` into attached single
sessions and `operator.sh` did not — the disparity was resolved by converging on the **lower**
authority, because the same command on two platforms granting an agent different powers is a
difference nobody reads the source to discover. Note the asymmetry is not "prompts versus no
prompts": the headless branch goes the other way precisely because there the lower-authority option
is the one that fails silently and unrecoverably. A user who wants any of these in any mode passes
them themselves; they land after the injected defaults and are honoured.

### Report types

`summary` (default), `sessions`, `models`, `projects`, `costs`, `tokens`. Every type must render on
Windows
(FR-010).

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success, including "nothing running" for `list` and `stop` |
| 1 | Error: unknown instance, missing prerequisite, bad arguments |

### Output contracts

- `list` — one row per **operator-managed** instance. Never lists foreign sessions (FR-006).
- `stop NAME` on an unknown instance — error to stderr, then the instance list, exit 1.
- `stop` with none running — `No running operator instances found.`, exit 0.
- Detaching from a single session — prints the re-attach hint and leaves the session alive (FR-005).
- Missing multiplexer — names the binary and the platform-appropriate install command, exit 1 (FR-018).
  **This is new behavior.** `operator.sh` currently prints only `Error: tmux is required but not
  found.` with no remediation, so this row is a requirement to implement, not existing behavior to
  preserve.

## `handoff`

```
handoff --instance NAME --status "..." --next "..."
        [--in-progress "..."] [--context "..."] [--prompt "..."]
        [--project-root DIR]
```

| Option | Required | Meaning |
|---|---|---|
| `--instance NAME` | No | Target instance. Inferred from running instances whose working directory matches the project root when omitted. |
| `--status TEXT` | **Yes** | What was completed. |
| `--next TEXT` | **Yes** | Prioritized next steps. |
| `--in-progress TEXT` | No | Work underway at session end. |
| `--context TEXT` | No | Decisions, gotchas. |
| `--prompt TEXT` | No | Ready-to-run prompt for the next session. |
| `--project-root DIR` | No | Project root for catalog lookup. Default: cwd. |

Both `--opt value` and `--opt=value` forms are accepted.

### Behavior

1. Resolve the project GUID from `~/.copilot/projects/catalog.csv` by normalized path match.
   **Windows: compare case-insensitively** (FR-007).
2. Write `~/.copilot/projects/{guid}/next-session.md` with sections `Status`, `In Progress` (optional),
   `Next Steps`, `Context` (optional), `Prompt` (optional).
3. Create the restart marker `~/.operator/restart/{instance}`.
4. Print the handoff file path and the restart marker path.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Handoff written and restart signalled |
| 1 | Missing required option, no catalog entry, or instance could not be inferred |

A target instance that is not currently running produces a **warning**, not a failure — the handoff file
is still written.

### Deliberate divergence from `handoff.sh`

`handoff.sh` also touches the legacy `~/.copilot/restart/{instance}` path for operators running
pre-migration code. That scaffolding is **not** carried into the Python port.

## Cross-cutting

### Filesystem layout

| Path | Purpose |
|---|---|
| `~/.operator/` | State root. Override with `COPILOT_OPERATOR_HOME`. |
| `~/.operator/metrics.db` | SQLite metrics. |
| `~/.operator/operator.log` | Operator log. |
| `~/.operator/restart/{name}` | Restart marker. |
| `~/.operator/restart/{name}.state` | Persisted instance state. |
| `~/.operator/restart/{name}.managed` | Ownership marker. |
| `~/.operator/restart/{name}.pid` | Copilot's real process ID, written by the launch script. |
| `~/.operator/run-{name}.{sh,ps1}` | Generated launch script. |
| `~/.operator/backups/` | Historical backups of the operator script. |
| `~/.copilot/logs/process-*.log` | Copilot logs (read-only input). |

`~` resolves to `%USERPROFILE%` on Windows. State is per-platform and not shared with WSL (spec
Assumptions).

### Prerequisites

| Tool | Linux / WSL / macOS | Windows |
|---|---|---|
| Multiplexer | `tmux` | `psmux` |
| Python | 3.10+ | 3.10+ |
| `copilot` | Required | Required |
| `git` | Required | Required |
| `sqlite3` binary | **No longer required** | **No longer required** |

### Documented platform difference

The generated launch script is a shell script on POSIX and a **PowerShell** script on Windows. Batch is
not used: it cannot carry the multi-line preamble, and PowerShell is required to capture Copilot's real
process ID via `-PassThru`. This is an internal artifact; no user-visible command changes.
