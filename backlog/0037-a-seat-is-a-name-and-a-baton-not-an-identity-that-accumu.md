---
id: 37
title: A seat is a name and a baton, not an identity that accumulates
status: proposed
opened: 2026-08-17
spec: none
---

## Evidence

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

## Notes

Design constraint that governs the whole thing: INV-AUTH. A seat's own past claims fed back into a later session as unattributed prose is backlog 0013's exact shape, with better disguise -- the seat would be quoting itself as authority. Anything delivered must arrive attributed and marked unverified, through mandate.py/vet_clause, the way an extension claim already does. Second constraint: the kernel has ~67 code lines of budget left (4033/4100), so this does not belong in operator_kernel. The extension system now has live hooks and a fleet host process (commits d321666, c11e7f9 in ~/repos/operator), and a seat journal is a plausible extension rather than kernel code -- but note detect_repo, the hook whose answers would reach a preamble, has no call site yet, which is the piece that would have to be built. Open questions: what is written (agent-authored notes vs derived facts), who writes it (the seat, or the supervisor observing it), how it is bounded so session 500 does not inherit 499 notes, and whether identity is per-instance or per-(instance, repository).
