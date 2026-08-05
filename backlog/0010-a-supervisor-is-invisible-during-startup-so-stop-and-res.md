---
id: 10
title: A supervisor is invisible during startup, so stop and restart-loop act as if none is running
status: proposed
opened: 2026-08-05
spec: none
---

## Evidence

A supervisor is treated as "running" by its loop pid file, but the file is
written near the end of its startup — after the interpreter has started, the
operator module has imported, arguments have been parsed, and the multiplexer
has been queried about the existing session. Between the moment the process
exists and the moment the file does, every consumer that gates on
`_running_loop_pid()` concludes that no supervisor is running.

Measured 2026-08-05 on this machine: the floor on that window — interpreter
start plus `import copilot_operator`, nothing else — is **105 ms**. The real
window is larger, because it also contains argument parsing,
`MUX.has_session`, `owns_live_session` and `handle_existing_session`, the last
three of which shell out.

Two concrete consequences, both found by adversarial review of the change that
introduced `_publish_supervisor_records`:

1. `operator stop NAME` calls `_request_supervisor_stop`, which returns
   immediately when `_running_loop_pid()` is `None` and therefore never
   touches `stop_marker` (`copilot_operator.py:2557-2559`). It then kills the
   multiplexer session directly. A supervisor still inside its startup window
   sees a session vanish with no stop marker and no restart marker, which is
   exactly the crash case, and relaunches a fresh session underneath the
   operator who asked for it to stop. The docstring of
   `_request_supervisor_stop` describes this race and closes it only for
   supervisors that have already published their pid.

2. `operator restart-loop NAME` reads the recorded arguments and then checks
   for a running supervisor. Finding none, it starts one — and the codebase
   already knows this can happen: the adopt path in `run_loop_mode` calls the
   pid check "the last line of defence against two supervisors watching one
   session", noting that "the handoff lock makes this unlikely; this makes it
   survivable". Both defences read the same file, so both are blind in the
   same window.

To reproduce: start a supervisor and issue `operator stop` for the same
instance within its startup window. Deterministically, patch a sleep between
the multiplexer checks and `_publish_supervisor_records` and stop it during
the sleep.

This is not caused by writing the pid file last. That reordering, made so
`operator ls` would stop reporting a healthy supervisor as unrecorded, moves
the write later by a measured **1.9 ms** — 1.8% of the 105 ms floor. The
window predates it and would survive reverting it.

## Why it matters

The two commands affected are the destructive ones. operator stop can leave a session running that the user asked to be stopped -- and worse, a *fresh* one, since the supervisor relaunches rather than merely surviving. operator restart-loop can leave two supervisors watching one session, which the code itself calls a state that relaunches over each other indefinitely. Both failures are silent: the user sees the command succeed.

## Notes

The fix is to stop using the pid file as the sole liveness gate during startup. Options considered but not chosen here, because both belong in a change scoped to the destructive paths rather than to a reporting fix: (a) always set stop_marker before checking the pid, so a supervisor that publishes mid-stop still sees it -- the supervisor already checks the marker every poll, so this costs nothing when no supervisor exists; (b) write a startup marker as the process's first act and have the liveness check consider both, which closes the window from the process's own first instruction rather than from partway through. (a) is smaller and fixes the worse of the two consequences. Found by three-model adversarial review (gpt-5.3-codex, gemini-3.1-pro-preview, claude-opus-4.6) of the CODE_UNRECORDED change; two of the three attributed the race to that change's write reordering, which the 1.9ms measurement refutes -- the reordering is a 1.8% contribution to a window that already existed.
