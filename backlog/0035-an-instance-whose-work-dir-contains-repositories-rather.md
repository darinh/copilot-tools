---
id: 35
title: An instance whose work dir contains repositories rather than being one is named after the container, and the progress breaker never runs
status: proposed
opened: 2026-08-16
spec: none
---

## Evidence

Measured 2026-08-16T18:02Z, prompted by a user report that "tiktok-offline
project is running in a loop without a project and the operator doesnt display
it in the list of loops or sessions".

**Both halves of that report are correct, and the loop is not the one the
report names.** `~/repos/tiktok-offline` has no instance, no supervisor and no
pane. The work in it is being done by the `repos` instance, whose work dir is
`~/repos` — the directory that *contains* the repositories.

From `~/.operator/operator.log`, the launch block, unedited:

```
[operator 2026-08-15 15:57:06] Started background loop supervisor for 'repos' (pid 18416)
[operator 2026-08-15 15:57:07]   Instance: repos
[operator 2026-08-15 15:57:07]   Progress breaker: inactive - no readable git state in C:\Users\darin\repos
[operator 2026-08-15 15:57:07]   Work dir: C:\Users\darin\repos
[operator 2026-08-15 15:57:07]   Session #1 running (copilot pid=24048)
```

`C:\Users\darin\repos` has no `.git`, which is why the progress breaker
declared itself inactive — and it has stayed inactive for the 19 hours since.

Copilot process 24048 is that session and is still alive. Its pinned log
mentions `tiktok-offline` **151,122 times**, more than any other path, and the
process tree confirms the ownership: 24048's parent is 40024 and its
grandparent 80732, which is the `repos` pane in `tmux list-panes -a`
(`repos 80732`), running `operator_runner.py .../restart/repos.launch.json`.

Meanwhile `git -C ~/repos/tiktok-offline log` shows a working agent:

```
1165a11 2026-08-16 10:38:24 -0700 Document sorting, count refreshing, and what blank figures mean
a251a5c 2026-08-16 10:35:31 -0700 Refresh engagement counts periodically without re-downloading media
870798b 2026-08-16 10:24:07 -0700 Remove a scratch diff dump and ignore that pattern
4c314b0 2026-08-16 10:23:47 -0700 Validate the stored sort preference and cover sorting in the UI test
d0f605b 2026-08-16 10:20:30 -0700 Sort the archive by popularity or publish date, either direction
95575f0 2026-08-16 10:01:30 -0700 Add archive repair for drift between the database and disk
b720351 2026-08-16 09:56:50 -0700 Move archive out of the repo; fix issues found by the review council
b01b76a 2026-08-16 08:13:35 -0700 Rebuild the viewer around profiles, grids, and a scoped player
```

Eight commits in two and a half hours, none of which any operator surface
attributes to `tiktok-offline`. `operator list` reports the instance as
`repos · looping · session #1 · up 19h 0m · ~\repos`.

**What the fleet's own instruments say about it, each one correct and each one
useless here:**

| instrument | reading | why it does not help |
|---|---|---|
| `operator list` | `repos · ~\repos` | names the container, not the repository |
| `tmux list-panes -a` | `repos 80732` | same |
| `~/.operator/tabs.json` | `repos`, cwd `~\repos` | same |
| `~/.operator/restart/*.loop.pid` | no `tiktok-offline` entry | correct: there is no such instance |
| progress breaker | inactive since launch | `~/repos` has no git state to read |
| catalog | no row for `repos` **or** `tiktok-offline` | item 0034 |
| handoff | still session #1 after 19h | item 0031 — uncatalogued, so it cannot |

The user separately ran bare `operator` inside the repository at
2026-08-16T17:34:01Z looking for the missing loop. The trace records it with
ancestry `WindowsTerminal.exe -> powershell.exe -> operator.exe`, `argv: []`,
and **nothing follows it** — no `supervisor_start`, no `operator.log` line, no
pane. So the one action taken to find the loop also left no record.

Liveness at the moment of measurement, by item 0030's recipe: 352,185 markers
in the log, newest event of any kind `session.background_tasks_changed` and
newest non-background event **`session.idle`**, both 23.9 minutes old, with the
process up and the supervisor reporting `looping`. The session that is doing
all of this had stopped, and that too is invisible.

## Why it matters

An instance's work dir is the unit every operator surface names. When that dir
is a repository, the name is meaningful: `operator list` tells you what is
being worked on, `operator ownership check` can refuse a branch that left its
subproject, the progress breaker can see whether anything changed, and the
handoff lands beside the code. When the work dir is a *container* of
repositories, every one of those degrades at once and none of them says so.

The costs here are concrete and all present in this one instance:

* **The progress breaker has been off for 19 hours.** It reported itself
  inactive at launch because `~/repos` has no git state, so the loop that is
  supposed to stop after three sessions that change nothing cannot see the
  eight commits that were made, and equally could not have seen zero.
* **Ownership is unenforceable.** The agent is free to commit in any repository
  under `~/repos` - which is every project on this machine, including the
  checkouts belonging to the other ten running instances - and no boundary
  declaration can express that, because the instance is not scoped to a
  subproject.
* **The human cannot find the loop.** That is the report this item was filed
  from. Someone watching an agent commit to `tiktok-offline` every few minutes
  asked operator to show them the loop and operator said there wasn't one,
  because operator was asked about a repository and it only knows about
  instances.

It also silently combines with two filed items into something worse than
either. Item 0034: uncatalogued launch says nothing, and this instance has no
catalog row. Item 0031: an uncatalogued instance cannot hand off, and this one
is still session #1 after 19 hours - so the entire day of context is held in a
process that item 0030 has just measured as idle, with no handoff written and
nothing watching for it.

## Done when

- Launching with a work dir that is not a git repository prints one line naming
  it as such and listing what that disables: the progress breaker, ownership
  enforcement, and repository naming in every operator surface.
- The same statement reaches the agent in its preamble.
- A session whose work dir *is* a repository prints neither -- the control, as in
  item 0034.
- The state is announced whether or not the launch proceeds.
- The launch record makes the configuration visible after the fact, not only at
  the moment it scrolls past. Nineteen hours of commits to `tiktok-offline` were
  attributed to nothing; a reader coming to `operator.log` afterwards should be
  able to see why.

## Not in scope

- **A repository-oriented view** -- answering "what loop, if any, is working in
  this repository", which is the question the user actually asked. That is the
  other half of this and is genuinely separate: it also covers the sub-agent and
  worktree cases where one instance legitimately touches several repositories.
  If it is wanted, it wants its own item; it is not a bigger version of the
  launch warning.
- Re-enabling the progress breaker for a container work dir. Whether that is even
  meaningful depends on the decision below.

## Risk

🟢 the launch path's reporting and one preamble clause. Same kernel-budget
constraint as item 0034 if the clause lands in `operator_kernel/preamble.py`:
item 0038 measures 11 total lines of headroom against a `<=` ceiling, so measure
the clause's net cost rather than assuming it fits or does not.

## Needs a decision before this can be worked

- **Whether operator should also refuse to launch with a container work dir.**
  The item records refusal as "not established" and argues, as item 0034 does,
  that costing an agent a session is worse than costing it a hint. Unsettled, and
  not settled here. It does not block the announcement above.
- **Whether a container work dir is a supported configuration.** It may be that
  a roving instance with the whole `~/repos` tree in scope is wanted, which is a
  legitimate thing to want. If it is supported, this item is a warning; if it is
  not, the remedy is different and larger. The item is explicit that the defect
  is not the choice but that the choice is unannounced -- and that framing
  depends on the choice being allowed.

## Notes

**Not established: whether the container work dir was deliberate.** It may be
that someone wanted a roving instance with the whole `~/repos` tree in scope.
That is a legitimate thing to want, and the defect here is not the choice - it
is that the choice is unannounced and turns off the progress breaker, ownership
and repository naming as a side effect.

**Not established: whether operator should refuse it.** Item 0034 argues
against refusing to launch on an uncatalogued repository, and the same argument
probably applies here: costing an agent a session is worse than costing it a
hint. The cheap version is again to say it - one launch line naming the work
dir as a non-repository and listing what that disables, and the same statement
in the preamble the agent reads.

**A repository-oriented view is the other half.** Everything above is a naming
problem: operator can only answer questions about instances. Nothing today
answers "what loop, if any, is working in this repository", which is the
question actually asked. That is worth considering separately from the launch
warning, because it also covers the sub-agent and worktree cases where one
instance legitimately touches several repositories.

**Do not read this as "the loop is broken".** The instance is running exactly
as configured and has produced eight commits this morning. What is broken is
that no instrument connects those commits to the repository they landed in.
