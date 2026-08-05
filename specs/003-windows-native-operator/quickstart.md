# Quickstart: Validation Guide

**Feature**: `003-windows-native-operator`

Runnable scenarios that prove the feature works. Each maps to user stories and success criteria in
[spec.md](./spec.md). Run the Windows scenarios on a Windows host and the regression scenario on
Linux/WSL.

## Prerequisites

### Windows

```powershell
winget install --id marlocarlo.psmux      # session backend
python --version                          # must be >= 3.10
copilot --version
git --version
```

`psmux` registers `psmux`, `pmux`, and `tmux` as aliases. Confirm:

```powershell
tmux -V     # expect: tmux 3.3.7
```

### Linux / WSL

```bash
tmux -V && python3 --version && copilot --version && git --version
```

`sqlite3` is **not** required on either platform.

### Install

```powershell
cd C:\Users\<you>\repos\copilot-tools
pip install -e .
operator help      # console script must resolve
```

## Scenario 1 — Native Windows single session

Validates **US1**, FR-001..FR-007, SC-001, SC-004.

```powershell
cd C:\Users\<you>\repos\some-project
operator --name qs-single
```

Expected:

1. A Copilot session starts and the console attaches to it.
2. In a second console, `operator list` shows `qs-single`.
3. Detach without exiting Copilot — the operator prints a re-attach hint and the session stays alive.
4. `operator qs-single` reattaches.
5. Exit Copilot; the operator prints a run summary.
6. `operator report sessions` includes the session.

Verify the session really is detached and the PID is real:

```powershell
tmux has-session -t qs-single           # exit 0
tmux display-message -t qs-single -p '#{pane_pid}'
```

### Path-with-spaces check (FR-007)

```powershell
mkdir "C:\Temp\qs space dir"; cd "C:\Temp\qs space dir"
operator --name qs-space
tmux display-message -t qs-space -p '#{pane_current_path}'   # exact path, unmangled
operator stop qs-space
```

### Name-sanitization check

Names containing `:` must never reach the backend raw — psmux silently creates nothing.

```powershell
operator --name "qs:bad.name"
tmux list-sessions -F '#{session_name}'   # expect qs-bad-name
operator stop qs-bad-name
```

Failing this check means the session silently does not exist.

## Scenario 2 — Loop mode and handoff

Validates **US2**, FR-011..FR-015, SC-003, SC-005.

```powershell
cd C:\Users\<you>\repos\some-project
operator --loop --name qs-loop --agent anvil:anvil
```

Then, from inside the agent session (or a second console):

```powershell
handoff --instance qs-loop --status "did a thing" --next "do the next thing"
```

Expected:

1. `~/.operator/projects/{guid}/next-session.md` is written with `Status` and `Next Steps`.
2. `~/.operator/restart/qs-loop` is created.
3. Within one poll interval (10s) the operator logs the restart signal, sends `/exit`, captures metrics,
   and starts session #2.
4. `~/.operator/restart/qs-loop.state` shows `SESSION_NUM=2`.

### Auto-continue and resume (FR-012, SC-005)

```powershell
# Ctrl+C the operator, then:
Get-Content ~/.operator/restart/qs-loop.state
operator --loop --name qs-loop --agent anvil:anvil
```

Expected: numbering continues (not 1), and the log records injecting `--resume=<uuid>` exactly once.

```powershell
operator --loop --name qs-loop --fresh --agent anvil:anvil   # resets to #1
```

### Interrupt handling (FR-015)

Ctrl+C a running loop. Expected: final metrics captured, aggregate run summary printed, session killed,
marker files removed.

## Scenario 3 — Multi-instance management

Validates **US3**, FR-014, FR-006, SC-010.

```powershell
operator --loop --name qs-a --agent anvil:anvil    # console 1
operator --loop --name qs-b --agent anvil:anvil    # console 2

operator list         # both listed
operator stop qs-a    # only qs-a stops
operator list         # qs-b still running
operator stop         # stops the rest
operator list         # "(none)"
```

### Foreign-session isolation (FR-006)

```powershell
tmux new-session -d -s qs-foreign "pwsh -NoLogo -NoExit"
operator list         # MUST NOT list qs-foreign
operator stop         # MUST NOT kill it
tmux has-session -t qs-foreign     # exit 0 — still alive
tmux kill-session -t qs-foreign
```

### Empty-state handling

With nothing running, `operator list` and `operator stop` must exit 0 with a friendly message — not an
error (spec Edge Cases).

## Scenario 4 — Missing prerequisite

Validates FR-018, SC-009.

Temporarily rename the psmux binary, then:

```powershell
operator --name qs-nomux
```

Expected: a message naming the missing multiplexer **and** `winget install --id marlocarlo.psmux`,
exit 1. A raw `FileNotFoundError` or a bare non-zero exit is a failure.

## Scenario 5 — Platform-agnostic instructions

Validates **US4**, FR-020..FR-025, SC-006.

Static check — no Linux-only construct may remain unlabelled:

```powershell
Select-String -Path templates\*.md, .github\copilot-instructions.md `
  -Pattern 'touch |chmod |ln -s|/home/|--script sh' 
```

Every hit must sit inside an explicitly platform-labelled block.

Behavioral check: on Windows, execute the restart-signal fallback exactly as the template prescribes and
confirm the marker is created:

```powershell
New-Item -ItemType File -Force ~/.operator/restart/qs-loop
Test-Path ~/.operator/restart/qs-loop      # True
```

## Scenario 6 — Linux regression

Validates FR-017, SC-007. **Gating** — this must pass before merge.

```bash
cd ~/projects/copilot-tools
bash -n operator.sh && bash -n handoff.sh && bash -n setup.sh
bash tests/test-todo-claims.sh
python3 -m pytest tests/ -v

operator --loop --name qs-linux --agent anvil:anvil
operator list && operator stop qs-linux
operator report summary
```

Expected: unchanged behavior. `operator.sh` remains functional and untouched.

## Scenario 7 — Setup idempotency

Validates **US5**, FR-026..FR-028, SC-008.

```powershell
python setup_tools.py                 # first run
# edit ~/.copilot/copilot-instructions.md, add a sentinel line
python setup_tools.py                 # second run
Select-String -Path ~\.copilot\copilot-instructions.md -Pattern 'sentinel'
```

Expected: the second run reports already-installed components, asks before overwriting edited config,
and the sentinel survives when overwrite is declined.

## Automated suite

```powershell
python -m pytest tests/ -v
```

CI runs this on `ubuntu-latest` and `windows-latest` across Python 3.10 and 3.12. Unit tests mock the
multiplexer, so they pass without psmux or tmux installed.

## Cleanup

```powershell
operator stop
Remove-Item ~/.operator/restart/qs-* -Force -ErrorAction SilentlyContinue
Remove-Item "C:\Temp\qs space dir" -Recurse -Force -ErrorAction SilentlyContinue
```
