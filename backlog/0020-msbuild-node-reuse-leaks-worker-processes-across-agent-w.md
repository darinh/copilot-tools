---
id: 20
title: MSBuild node reuse leaks worker processes across agent worktrees
status: proposed
opened: 2026-08-05
spec: none
---

## Evidence

Reported by the user on 2026-08-05: a VM running copilot-tools ran out of RAM.
Diagnosis given: MSBuild node reuse is on (`nodeReuse:true`) by default, so
every build leaves a pool of long-lived `MSBuild.exe` worker nodes behind. Agent
worktrees multiply this: each worktree is a distinct project directory, so the
nodes are not shared between them, and operator agents build repeatedly across
many worktrees over a long-lived loop. The pool is never reclaimed while the
processes stay alive, so memory use grows monotonically with the number of
worktrees built in rather than with concurrent work.

Not reproduced locally by this agent -- this repository is Python and runs no
MSBuild at all, so the evidence here is the user's report from a .NET project
running under operator, not a measurement taken in this tree.

Where the fix goes: operator never sets an environment for the sessions it
launches. `Mux.new_session(session, cwd, argv)` (`operator_mux.py:240`) shells
out to `tmux new-session -d -s ... -- argv` with no env parameter, and
`copilot_operator.py` reads `os.environ` in several places but writes it
nowhere. So there is currently no seam for "environment every operator agent
session should inherit", and this item needs one.

## Why it matters

Operator agents run unattended for weeks. A leak that grows with the number of worktrees built in, and that nothing in the loop reclaims, ends as an out-of-memory on the whole VM -- taking down every instance on it, not just the one that built. The agent holding the failure has no way to attribute it to a build it ran days earlier in a worktree that has since been deleted.

## Notes

Suggested fix from the user: set MSBUILDDISABLENODEREUSE=1 in the operator agents' environment. Cheap and reversible. Worth deciding at the same time whether operator should own a general 'session environment' seam rather than one variable -- but note that a general mechanism is a larger change than the problem needs, and the one-variable fix does not block adding the seam later. Not actioned: filed proposed per the standing approval gate.
