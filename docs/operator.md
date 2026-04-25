# Operator

The operator is a metrics-capturing wrapper for GitHub Copilot CLI. It wraps `copilot` to capture usage metrics (premium requests, API time, session time, per-model breakdown) into a SQLite database. It supports single-session mode (default) and autonomous loop mode with automatic restarts.

## Prerequisites

- `tmux` — session management
- `sqlite3` — metrics database
- `python3` — log parsing
- `copilot` — GitHub Copilot CLI

## Installation

```bash
# From the copilot-tools repo
./setup.sh

# Or manually
ln -sf /path/to/copilot-tools/operator.sh ~/.local/bin/operator
```

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
- How to trigger a restart (touch a marker file)
- To check for session handoff files on startup

The operator polls for the restart marker. When detected, it captures metrics, restarts copilot, and delivers the same preamble.

```bash
operator --loop --name myproject --agent=anvil:anvil --model=claude-opus-4.6-1m
```

Ctrl+C captures final metrics and shows an aggregate run summary.

### Auto-Continue

Named instances automatically resume where they left off when restarted. Session numbering and run summary scope carry over between operator restarts.

State is stored in `~/.operator/restart/{name}.state` and includes the session number and original run start time.

```bash
# First run — starts at session #1
operator --loop --name myproject --agent=anvil:anvil

# Stop with Ctrl+C, then restart later — continues from session #6
operator --loop --name myproject --agent=anvil:anvil

# Explicitly reset to start fresh
operator --loop --name myproject --fresh --agent=anvil:anvil
```

Unnamed instances (no `--name`) are always ephemeral and don't persist state.

### Multi-Instance

Multiple operator instances can run concurrently. Each gets its own tmux session and restart marker file.

- `--name NAME` sets the instance name
- Without `--name`, defaults to the current directory name (e.g., `~/projects/my-project` → `my-project`)

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

## Metrics

The operator stores metrics in `~/.operator/metrics.db` (SQLite). Each session records:

- Premium requests consumed
- API time (seconds)
- Session wall-clock time
- Lines added/removed
- Working directory and git branch
- Per-model token usage

### Log Parser

`operator-ingest.py` parses copilot process logs (`~/.copilot/logs/process-*.log`) to extract metrics. It's called automatically during session transitions and can be run manually via `operator ingest`.

## Files

| Path | Description |
|------|-------------|
| `~/.operator/` | Operator state directory (override with `COPILOT_OPERATOR_HOME`) |
| `~/.operator/metrics.db` | SQLite metrics database |
| `~/.operator/operator.log` | Operator log file |
| `~/.operator/restart/` | Per-instance restart marker files |
| `~/.operator/restart/*.state` | Auto-continue state (session number, run start time) |
| `~/.operator/run-<instance>.sh` | Per-instance launch script |
| `~/.operator/backups/` | Historical backups of operator.sh |
| `~/.copilot/logs/process-*.log` | Copilot process logs (source data) |

> **Note**: Operator state used to live under `~/.copilot/`, but the copilot CLI itself wholesale-deletes `~/.copilot/restart/` on every startup (confirmed via fatrace). State was moved to `~/.operator/` to eliminate the collision. On first run, `operator.sh` automatically migrates any legacy state from `~/.copilot/` into `~/.operator/`.

## Troubleshooting

**"No instance found" when stopping**
The `stop` command finds operator-managed sessions via `.managed` marker files in `~/.operator/restart/`. If a session was started before this tracking was added, `operator stop` won't find it — use `tmux kill-session -t <name>` directly.

**Metrics not captured**
The operator finds copilot's log by matching the pane PID to `process-*-{pid}.log`. If copilot was restarted outside the operator, the PID won't match. Run `operator ingest` to process any unprocessed log files.

**"tmux: no server running"**
This is normal if no operator sessions are active. Start one with `operator --loop --name myproject`.

**Can't attach to a session**
Find the session name with `operator list`, then: `tmux attach -t myproject`
