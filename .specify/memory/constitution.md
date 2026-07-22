# Copilot Tools Constitution

## Core Principles

### I. Specifications Are Executable Inputs

Every non-trivial change MUST begin with an in-repository spec-kit feature
specification under `specs/`. The specification defines observable behavior,
the plan records technical decisions, and `tasks.md` is the executable work
breakdown. Documentation MUST describe delivered behavior, not aspirations.

### II. Setup Is Idempotent and Recoverable

Setup and upgrade paths MUST be safe to rerun. They MUST preserve user-edited
configuration, detect already-installed tools, report actionable failures, and
avoid success-shaped fallbacks. External tools MUST come from documented
upstream sources and SHOULD be version-pinned with an explicit override.

### III. Parallel Work Is Explicitly Owned

An agent MUST atomically claim a todo before changing files for it. A todo may
have at most one active owner. Agents MUST respect dependency state, choose
other ready work rather than waiting on an in-progress prerequisite, and use
isolated git worktrees for concurrent implementation or review. Claims MUST
never be stolen without confirming that the previous owner has stopped.

### IV. Tooling Must Earn Its Place

The repository SHOULD install and document only tools that provide clear,
repeatable value. Low-value or redundant integrations MUST be removed from
setup, templates, skills, and documentation together so the supported toolchain
stays coherent.

### V. Verification Precedes Completion

Changed shell scripts MUST pass syntax checks and behavior-specific tests.
Generated spec-kit artifacts MUST pass their prerequisite checks. Significant
deliverables MUST receive adversarial review from isolated agents using more
than one model, and high-confidence findings MUST be resolved before merge.

## Operational Constraints

- Bash scripts target Linux and WSL and MUST use strict error handling.
- Repository changes occur on dedicated feature branches in isolated worktrees.
- User configuration and credentials MUST never be committed or overwritten
  without explicit consent.
- Parallel tasks that touch the same file are not independent and MUST run
  sequentially.
- `main` is an integration branch; direct authoring in its base worktree is
  prohibited.

## Development Workflow

1. Run the relevant spec-kit workflow: specify, clarify when needed, plan,
   tasks, implement, and analyze.
2. Mirror executable tasks into the session todo database when multiple agents
   participate.
3. Initialize shared todo-claim coordination before dispatching agents.
4. Claim one ready todo atomically, implement it in an isolated worktree, and
   mark both the SQL todo and matching `tasks.md` item complete.
5. Validate the smallest surface that proves the requirement, then run
   adversarial review before integration.

## Governance

This constitution supersedes conflicting workflow guidance in repository
templates. Amendments require a documented spec change, migration notes for
affected templates or generated artifacts, and an updated version below.

**Version**: 1.0.0 | **Ratified**: 2026-07-22 | **Last Amended**: 2026-07-22
