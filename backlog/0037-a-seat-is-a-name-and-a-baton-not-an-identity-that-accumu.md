---
id: 37
title: A seat is a name and a baton, not an identity that accumulates
status: proposed
opened: 2026-08-17
spec: none
---

## Evidence

A design proposal now exists and is the place to argue with this item:
`~/repos/operator/docs/seat-identity.md` (commit 0a6ba8e). It is a proposal
only — nothing is built, and it records the objection that may kill the idea
alongside the case for it.

The request, in the user's words (2026-08-17):

> "What I'm really interested in is being able to use agents that have
> persistent memory across sessions."

and, when asked to sharpen it:

> "I don't mean to say it's the only thing I care about. I want agents who
> really are a specific identity / seat and not just a session with a handoff."

What a seat actually is today, read out of the code rather than inferred:

* `operator_kernel/instance.py` — `Instance(display_name)` is a name plus a set
  of marker and state files: pid, loop pid, session number, restart/stop/detach
  markers, ownership token, and two failure streaks. Every one of them is
  mechanical and every one is about the *current* run. Nothing on the class
  holds anything the seat has learned, done, or is like.
* `operator_kernel/preamble.py::build_preamble` — what a launching session is
  told about itself is two sentences: "(4) You are the @{agent} agent" and
  "(5) Operator instance: {display_name}". The rest of the preamble is
  mechanism (it relaunches, nobody is watching) and the authority clause.
* The handoff is a **baton, not a memory, by design.** The preamble instructs:
  "The reader is the one who deletes a handoff, so delete it once you have
  taken in its contents." One file per instance, written by the previous
  session, consumed and removed by the next.

How to see it:

* `ls ~/.operator/projects/b62cff6b-6838-460f-9f3b-91a5c3ee270e/` on this
  machine shows exactly one directory, `handoff/`, holding `operator.md` and
  `operator.prev.md`. That is the entire durable record of that seat.
* `operator list` shows `prism` at session #226 and `copilot-tools` at #250.
  Neither retains anything from sessions 1..N-1 beyond whatever the immediately
  preceding session chose to write into one file that its successor then
  deletes.
* `~/.operator/trace.jsonl` holds the mechanical history (496 `session_exit`
  records readable in the recent tail alone), but it records what the
  *supervisor observed* — exits, admissions, markers — never what the seat
  learned.

So the seat's identity is a directory name plus the last session's note. A seat
that has run 226 times can answer "what was I doing an hour ago" and cannot
answer "have I seen this before", "what did I decide about X", or "what am I
like" — the questions that distinguish an identity from a process with a
resume file.

Related capability that exists and does not close this: the Copilot CLI's own
`store_memory` has scopes `repository` and `user` only. There is no seat or
instance scope, so two seats working the same repository share one memory and a
seat working two repositories has none of its own.

## Why it matters

The handoff is designed to be consumed and deleted, so a seat's whole durable self is the last session's note. Everything earlier is gone. That makes each session a stranger wearing the seat's name: it cannot tell whether it has already tried an approach, what it concluded about a subsystem, or what it is habitually wrong about. The cost lands hardest exactly where this fleet is weakest -- repeated work, repeated mistakes, and the recurring pattern in this repository's own history of a session rediscovering a defect a predecessor already understood.

## Done when

- A session can read what earlier sessions of the same seat recorded, without
  that content having passed through a handoff that its reader deletes.
- Everything it reads arrives **attributed and marked unverified**, through
  `mandate.py`/`vet_clause`, the way an extension claim already does. A seat's
  own past claims fed back as unattributed prose is item 0013's exact shape with
  better disguise -- the seat quoting itself as authority.
- It is bounded, by a stated rule, so session 500 does not inherit 499 notes.
  What the rule is matters less than that a stranger can predict what a session
  will be shown.
- It does not live in `operator_kernel`. Item 0038 measures 11 total lines of
  headroom, and this is not supervision.
- The seat that is *running* is told the mechanism exists. See the update below:
  this is the half that is currently missing, and without it the substrate is
  reachable only by an agent who already knows it is there.

## Not in scope

- Deriving facts about a seat from the trace. The trace records what the
  supervisor observed -- exits, admissions, markers -- and a derived-facts
  channel is a different item from an authored one.
- Anything that makes a seat's past claims *authoritative*. See INV-AUTH above.

## Risk

🔴 by content rather than by code. This channel feeds text into every future
session of a seat, and the failure mode is an agent acting on its predecessor's
unverified belief as if it were established. `docs/seat-identity.md` records the
objection that may kill the idea; it should be answered rather than routed
around.

## Needs a decision before this can be worked

The item's four open questions are still open and all four change what gets
built:

- **What is written** -- agent-authored notes, or facts derived from observation.
- **Who writes it** -- the seat, or the supervisor observing it.
- **How it is bounded** -- so session 500 does not inherit 499 notes.
- **Whether identity is per-instance or per-(instance, repository).** The seat
  named `operator` works in at least two repositories today, so this is not
  hypothetical.

## Substantially built since this was filed — 2026-08-31

The evidence above says "nothing is built" and that
`~/.operator/projects/b62cff6b-.../` holds exactly one directory. Both are now
out of date. In `~/repos/operator`:

* `operator_memory/journal.py` and `operator_cli/seat.py` exist -- `operator-seat
  --instance NAME remember|recall|forget`.
* `operator_kernel/paths.py:183` `project_journal_file` and `:207`
  `seat_has_journal` resolve `project_dir(guid)/journal/{instance}.jsonl`.
* `operator_kernel/preamble.py` takes `has_journal` and tells a session the
  command exists. Its comment at :161 records a defect already fixed: both
  clauses used to hang off `has_journal`, so a seat with an empty journal was
  never told the command existed and the journal could only ever start by
  accident.
* The seat directory now holds three entries, not one:

      handoff/operator.md        3,028 bytes
      handoff/operator.prev.md   6,087 bytes
      journal/operator.jsonl     3,068 bytes   <- 5 entries
      work.db                   28,672 bytes

  `operator-seat --instance operator recall` returns those five, each prefixed
  `[seat operator, session 6, 2026-08-31, unverified]` -- which is the INV-AUTH
  requirement above, delivered.

**And it does not reach this fleet.** `copilot_operator.py` -- the process that
actually launches these sessions -- contains the string `journal` **zero times**:

    $ (Select-String copilot_operator.py -Pattern "journal").Count
    0

Only `operator_kernel/preamble.py` advertises the command, and that kernel
supervises nothing today. So no session is ever told the journal exists, and the
five entries in it were written by hand by a seat that went looking. That is the
same shape as item 0013 (fixed in the kernel, live in the supervisor) and item
0015 (skills correct in the repository, absent from the machine), and it is now
the third instance of it.

**What this does to the item.** It makes the four open questions concrete rather
than answering them: there is now a working substrate to argue about, and
arguing about a built thing is easier than arguing about a proposal. It does not
make wiring that substrate into the live supervisor the remaining work.
Advertising the agent-authored journal to every session *is* an answer to what is
written and who writes it, and those are two of the four questions. If the owner
chooses this design, the wiring is small and well understood; if he prefers
derived facts, supervisor-authored state, or the objection in
`docs/seat-identity.md` that may kill the idea, the built substrate is a
prototype rather than a decision. `detect_repo`, the hook whose answers would
reach a preamble, still has no call site
(`docs/extensions.md:414`: "**no call site** -- nothing composes its answer into
a preamble").

## Notes

Design constraint that governs the whole thing: INV-AUTH. A seat's own past claims fed back into a later session as unattributed prose is backlog 0013's exact shape, with better disguise -- the seat would be quoting itself as authority. Anything delivered must arrive attributed and marked unverified, through mandate.py/vet_clause, the way an extension claim already does. Second constraint: the kernel has ~67 code lines of budget left (4033/4100), so this does not belong in operator_kernel. The extension system now has live hooks and a fleet host process (commits d321666, c11e7f9 in ~/repos/operator), and a seat journal is a plausible extension rather than kernel code -- but note detect_repo, the hook whose answers would reach a preamble, has no call site yet, which is the piece that would have to be built. Open questions: what is written (agent-authored notes vs derived facts), who writes it (the seat, or the supervisor observing it), how it is bounded so session 500 does not inherit 499 notes, and whether identity is per-instance or per-(instance, repository).
