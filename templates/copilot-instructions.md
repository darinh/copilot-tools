# User Workflow Conventions

Not rendered. `render()` replaces this preamble with a generated header, and replaces the Project
Configuration System section with this project's resolved ids and paths. Every other section is
copied through, gated on its feature flag.

---

## Git Worktrees — Always

**All work happens in a worktree. Never edit the primary checkout.** Use `operator worktree
new|finish`.

- **Leave worktrees you did not create alone.** Nothing outside tells an idle tree from an occupied
  one.

By hand: the `worktrees` skill.

---

## Scratch Files — Never in the Checkout

**Probes, reproductions, scratch copies and fixtures go in a temp directory, never in a checkout.**

- **Tell your subagents the same thing, by name.** No tool can check a prompt you wrote.
- **An explanation that fits the evidence is not the explanation.** Reproduce the mechanism before
  explaining the artifact.

Why: `docs/rationale.md`.

---

## Handing a Worktree to a Subagent

**Commit before you delegate. Staging is not enough.** Point reviewers at `git diff main...HEAD`.

- **Forbid the mutating git verbs by name** — `stash`, `checkout`, `reset`, `clean`, `restore`,
  `rebase`. Git plumbing writes no new files.
- **Verify the worktree before you read the findings.** A reviewer once destroyed 454 lines and
  mentioned it in passing.

---

## Project Configuration System

Replaced at render time. Nothing written here ships.

---

## Session Handoff Protocol

*Enabled by feature flag: `session-handoff`*

Open and close every session with `operator session start|end`.

- **Write it proactively.** Noticing that context has got heavy is a judgement no tool can make.
- **Never write the handoff file by hand** — it destroys an unread one and raises no restart marker.

---

## Session History

*Enabled by feature flag: `session-history`*

`operator session start|end` records it. No schema to paste, no table to create.

- **A session left open is recorded as abandoned, not lost.** Say what happened rather than
  reopening it quietly.

---

## Parallel Agents

*Enabled by feature flag: `parallel-agents`*

Claim before you edit: `operator work request|release|list`.

- **A refused claim is an answer.** Take other ready work; never wait, never take a claim a peer
  holds.

---

## Tracked Backlog

*Enabled by feature flag: `tracked-backlog`*

**Open work belongs in the repository, not in a handoff.** `operator backlog ready|close`; format in
the `backlog` skill.

- **Evidence is what was measured** — a command and its output, a mutation that ran green. Without
  it, a rumour.
- **Never seed an item you have not verified yourself.**
- **A guard that cannot fire reads exactly like coverage.** Violate each check once and watch it go
  red.

---

## Field Notes (Agent Journal)

A cross-project journal on working with AI, in its own repository. See the `field-notes` skill.

- **Volunteer them — don't wait to be asked.** No skill can be loaded on noticing something
  transferable.
- **Don't edit an old entry to make it right.** Being wrong is data.

---

## Specification-Driven Development

*Enabled by feature flag: `spec-driven`*

Specs are the source of truth, under `.specify/` and `specs/`.

- **Write factual specs, not aspirational ones**, naming real code in every claim. *Should* reads
  exactly like *does*.

---

## Operator — Peer Agents

*Enabled by feature flag: `operator-agents`*

`operator` starts a **peer, not a sub-agent**: its own process and git work, still running after you
stop watching.

- **A boundary is enforceable once declared.** `operator ownership check` refuses a branch that left
  its subproject; undeclared, only your instruction enforces it.
- **Answer what you are asked before you end your session.** A peer blocked on your reply is burning
  sessions doing nothing.

See the `peer-agents` skill.

---

## Branching Strategy

*Enabled by feature flag: `branching-strategy`*

- `main` is the integration branch. **There is no `develop` branch — don't create one, don't assume
  one.**
- `feat/`, `fix/`, `docs/` off `main`, worked in a worktree. Conventional commits.
