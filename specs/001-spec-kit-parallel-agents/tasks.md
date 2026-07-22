# Tasks: Spec Kit and Parallel Agents

**Input**: Design documents from `specs/001-spec-kit-parallel-agents/`

**Tests**: Setup installation, SQLite claim behavior, spec-kit prerequisites, and
repository-wide MCP reference removal are required by the feature specification.

## Phase 1: Spec-kit Foundation

- [X] T001 Initialize spec-kit v0.13.4 with Copilot skills in `.specify/` and `.github/skills/`
- [X] T002 Write project governance in `.specify/memory/constitution.md`
- [X] T003 Create feature artifacts in `specs/001-spec-kit-parallel-agents/`

## Phase 2: Setup and Toolchain

- [ ] T004 [P] [US1] Add conditional pinned spec-kit installation to `setup.sh`
- [ ] T005 [P] [US1] Add isolated setup coverage in `tests/test-setup-spec-kit.sh`
- [ ] T006 [P] [US4] Remove codebase-memory-mcp from `templates/mcp-config.json`, `skills/code-intelligence/SKILL.md`, `docs/skills.md`, and `README.md`

## Phase 3: Specification Workflow

- [ ] T007 [US2] Replace legacy spec-writing rules in `templates/copilot-instructions.md`
- [ ] T008 [P] [US2] Enable spec-kit in `templates/project-instructions.md`
- [ ] T009 [P] [US2] Document spec-kit setup and commands in `docs/spec-kit.md` and `README.md`

## Phase 4: Parallel Agent Coordination

- [ ] T010 [US3] Add atomic todo claim and ready-work SQL guidance to `templates/copilot-instructions.md`
- [ ] T011 [P] [US3] Add repository coordination rules to `.github/copilot-instructions.md`
- [ ] T012 [US3] Add claim-before-work behavior to `.github/skills/speckit-implement/SKILL.md`
- [ ] T013 [P] [US3] Clarify parallel eligibility and ownership in `.specify/templates/tasks-template.md`

## Phase 5: Validation and Review

- [ ] T014 Run shell, setup, SQLite, spec-kit, JSON, and repository search checks
- [ ] T015 Run isolated multi-model adversarial reviews and resolve all consensus findings
- [ ] T016 Update `specs/001-spec-kit-parallel-agents/spec.md` and this task list to reflect delivered behavior

## Dependencies & Execution Order

- T001-T003 establish the spec-kit foundation.
- T004-T006 can proceed in parallel after T001.
- T007 depends on T003; T008-T009 can proceed after T003 without touching the
  same files as T007.
- T010 depends on T007 because it extends the same instruction template.
- T011 and T013 can proceed in parallel after T003.
- T012 depends on T010 so the generated implementation skill matches the global
  coordination contract.
- T014-T016 depend on all implementation tasks.

## Parallel Execution Rules

- `[P]` means a task is eligible for parallel execution because its declared
  files and dependencies do not overlap.
- `[P]` does not assign the task. Every agent must still acquire the matching
  SQL todo claim before editing.
- If a dependency is not done, leave the todo pending and claim another ready
  item.
- Tasks touching the same file run sequentially even if they concern different
  user stories.
