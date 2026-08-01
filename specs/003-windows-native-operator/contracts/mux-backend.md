# Session Backend Contract

**Feature**: `003-windows-native-operator`
**Module**: `operator_mux.py`

Defines the capabilities a session backend must provide. `copilot_operator.py` and `handoff_tool.py`
MUST NOT invoke a multiplexer directly — every call goes through this module, so the backend stays
replaceable.

## Backend selection

Probe order: `tmux`, then `psmux`, then `pmux`. First hit wins; the result is cached for the process.

On Windows the psmux installer registers `psmux`, `pmux`, **and `tmux`** as aliases, so probing `tmux`
first is correct on every platform and needs no OS branch.

When no backend is found, raise `MuxNotFoundError` naming the platform-appropriate remedy (FR-018,
SC-009):

| Platform | Message |
|---|---|
| Windows | `No terminal multiplexer found. Install psmux: winget install --id marlocarlo.psmux` |
| Linux / WSL | `No terminal multiplexer found. Install tmux with your package manager (e.g. sudo apt install tmux)` |
| macOS | `No terminal multiplexer found. Install tmux: brew install tmux` |

## Required operations

| Operation | Backend invocation | Returns | Notes |
|---|---|---|---|
| `sanitize_name(name)` | none (pure) | `str` | Replaces characters unsafe in session names or filenames. |
| `safe_instance_id(name)` | none (pure) | `str` | Adds a digest when sanitizing changes the name, or for Windows reserved device names, so distinct names cannot collide. |
| `new_session(name, cwd, argv)` | `new-session -d -s NAME -c CWD -- ARGV...` | `None`, raises on failure | argv passed after `--` so quoting is never re-parsed. MUST verify via `has_session` afterwards. |
| `has_session(name)` | `has-session -t NAME` | `bool` | Exit 0 = present. |
| `kill_session(name)` | `kill-session -t NAME` | `bool` | Idempotent; absent session is not an error. |
| `list_sessions()` | `list-sessions -F '#{session_name}'` | `list[str]` | Empty **output** = empty set. MUST NOT rely on exit status. |
| `pane_pid(name)` | `display-message -t NAME -p '#{pane_pid}'` | `int \| None` | Informational only — see below. |
| `pane_dead(name)` | `display-message -t NAME -p '#{pane_dead}'` | `bool` | Informational only — see below. |
| `set_remain_on_exit(name, on)` | `set-option -t NAME remain-on-exit on\|off` | `None` | |
| `send_keys(name, text, enter=True, literal=True)` | `send-keys -t NAME -l TEXT` then `send-keys -t NAME Enter` | `None`, raises on failure | Delivers `/exit` and agent-to-agent messages. MUST default to `-l`: without it the backend resolves whitespace-separated tokens in TEXT as key names, so message text is an injection vector (verified psmux 3.3.7 — an embedded `Enter` submits early and truncates, `C-c` interrupts the pane). `-l` takes no trailing key name, hence the second call. Both calls MUST be checked: mail delivery treats the exception as "not delivered" and queues the message instead, so a swallowed exit code files an undelivered message to the archive as read and loses it. |
| `pane_current_path(name)` | `display-message -t NAME -p '#{pane_current_path}'` | `str \| None` | Used by handoff instance inference. |
| `attach(name)` | `attach -t NAME` | exit code | Returns when the user detaches. |

### `pane_pid` and `pane_dead` are not load-bearing

Both are exposed for diagnostics but **must not** be used to identify the
Copilot process or to decide whether a session's work has finished.

`pane_pid` reports the pane's direct child. On POSIX the run script `exec`s
Copilot so the two coincide; on Windows it is the multiplexer's own shell, with
Copilot two or more levels below. Copilot's real PID comes from the supervisor
(`operator_runner.py`), which spawned it.

Liveness likewise comes from the supervisor's `{id}.exit` marker rather than
`pane_dead`. This keeps correctness independent of backend-specific format
variables — and means a backend lacking them entirely would still be usable.

## Invariants

1. **Sanitize before use.** Every caller-supplied name passes through `sanitize_name()` first. A name
   containing `:` creates no session on psmux while reporting success, so this is a correctness
   requirement, not cosmetic.
2. **Verify after create.** `new_session()` MUST call `has_session()` and raise `MuxSessionError` when
   the session is absent. This converts psmux's silent failure into a loud one, as Constitution
   Principle II requires.
3. **Empty is not an error.** No running server is a normal empty state (spec Edge Cases), so
   `list_sessions()` returns `[]` rather than raising.
4. **No backend-specific commands.** Only the verbs in the table above may be used. Anything outside it
   would couple the operator to psmux and break substitutability.
5. **Ownership.** The operator acts only on sessions with a matching `.managed` marker. `list_sessions()`
   returns everything the backend knows about; filtering by ownership is the caller's job (FR-006).

## Verified backend behavior

Verified on psmux 3.3.7 (`05cc5d4`, 2026-07-20) — full evidence in [research.md](../research.md) §R2.

| Capability | tmux | psmux | Divergence handling |
|---|---|---|---|
| `new-session -d` | OK | OK | — |
| `has-session` | OK | OK | — |
| `list-sessions -F` | OK | OK | psmux exits 0 with no server; tmux exits 1. Parse output, ignore status. |
| `#{pane_pid}` | OK | OK — real, live PID | — |
| `#{pane_dead}` | OK | OK — `0` running, `1` exited | — |
| `remain-on-exit on` | OK | OK — session outlives program | — |
| `remain-on-exit` unset | session closes | session closes | Matching behavior. |
| `send-keys ... Enter` | OK | OK | — |
| `#{pane_current_path}` | OK | OK | — |
| `kill-session` | OK | OK | — |
| Detached persistence | OK | OK — survives creating shell | — |
| cwd containing spaces | OK | OK | — |
| Name containing `.` | rewritten to `_` | preserved as-is | Sanitize to `-` for identical names on both. |
| Name containing `:` | rewritten to `_` | **silent failure — no session, exit 0** | Sanitize to `-`, then verify. |

## Test obligations (`tests/test_mux.py`)

- `sanitize_name` maps `.` and `:` to `-` and leaves other characters untouched.
- `new_session` raises `MuxSessionError` when `has_session` reports absent afterwards.
- `list_sessions` returns `[]` for empty output regardless of exit status.
- Backend probe order is `tmux` → `psmux` → `pmux`, with the result cached.
- `MuxNotFoundError` carries the correct per-platform install command.
- `pane_dead` maps `"1"` to `True` and `"0"` to `False`.

Tests mock `subprocess`, so they run on any platform without a multiplexer installed.
