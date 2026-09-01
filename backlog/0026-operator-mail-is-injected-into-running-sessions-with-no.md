---
id: 26
title: Operator mail is injected into running sessions with no recipient readiness signal and no acknowledgement
status: proposed
opened: 2026-08-09
spec: none
---

## Evidence

Raised independently by all three seats of the 0025 review council on
2026-08-09 -- `gpt-5.6-sol`, `gemini-3.1-pro` and `grok-4.5`, each answering
the same question in parallel with no sight of the others, none of them asked
about this. Each arrived at it from a different direction:

- gemini: "the asymmetry of consent is the real root cause. Even within the
  *same* project, a live injection can derail a fragile reasoning task or sit
  stale in a terminal input buffer. The ultimate defense against context
  poisoning belongs to the recipient, not the sender."
- grok: "Live mid-task injection hurts same-project mail too... Scoping
  'queue instead' to cross-project alone is a category error; if you ever
  want queue-on-busy, key it off recipient readiness, not project inequality."
- sol: "Live 'delivered/read' has no recipient acknowledgment. Record it as
  injected/presented, not proven read."

The code agrees. `send_message()` in `copilot_operator.py` decides live versus
queued with `_can_receive_live(target)`, which asks whether the recipient's
process is *running* -- never whether it is busy, mid-turn, or willing.
`MUX.send_keys(target.session, ...)` then types the message into the session
with `enter=True` (`operator_mux.py:344`). There is no readiness signal, no
acknowledgement, and no way for a recipient to defer. `record_delivered()`
stamps the message the moment the keystrokes are sent, so `read_at` records
that text was injected, not that anything read it.

Measured on this machine the same day: 286 mail files, of which 284 are
`delivery: "live"`. 98.6% of all operator mail is injected directly into a
running session. Only 4 messages were ever queued.

Directly observed once, and the cause is NOT diagnosed: a message reached its
recipient days late, found by the human "sitting in the textbox" of a session
and submitted by hand. Since `enter=True` is sent, the simple explanation
(typed but not submitted) is refuted. Whatever the mechanism, the message was
neither delivered nor visibly undelivered, and both ends believed otherwise.

## Why it matters

Every mechanism 0025 considered is sender-side or label-side. None of them
gives the recipient a say, and the recipient is the one paying the cost.

The 98.6% figure is what makes this structural rather than theoretical. Live
injection is not an edge case in this system, it is how operator mail works.
An agent halfway through a delicate refactor, a reviewer mid-analysis, and an
agent sitting idle at a prompt are all treated identically, because the only
question asked is whether a process exists.

Three specific consequences:

- **No deferral.** A recipient cannot say "not now". The context cost is paid
  before relevance or timing can be judged, because judging either requires
  reading.
- **No acknowledgement.** The sender learns that keystrokes were written to a
  terminal. That is not the same as delivery, and the one observed failure is
  precisely a case where the two came apart -- with the record claiming
  success.
- **Project is the wrong axis.** 0025 approved labelling cross-project mail
  and explicitly rejected gating on it. If gating is ever right, the council's
  position is that it should key off recipient readiness. Nothing today
  expresses readiness.

This is the same shape this repository keeps finding: a mechanism that returns
a confident answer nobody can check. `delivery: "live"` and `read_at` both
read as proof of something they do not prove.

## Done when

The item names three directions and only the second is finishable on its own.
Split accordingly.

**The half that can ship alone (acknowledgement):**

- The mail record distinguishes *injected* from *acknowledged by the receiving
  agent*. `delivery: "live"` and `read_at` no longer assert readership that
  nothing observed.
- The 286 existing mail files still read after the change. Whatever the old
  fields meant, they are not silently reinterpreted as the new stronger claim --
  that would relabel 284 unproven deliveries as proven ones in a single commit.
- A message that is injected and never acted on is distinguishable, after the
  fact, from one that was read.

**The rest (readiness, queue-on-busy):** not finishable until designed, and the
design question below is unanswered. Do not approve those halves as one item.

## Not in scope

- Item 0025's affiliation and labelling work, which is finishable and should not
  be reopened here.
- Gating live delivery on whether the mail is cross-project. The 0025 council
  considered and rejected that axis explicitly.

## Risk

🟡 `copilot_operator.py::send_message` / `record_delivered` /
`_can_receive_live`, `operator_mux.py:344`. The acknowledgement half changes the
meaning of a recorded field, so existing records need a stated interpretation
rather than a migration that guesses. 98.6% of all mail is live-injected, so
anything that changes delivery affects effectively all of it.

## Needs a decision before this can be worked

- **Whether a busy agent is interruptible at all for urgent mail, and who
  decides which mail is urgent.** The item names this as unanswered, and it is
  load-bearing: a readiness gate with no override converts one failure mode into
  another, and an override with no rule is the current behaviour with extra
  steps.

## Open and unexplained

One message reached its recipient days late, found "sitting in the textbox" and
submitted by hand. `enter=True` is sent, so the simple explanation is refuted and
no other has been established. A readiness design that assumes injection works
except when the recipient is busy would be assuming away this observation.

## Notes

Filed out of the 0025 council, deliberately as a separate item rather than
folded into it. 0025 is finishable; this is a protocol change and would have
made it not so.

Not yet designed, and this item should not pretend otherwise. Directions the
council named, none decided and none costed:

1. A recipient-declared readiness signal -- a status file an instance writes
   when it is mid-task and clears at a prompt -- with `_can_receive_live()`
   consulting it. gemini's lockfile proposal.
2. Real acknowledgement: distinguish "injected" from "acknowledged by the
   receiving agent", and stop recording the first as if it were the second.
   sol's, and the cheapest of the three to make honest.
3. Queue-on-busy for all mail regardless of project, if 1 exists. grok's, and
   explicitly conditional on 1.

Sequencing note: 2 is independent of the others and is a correction to a claim
the data already makes falsely. It could ship alone.

Open question nobody answered: whether a busy agent should be *interruptible*
at all for genuinely urgent mail, and who decides which is which. A readiness
gate with no override converts one failure mode into another.

Depends on nothing in 0025, but should not be worked before it -- 0025
establishes affiliation, and any readiness design will want to know who is
asking.
