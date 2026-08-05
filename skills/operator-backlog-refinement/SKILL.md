---
name: operator-backlog-refinement
description: Refine backlog items until an agent could pick one up and finish it without asking anything, then walk the product owner through approving them. Use before starting work when the queue is empty, or when an item is too vague to execute.
---

# Refine the backlog

`$ARGUMENTS` may name one item id. With no argument, refine everything that is
awaiting approval.

Refinement is the step that makes a queue *consumable*. An unrefined item
guarantees the failure it was filed to prevent: an agent picks it up, discovers
it cannot tell what "done" means, and either asks (ending the session) or
decides for itself (shipping something nobody asked for).

## What to refine

```
backlog ready --explain      # the queue, and why everything else is out of it
backlog list --status proposed
backlog show <id>
```

Work on the `proposed` items. Approved items are the product owner's decision
already made — do not rewrite their scope under the guise of refining them; if
one is wrong, say so and let the owner decide.

## What "refined" means

An item is refined when **another agent, with no memory of this conversation,
could finish it and know that it had.** Concretely:

1. **Evidence a stranger can verify.** A date, a command, an observed output.
   Replace "it's flaky" with the run that failed and what it printed.
2. **A stated outcome.** What is true when this is done that is not true now.
   Not a design — an observable.
3. **Scope, including its edges.** Say what is *not* in it. An item with no
   stated boundary grows one at implementation time, chosen by whoever is
   holding it.
4. **Named risk.** Which files, and how dangerous. Auth, payments, data
   deletion, schema migration and concurrency all deserve saying out loud.
5. **A spec tie.** `spec:` is a path under `specs/` or the literal `none`, and
   `none` must be written out — "this changes no specification" is a decision
   somebody recorded, not a silence that could equally mean nobody looked.

Edit the item file directly. Keep the front matter intact; the body is prose.

## Ask, do not guess

Where the item is ambiguous, ask the user — one question at a time, each
phrased so that either answer changes what gets built. A question whose answers
lead to the same implementation is not worth a turn.

Do not resolve an ambiguity by picking the more likely reading and writing it
down as settled. A guess recorded in an item is indistinguishable from a
decision, and the next agent has no way to tell it was invented.

## Approval is the owner's act, not yours

```
backlog approve <id>          # proposed -> open
```

**Run this only when the user has said yes**, and say so plainly when you
report: "approved 7 on your say-so". Nothing in the tool can stop an agent
approving its own work — the file is right there — so the only real control is
that the act is visible in the commit and that you do not do it unasked. An
agent that approves its own items has not found a shortcut; it has removed the
one signal telling the owner what they agreed to.

If an item should not be built, say why and leave it. Rejecting is also the
owner's decision: it takes a `closed` date and no commit, because nothing
shipped.

## Finishing

1. `backlog check` must pass.
2. Commit the refinements. Refinement that lives only in a working tree is not
   refinement.
3. Report: how many items are now ready, which are still awaiting approval, and
   any question you are still blocked on.
