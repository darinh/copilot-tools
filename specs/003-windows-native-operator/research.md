# Research: Windows-Native Operator

**Feature**: `003-windows-native-operator`
**Date**: 2026-07-27
**Status**: Backend selected and empirically verified — carried forward from the combined-scope planning

## Decision Summary

| Question | Decision |
|---|---|
| Implementation base | Forward-port the Python operator from `origin/develop` onto `main` |
| Windows session backend | **psmux** (`marlocarlo.psmux`, v3.3.7) |
| Backend abstraction | Keep the existing tmux-verb surface; select the binary at runtime |
| Rejected backend | WezTerm — cannot satisfy three required capabilities |

## R1. Implementation base: Python, not PowerShell

### Context

The repository contains two prior, unreconciled cross-platform attempts:

1. **`origin/develop`** — an unmerged Python port: `copilot_operator.py` (911 lines), `operator_ingest.py`,
   `setup_tools.py`, `pyproject.toml`, a pytest suite, and a CI matrix already running `windows-latest`
   against Python 3.10 and 3.12. Merge base with `main` is `4619cba`; `main` has since advanced ~40 commits.
2. **`main` commit `9a42554`** — a revert of `1d08344` ("Merge pull request #4 from
   darinh/anvil/cross-platform-setup"), which removed `setup.ps1`, `upgrade.ps1`, `upgrade.sh`,
   `templates/mcp-config.windows.json`, `.gitattributes`, `.gitignore`,
   `lib/migrate-operator-state.sh`, and `lib/test-migrate-operator-state.sh`. The revert commit
   records no reason.

### Decision

Consolidate on the Python implementation.

### Rationale

- `python3` is already a hard prerequisite of the toolkit, so Python adds no new dependency, whereas a
  PowerShell implementation would add a third language to maintain alongside bash and Python.
- The `develop` port already carries a pytest suite and a Windows CI matrix — infrastructure that would
  otherwise have to be rebuilt.
- A PowerShell path was already attempted and reverted once on `main`. Repeating it without a recorded
  reason for the revert risks repeating the failure.
- One implementation covering Linux, macOS, and Windows satisfies the constitution's principle that
  tooling must earn its place, rather than maintaining parallel implementations that will drift.

### Consequences

- `develop`'s port predates features that landed on `main` and must be re-added during the forward-port:
  Copilot session resume (`--resume=`), `join`, `reload`, legacy state migration, session-name
  sanitization, terminal tab titles, duplicate-session handling, and the reserved-word list.
  Instance state persistence, `--fresh`, and the SIGINT/SIGTERM handler are **already present** in the
  `develop` port and need only extension, not reimplementation — see `plan.md` §1.2 for the
  function-by-function verification.
- `develop`'s port carries defects of its own that must be fixed rather than inherited: a batch launch
  script that cannot survive the multi-line preamble, log reads without an explicit encoding,
  string-interpolated SQL, and no database concurrency configuration. See R5.
- `operator.sh` remains in place for the existing Linux/WSL path so FR-017 (no regression) holds.

## R2. Windows session backend: psmux

### Alternatives considered

| Option | Outcome |
|---|---|
| **psmux** | **Selected.** Ships `tmux`/`pmux`/`psmux` as identical aliases; native ConPTY; verified against every verb the operator uses. |
| WezTerm | **Rejected.** Fails three required capabilities (below). |
| Zellij 0.44 | Viable fallback. Native Windows since 2026-03-23, in-terminal attach, but exposes neither pane PID nor pane-dead state. |
| wmux | Rejected. GUI multiplexer with a JSON-RPC API; no detached-session verbs. |
| abduco / dtach | Rejected. No native Windows port; both require POSIX PTY APIs. |

### Why WezTerm was rejected

WezTerm's mux covers spawn, send-text, kill-pane, and list well, but fails three capabilities the
operator depends on:

- **`#{pane_pid}` — not possible.** No PID in `wezterm cli list --format json`.
  `get_foreground_process_info()` returns `nil` for any pane reached through a mux domain, because the
  process information lives in the mux server process rather than the connecting client.
- **`#{pane_dead}` — not possible.** No dead-state field is exposed; detection would require polling for
  a pane id to disappear.
- **`tmux attach` — not possible.** `wezterm connect <domain>` always opens a **new GUI window**. There
  is no in-terminal attach from a plain console.

The operator needs the PID to locate the correct Copilot process log for metrics, and needs dead-pane
detection to drive the loop-mode restart cycle. Both are load-bearing, so WezTerm is not viable.

### psmux verification

Verified empirically on this machine against psmux 3.3.7 (`05cc5d4`, 2026-07-20), installed via
`winget install --id marlocarlo.psmux`. The installer registers `psmux`, `pmux`, **and `tmux`** as
command aliases, and the binary self-reports as `tmux 3.3.7`.

| Capability | Command tested | Result |
|---|---|---|
| Create detached session | `new-session -d -s NAME -c DIR PROG` | PASS (exit 0) |
| Session existence | `has-session -t NAME` | PASS (0 when present, 1 after kill) |
| Enumerate sessions | `list-sessions -F '#{session_name}'` | PASS |
| Pane PID | `display-message -t NAME -p '#{pane_pid}'` | PASS — returned `82728`, confirmed a real live `pwsh` process |
| Pane dead state | `display-message -t NAME -p '#{pane_dead}'` | PASS — `0` while running, `1` after exit |
| Hold session open | `set-option -t NAME remain-on-exit on` | PASS — session survived process exit |
| Default close behavior | session with `remain-on-exit` unset | PASS — session disappeared on exit, matching tmux |
| Send input | `send-keys -t NAME "<cmd>" Enter` | PASS — command executed in the pane |
| Read pane output | `capture-pane -t NAME -p` | PASS |
| Working directory | `display-message -t NAME -p '#{pane_current_path}'` | PASS |
| Destroy session | `kill-session -t NAME` | PASS |
| Persistence | queried from a **new** shell after the creating shell exited | PASS — session and PID still valid |
| Path containing spaces | `-c "C:\...\ps mux dir"` | PASS — path preserved exactly |
| Attach verb present | `attach` / `attach-session` / `detach-client` | Present in command list |

Every verb the operator relies on is supported, which validates `develop`'s original psmux bet.

### Verified behavioral differences from tmux

These are real differences found during testing and must be handled in the implementation:

1. **Session names containing `:` fail silently.** `new-session -d -s "my.proj:v2"` returned **exit 0 but
   created no session**; a subsequent `has-session` returned 1 and `list-sessions` was empty. tmux
   instead rewrites `.` and `:` to `_`. A silent success-shaped failure is the most dangerous variant,
   so name sanitization is mandatory rather than cosmetic on Windows.
2. **`.` is preserved, not rewritten.** `new-session -d -s "my.proj"` succeeded and the session was
   listed as `my.proj`, whereas tmux would have stored it as `my_proj`. Sanitizing `.` to `-` before use
   keeps the name identical across platforms.
3. **`list-sessions` with no server returns exit 0** and empty output, where tmux returns exit 1 with
   "no server running". Code must treat empty output — not a non-zero exit — as the empty state.

The existing `sanitize_session_name` behavior on `main` (replacing `.` and `:` with `-`) already
prevents differences 1 and 2, and must be carried into the Python port rather than dropped.

### Residual risk

psmux is young (v3.3.7, active 2025–2026). The mitigation is that the operator targets the standard tmux
verb surface only, so the backend is replaceable: Zellij 0.44 or a future alternative can be substituted
without changing operator logic. No psmux-specific commands are used.

## R3. Metrics pipeline portability

`operator-ingest.py` on `main` shells out to POSIX-only binaries and is therefore unusable on Windows:

- `sqlite3` CLI for every database read and write — not present on Windows (confirmed absent on this
  machine), despite Python shipping the `sqlite3` module in its standard library.
- `grep -B 1 -A 150` to extract the `session_shutdown` event and `grep -A 20` for `assistant_usage`.
- `head -1`, `tail -1`, and `head -c 50000` to read timestamps and the log header.

`operator_ingest.py` on `develop` already replaces these with pure-Python equivalents. Adopting it
removes the `sqlite3` binary from the prerequisite list on **all** platforms.

It does **not**, however, remove the string-interpolated SQL: it still builds statements with f-strings
plus a manual `sql_esc()` helper (lines 212, 230–234, 332–348) and passes them to `cursor.execute()`.
The injection surface is unchanged from the shell version, so converting to parameter binding is
in-scope work for this feature rather than something inherited for free.

## R4. Platform-specific content in shipped instructions

Audit of the templates that setup installs, confirming the second scope item:

| Location | Issue |
|---|---|
| `templates/copilot-instructions.md` — Catalog | Example paths are POSIX-only (`/home/user/projects/my-app`); a Windows user cannot infer the expected format. |
| `templates/copilot-instructions.md` — Handoff fallback | Prescribes `touch ~/.operator/restart/{instance-name}`; `touch` does not exist in PowerShell. |
| `templates/copilot-instructions.md` — Handoff example | Uses bash `\` line continuations inside a ```bash fence. |
| `templates/copilot-instructions.md` — Spec-kit init | Hard-codes `--script sh`; Windows requires the PowerShell script variant. |
| `templates/copilot-instructions.md` — Field Notes | Uses a POSIX-style `~/projects/...` path. |
| `templates/project-instructions.md` — Restart protocol | Prescribes `touch ~/.operator/restart/{instance-name}` as the mandatory final action. |
| `templates/project-instructions.md` — Header | `> **Project root**: /path/to/project` shows only a POSIX path. |

The `touch` occurrences are the highest-impact defects: they appear in the mandatory session-end
protocol, so an agent on Windows following the instructions literally fails at exactly the moment the
handoff must succeed.

### Additional defects outside the templates

The initial audit covered only the two installed templates. A follow-up audit of the repository's
user-facing documentation found further platform-specific instructions. These are in scope for FR-019
through FR-025, because a Windows user following the README cannot complete installation at all.

| File | Line | Construct | Why it fails on Windows |
|---|---|---|---|
| `README.md` | 20–21 | `git clone … ~/projects/copilot-tools`, `cd ~/projects/…` | POSIX home-path convention |
| `README.md` | 22 | `chmod +x setup.sh operator.sh` | `chmod` does not exist |
| `README.md` | 23 | `./setup.sh` | No bash on stock Windows |
| `README.md` | 28, 30 | Symlink `operator` and extensions into POSIX paths | `ln -s`; Windows symlinks need elevation or Developer Mode |
| `docs/operator.md` | 16 | `./setup.sh` | No bash on stock Windows |
| `docs/operator.md` | 19 | `ln -sf /path/to/operator.sh ~/.local/bin/operator` | POSIX symlink + POSIX bin path |
| `docs/operator.md` | 103 | `~/projects/my-project` example | POSIX home-path convention |
| `docs/spec-kit.md` | 13 | `specify init … --script sh` | Windows needs `--script ps` — same defect class as the template, but outside it |
| `docs/spec-kit.md` | 18 | Bootstrap "requires `curl`" | POSIX tool assumed present |
| `docs/spec-kit.md` | 19 | `SPEC_KIT_VERSION=vX.Y.Z ./setup.sh` | env-var-prefix syntax is invalid in PowerShell |
| `docs/skills.md` | 16 | `cp -r skills/… your-project/.github/skills/` | `cp -r` is POSIX; PowerShell uses `Copy-Item` |
| `templates/project-instructions.md` | 22, 24 | `~/.copilot/...` paths | POSIX home paths beyond the header already flagged |

`.github/copilot-instructions.md` was audited and is clean — it contains only SQL and git-worktree
guidance with no platform-specific commands or paths.

The **symlink-based install** is the most consequential item: `setup.sh` links `operator` and `handoff`
into `~/.local/bin`, which has no Windows equivalent and would require elevation. This is why the plan
routes installation through `pyproject.toml` console scripts instead.

## R5. Defects in the `develop` port and the Windows process model

Found by adversarial review and confirmed empirically. These are not reasons to reject the Python base;
they are work items that the forward-port must absorb.

### R5.1 `#{pane_pid}` is not the Copilot process on Windows (CRITICAL)

On Linux the generated run script ends in `exec copilot ...`. `exec` replaces the shell, so the
multiplexer's pane PID **is** Copilot's PID, and Copilot's log file `process-{startMs}-{pid}.log` can be
found by PID match. Windows has no `exec`.

Measured process tree for a psmux session launched with a script:

```
pane_pid = 86524  →  pwsh           (psmux's own default shell)
                   └── 8784         cmd.exe        (the generated run script)
                       └── 100132   powershell.exe (the actual program)
```

psmux reports its **default shell**, not even the script it was given. Two silent failures follow:
`find_copilot_log_for_current_launch()` can never match, so `--resume` never works (FR-012, SC-005);
and `find_copilot_log()` falls back to the newest log in the directory, so concurrent instances can
steal each other's metrics (FR-014, SC-010).

Resolution: the launch script records Copilot's real PID itself. See `plan.md` §1.5.

### R5.2 Batch cannot carry the preamble

`develop` emits `@echo off` batch on Windows and assigns the preamble via `set PREAMBLE=...`. The
preamble is one long string with parentheses, apostrophes, quotes, and `--` flags. Batch ends a `set` at
the first newline and executes the remainder as commands, and separately mangles `%`, `^`, `&`, and `>`.
Verified to fail. Resolution: generate PowerShell with a single-quoted here-string (`plan.md` §1.6).

### R5.3 Log reads lack an explicit encoding

`operator_ingest.py` opens log files at lines 51, 78, 91, and 156 as `open(..., 'r', errors='replace')`
with no `encoding=`. On Windows this defaults to the locale code page, so non-ASCII UTF-8 content is
mis-decoded; because `errors='replace'` corrupts rather than raises, the JSON parse then fails and the
session's metrics are **silently discarded**.

### R5.4 No database concurrency configuration

Connections use the default 5-second timeout and the rollback journal. Concurrent instances are an
explicit requirement (FR-014), so a busy timeout and `PRAGMA journal_mode=WAL` are needed.

### R5.5 Verified as safe

Process termination needs no redesign. psmux routes ConPTY stdin to the foreground process correctly, so
the existing `/exit` mechanism works, and `kill-session` was verified to terminate the whole process
tree with no orphans left behind.

## References

- psmux — `https://github.com/psmux/psmux`; installed package `marlocarlo.psmux` 3.3.7
- WezTerm multiplexing — `https://wezterm.org/multiplexing.html`
- WezTerm CLI — `https://wezterm.org/cli/cli/index.html`
- WezTerm `get_foreground_process_info` — `https://wezterm.org/config/lua/pane/get_foreground_process_info.html`
- Zellij Windows support — `https://zellij.dev/news/remote-sessions-windows-cli/`
