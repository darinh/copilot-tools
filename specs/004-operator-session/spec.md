# Feature Specification: Operator Session, Assignment, and Liveness

**Feature Branch**: `feat/operator-session`

**Created**: 2026-08-05

**Status**: **In progress**

## Summary

The generated `AGENTS.md` is ~6,300 words. It gets smaller by moving what it
says into places that can enforce it: assignment resolved by `operator` before
the agent's first token, procedures loaded as skills when the work is reached,
and every checkable rule turned into a check.

The measurement that makes this worth doing rather than tidy: Gloaguen et al.,
*Evaluating AGENTS.md* (arXiv:2602.11988, ETH Zurich / LogicStar, Feb 2026)
found that instructions in a context file **are** acted on — a tool named in
the file is used ~1.6 times per instance against under 0.01 when unnamed —
while context files cost **20–23% more per task** in steps and inference, and
repository overviews change nothing measurable. Their corpus averaged 641 words
per file.

So an always-loaded line is not documentation. It is live weight the agent will
act on and pay for on every task, and narrative content is cost with no
measured benefit.

## Why the file is long

Not verbosity. The spec's diagnosis:

> Most of the prose exists because state is keyed one axis short of where it is
> used, and the agent is then asked, in English, to compensate.

The handoff is the worked example. It is written in the first person — *my*
worktree, *I* claimed this — so it is instance-scoped by construction, but it
is stored at a project key. Everything built on top of that mismatch
(`superseded/`, the lock, the author stamps, the never-prune promise, ~400
words of prose teaching agents to tell two cases apart) is scaffolding holding
up a type error. Re-keying deletes all of it.

The generalisation is the reusable part: **when a rule needs a lot of prose to
explain when it applies, check whether the state underneath it is keyed one
axis short.**

## Keying model

| State | Keyed by today | Keyed by after this |
|---|---|---|
| Project id | repo root | repo root (unchanged) |
| Handoff | project | **instance** |
| Session log | session db | **instance + session** |
| Work-item claim | `agent_id` UNIQUE | **work item**, owner recorded |
| Backlog item | project | **project + subproject** |
| Worktree | branch | **work item** (1:1) |

## Requirements

### FR-1 — Handoff is keyed by instance

`~/.operator/projects/{projectId}/handoff/{instance}.md`. One writer per file.
No race, no `superseded/`, no author stamp, no destructive read.

Existing handoffs are **moved, not deleted**. A banked handoff is dropped
context; the migration that discards one to tidy up is the failure this whole
feature exists to stop.

### FR-2 — Assignment is resolved before the session starts

`operator session start --instance <name> [--project <sub>]` resolves, in
order: a live claim held by this instance (resume), a claim whose owner is
provably dead (offer, oldest first), otherwise no assignment.

The agent never discovers its own worktree, because both discovery routes are
silently wrong:

- `git rev-parse --show-toplevel` inside a worktree returns the **worktree**.
  Using it for project identity mints a duplicate project id and splits state.
- "walk up until you find `AGENTS.md`" finds the **nearest** file — in a
  monorepo a subproject's, inside a worktree the worktree's own tracked copy.

Both resolve correctly in code, identically every time. That is the whole
argument for moving it out of the instruction file.

### FR-3 — A claim is reclaimable only when its owner is provably gone

Cascade, cheapest first. The first three are conclusive; the fourth is not.

1. `boot_id` differs from current → **DEAD**. The unplanned-reboot case, and it
   needs no timeout: nothing from the previous boot is running.
2. PID absent, or present with a different start time → **DEAD**. The
   start-time comparison is what makes this safe after PID reuse. Asked before
   the mux session because it is a syscall where that is a subprocess spawn;
   both can only conclude DEAD, so the order changes cost, never a verdict.
3. Mux session absent → **DEAD**. Direct and exact, because every agent runs
   inside one.
4. Heartbeat older than `staleAfter` with 1–3 inconclusive → **STALE**. Report;
   never auto-steal. This combination means something unusual — a hung process,
   a clock problem — and guessing is how two agents end up in one tree.

### FR-4 — Reclaim never touches worktree contents

Clean → reassign as-is. Uncommitted changes → commit to
`wip/{item}-{deadInstance}` **first**, then reassign and name that branch to the
new owner. Never `stash`, `reset`, `clean`, `checkout`, `restore`, or delete.

A crashed agent's uncommitted work is the most expensive thing in the tree. The
rule is the direct lesson of the incident in `docs/rationale.md`, where a
reviewer subagent's `git stash` destroyed 454 lines and the stash was dropped —
recoverable only because the work had been `git add`-ed, so the blobs survived
as dangling objects.

### FR-5 — Session end is one call

`operator session end` writes the instance handoff, releases the work-item
claim, and closes the session log **atomically**. Three calls is three chances
to end a session having done one of them.

### FR-6 — The managed block carries only what cannot be enforced

Every candidate line is exactly one of:

- a **guardrail** that fires at a moment the agent would not recognise as
  procedural, *and* that no check can catch → stays;
- a **procedure** → moves to a skill, loaded when the work is reached;
- **anything else** → becomes a check, and the sentence is deleted.

A skill cannot cover the guardrail class, because loading a skill requires
already knowing you need it. "Commit before delegating" fires when the agent
thinks *I'll have a reviewer glance at this*, not when it thinks *time to
follow the delegation procedure*.

**A rule is never deleted in a commit that does not add its check.** Otherwise
there is a window in which it is neither written nor enforced, and nothing in
the suite would show it.

### FR-7 — Project content is appendable and preserved

The managed block is delimited; everything outside it survives regeneration
byte-for-byte, and that is **tested**, not merely promised in a docstring.

The word budget therefore applies to **the managed block, not the file**.
Budgeting the file would fail the build the first time a project appended its
own test commands.

### FR-8 — Generation is budgeted and defaults off

Generation **errors** above budget rather than warning: without a hard number
the file only grows, because every incident adds a paragraph and nothing
removes one. Feature flags default **off** — every enabled section is a live
requirement. One platform's commands, chosen from the host.

### FR-9 — Nested files are additive only

Claude Code concatenates parent and child; Codex lets the nearer file win. Only
additive content behaves identically under both, so a subproject file never
contradicts a root rule.

`AGENTS.override.md` is **not** used: it is a Codex CLI mechanism that Claude
Code does not implement, and building the monorepo story on it would put a
harness dependency inside the thing being made harness-agnostic.

## Decisions

| # | Decision | Why |
|---|---|---|
| D1 | State stays at `~/.operator`, not `~/.agent-tools` | Already harness-neutral — it names the tool, not the harness. A third move of live project identity in one day risks the one file whose loss costs every project its id. |
| D2 | Harness-coupled renaming deferred | `copilot_operator.py`, `COPILOT_OPERATOR_HOME`, the repo name. A whole-tree diff bundled with functional work hides bugs in it. |
| D3 | Marker becomes `<!-- BEGIN operator:managed -->` | Forced, since the template ships a new marker either way. Migration mandatory: a writer knowing only the new marker **appends** a second block rather than replacing, leaving two contradictory rule sets. |
| D4 | `staleAfter` 30 min, configurable, never the sole signal | Spec §9.1. |
| D5 | Slugs, not `NNN-`, for subproject specs | A sequential counter is shared mutable state; two agents specifying on one day both get `007`. |
| D6 | One work item per agent | Primary key on the item. Batching is "yes, later" — allowing it now means designing release and reclaim for a set nobody has needed. |
| D7 | `peer-agents` replaces `operator-agents`; `operator-backlog-*` stay | Two skills on one subject is the drift this repo keeps paying for. The backlog slash-commands are a different artifact and were specifically requested. |
| D8 | Budget the managed block, not the file | See FR-7. |
| D9 | Enforcement-first (FR-6) | The user's requirement: control forced by tooling, not by prose. |
| D10 | Never delete a rule without its check in the same commit | See FR-6. |
| D11 | Build/test/lint commands leave the managed block | Named as the thing projects append. Generating them puts operator and the user in a fight over the same three lines that regeneration wins silently — and nothing reads them back today. |

## Out of scope

- Renaming the project or its state directory (D1, D2).
- Batching several work items to one agent (D6).

## Known gaps, stated rather than faked

- **Monorepo support ships unexercised against a real monorepo.** This
  repository is not one, so owned-path and contract checks are covered by
  synthetic fixtures only.
- **Enforcement reach is bounded by the harness.** Copilot CLI extensions load
  only in experimental mode (measured, CLI 1.0.77 — see `setup_tools.py`).
  Where no check is reachable, the sentence stays in the file and says so. A
  guard that silently never fires is worse than a written rule, because it
  reads as coverage.
