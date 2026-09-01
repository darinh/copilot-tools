---
id: 28
title: Sub-agent message provenance (agent-<uuid>) is the largest unhandled source family and nothing reads it
status: proposed
opened: 2026-08-09
spec: specs/005-conversation-log/spec.md
---

## Evidence

Found while implementing provenance-based classification (commit a17c3e4),
by scanning every `user.message` event in
`~/.copilot/session-state/*/events.jsonl` on this machine, 2026-08-09:

    session dirs with events.jsonl: 2233   total 3088.9 MB
    scanned 6779 user.message events in 12.0s
    sources:
       2884  None                                  <- the person typing
        597  'instruction-discovery'               <- handled
        442  'thinking-exhausted-continuation'     <- handled
        131  'skill-*'                             <- handled
       2720  'agent-<uuid>'                        <- NOT handled

`agent-<uuid>` is the largest machine-source family in the corpus, larger
than the other three combined, and nothing in `conversation_log.py` reads it.
Examples of the exact values:

    'agent-10609269-13dc-4ebc-8539-db2ff6ab098d'   69
    'agent-213f3d8c-5d27-44a1-9cc7-4cba01a8c5ef'   61
    'agent-5f081790-16c1-45b4-aa65-f241272480d2'   27

The third of those is the session id of the session that filed this item, so
at least some of these are messages a parent session injected into a
sub-agent it launched.

What is NOT established, and must be measured before anything is built:

- How many of the 2720 correspond to rows in `turns`, and therefore to rows
  in the conversation store at all. The store holds 3,527 turns total while
  these events number 2,720, so the overlap could be anywhere from none to
  most. Sub-agent sessions may be recorded as their own sessions with their
  own ids, in which case some of this is already filed under those sessions.
- Whether `agent-<uuid>` names the *sending* agent or the *receiving* one.
- Whether the uuid resolves to a session id in `sessions`, an instance, or
  neither.

The classification consequence is unknown until the first of those is
answered, which is exactly why this is filed rather than fixed.

## Why it matters

The conversation log exists to answer "what did I say, and what came back".
It now separates the human from the operator preamble, the CLI's own
injections and a batch pipeline. Agent-to-agent traffic is separated too --
but only the kind that travels through `operator send` and lands in the mail
store.

Delegation inside a session is a different path and is currently invisible.
If any of the 2,720 `agent-<uuid>` events reach the store, they are filed by
whatever the prose rules make of them, which for most text means `human`.
That is the same defect this feature has now fixed three times in other
guises, and it is the largest remaining source family.

It also has a shape the others do not: a message from a sub-agent is neither
machine scaffolding nor the human. It is an agent talking, in a channel the
store has no name for -- `agent-agent` currently means peer instances
exchanging mail, not a parent delegating to a child. Deciding that vocabulary
is the substance of this item.

## Done when

The item is two-staged and the first stage can close it.

**Stage 1 -- the measurement, and on its own a sufficient end for this item:**

- The number is known: how many of the 2,720 `agent-<uuid>` events correspond to
  rows in `turns`, by the session-id-and-exact-content join whose mapping was
  verified 194/194 in a17c3e4.
- If that number is near zero, this item closes as `rejected` **with the number
  recorded**. The events exist and never reach the store; nothing needs building
  and the next agent to notice the family does not pay for the measurement again.

**Stage 2 -- only if the number is non-zero:**

- What the uuid names is established, not assumed. `sessions.id` is the cheap
  candidate and either resolves or does not.
- Whether it names the sending or the receiving agent is established.
- A channel vocabulary is chosen and applied, and `specs/005-conversation-log/spec.md`
  is updated in the same commit that applies it.
- The control this feature has needed every previous time holds: a real human
  message is not reclassified. Without that control the change is unfalsifiable
  by its input.

## Not in scope

- The three source families already handled (`instruction-discovery`,
  `thinking-exhausted-continuation`, `skill-*`).
- Redefining `agent-agent`, which today means peer instances exchanging mail,
  unless stage 2's vocabulary decision explicitly does so.

## Risk

🟡 `conversation_log.py`. Misclassification rewrites what the log says was said
and by whom, and nothing downstream can detect it -- which is why the human-message
control is not optional.

## Needs a decision before this can be worked

None yet. Stage 1 is a query, and the vocabulary question in stage 2 should not
be answered before stage 1 says whether it matters.

## Notes

Sequenced deliberately after a17c3e4 rather than folded into it. That change
had verified answers for the three source families it handled; this one has a
count and no diagnosis, and shipping a classification rule on that basis is
what the asymmetry in this feature forbids.

First measurement, before any design:

    SELECT COUNT(*) FROM turns t WHERE EXISTS (
      -- join turns to events by session id and exact content, the mapping
      -- verified 194/194 in a17c3e4
    );

If the answer is near zero, close this as rejected with the number: the
events exist but never reach the store, and nothing needs building.

Only if it is non-zero:

1. Resolve what the uuid names -- `sessions.id` is the obvious candidate and
   is cheap to test.
2. Decide the vocabulary. Options, none chosen: a new channel
   (`agent-subagent`), reuse of `agent-agent` with the recipient naming the
   child, or `system` with sender naming the parent.
3. Whatever is chosen, the control is the same one this feature has needed
   every time: a real human message that must not be reclassified.

Related: the review council for a17c3e4 (sol, gemini, grok) reviewed the
stored corpus and none of them surfaced this, because it is not visible from
the database -- it is only visible in the CLI's event logs, which nothing was
reading until that commit.
