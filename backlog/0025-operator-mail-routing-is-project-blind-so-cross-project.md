---
id: 25
title: Operator mail routing is project-blind, so cross-project messages land mid-task with no affiliation, age or opt-out
status: proposed
opened: 2026-08-09
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

- **Staleness.** Live delivery types into a session; if that session is not
  at a prompt, the text sits in the input box until a human notices. One such
  message here sat for an unknown period and arrived days late, describing a
  state that had moved on. The recipient cannot tell a stale message from a
  fresh one -- `sent_at` is recorded but never shown at the point of delivery.

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
