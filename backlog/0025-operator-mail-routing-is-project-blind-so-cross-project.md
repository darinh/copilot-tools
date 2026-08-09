---
id: 25
title: Operator mail routing is project-blind, so cross-project messages land mid-task with no affiliation, age or opt-out
status: closed
opened: 2026-08-09
closed: 2026-08-09
commit: 608799b5bd5f999935b35d29b6b52ba8615da8d0
spec: none
---

## Evidence

Measured on this machine, 2026-08-09, over `~/.operator/messages/**/*.json`
(286 files, the complete local mail record):

    mail files: 286   distinct senders: 5   distinct recipients: 5
    distinct ordered pairs: 10

Five of the ten ordered pairs are cross-project:

    4  discord-invite-manager -> copilot-tools
    4  copilot-tools -> discord-invite-manager
    4  scripts -> copilot-tools
    4  copilot-tools -> scripts

16 messages, 34,212 characters, roughly 8,500 tokens, mean 2,138 characters.
The longest single message is 4,122 characters. For comparison, the whole
managed conventions block that every agent in this repository is given was
cut to a 700-word budget precisely because context is scarce.

Nothing in the code makes any of this project-aware. `send_message()` in
`copilot_operator.py:3736` resolves the recipient with `Instance(recipient)`
and gates only on `_instance_is_known(target)` -- existence, not affiliation.
There is no project field on a message: `operator_mail.new_message(sender,
display_name, target.id, text)` records `from`, `to`, `to_id`, `sent_at` and
`text`, and `~/.operator/instances` does not exist, so an instance carries no
recorded project at all. `--force` will queue to a name that has never
started. The mailbox is per-instance and the restart signal is per-instance,
but neither is per-project.

Two first-hand observations from the session that filed this item, both
costs actually paid rather than predicted:

1. A `discord-invite-manager` message about that project's Rollup
   content-hash collision (4,122 chars of another repository's debugging
   narrative) was delivered into a `copilot-tools` session mid-task. The
   human's verdict, verbatim: "you can ignore that message from the discord
   invite manager. i think the message is stale. i connected and saw the
   message sitting in the textbox to send and i sent it myself. i dont know
   how long it had been there." The message was read, reasoned about, and
   then discarded on the human's instruction.

2. A `scripts` message the same week ended "No reply needed." It was still
   read in full first, because whether a reply is needed is only knowable
   after reading.

What is NOT demonstrated, and must not be read as demonstrated: that any of
this changed an outcome. No wrong action has been traced to a cross-project
message. The measured cost is attention and context; the productivity and
context-poisoning harms are a plausible mechanism, not a diagnosis.

## Why it matters

The concern is not that agents talk. Cross-project mail has demonstrably
produced good work -- the `scripts` exchange above corrected a defect in this
repository, and 0011 shipped because of it. Two of the four cross-project
threads changed code here for the better.

The concern is that the routing is unconstrained in a system where the
recipient cannot decline. A message is injected into a live session's context
whether or not it is relevant, whether or not it is current, and whether or
not the recipient has capacity. Three specific exposures:

- **Staleness.** One cross-project message reached its recipient days late:
  the human found it "sitting in the textbox" of a session and submitted it
  by hand. That the delay happened is observed. **Why it happened is not
  diagnosed, and an earlier draft of this item got it wrong.** That draft
  asserted that live delivery types text without submitting it; it does not.
  `OperatorMux.send_keys()` in `operator_mux.py:344` takes `enter: bool =
  True` and `send_message()` calls it without overriding that, so the Enter
  is sent. The remaining candidates -- a TUI that discards the submit while
  the agent is mid-turn, a partial write, or something else entirely -- are
  untested. Whoever picks this up should reproduce the incident before
  building anything that assumes a cause.

  What *is* demonstrated regardless of cause: `render_line()` puts no
  timestamp on the delivered line, so a recipient reading a message cannot
  tell whether it was sent a minute or a week ago. `sent_at` is recorded --
  set in `new_message()` before the live/queue split, so it is genuinely send
  time -- and simply never shown on the live path.

- **Context cost.** ~8,500 tokens of another project's narrative landed in
  this project's sessions. That is spent before the recipient can judge
  relevance, because judging relevance requires reading.

- **No affiliation in the data.** Because a message carries no project, a
  recipient cannot filter, a viewer cannot separate in-project from
  cross-project threads, and this item's own evidence had to be reconstructed
  by pattern-matching instance names. The conversation log being built under
  `specs/005-conversation-log` will inherit exactly this gap: the human asked
  to see "conversations between agents somewhat separately", and the store
  cannot currently say which project an agent-to-agent thread belongs to.

The failure this guards against is the one this repository keeps finding in
other guises: a mechanism that returns a confident answer nobody can check.
An agent that acts on a stale cross-project message produces work that looks
correct and is founded on a state that no longer exists.

## Notes

Explicitly NOT proposing a ban. The human's position, verbatim: "i am okay
with agents reqching out to anyonr, but am concerned how the randomization
may affect productivity and context poisoning."

Investigate, then have a review council refine and approve a course of
action. Candidate directions, in rough order of cost, none of them decided:

1. Record affiliation. Stamp each message with the sender's project id and
   cwd at send time. Cheap, additive, and unblocks every other option --
   including the conversation log's "agent-to-agent, separately" view.
2. Surface staleness at delivery. Render the age of a message in the
   delivered line so a recipient can weigh it before reading.
3. Mark cross-project mail as such in the delivered line, so the recipient
   knows the cost before paying it.
4. Same-project by default, cross-project on an explicit flag
   (`--cross-project`), refused otherwise with the reason. Preserves reach,
   removes accident.
5. Queue rather than inject cross-project mail, so it is read between tasks
   instead of mid-turn.
6. Do nothing, and record that the measured cost was judged acceptable.

Option 1 is a prerequisite for 3, 4 and 5, and is the only one that is
strictly additive to the data. Option 6 is a legitimate outcome and must stay
on the table, or the council is not a decision.

## Council decision — 2026-08-09

Three seats, three model families, each asked the identical question in
parallel with no sight of the others: `gpt-5.6-sol` (xhigh), `gemini-3.1-pro`
(high), `grok-4.5` (high). The human delegated the decision: *"whatever the
review council approves / decides is fine."*

| Option | sol | gemini | grok | Verdict |
|---|---|---|---|---|
| 1 — record affiliation | approve | approve | approve | **APPROVED, unanimous** |
| 2 — surface staleness | approve* | silent | approve | **APPROVED** |
| 3 — mark cross-project | approve | silent | approve | **APPROVED** |
| 4 — refuse without a flag | reject | approve | reject | **REJECTED, 2–1** |
| 5 — force-queue cross-project | reject | approve | reject | **REJECTED, 2–1** |
| 6 — do nothing | reject | reject | reject | **REJECTED, unanimous** |

\* approved with a modification that changes the design; see below.

### What is approved, and what "done" means

**1. Affiliation, recorded as nullable and never inferred.** `new_message()`
gains an origin and a destination: the sender's cwd and resolved project id,
and the recipient's, each with an explicit status when unknown
(`catalog-missing`, `catalog-unreadable`, `no-entry`, `unplaceable`). Filled
at send time from the `operator send` process's own cwd resolved through the
primary checkout and the project catalog. On any failure the field is `null`
and **the send still succeeds** — affiliation is metadata, not a gate.

**2. Staleness — as an absolute timestamp, not an age.** This is sol's
modification and it overrides the item's own proposal. A computed age is
written once, at delivery, and a delivered line that says "just now" then sits
unread *stays* saying "just now" — the rendered age freezes at exactly the
moment the failure begins. `render_line()` therefore carries the immutable
`sent_at`; an age may supplement it where it is computed at read time
(`inbox`, history), never replace it. Malformed or missing renders as
`sent time unknown`.

**3. Tri-state relationship, shown before the body.** `SAME-PROJECT`,
`CROSS-PROJECT`, or `PROJECT UNKNOWN` — the third is a first-class state, not
a blank. Classification happens only when *both* ids are known. A sender-side
note on stderr for a known cross-project send, non-blocking, exit 0.

### What is rejected, and why

**4 and 5 fail on evidence, not on principle.** No wrong outcome has been
traced to a cross-project message, and two of the four threads improved this
repository. Refusing or delaying delivery on that record would spend a real
cost against a harm nobody has demonstrated. Grok named the specific danger:
a hard refuse is this repository's own defect class — a check returning a
confident wrong answer — and unknown affiliation makes the refusal unreliable
in exactly the cases it would matter. Sol added that queueing does not even
solve the stated problem: the full body still enters the next session's
context, so the token cost is deferred, not avoided.

Both remain reopenable. The trigger is named: measure known-cross live
volume and stale-discard incidents after this ships, and revisit if they stay
costly.

### Corrections the council forced on this item

- The staleness mechanism was asserted and is not diagnosed. Grok checked
  `send_keys` and found `enter=True`; the Evidence section above now says so.
- **A message does not have one project — it has an origin and a delivery
  context, and they can differ, including in time** (sol). The conversation
  log under `specs/005-conversation-log` has a single `project` column and
  therefore *cannot* represent an agent-to-agent thread truthfully. It must
  store both endpoints and the tri-state.
- `--from` is self-asserted. This is command provenance, not authenticated
  identity, and nothing downstream may treat it as the latter.
- Live delivery records that keystrokes were *injected*, not that anything was
  read. `read_at` is not evidence of reading.

### The finding all three seats reached independently

Project affiliation is not the root cause. **The recipient has no way to
decline or defer, and that is equally true of same-project mail.** Sol,
gemini and grok each arrived at this unprompted, from different directions:
gemini proposed a busy lockfile, grok called scoping queue-on-busy to
cross-project "a category error", sol observed there is no acknowledgement in
the protocol at all. Three independent seats converging on the same unasked
question is the strongest signal this exercise produced.

It is deliberately **not** folded into this item — it is a larger design and
smuggling it in here would be how 0025 stops being finishable. Filed
separately as **0026**.
