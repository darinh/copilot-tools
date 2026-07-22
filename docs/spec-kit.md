# Spec Kit

The `copilot-tools` repository leverages **Spec Kit** to drive Specification-Driven Development.

## Installation & Setup

When the `spec-driven` feature is enabled for a project and `.specify/` is
missing, agents initialize Spec Kit automatically.

You can also initialize it manually:

```bash
specify init --here --force --integration copilot --integration-options="--skills" --script sh
```

During `copilot-tools` setup, the official `specify-cli` is installed from the
pinned GitHub release when `specify` is absent. A fresh bootstrap requires
Python 3.11+ and `curl`; setup installs `uv` when needed. Override the release
pin with `SPEC_KIT_VERSION=vX.Y.Z ./setup.sh`.

## Standard Paths

Spec Kit uses the following standard paths in your repository:

- `.specify/` — Contains templates, scripts, and the `memory/constitution.md` which dictates agent governance.
- `specs/` — The directory where your feature specifications, plans, and tasks reside.
- `specs/[feature-id]/spec.md` — The observable behavior specification for a feature.
- `specs/[feature-id]/plan.md` — Technical plan for implementation.
- `specs/[feature-id]/tasks.md` — Executable task breakdown.

## Commands

Use the Copilot CLI skills (or equivalent slash commands if configured) to drive the workflow:

- `/speckit-constitution` — Establish or update project governance.
- `/speckit-specify` — Scaffold or update a specification based on requirements.
- `/speckit-clarify` — Resolve ambiguities in the spec.
- `/speckit-plan` — Generate a technical implementation plan.
- `/speckit-tasks` — Break the plan into an executable task list (`tasks.md`).
- `/speckit-implement` — Execute tasks in `tasks.md`.
- `/speckit-analyze` — Review the deliverables for accuracy.

## Upgrades

The setup script intentionally leaves an existing installation unchanged. Use
the official self-management commands to check or upgrade it:

```bash
specify self check
specify self upgrade --tag v0.13.4
```

## Parallel Workflow

When working with parallel agents, spec-kit utilizes the session SQLite database to coordinate tasks and prevent duplicate work:

1. **Initialization**: A coordinator creates a `todo_claims` table in the shared SQL database.
2. **Identity**: Each agent is assigned a unique stable ID.
3. **Claiming**: Agents atomically claim one ready task at a time using a `BEGIN IMMEDIATE` transaction.
4. **Dependency Awareness**: A task is only ready if all its dependencies are `done` and it remains unclaimed. If a preferred task is blocked by an in-progress dependency, agents look for other ready work instead of idling.
5. **Updating**: Upon completion, the worker agent updates the SQLite status and reports completion. To prevent filesystem race conditions, ONLY the coordinator serially updates the `tasks.md` checkboxes (in single-agent mode, the lone agent handles both).

Tasks marked `[P]` in `tasks.md` are eligible for parallel execution, but actual ownership is governed entirely by the SQL database claims to guarantee safe coordination. Tasks that modify the same file must be run sequentially even if marked `[P]`.