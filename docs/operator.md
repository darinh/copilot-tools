# Operator

The operator is a metrics-capturing wrapper for GitHub Copilot CLI. It wraps `copilot` to capture usage metrics (premium requests, API time, session time, per-model breakdown) into a SQLite database. It supports single-session mode (default) and autonomous loop mode with automatic restarts.

## Platform Support

The operator runs natively on **Windows, Linux, WSL, and macOS**. Session
management uses a terminal multiplexer, selected automatically:

| Platform | Multiplexer | Install |
|----------|-------------|---------|
| Windows | `psmux` | `winget install --id marlocarlo.psmux` |
| Linux / WSL | `tmux` | `sudo apt install tmux` |
| macOS | `tmux` | `brew install tmux` |

psmux implements tmux's command surface and installs a `tmux` alias, so the same
code path serves every platform. Override the choice with
`COPILOT_OPERATOR_MUX` if you need a specific binary.

> The original bash `operator.sh` is retained unchanged for existing Linux and
> WSL users. The Python implementation described here is the supported entry
> point on all platforms.

## Prerequisites

| Tool | Purpose | Notes |
|------|---------|-------|
| multiplexer | Session management | See table above |
| `python` 3.10+ | The operator itself | |
| `copilot` | GitHub Copilot CLI | |
| `git` | Branch detection for metrics | |

`sqlite3` is **not** required: metrics use Python's standard-library `sqlite3`
module rather than the standalone binary.

## Installation

```
python setup_tools.py
```

This is the same command on every platform. It verifies prerequisites, installs
the `operator`, `handoff` and `operator-ingest` console scripts, links runtime
extensions, and installs configuration templates. It is safe to rerun and will
not overwrite edited configuration without asking.

<details>
<summary>Legacy bash install (Linux/WSL only)</summary>

```bash
# From the copilot-tools repo
./setup.sh

# Or manually
ln -sf /path/to/copilot-tools/operator.sh ~/.local/bin/operator
```
</details>

## Architecture

Each session runs under a supervisor process inside the multiplexer pane rather
than launching Copilot directly. This exists for two reasons:

**Correct process identity.** The bash implementation relied on `exec copilot`,
which replaces the shell so that the pane's PID *is* Copilot's PID — the key
used to find Copilot's telemetry log. Windows has no `exec`, so the pane PID
identifies the multiplexer's own shell instead. Worse, `copilot.exe` installed
by WinGet is a shim that re-execs the real binary under a different PID. The
supervisor launches Copilot itself and matches the log against the whole
process tree it created, so attribution is correct on every platform and
concurrent instances never record each other's usage.

**Metrics survive detaching.** Because the supervisor lives inside the session,
it captures metrics when Copilot exits even if you detached long before and no
operator process remains.

## Usage

```bash
# Single session — launches copilot in tmux, auto-attaches
operator --agent=anvil:anvil --yolo

# Autonomous loop mode — restarts on agent signal
operator --loop --name myproject --agent=anvil:anvil

# Named instances for concurrent loops
operator --loop --name project-a --agent=anvil:anvil
operator --loop --name project-b --agent=anvil:anvil

# Management
operator list                        # show running instances
operator stop project-a              # stop one instance
operator stop                        # stop all instances

# Reports
operator report summary              # premium request totals
operator report sessions             # last 20 sessions with details
operator report models               # usage breakdown by AI model
operator report projects             # usage breakdown by project directory
operator report costs                # cost estimates

# Ingest historical logs
operator ingest                      # process unprocessed logs
operator ingest --force              # reprocess all logs
```

## Modes

### Single Session (default)

Launches copilot in a tmux session, auto-attaches your terminal. When copilot exits, metrics are parsed from its process log and stored in the database.

```bash
operator --agent=anvil:anvil --yolo
```

### Loop Mode (`--loop`)

Adds `--yolo --autopilot --no-ask-user` automatically. Sends a preamble that tells the agent:
- It has blanket approval for all decisions
- How to trigger a restart (create a marker file)
- To check for session handoff files on startup

The operator polls for the restart marker. When detected, it captures metrics, restarts copilot, and delivers the same preamble.

```bash
operator --loop --name myproject --agent=anvil:anvil --model=claude-opus-4.6-1m
```

Ctrl+C captures final metrics and shows an aggregate run summary.

### Auto-Continue

Named instances automatically resume where they left off when restarted. Session numbering, run summary scope, and the active Copilot CLI session ID carry over between operator restarts.

State is stored in `~/.operator/restart/{name}.state` and includes the session number, original run start time, and most recently observed Copilot CLI session ID. After a WSL crash or Windows reboot, starting the same named loop again injects `--resume=<session-id>` once so Copilot rejoins the prior CLI session instead of creating a disconnected one.

```bash
# First run — starts at session #1
operator --loop --name myproject --agent=anvil:anvil

# Stop with Ctrl+C, then restart later — continues from session #6
operator --loop --name myproject --agent=anvil:anvil

# Explicitly reset to start fresh
operator --loop --name myproject --fresh --agent=anvil:anvil
```

Unnamed instances (no `--name`) are always ephemeral and don't persist state.

Intentional operator handoffs still start a fresh Copilot CLI session and rely on the handoff file for context. The saved CLI session ID is only reused when the operator process itself is restarted.

### Multi-Instance

Multiple operator instances can run concurrently. Each gets its own multiplexer
session, state files, and supervisor.

- `--name NAME` sets the instance name
- Without `--name`, defaults to the current directory name

Instance names become both session names and filenames, so characters that are
unsafe in either (`. : \ / * ? " < > |`) are replaced with `-`. Because
`a.b`, `a:b` and `a-b` would otherwise collapse onto the same files, a short
digest of the original name is appended whenever sanitizing changes it, and the
same applies to Windows reserved device names such as `CON` and `NUL`. Your
original name is what you type and what `operator list` shows.

```bash
# Start two independent loops
operator --loop --name frontend --agent=anvil:anvil
operator --loop --name backend --agent=anvil:anvil

# See what's running
operator list

# Stop one
operator stop frontend

# Stop all
operator stop
```

`list` and bare `stop` act only on sessions the operator created. A session of
the same name created by anything else is never listed as owned, adopted, or
killed.

### Ownership

A session is *owned* when a claim record written at launch matches a session
that is currently running. Continuity state (`<id>.state`, which deliberately
outlives a session so a named loop can auto-continue) never confers ownership
on its own — otherwise a leftover file could authorize stopping an unrelated
session that happened to take the same name later.

If `operator stop NAME` reports that a running session was not started by this
operator, drop the stale state without touching the session:

```
operator forget NAME
```

## Metrics

The operator stores metrics in `~/.operator/metrics.db` (SQLite). Each session records:

- Premium requests consumed
- API time (seconds)
- Session wall-clock time
- Lines added/removed
- Working directory and git branch
- Per-model token usage

### Log Parser

`operator_ingest.py` parses copilot process logs (`~/.copilot/logs/process-*.log`)
to extract metrics. The session supervisor calls it when Copilot exits, and you
can run it manually with `operator ingest`. It is pure Python — no `sqlite3`,
`grep`, `head` or `tail` binaries required — and reads logs as UTF-8 explicitly
so non-ASCII content does not corrupt parsing on Windows.

When a session's log cannot be matched to the process the supervisor launched,
**no metrics record is written**. Guessing (for example, taking the most recent
log in the directory) is what causes one instance to record another's usage, so
it is deliberately not done. Run `operator ingest` to sweep up any unattributed
logs.

## Files

| Path | Description |
|------|-------------|
| `~/.operator/` | Operator state directory (override with `COPILOT_OPERATOR_HOME`) |
| `~/.operator/metrics.db` | SQLite metrics database |
| `~/.operator/operator.log` | Operator log file |
| `~/.operator/restart/` | Per-instance markers and state |
| `~/.operator/restart/<id>` | Restart signal marker |
| `~/.operator/restart/<id>.state` | Auto-continue state (session number, run start time, Copilot CLI session ID) |
| `~/.operator/restart/<id>.managed` | Ownership record (token, display name, session) |
| `~/.operator/restart/<id>.pid` | PID of the launched Copilot process, while running |
| `~/.operator/restart/<id>.session` | Copilot CLI session UUID |
| `~/.operator/restart/<id>.exit` | Exit code, written after metrics capture |
| `~/.operator/restart/<id>.launch.json` | Launch spec for the session |
| `~/.operator/restart/<id>.runner.log` | Supervisor log for the instance |
| `~/.operator/backups/` | Historical backups of the operator script |
| `~/.copilot/logs/process-*.log` | Copilot process logs (override with `COPILOT_LOG_DIR`) |

> **Note**: Operator state used to live under `~/.copilot/`, but the copilot CLI itself wholesale-deletes `~/.copilot/restart/` on every startup (confirmed via fatrace). State was moved to `~/.operator/` to eliminate the collision. On first run the operator automatically migrates any legacy state from `~/.copilot/` into `~/.operator/`.

## Environment Variables

| Variable | Effect |
|----------|--------|
| `COPILOT_OPERATOR_HOME` | Relocate the operator state directory |
| `COPILOT_OPERATOR_MUX` | Force a specific multiplexer binary |
| `COPILOT_LOG_DIR` | Point at a non-default Copilot log directory |

## Troubleshooting

**"No terminal multiplexer found"**
Install the one for your platform — the error message names the exact command.
On Windows: `winget install --id marlocarlo.psmux`.

**"No instance found" when stopping**
`stop` finds operator-managed sessions via ownership records in
`~/.operator/restart/`. Sessions started outside the operator are deliberately
not listed or stopped; end those yourself.

**"running but was not started by this operator"**
A session with that name exists but carries no matching claim — usually stale
state left by an earlier run whose name has since been reused. Run
`operator forget NAME` to drop the state; the running session is untouched.

**Metrics not captured**
The supervisor attributes logs to the process tree it launched. If Copilot was
started outside the operator, or its log appeared after the startup window, no
record is written for that session. Run `operator ingest` to process any
unprocessed log files.

**"no server running"**
Normal when no sessions are active. Start one with `operator --loop --name myproject`.

**Session name looks different from what I typed**
Unsafe characters are replaced and a digest is appended so distinct names keep
distinct state. `operator list` shows your original name alongside the session
id.

**Can't attach to a session**
Find the name with `operator list`, then: `operator join myproject`
