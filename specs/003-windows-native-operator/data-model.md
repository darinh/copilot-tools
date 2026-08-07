# Data Model

**Feature**: `003-windows-native-operator`

Concrete representation of the entities in [spec.md](./spec.md). Formats are unchanged from the current
bash implementation so that an existing `~/.operator/` directory stays readable — only the code that
reads and writes them changes.

## Path resolution

All state lives under a single root, overridable by `COPILOT_OPERATOR_HOME`:

| Symbol | Default (POSIX) | Default (Windows) |
|---|---|---|
| `OPERATOR_HOME` | `$HOME/.operator` | `%USERPROFILE%\.operator` |
| `RESTART_DIR` | `$OPERATOR_HOME/restart` | `%OPERATOR_HOME%\restart` |
| `METRICS_DB` | `$OPERATOR_HOME/metrics.db` | `%OPERATOR_HOME%\metrics.db` |
| `LOG_FILE` | `$OPERATOR_HOME/operator.log` | `%OPERATOR_HOME%\operator.log` |
| `BACKUPS_DIR` | `$OPERATOR_HOME/backups` | `%OPERATOR_HOME%\backups` |
| `COPILOT_LOG_DIR` | `$HOME/.copilot/logs` | `%USERPROFILE%\.copilot\logs` |

Resolution uses `pathlib.Path.home()`, which honors `USERPROFILE` on Windows. State is **not** shared
with a WSL installation, which has its own `$HOME` (spec Assumptions).

State must never live under `~/.copilot/`: the Copilot CLI wholesale-deletes `~/.copilot/restart/` on
startup (FR-016).

## Operator instance

The central entity. Identified by a sanitized name; defaults to the current directory name.

**Name rule**: `.` and `:` are replaced with `-` before any use. Required for correctness — psmux
creates no session at all for a name containing `:` while reporting success
([mux-backend.md](./contracts/mux-backend.md)).

Files per instance `{name}`:

| File | Meaning | Lifetime |
|---|---|---|
| `restart/{name}` | Restart signal | Created by agent/handoff, deleted by operator on observe |
| `restart/{name}.state` | Persisted state | Survives operator restarts; deleted by `stop` |
| `restart/{name}.managed` | Ownership marker | Created at launch; deleted at shutdown |
| `restart/{name}.pid` | Copilot's real process ID | Written by the launch script; replaced each session |
| `run-{name}.sh` / `run-{name}.ps1` | Generated launch script | Recreated per session; deleted at shutdown |

An instance is **operator-managed** if `restart/{name}.managed` or `restart/{name}.state` exists.
`list` and bare `stop` act only on managed instances (FR-006).

## Instance state record

`restart/{name}.state` — line-oriented `KEY=VALUE`, unchanged from bash:

```
SESSION_NUM=7
RUN_STARTED=2026-07-27T14:03:11Z
COPILOT_SESSION_ID=3f2a9c1e-...
```

| Key | Type | Meaning |
|---|---|---|
| `SESSION_NUM` | int | Last started session number. Next run starts at `SESSION_NUM + 1`. |
| `RUN_STARTED` | ISO-8601 UTC | Original run start, scoping the aggregate summary. |
| `COPILOT_SESSION_ID` | UUID, optional | Last observed Copilot CLI session. Omitted when unknown. |

**Rules**

- Written only for named instances; unnamed instances are ephemeral.
- `--fresh` ignores an existing record and restarts numbering at 1.
- `COPILOT_SESSION_ID` is consumed **exactly once**: injected as `--resume=<id>` on the first session
  after an operator restart, then cleared. It is suppressed when the user already passed a session
  argument (FR-012).
- Values are validated on load — a non-UUID `COPILOT_SESSION_ID` is discarded rather than passed to
  Copilot.

### State transitions

```
absent ──start named loop──▶ SESSION_NUM=1
   ▲                              │
   │                       restart signal
   │                              ▼
   └────── stop ──────  SESSION_NUM=n+1 (COPILOT_SESSION_ID cleared)

SESSION_NUM=n ──operator restart──▶ resume at n+1 with --resume=<id> (once)
SESSION_NUM=n ──--fresh──▶ SESSION_NUM=1
```

## Restart signal

`restart/{name}` — an empty marker file; only existence matters. Created by `handoff` or directly by the
agent; polled every 10s by loop mode; deleted by the operator when observed and before each launch.

Cross-platform creation (FR-022):

| Shell | Command |
|---|---|
| bash | `touch ~/.operator/restart/{name}` |
| PowerShell | `New-Item -ItemType File -Force ~/.operator/restart/{name}` |

## Ownership marker

`restart/{name}.managed` — empty file created after a successful session launch, distinguishing operator
sessions from unrelated sessions with the same name (FR-006).

The bash implementation snapshots sibling markers before launch and restores them if `RESTART_DIR`
vanishes mid-launch — defensive scaffolding from when state lived under `~/.copilot/`. Restoration is
gated on the session still being live. This behavior is preserved.

## Metrics database

SQLite at `METRICS_DB`, accessed through Python's stdlib `sqlite3`. Schema is unchanged, so existing
databases work without migration.

### `sessions`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `session_num` | INTEGER NOT NULL | Loop session number; `0` for no-op records |
| `log_file` | TEXT **UNIQUE** | The log's full path (`operator_ingest.log_key`). Drives idempotent upsert. |
| `log_file_mtime` | TEXT | Skip-if-unchanged marker for ingest |
| `no_op` | INTEGER NOT NULL DEFAULT 0 | `1` when the log carried no usage data |
| `started_at` / `ended_at` | TEXT NOT NULL | ISO-8601 UTC |
| `work_dir`, `git_branch` | TEXT | |
| `nano_aiu` | INTEGER NOT NULL DEFAULT 0 | **Billionths of an AI credit** — the current billing signal |
| `tokens_input`, `tokens_cache_read`, `tokens_cache_write`, `tokens_output` | INTEGER NOT NULL DEFAULT 0 | Token counts by type |
| `premium_requests` | INTEGER | Legacy request-based billing, retained for annual plans |
| `api_time_seconds`, `session_time_seconds` | INTEGER | NULL when unmeasured — see below |
| `lines_added`, `lines_removed` | INTEGER | NULL when unmeasured — see below |
| `raw_metrics` | TEXT | Rendered human-readable summary |

Those four INTEGER columns are nullable on purpose. They are supplied only by the
`session_shutdown` telemetry event, which current Copilot CLI versions usually do not write to the
process log. Recording `0` there would be a fabricated measurement — "this session changed no code"
would be indistinguishable from "nobody measured it", and every average over the column would be
dragged toward zero by sessions that were never observed. Ingest writes NULL when the event is
absent, `raw_metrics` says `unknown` rather than `0s`/`+0 -0`, and both report renderers
(`copilot_operator.py` and `operator.sh report sessions`) display `—`. `SUM()` skips NULLs, so
aggregates report totals over measured sessions only.

`backfill_unknown_metrics.py` repairs rows written before this rule existed, clearing zeros only
for sessions whose log genuinely lacks a shutdown event. Rows whose log has since been deleted are
skipped unless `--missing-logs` is passed: an all-zero row claims a zero session duration, which no
measured session has, so the row is fabricated on its own evidence even with the log gone.

The legacy bash ingester `operator-ingest.py` is deliberately unchanged. It refuses a log with no
shutdown event (exit 2, recorded as `no_op`), so it cannot produce the fabricated zeros in the first
place. `operator.sh`'s report *is* updated, because a rollback to the bash entry point reads the same
database and would otherwise redisplay NULLs as `+0 -0`.

### `model_usage`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `session_id` | INTEGER NOT NULL REFERENCES sessions(id) | |
| `model_name` | TEXT NOT NULL | |
| `tokens_in`, `tokens_out`, `tokens_cached` | TEXT | Pre-formatted (`1.2M`, `340k`) |
| `nano_aiu` | INTEGER NOT NULL DEFAULT 0 | AI credits for this model, in billionths |
| `premium_requests` | INTEGER | Legacy billing, retained |

Rows are deleted and reinserted per session on reprocessing.

### Access rules

- The `sqlite3` **binary** is no longer used. Stdlib `sqlite3` replaces it on every platform.
- All statements use **parameter binding**, not string interpolation. The bash and current Python
  implementations interpolate escaped strings; log-derived values (model names, work dirs) reach these
  statements, so binding is required.
- Concurrent instances share one database, so connections set a **busy timeout** and the ingest path is
  idempotent via the `log_file` unique constraint (spec Edge Cases).

`log_file` holds a full path rather than a basename, and the constraint above is why. Copilot names a
process log after a timestamp and a pid, so the same name recurs in any second log directory — logs
copied from another machine, a restored backup, a moved `COPILOT_LOG_DIR`. Under a basename key the
`UNIQUE` constraint merged the two sessions: with equal mtimes the second log returned
`SKIP (already processed)` and was never recorded, and with differing mtimes the upsert overwrote the
first session's row and deleted its `model_usage` rows. The column is written and read through one
helper, `operator_ingest.log_key`, because `copilot_operator.manage_logs` decides from this value
whether a log has been captured and may be deleted.

Rows written before that change hold a basename. `operator_ingest._adopt_legacy_row` re-keys such a
row onto the full path the next time its log is ingested, rather than inserting a second row — which
would count every historical session twice. It re-keys only when the row's `started_at` equals the
start time parsed from the log's first line: a basename is exactly the ambiguity being removed, so
adopting on the name alone would let a log take over a different session's row and the upsert that
follows would delete that session's `model_usage` rows. `log_file_mtime` is deliberately not accepted
as a second witness — it records only when a file stopped changing, to the second, and two
same-basename logs sharing an mtime is the very collision this design removes. When the evidence does
not match, the legacy row is left alone and the log gets a row of its own: a duplicate count is a
wrong number, but overwriting the older row is a wrong number and the loss of the only record that
could correct it. Adoption runs after the log is parsed, so the write transaction it opens is not
held across the read. Rows whose log is gone keep their basename; both
`manage_logs` and `backfill_unknown_metrics` still resolve them, the latter because joining a
directory onto an absolute path yields the absolute path, so one expression reads both spellings.
The schema itself is unchanged, so the legacy bash ingester's `ON CONFLICT(log_file)` still applies.

## Project catalog entry

`~/.operator/projects/catalog.csv` — two columns, optionally quoted:

```csv
"C:\Users\dev\repos\my-app",EXAMPLE1-1111-1111-1111-111111111111
"/home/dev/repos/my-app",EXAMPLE2-2222-2222-2222-222222222222
```

The GUIDs shown are deliberately invalid, matching `templates/copilot-instructions.md`: an agent
reads both files as instructions, and a realistic value beside a write instruction is one that gets
copied into the user's real catalog.

Lookup normalizes the project root and compares. On Windows the comparison is **case-insensitive** and
separator-normalized; on POSIX it is case-sensitive. A catalog written on one platform stores that
platform's paths; cross-platform sharing is out of scope.

Resolving a GUID yields `~/.operator/projects/{guid}/`, where `next-session.md` is written.

## Handoff file

`~/.operator/projects/{guid}/next-session.md`, written with explicit UTF-8. Sections in fixed order;
optional sections are omitted entirely rather than emitted empty:

```markdown
# Session Handoff

## Status
...
## In Progress      (optional)
## Next Steps
...
## Context          (optional)
## Prompt           (optional)
```

Ephemeral: the next agent reads it once and deletes it.

## Copilot process log

`~/.copilot/logs/process-*.log`, read-only input owned by the Copilot CLI. Named
`process-{startTimeMs}-{pid}.log`, where `{pid}` is **Copilot's own** process ID.

### Attribution

The PID used for matching comes from `restart/{name}.pid`, written by the launch script, **not** from
the multiplexer's pane PID. On POSIX the two are identical because the run script `exec`s Copilot; on
Windows they are not — the pane PID is the multiplexer's own shell, two levels above Copilot
([research.md](./research.md) §R5.1).

When the PID file is missing, the operator records **no** metrics for that session and logs a warning.
It must not fall back to "most recently modified log", which silently misattributes one instance's usage
to another when instances run concurrently.

Files are opened with **explicit UTF-8 and lenient error handling** — the default encoding on Windows is
locale-dependent and silently corrupts non-ASCII content, which then fails JSON parsing and discards the
session's metrics entirely.

### What the parser reads

Since the 2026-06-01 billing change, usage is metered in **AI credits**. Each chat-completion response
body carries:

```json
"copilot_usage": {
  "token_details": [
    {"batch_size": 1000000, "cost_per_batch": 500000000000,
     "token_count": 2, "token_type": "input"}
  ],
  "total_nano_aiu": 20242875000
}
```

`total_nano_aiu` is billionths of an AI credit and is authoritative; the parser sums it across every
response in the session. `1 AI credit = $0.01 USD`.

**These bodies are only written at debug log level.** At the default level the process log contains no
usage data at all, so the operator appends `--log-level debug` when launching Copilot
(`COPILOT_OPERATOR_NO_DEBUG_LOG=1` opts out).

For legacy request-billed accounts the parser still reads the `session_shutdown` event and sums
`assistant_usage` cost fields, because `session_shutdown.total_premium_requests` reports only the last
call's cost. A log is recorded when it carries **either** signal; current Copilot versions no longer
write a `session_shutdown` payload to the process log, so requiring one would discard every modern
session.
