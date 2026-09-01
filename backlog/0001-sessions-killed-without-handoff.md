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
file and deliberately carried across a supervisor restart — `run_loop_mode`
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
`run_loop_mode` stamps `run_started = utcnow()` and only afterwards reaches
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

**And a pid is not an identity.** All three adversarial reviewers
independently reached this, which is the strongest signal any of them
produced. Windows recycles pids aggressively, and `_save_loop_code` tolerates
a failed write, so a supervisor that could not replace its predecessor's
record can be handed that predecessor's pid by the OS — and a pid-only check
reads a dead process's record as its own, which is the hole this section
claimed to close. `_save_loop_code` now also stamps `pid_start`, from
`operator_liveness.process_start_token`, which the repository already uses for
exactly this in `operator_session` and `operator_work`: it is compared only
for equality and only for the same pid, so a recycled pid carries a different
token. Measured on this machine — `win:134308020110986193` for the live
process, its own record reading `True` and a forged token `False`.

Absence is treated differently from the `pid` above, and deliberately.
`pid_start` has a real pre-stamp history where `pid` had none, so a record
without a token, or a live process whose token cannot be read, falls back to
the pid comparison. Refusing those instead would have reported every
supervisor on the machine as a leftover the day it shipped.

### The remedy reproduced the failure it was built for

The first draft answered `CODE_UNKNOWN` on a pid mismatch. `list_instances`
prints a group for `stale` and a group for `unrecorded` and **nothing at all
for `unknown`** — and a mismatched record also silences `loop_started_at`,
`loop_adopted` and `loop_began_run`, so `supervisor_took_over` goes quiet too.
Every question about that row therefore went unanswered, and the row printed
byte-identical to a healthy one. That is this item's signature failure, for
the sixth time, inside the remedy for the fifth.

It is now `CODE_MISMATCH`: a fifth verdict rather than a shade of "cannot
tell", for the same reason `_read_loop_record` keeps "absent" and "could not
look" apart. It is a *positive* observation — a record was read and it names
somebody else — so filing it as an absence of evidence is a category error as
well as a silence. `operator list` names those instances in their own group
with the same restart remedy, and the row carries `[supervisor record is not
its own]`.

Caught by adversarial review, not by the suite: every test asserted on the
verdict `loop_code_state` returned, and none asked whether anything ever
printed it. A verdict no reader displays is indistinguishable from a verdict
that was never computed.

### Three more, from reviewing the remedy for the remedy

A second review round over the fix above, by readers from two more model
families, found three things independently of each other. All are the same
family of error — a rule justified by a history, applied one case too wide.

**Damage is not a legacy schema.** The fallback that lets a genuinely older
record through — one written before `pid_start` existed — also cleared `17`,
`[]` and `""`. `_save_loop_code` writes a non-empty string or `None`, so no
version of it produces those: they are corruption, and accepting them
rubber-stamps exactly the impersonation the token was added to refute. This
is the mistake corrected two paragraphs above for `pid`, made again one field
over, which is worth recording because the corrected reasoning was *directly
adjacent in the same function* and still did not transfer.

**A Windows measurement is not a cost.** Each of the four readers re-opens the
record and re-probes process identity. That was measured here at 0.021 ms per
probe and called free — but `operator_liveness._ps_start_token` shells out to
`ps` with a ten-second timeout on macOS and BSD, so `operator list` would fork
four subprocesses per instance there. This repository's `os.path` note already
says a green local Windows result is evidence about one leg; the same error
was made again with a stopwatch instead of a test suite. `loop_record_facts`
now answers all four questions from one read and at most one probe.

**A start token is boot-relative on Linux.** `_linux_start_token` is field 22
of `/proc/<pid>/stat`, counted in ticks since boot, so across a reboot a
replacement can collide with its predecessor on *both* pid and token — the one
case the token alone cannot refute. The record now carries `boot` from
`boot_identity()` and compares it with `same_boot`, which is exact for Linux's
boot uuid, tolerant for the Windows/macOS instant, and answers "cannot tell"
across kinds, so it only ever refutes on evidence.

The tests learned two things from the same round. The parametrised verdict
sweeps in `tests/test_preamble_code_staleness.py` were hand-written lists, so
`CODE_MISMATCH` could be added to the module without any of them failing —
they would simply have kept passing over a set that no longer described the
code. They now derive from one named tuple that is checked against the
`CODE_*` constants by introspection, verified by adding a constant to a
scratch copy and watching it go red. And several token tests monkeypatched a
probe that the branch under test short-circuits before reaching; a patch that
is never reached looks exactly like one that is, so they now count the calls.

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

## Re-measurement, 2026-08-10: a sixth instrument, and this one was the discriminator

Run by the recipe above. Two separate findings: the kills are live but have
changed shape, and the one measurement that could tell a crash from a kill has
been disarmed on every ending since the toolkit was written.

### The kills have not stopped, but they are no longer waves

`operator.log`, exclusive of the 2026-08-09 17:25 wave already recorded above:

- **Five more `copilot exited unexpectedly`**, at 18:46:33, 22:19:19, 22:20:06,
  22:29:00 and 22:31:20 local, hitting `copilot-tools`, `snes-ghosts`,
  `ac-unreal` (twice) and `copilot-tools` again.
- They are **not** a broadcast. The five span 3h45m, and the four in the last
  twelve minutes hit three different instances at 47s, 8m54s and 2m20s apart —
  nothing like the 5-second, six-instance signature of a wave.
- The `TerminalServices-LocalSessionManager` log holds **no event 21** in that
  window, only routine event 24/25 disconnect-reconnect pairs. So the logon
  replacement that explains the 17:25 wave explains none of these.
- Uptimes were 4794s, 1005s, 17606s, 529s and 8629s — no common lifetime.

Recorded as a boundary for the next reader, not as a hypothesis. Five isolated
endings across three instances is equally consistent with five independent
causes, and the section below is why that could not be settled either way.

### The exit code was the discriminator, and it was queued behind a log parse

`markers.exit_code` in a `session_exit` record is the one field that separates
*Copilot crashed on its own* from *something took the whole pane*. Measured
across `trace.jsonl`:

- **1042 `session_exit` records. Three carry an exit code.**

The reason is in `operator_runner.run`, and it is a fact about ordering rather
than about kills. The runner learns the code at `proc.wait()` and logs it
immediately — `copilot exited rc=...` — but it wrote the `{id}.exit` marker
*after* `time.sleep(2)` and `operator_ingest.ingest_file`. Measured across
every `*.runner.log` on this machine, 11 endings reached that capture:

- the gap between Copilot's death and the marker ran **7s to 47947s (13.3
  hours), averaging 5692s (95 minutes)**;
- **10 of the 11 read `rc=3221225477`** — `0xC0000005`, an access violation.
  That is Copilot faulting in its own process, not an external kill.

Both instruments agree on the clearest case. For `copilot-tools` session #240
the runner logged `copilot exited rc=3221225477` at 18:20:38 and `operator.log`
recorded `copilot exited unexpectedly after 4794s` at **18:46:33**. A
supervised agent was dead for 25m55s with nothing relaunching it — and that is
the *good* case, the one where the runner survived to write anything at all.

So the marker was doing two jobs and was late for both. It is the signal that
ends the supervisor's poll, so a dead session went unrelaunched for an average
of 95 minutes; and it is the only durable record of *how* the session ended, so
anything killing the runner inside that window destroyed the evidence. The
runner observed 18 endings across its whole history; 11 survived to the capture;
3 reached a trace record. Every other ending was filed as `exit_code: null`,
which reads identically to an external kill and is exactly how this item's
opening evidence read it — *"939 of the 940 carry no exit code at all; the
single exception is 3221225477"*. That single exception was never the outlier.
It was the only one that got through.

**This is the item's signature failure for the seventh time, and the first
found outside the operator.** An instrument that cannot report the thing it is
read as reporting: `None` meant "nobody observed Copilot terminate", and it was
being returned for "the runner is alive and busy parsing a 1.4 GB log".

**Fixed here.** `operator_runner.run` writes the exit marker the instant
`proc.wait()` returns, before the capture. Metrics are the right side of that
trade to lose, and they are not actually lost: a relaunch that kills the pane
mid-capture leaves the Copilot log on disk, `ingest_file` keys on path plus
mtime and skips what it has done, so `operator ingest` collects anything cut
short. An exit code is recoverable from nothing — the process is gone.

Three tests pin the ordering (`tests/test_runner.py`), each verified by
restoring the old ordering and watching it go red: the marker is on disk when
`ingest_file` is entered; a runner killed mid-capture still leaves the code
(raised as `BaseException`, because `run` catches `Exception` around the
capture and an `Exception` would prove nothing about a kill); and the capture
still happens when nothing interrupts it, without which the first two are
satisfied by a runner that never ingests at all.

One caller paid for it and is handled rather than left to rot. The attached
single-session path printed its summary straight out of the metrics database
on the strength of the exit marker, which no longer implies the capture is
done — it would have reported a session that used no credits, a wrong number
rather than a missing one. `copilot_operator.wait_for_metrics_capture` waits
for the pane instead, bounded at 15s: the capture has no ceiling, and a summary
that is thin is recoverable where a terminal that never returns is not. Loop
mode deliberately does not call it, because relaunching promptly is the whole
point there.

### What the next reader should do differently

**The re-measurement recipe now has a fourth instrument, and it is the one to
read first.** `markers.exit_code` in `trace.jsonl`, for records written by a
supervisor started after this change:

- **`3221225477`** (`0xC0000005`) means Copilot faulted in its own process.
  That is not a kill and does not belong in a wave count, however much the
  timing suggests one.
- **`null`** still means nobody observed the ending, which remains the
  signature of the whole pane going — but it now means that *only*, rather
  than also meaning "the runner is still busy".

Everything this item concluded about kills before 2026-08-10 was reached with
that field blank, so **it cannot distinguish a Copilot crash from an external
kill in any population above.** The nine `copilot exited unexpectedly` lines
from 2026-08-09, the 979 from the original window: none of them is refuted, and
none is confirmed either. Ten of the eleven codes ever recovered say the
process faulted on its own, which is a reason to suspect the population is
mixed and not a reason to conclude it. Re-count from post-change records only.

### The remedy for the remedy did it again, in the compensating wait

The change above moved the exit marker ahead of metrics capture and, because
that stops the marker implying the metrics are in the database, added
`wait_for_metrics_capture` in front of all eight `show_run_summary` calls. Its
early-return asked `MUX.has_session`, and its docstring justified that with
"the multiplexer session outlives Copilot exactly as long as the runner does".

**That sentence is true of exactly one of the eight callers.**
`run_single_session` launches with `remain_on_exit=False`. Loop mode sets
`remain_on_exit=True` — `is_copilot_running` has a comment saying so, three
functions further up the same file, and it is why that function consults
`pane_dead` at all. So on all seven loop-mode paths the session check could
never fire, and every one of them sat out the full 15-second timeout however
long ago the runner had finished. The wait was doing nothing except waiting.

**This item's signature failure, for the eighth time and the fourth inside a
remedy for a previous one.** What makes it worth recording rather than just
fixing is *why the tests agreed it worked*:
`test_every_run_summary_waits_for_the_capture_first` reads the source and
asserts each summary is preceded by the wait. It is a real guard and it
catches a real regression — but it asserts the wait is **present**, never that
it **works**, and a wait that returns only on timeout is present. The three
first-round reviewers, on three model families, all confirmed the change was
sound; none of them ran the wait under `remain_on_exit=True` either, because
nothing in the diff says which mode a caller is in.

Two things fixed here, both derived from constants rather than restated:

- `wait_for_metrics_capture` asks `pane_dead` as well as `has_session`, for the
  same reason `is_copilot_running` does. Both are needed: a session that is
  gone has no pane to interrogate, and a pane that is dead is still in a
  session. `test_waiting_for_metrics_capture_ends_when_a_kept_pane_dies` pins
  it *by elapsed time*, and reverting the check makes that test take 36
  seconds and then fail — the defect reproduced, not merely described.
- `_request_supervisor_stop`'s budget is `20.0 + METRICS_GRACE_SECONDS`.
  `operator stop` blocks on the supervisor, whose stop branch now waits for the
  capture, so a budget that does not know that expires while the supervisor is
  still doing what it was told and the caller then kills the session out from
  under it. `_do_restart_loop` already derived its budget from `POLL_INTERVAL`
  for this exact reason; the stop path had not been given the same treatment.

And the structural guard was hardened against the failure it was one edit away
from having: it matched only single-line `show_run_summary(run_started)`, so a
call reformatted across two lines would have dropped silently out of the scan
and gone unguarded while the test stayed green. Any call shape it cannot read
is now reported rather than skipped, verified by reformatting one and watching
it go red.

**For the next reader.** The instrument to distrust here is a test that asserts
a call is *made*. Presence is not behaviour, and the gap between them is
exactly wide enough to hold this item's entire history.

## Re-measurement, 2026-08-15: no kills in 5.7 days, and the recipe reads the wrong field

Run by the recipe above. Two findings: the fault is quiet again, on a window
whose evidential power is a hundredth of what the previous ones had; and the
fourth instrument the previous re-measurement introduced is described in a way
that misreads 35 of the 36 records it applies to.

### The boundary, in both time zones

The window opens at the last ending the 2026-08-10 re-measurement recorded:
**2026-08-09 22:31:20 local**, which is **2026-08-10T05:31:20Z**. That section
states its five endings in local time and `trace.jsonl` stamps everything in
UTC, and the first pass here compared the log's local stamps against
`2026-08-10 22:31:20` — a day late — and reported a clean window that was
really 152 log lines wide. It is the third arithmetic trap of the same family
as the two recorded above, so it goes on the same list: **the boundary must be
carried in local time for `operator.log` and in Zulu for `trace.jsonl`, and
this item states its boundaries in local.**

### Both instruments, and this time they reconcile ending-for-ending

`operator.log`, exclusive of the boundary, through 2026-08-15 14:12 local
(5.66 days):

- **1 `copilot exited unexpectedly`** against **11 `restart signal detected!`**
- **0 `Giving up`** and **0 `markers cannot be examined`** — both must be zero
  for the rest of the count to mean anything, since either one is the
  supervisor saying it could no longer classify an ending
- 36 `adopting the running session`

`trace.jsonl`, exclusive of `2026-08-10T05:31:20Z`:

- **12 `session_exit`**: 11 with `restart=True`, 1 with `restart=False`
- 36 `supervisor_start`

Twelve endings against twelve records; eleven restarts on each side; one
unexpected exit on each side. **This is the first window in this item's history
in which the two instruments agree ending-for-ending with nothing
unaccounted.** The 2026-08-05 window had 60 endings against zero records and
the 2026-08-10 one needed the SESSION_NUM arithmetic to reconcile at all. Here
there is no gap for a kill to sit in.

### The single ending is not a kill, and for the first time the exit code says so

`subtitle-localizer` session #1, 2026-08-10 01:28:18 local, up 23200s,
`exit_code: 0`. Zero is Copilot terminating of its own accord: not
`3221225477` (a fault in its own process), and not `null` (nobody watching).
Its runner log agrees independently — `copilot exited rc=0` at 00:27:09.

The gap between those two timestamps is 61 minutes, and it is the *old* defect
rather than a new one: that runner was launched 2026-08-09 19:01:37, before
9650435 moved the exit marker ahead of metrics capture, so it is the last
measurement of the pre-fix ordering and not a recurrence of it. Every runner
launched since takes its code from disk at launch, so unlike a supervisor a
runner cannot stay stale for long — which is why this instrument, alone among
the six, needed no deployment step.

**No kill has been observed by either instrument in 5.66 days.**

### What that is worth, which is much less than it looks

Both detectors need endings, and endings have collapsed. Twelve in 5.66 days
across nine instances, against **60 in 11.1 hours** at the 2026-08-05
measurement — about one hundredth the rate. Four instances
(`book-translator`, `finances`, `prism`, `scripts`) produced no ending at all
in the window; their Copilot processes have been up since 2026-08-09.

Checked before concluding anything, because *no endings* and *nothing can end*
produce the identical count: all eight sessions are alive and working. Each
instance's pinned Copilot debug log had been written within six minutes of the
measurement (2026-08-15 14:07–14:12 local), at sizes from 5.6 MB to 207 MB.
These are long-lived sessions, not stalled ones.

So the verdict stands and its weight does not. **A 5.66-day window holding 12
endings carries roughly what 80 minutes carried in the original regime**, and
"no kills in 5.66 days" should be read as about as strong as "no kills in 80
minutes" would have been then. Nothing here identifies the emitter. The fifth
hypothesis for the original waves is still owed.

### No event 21 in the window

All 43 `TerminalServices-LocalSessionManager` events of Id 21 or 25 between the
boundary and 2026-08-15 14:12 local are **Id 25 reconnections; there is no
Id 21**. The 2026-08-09 logon replacement has not recurred — consistent with
there being no wave for it to explain.

### Nine supervisors restarted in 50 seconds, and it is not a wave

Every `~/.operator/restart/*.loop.pid` was rewritten between 2026-08-13
23:35:10 and 23:36:00 — nine instances inside 50 seconds, which is a tighter
spread than the six-instance wave of 2026-08-09 — and `operator list` said
nothing about any of it. That is precisely the shape the recipe above tells
the next reader to treat as a broadcast, and this time it is not one:

- every `*.loopcode.json` records `"adopted": true, "began_run": false`;
- `operator.log` holds nine `adopting the running session` lines across those
  50 seconds and no `copilot exited unexpectedly` anywhere near them.

A fleet-wide `operator restart-loop` sweep, deploying current code. **This is
the first live exercise of the `adopted` stamp added on 2026-08-09**, and it
answered correctly — and correctly *silently*, which on this item is the
answer that has to be checked rather than accepted, since a silent all-clear is
how five earlier instruments failed. The positive control is the nine records
themselves: they carry `adopted: true`, so the silence is a verdict that was
computed and printed nothing, not one that was never reached. Their digest
(`a61c296821330203`) matches the source on disk, so nothing is stale either.

Three more sweeps sit in the same window (2026-08-10 02:33, 03:08 and 04:01,
nine instances each), which is what the 36 adoptions and the 36
`supervisor_start` events are — they match exactly.

### The fourth instrument is sound; its instructions are not

The recipe above introduces `markers.exit_code` and says:

> **`null`** still means nobody observed the ending, which remains the
> signature of the whole pane going

**That is false for every record carrying `restart=True`, and false by design
rather than by oversight.** `_record_session_exit` is called with
`session_gone=False` on the handoff path, and its docstring gives the reason:
the record is written while Copilot is still up, so no exit code can belong to
it and none is read. `null` there means "recorded before the process ended",
not "nobody saw it end".

Eleven of the twelve records in this window are `restart=True, exit_code=null`.
Across the 36 records since 2026-08-10, 35 are. A reader following the recipe
as written finds 35 nulls and has been told each one is the signature of a pane
being taken.

The code does not make this mistake. `ending_was_observed` is consulted only in
the `restart_probe is not True` branch, so the exit code is never asked to
account for a handoff. Only the handover text does. That is a new position for
this item's signature failure: eight times an instrument could not report what
it was read as reporting, and this time the instrument reports correctly and
**the instructions name the wrong field**. Writing the instrument was not the
end of the job.

The kill signature needs both fields together — `restart=False` **and**
`exit_code=null`. Either one on its own is ordinary.

### Re-measurement recipe, superseding the one above

1. **Fix the boundary in both time zones before anything else.**
   `operator.log` is local; `trace.jsonl` is UTC; this item quotes local.
2. **`operator.log`, exclusive of the boundary.** Count `copilot exited
   unexpectedly` against `restart signal detected!`, and count `Giving up` and
   `markers cannot be examined` as well. If either of the latter two is
   non-zero the supervisor stopped being able to classify endings and the rest
   of the count is unsafe.
3. **`trace.jsonl`, exclusive of the boundary in UTC. Reconcile before
   reading any field.** Endings must equal records and `restart=True` must
   equal the restart-signal count. If they do not, the population is censored
   and nothing derived from it means anything — which is what went wrong in
   three of the four measurements above.
4. **Read `restart` and `exit_code` together. Never `exit_code` alone.**
   - `restart=True`, `exit_code=null` — a handoff, recorded before the process
     ended. **Not evidence of anything.** It is the overwhelming majority.
   - `restart=False`, `exit_code=3221225477` — Copilot faulted in its own
     process. Not a kill.
   - `restart=False`, `exit_code=0` — Copilot ended cleanly on its own.
   - `restart=False`, `exit_code=null` — nobody observed the ending. **This is
     the kill signature among endings that were recorded.** It is *not* the
     only kill shape: a supervisor that dies together with its session writes
     no record at all, so those endings are missing from the population rather
     than present and unaccounted. **Run the completeness check in step 4a
     before reading any count from this classifier** — see the 2026-08-31
     correction, which measured eleven such endings in one window.
4a. **Count what may be missing before classifying what is present.** For every
   instance with state, compare the session number in its last `session_exit`
   against the one in its newest `supervisor_start`. The arithmetic is
   `new_start - last_exit - 1`; an instance with no prior exit cannot be
   compared at all and should be listed separately, not scored as zero.
   **A gap is "unrecorded, cause unknown", not proof of that many deaths.**
   Three things other than a co-death produce one: `SESSION_NUM` is persisted
   *before* the session launches (`copilot_operator.py:5589` against 5591), so
   a launch that failed can consume a number without a session ever running;
   an adopting supervisor also emits `supervisor_start`, at the adopted number
   rather than a new one; and a trace write can simply fail. Before calling a
   gap a death, corroborate it — `operator.log` should carry a
   `Session #N: launching copilot` and a `Session #N running (copilot pid=...)`
   line for the intermediate session, and the current supervisor should be a
   non-adoption. With that corroboration the gap counts endings the trace never
   recorded, and the ending-count is short by at least that many. Look for the
   cause in step 6, not in this classifier.
5. **Divide endings by days before believing a quiet window.** A detector that
   only fires at an ending is blind in proportion to how few there are, and
   this fleet's rate fell about sixtyfold between 2026-08-05 and 2026-08-15 —
   12 endings in 5.66 days against 60 in 11.1 hours, which is 61x, not the
   hundredfold stated in the section that introduced this step. When it is low, confirm the sessions are alive rather than
   wedged, because "no endings" and "nothing can end" are the same number.
   **Not by Copilot log mtime**, which cannot answer it — see the correction
   below. Read the newest `Forwarding event for session` line in each pinned
   log instead. Two things make that reading honest, and without them it
   decays into the mtime check it replaced: confirm the marker occurs in that
   file **at all** before believing its absence, and check it is current in a
   session you know is working. **If the marker is missing from a whole log,
   the answer is "cannot tell", not "idle"** — fall back to the instruments
   that do not read the log: last commit in the instance's repository, and the
   newest non-supervisor `operator` invocation from that repository in
   `trace.jsonl`. Both are downstream of the agent completing a turn, so they
   confirm inactivity without explaining it.
6. **Consult the event log when step 4 finds a wave *or* step 4a finds missing
   records.** The original "only once step 4 has found a kill" is withdrawn as
   an ordering rule, because a supervisor that dies with its session produces
   no step-4 row at all — so an ending-count that step 4 calls clean is not a
   reason to skip this. Note what this correction does **not** rest on: the
   2026-08-10T00:25 wave *did* produce step-4 rows, seven of them
   (`restart=False`, `exit_code=null`), so step 4 detected that wave perfectly
   well and a draft of this correction was wrong to say otherwise. The reason
   to widen the gate is the co-death shape, which produces nothing. Id 21 at
   its timestamp is the 2026-08-09 cause, a logon-session replacement with
   `LastBootUpTime` unchanged; System 1074/6005/6006/41 is a reboot, the
   2026-08-28 cause. Id 25 is routine, frequent and harmless, and treating it
   as suspicious buries the one that matters.
7. **A fleet-wide `*.loop.pid` rewrite is not a wave by itself.** Read
   `adopted` in `*.loopcode.json`: `true` is an `operator restart-loop` sweep.
   A wave leaves supervisors that restarted without adopting anything.

## Correction, 2026-08-15: the liveness check above is not one, and the fleet is inert

Written hours after the section above, by the session that followed it. The
counts it reports are reproduced exactly and nothing in them is withdrawn.
What is withdrawn is the sentence that licensed reading them as reassuring:

> Checked before concluding anything, because *no endings* and *nothing can
> end* produce the identical count: all eight sessions are alive and working.
> Each instance's pinned Copilot debug log had been written within six minutes
> of the measurement [...] These are long-lived sessions, not stalled ones.

**They were idle ones.** At the moment that was written, all eight had
completed a turn, emitted `session.idle`, and done nothing since — for between
2.8 and 5.9 days.

### Why the log mtime could not answer it

Copilot's process log is written by the runtime, not by the agent. ExP
assignment polls, telemetry queue flushes and IDE lock-file scans continue on
their own timers for as long as the process exists, so **the mtime measures
that the process is running, which was never in doubt.** It is the ninth
instrument in this item that could not report the thing it was read as
reporting, and the first introduced by a re-measurement *of this item* and
relied upon in the same session.

Stated at the strength it was measured: what is established for all eight is
the negative, and it is the only part the argument needs. **The region of every
log written after the agent went quiet contains no agent event at all**, while
the mtime advanced through the whole of it. Spot classification of that region
finds rust-runtime records, ExP assignment polls and IDE lock scans; an
independent reviewer tailing `69536` at measurement time saw the same, an ExP
POST and a `cli.telemetry` flush timestamped 2026-08-15T22:06:56Z in a log
whose agent had stopped on 2026-08-10. No exhaustive attribution of those bytes
is claimed, and none is needed: whatever that traffic is, it is not the agent,
and mtime cannot tell the difference.

### The measurement it should have been

`Forwarding event for session <uuid>` is written whenever the agent does
anything — a tool call, a background-task change, a message turn. Measured
2026-08-15T22:17Z over the whole of each pinned log:

| pid | log size | mtime | newest `Forwarding event` |
|---|---|---|---|
| 69536 | 211.3 MB | 0.3 min | 2026-08-10T05:10:56Z — **137.1h** |
| 52936 | 68.0 MB | 0.3 min | 2026-08-10T01:09:12Z — **141.1h** |
| 36676 | 82.4 MB | 8.9 min | 2026-08-10T01:53:29Z — **140.4h** |
| 13584 | 120.8 MB | 0.7 min | 2026-08-10T04:39:20Z — **137.6h** |
| 37600 | 20.3 MB | 2.1 min | 2026-08-10T06:46:01Z — **135.5h** |
| 12284 | 16.7 MB | 4.6 min | 2026-08-10T19:53:27Z — **122.4h** |
| 66784 | 251.6 MB | 8.6 min | 2026-08-13T01:41:59Z — **68.6h** |
| 54508 | 217.4 MB | 6.9 min | 2026-08-13T01:51:07Z — **68.4h** |
| 57864 | 14.2 MB | 0.0 min | 2026-08-15T22:17:38Z — **0.0h** |

The last row is the session that measured this, and it is the positive
control: the marker is current in a session that is working, so its absence
elsewhere is a reading rather than a broken probe. The second control is the
marker count per file — 18,290 to 130,455 occurrences in the eight inert logs.
**The marker is abundant in exactly the files where it has gone quiet**, which
is what separates "this session stopped" from "this build stopped logging it".
Check both before believing this measurement; a Copilot release that renames
the string turns it silently into the mtime check it replaced.

Between the newest marker and the end of file, those logs grew by 0.93 MB to
1.93 MB apiece. That is what the mtime was reporting.

### What state they are in, which is measured and not inferred

The last forwarded event before each silence is the same in all eight, once the
`session.background_tasks_changed` churn that continues afterwards is set
aside:

```
assistant.turn_end → hook.start → hook.end → session.usage_checkpoint
                   → assistant.idle (ephemeral) → session.idle (ephemeral)
```

Seven are exactly that. The eighth, `12284`, carries a `session.error` between
`assistant.turn_end` and the idle pair. **Every one of these sessions completed
a turn and went idle, and nothing issued another turn.** They are not wedged
part-way through one.

That is a state, not a cause. Why nothing continued — the harness not
resuming, the agent ending its own loop, something upstream — is unmeasured
here, and this item has been burned three times by an explanation that fitted.

### Two instruments, independent in code and not in what they depend on

Same measurement window, neither reading `~/.copilot/logs` at all:

- **git.** Last commit in each instance's repository: 68.4h, 68.5h, 123.7h,
  135.5h, 137.1h, 137.6h, 140.3h, 141.1h. Every working tree clean, so it is
  not uncommitted work either.
- **The last *agent* `operator` invocation per repository**, taken from
  `trace.jsonl` with the supervisor's own `--_supervise` launches excluded.
  These pair with the marker ages to within a fraction of an hour: `finances`
  `backlog check` 141.4h against a 141.1h marker, `book-translator`
  `backlog check` 140.7h against 140.4h, `prism` `backlog check` 137.4h
  against 137.1h, `discord-invite-manager` `backlog ready` 135.8h against
  135.5h, `snes-ghosts` `backlog list` 68.7h against 68.4h. **Two unrelated
  files agree on when each agent stopped, instance by instance.**

  The first draft of this bullet said the newest invocation from those
  repositories was the supervisor launch at 2026-08-14T06:35Z. That is the
  newest *row*, not the newest agent command, and reading it as the latter
  clips the instrument to a 40-hour window — too short to say anything about a
  2.8-to-5.9-day silence. The pairing above was in the same file all along.

**Three claims withdrawn from the first draft of this section.** It listed four
instruments and called them independent:

- *Handoff files.* "No `next-session.md` written anywhere since 2026-08-07" is
  **false twice over.** `next-session.md` is the legacy path; handoffs are
  written per instance to `~/.operator/projects/{guid}/handoff/{instance}.md`,
  and `copilot-tools.md` was written at 2026-08-15T22:07:56Z, ten minutes
  before the measurement. Worse, the reader deletes the handoff at session
  start — the 2026-08-05 correction in this file says exactly that about
  exactly this instrument — so surviving files could not date the last write
  even on the right path. It is the same instrument this item already
  withdrew once, picked up again by someone who had read the withdrawal.
- *`session_exit`.* It fires at an ending, so for these eight it dates the
  *start* of the session still running, not the stop of the work. Kept below
  as an ending record; it is not evidence of inactivity.
- *Independence.* None of these is independent *evidence*: every one is
  downstream of "the agent completed a turn", so a single cause accounts for
  all of them at once. What they establish is that the inactivity is real
  rather than an artifact of one reader — the failure this item keeps hitting.
  They say nothing about why.

For the record, since the first draft got the tally wrong: the last
`session_exit` per instance is 2026-08-10 for **seven** of the nine,
2026-08-13 for `snes-ghosts`, and 2026-08-15 for `copilot-tools` — 7 + 1 + 1,
the whole fleet. The draft's "five, two, and copilot-tools" dropped an instance
and is the fourth arithmetic trap of the family this file already lists three
of.

### What this does to the verdict above

The section above is right that "no kills in 5.66 days" is worth far less than
it looks, and right that the reason is the collapse in endings — a detector
that only fires at an ending is blind in proportion to how few there are. What
is withdrawn is its account of *why* the endings collapsed. They did not
collapse because sessions got longer. They collapsed because most of the fleet
stopped working and then held its processes open.

That makes even the discounted figure generous. **Four of the eight had already
gone idle before the window opened.** The window starts at 2026-08-10T05:31Z,
and `52936`, `36676`, `13584` and `69536` emitted their last marker at 01:09Z,
01:53Z, 04:39Z and 05:10Z that morning. Those four contributed no working time
to the 5.66 days at all.

**Every instrument this item owns fires at an ending.** `operator.log` counts
exits; `trace.jsonl` records `session_exit`; the supervisor polls for a dead
process. A session that stops working without exiting satisfies none of them,
and `operator list` prints `looping · session #N` for it indefinitely — as it
did for eight instances while this was measured.

So the quiet window is evidence about a narrower thing than it reads as.
**Eight live processes were exposed to a kill throughout it**, and an emitter
that takes a pane does not care whether the agent inside it is mid-turn — the
2026-08-09 logon replacement did not. Against *that* the window still counts:
roughly 1,000 live process-hours passed without one. What it cannot weigh is
the harm this item is named for, a session killed **mid-turn** with context
unwritten, because for 2.8 to 5.9 days of those hours there were no turns in
progress to interrupt. Nor can any of these instruments have noticed a session
that stopped without ending, which is the state all eight were in.

**The emitter is still unidentified and nothing here identifies it.** Nor does
this name a cause for the inertia: the observable is that the agent stopped and
the process did not, and this item has already been burned three times by an
explanation that fitted. The mechanism is filed as its own item rather than
guessed at here.

### How long a silence this fleet has come back from

A trailing silence only means *stopped* if it is longer than silences these
sessions have recovered from, so that was measured rather than assumed. Every
gap of an hour or more between consecutive markers, across all nine logs:

- **Seven of the eight inert sessions have no internal gap of even one hour.**
  Their whole log is continuous work followed by one silence — the trailing
  one. The silence is not the largest of many; it is the only one.
- The exception is `66784`, which **recovered from a 63.05h silence**
  (2026-08-10T09:13Z to 2026-08-13T00:16Z), worked for about 90 minutes, and
  has been silent again for 68.6h.

So this fleet has come back from 2.6 days of silence once, and nothing recorded
either the stop or the restart. **The claim here is that eight sessions have
been silent for 2.8 to 5.9 days, not that they are dead.** The two shortest —
68.4h and 68.6h — are only about a tenth longer than the silence `66784` came
back from, and should be read as no more than "longer than anything this fleet
has recovered from". The other six, at 122.4h to 141.1h, are twice that.

For scale in the other direction: inside their own working periods these
sessions emit the marker with a median gap of 0.0s and a maximum, across every
log measured, of 38.6 minutes.

**The marker under-reports liveness, and that direction matters.** It is
written when the agent does something the session layer sees; an agent blocked
for hours inside one long local command emits nothing meanwhile, so a silence
is an upper bound on idleness rather than proof of it. The 38.6-minute figure
is what bounds that empirically here — it is the longest any of these sessions
has gone quiet while demonstrably still working — and it is two orders of
magnitude below the silences being reported. A future reader on a fleet with
much longer tool calls should re-derive it rather than reuse the number.

### For the next reader

Do step 5 before steps 2 to 4, not after. A quiet count is only evidence that
the fault is quiet if something was running that the fault could have taken,
and on 2026-08-15 that was true of one instance out of nine.

## Re-measurement, 2026-08-16: the fleet is working again, and the endings sort into two shapes

Measured 2026-08-16T01:25Z. The section above closed by saying a quiet
ending-count is only evidence about kills if something was running that a kill
could have taken, and that on 2026-08-15 that was true of one instance in nine.
**Exposure has partly returned.** Eleven supervised sessions are live and seven
of them emitted an agent-activity event within the last six minutes (the table
is in item 0030), where a day earlier one of nine did.

Keep the distinction that section drew, because the first draft of this one
collapsed it. An idle session still holds a live process and a live pane, so
the quiet window was always real evidence against anything that takes a *pane*
— roughly 1,000 process-hours of it. What idleness removes is evidence about
the harm this item is named for, a session interrupted **mid-turn** with
context unwritten, because a session with no turn in progress cannot suffer it.
From 2026-08-16T00:09Z there are turns in progress again, so the second kind of
evidence has started accruing; it had not before, and nothing below should be
read backwards into the 5.66-day window.

### Every ending the trace can actually vouch for

The 2026-08-05 correction in this file establishes that records written by a
pre-fix supervisor cannot answer this question. **A calendar cutoff does not
separate them**, and the first draft of this section used one. A supervisor
imports its code once and keeps it for its whole run, so records written after
the fix landed can still come from a supervisor that predates it — which is the
same fact item 0011 is about, applied to this file's own arithmetic.

The discriminator is in the record: `session_exit` carries a `code` fingerprint
naming the supervisor code that wrote it, and a record without one was written
by a supervisor old enough not to stamp it. Of the 132 records dated 2026-08-05
or later, **39 carry no fingerprint, and all 39 fall in one 17-minute span** —
2026-08-05T00:45:21Z to 01:02:59Z, the earliest window in the population. The
oldest stamped record is 2026-08-05T16:12:22Z.

Those 39 are exactly the artifact the correction above withdrew. An unfixed
supervisor called `_record_session_exit` only from the branch that runs when no
restart was requested, so **every record it ever wrote reads as unaccounted**,
whatever actually happened. Counting them as endings-without-a-handoff is
counting the instrument.

So the population is the 93 stamped records:

| how it ended | count |
|---|---|
| by restart request (a handoff was written) | 76 |
| unaccounted: no exit code, restart marker observed absent | 12 |
| carrying an exit code | 5 |

with 39 further records that are dated inside the window and cannot be
classified at all.

The five that carried an exit code, in full:

```
2026-08-08T23:22:03Z discord-invite-manager #230  rc=3221225477  uptime=77811s
2026-08-10T01:46:33Z copilot-tools          #240  rc=3221225477  uptime=4794s
2026-08-10T08:28:18Z subtitle-localizer     #1    rc=0           uptime=23200s
2026-08-15T22:52:22Z tiktok-downloader      #1    rc=0           uptime=112s
2026-08-16T00:09:00Z copilot-tools          #247  rc=3221225477  uptime=498s
```

`3221225477` is `0xC0000005`, an access violation. The original evidence at the
top of this item found exactly one of those in 940 pre-fix records; there have
been three in the 132 post-fix ones.

### One burst survives the provenance filter, and it is 2026-08-10

Grouping the 12 unaccounted endings by arrival, treating any gap of 30 seconds
or less as one burst:

- **2026-08-10T00:25:44–49** — seven instances inside five seconds. The "one
  broadcast, not seven independent decisions" shape this item was opened on.
- **Five singletons** — `scripts` #87 at 2026-08-08T04:54:48, then
  `snes-ghosts` #236 05:19:19, `ac-unreal` #29 05:20:06 and #30 05:29:00, and
  `copilot-tools` #242 05:31:20, all on 2026-08-10.

**The newest unaccounted ending of any kind is 2026-08-10T05:31:20Z**, 5.8 days
before this measurement. That timestamp is the same under either population —
it is the one number the provenance filter does not move, because everything it
removes is older.

Run over all 132 date-filtered records the same grouping gives 46 in six bursts
and 5 singletons, and five of those six bursts are the unclassifiable 39. That
tally is recorded here only so the next reader recognises it: it is the shape
this section reported before the fingerprint was checked, and it overstates the
evidence roughly four-fold.

### One fresh access violation, which is a crash and not shown to be a kill

`copilot-tools` #247 ended at 2026-08-16T00:09:00Z with `rc=3221225477`,
`uptime_s=498`, `restart=False`, `stop=False`, `detach=False`.

**It died mid-turn, and that part is directly observed rather than inferred.**
Its process log, `process-1786838442435-2596.log`, ends at 00:08:44.916Z in the
middle of a streamed model response:

```
00:08:44.916Z [DEBUG] Forwarding event ...: assistant.streaming_delta (ephemeral)
00:08:44.916Z [DEBUG] Forwarding event ...: assistant.reasoning_delta (ephemeral)
<end of file>
```

There is no shutdown sequence after it — no extension-host exits, no runtime
teardown, the file simply stops inside a token stream. So no handoff was
written and the turn's context went with the process, which is the harm this
item is named for. Its successor ran `session start --instance copilot-tools`
at 00:09:15.

**It is not evidence that the emitter is back**, and the first draft of this
section called it a kill, which overstates it twice over:

- `0xC0000005` is an access violation — a fault taken *by* the process. The
  wave signature described at the top of this item is the opposite: seven
  extension hosts exiting with `0xC000013A` (`STATUS_CONTROL_C_EXIT`) and an
  orderly shutdown following. This log has neither.
- It stands alone. No other instance ended unaccounted anywhere near it, and
  the eight endings between 00:09:17 and 00:14:02 all carry `restart=True` —
  they are the handoffs of eight agents that had just been woken by a message
  (item 0030). A burst of `restart=True` endings and a burst of unaccounted
  ones look identical in a list of timestamps; only the marker separates them,
  and reading the one as the other would manufacture a wave that did not
  happen.

On the stamped record the two shapes are therefore:

- **bursts of unaccounted endings** — one that survives provenance checking,
  seven instances on 2026-08-10T00:25, plus five singletons over the following
  five hours, and nothing since;
- **isolated access violations** — three since 2026-08-05, one of them 76
  minutes before this measurement.

Whether either shape has an emitter, and whether it is the same one, is not
established here.

### For the next reader

The step-5-first instruction above still holds, and it now has a cheap answer:
item 0030 carries a liveness table for 2026-08-16 and the recipe that produced
it. Re-run that first, and read the ending-count against it rather than on its
own — an ending-count measured over an idle fleet still bounds pane-level
kills, but says nothing about mid-turn loss.

And check the `code` fingerprint before counting anything. A calendar cutoff
looks like a provenance filter and is not one, because a supervisor keeps its
code for its whole run. Using one here inflated the unaccounted count from 12
to 51 and the burst count from one to six, and every added record was the
instrument this file had already withdrawn.

## Re-measurement, 2026-08-16T02:00Z: the ending-count finally has a denominator

The section above says an ending-count over an idle fleet "says nothing about
mid-turn loss", and every section before it says some version of the same
thing without being able to put a number on it. **This one puts a number on
it.** Exposure is measured here rather than asserted, from the same logs item
0030 classifies, and the ending-count is then read against it.

### Eventful log-minutes, and why that is the denominator

For every supervised session log, count the distinct wall-clock minutes
containing at least one agent event — a `Forwarding event for session` marker
whose type is not `session.background_tasks_changed`. Summed over the fleet
that is **eventful log-minutes**, reported below in hours: an index of how much
session-time was spent doing something a mid-turn kill could have interrupted.

**It is an index, not a duration, and the direction of its error is unknown.**
A single marker credits a whole minute even if the activity lasted
milliseconds, which overcounts; the marker is silent while an agent waits
inside one long tool call, which undercounts; and a session that ends and
restarts within one minute has both logs credited for it, which double-counts.
A first draft called the figure a lower bound on exposure and reasoned that its
error was conservative in the direction of the conclusion. That was not
measured and is withdrawn — neither the size nor the sign of the net bias is
known here.

Background events are excluded because item 0030 measured them landing 13–50 ms
after `session.idle`, as the trailing event of the same turn; counting them
would credit the fleet with activity it did not have. Which logs belong to
supervised sessions is decided from `trace.jsonl` — the pids it names as
sessions, plus live processes whose log creation time matches their process
start — rather than from the log directory, for the reason item 0030 now
records: two live `copilot` processes on this machine are nobody's session.
Those two are in the scanned population and contribute zero minutes, having no
markers at all, so they move no number here.

| window | span | eventful log-hours | mean sessions eventful at once | endings | unaccounted |
|---|---|---|---|---|---|
| quiet, 2026-08-10T05:32Z → 08-15T22:17Z | 136.75h | 23.25 | 0.17 | 13 | **0** |
| inert, 2026-08-15T20:29Z → 22:17Z | 1.80h | 1.82 | 1.01 | 2 | **0** |
| exposure, 2026-08-16T00:09Z → 02:00Z | 1.85h | 12.48 | 6.75 | 18 | **0** |

**The quiet row is fragile in the one direction that flatters this
measurement.** Log eviction removes old files only, so re-running the scan
shrinks the quiet denominator while leaving the exposure one untouched: it read
23.63 at 01:59Z and 23.25 four minutes later, and about 1.7 further missing
hours would take the exposure window from "more than half" of it to less. The
comparison below should be read with that sensitivity in mind, and item 0033
is why it exists.

The quiet window starts one minute after the newest unaccounted ending this
file knows of (2026-08-10T05:31:20Z), so it is exactly the interval over which
this item has been unable to say whether the kills stopped or the fleet did.
"Mean sessions eventful at once" is eventful log-hours over span, deliberately
*not* a duty cycle against an instance count: the fleet was nine instances for
part of that history and eleven for the rest, and nothing here should rest on
which.

**Its 13 endings are not the "12 endings in 5.66 days" quoted earlier in this
file, and the difference is the boundary rather than a correction.** The
population here is every `session_exit` strictly after 2026-08-10T05:31:20Z and
before 2026-08-15T22:17Z, and it is listed in full so the next reader can check
it: `discord-invite-manager` #238 and #239, `snes-ghosts` #237 and #238,
`ac-unreal` #31 through #36, `copilot-tools` #243 and #244 — twelve
`restart=True` — plus `subtitle-localizer` #1 at 08:28:18Z with `exit_code=0`,
a clean but unexplained exit. Neither count contains an unaccounted ending, so
nothing downstream of the difference changes.

**On the session-hours unit, one hour and fifty-one minutes of the restored
fleet carries more than half the exposure of the preceding five and a half
days** — 12.48 eventful log-hours against 23.25. Read that as a statement
about an independent per-session process only; against a fleet-wide emitter
concurrency counts once, and the section below sets out both denominators
before drawing anything from either.

### Two things about this window that weaken it, stated before the result

**The window was manufactured, and by an agent reading this backlog.** The
fleet is working because eight inert sessions were sent a message — one at
2026-08-16T00:06:25Z and the other seven between 00:08:11Z and 00:08:16Z (item
0030) — each replied within 10 to 18 seconds, handed off, and was relaunched by
its supervisor. So this is not a sample of normal operation; it is a sample of
the fleet in the two hours after a deliberate intervention. Whether kill
exposure during a post-intervention burst resembles exposure during ordinary
running is not known, and nothing here assumes it.

**The measuring session is in the population it measures.** `copilot-tools` is
one of the eleven instances, it accounts for 3 of the 18 endings, and one of
those three is the access violation. It is not excluded, because excluding the
one instance whose behaviour is best understood would bias the count in an
unknown direction — but a reader weighing the 18 should know that a sixth of
them belong to the instrument.

### The ending-count, read against it

All 18 endings in the exposure window carry a `code` fingerprint, all of them
the same one (`a61c29682133`), so none is the unclassifiable pre-fix artifact.
`giving_up` is false on every record, which means only that no supervisor
reached its consecutive-failure limit (`operator_trace.py:662` defines it as
`consecutive >= limit` and nothing else) — it is not, as a first draft had it,
a statement that classification stayed healthy. What makes these records
classifiable is that each carries the marker values step 4 needs. Reconciled by
class, reading `restart` and `exit_code` together per that step:

- **17 `restart=True`** — a handoff was written. Not evidence of anything.
- **1 access violation** — `copilot-tools` #247 at 00:09:00Z, `rc=3221225477`,
  already documented in the section above.
- **0 unaccounted.** The kill signature — `restart=False`, `exit_code=null` —
  did not occur once.

Normalising by exposure rather than by days changes the aggregate ending-rate
comparison by a factor of about forty:

| | quiet | exposure | ratio |
|---|---|---|---|
| endings per wall-clock day | 2.3 | 233.5 | **102x** |
| endings per eventful log-hour | 0.56 | 1.44 | **2.6x** |

An ending-count divided by days — step 5 of the recipe as written — reports a
fleet behaving 102 times differently; divided by exposure, 2.6 times. **Neither
number measures kills**, and a first draft's claim that "97% of the swing is
exposure" is withdrawn as well: that is an additive share read off a pair of
multiplicative ratios, and it varies from 79% to 98% depending on how it is
taken.

**The residual 2.6x is not explained here, and the field that looked like the
explanation does not mean what it says.** A first draft attributed it to
session length, from `markers.uptime_s` on the ending records. That field is
**the current supervisor's observation time, not the session's age**, and the
difference is not small: the eight sessions it reports as 41.6 hours old have
pinned logs created 2026-08-10T00:26Z, six days earlier. `uptime_s` restarts
when a supervisor adopts a running session, so on this fleet — where every
supervisor has been restarted at least once — it systematically understates
age by however long ago the last adoption was.

The whole uptime comparison is therefore withdrawn, not corrected: a session-age
distribution was never computed, and nothing here explains the residual. It is
left open rather than furnished with a story that fits.

### What this is worth, stated at the strength it was measured

**There are two denominators here and they must never be added.** Which one
applies depends on what is being looked for, and the first draft of this
section led with the wrong one.

- **Against a fleet-wide emitter** — the only burst this file can vouch for
  took seven instances inside five seconds — concurrency buys nothing. Seven
  sessions dying together is *one* draw, not seven, so the unit is wall-clock
  time during which there were live panes to take. On that unit the quiet
  window is 136.75 hours and this one is **1.85**. The new window is on the
  order of a percent of the standing evidence against a wave, not half of it.
- **Against an independent per-session process** — something that could take
  one session mid-turn without touching its neighbours — eventful log-hours
  is the right unit, and it is 12.48 against 23.25.

The first draft said "111 minutes of the restored fleet is worth more than half
of the preceding five and a half days". That is true only of the second
estimand, and the wave is the first. **Item 0001 is named for the wave.**

**The 2.6x is a handoff rate, and cannot be read as a kill rate.** Seventeen of
the eighteen endings in the exposure window are `restart=True`, which step 4 of
the recipe calls "not evidence of anything", and **eight of those seventeen
were caused by the intervention that created the window** — the woken sessions
handing off together. The table above is therefore worth keeping for exactly
one purpose, which is to show that dividing endings by days rather than by
exposure changes the answer forty-fold. It says nothing about kills. The kill
rate is 0 in both windows and the ratio of two zeroes does not exist.

**Who owns these hours, which is not the fleet in equal shares.** Attributing
each eventful minute to its instance, through `session_pid` on the ending
record:

| window | total | largest attributed contributor | the measuring instance | unattributed |
|---|---|---|---|---|
| quiet | 23.25h | `ac-unreal` 12.18h (52.4%) | `copilot-tools` 5.48h (**23.6%**) | 0 |
| inert | 1.82h | `copilot-tools` 1.82h (100%) | **all of it** | 0 |
| exposure | 12.48h | `operator` 1.87h (15.0%) | `copilot-tools` 1.40h (11.2%) | **3.22h (25.8%)** |

The unattributed column is sessions that had not yet ended when this was
measured, so no `session_exit` record names their instance. It matters that it
is a quarter of the exposure window: **no claim can be made about the maximum
share any instance holds there**, because a single unattributed owner could
exceed every attributed one. A first draft said no instance exceeded 15%, which
the data does not support.

Three things do follow, and two of them are unflattering:

- **The inert row is withdrawn as a statement about the fleet.** Every one of
  its 109 eventful minutes, and both of its endings, are `copilot-tools` — the
  session taking the measurement. It measures the instrument, and it is left in
  the table only because deleting a row after reading it is worse.
- **Nearly a quarter of the quiet window's evidence is the measurer**, and
  another half is a single instance cycling handoffs on 2026-08-10, one of them
  ten seconds long. Five instances contribute anything at all. A
  "fleet-wide" figure it is not.
- **The exposure window is much better spread even so.** Ten instances
  contribute among the 74.2% that can be attributed, the largest of them 15.0%,
  and the measurer is 11.2% — against one instance holding 52.4% of the quiet
  window. That breadth is the one respect in which this window beats the quiet
  one on its merits rather than by choice of unit.

**It is not evidence that the kills have stopped.** The newest unaccounted
ending is 2026-08-10T05:31:20Z, 5.9 days before this measurement. Absence
across 12 eventful log-hours does not exclude a phenomenon whose observed
inter-arrival is days, and nobody should read the zero as more than the
ordinary accrual of evidence it is.

**What the zero actually bounds, and why even that is generous.** Zero events
in 12.48 eventful log-hours puts a 95% upper bound of about 0.24 unaccounted
endings per eventful log-hour on the rate — the rule of three, 3/12.48 —
against 0.129 from the quiet window's 23.25 hours, so the two together bound it
near 0.084. That still permits an unaccounted ending roughly every twelve
working session-hours, which at this window's concurrency is several a day: it
is a weak bound, not a clean bill. And it assumes independent arrivals, **while
the one burst this file can vouch for was seven instances in five seconds**,
which is the exact opposite. The fleet could sit clean for a week and then lose
every session in one second, and nothing measured here would have moved
beforehand. Read the bound as a loose upper limit on a *background* rate, and
as no bound at all on the wave.

**What did get faster, stated precisely.** The background-rate denominator
accrues about forty times faster per wall-clock hour with the fleet working
than with it parked — 6.75 eventful sessions at once against 0.17. That is
worth having and it is why this measurement was possible at all in an evening.
It is not a substitute for calendar time against a wave, and an earlier draft
of this section said forward testing "no longer takes a week". Withdrawn: for
the phenomenon this item was opened on, it still does.

**The denominator is a lower bound, and knowing which way it errs matters.**
The marker is silent while an agent waits inside one long tool call, so a
minute spent blocked in a build counts as inactive. Exposure is therefore
understated in every window — which understates the evidence rather than
inflating it, and understates it *more* in the working window than the idle
one, because idle sessions have no long tool calls to miss. Both errors point
away from the conclusion drawn, so the conclusion survives them.

**A tenth instrument defect, in the reader rather than the writer.** The first
version of this measurement selected supervised logs by reading `session_pid`
from `session_exit` records, and it dropped the crashed session — because
`session_pid` is `null` on exactly the record where the supervisor lost the
process, which is the access violation above, and the pid appears only in the
other spelling, `source.session_pid` on `invoke` records. The one ending in
this window that this item is actually about was the one the filter could not
see. Read both spellings.

### The evidence is being deleted while this is written

`~/.copilot/logs` holds **exactly 50 `process-*.log` files**, and it was
holding exactly 50 at every observation. Between two runs of the same scan,
finishing 01:59:15Z and 02:03:12Z, one file appeared and one previously-present
file vanished: `process-1786338840160-82184.log`, confirmed absent from disk,
29 eventful minutes of a supervised session gone.

**The eviction rule is not established, and the obvious guess is wrong.** The
evicted file was created 2026-08-10T05:14:00Z, and five files created before it
are still retained (four of them supervised sessions), so eviction is not
oldest-by-creation. Least-recently-written could not be tested at all: **the
file's mtime was never recorded before it was deleted**, which is the same
lesson this section is about, arriving one level up. This section deliberately
stops there rather than proposing a rule that also fits. What is measured is
the cap and one eviction, and those are enough:

- A measurement of a fixed past window **can shrink between runs** when a log
  inside it is evicted. The quiet window read 23.63 eventful log-hours at
  01:59Z and 23.25 at 02:03Z, and the difference is a deleted file, not a
  correction. One eviction is not a rate, and nothing here claims every run
  returns a smaller number.
- The exposure window's fleet produced 18 successor sessions in 111 minutes,
  about ten new logs an hour, against a 50-file ceiling. That is **eviction
  pressure equal to the whole cache in about five hours** — not a demonstration
  that every file now present will be gone by then, which needs the
  oldest-first rule the paragraph above has just refused to assume.
- The logs of the 2026-08-10T00:25 burst are already gone. The oldest file now
  retained was created at 00:26:39Z, 55 seconds after the last of the seven
  endings — it is a successor session, not a killed one. No future reader will
  be able to look at what those seven processes were doing when they died.
- The days-long silences item 0030 measured were only observable because an
  idle fleet writes no new logs. **The state is easiest to detect exactly when
  the evidence survives longest, and hardest when it does not.**

Anything that forward-tests this item has to snapshot what it needs at
measurement time. Filed as item 0033.

### For the next reader

Re-run item 0030's liveness classification first — that instruction has not
changed — but then compute eventful log-hours before dividing anything by
days. An ending-count over an idle fleet is not a small measurement, it is a
measurement of the fleet; and this file has now published a reassuring quiet
window twice and withdrawn it twice for exactly that reason.

And do it promptly. The logs this rests on have a retention measured in hours
once the fleet is working, so a window you did not measure today is a window
you cannot measure.

## Re-measurement, 2026-08-31: a reboot took the fleet, and step 4 could not see it

Measured 2026-08-31T23:21Z-23:30Z, fifteen days after the section above, which
is the forward test that section asked for. It returns a result about kills,
and it also **withdraws the word "only" from step 4 of the recipe** — the
signature that step names is right, but it is not the only shape a kill takes,
and the other shape leaves nothing for step 4 to classify.

### What the ending-count says on its own

Window 2026-08-16T02:00Z (where the previous measurement closed) to the
measurement, 15.89 days. Every record classified per step 4, provenance checked
per the 2026-08-16 correction:

| | |
|---|---|
| `session_exit` records in window | 37 |
| carrying a `code` fingerprint | 37 (one value, `a61c29682133`) |
| unclassifiable (unstamped) | 0 |
| `giving_up` true | 0 |
| `restart=True` — a handoff | 35 |
| `exit_code=0` — clean, unexplained | 1 |
| **unaccounted — the kill signature** | **1** |

The one unaccounted ending is `discord-invite-manager` #241 at
2026-08-20T08:28:32Z. It is a singleton: no other instance ended unaccounted
within hours of it, so it has none of the seven-in-five-seconds shape of the
2026-08-10 burst. That is the first unaccounted ending since
2026-08-10T05:31:20Z, and it moves that date forward for the first time in
this file.

**Read on its own, this window is reassuring, and reading it on its own would
be the eleventh instrument error in this file.**

### Nine sessions ended and the trace recorded none of them

`operator list` reports nine instances. For every one, the newest
`supervisor_start` is at a session number exactly **two** above that instance's
last recorded ending:

| instance | last recorded ending | current session |
|---|---|---|
| book-translator | #319 | #321 |
| copilot-tools | #249 | #251 |
| discord-invite-manager | #243 | #245 |
| operator | #4 | #6 |
| prism | #225 | #227 |
| scripts | #110 | #112 |
| snes-ghosts | #241 | #243 |
| subtitle-localizer | #2 | #4 |
| repos | (none ever) | #2 |

A gap of two means one session in between both started and ended with no
`session_exit` written. `copilot_operator.py:5335` is what makes the reading
unambiguous — `start_session_num = SESSION_NUM + (0 if adopt else 1)`, with the
comment "Adoption joins the session that is already running, so it keeps that
session's number. Only a launch moves to the next one." The supervisor writing
this session's record logged `Continuing from session #251` at 16:19:00 and
launched it one second later at 16:19:01, so it took the `+1` branch from a
stored `SESSION_NUM` of 250. (A draft said "48 seconds later", which was the
delay until this agent's own first turn, not the launch.)
**Session #250 is a session that ran and ended, and no record of it exists.**

**Disclosure, because it is not incidental: this section is written by session
#251, and the unrecorded #250 is its own immediate predecessor.** The author is
the successor of one of the nine sessions it counts as lost, and can testify to
one thing no instrument here shows — the handoff file that #251 read at startup
still contained #249's text, so #250 wrote nothing before it went. That is a
sample of one, and it is the sample this section is standing in.

`operator.log` says the same thing in the supervisor's own voice, and says how
long it lasted:

```
[operator 2026-08-15 18:51:15] Session #250: launching copilot
[operator 2026-08-15 18:51:16]   Session #250 running (copilot pid=58856)
[operator 2026-08-31 16:19:00] Continuing from session #251 (run started 2026-07-30T10:36:35Z)
[operator 2026-08-31 16:19:01] Session #251: launching copilot
```

**Nothing between those lines.** The supervisor that launched #250 wrote
nothing further, and the next line in the file is a *different* supervisor
starting fifteen days later. Be careful what that spans: the first supervisor
existed only until the 2026-08-28 reboot, so the silence is **11.4 days of a
live supervisor that recorded nothing, followed by 3.85 days in which no
supervisor existed at all.** An earlier draft called the whole gap "a
supervisor that reported `looping` throughout", which is wrong for the last
four days of it — after the reboot there was nothing left to report anything.

### What ended them: a reboot, and which sessions that statement covers

The machine rebooted. From the Windows event log and `LastBootUpTime`:

```
2026-08-28T03:02:28Z  evt 1074       shutdown initiated by a process
2026-08-28T03:03:34Z  evt 6006       event log service stopped
2026-08-28T03:04:33Z  LastBootUpTime system up
2026-08-28T03:04:39Z  evt 6005       event log service started
```

(`LastBootUpTime` and event 6005 are different timestamps six seconds apart; a
draft of this section labelled the first as the second.)

**Two different populations are in play here and an earlier draft of this
section merged them.** The gap census above is computed from `operator list`,
so it sees only instances a human restarted on 08-31; the surviving Copilot
logs see whatever escaped the ring. They overlap in two instances, not nine.
Set out separately, with the newest non-background event in each pre-reboot
log:

| instance | pre-reboot session | last event of any kind | last event that was *agent work* |
|---|---|---|---|
| ac-unreal | 54460 | `tool.execution_start` 03:03:09.856Z | **`tool.execution_start` 03:03:09Z** |
| finances | 97616 | `session.tools_updated` 03:03:10.377Z | `hook.end` 03:03:10Z |
| operator | 70072 | `session.tools_updated` 03:03:09.759Z | `assistant.idle` **08-17T23:26Z** |
| discord-invite-manager | 34096 | `session.tools_updated` 03:03:09.064Z | `assistant.idle` **08-20T09:30Z** |
| repos | 24048 | `session.idle` 08-21T06:27Z | idle 6.9 d |
| book-translator | 20076 | `session.idle` 08-17T18:58Z | idle 10.3 d |
| scripts | 14184 | `session.idle` 08-17T05:10Z | idle 10.9 d |
| copilot-tools | 58856 | `session.idle` 08-16T18:12Z | idle 11.4 d |
| snes-ghosts | 50656 | `session.idle` 08-16T03:40Z | idle 12.0 d |
| prism | 24072 | `session.idle` 08-16T03:11Z | idle 12.0 d |
| subtitle-localizer | 86508 | `session.idle` 08-16T00:36Z | idle 12.1 d |

**The fourth column is the one that matters, and an earlier draft of this
section did not have it.** Four logs emit something at 03:03:09-10Z, and it is
tempting to call all four "working" — but `session.tools_updated` and
`session.shutdown` are *lifecycle* events the runtime emits as it goes down,
not agent activity. Classified by whether the last event was work:

- **One session was mid-turn: `ac-unreal`.** Its last event is
  `tool.execution_start`, which is a turn in progress.
- **`finances` was doing something** — a `hook.end` at 03:03:10Z — though a
  hook firing during shutdown is a weak reading of "working" and is not
  claimed as more.
- **`operator` and `discord-invite-manager` were idle**, and had been since
  2026-08-17 and 2026-08-20 respectively. Their shutdown-time events are the
  runtime's, not the agent's.

So: **eleven sessions were alive when the machine went down; one was
demonstrably mid-turn, and the rest were idle or near it.** Four processes are
*directly observed* alive at the shutdown, because only they emitted anything
then. The other seven are *inferred* alive — an idle session emits nothing and
item 0030 is precisely the finding that its process stays up regardless, so a
log whose last event is 08-16 is silent about 08-28 either way. The inference
rests on the session-number gap and on nothing else having intervened.

Note which instances the two methods miss. **`ac-unreal` and `finances` are
absent from the gap census entirely**, because nobody restarted them on 08-31
— and `ac-unreal` is the one session that was actually mid-turn. A census taken
from `operator list` is conditioned on what a human chose to bring back, which
makes nine a **lower bound on unrecorded endings, biased by that choice**.

`54460`'s last act was to **start a tool call**. That is the harm this item is
named for, observed directly rather than inferred: a session interrupted
mid-turn. The end of that file is a model finishing a message and dispatching
a tool —

```
03:03:09.854Z  assistant.message
03:03:09.856Z  tool.execution_start      <- last forwarded event in the file
03:03:09.860Z  Broadcasting session lifecycle event: session.updated   (x2)
                                          <- then the file stops
```

— with **no `tool.execution_complete` anywhere after it**. (An earlier draft
called the tool start the file's last *line*; two lifecycle broadcasts follow
it. It is the last forwarded event, and the unmatched start is the claim.) The
check matters because an unmatched start is the whole point: in the last 4 MB
alone there are 22 starts and 24 completes, so completions do normally follow,
and this one did not.

Seek-sampling that file at forty offsets returns monotonically increasing
timestamps from 2026-08-16T19:39:34Z to 2026-08-27T18:54:20Z, and every sample
carrying a session id shows the same one
(`cfabc021-bedf-49b6-a787-56d87c8d5d0e`). Forty samples cannot *prove* the file
holds no other session, so read this as: one session, 12.33 GB, spanning
**11.3 days**, with roughly 16.6 million forwarded events by a full count of
the marker. That is the age of the session and the volume of its debug log; how
much model context it still held is a different quantity and is not measured
here.

Each of the four is the successor of its instance's last recorded ending, and
the arithmetic is tight enough to leave no room for another reading:

| instance | last recorded ending | successor log created | gap |
|---|---|---|---|
| finances | #219 at 2026-08-16T04:56:57Z | 04:57:28Z | 31 s |
| ac-unreal | #48 at 2026-08-16T19:39:03Z | 19:39:34Z | 31 s |
| operator | #4 at 2026-08-17T18:44:46Z | 18:44:54Z | 8 s |
| discord-invite-manager | #243 at 2026-08-20T09:27:47Z | 09:28:11Z | 24 s |

So each supervisor recorded one ending normally, launched the next session
within half a minute, and then never recorded anything again.

**`session_exit` records written between 2026-08-27T00:00Z and the relaunch:
zero.** The largest loss of agent context this file has ever documented
contributed **nothing** to the ending-count above.

**A reboot is not the only thing that does this, and this file already contains
another.** What produces an unrecorded ending is the supervisor and its session
dying *together*, whatever kills them. The 2026-08-09 wave in this item was a
logon-session replacement with `LastBootUpTime` unchanged — the same co-death
with a different cause. A logoff, a console teardown or a fast-startup hybrid
shutdown would do it too. Reboot is the instance measured here, not the class.

### The correction, which is to step 4's *exclusivity*

Step 4 says, of `restart=False` with `exit_code=null`:

> **This is the kill signature, and it is the only one.**

**The signature is right; "the only one" is false.** Step 4 is a classifier of
rows that exist, and on those rows it reads correctly — that is what a
surviving supervisor writes about a session it watched die. What it cannot do
is see a session whose supervisor died in the same instant, because that
ending produces **no row at all**. This is a census gap, not a misread field,
and the 2026-08-05 correction in this file is the precedent: handoffs were
invisible then because they produced no record, not because a field lied.

The two failure modes are opposite in the worst possible way. An unaccounted
ending *raises* the count step 4 reads. A co-death *lowers* it — not because
the sessions failed to end, they very much ended, but because **the ending is
never written down.**

Be precise about what that does to a window, because "reads quieter" bundles
two different things and only one of them is a defect:

- **The reboot deaths are the defect.** Eleven endings occurred and contributed
  zero to the numerator. That is the instrument failing.
- **The 3.85 days of downtime afterwards are not.** No sessions existed, so no
  endings occurred, and a count of zero is the correct answer. Item 0030's
  lesson applies unchanged.
- **The calendar denominator is a third thing.** Dividing by 15.89 days spreads
  a real 12-day window over a span that includes four days of an empty machine,
  which is why the exposure table below is cut at the boot instant.

Only the first is the instrument reading backwards, and it is enough on its
own: a window containing a co-death reads *quieter than the truth*, and every
quiet window in this file has been read as evidence that kills had stopped.

This is the eleventh instrument defect recorded in this item — the ninth is
the log mtime, the tenth was in a reader rather than a writer — and it is the
first that is a gap in the *taxonomy* rather than a misreading of a signal.
Step 4 reads `session_exit` correctly. There is simply an ending it never
gets to read.

Note what this does **not** say. The 2026-08-09 wave was explicitly checked
against a reboot and cleared — the section above records "No reboot:
`LastBootUpTime` is 2026-08-04 10:02:54 and unchanged". That check was right
and stands, and that wave turned out to be a logon-session replacement, which
is a co-death of the same class arriving by a different route. What is new is
that co-death leaves *no* record, so it has to be excluded by looking for the
event that caused it, and can never be excluded by finding the ending-count
quiet.

### The exposure, which is one session wearing the fleet's clothes

Eventful log-minutes per surviving log, 2026-08-16T02:00Z to the reboot, by
the definition the section above settled on:

| | |
|---|---|
| window span | 289.07 h |
| eventful log-hours (summed over logs) | 240.82 |
| wall-clock hours with any agent active (union) | 229.75 |
| mean sessions eventful at once | **0.83** |

240.82 eventful log-hours is an order of magnitude more exposure than any
window previously measured here — and **93.0% of it is the single ac-unreal
session**, 223.85 hours of it. One other instance, `repos`, contributed 10.40
hours; the remaining eight contributed under two hours *each* across twelve
days:

```
pid 54460  223.85 h  93.0%      pid 58856    0.93 h   0.4%
pid 24048   10.40 h   4.3%      pid 50656    0.83 h   0.3%
pid 70072    1.60 h   0.7%      pid 97616    0.65 h   0.3%
pid 24072    1.13 h   0.5%      pid 20076    0.27 h   0.1%
pid 14184    1.08 h   0.4%      pid 34096    0.07 h   0.0%
```

A mean of 0.83 sessions working at once, across a fleet of nine to eleven, is
item 0030's inertia at full scale: seven of the surviving logs end in
`session.idle` between 2026-08-16 and 2026-08-21 and never resume. **Nearly all
of the fleet's measured activity is one runaway session.** Everything except
`54460` totals 16.97 hours — which is real work by nine other sessions, not
nothing, and the fourteen-fold figure below is a statement about
*concentration*, not about the sum being wrong. 240.82 is the correct
fleet-wide total; quoting it as though it were typical of an instance
overstates the rest of the fleet by 240.82/16.97 ≈ 14.2x.

These figures are a lower bound, per item 0033: they are computed from the 21
surviving logs, and the ring held 50 a fortnight ago, so the sessions whose
logs are gone contributed hours that are not counted here. **No claim is made
about which logs eviction takes** — the 2026-08-31 section above measured one
eviction and found the evicted file had five *older* files still retained, and
recorded the rule as not established. It has not become established since.

### What this window is worth

- **On the wave**, nothing. Zero bursts, and the only recorded unaccounted
  ending is a singleton. Against an emitter that fires on many sessions at
  once, this is one draw.
- **On a background per-session rate, no figure should be quoted at all**, and
  an earlier draft of this section quoted one. Dividing the single unaccounted
  ending by 240.82 eventful log-hours puts a numerator and a denominator
  belonging to *different sessions* over one another: the ending is
  `discord-invite-manager` #241, while 93% of the hours are ac-unreal, which
  recorded no unaccounted ending at all. The trials are not merely
  non-independent, they are **not the same kind of trial** — and seven of the
  eleven sessions alive in this window were idle, so they could not have
  suffered the mid-turn harm this item names whatever the rate was. What the
  window supports is a list, not a rate: one recorded singleton on a
  short-lived instance, ~224 eventful hours on one session that recorded
  nothing, eleven endings the count never saw, and an idle remainder that was
  never exposed.
- **On the instrument**, decisively. The ending-count cannot see a co-death,
  and a co-death is now a documented cause of exactly the harm this item
  exists to count.

### For the next reader

The two-step instruction above is not enough. Before reading any ending-count,
run **four** checks, and note that the last two are new:

1. Item 0030's liveness classification, to see whether sessions were running.
2. Eventful log-hours **with the per-log breakdown**, because one runaway
   session can supply nearly all of it.
3. **A completeness check on the count itself.** For every instance that has
   state — not only the ones currently looping — compare its last
   `session_exit` session number against the session number in the newest
   `supervisor_start`. Any gap is that many endings the trace never recorded,
   and the count is short by at least that much. This is computed from the
   trace and instance state, and it is the only check here that gives a
   *number*.
4. **If step 3 is non-zero, find what killed the supervisors**, because step 3
   says how many were lost and nothing about why. `LastBootUpTime` and System
   events 1074/6005/6006/41/6008 catch a reboot; **TerminalServices event 21
   catches a logon-session replacement, which is what the 2026-08-09 wave in
   this file turned out to be and which leaves `LastBootUpTime` untouched.** A
   logoff or a fast-startup hybrid shutdown is the same class again.

Step 6 of the recipe above — "consult the event log only once step 4 has found
a kill" — is therefore **withdrawn as an ordering rule.** A co-death produces
no step-4 row, so gating the event log behind step 4 guarantees the one wave
this item has actually explained would never be looked for. Consult it whenever
step 3 is non-zero.


