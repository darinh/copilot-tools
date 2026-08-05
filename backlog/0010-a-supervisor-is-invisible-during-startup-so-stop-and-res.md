---
id: 10
title: A supervisor is invisible during startup, so stop and restart-loop act as if none is running
status: closed
opened: 2026-08-05
closed: 2026-08-05
commit: cc5126354c1f49590c4795d3694ec7189767d4f2
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

## Resolution

Option (b) from the Notes, and not (a): a startup record written as the
process's first act, with liveness checks that consider it. (a) -- always
setting the stop marker before checking the pid -- is in there too, because it
is free and closes the worse consequence, but it is not sufficient on its own:
`restart-loop` is not fixed by a stop marker.

`<instance-id>.loopstarting` is written by the parent immediately after
`Popen`, claimed by the child with its own pid as the child's first act, and
removed when the real pid file is published. `_supervisor_status` returns
`(pid, still_starting)` from one pass over both files, and every caller that
acts destructively uses it instead of `_running_loop_pid`. The two callers that
are *confirming a supervisor came up* deliberately keep the narrow reader,
because the wide one is satisfied by the record their own spawn just wrote.

Landed as cc51263 with six further commits of review-driven hardening; four
rounds of adversarial review across three models found, in order:

- a record believed for roughly twice the grace while every wait budgeted one
  grace for it, fixed by deriving `SUPERVISOR_STARTUP_ALLOWANCE`;
- a dead launcher shim's pid reported as "running";
- a strict future-side bound that prunes every record, every time, on a
  filesystem whose timestamps round to two seconds;
- a TOCTOU between two separate stats of the same two files;
- and a live pid believed without any upper bound, so a supervisor
  hard-killed inside its startup window left a record that the operating
  system's pid reuse turned into a permanent, silent refusal to launch --
  `start_loop_headless` returning 0 having started nothing. Bounded by
  `SUPERVISOR_STARTUP_CEILING`.

Pinned by 69 tests in `tests/test_supervisor_startup_window.py` and a
39-mutation driver in which every mutation lands as predicted, one declared
equivalent with its proof. The driver caught two things the suite could not:
a test made unfalsifiable by a refactor that moved the function its spy was
patched onto, and a new test that passed under the very mutation it named.
