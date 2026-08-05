---
id: 1
title: Supervised sessions are killed mid-turn and never write a handoff
status: open
opened: 2026-08-04
spec: specs/003-windows-native-operator/spec.md
requirement: User Story 2 - Autonomous loop mode and handoff on Windows
---

## Evidence

Measured 2026-08-04 from `~/.operator/trace.jsonl`:

- 940 `session_exit` events recorded. **Every one carries `restart=False`.**
  Zero handoffs have ever been written by any instance.
- `handoff` appears 0 times in the entire trace, over its whole history.
- 939 of the 940 carry no exit code at all; the single exception is
  `3221225477` (`0xC0000005`, access violation).
- `copilot-tools` alone accounts for 155 of them. The four most recent ran
  387s, 367s, 268s and 458s before dying -- 4.5 to 7.6 minutes. None was idle.
- No `next-session.md` exists for this project, and `superseded/` holds 10
  files, which is the shape of handoffs written but never read.

The restart marker is load-bearing here and was verified rather than assumed:
`handoff_tool.py:975` touches `state_dir() / instance_id`, and
`copilot_operator.py:4236` records that same marker as `"restart"` into every
`session_exit`. So a session that ended *via* a handoff would show
`restart=True`. None does.

Root cause, from the Copilot debug log of one dead session
(`~/.copilot/logs/process-1785875597356-65372.log`): all seven extension hosts
exit with `3221225786` (`0xC000013A`, `STATUS_CONTROL_C_EXIT`) within 22ms of
each other, and Copilot then shuts down cleanly 3ms later because its
extensions are gone. The shutdown is orderly; what precedes it is not. All
eight project instances are hit within roughly 70ms, repeatedly -- one
broadcast, not eight independent decisions.

## Why it matters

The spec this item names records User Story 2, "Autonomous loop mode and
handoff on Windows", as **Delivered**, and in practice it is not: no handoff
has ever been written. Every session's accumulated context is dropped on the
floor when it dies, which is the exact failure the backlog this item lives in
exists to mitigate.

The agent is killed mid-turn, so it never reaches the point where it would
write a handoff. No amount of agent-side discipline fixes this.

## Notes

**The emitter is still unidentified.** Three hypotheses were tested and
refuted by measurement, not by argument:

- *Shared console.* Each instance has its own console with 11 members and the
  pid sets are completely disjoint, measured via `GetConsoleProcessList` and
  `AttachConsole` from a child process. A Ctrl+C in one terminal does not
  reach the others.
- *Operator's own control plane.* No operator command in the trace coincides
  with any kill.
- *Name-based process kills by agents, and scheduled tasks.* Neither lines up.

Do not close this by asserting a cause. An explanation that fits the evidence
is not the same as the explanation, and this item has already survived three
that fitted.
