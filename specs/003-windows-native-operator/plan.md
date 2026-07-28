# Implementation Plan: Windows-Native Operator

**Branch**: `003-windows-native-operator` | **Date**: 2026-07-27 | **Spec**: [spec.md](./spec.md)

**Status**: Delivered. This records what was built.

## Summary

A single Python implementation replaces the bash operator on all platforms.
Session management is abstracted behind `operator_mux.py` (tmux on POSIX, psmux
on Windows), and every session is supervised by `operator_runner.py` running
inside the multiplexer pane.

The bash scripts are retained unchanged so existing Linux and WSL users are
unaffected.

## Technical Context

**Language/Version**: Python 3.10+ (CI: 3.10 and 3.12)

**Primary Dependencies**: Standard library only at runtime — `sqlite3`,
`subprocess`, `argparse`, `json`, `re`, `pathlib`, `signal`, `ctypes`. External
processes: `copilot`, `git`, and a multiplexer. `pytest` for development.

**Storage**: SQLite at `~/.operator/metrics.db` via stdlib `sqlite3`, WAL
enabled with a busy timeout for concurrent instances.

**Target Platform**: Windows 10/11, Linux, WSL, macOS.

**Constraints**: No runtime dependency outside the standard library. The
`sqlite3` binary is not required on any platform. No Linux regression.

## Architecture

```
operator (copilot_operator.py)
   │  writes {id}.launch.json
   ▼
operator_mux.py ── new-session -d -s ID -c CWD -- python operator_runner.py SPEC
   │
   ▼  (inside the pane, survives detach)
operator_runner.py
   ├── spawns copilot, records the process tree
   ├── pins the telemetry log for that tree
   ├── writes {id}.pid / {id}.session
   ├── waits for exit
   ├── ingests metrics via operator_ingest.py
   └── writes {id}.exit
```

### Why a supervisor

Two defects made direct launching unworkable:

1. **Process identity.** The bash version depended on `exec copilot` making the
   pane PID equal Copilot's PID — the key used to find its telemetry log.
   Windows has no `exec`, and the launcher is often a *shim* (WinGet's
   `copilot.exe`, virtualenv's `python.exe`) that re-execs the real binary under
   a different PID. Measured live: launcher pid 80864, log written by pid 70976.
   The runner matches against the whole process tree it created.
2. **Supervision across detach.** The operator previously exited after detach,
   so the promised "metrics captured when copilot exits" never happened. The
   runner lives in the pane and outlives detach.

### Module responsibilities

| Module | Responsibility |
|---|---|
| `copilot_operator.py` | CLI, instance lifecycle, loop mode, reports |
| `operator_mux.py` | Backend selection, session verbs, safe naming |
| `operator_runner.py` | In-pane supervision, process tree, metrics capture |
| `operator_ingest.py` | Log parsing, SQLite storage |
| `operator_console.py` | UTF-8 console output on Windows |
| `handoff_tool.py` | Handoff file + restart signal |
| `setup_tools.py` | Cross-platform install |

## Key decisions

**Instance identity.** Names become both session names and filenames.
`safe_instance_id` replaces unsafe characters and appends a digest whenever
sanitizing changes the name, so `a.b`, `a:b` and `a-b` cannot share state.
Windows reserved device names get the same treatment. The display name is
preserved in the ownership record.

**Silent-failure detection.** psmux returns exit 0 while creating nothing for a
name containing `:`. `new_session` verifies with `has_session` and raises,
converting a success-shaped failure into a loud one.

**Ownership.** The `.managed` marker is a JSON record with a token, display
name, session and pid, written atomically. `list` and `stop` act only on
records the operator wrote; a foreign session sharing a name is never touched.

**No guessing on attribution.** If the telemetry log cannot be matched to the
launched process tree, no metrics record is written. Falling back to "newest log
in the directory" is what allows concurrent instances to steal each other's
usage.

**Signals.** Handlers only set a flag; the main loop performs the shutdown
sequence, so no blocking work runs in a handler.

**Installation.** Console scripts via `pyproject.toml`, installed by
`setup_tools.py`. Extensions link with directory junctions on Windows, which
need neither Developer Mode nor elevation, falling back to a refreshed copy.

## Verification

- 162 automated tests; 8 drive a real psmux session.
- CI: Ubuntu / Windows / macOS x Python 3.10, 3.12, each with a real
  multiplexer installed so integration tests execute rather than skip.
- Manual Windows 11 verification of loop mode, restart, resume after a killed
  operator, reports, `list`/`stop`, foreign-session isolation, and `handoff`.
- `git diff main -- operator.sh handoff.sh operator-ingest.py` is empty,
  proving the legacy path is untouched.

## Complexity Tracking

| Decision | Why | Alternative rejected because |
|---|---|---|
| Bash and Python coexist | Deleting the working Linux path in the same change that introduces its replacement would make any defect a total outage | Immediate deletion removes the fallback exactly when it is least proven |
| Separate runner process | Only way to know Copilot's real PID and to capture metrics after detach | Launching Copilot directly cannot satisfy either on Windows |
| Digest-suffixed instance ids | Prevents distinct names sharing state files | Plain sanitizing silently merges `a.b`, `a:b`, `a-b` |
