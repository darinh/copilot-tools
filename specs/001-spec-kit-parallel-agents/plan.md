# Implementation Plan: Spec Kit and Parallel Agents

**Feature**: `001-spec-kit-parallel-agents` | **Date**: 2026-07-22

**Spec**: `specs/001-spec-kit-parallel-agents/spec.md`

## Summary

Initialize the repository with spec-kit v0.13.4 in GitHub Copilot skills mode,
install the `specify` CLI from the official GitHub release during setup when
missing, replace legacy specification guidance, add an atomic SQLite todo-claim
protocol for parallel agents, and remove the retired code graph MCP.

## Technical Context

**Language/Version**: Bash 4+, Markdown, SQLite 3

**Primary Dependencies**: GitHub spec-kit `specify-cli`, `uv`, GitHub Copilot CLI,
optional dotnet-roslyn-mcp

**Storage**: Session SQLite database (`todos`, `todo_deps`, `todo_claims`)

**Testing**: `bash -n`, isolated-home shell fixtures, SQLite concurrency and
dependency queries, spec-kit prerequisite scripts, repository text search

**Target Platform**: Linux and WSL

**Project Type**: CLI tooling and workflow templates

**Performance Goals**: Todo claim and ready-work selection complete in one short
SQLite transaction; setup adds no network work when `specify` already exists.

**Constraints**: Setup must be idempotent, preserve user-edited templates, fail
clearly, and avoid duplicate work across agents.

**Scale/Scope**: One setup script, repository and global instruction templates,
generated spec-kit workflow files, MCP template, one code-intelligence skill,
and supporting documentation/tests.

## Constitution Check

- **Specifications Are Executable Inputs**: PASS. This change has a feature
  specification, plan, research record, quickstart, and task list.
- **Setup Is Idempotent and Recoverable**: PASS. Installation is conditional,
  pinned, configurable, and behavior-tested.
- **Parallel Work Is Explicitly Owned**: PASS. The design uses a unique claim row
  and dependency-aware selection.
- **Tooling Must Earn Its Place**: PASS. The low-value MCP integration is removed
  from every supported surface.
- **Verification Precedes Completion**: PASS. Shell, SQLite, spec-kit, search,
  and isolated adversarial review gates are included.

## Project Structure

```text
.github/
├── copilot-instructions.md
└── skills/speckit-*/SKILL.md
.specify/
├── memory/constitution.md
├── scripts/bash/
├── templates/
└── workflows/
docs/
├── operator.md
├── skills.md
└── spec-kit.md
skills/code-intelligence/SKILL.md
specs/001-spec-kit-parallel-agents/
├── plan.md
├── quickstart.md
├── research.md
├── spec.md
└── tasks.md
templates/
├── copilot-instructions.md
├── mcp-config.json
└── project-instructions.md
tests/test-setup-spec-kit.sh
tests/test-todo-claims.sh
README.md
setup.sh
```

**Structure Decision**: Keep spec-kit infrastructure in its standard generated
locations, retain reusable user-facing templates under `templates/`, and cover
setup plus SQLite coordination with focused shell tests.

## Design

### Spec-kit installation

- Define `SPEC_KIT_VERSION` with a stable default and environment override.
- If `specify` exists, report its version and skip all installation work.
- Otherwise require Python 3.11+, bootstrap `uv` through its official installer
  when necessary, install `specify-cli` from the pinned GitHub tag, and verify
  the command.

### Parallel todo coordination

- Create `todo_claims` with `todo_id` as the primary key and `agent_id` unique.
- The coordinator creates the table before dispatching agents.
- Agents acquire claims inside `BEGIN IMMEDIATE` transactions and only for
  pending todos whose dependencies are all `done`.
- Ready-work selection excludes claimed and dependency-blocked todos.
- Completion updates the todo and removes its active claim in one transaction.
- Genuine blockers set the todo to `blocked` and release the agent; dependency
  waits leave the todo `pending`.
- Only the coordinator may recover a stale claim after confirming the owner is
  no longer active.

### Instruction integration

- Global and project templates use spec-kit commands and in-repository specs.
- The spec-kit implementation skill and task template state that `[P]` means
  eligible for concurrent execution, not assigned.
- Repository-local instructions carry the same ownership rules.

## Complexity Tracking

No constitution violations require exceptions.
