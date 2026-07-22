# Repository Agent Instructions

## Spec-kit workflow

- Read `.specify/memory/constitution.md` and the active feature under `specs/`
  before implementing a non-trivial change.
- Use the `speckit-*` skills for specification, planning, tasks,
  implementation, and analysis.
- Keep specs factual and update `spec.md`, `plan.md`, and `tasks.md` with
  delivered behavior.

## Parallel todo ownership

- The coordinator must create the shared `todo_claims` table before launching
  parallel agents.
- Use a unique stable agent ID and atomically claim one ready todo before
  changing files.
- Never work on a todo claimed by another agent and never steal a claim without
  coordinator confirmation that its owner has stopped.
- A todo is ready only when it is pending, unclaimed, and every dependency is
  `done`.
- If preferred work depends on an in-progress todo, leave it pending and select
  another ready todo instead of waiting.
- Work in an isolated git worktree. Tasks that modify the same file are
  sequential even when they are otherwise marked parallel.
