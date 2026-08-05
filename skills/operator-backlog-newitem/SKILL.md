---
name: operator-backlog-newitem
description: Turn a request into a tracked backlog item instead of an interrupt. Use when the user asks for a feature, reports a defect, or describes work to do later - and whenever you find a defect mid-task that you are not currently authorised to fix.
---

# File a backlog item

`$ARGUMENTS` is the request, in the user's words if you have them.

A request that arrives mid-task has two possible fates: it derails the task, or
it is held in your context. Context is the thing that does not survive — this
machine's trace records 940 session exits and not one of them wrote a handoff,
so everything held in context on those 940 occasions was lost. This command is
the third option.

## When to file

**Always offer. File unprompted only when the request is unambiguously scoped
work.**

The error here is asymmetric and it is worth being explicit about which way.
Filing a stray remark costs a human one rejection; failing to file loses the
request entirely, which is the exact failure this command exists to stop. But
an agent that files every utterance produces a backlog nobody reads, and an
unread backlog is worse than none because it costs a reader time to discover
that. So:

- **File it, then say you did**: "add X", "we should do Y", "Z is broken" —
  anything with a verb and an object you could hand to another agent.
- **Offer, and wait**: musings, half-formed preferences, anything where you
  would have to invent the scope. "I wonder whether..." is not a work item.
- **Never file**: things the user is telling you to do *now*. Do those.

## Filing one

The tool writes the file. Do not hand-roll front matter and do not pick the id
yourself — a second writer of this format is how the format drifts.

```
backlog new --title "One line naming the defect or the change" \
            --evidence "What was observed, when, and how it reproduces" \
            --why "Why it matters" \
            --notes "Design questions, constraints, links"
```

New items are filed `proposed`, meaning *filed, not approved*. That is the
default and you should leave it alone: filing is not approving, and an agent
that files an item as `open` has approved its own work.

**Evidence is required and the tool refuses without it.** Evidence is what was
observed, when, and how to see it again. "The mail system is unreliable" is not
evidence; "on 2026-08-05 `operator inbox` returned empty for a mailbox that
`ls` showed held two files" is. Quote the user verbatim where the request *is*
the evidence — a first-hand report in their own words outlives your summary of
it.

`--spec` defaults to `none`. Set it to a path under `specs/` when the item
changes a specification, and add a `requirement:` line to the file afterwards
when you can name the exact requirement text. That is what makes the mapping
load-bearing rather than decorative: rename the requirement and the item goes
red, so somebody has to look at it again.

## The one case where you may work what you filed

You are working an approved item and you find a defect. You may not work an
unapproved item, and stopping to ask costs the session. The lawful move is to
file the defect naming the approved item it blocks:

```
backlog new --title "..." --evidence "..." --blocks 12
```

`backlog ready` will then list it, because item 12 is approved and this item is
what stands in its way. The exception is deliberately narrow — the blocked item
must itself be approved, so a chain cannot begin at something nobody approved —
and, more importantly, it is *recorded*. An agent with no lawful move does not
stop; it approves its own item and carries on, which repeals the gate while
appearing to honour it and leaves nothing behind that says so.

Use it for what genuinely blocks the work in hand. A defect you merely noticed
is a `proposed` item with no `blocks`, and it waits.

## After filing

1. `backlog check` — the item you just wrote must conform. `backlog new` runs
   this for you and tells you when it does not.
2. **Commit it.** An item that exists only in your working tree is in exactly
   the same danger as one that existed only in your context.
3. Tell the user in one line: the id, the title, and that it awaits their
   approval. Then go back to what you were doing.
