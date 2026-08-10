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

## Correction, 2026-08-05: the handoff evidence above was an instrument artifact

**Everything in *Evidence* about handoffs never being written is withdrawn.**
The kills are real and unexplained; the claim that no handoff has ever been
written was measured with three instruments, and all three were incapable of
reporting the thing they were read as ruling out.

1. *"940 `session_exit` events, every one carries `restart=False`."*
   `_record_session_exit` was called from exactly one place:
   the `else` of `if marker_set(instance.restart_marker)`. It only ran once
   the restart marker had already been established absent, so `restart` could
   not carry `True` in any record, ever. Sessions that *did* end by
   handoff took the other branch and were **not recorded at all** — the
   population excluded the cases being counted, and an excluded case does not
   look like a missing row.

   The `False` was not even reliably a `False`. `marker_set` answers the same
   way for "the marker is not there" and "the probe could not look", and the
   unreadable-marker guard on that path checked only the stop and detach
   probes — so an unreadable restart probe was written down as definite
   absence. Both are fixed here: the branch now records the tri-state
   `marker_state` result, so a `False` in a record dated 2026-08-05 or later
   means the marker was observed absent, and `null` means nobody could tell.

2. *"`handoff` appears 0 times in the entire trace."* `handoff_tool.py` does
   not import `operator_trace` and never has. The trace records `operator`
   invocations; `handoff` is a separate console script, so its absence there
   is a fact about which program writes the file.

3. *"No `next-session.md` exists for this project."* True at that instant and
   consistent with the opposite conclusion: the protocol has the *reader*
   delete it at session start — a convention agents follow rather than
   something any code here enforces — so an absent handoff is the normal
   steady state between a session starting and the next one ending. This is
   the weakest of the three refutations: it establishes that the observation
   cannot distinguish the two cases, not that the handoff was there. The
   positive counter-evidence below is what settles it.

Positive counter-evidence, measured 2026-08-05T02:20Z:
`~/.copilot/projects/{guid}/next-session.md` was written at 02:16:00Z with a
full handoff, and the supervisor relaunched 33 seconds later at 02:16:33Z. The
state file advanced `SESSION_NUM` 221 → 223, and session 222 — the one that
wrote it — appears **nowhere** in `trace.jsonl`, whose last record of any kind
is 40 minutes earlier. That is the branch above, observed. `superseded/`
holding 10 files points the same way: that directory is only written by the
preserve-then-publish path, which runs when a handoff is being *written* over
an unread one.

Both instruments are fixed as of this correction: `session_exit` is now
recorded on the restart paths too, with the marker state passed in rather than
re-probed at a call site that had already decided it (see
`tests/test_loop_resilience.py::test_a_session_ended_by_a_restart_request_is_traced`).
**The trace is only trustworthy for sessions after that change**, so any
re-measurement must be scoped to records at or after 2026-08-05 — the 979
older ones cannot answer this question and never could.

There was also a mechanism actively teaching agents the wrong thing here. The
"a handoff file could not be found" note was decided **once**, before the
supervisor's loop began, and then reused for every session of the run. This
project's run started 2026-07-30 and had reached session #223, so sessions
were being told their predecessor had crashed on the strength of a probe taken
25 days and hundreds of handoffs earlier. Fixed in the same change. An agent
reading that note is a plausible origin for this item's framing, and it means
the *reports* of lost handoffs are not independent of each other.

**What still stands, unchanged:** sessions are being killed mid-turn, in
broadcast waves, by something unidentified, and the copilot debug log evidence
(`0xC000013A` across all seven extension hosts within 22ms) is untouched by
any of this. What is no longer established is that the kills cost a handoff
every time. Re-measure before assuming either way.

## Correction, 2026-08-05: the fixed instruments were never running

The correction above ends by telling its reader to scope any re-measurement
to records "at or after 2026-08-05". **That instruction was already false when
it was written, and following it would have produced the same wrong answer a
third time.**

Measured at 2026-08-05T06:30Z, before anything was changed:

- Every loop supervisor on this machine was started at 13:27–13:28 local on
  2026-08-04 (`Win32_Process` creation times; `copilot-tools` is pid 62628,
  started 13:28:41).
- `crash_recovery_verdict` and the `session_exit` recording fix landed in
  8f58a00 at **19:36 local on 2026-08-04** — about six hours *after* every
  supervisor had already started.
- A supervisor is a long-lived process that imported the operator's code once,
  at startup. `restart-loop`'s own docstring says so. None had been restarted,
  so every supervisor was still executing pre-fix code, and every record it
  wrote was a pre-fix record wearing a post-fix date.

Two independent observations confirm it rather than inferring it:

1. This session was launched at 23:29:52 local and told "a handoff file could
   not be found", while `next-session.md` had been written at 23:29:22 — 30
   seconds *earlier* — and was sitting on disk, and was read. That is the
   stale once-per-run verdict 8f58a00 removed, still being served.
2. `trace.jsonl` contains **no `session_exit` of any kind after
   2026-08-05T01:02:59Z**, across five relaunches in the following hours.
   That is the unrecorded restart branch, observed: those sessions ended by
   handoff and the pre-fix code recorded nothing at all for them.

So the population is still censored, and the censoring is invisible in the
data. This is the third iteration of one failure — an instrument that cannot
report the thing it is being read as ruling out — and dates cannot detect it,
because the thing that goes stale is a *process*, not a file.

**Fixed by stamping provenance rather than trusting the clock.** Each
supervisor now records the digest of the operator source it actually loaded
(`{instance}.loopcode.json`), emits a `supervisor_start` trace event carrying
it, and stamps that digest into every `session_exit` it writes. A re-measurement
is now scoped by `code=`, which is a fact about the instrument, and records
written before this read `code=unrecorded` — which is the honest answer for
every record discussed anywhere above. `operator ls` names any instance whose
supervisor has fallen behind the code on disk, with the `restart-loop` command
to fix it, because nothing surfaced that before and the remedy already existed.

Note that a toolkit *version* could not have carried this: 8f58a00 changed
`copilot_operator.py` and bumped no version, so a version comparison would have
called the stale supervisor and the fixed one identical.

### A fourth hypothesis for the kills, refuted the same day

*One shared multiplexer server.* If all instances were panes of a single
tmux server, that server dying would kill every session within milliseconds —
which fits the observed signature exactly, including disjoint consoles. It is
wrong: each instance has **its own** `tmux.exe server -s <name>` process with
a distinct parent (measured, 2026-08-05T06:35Z). The correlation that
suggested it is real but is not causation — the currently-running `__warm__`
server started at 01:02:58Z, inside the last kill wave (01:02:52–01:02:59Z),
i.e. it is a *survivor of* that wave, not its cause.

### The kills themselves, re-measured

979 `session_exit` records spanning 2026-08-04T00:20Z (the trace's first
record — "its whole history" is about 30 hours, not the 25-day run) to
2026-08-05T01:02:59Z. Clustering exits within 60s of each other gives 175
waves, of which **156 hit four or more distinct instances** and 135 hit
exactly six. Median session uptime 344s. Inter-wave gaps during active periods
are 6–7 minutes and match the following wave's uptimes, so sessions are
running until the next broadcast rather than dying of anything of their own.

**No wave has occurred since 2026-08-05T01:02:59Z** — 5.5 hours at time of
writing, spanning at least five clean handoff-ended sessions. Whether that is
a fix, a coincidence, or the emitter merely being idle is *not* established.
It is recorded here so the next re-measurement has a boundary to test against,
and this time the records on either side of it can be told apart by their
`code=` stamp instead of their date.

## Correction, 2026-08-05: the boundary was testable all along, and it holds

The correction above tells its reader to scope the re-measurement by `code=`.
**That instruction is not usable either, for the same reason as the one it
replaced, one level further up.** The `code=` stamp is written by the change
that introduced it, and no supervisor has loaded that change.

Measured 2026-08-05T11:35Z, before anything was altered:

- The six supervisors are the *same processes* as at the 06:30Z measurement —
  pids unchanged, all created 2026-08-04 13:27:53–13:28:41. None has
  restarted.
- `~/.operator/restart/` holds no `*.loopcode.json` for any instance, though
  every instance has the `*.loopargs.json` written by the neighbouring line of
  the same startup path. The stamp has never been written.
- `trace.jsonl` contains **zero** `supervisor_start` events over its whole
  history, and all 979 `session_exit` records read `code=unrecorded`.

So a third re-measurement scoped as instructed would again have found nothing
and concluded nothing. The lesson from the previous correction generalises
one step further: **an instruction that depends on a fix is only as good as
the deployment of that fix**, and "landed on `main`" is not deployment when
the consumer is a process that imported its code days ago.

### The instrument that was supposed to catch this said nothing

`operator ls` was given a staleness notice in that same change, precisely so
this could not recur. It printed nothing. A supervisor with no record read
`CODE_UNKNOWN`, and `CODE_UNKNOWN` is deliberately silent to keep the notice
from becoming noise — so on a machine where **not one supervisor could be
checked at all**, the output was byte-identical to a machine where every
supervisor was current. That is the fourth iteration of this item's signature
failure, now inside the remedy for the third.

The defect is one this repository already has a name for: a read that failed
and a read that returned nothing were collapsed into one answer. `_digest_file`
keeps the two apart, and says in its own docstring why — but the outer read of
the record did not, catching `OSError` and `ValueError` together. A record
*observed absent* while its supervisor is running is a definite observation,
not a failure to look: the record is written by the same change that reads it,
so a running supervisor that has left none either predates it or could not
write one, and both mean the verdict is unavailable until it restarts.

Fixed here by splitting `CODE_UNRECORDED` out of `CODE_UNKNOWN` and reporting
it as its own group with the same `restart-loop` remedy and a different
reason. Verified against this machine rather than only in tests: the patched
`operator list` names all six supervisors, where the shipped one printed
nothing.

Reporting the absence at all created a second problem, which adversarial
review caught before this was published: the startup wrote the loop pid file
*before* the code record, so an `operator ls` landing in that window would see
a live supervisor with no record and tell a perfectly healthy one, running the
newest code there is, to restart. A notice that is sometimes wrong stops being
read — which is how this item got here. The three startup writes now live in
`_publish_supervisor_records`, which writes the records first and the pid file
last, making the pid file the commit point: once it exists, what describes it
already does. The reverse window costs nothing, because nothing treats a
supervisor as running on the strength of a record alone.

Two of the three reviewers read that reordering as *introducing* a race in
which a starting supervisor is invisible to `operator stop` and
`operator restart-loop`. The race is real and is now item 0010; the
attribution is not. The pid file is written near the end of a startup whose
floor — interpreter plus import, nothing else — measures 105 ms, and moving
the write after the two records costs 1.9 ms of that, 1.8%. Reverting the
order would leave the window essentially unchanged and bring back the false
notice. Worth recording as its own small lesson: **a plausible cause offered
by review still has to be measured**, which is the same discipline this item
has needed four times now.

### What the censored trace could still see: the kills

The previous correction concluded that records after the boundary "cannot
answer this question". That is too strong, and the over-correction cost this
item a whole cycle. **A censored population is censored in a direction, and
the direction decides which questions survive it.**

The pre-fix code the supervisors are running calls `_record_session_exit` from
the `else` of `if marker_set(instance.restart_marker)` (verified in
`ea2331b:copilot_operator.py`, the parent of the fix). The branch that writes
nothing is the *handoff* branch. A session killed mid-turn leaves no restart
marker, takes the `else`, and **is recorded** — by exactly the code that is
running. The blind spot runs opposite to the question this item asks.

That is a statement about one branch, not a guarantee, and the difference
matters on an item that has already survived three explanations that fitted.
**A missing record is not by itself proof of a missing kill.** Four ways a
kill could leave the trace empty, and what closes each of them here:

1. *Recording is best-effort.* `_record_session_exit` wraps its whole body in
   `except Exception: return`, so a kill whose record failed to write looks
   exactly like no kill. Nothing in the trace can exclude this; it is why the
   conclusion below rests on a second instrument rather than on this one.
2. *The supervisor is killed along with its session.* Then no code runs to
   record anything. Excluded by measurement: all six supervisors are the same
   processes throughout the window, pids created 2026-08-04 13:27:53–13:28:41
   and still alive at 12:13Z. Their pids (5928, 15584, 37256, 38140, 62628,
   38268) are the very ones stamped on the last `session_exit` records the
   trace holds — so the processes that recorded the final kills are still
   running, and have recorded nothing since. No supervisor died; every ending
   was observed by a live one.
3. *The unreadable-marker path.* If the stop/detach markers cannot be examined,
   the supervisor takes the `unknown_markers` branch, logs, and continues or
   gives up without recording. Excluded: `operator.log` holds no
   "markers cannot be examined" line and no `Giving up` line after the
   boundary.
4. *Shutdown mid-poll.* A signal arriving during the poll sleep leaves the loop
   before the exit is classified. Excluded by the same log: a supervisor that
   left its loop would have stopped launching sessions, and all six kept
   launching.

Measured 2026-08-05T12:10Z, 11.1 hours after the boundary:

- Across the six live instances, `SESSION_NUM` has advanced 66 past the last
  session each has a `session_exit` for. One session per instance is still in
  flight, so **60 sessions have ended** since 01:02:59Z.
- `trace.jsonl` records `session_exit` for **none** of them.
- The trace is not merely dead: it holds 52 other records after the boundary,
  the most recent minutes before the measurement.

The conclusion rests on `operator.log`, which the same supervisors write
through a different code path, so a failure of the trace's recorder cannot
also silence it. After the boundary that log holds **60 `restart signal
detected!` and zero `copilot exited unexpectedly`**. Sixty derived endings,
sixty restart signals, from two instruments that share no code: every ending
in the window is individually accounted for as a handoff, leaving none
unexplained for a kill to hide in. Over its whole life the same log holds 290
restart signals against **1150** unexpected exits, so it is demonstrably
capable of recording the event whose absence is being claimed.

The two instruments also agree on the last kill itself. `operator.log` has
`Session #36: copilot exited unexpectedly after 345s` at 18:02:59 local, and
the final `session_exit` in the trace is `scripts` session 36 at
2026-08-05T01:02:59Z. The boundary is a single event seen twice, not an
artifact of where one instrument stops.

**No kill has been observed by either instrument since 2026-08-05T01:02:59Z —
11.1 hours and 60 sessions, every one of which is individually accounted for
as a handoff.** That is a measurement rather than the previous hedge, and it
is still a statement about what two instruments can see: hypothesis 1 above
has no independent refutation, only the implausibility of 60 consecutive
recorder failures that spared the log.

What is still not established is *why*, and nothing here identifies the
emitter — the fifth hypothesis is still owed. Two things about the boundary
remain unexplained and should not be smoothed over: nothing landed at
01:02:59Z that anyone has connected to the kills, and the surviving `__warm__`
multiplexer server started at 01:02:58Z, inside the final wave, which is a
coincidence this item has already been burned by once. The item stays open on
the cause. What changes is that the harm is no longer ongoing, so the next
reader is diagnosing a stopped fault, not a live one — and should check first
whether it has resumed, by the same two instruments, before anything else.

**Re-measurement recipe, for whoever is next.** It does not depend on any
unshipped fix:

- `operator.log`: count `restart signal detected!` against `copilot exited
  unexpectedly` after your boundary. Non-zero unexpected exits means the
  kills are back.
- `trace.jsonl`: under pre-fix supervisors a `session_exit` record means a
  *non-handoff* ending, not necessarily a kill — `operator stop-session` and
  any other deliberate teardown take the same branch. Treat a post-boundary
  record as a lead and corroborate it against `operator.log`, which
  distinguishes `copilot exited unexpectedly` from a requested stop, before
  calling it a kill. Once a supervisor has been restarted onto post-fix code,
  its records carry `code=` and both endings appear, so check
  `markers.restart` too.
- `operator list` now names any supervisor whose code is stale or unrecorded.
  If it names one, that instance's records are pre-fix and the rule above
  applies to them.

Two arithmetic traps, both of which produced a wrong sentence in this session
before being caught:

- **Compare against the boundary exclusively.** The last kill happened *at*
  01:02:59Z, so `>=` counts it as post-boundary and reports one unexpected
  exit in a window that has none. The same instant is the end of the old
  regime, not the start of the new one.
- **Subtract the sessions still in flight.** `SESSION_NUM` counts the session
  currently running, so endings is the advance minus one per live instance —
  66 across six instances is 60 endings, not 66. Getting this wrong breaks
  the match against the log's restart-signal count, which is the whole
  corroboration.


## Re-measurement, 2026-08-09: the kills have resumed, and this one has a cause

Run by the recipe above, which needed no unshipped fix. **The fault is live
again**, and for the first time a wave was measured 90 seconds after it
happened rather than hours or days later — this session was launched by it.

`operator.log`, exclusive of the 2026-08-05T01:02:59Z boundary:

- **9 `copilot exited unexpectedly` against 188 `restart signal detected!`.**
  Six of the nine are one wave, at 2026-08-09 17:25:44–49 local, hitting six
  distinct instances in five seconds. The other three are isolated singles
  (08-07 21:54, 08-08 16:22, and one more inside the wave second) and are not
  claimed here as anything.
- Uptimes in the wave were 90220s to 238902s — **1.0 to 2.8 days**, against a
  median of 344s in the original population. These sessions were not dying of
  anything of their own.

So the 11.1-hour quiet period the previous correction recorded held for about
four and a half days and then ended.

### The cause of this wave: the interactive logon session was replaced

Measured from the Windows event logs, which no previous iteration of this item
consulted:

- `Microsoft-Windows-TerminalServices-LocalSessionManager/Operational`:
  **17:25:08 session 2 disconnected**; 17:25:30 begin session arbitration;
  **17:25:54 event 21, "Session logon succeeded: User: CABRO\darin Session
  ID: 4, Source Network Address: 192.168.88.4"**. A new logon session, not a
  reconnection to the existing one.
- `System`: twelve **per-user services** — the `_4c456a` suffix is the logon
  session's LUID — logged 7031 "terminated unexpectedly" together at
  17:25:52, followed by Winlogon 7001 at 17:25:54. That is a logon session
  being torn down and another built, not a service fault.
- No reboot: `LastBootUpTime` is 2026-08-04 10:02:54 and unchanged, and there
  is no `Kernel-Power` boot event.

Everything in a torn-down session dies, which accounts for the signature that
refuted the first four hypotheses without needing any of them: consoles are
disjoint because the session teardown does not travel through a console; each
instance's own multiplexer server dies because it is *in* the session; no
operator command coincides because none is involved. It also matches the
original copilot debug log evidence — `0xC000013A`,
`STATUS_CONTROL_C_EXIT`, across all seven extension hosts within 22ms — which
is what console processes report when the session they belong to goes away.

**The discriminating evidence, which is why this is offered as a cause at all
and not a fifth thing that fits.** The same log holds *dozens* of
`event 25 — Session reconnection succeeded` to session 2 across 08-07, 08-08
and 08-09, several of them minutes apart. **None of them killed anything.**
The single event 21 in the entire history — a new session ID for the same
user — is the one that coincides with a kill wave, to the second. A
reconnection preserves the session and costs nothing; a new logon replaces it
and takes every process with it.

### It does *not* explain the original waves, and is not offered as doing so

Checked before claiming anything, because this item has already survived four
explanations that fitted:

- Across 2026-08-04 12:00 → 2026-08-05 01:03 local, the window holding ~175
  waves and 979 `session_exit` records, the same log holds **only event-25
  reconnections to session 2 — no event 21, no new session at all.** Kills
  every 6–7 minutes for 30 hours cannot be logon replacements that did not
  happen.
- The two populations differ in a second, independent way. The previous
  correction established by measurement that **the supervisors survived** the
  original waves — same pids throughout. On 2026-08-09 **every supervisor
  died too**: `~/.operator/restart/*.loop.pid` and the loopcode records were
  all rewritten at 17:26:36–37, in the new session 4, and the copilot-tools
  supervisor's `Win32_Process` creation time is 2026-08-09 17:26:36. A
  session teardown cannot spare a supervisor that lives in that session.

So there are **two distinct phenomena** under this item. This one is
explained; the original waves are not, and the fifth hypothesis for them is
still owed. The item stays open.

### A fifth instrument that could not report what it was read as reporting

`operator list` printed this at 17:27, ninety seconds after every supervisor
on the machine had been destroyed and relaunched:

    copilot-tools  ·  looping · session #240  ·  up 10d 13h  ·  ~\repos\copilot-tools

`up` is `_age_since(RUN_STARTED)`, and `RUN_STARTED` is persisted in the state
file and deliberately carried across a supervisor restart — `launch_loop`
reads it back with `state.get("RUN_STARTED", run_started)`. So the one field
on the row was the one field that a mass kill cannot change, and the output
was **byte-identical to a machine where nothing had happened**. That is this
item's signature failure for the fifth time, and the third time inside the
remedy for a previous one: the staleness notice added last time stayed silent
too, correctly — a supervisor relaunched from current code *is* current.

The supervisor's own start instant was on disk the whole time.
`_save_loop_code` has always stamped `recorded` into `{instance}.loopcode.json`
(`"recorded": "2026-08-10T00:26:37Z"` for the supervisor in question). Nothing
read it. **Fixed here** by `loop_started_at` and `supervisor_restarted_after`:
a supervisor that started materially later than the run it is running is not
the process that began that run, so the session it was running died with it,
and `operator list` now says so per row and explains the cost in a group at
the foot. Verified against this machine rather than only in tests — the
patched `operator list` names all eight instances as
`[supervisor restarted 19m 43s ago]`, where the shipped one printed nothing.

Three things about the fix are deliberate, and **two of them are corrections
made after adversarial review, not part of the first draft.**

The margin (5 minutes) errs towards silence, and it is load-bearing rather
than merely defensive. The first draft justified it with the claim that a
supervisor publishes its records *before* the run's first session is launched,
so on a fresh run it is older than `RUN_STARTED`. **That is backwards.**
`launch_loop` stamps `run_started = utcnow()` and only afterwards reaches
`_publish_supervisor_records`, so a healthy fresh supervisor is recorded
*later* than its run by however long startup takes. Without a margin every new
run would announce itself as a restart. The direction of the remaining error
is chosen: too wide misses a restart within five minutes of a run beginning,
too narrow calls every ordinary startup a restart, and a notice that is
sometimes wrong stops being read — which is how this item got here.

**A deliberate handover leaves the identical trace and had to be excluded.**
`operator restart-loop` retires one supervisor and hands the live session to a
new one on purpose — its docstring is "Replace an instance's loop supervisor,
leaving its session running" — and the result on disk is exactly what a
destroyed supervisor leaves: one younger than the run it is running. The first
draft reported those too, and told the reader the session had "died mid-turn
and without a handoff" when `restart_loop` exists precisely to keep it alive.
`_save_loop_code` now stamps `adopted`, and `supervisor_took_over` requires
both that the supervisor did not begin the run and that it did not record
adopting a session. A supervisor that recorded *nothing* about adopting is
still reported — every one predating the stamp is in that state, and skipping
them would be the same silence-as-all-clear — so the wording stops at what the
record establishes and makes no claim about cost. Whether a handoff was
written is a separate question with its own instrument
(`crash_recovery_verdict`).

**A leftover record must not describe the live supervisor.** `_save_loop_code`
tolerates a failed write by design, so a supervisor whose record could not be
rewritten keeps its predecessor's — which would date a live process by a dead
one and hide the very restart this reports. The record already carried a
`pid`; nothing checked it. `loop_started_at` and `loop_adopted` now take the
running supervisor's pid and report "cannot tell" on a mismatch.

An absent or unreadable record yields no claim rather than "did not restart";
the missing record is already reported in its own right by `loop_code_state`.

### A test that was asserting the right answer for the wrong reason

Found while adding the above, and repaired in the same change.
`tests/test_supervisor_code_staleness.py::_unreadable` simulates a revoked
file by patching `builtins.open`. `_digest_file` calls `open()` directly so it
was genuinely denied — but `Path.read_text` goes through **`io.open`**, which
is the *same function object under a different name*, so rebinding one leaves
the other untouched. Measured with a probe: under `_unreadable`, `read_text`
succeeded and the direct `open` was refused.

`test_an_unreadable_record_is_unknown_not_unrecorded` therefore never had its
read denied. It passed because the record it wrote, `{}`, has no `files` key,
and a record with no files is `CODE_UNKNOWN` anyway — the assertion held for
any implementation, including one with the branch it names deleted. The helper
now patches both names, and that test writes a record agreeing with disk, so
it would answer `CODE_CURRENT` and fail if the denial ever stopped biting.

**Recipe for the next reader, unchanged except for one addition.** When a wave
is found, check the Windows event log before anything else:

    Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-TerminalServices-LocalSessionManager/Operational'; Id=21,25}

An **event 21** (new session ID for the same user) at the wave's timestamp is
this cause. An **event 25** (reconnection) is not — those are frequent and
harmless, and treating them as suspicious would bury the one that matters.
`operator list` now names any supervisor that restarted without recording that
it adopted a session, which is the cheapest available detector for a wave that
has already happened: several instances reporting the same restart age is one
broadcast, not several coincidences. A single instance named on its own is far
more likely to be an `operator restart-loop` from a supervisor predating the
`adopted` stamp than a kill — the flag only distinguishes them for supervisors
started after this change.
