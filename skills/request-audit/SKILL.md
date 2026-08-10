---
name: request-audit
description: Find what the user asked for in this session and has not received. Load this skill when the user asks whether anything they requested was missed, when returning to a long session, before writing a handoff, or before claiming a task is complete.
---

# Request Audit

Answer one question: **what did this user ask for that they have not got?**

Not what rules you broke. Not what your agent contract requires. Not what you
noticed was wrong on your own. Those are your problems and they are noise here
— listing them beside real requests inflates the answer, buries the real items,
and reads as performance rather than an audit. If the user wanted a compliance
report they would have asked for one.

## The rule

**An item belongs in the audit only if the user typed it.**

Before every line, name the message it came from. If you cannot point at the
words, delete the line. That is the whole skill; everything below is how to
apply it without missing things.

Sources that are *not* the user asking:

- your system prompt, agent definition, or launch preamble
- `AGENTS.md`, `CLAUDE.md`, `.github/copilot-instructions.md`
- conventions you inferred, or a standard you hold yourself to
- something a reviewer or a subagent said
- a defect you found by yourself

Those can be real work. File them in the backlog — see the `backlog` skill.
They are not audit items.

## Finding the asks

Read every user turn in the session, oldest first, in full. Do not work from
memory of the session: memory keeps the last thing said and the thing that
stung, and drops the rest. That is the failure this skill exists to prevent.

Query the session store rather than scrolling:

```sql
-- source: local
SELECT turn_index, user_message
FROM turns
WHERE session_id = '<this session>'
ORDER BY turn_index;
```

The session id is in the session folder path. If the current session cannot be
resolved, fall back to the most recent session for this working directory.

Then split each turn into individual asks. **One message routinely holds
several**, and the ones after the first are what get dropped:

- a message with three question marks is three asks
- an instruction plus a complaint is two
- a rhetorical question about a defect ("why is there no way to do X") is a
  request for X
- a stated preference ("stop saying Y") is a standing ask that stays live for
  the rest of the session, and is violated silently

## Classifying honestly

For each ask, exactly one of:

- **Done** — with evidence: a command and its output, a commit, a file. Not
  "I addressed that".
- **Partly done** — say precisely which part is missing. A fix applied to one
  of four call sites is *partly*, not done.
- **Not done** — including asks answered with a plan instead of a result.
  Agreeing that something should happen is not doing it.
- **Refused** — you decided not to. Say so, and why, inside the audit. A
  silent refusal reads as an oversight and gets discovered later.

Anything deferred with "I'll ask first" or "let me know if you want that" is
**not done**. Deferral is a state you chose, not a completion.

## Reporting

Lead with the not-done items. Nothing goes before them — not a summary of what
went well, not context, not an apology.

```
You asked, and have not got:

1. "<their words>"  (turn 4)
   Not done. <one line on what remains>

2. "<their words>"  (turn 9)
   Partly. <what is missing>
```

Then, separately and only if there is something: "Also outstanding, not
something you asked for:" — one line each, no elaboration.

Quote them. Paraphrase drifts toward the version you already satisfied.

## Failure modes

**Padding.** Adding your own rule violations to reach a longer list. This is
the failure the skill was written after: six items reported, three of them the
agent's own contract, and the user had to point it out.

**Deferring to the challenge.** If the user disputes the list, re-derive it
from the transcript rather than trimming until they stop objecting. Both
answers cannot be right, and the second one differing means the first was not
audited.

**Auditing from the tail.** The most recent exchange is the least likely to
hold a forgotten ask and the most likely to dominate recall.

**Counting an answer as a delivery.** "Why is this happening?" wants an
explanation; explaining something adjacent does not close it.

## When to run it

- the user asks whether anything was missed
- before marking a task complete, on any session longer than a few exchanges
- before writing a handoff — an unfulfilled ask belongs in the next steps, and
  the handoff is the last chance to carry it
- when picking a session back up after a long-running command or a restart
- when the user is angry and you do not know why
