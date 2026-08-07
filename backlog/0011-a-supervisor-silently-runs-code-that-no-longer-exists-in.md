---
id: 11
title: A supervisor silently runs code that no longer exists in the tree
status: closed
opened: 2026-08-05
closed: 2026-08-07
commit: a08bb2d2b5c0919cfe35cc7caff03fef573dc80c
spec: none
---

## Evidence

A running loop supervisor imports `copilot_operator` once, at loop start, and
holds that module object for its entire life. `copilot_tools` is installed
editable (`__editable__.copilot_tools-1.2.7.pth` -> `C:\Users\darin\repos\copilot-tools`),
so every edit to the tree is invisible to it. Nothing anywhere says so.

Diagnosed 2026-08-05 by the `discord-invite-manager` operator instance, which
had spent several sessions chasing what looked like a defect in the
crash-recovery verdict. Evidence chain, reproduced and confirmed:

1. Sessions were being told "a handoff file could not be found for this
   project" while a current `next-session.md` sat on disk. One launch had the
   handoff written at 07:17:48 and the launch at 07:18:25, 37 s later, with
   the false clause still present.
2. `operator.log` has no "No handoff file found for this project - treating
   this as crash recovery" line at that launch. The running code never
   evaluated the verdict.
3. The last such line anywhere in `operator.log` is 2026-08-04 13:28:41.
4. Every `*.loop.pid` in `~/.operator/restart` has an mtime of 2026-08-04
   13:27-13:28. That is when the supervisors started.
5. `crash_recovery_verdict` and its per-launch call site landed in 8f58a00
   (2026-08-04 19:36), about six hours *after* those supervisors started. The
   pre-8f58a00 shape baked the verdict once at loop start.
6. A fresh-process probe of the current code returns the right answer:
   `crash_recovery_verdict` -> False with the handoff present.

Fleet check with the `*.loop.pid` mtime discriminator: all six live
supervisors (book-translator, copilot-tools, discord-invite-manager, finances,
scripts, snes-ghosts) predate 8f58a00. 355 of 1502 launches across all
instances carry the false clause.

To reproduce the class rather than this instance: start a supervisor, change
any function in `copilot_operator.py` that the supervisor calls per launch,
and observe that the running supervisor keeps the old behaviour with nothing
reported anywhere.

## Why it matters

The cost is not one stale verdict; it is that every fix to this file is
invisible until someone restarts, and nothing tells them. Six hours of agents
were told their predecessor had crashed while a perfectly good handoff sat on
disk, and the agents did the expensive correct thing with that: they distrusted
the handoff and re-derived work. The bug reported itself as an absence, which is
the hardest shape to notice.

It also silently invalidates every measurement taken against a running
supervisor. Any session that concludes "the fix is working" or "the fix is not
working" from live supervisor behaviour is reading code that may be arbitrarily
old, and cannot tell.

This is a class, not an incident. An editable install is the normal development
configuration here, and long-lived supervisors are the whole point of the tool,
so the two are always both true.

## Notes

Proposed fix: have the supervisor record what it imported -- the source mtime,
or a hash of copilot_operator.py -- at loop start, and compare on each launch.
When the tree has moved on, say so: a line in operator.log, a line in
`operator status`, and ideally in the session preamble, since the preamble is
where the misinformation lands. That turns "six hours of silent
misinformation" into something a reader can see in one command.

Do not make it restart itself. A supervisor that decides on its own to reload
is a supervisor that can drop a live session for a change it did not
understand; the point is to make staleness legible, not automatic.

The cheap discriminator for a human in the meantime: compare the instance's
`*.loop.pid` mtime against the commit that changed the behaviour in question.

Diagnosed by the `discord-invite-manager` operator instance, which also
declined to restart anything on its own initiative after finding it -- the
right call, since a restart across eight projects was not its to make.

## Correction, 2026-08-05

**The remedy proposed in the Notes already exists.** 6d2385c, "a supervisor's
records now say which code wrote them", landed earlier the same day: a
supervisor stamps a digest of the operator source it actually imported into
its state file and into its trace records, and `operator list` names any
instance whose code cannot be shown to be current, together with the
`restart-loop` command that fixes it. This item was filed without that being
checked, from a diagnosis that was itself correct.

What the fleet looked like at 09:10, immediately after the first supervisor
restart on this machine since the feature landed:

    book-translator  ...  [supervisor code unrecorded]
    copilot-tools    ...
    finances         ...  [supervisor code unrecorded]
    scripts          ...  [supervisor code unrecorded]
    snes-ghosts      ...  [supervisor code unrecorded]

`copilot-tools` is unmarked because it had just been restarted; the other four
predate the record entirely, which is why they read "unrecorded" rather than
"stale". That is also the first time 6d2385c has been exercised in production
-- until some supervisor restarted, every instance read UNKNOWN and the
feature proved nothing.

The residue this item still names, and the only part worth keeping open: the
misinformation lands in the **session preamble**, and `operator list` is not
where the agent reading that preamble looks. An agent told its predecessor
crashed has no reason to go and check whether its supervisor is current, and
355 launches did not. Making staleness legible to a human at the command line
does not make it legible to the agent being lied to.

So the open question is narrower than the title suggests: should a launch
whose supervisor cannot show it is running current code say so *in the
preamble*, next to the claims that code is responsible for? Everything else
here is done.

## Second independent confirmation, 2026-08-05

Reported unprompted by the `scripts` operator instance, which had measured the
symptom carefully and deliberately declined to hand over a mechanism it had
not demonstrated. Its evidence: of 64 launches in
`~/.operator/restart/scripts.runner.log`, 51 carry the "a handoff file could
not be found for this project" note, consecutively, while over the same window
handoffs were being written and read normally — 12 banked handoffs in that
project's `superseded/` plus a live `next-session.md`, and a launch 34 seconds
after that file was written still carried the note.

It explicitly *ruled out* this item's mechanism, and the reasoning is worth
recording because it is wrong in an instructive way: it compared the notice
dates against the **commit date** of 8f58a00 and found 13 notices postdating
it. That does not discriminate. A commit landing does not change a running
process, so the number to compare against is the **supervisor start time**.
Measured: `scripts.loop.pid` has mtime 2026-08-04 13:27:53 and 8f58a00 landed
2026-08-04 19:36:03 — the supervisor started six hours before the fix and is
running the pre-8f58a00 shape that baked the verdict once at loop start. One
decision replayed 51 times, not 51 evaluations.

The same fact explains the observation that made it doubt the mechanism:
`scripts.runner.log` contains none of the verdict's branch log strings, because
the running code predates those log lines entirely. **An absent log line is
evidence about which code is running, not about which branch it took** — and
reading the source cannot show it, because the source is not what is executing.

`operator list` named the instance `[supervisor code unrecorded]` at the time
of the report, so 6d2385c's record did fire; it simply is not where either
agent looked. That is the residue above, now confirmed twice: the misinformation
lands in the preamble and the remedy is legible only at the command line. The
second instance cost a peer a diagnosis it could not complete rather than
several sessions, which is an improvement attributable to nothing in this
repository.

## Resolved for that instance, and a sharper signature, 2026-08-05

The `scripts` instance re-measured against the boundary this item names and
ran the remedy. Both halves are worth keeping, because the measurement is a
better tell than the one this item had, and the remedy is the first recorded
confirmation that it works on a live session.

Classifying **every** launch line in `scripts.runner.log` by whether it
carries the note, rather than looking for the first one:

    off  from 2026-07-31 17:09:08
    ON   from 2026-08-01 15:36:33
    off  from 2026-08-01 23:02:00
    ON   from 2026-08-04 13:27:54   -> 36 of 36, no gap

`scripts.loop.pid` mtime: **2026-08-04 13:27:53**. The first notice of the
run lands **one second after the supervisor process starts**, and then every
launch of that run carries it without a gap. A verdict evaluated per launch
cannot produce that; a verdict taken once at loop start produces exactly it.

The re-measurement also refuted the report's own framing — "consecutive since
08-01 23:16:54" was wrong, there was an earlier note-carrying block on 08-01
from a **previous run**. That is the same shape as the mistake corrected
above: a window drawn from where the evidence was first noticed rather than
from where the process boundaries are. Blocks of the note are per-run, so the
unit to bucket by is the supervisor process, not the day.

`operator restart-loop scripts` cleared it: pid 5928 -> 42356, the live
session kept running, and `operator list` immediately dropped the tag. The
four other instances were deliberately left alone — live sessions in other
projects, not that agent's call to make.

Two things that peer asked be recorded, both of which are about how this is
diagnosed rather than what it is:

1. **The absent log line is the one piece of evidence that actively misleads.**
   It reads as "the verdict was not reached through that code" and it means
   "the running code predates those log lines". This item already says so a
   few paragraphs up; it is repeated here because two independent agents
   reached for that evidence first and both were pointed the wrong way by it.
   It is worth more than a mention in passing.
2. **`operator list` naming the condition is what allowed the fix to be
   confirmed without waiting for a launch.** The value of 6d2385c's record is
   not only that it names the condition, but that it makes the remedy's effect
   observable immediately — otherwise confirmation costs a restart plus a wait
   for the next session, which is long enough that nobody checks.

One thing a parallel write-up of this exchange caught that the account
above does not: **step 2 of the Evidence section is itself that
inference.** "`operator.log` has no ... line at that launch. The running
code never evaluated the verdict" reads the absence as a statement about
which branch ran. It reached the right conclusion, but only because step 4
was already in hand — which is exactly the condition under which this
evidence is safe, and exactly the condition a reader diagnosing it fresh
does not have.

## Third confirmation, and the residue closed, 2026-08-07

Reported by the `discord-invite-manager` operator instance, which supplied
two things this item could not produce for itself.

**It closed the discrimination the second confirmation left open.** That run
proved the crash-recovery clause behaves correctly when a handoff is present,
and flagged that it could not tell "the clause is gone entirely" from "the
clause is now correctly conditional". `operator.log`, 2026-08-07 07:05:18:

    No handoff at ...\handoff\discord-invite-manager.md, but an unmigrated
    one is at ...\next-session.md -- not reporting this as crash recovery

That is the genuinely-absent-at-the-canonical-path case, and the code did not
merely stay silent: it reasoned about the alternate location and explicitly
declined to claim a crash. The clause is conditional and the condition is
correct.

**An absent tag in `operator list` is ambiguous, and the digest is what
settles it.** `CODE_UNKNOWN` prints nothing, exactly as `CODE_CURRENT`
does, so "no tag" is not a verdict. That peer recomputed SHA-256 over all 19
recorded files itself -- 19/19 matching, pid alive -- and only then concluded
its supervisor was current. This instance did the same and reached the same
answer. It is the same failure shape as the absent log line above: **a signal
whose silence has two causes cannot distinguish them, and reads as the
reassuring one.**

**On the shape of the remedy**, that peer pushed on the wording rather than
the placement, and was right to. "This supervisor cannot show it is current"
is an epistemic state of the supervisor; "your predecessor crashed" is a claim
about the previous session. If the caveat is phrased as the second it becomes
a second confident-sounding sentence in the exact place the first one already
misled. It has to read as *the supervisor declining to vouch for its own
output*. Its own session is the evidence: its handoff instructed a restart on
the assumption the supervisor was stale, and only independent verification
avoided a pointless one. An agent that trusts its preamble does the wrong
thing; an agent that distrusts it burns a session verifying. Both costs are
paid in the preamble, which is the argument for putting the claim there.

That peer also reported the same class from its own repository, in a domain
with no supervisor in it: a "test suite is green" claim inherited through six
consecutive verification-only sessions, where actually running the suite gave
exit 1 on a real nondeterministic failure. **A claim cheap to check and
expensive to inherit, with nothing in the loop forcing the check** -- the
supervisor case is one instance of that, not the whole of it.

## Resolution

Closed by `a08bb2d`, recorded above. `build_preamble` now consults
`_launch_code_state()` and appends a caveat for `CODE_STALE` and for the
cannot-tell verdicts, worded per the argument above: scoped to this preamble's
own claims rather than issued as a general warning, wording the two verdicts
differently because one is an observed difference and the other an absence of
evidence, naming `restart-loop` as information rather than as an
instruction, and silent for `CODE_CURRENT` so the caveat does not become
another always-present line that stops being read.
