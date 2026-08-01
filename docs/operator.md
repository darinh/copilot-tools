# Operator

The operator is a metrics-capturing wrapper for GitHub Copilot CLI. It wraps `copilot` to capture usage metrics (AI credits, tokens, API time, session time, per-model breakdown) into a SQLite database. It supports single-session mode (default) and autonomous loop mode with automatic restarts.

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

# Start a loop without attaching (one agent starting another agent's loop)
operator --loop --headless --name project-c --agent=anvil:anvil

# Messaging between instances
operator send --from project-a --to project-b "schema is frozen"
operator inbox                       # read this directory's instance mail
operator inbox project-b --peek      # read without marking read
operator inbox project-b --history   # already-delivered messages

# Management
operator                             # interactive menu (no args)
operator list                        # show running instances
operator stop project-a              # stop one instance (loop + session)
operator stop-loop project-a         # stop only the background supervisor
operator restart-loop project-a      # replace the supervisor, keep the session
operator stop-session project-a      # stop only the Copilot session
operator stop                        # stop all instances

# Reports
operator report summary              # AI credit totals
operator report sessions             # last 20 sessions with details
operator report models               # usage breakdown by AI model
operator report projects             # usage breakdown by project directory
operator report costs                # cost estimates
operator report tokens               # token counts by type

# Ingest historical logs
operator ingest                      # process unprocessed logs
operator ingest --force              # reprocess all logs

# Windows Terminal tab tracking and restore (see below)
operator tabs list                   # show tracked tabs
operator restore                     # pick which tracked tab(s) to reopen
operator restore --all               # reopen every tracked tab, resuming sessions
operator restore myproject           # reopen one tracked tab by name
operator restore --all --dry-run     # preview without launching anything
```

## Modes

### Single Session (default)

Launches copilot in a tmux session, auto-attaches your terminal. Always runs with `--yolo`. When copilot exits, metrics are parsed from its process log and stored in the database.

```bash
operator --agent=anvil:anvil
```

### Loop Mode (`--loop`)

Adds `--yolo --autopilot --no-ask-user` automatically. Sends a preamble that tells the agent:
- It has blanket approval for all decisions
- How to trigger a restart (create a marker file)
- To check for session handoff files on startup

`operator --loop` never occupies your invoking terminal with raw supervisor
logs. It spawns the supervisor as a detached background process, waits for
the Copilot session to come up, then attaches your current tab to that
session — the same as running `operator join NAME` right afterward. The
supervisor keeps running in the background even after you detach (`Ctrl-b d`
in tmux/psmux, or just closing the tab), watching for restart signals and
unexpected crashes. If you rerun `operator --loop --name X` while a
supervisor for `X` is already running, it skips spawning a new one and just
attaches you to the existing session.

The supervisor polls for the restart marker. When detected, it captures
metrics, restarts copilot, and delivers the same preamble. If the Copilot
session disappears **without** a restart/detach/stop marker present (an
unexpected crash, an OOM kill, a killed pane, etc.), the supervisor treats
this the same as a restart request and relaunches automatically, up to a
bounded number of consecutive failures — the loop keeping a project running
unattended is the whole point of `--loop`.

```bash
operator --loop --name myproject --agent=anvil:anvil --model=claude-opus-4.6-1m
```

Ctrl+C (from an attached pane) captures final metrics and shows an aggregate run summary.

### Loop vs. session: stopping just one

Loop mode has two independent lifecycles: the background supervisor and the
Copilot session it's watching. Plain `operator stop NAME` stops both. To
control them independently:

```bash
operator stop-loop NAME     # stop only the supervisor; session keeps running
                             # re-attach any time with `operator join NAME`
operator restart-loop NAME  # replace the supervisor with a fresh one, leaving
                             # the session running
operator stop-session NAME  # stop only the session; if the supervisor is
                             # still running it relaunches a fresh one shortly
operator stop NAME          # stop both, cleanly, with no relaunch
```

This is useful when you want to keep working in a session by hand for a bit
without the supervisor auto-restarting it out from under you (`stop-loop`),
or you want to force a clean restart of just the Copilot process while
leaving the crash-recovery supervisor in place (`stop-session`).

### Picking up a new operator version

The supervisor is a long-lived Python process that imported the operator's
code when it started. Updating the operator therefore does **not** affect
supervisors that are already running — they keep executing the code they
started with, and only newly started ones pick up the change.

`operator restart-loop NAME` is how a running instance adopts a new version
without losing its work:

```bash
git pull                     # or however the operator gets updated
operator restart-loop alpha  # alpha's supervisor is replaced; its session
                             # keeps running, uninterrupted
```

The old supervisor is asked to detach (the same mechanism as `stop-loop`, so
the session is never touched), and the replacement *adopts* the running
session rather than launching a new one — the Copilot process keeps its pid,
its scrollback and its context. Since the adopted session is the one already
running, it keeps its session number too.

`restart-loop` refuses rather than guess when it cannot do this safely:

| Situation | Behaviour |
|---|---|
| No session running | Refuses — there is nothing to keep alive |
| Session not owned by this operator | Refuses — it belongs to someone else |
| No recorded loop arguments | Refuses — the replacement would differ from the original; it tells you the manual `stop-loop` + `--adopt` commands instead |
| Supervisor does not stop in time | Aborts without spawning, leaving the session and its old supervisor alone |
| No supervisor running (e.g. after `stop-loop`) | Starts one that adopts the session |

### Interactive menu

Running `operator` with no arguments at all opens an interactive menu instead
of starting a session:

```
═══ Copilot Operator ═══

  1) Sessions — inspect, join or stop  (2 running)
  2) Restore tabs (pick which)
  3) Restore all tracked tabs
  4) View usage report
  5) Exit
```

The menu stays open until you choose Exit, so one action is not the end of the
program.

**Sessions** lists everything that is running and lets you pick one rather than
retyping a name the operator already knows:

```
═══ Running Operator Instances ═══

  1) my-project  ·  looping · session #7  ·  up 10h 45m  ·  ~/repos/my-project
  2) scratch     ·  single session · no loop  ·  up 12m  ·  ~/repos/scratch

Instance [1-2] (blank to cancel):
```

Picking one shows its stats and only the actions that apply to its current
state:

```
═══ my-project ═══

  Status            looping · session #7
  Running for       10h 45m (since 2026-07-30T09:00:00Z)
  Loop session      #7
  Loop supervisor   pid 49532
  Session           my-project — live
  Copilot pid       51204
  Copilot session   3f2a1b4c-5d6e-7f80-9a1b-2c3d4e5f6071
  Directory         ~/repos/my-project
  Usage             41 recorded session(s) · 812.4 credits · $32.50
  Command           --yolo --autopilot --effort high -i <preamble>

  1) Join this session (attach the terminal)
  2) Stop the loop, leave the session running
  3) Stop the session, let the loop restart it
  4) Stop everything (loop and session)
  5) Refresh
  6) Back
```

A loop that is between sessions still appears here, because that is exactly
when you may want to stop it. Stop options are only offered for things that
are actually running, and after a stop the view re-reads state rather than
offering actions against something already gone.

`operator list` prints the same one-line summaries non-interactively, for
scripts and for a quick glance.

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

If a named loop resumes with a saved CLI session ID but finds **no handoff file** for the project (`~/.copilot/projects/{guid}/next-session.md`, resolved from `~/.copilot/projects/catalog.csv`), that almost always means the previous session ended without calling `handoff` — most likely a crash. The preamble gets an extra note in that case telling the agent this looks like crash recovery and, if it *did* mean to stop cleanly, to remember to write a handoff next time. Resuming after a clean `handoff`-triggered restart (or any run where the handoff file already exists) never adds this note.

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

### Telling a looping tab from an idle one

The tab title is set to `operator - NAME` (or `terminal - NAME` when you
join), and the operator also emits the OSC `9;4` progress sequence, which
Windows Terminal and ConEmu draw as a ring on the tab itself:

| Tab shows | Meaning |
|---|---|
| Animated indeterminate ring | Loop mode — the agent is running unattended |
| Steady ring | A single interactive session, or a join |
| No ring | Nothing running in that tab |

The animation is the closest a terminal gets to an animated tab icon:
custom icons (`"icon"` in a Windows Terminal profile) are static images
only, and are per-profile rather than per-tab, so they cannot reflect state.
Terminals that do not implement the sequence ignore it. Inside tmux the
sequence is sent through tmux's DCS passthrough, which requires
`set -g allow-passthrough on` (tmux 3.3+) to reach the outer terminal. Set
`OPERATOR_NO_TAB_PROGRESS=1` to turn the ring off entirely.

### Restoring tabs after a reboot or crash

Windows Terminal (and most terminal apps) expose no API to list their own
tabs, so the operator keeps its own record. Whenever a named instance
(`--name`/`--loop`) is started inside a Windows Terminal tab (detected via
`$WT_SESSION`), it upserts an entry in `~/.operator/tabs.json` recording the
working directory and the exact `operator` command line used. `operator stop`
and `operator forget` remove the entry again, since those are intentional
shutdowns you don't want replayed.

```bash
# Inspect what's tracked
operator tabs list

# Drop one entry without touching any session
operator tabs remove myproject

# Drop everything
operator tabs clear
```

After a reboot or crash every multiplexer server and Copilot process is gone,
so there is nothing left to reattach to. `operator restore` needs Windows
Terminal reachable on PATH (`wt.exe`), so run it from native Windows
PowerShell **or from within a WSL distro** (Windows interop must be enabled).
It reads the local machine's registry plus every installed WSL distro's
registry (queried live via `wsl.exe -d <distro> -- cat
~/.operator/tabs.json`, so it works regardless of that distro's `$HOME` layout
or `COPILOT_OPERATOR_HOME`), then opens a single Windows Terminal window with
a tab per selected instance, replaying each command line. When run from
inside WSL, only this machine's WSL registries are visible — restore can't
see a native Windows-side registry from there, and vice versa; each side only
knows about its own tracked tabs. The operator's existing auto-continue logic
(session numbering + saved `--resume=<uuid>`) takes it from there, so each
Copilot session resumes rather than starting over.

```bash
# List tracked tabs and choose which to restore
operator restore

# Restore every tracked tab without prompting
operator restore --all

# Restore one or more specific tabs by name
operator restore myproject other-project

# Preview any of the above without launching anything
operator restore --all --dry-run
```

WSL-based instances are relaunched as `wsl.exe -d <Distro> --cd <path> --
bash -lic "operator ..."` inside their own tab; native instances are relaunched
as `powershell -NoExit -Command "operator ..."` in the recorded directory.

## Parallel agents and messaging

An operator instance is a **full Copilot CLI in its own process**, not a
sub-agent of whoever started it. Two instances share nothing — not context, not
a working directory, not a git index — so they need a way to talk.

### Starting one headlessly

```bash
operator --loop --headless --name payments-api --agent anvil
```

`--headless` (synonym: `--detached`) starts the background loop supervisor and
returns instead of attaching. Without it, `operator --loop` takes over the
calling terminal with a full-screen TUI, which makes it unusable for an agent
starting another agent's loop. It still waits for the session to come up and
exits non-zero if it never does, so a caller that never attaches still learns
about a failed launch.

Give each instance its own directory — ideally its own repository. Instance
names, handoff files and git state are all keyed to the working directory, and
two loops in one tree fight over the index and each other's uncommitted work.
Pointing two agents at one project is possible but unenforced: nothing prevents
an agent from editing outside its assigned area.

### Messaging

```bash
operator send --from alpha --to beta "the schema is frozen"
operator send --from alpha --to beta --queue "pick this up at a break"
operator send --from alpha --to gamma --force "for an instance not started yet"

operator inbox                    # this directory's instance, marks read
operator inbox beta --peek        # leave them unread
operator inbox beta --history     # already-delivered messages
operator inbox beta --json        # machine-readable
```

`--from` and `--to` are both required. The recipient cannot see who is running
what, so an unattributed message is one it cannot answer; every delivered
message carries a ready-made reply command. A `--to` naming no known instance
is refused and the known names are listed, because a typo would otherwise queue
a message into a mailbox nobody ever reads.

**Delivery.** If the recipient's Copilot process is running, the message is
typed into its session and lands immediately. If it is between sessions (or the
pane is dead), the message is queued and handed over in the next session's
preamble. `--queue` forces the queued path.

Queued mail is read when a session launches but only archived once that session
is actually up, so a launch that fails and retries does not swallow it. Live
delivery uses literal key input: without that, a message containing the word
`Enter` would submit early and one containing `C-c` would interrupt the
recipient.

Messages live in `~/.operator/messages/<instance>/inbox/` and move to
`../archive/` when read — read mail is archived rather than deleted, so
`--history` is a genuine audit trail of what agents told each other.

## Metrics

The operator stores metrics in `~/.operator/metrics.db` (SQLite). Each session records:

- AI credits consumed (and tokens by type)
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
| `~/.operator/tabs.json` | Tracked terminal tabs, used by `operator restore` |
| `~/.operator/messages/<id>/inbox/` | Undelivered messages for an instance |
| `~/.operator/messages/<id>/archive/` | Messages already delivered, kept as an audit trail |
| `~/.operator/backups/` | Historical backups of the operator script |
| `~/.copilot/logs/process-*.log` | Copilot process logs (override with `COPILOT_LOG_DIR`) |

> **Note**: Operator state used to live under `~/.copilot/`, but the copilot CLI itself wholesale-deletes `~/.copilot/restart/` on every startup (confirmed via fatrace). State was moved to `~/.operator/` to eliminate the collision. On first run the operator automatically migrates any legacy state from `~/.copilot/` into `~/.operator/`.

## Environment Variables

| Variable | Effect |
|----------|--------|
| `COPILOT_OPERATOR_HOME` | Relocate the operator state directory |
| `COPILOT_OPERATOR_MUX` | Force a specific multiplexer binary |
| `COPILOT_LOG_DIR` | Point at a non-default Copilot log directory |
| `COPILOT_OPERATOR_NO_DEBUG_LOG` | Don't add `--log-level debug`; disables usage capture |


## Billing and AI credits

GitHub replaced premium requests with **AI credits** on 2026-06-01. Usage is
metered on token consumption — input, output, cache-read and cache-write
tokens are each priced per model — and **1 AI credit = $0.01 USD**.

The operator records, per session:

- `nano_aiu` — billionths of an AI credit, as reported by Copilot
- token counts split by type (`input`, `cache_read`, `cache_write`, `output`)
- `premium_requests` — retained for legacy annual plans, which still bill that way

`operator report costs` prices AI credits at $0.01 and falls back to
$0.04/premium request for sessions recorded before the change, so historical
totals stay meaningful.

> **Why the operator forces debug logging.** Copilot reports usage in its
> chat-completion response bodies (`copilot_usage.total_nano_aiu`), and those
> bodies are only written at debug log level. At the default level the process
> log contains no usage data at all — a session's metrics would silently be
> empty. The operator therefore appends `--log-level debug` when launching
> Copilot. This makes logs substantially larger; set
> `COPILOT_OPERATOR_NO_DEBUG_LOG=1` to opt out and forgo usage capture.

### Managing log growth

Debug logs are substantially larger and Copilot does not rotate them. Inspect
and reclaim the space with:

```
operator logs                       # file count and total size
operator logs --prune --days 30     # remove ingested logs older than 30 days
```

Pruning removes only logs that have **already been ingested**, so recorded
usage is never lost. Run `operator ingest` first to capture anything
outstanding; logs that are still unprocessed are kept and reported.

Copilot itself exposes usage interactively via `/usage`, `/statusline` and the
exit summary, and `copilot help billing` documents the model.

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

**"operator restore needs Windows Terminal (wt.exe)"**
`restore` shells out to `wt.exe`, which only exists on Windows. It's reachable
from native Windows PowerShell and from within WSL (via Windows interop), but
not from plain Linux or macOS. On those platforms, just start the loop
directly (`operator --loop --name myproject ...`) — its own auto-continue
logic will resume the prior session. If you're in WSL and still see this
error, check that interop is enabled (`/etc/wsl.conf`: `[interop]
enabled=true`, `appendWindowsPath=true`) so `wt.exe`/`wsl.exe` resolve on PATH.

**`operator restore` reopens nothing**
Only named instances (`--name`/`--loop`) started inside a Windows Terminal tab
are tracked (`$WT_SESSION` must be set). Check `operator tabs list`; unnamed,
ad-hoc `copilot`/`operator` runs are intentionally not tracked.
