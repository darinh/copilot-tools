# Feature Specification: Windows-Native Operator

**Feature Branch**: `003-windows-native-operator` (not yet created)

**Created**: 2026-07-27

**Status**: **Deferred — design seeded, blocked on unresolved review findings**

## Why this exists

This feature carries the deferred scope from the original combined "make it work on Windows" effort.
Phase 1 (platform-agnostic instructions and documentation) shipped separately as
`specs/002-platform-agnostic-instructions/`.

An adversarial review council of three models returned **DO NOT SHIP** on the combined plan — not
because the direction was wrong, but because the Windows process-supervision model, instance-ownership
semantics, and installation path were incomplete. That verdict applies to this feature's scope, so the
findings below **must be resolved before implementation begins**.

## Design work already completed and verified

These artifacts are carried forward and remain valid:

| Artifact | Content |
|---|---|
| [`research.md`](./research.md) | Backend selection with empirical verification; implementation-base decision; template audit |
| [`contracts/cli-surface.md`](./contracts/cli-surface.md) | Full `operator` + `handoff` command contract, fact-checked against `operator.sh` |
| [`contracts/mux-backend.md`](./contracts/mux-backend.md) | Session-backend capability contract with verified psmux/tmux divergences |
| [`data-model.md`](./data-model.md) | On-disk state formats, metrics schema, attribution rules |
| [`quickstart.md`](./quickstart.md) | Validation scenarios |

**Decisions already made and evidenced:**

1. **Implementation base: Python**, forward-porting `origin/develop`'s port onto `main` — not PowerShell
   (a PowerShell attempt was previously reverted on `main`) and not bash.
2. **Windows session backend: psmux**, verified against all 13 required tmux verbs on a real machine.
   WezTerm was rejected: it cannot supply pane PID, pane-dead state, or in-terminal attach.

## Blocking findings — resolve before implementing

### B1. No valid Windows process-supervision model (CRITICAL)

On Linux the run script ends in `exec copilot`, so the multiplexer's `#{pane_pid}` **is** Copilot's PID.
Windows has no `exec`. Measured tree:

```
pane_pid = 86524  →  pwsh          (psmux's own default shell)
                   └── 8784        cmd.exe        (the run script)
                       └── 100132  powershell.exe (the actual program)
```

Consequences, both silent: `--resume` can never work, and metrics fall back to "newest log in the
directory", letting concurrent instances steal each other's usage data.

**Direction**: a persistent Python runner inside the pane that launches Copilot, records its real PID and
status, waits for exit, and ingests the exact log. This also replaces the fragile
generate-then-reparse run-script design with structured launch state.

### B2. Detach leaves no supervisor (CRITICAL)

`run_single_session` prints *"Metrics will be captured when copilot exits"* and then **exits**. Nothing
remains to capture them. This is a **pre-existing defect on Linux**, not a Windows-only one, and it means
any "100% of sessions produce a metrics record" criterion is unachievable until a supervisor exists.
The runner in B1 resolves this too.

### B3. Instance naming is unsafe on Windows (CRITICAL)

Sanitizing only `.` and `:` is insufficient. Names are embedded in filenames, so `\ / * ? " < > |`,
reserved device names (`CON`, `NUL`, `COM1`), and rooted paths all break. Worse, `a.b`, `a:b`, and `a-b`
all collapse to the same sanitized name, so distinct instances can collide.

**Direction**: separate display name from an internal filesystem-safe ID (hash-suffixed), storing the
original in metadata.

### B4. Ownership markers do not prove ownership (HIGH)

An empty `.managed` file cannot distinguish the session that created it from a foreign session that
later took the same name. Combined with no per-instance lock, two simultaneous same-name starts can both
pass the existence check and destroy each other.

**Direction**: store an ownership token plus backend session identity; take an exclusive per-instance
lease before create/replace/stop; write state atomically via `os.replace`.

### B5. Cleanup captures metrics before Copilot exits (HIGH)

The current cleanup path captures metrics while Copilot is still running, so the shutdown event often
does not exist yet and the record lands as a no-op. It never performs graceful `/exit` → bounded wait →
force-kill. Signal handlers must only set a shutdown event; the main loop performs the sequence.

### B6. `develop`'s port is behaviorally stale (HIGH)

Verified: it omits `--effort high`, its help text references the **old** `~/.copilot/restart/` state
path, its agent preamble hard-codes a bash `touch` instruction, zero-argument invocation shows help
instead of starting a session, and `list`/`stop` use obsolete name-prefix ownership. The forward-port
must be driven by a **behavior-level parity matrix**, not by function-name presence.

Also verified: `save_instance_state`/`load_instance_state`, `--fresh`, and the SIGINT/SIGTERM handler
**already exist** on `develop`. `time_str_to_seconds` is dead code in `operator.sh` and must not be
ported.

### B7. Windows installation has no complete design (HIGH)

`pyproject.toml` console scripts only help *after* a successful install, and nothing specifies who
performs it or how PATH is fixed. `develop` exposes only `copilot-operator`, not `operator`/`handoff`;
continues after install failure; does not install runtime extensions; and omits `main`'s spec-kit/uv
setup. Extension installation needs directory junctions or an explicit copy strategy, since Windows
symlinks generally require Developer Mode or elevation. There is also an unresolved Python-version
conflict: the port targets 3.10 while spec-kit setup requires 3.11.

### B8. psmux risk mitigation is not yet credible (HIGH)

The claim that psmux is "version-pinned" was **false** — the documented `winget` command installs latest.
Verification so far is a single-machine verb smoke test with no soak, Unicode, resize, or scrollback
coverage, and unit tests mock the backend entirely. Upstream has open reports on runaway process growth,
a session-wide pipe wedge, resize input freeze, Unicode width corruption, and unkillable panes.

**Direction**: pin and verify a tested version, add a real Windows psmux integration job plus a soak
gate, and add a `COPILOT_OPERATOR_MUX` override with a capability self-test.

### B9. The backend abstraction is not genuinely backend-neutral (MEDIUM)

`contracts/mux-backend.md` exposes tmux concepts directly (format variables, `remain-on-exit`, pane PID,
tmux attach semantics), so it is an adapter for tmux-compatible clones rather than a neutral interface —
Zellij could not satisfy it. Note that once the B1 runner reports process status, pane PID and pane-dead
stop being backend requirements, which would also reopen WezTerm as a candidate. Either redesign around
runner-provided status or explicitly document the abstraction as tmux-family-specific.

## Recommended delivery phases

1. **Backend/runner spike** — resolve B1, B2, B3, B4 with real psmux integration tests. No user-facing
   change; purely proving the process model.
2. **Windows MVP** — single session, detach/reattach, owned `list`/`stop`, guaranteed metrics. Manual
   documented install. Bash remains the Linux default.
3. **Loop delivery** — handoff, restart, resume, interruption, concurrency.
4. **Setup and consolidation** — Windows bootstrap, extensions, idempotency; make Python the Linux
   default only after proven parity.

## Constitution note

Making Python the primary implementation language requires a constitution amendment: Operational
Constraints currently address only bash ("Bash scripts target Linux and WSL..."). This is an additive
gap rather than a conflict, but governance requires a documented amendment and version bump.
