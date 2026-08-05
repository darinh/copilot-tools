---
name: operator-backlog-scrum
description: Report what changed since the last check-in - commits, backlog movement, what is ready to work and what is waiting on the product owner. Use when the user asks what has been done lately, at the start of a working session, or before a handoff.
---

# Check in

```
backlog scrum           # report, and advance the watermark
backlog scrum --peek    # report without advancing it
```

"What has been done lately?" is a question agents answer badly, and the reason
is structural rather than a matter of effort: closed work is answerable from
`git log`, but open and in-flight work were answerable from nothing, and an
agent asked to summarise them reaches for its own context — which is a
re-summary of a re-summary and cannot be checked against anything.

`backlog scrum` reads git and the tracked backlog instead. Everything in the
report has a file or a commit behind it.

## The watermark

"Since the last check-in" needs a *durable* boundary. This one lives in the
per-project directory outside the repository, keyed on the primary checkout, so
every worktree of a project shares it and no session's death resets it. It is
deliberately not session state: session state is the artifact this machine has
measured not to survive.

**A plain `backlog scrum` advances the watermark — it consumes the period, like
reading a mailbox.** Use `--peek` when you want the report but the human has
not seen it yet, and in particular when you are drafting a handoff. Reporting a
period into a file nobody has read yet, and then marking it read, is how a week
of work goes unmentioned.

If the recorded commit no longer resolves — a rewritten history, a fresh clone
— the report says so and covers everything rather than silently covering
nothing. If the watermark cannot be written, the command fails loudly, because
a check-in that reports and does not advance repeats itself, and a repeated
report looks exactly like a quiet week.

## Reading the report to the user

Do not paste it. Read it and say, in a few lines:

1. **What shipped** — commits, in the user's terms rather than subject lines.
2. **What moved in the backlog** — filed, approved, closed.
3. **What is ready to work**, and what you propose to pick up next.
4. **What is waiting on them** — items awaiting approval, and any question
   blocking you. This is the part they can act on; lead with it if it is not
   empty.
5. **Anything under "Caveats on this report"** — those are the parts the tool
   could not verify. Never drop them; a report that quietly omits its own
   failures is the thing this command exists to replace.

## Ending the session on a completed item

The other half of the user's complaint is that sessions end wherever the
context ran out rather than at a shippable increment. So:

- Pick the next item from `backlog ready` — **not** from your own sense of
  what is important. That is what the gate is for.
- When an item is done, close it in the same commit that does the work: set
  `status`, `closed` and `commit`, and update the linked spec. A close landing
  separately from its fix is a window in which the backlog is wrong.
- Then check in again, and write the handoff. The handoff says what is in
  flight; the backlog says what is outstanding. They are not the same document
  and neither substitutes for the other.
