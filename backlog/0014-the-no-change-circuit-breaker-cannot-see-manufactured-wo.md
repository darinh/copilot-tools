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

## Done when

Stated as properties rather than a design, because the item is filed to be
decided and a design written here would read as one:

- A loop whose sessions commit work that traces to no approved item does not
  read as healthy progress.
- A session that correctly finds nothing approved and stops is not punished for
  it. Today it scores strictly worse than the unauthorised case, and any fix
  that leaves that ordering intact has not addressed the item.
- Whatever signal is added can be observed from outside: given a session's
  commits, a reader can say which item authorised them, or that none did.

## Not in scope

- Making the breaker stricter about idleness. The item's own argument is that
  this makes the failure worse.
- Deciding what happens when the breaker trips. Only what it can see is in
  question here.

## Risk

🟡 `copilot_operator.py:155` (`MAX_NOCHANGE_SESSIONS`) and `workspace_fingerprint()`.
This is the supervisor's runaway-loop brake: too eager and it kills working
fleets, too slack and it stops being a brake. Treat any change here as needing
both a positive and a negative control.

## Needs a decision before this can be worked

- Whether the breaker may consult the backlog at all. That couples supervision
  to a work-tracking convention that not every project using this toolkit has,
  and the coupling is the substance of the item rather than a detail of it.

## Notes

May not be fixable at the fingerprint layer, and a fix that punishes idleness harder makes it worse. The property that separates the two cases is whether a session's commits trace to an approved item, which backlog already knows. Filed to be decided, not to prescribe a design.
