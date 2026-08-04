---
id: 5
title: Add /operator-backlog-* slash commands so requests become work items instead of interrupts
status: open
opened: 2026-08-05
spec: none
---

## Evidence

Recorded as it happened, 2026-08-05, in this session.

While the backlog merge was finishing, the user sent a message containing two
new feature requests and closing with:

> "The problem is that I have agents work on projects in a way that doesn't
> result in a shippable product at the end of each working session. They also
> struggle to tell me what has been done lately, and they sometimes get
> distracted when I pass in a new work item for them to work on later, sort of
> like what I'm doing to you right now... randomizing you."

That is a first-hand reproduction, not a report. The request arrived mid-merge
with no mechanism to hold it, so its survival depended entirely on the agent
choosing to write it down before its context filled.

Every prior instance of that pattern in this repository was lost.
`~/.operator/trace.jsonl` records 940 `session_exit` events and **every one**
has `restart=False`, meaning no session has ever written a handoff (item 0001).
Anything a user said mid-session that was not committed died with the process.

Measured the same day, independently: a full-suite run was reported green from
a worktree whose `README.md` had not been updated, and the omission surfaced
only at merge (fixed in 77a5b75). Agent self-report of state is unreliable even
*within* one session; a file on disk is not.

## Why it matters

Three failures the user describes share one cause -- **there is no durable
queue between the human and the agent**:

1. **Randomization.** A request delivered mid-task must either derail the
   current task or be held in context, and context is precisely the thing that
   does not survive. There is no third option today.
2. **No shippable increment.** With no approved, prioritized queue an agent
   picks its own next task, so a session ends wherever its context ran out
   rather than at a completed item.
3. **No answer to "what happened lately".** Closed work is answerable from
   `git log`. Open and in-flight work are answerable from nothing.

Items 0001-0004 delivered the *storage* half of this. This item is the
*workflow* half: the commands that put work into the queue and take it out.

## Notes

Requested shape, from the user, modelled on spec-kit's `/speckit-*` commands.
Naming convention `/{namespace}-{feature}-{command}`:

- `/operator-backlog-newitem` -- turn a request into a backlog item.
- `/operator-backlog-refinement` -- refine an unready item until an agent can
  consume it.
- `/operator-backlog-scrum` -- report what changed since the previous `-scrum`.

Stated intent: *"I want agents to ONLY work on prioritized work items in the
backlog that have been refined and approved by the product owner (me)."*

Design questions raised in reply, to be settled during refinement rather than
assumed here:

- **Auto-filing is the risky part.** "Whenever a user asks for a feature, file
  an item" makes an agent classify every utterance, and the error is
  asymmetric: filing a stray remark makes junk a human must weed, while failing
  to file reproduces exactly the loss this item exists to stop. Preference: the
  agent always *offers*, and files unprompted only when the request is
  unambiguously scoped work.
- **The approval gate needs a legal escape hatch.** An agent permitted to touch
  only approved items has no lawful move when it finds a defect mid-task. Given
  none it will either stall or file a self-approved item to justify continuing
  -- which silently repeals the gate while appearing to honour it.
- **`-scrum` needs a persisted watermark.** "Since the last check-in" is
  meaningless if the timestamp lives in session state, which is the one
  artifact already known not to survive.
- **The status vocabulary is short by at least one value.** `open` / `closed` /
  `rejected` cannot express "filed but not yet approved", so the gate is not
  expressible until it can. `backlog_tool` owns that vocabulary in exactly one
  place by design, which is where the addition goes.
