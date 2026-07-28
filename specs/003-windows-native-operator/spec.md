# Feature Specification: Windows-Native Operator

**Feature Branch**: `003-windows-native-operator`

**Created**: 2026-07-27

**Status**: **Delivered**

## Summary

The operator, handoff tool, log parser and setup now run natively on Windows,
Linux, WSL and macOS from a single Python implementation. Session management
goes through a backend abstraction that selects `psmux` on Windows and `tmux`
elsewhere, and each session is supervised by a process running inside the
multiplexer pane.

The original bash scripts remain unchanged, so existing Linux and WSL users are
unaffected.

## User Scenarios & Testing

### User Story 1 - Run a Copilot session natively on Windows (Priority: P1)

**Delivered.** `operator --name foo` starts a Copilot session in a psmux session
on Windows, attaches the terminal, and records usage metrics when the session
ends.

**Acceptance Scenarios**

1. **Given** Windows with psmux installed, **When** the developer runs the
   operator, **Then** a session starts, is attachable, and survives detaching. ✅
2. **Given** a running session, **When** Copilot exits, **Then** metrics are
   recorded — *including when the user detached first*. ✅
3. **Given** a working directory containing spaces, **When** a session starts,
   **Then** the path is preserved exactly. ✅
   (`test_working_directory_with_spaces_is_preserved`)

### User Story 2 - Autonomous loop mode and handoff on Windows (Priority: P2)

**Delivered.** Verified end to end on Windows 11: session #1 launched, restart
signal detected, `/exit` delivered, metrics captured
(`1 premium, 5s api, +3 -1`), session #2 launched with a fresh CLI session id.

1. Handoff writes the file and raises the restart marker. ✅
2. Restart captures metrics and starts the next numbered session. ✅
3. After the operator process is killed, restarting the same named loop resumes
   at the next session number and injects `--resume=<uuid>` exactly once. ✅
4. Interrupting the operator captures final metrics and shuts down cleanly. ✅

### User Story 3 - Manage and inspect instances on Windows (Priority: P2)

**Delivered.** `list`, `join`, `stop NAME`, `stop`, `reload` and all five report
types work. Verified that a foreign session with a colliding name is never
listed, adopted, or killed.

### User Story 5 - Install the toolkit on Windows (Priority: P3)

**Delivered.** `python setup_tools.py` verifies prerequisites, installs the
console scripts, links extensions using directory junctions (no elevation
required), and installs templates without silently overwriting edited files.

## Requirements

All requirements from the original combined specification are met.

| Requirement | Status | Evidence |
|---|---|---|
| FR-001 Native Windows, no POSIX layer | ✅ | Pure Python + psmux |
| FR-002 Persistent detachable session | ✅ | `test_session_survives_the_creating_process` |
| FR-003 Liveness detection | ✅ | Supervisor exit marker + backend query |
| FR-004 Graceful exit then force | ✅ | `stop_session_gracefully` |
| FR-005 Attach / detach | ✅ | `operator join` |
| FR-006 Only own sessions | ✅ | Ownership records; verified live |
| FR-007 Spaces / quotes / non-ASCII | ✅ | argv passed after `--`; UTF-8 I/O |
| FR-008 Full command surface | ✅ | `contracts/cli-surface.md` |
| FR-009 Metrics captured | ✅ | Supervisor ingests on exit |
| FR-010 All report types | ✅ | Verified against live data |
| FR-011 Loop mode | ✅ | Verified end to end |
| FR-012 State + resume once | ✅ | Verified after simulated crash |
| FR-013 Handoff | ✅ | `handoff_tool.py`, verified live |
| FR-014 Concurrent instances | ✅ | `test_concurrent_instances_do_not_cross_contaminate` |
| FR-015 Interrupt handling | ✅ | Signal sets a flag; main loop shuts down |
| FR-016 State root, relocatable | ✅ | `COPILOT_OPERATOR_HOME` |
| FR-017 No Linux regression | ✅ | Bash scripts untouched; `bash -n` clean |
| FR-018 Actionable missing dependency | ✅ | `test_missing_backend_names_platform_install_command` |
| FR-019 Documented platforms | ✅ | README + `docs/operator.md` |
| FR-026..FR-028 Setup | ✅ | `setup_tools.py` |

## How the blocking findings were resolved

The pre-implementation review returned DO NOT SHIP on the earlier design. Each
finding and its resolution:

**B1 — pane PID is not Copilot's PID on Windows.** Resolved by
`operator_runner.py`, which spawns Copilot itself. During verification a second,
worse case appeared: `Popen.pid` is *also* wrong when the launcher is a shim,
which WinGet's `copilot.exe` and virtualenv's `python.exe` both are. Measured
tree from a live run:

```
launcher pid=80864  ->  log written by pid 70976 (a grandchild)
tree discovered: [17344, 70976, 80864]  ->  log pinned correctly
```

The runner therefore matches the log against the whole process tree it created,
and pins the file while that tree is still alive.

**B2 — detach left no supervisor.** Resolved: the supervisor lives in the pane,
so metrics are captured after detach. Covered by
`test_runner_captures_metrics_after_detach`, which never attaches at all.

**B3 — unsafe instance names.** Resolved by `safe_instance_id`: unsafe filename
characters are replaced, Windows reserved device names are handled, and a digest
is appended whenever sanitizing changes the name so `a.b`, `a:b` and `a-b`
cannot collide.

**B4 — ownership markers proved nothing.** Resolved: the marker is a JSON record
carrying a token, display name, session and pid, written atomically via
`os.replace`.

**B5 — metrics captured before Copilot exited.** Resolved: signal handlers only
set a flag, and capture happens in the supervisor strictly after the process
exits.

**B6 — stale forward-port.** Resolved by writing the modules against
`operator.sh` semantics rather than patching the stale branch. `--effort high`,
current state paths, ownership filtering and a platform-neutral preamble are all
present. `time_str_to_seconds` was confirmed dead code and deliberately not
ported.

**B7 — no Windows install design.** Resolved: `pyproject.toml` console scripts
installed by `setup_tools.py`, with PATH guidance, junction-based extension
linking, and fatal treatment of install failure.

**B8 — psmux risk.** Mitigated: CI installs a pinned psmux 3.3.7 on
`windows-latest` and runs the integration suite against it, and
`COPILOT_OPERATOR_MUX` overrides the binary.

**B9 — abstraction not backend-neutral.** Partially addressed. Because the
supervisor now reports process status, `pane_pid` and `pane_dead` are no longer
load-bearing — liveness comes from the supervisor's exit marker. The contract
still uses tmux verbs for create/list/kill/attach, so it remains a tmux-family
adapter; this is documented rather than claimed otherwise.

## Success Criteria

| Criterion | Result |
|---|---|
| SC-002 Command parity Windows/Linux | Met — one implementation serves both |
| SC-003 Consecutive automatic handoffs | Met — verified through session #3 |
| SC-004 Sessions produce a metrics record | Met, with an explicit exception: when a log cannot be attributed, **no** record is written rather than a wrong one |
| SC-005 Resume after restart | Met — verified after killing the operator |
| SC-007 Zero Linux regressions | Met — bash scripts unmodified |
| SC-009 Missing dependency named | Met |
| SC-010 Concurrent instances isolated | Met — verified with two live sessions |

## Verification

- 124 automated tests pass, of which 8 drive a **real psmux session** on Windows.
- `verify_cross_platform.py` — a stdlib-only smoke test needing no pytest —
  passes **36/36 on both platforms**: Windows with psmux 3.3.7 and Linux with
  tmux 3.4, including full runner supervision and metrics capture on each.
- Real tmux 3.4 was confirmed to accept the `-- argv` launch form, and an
  argument containing spaces and both quote styles survived verbatim.
- CI matrix: Ubuntu, Windows and macOS x Python 3.10 and 3.12, with a real
  multiplexer installed on each so the integration tests execute rather than
  skip.
- End-to-end loop mode, restart, resume, reports, `list`/`stop`,
  foreign-session isolation and `handoff` were each exercised manually on
  Windows 11.

## Out of Scope

- Retiring the bash implementation. It stays until the Python path has proven
  parity in daily use.
- Sharing state between a Windows-native operator and a WSL operator.
- Soak, Unicode-width and resize testing of psmux beyond current coverage.
