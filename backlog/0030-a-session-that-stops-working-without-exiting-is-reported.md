---
id: 30
title: A session that stops working without exiting is reported as looping forever
status: proposed
opened: 2026-08-15
spec: specs/003-windows-native-operator/spec.md
---

## Evidence

Measured 2026-08-15T22:17Z on this machine, across the nine supervised
instances. Eight of them had a live `copilot` process, a supervisor reporting
`looping · session #N`, and had been idle for between 2.8 and 5.9 days.

`Forwarding event for session <uuid>` is written to the Copilot process log
whenever the agent does anything — tool call, background-task change, message
turn. Newest occurrence per pinned log, against the log's mtime at the moment
of measurement:

| pid | log size | mtime | newest `Forwarding event` |
|---|---|---|---|
| 69536 | 211.3 MB | 0.3 min | 2026-08-10T05:10:56Z — 137.1h |
| 52936 | 68.0 MB | 0.3 min | 2026-08-10T01:09:12Z — 141.1h |
| 36676 | 82.4 MB | 8.9 min | 2026-08-10T01:53:29Z — 140.4h |
| 13584 | 120.8 MB | 0.7 min | 2026-08-10T04:39:20Z — 137.6h |
| 37600 | 20.3 MB | 2.1 min | 2026-08-10T06:46:01Z — 135.5h |
| 12284 | 16.7 MB | 4.6 min | 2026-08-10T19:53:27Z — 122.4h |
| 66784 | 251.6 MB | 8.6 min | 2026-08-13T01:41:59Z — 68.6h |
| 54508 | 217.4 MB | 6.9 min | 2026-08-13T01:51:07Z — 68.4h |
| 57864 | 14.2 MB | 0.0 min | 2026-08-15T22:17:38Z — 0.0h |

The last row is the session that took the measurement, and is the positive
control: the marker is current in a session that is working. The second
control is the marker count per file — 18,290 to 130,455 occurrences in the
eight inert logs — so the marker is abundant in exactly the files where it has
gone quiet, which distinguishes "this session stopped" from "this build stopped
emitting the string".

Four instruments read none of those logs. Two of them measure inactivity:

- **git.** Last commit in each instance's repository is 68.4h, 68.5h, 123.7h,
  135.5h, 137.1h, 137.6h, 140.3h and 141.1h old. Every working tree is clean,
  so it is not uncommitted work either.
- **The last *agent* `operator` invocation per repository** in `trace.jsonl`,
  with the supervisor's own `--_supervise` launches excluded. These pair with
  the marker ages instance by instance: `finances` 141.4h against a 141.1h
  marker, `book-translator` 140.7h against 140.4h, `prism` 137.4h against
  137.1h, `discord-invite-manager` 135.8h against 135.5h, `snes-ghosts` 68.7h
  against 68.4h.

The other two fire only at an ending, so they establish that these sessions
have not ended — not that they stopped working:

- **`session_exit`.** Last record per instance: 2026-08-10 for seven of the
  nine, 2026-08-13 for `snes-ghosts`, 2026-08-15 for `copilot-tools`. For the
  eight, that dates the *start* of the session still running.
- **Handoffs.** `~/.operator/projects/{guid}/handoff/{instance}.md` was last
  written 70.1h ago for `snes-ghosts`, 122.9h for `ac-unreal`, 136.2h for
  `discord-invite-manager`, 140.0h for `scripts`, 141.3h for
  `book-translator`, 203.8h for `prism` — and 0.4h ago for `copilot-tools`.
  The reader deletes this file at session start, so it dates the last ending,
  not the last opportunity to write one.

None of the four is independent *evidence*: all are downstream of the agent
completing a turn, so one cause accounts for all of them. What they establish
is that the inactivity is real rather than an artifact of one reader.

### The state they are in

The last forwarded event before each silence is the same in all eight:

```
assistant.turn_end → hook.start → hook.end → session.usage_checkpoint
                   → assistant.idle (ephemeral) → session.idle (ephemeral)
```

Seven are exactly that; `12284` carries a `session.error` between
`assistant.turn_end` and the idle pair. **Every one completed a turn and went
idle, and nothing issued another turn.** They are not wedged part-way through
one.

Throughout, `operator list` reported all nine as `looping`, with uptimes of 14
to 16 days.

## Why it matters

Every liveness instrument in the supervisor fires at an *ending*.
`copilot_operator` polls for the pane's process to die; `operator.log` counts
`copilot exited unexpectedly`; `trace.jsonl` records `session_exit`. A session
whose agent stops working while its process stays up satisfies none of them, so
it is reported as healthy for as long as it sits there — five and a half days,
in the measurement above, across eight of nine instances.

The cost is the whole point of the supervisor. Loop mode exists so that a
session that ends is replaced by another that carries the work forward, and for
those days it replaced nothing, because nothing it can see had happened. The
fleet was idle and every readout said `looping`.

It also silently poisons backlog item 0001, which is measured entirely with
ending-based instruments. Its 2026-08-15 re-measurement counted 12 endings in
5.66 days against 60 in 11.1 hours a week earlier and read the quiet as
evidence that the kills had stopped. A quiet ending-count is only evidence
about kills if something was running that a kill could have taken, and on
2026-08-15 that was true of one instance out of nine. No sharpening of those
instruments can fix this, because the population they sample has gone.

## Notes

**The mechanism is not established and is deliberately not guessed at here.**
What is measured is the state: every one of the eight completed a turn, emitted
`session.idle`, and nothing issued another turn. Why nothing continued — the
harness not resuming, the agent ending its own loop, something upstream — is
unmeasured. Item 0001 has been burned three times by an explanation that fitted
the evidence, and this is the same evidence.

**A session has come back from this once, and nothing recorded either
transition.** `66784` was silent for 63.05h (2026-08-10T09:13Z to
2026-08-13T00:16Z), worked for about 90 minutes, and has been silent again for
68.6h. So the state is recoverable and is not obviously death — which makes the
detection gap worse rather than better, because a supervisor that could see it
would have had 2.6 days in which acting was possible. The other seven inert
sessions have no internal gap of even one hour anywhere in their logs:
continuous work, then one silence.

Read the claim as "silent for 2.8 to 5.9 days", not "dead". The two shortest
silences are only about a tenth longer than the one `66784` recovered from.

**What a fix needs, and what it must not do.** The supervisor needs a liveness
signal that does not depend on an ending. The one used to measure this —
newest `Forwarding event for session` in the pinned Copilot log — is a
Copilot-internal debug string, so anything built on it must fail loudly when
the marker is absent from a log entirely. A file with zero occurrences means
*cannot tell*, not *idle*, and a check that conflates them degrades silently
into the mtime check it replaced. The positive control is cheap and belongs in
the code: the marker occurs 18,290 to 130,455 times in the very logs where it
has gone quiet.

The marker also under-reports liveness — an agent blocked inside one long
local command emits nothing meanwhile — so any threshold has to be derived
from the fleet rather than assumed. Here the longest silence observed while a
session was demonstrably still working is 38.6 minutes, against reported
silences of 68.4 to 141.1 hours.

Do not build it on log mtime. That is what the 2026-08-15 re-measurement of
item 0001 did, and it reported eight inert sessions as "alive and working"
because the Copilot runtime keeps writing ExP polls, telemetry flushes and
IDE lock scans for as long as the process exists.

**This repository is frozen to safety fixes** (`FROZEN.md`), and the
supervision kernel now lives in `../operator`. A session that a supervisor
cannot tell has stopped is arguably a defect affecting running sessions, but
the remedy is new detection rather than a repair, so it most likely belongs in
the kernel. That call is the owner's, and it is why this is filed rather than
fixed.

**The eight sessions were left running.** They hold days of context that no
handoff has been written for, and killing them would destroy the only copy.
Whoever acts on this should decide that deliberately.

## Re-measurement, 2026-08-16: all eight woke, and the wake was an inbound message

Measured 2026-08-16T01:25Z, ten hours after the section above. **Every one of
the eight recovered.** Eleven supervised `copilot` processes are live, none of
them the pids tabulated above, and the marker is current in most:

| pid | proc started (UTC) | MB | log mtime | markers in file | newest `Forwarding event` | age |
|---|---|---|---|---|---|---|
| 20076 | 00:10:06 | 49.5 | 4.5 min | 39,327 | 2026-08-16T00:36:16Z | 0.81h |
| 24048 | 2026-08-15T22:57:07 | 119.6 | 7.5 min | 151,144 | 2026-08-15T23:57:30Z | 1.46h |
| 42876 | 00:10:21 | 236.4 | 0.0 min | 160,700 | 2026-08-16T01:25:10Z | 0.00h |
| 47944 | 00:09:57 | 26.9 | 4.7 min | 36,831 | 2026-08-16T00:37:53Z | 0.79h |
| 73400 | 00:42:04 | 97.3 | 0.1 min | 76,277 | 2026-08-16T01:25:04Z | 0.00h |
| 74612 | 00:12:39 | 93.2 | 2.0 min | 47,684 | 2026-08-16T01:20:51Z | 0.07h |
| 75208 | 01:21:04 | 6.0 | 0.0 min | 4,734 | 2026-08-16T01:25:09Z | 0.00h |
| 82116 | 00:14:32 | 54.4 | 0.1 min | 55,706 | 2026-08-16T01:22:13Z | 0.05h |
| 86508 | 00:09:46 | 17.0 | 4.9 min | 28,481 | 2026-08-16T00:36:06Z | 0.82h |
| 91392 | 00:09:47 | 169.0 | 0.1 min | 111,040 | 2026-08-16T01:25:06Z | 0.00h |
| 91624 | 01:10:48 | 13.5 | 3.9 min | 6,028 | 2026-08-16T01:19:53Z | 0.09h |

Same instrument, same two controls as above: `75208` is the session that took
the measurement and is the positive control, and the marker count per file is
in the tens of thousands throughout, so a quiet marker is a reading rather than
a build that stopped emitting it.

### What woke them, measured per instance rather than per cluster

`trace.jsonl`, window 00:05Z–00:20Z. For each instance: the `operator send`
addressed to it, its `operator reply`, and its `session_exit`.

| instance | `send --to` | its `reply` | latency | `session_exit` | restart |
|---|---|---|---|---|---|
| subtitle-localizer | 00:06:25 | 00:06:38 | 13s | 00:09:17 | True |
| ac-unreal | 00:08:11 | 00:08:21 | 10s | 00:09:51 | True |
| book-translator | 00:08:12 | 00:08:23 | 11s | 00:09:36 | True |
| discord-invite-manager | 00:08:12 | 00:08:22 | 10s | 00:09:27 | True |
| finances | 00:08:13 | 00:08:24 | 11s | 00:09:17 | True |
| prism | 00:08:14 | 00:08:26 | 12s | 00:12:09 | True |
| scripts | 00:08:15 | 00:08:26 | 11s | 00:14:02 | True |
| snes-ghosts | 00:08:16 | 00:08:34 | 18s | 00:09:51 | True |

**Eight for eight, each reply following its own message by 10 to 18 seconds.**
The pairing is what makes this causal rather than two clusters that happen to
overlap: `subtitle-localizer` was sent to 106 seconds before the other seven
and replied 106 seconds before them, so the offset instance is the control on
the batch. Every one then ended `restart=True` — it wrote a handoff — and its
supervisor relaunched it, which is where the eleven live pids above come from.

The sends came from a supervised `copilot-tools` agent session (pid 2596 in the
invocation ancestry), not from a human and not from the harness.

### What that establishes, and what it does not

- **The state is idle, not wedged.** A session silent for 2.8 to 5.9 days
  answered in 10 to 18 seconds. Nothing that had hung, deadlocked or lost its
  model connection does that. The section above declined to call it either way
  and was right to; this settles it.
- **The state is recoverable, and the recovery is a turn.** `operator send`
  delivers into the running session, which is exactly the thing the supervisor
  has no way to do. The note above records one spontaneous recovery with
  neither transition observed; here are eight with both.
- **Nothing in the harness could have done it.** The supervising loop advances
  on exactly four conditions — the stop marker, the detach marker,
  `is_copilot_running()` going false, and the restart marker
  (`copilot_operator.py` 5640-5800). There is no liveness probe of a running
  session and no nudge; `grep -i 'idle|nudge'` over `copilot_operator.py`
  returns only the no-change progress breaker, which is itself driven by
  endings. So an agent that finishes a turn without handing off parks its
  instance until something outside sends it a message.
- **Detection remains the whole gap.** Recovery took 18 seconds once triggered;
  the trigger took between 2.8 and 5.9 days to arrive, and it arrived because
  an agent read this backlog item, not because anything measured the fleet.

**Not established: why they stopped.** Eight agents ending a turn without a
handoff still has no measured cause, and the wake tells us nothing about it —
being woken by a message says only that the session could still take a turn.
This item has been burned three times by an explanation that fitted; that
question is still open.
