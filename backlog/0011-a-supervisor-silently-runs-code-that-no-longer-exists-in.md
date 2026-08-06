---
id: 11
title: A supervisor silently runs code that no longer exists in the tree
status: proposed
opened: 2026-08-05
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

## The absent log line is the trap, 2026-08-06

Reported by the `scripts` operator instance, which hit this class again and
re-measured it. Worth recording because it is the one piece of evidence in
this class that **actively points the wrong way**, and this item's own
evidence chain steps straight into it.

`scripts` saw 51 of 64 launches carrying the false "a handoff file could not
be found" clause while handoffs were demonstrably being written and read --
12 banked in `superseded/` plus a live `next-session.md`, and a launch 34
seconds after that file was written still carried the note. Its strongest
lead was an absence: `crash_recovery_verdict` logs a line on *every* branch
that returns early, and the runner log contained none of those strings. The
natural reading is "the verdict is not being reached through the code I
read" -- so you go looking for a second call site, or a second log sink.

There is no second call site. **An absent log line is evidence about which
code is loaded, not about which branch ran.** The log lines are younger than
the running process, so nothing it does can produce them.

Step 2 of the Evidence section above ("`operator.log` has no ... line at that
launch. The running code never evaluated the verdict") is that same
inference. It reached the right conclusion, but only because step 4 was
already in hand.

The antidote is to pair the absence with the pid mtime, which is positive
evidence and dates the process rather than the code. `scripts` measured the
boundary precisely: the first note-carrying launch of the run landed at
13:27:54 and `scripts.loop.pid` had an mtime of 13:27:53 -- one second
earlier. A verdict taken once at loop start has no cleaner signature than
that, and it also refuted its own earlier framing that the notes were
consecutive since 08-01, which a run boundary explained and a code path did
not.

Second thing `scripts` reported, which is a result rather than a lesson:
`operator list` naming the condition let it confirm the fix **without waiting
for a launch**. That is the affordance the Correction above says had never
been exercised in production; it now has been, by a second instance, for a
purpose it was not specifically built for.
