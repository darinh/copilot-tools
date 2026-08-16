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

Same instrument as the table above, and the same positive control: `75208` is
the session that took the measurement. The count column is the second control —
the marker occurs thousands of times in every one of these files, so a quiet
marker is a reading rather than a build that stopped emitting the string.

**The count control is weaker than the section above claimed, and the age
column carries less than it looks.** Two corrections to how this table should
be read, both applying to the table above as well:

- The marker fires on *any* forwarded event, including the
  `session.background_tasks_changed` churn that the section above already had
  to set aside. A recent marker therefore establishes that the session layer is
  doing something, not that the agent is. Classify the event before concluding
  (see the next subsection, which does).
- Marker abundance shows the string was emitted in the past, which rules out a
  build that never emits it. It does not rule out a build that stopped, and
  the count for a young log — 4,734 and 6,028 here — is a fact about the log's
  age rather than about the marker's health.

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
- **No automatic path in the harness could have done it.** The supervising loop
  advances on the stop marker, the detach marker, `is_copilot_running()` going
  false, the restart marker, and its own shutdown signal
  (`copilot_operator.py` 5640-5800). None of those is "a live session went
  quiet": there is no liveness probe of a running session and no nudge, and
  `grep -i 'idle|nudge'` over `copilot_operator.py` returns only the no-change
  progress breaker, which is itself driven by endings. So an agent that
  finishes a turn without handing off parks its instance until something sends
  it a message.

  `operator send` *is* harness code — it types into the target pane — so the
  claim is not that the toolkit cannot deliver a turn. It is that nothing
  decides to. Every wake in the table above was invoked by an agent that had
  read this backlog item.
- **Detection remains the whole gap.** Recovery took 18 seconds once triggered;
  the trigger took between 2.8 and 5.9 days to arrive, and it arrived because
  an agent read this backlog item, not because anything measured the fleet.

**Not established: why they stopped.** Eight agents ending a turn without a
handoff still has no measured cause, and the wake tells us nothing about it —
being woken by a message says only that the session could still take a turn.
This item has been burned three times by an explanation that fitted; that
question is still open.

### The state came back within ninety minutes, and it is measurable live

Measured 2026-08-16T01:39Z, fourteen minutes after the table above, this time
classifying the newest forwarded event by *type* rather than only dating it.
The type is what names the state: an age says a session has been quiet, and
`session.idle` says it finished a turn and stopped.

| pid | newest event of any kind | newest non-background event |
|---|---|---|
| 20076 | 1.05h `session.background_tasks_changed` | 1.05h **`session.idle`** |
| 24048 | 1.70h `session.background_tasks_changed` | 1.70h **`session.idle`** |
| 29312 | 0.00h `assistant.tool_call_delta` | 0.00h `assistant.tool_call_delta` |
| 47944 | 1.02h `session.background_tasks_changed` | 1.02h **`session.idle`** |
| 58612 | 0.00h `model.call_start` | 0.00h `model.call_start` |
| 74612 | 0.06h `session.background_tasks_changed` | 0.06h `hook.end` |
| 75208 | 0.00h `session.background_tasks_changed` | 0.00h `hook.end` |
| 82116 | 0.00h `model.call_start` | 0.00h `model.call_start` |
| 86508 | 1.05h `session.background_tasks_changed` | 1.05h **`session.idle`** |
| 91392 | 0.00h `assistant.reasoning_delta` | 0.00h `assistant.reasoning_delta` |
| 91624 | 0.00h `assistant.streaming_delta` | 0.00h `assistant.streaming_delta` |

**Read the two columns as agreeing, because they do.** A first draft of this
section presented the split as though background churn went on flattering the
fleet after the agent stopped, and it does not: in all four quiet logs the last
`session.background_tasks_changed` lands 13 to 50 ms after `session.idle` — it
is the trailing event of the same turn, not activity during the silence. `20076`
emitted `session.idle` at 00:36:16.470Z and its last background event at
00:36:16.498Z, and nothing follows in either column. The instrument that does
keep advancing through the silence is the log's mtime, which the section above
already disposes of. **Do not read "the two columns differ" as the detector's
signal here; there is no row in which they differ.**

**Four instances are already back in the state this item describes** — newest
non-background event `session.idle`, between 1.0 and 1.7 hours ago, each with a
live process and a supervisor reporting `looping`. Three of them started at
00:09–00:10Z as replacements for the woken sessions, so they worked for roughly
half an hour and stopped.

Two things follow that the days-old measurement could not show:

- **The wake is not a fix.** It bought about thirty minutes on those three. The
  state recurs on the ordinary path, not as a rare accident, and the eight
  sessions silent for days were an accumulation of it rather than one event.
- **It does not need days to observe.** The signature is visible about an hour
  in, from one file, on a machine anyone can run this on. Whatever detector is
  eventually built has a same-day test rather than a week-long one — and this
  table is what it must reproduce: four sessions whose newest forwarded event
  of any kind is over an hour old and is `session.idle`, while their processes
  are up, their logs' mtimes are minutes old, and `operator list` calls all
  eleven `looping`.

Two things this table does **not** show, against the reviewer's checks rather
than by assumption. None of the four is mid-tool-call: `tool.execution_start`
and `tool.execution_complete` occur in equal numbers in each of their logs
(166/166, 500/500, 151/151, 85/85), so nothing was still running. And 1.02h is
safely past the 38.6-minute ceiling this item measured for a silence with the
agent demonstrably still working, though that ceiling is empirical and a fleet
with longer tool calls should re-derive it.
