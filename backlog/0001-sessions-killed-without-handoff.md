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
  every instance has the `*.loopargs.json` written three lines earlier in the
  same startup. The stamp has never been written.
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

So the boundary is testable with the instrument in place, and it was testable
in 06:30Z too. Measured 2026-08-05T11:35Z:

- Across the six instances, `SESSION_NUM` has advanced 65 past the last
  session each one has a `session_exit` for — so **at least 59 sessions have
  ended** since 01:02:59Z.
- `trace.jsonl` records `session_exit` for **none** of them. Under the running
  code, that means every one of the 59 ended on the restart-marker branch,
  i.e. by handoff.
- The trace is not merely dead: it holds 50 `invoke`/`exit` records after the
  boundary, the most recent 2026-08-05T11:34:41Z.

Corroborated by `operator.log`, which the same supervisors write through a
different code path. After the boundary it holds **59 `restart signal
detected!` and zero `copilot exited unexpectedly`** — no `Giving up`, and no
unreadable-marker lines either. The 59 matches the count derived from
`SESSION_NUM` exactly, from an instrument that shares no code with it. All
time, that log holds 289 restart signals against **1150** unexpected exits, so
it is demonstrably capable of recording the event whose absence is being
claimed.

**The kills stopped at 2026-08-05T01:02:59Z and have not resumed in the 10.7
hours and 59 sessions since.** That is now a measurement rather than a hedge.

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
- `trace.jsonl`: any `session_exit` at all after your boundary is a kill under
  pre-fix supervisors. Once a supervisor has been restarted onto post-fix
  code, its records carry `code=` and both endings appear, so check
  `markers.restart` before reading a record as a kill.
- `operator list` now names any supervisor whose code is stale or unrecorded.
  If it names one, that instance's records are pre-fix and the rule above
  applies to them.

