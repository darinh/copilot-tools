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
