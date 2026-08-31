---
id: 39
title: Nothing restarts the fleet after a reboot and nothing reports that it is down; the 2026-08-28 reboot cost 3.85 days
status: proposed
opened: 2026-08-31
spec: none
---

## Evidence

Measured 2026-08-31T23:2xZ on this machine.

The machine rebooted on 2026-08-28. From the Windows System log and
`LastBootUpTime`:

```
2026-08-28T03:02:28Z  evt 1074  shutdown initiated by a process
2026-08-28T03:03:34Z  evt 6006  event log service stopped
2026-08-28T03:04:33Z  evt 6005  system up
```

That ended every supervisor and every session on the machine. **Nothing
started them again for 3.85 days.** The fleet was restored at
2026-08-31T23:18:52-23:19:01Z by nine hand-typed commands, which the trace
records as `kind=user` invocations, one per instance, each from that
instance's own directory:

```
23:18:59Z  argv=--loop --name book-translator          cwd=~\repos\book-translator
23:18:59Z  argv=--loop --name copilot-tools            cwd=~\repos\copilot-tools
23:19:00Z  argv=--loop --name discord-invite-manager   cwd=~\repos\discord-invite-manager
23:19:00Z  argv=--loop --name operator                 cwd=~\repos\operator
23:19:00Z  argv=--loop --name prism                    cwd=~\repos
...
```

Every one succeeded and launched a fresh session, which is itself evidence
that nothing was running: `restart_loop` refuses an instance with no live
session, and a second supervisor for a live instance is refused too. All nine
Copilot leads on the machine now have a start time of 2026-08-31 16:19:01-04
local.

**There is no mechanism that would have done this.** Checked directly:

* `Get-ScheduledTask` — no task whose name or action mentions `operator` or
  `copilot`.
* Startup folder (`%APPDATA%\...\Start Menu\Programs\Startup`) — one entry,
  `Ollama.lnk`.
* `HKCU` and `HKLM` `...\CurrentVersion\Run` — fourteen entries, all
  third-party (Steam, OneDrive, Plex, Teams, ...). None is operator.

So the toolkit installs nothing that survives a reboot, and this is not a
setting that was switched off.

Nothing announced the state either. The instruments that exist report on
instances that are running; with no supervisors alive there is nothing for
them to be silent *about*, so `operator list` prints an empty fleet, which is
the same thing it prints on a machine where the fleet was never started. The
gap was closed by a human noticing.

Cost, measured rather than asserted: 3.85 days multiplied by nine instances is
about 832 instance-hours of loop time that did not happen. For comparison, the
twelve days *before* the reboot produced 240.82 eventful log-hours of agent
activity across the whole fleet (backlog 0001, 2026-08-31 section).

## Why it matters

The supervisor exists so that a session which ends is replaced by one that
carries the work forward. A reboot defeats that at the root: it removes the
thing doing the replacing, so the loop does not resume degraded, it stops
existing. Three and a half days of a nine-instance fleet passed with nothing
running and nothing saying so.

The silence is the expensive half. An empty `operator list` on a machine whose
fleet died in the night is identical to an empty `operator list` on a machine
where nobody has started anything -- the same
signal-indistinguishable-from-its-absence failure this toolkit exists to
refuse, and the same shape as items 0031 and 0034, here applied to the whole
machine rather than one project.

It also silently corrupts backlog 0001, which is this repository's longest
measurement. That item counts endings from `session_exit` records to decide
whether sessions are being killed. A reboot takes the supervisor and the
session in the same instant, so no record is written at all -- not even the
`restart=False, exit_code=null` that step 4 of its recipe calls "the kill
signature, and it is the only one". The window containing this reboot
therefore reads QUIETER than a window without it, while being the window in
which the most context was actually lost: four sessions were killed mid-work,
one of them mid-tool-call with 12.33 GB of log and 11.3 days of context, and
none of the four appears in any count. A defect that makes the fleet's own
loss-measuring instrument read backwards is worth more than the downtime.

## Notes

**Not established: why the machine rebooted.** Event 1074 names a process as
the initiator and the shutdown was orderly, so it was not a crash. Whether it
was Windows Update, a user, or something else is not determined here, and the
remedy does not depend on it -- a fleet that cannot survive a planned reboot
cannot survive an unplanned one either.

**Not established: whether unattended restart is wanted.** Bringing nine
autonomous agents back up unattended after a reboot is a decision with real
consequences -- they resume with --yolo and blanket authority, into
repositories whose state nobody has looked at since the machine went down.
The safe half of this item is the announcement, not the auto-start, and they
should be considered separately.

**The cheap half is to say it.** Something that knows the fleet's intended
membership can compare it against what is running. The project catalogue is
that list for registered projects, though item 0034 measures that 1 of 11 live
instances had no catalogue row, so it is not currently a complete one. A
recorded "these instances were up when the machine went down" would be enough
and needs no catalogue at all.

**A boot-time marker would also date the loss for item 0001.** If the toolkit
recorded a `machine_boot` event in the trace, every reader of that file could
subtract the sessions a reboot removed instead of measuring `LastBootUpTime`
separately and hoping to remember. That is a small addition to
`operator_trace.py` and would have prevented this item's discovery being
accidental.

**Where the remedy belongs is the owner's call.** FROZEN.md limits this
repository to fixes for defects affecting running sessions, and the
supervision kernel is now ../operator. Auto-start is new behaviour and
probably belongs there; the trace marker is arguably a safety fix here,
because without it every future reading of 0001 is wrong in the same
direction.
