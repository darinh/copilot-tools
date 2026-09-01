---
id: 39
title: Nothing restarts the fleet after a reboot and nothing reports that it is down; the 2026-08-28 reboot cost 3.85 days
status: proposed
opened: 2026-08-31
spec: none
---

## Evidence

Measured 2026-08-31T23:21Z-23:30Z on this machine.

The machine rebooted on 2026-08-28. From the Windows System log and
`LastBootUpTime`:

```
2026-08-28T03:02:28Z  evt 1074  shutdown initiated by a process
2026-08-28T03:03:34Z  evt 6006  event log service stopped
2026-08-28T03:04:33Z  evt 6005  system up
```

That ended every supervisor and every session on the machine. **Nothing
started them again for 3.85 days.** The fleet was restored at
2026-08-31T23:18:52-23:19:01Z by a burst of user-originated invocations, which
the trace records as `kind=user` — a bare `operator` at 23:18:52 followed
seven seconds later by nine `--loop` launches within two seconds of each
other, each from its instance's own directory:

```
23:18:52Z  argv=(bare operator)                        cwd=~
23:18:59Z  argv=--loop --name book-translator          cwd=~\repos\book-translator
23:18:59Z  argv=--loop --name copilot-tools            cwd=~\repos\copilot-tools
23:19:00Z  argv=--loop --name discord-invite-manager   cwd=~\repos\discord-invite-manager
23:19:00Z  argv=--loop --name operator                 cwd=~\repos\operator
23:19:00Z  argv=--loop --name prism                    cwd=~\repos\prism
23:19:01Z  argv=--loop --name repos                    cwd=~\repos
...
```

Nine launches in two seconds is not nine things typed by hand, and this item
does not claim it was. It is consistent with `operator restore --all`, which
is exactly the documented recovery path (below); what the trace establishes is
only that a human initiated it, not that they typed each line.

Every one succeeded and launched a fresh session, which is itself evidence
that nothing was running: `restart_loop` refuses an instance with no live
session, and a second supervisor for a live instance is refused too. All nine
Copilot leads on the machine now have a start time of 2026-08-31 16:19:01
local (UTC-07:00).

**The recovery mechanism exists; the trigger does not.** An earlier draft of
this item missed this and was wrong to. `operator restore` replays
`~/.operator/tabs.json`, is offered in the bare-`operator` menu, and is
documented for precisely this case (`copilot_operator.py:1398` `restore_tabs`,
`docs/operator.md:134-139`):

```
operator restore              # pick which tracked tab(s) to reopen
operator restore --all        # reopen every tracked tab, resuming sessions
```

`tabs.json` is therefore also the recorded "these instances were up" list — a
later note in this item proposed inventing one, and it already exists. So the
defect is **not** that recovery is impossible or unrecorded. It is that
nothing *fires* the recovery and nothing *says* it is needed.

**Nothing this toolkit installs runs at boot.** Checked directly:

* `Get-ScheduledTask` — no task whose name or action mentions `operator` or
  `copilot`.
* Startup folder (`%APPDATA%\...\Start Menu\Programs\Startup`) — one entry,
  `Ollama.lnk`.
* `HKCU` and `HKLM` `...\CurrentVersion\Run` — fourteen entries, all
  third-party (Steam, OneDrive, Plex, Teams, ...). None is operator.

Those are three persistence surfaces, not all of them: Windows services,
unnamed scheduled tasks, logon scripts, Group Policy and the alternate
registry views were not swept. The claim they support is bounded accordingly —
**no direct operator reference exists on the three surfaces checked**, so the
absence of boot recovery is not a setting somebody switched off on one of
them.

Nothing announced the state either. The instruments that exist report on
instances that are running; with no supervisors alive there is nothing for
them to be silent *about*, so `operator list` prints an empty fleet, which is
the same thing it prints on a machine where the fleet was never started —
even though `tabs.json` on that same machine still lists what ought to be up.
The gap was closed by a human noticing.

Cost: 3.85 days across the nine instances that were later restored is about
832 instance-hours during which no loop ran. **That is nine restored instances,
not a measured fleet total** — eleven sessions were alive before the reboot,
and the nine is what somebody chose to bring back. **Nor is it 832 hours of
lost work.** The twelve days before the reboot produced 240.82 eventful
log-hours in total, 93% of it from one session on an instance that was *not*
among the nine restored, at a fleet mean of 0.83 sessions active at once
(backlog 0001, 2026-08-31 section). The honest statement is that fleet
availability was zero for 3.85 days; what it would have produced is not
measurable from here.

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
which the most context was actually lost: eleven sessions ended without a
record, one of them mid-tool-call after 11.3 days, and none of the eleven
appears in any count. A defect that makes the fleet's own loss-measuring
instrument read short is worth more than the downtime.

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

**The cheap half is to say it, and most of the parts already exist.**
Something that knows the fleet's intended membership can compare it against
what is running, and `~/.operator/tabs.json` is already that list — it is what
`operator restore` replays, and it survived the reboot intact. An earlier
draft of this note proposed recording "these instances were up when the
machine went down" as though it had to be invented; it does not. What is
missing is something that performs the comparison and says the answer out
loud. The project catalogue is a second candidate list, though item 0034
measures that 1 of 11 live instances had no catalogue row, so it is not
currently complete; `tabs.json` needs no catalogue at all.

**A boot marker cannot be written at boot, which is the point of this item.**
An earlier draft proposed that `operator_trace.py` record a `machine_boot`
event so readers of item 0001 could subtract the sessions a reboot removed.
Nothing in this toolkit is alive at boot — that is the defect being filed — so
such a marker could only be written by the *next* `operator` process to run,
which here was the human's relaunch 3.85 days later. That is a convenience
over reading `LastBootUpTime`, not a detector, and it announces nothing while
the fleet is down. Writing it *at* boot requires a boot-time task, which is the
auto-start half deferred above. Withdrawn as a remedy for this item; if it is
wanted at all it belongs in item 0001's re-measurement recipe, which now tells
the reader to check `LastBootUpTime` and the event log directly.

**Where the remedy belongs is the owner's call.** FROZEN.md limits this
repository to fixes for defects affecting running sessions, and the
supervision kernel is now ../operator. Both halves — auto-start and the
announcement — are new behaviour rather than repairs to something that
misbehaves while sessions run, so on the plain reading of the freeze both
belong in the kernel. An earlier draft argued the trace marker was "arguably a
safety fix here, because without it every future reading of 0001 is wrong in
the same direction"; that is withdrawn. 0001 being misread is a measurement
problem, not a running-session defect, and the correction for it has landed
where it belongs — in 0001's own recipe, which now carries a completeness
check that needs no new code at all.
