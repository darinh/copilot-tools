---
id: 14
title: The no-change circuit breaker cannot see manufactured work
status: proposed
opened: 2026-08-05
spec: none
---

## Evidence

MAX_NOCHANGE_SESSIONS (copilot_operator.py:155) trips when consecutive sessions leave
`workspace_fingerprint()` unchanged: git refs, uncommitted and untracked state.

On 2026-08-08 a session that had no authorized work available produced two real commits
(a08bb2d, 52a3080) on a new branch. Each moved the fingerprint. The breaker read the
session as healthy progress and did not trip.

## Why it matters

The breaker was built to stop a loop that is spinning. This failure is the opposite shape: a loop that is working, on nothing anyone asked for. Unauthorized work does not merely evade the fingerprint, it scores strictly better than correct behaviour -- an agent that finds nothing approved and stops leaves the fingerprint untouched, which is precisely what the breaker trips on.

## Notes

May not be fixable at the fingerprint layer, and a fix that punishes idleness harder makes it worse. The property that separates the two cases is whether a session's commits trace to an approved item, which backlog already knows. Filed to be decided, not to prescribe a design.
