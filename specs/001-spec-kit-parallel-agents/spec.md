# Feature Specification: Spec Kit and Parallel Agents

**Feature ID**: `001-spec-kit-parallel-agents`

**Created**: 2026-07-22

**Status**: In Review

**Input**: Adopt GitHub spec-kit, install it during setup when absent, update
spec-writing guidance, coordinate parallel agents through todo ownership and
dependency-aware work selection, and remove the retired code graph MCP.

## User Scenarios & Testing

### User Story 1 - Bootstrap Spec-Driven Development (Priority: P1)

A user runs the repository setup and receives a working `specify` CLI plus
project instructions that use the current spec-kit workflow.

**Why this priority**: The repository cannot consistently use spec-kit until the
CLI and its workflow are available to every configured environment.

**Independent Test**: Run setup in an isolated home with `specify` absent and
confirm setup installs it; rerun with `specify` present and confirm no reinstall
occurs.

**Acceptance Scenarios**:

1. **Given** `specify` is not on `PATH`, **When** setup runs with network access,
   **Then** it installs the pinned official spec-kit CLI and verifies the command.
2. **Given** `specify` already exists, **When** setup runs, **Then** setup reports
   the installed version and leaves it unchanged.
3. **Given** this repository is cloned, **When** an agent inspects it, **Then**
   the spec-kit constitution, templates, Copilot skills, and feature specs are
   available in the standard locations.

---

### User Story 2 - Write and Maintain Specifications (Priority: P1)

An agent uses spec-kit commands and in-repository artifacts instead of the
legacy custom spec-change-proposal process.

**Why this priority**: Installing the CLI without replacing contradictory
writing guidance would create two competing specification systems.

**Independent Test**: Inspect the installed global and project instruction
templates and confirm they direct agents through the spec-kit constitution,
specification, clarification, plan, tasks, implementation, and analysis flow.

**Acceptance Scenarios**:

1. **Given** spec-driven development is enabled, **When** an agent starts a
   non-trivial change, **Then** it reads the constitution and relevant feature
   artifacts before implementation.
2. **Given** behavior changes, **When** implementation completes, **Then** the
   corresponding spec and tasks describe the delivered behavior and validation.

---

### User Story 3 - Coordinate Parallel Agents (Priority: P1)

Multiple agents share a todo graph without duplicating work or idling behind a
dependency that another agent is still implementing.

**Why this priority**: Parallel execution is unsafe unless ownership and
dependency checks are atomic and visible to every agent.

**Independent Test**: Simulate two agents claiming the same todo and confirm only
one owns it; mark a prerequisite in progress and confirm dependent work is
excluded while another ready todo is returned.

**Acceptance Scenarios**:

1. **Given** an unclaimed ready todo, **When** two agents attempt to claim it,
   **Then** exactly one claim succeeds.
2. **Given** a todo depends on an in-progress item, **When** an agent looks for
   work, **Then** that todo remains pending and another unclaimed todo with all
   dependencies done is selected.
3. **Given** an agent owns a todo, **When** another agent inspects the todo graph,
   **Then** the owner and claim time are visible and the second agent does not
   work on it.
4. **Given** an agent stops unexpectedly, **When** a coordinator confirms it is
   no longer running, **Then** the coordinator can release or reassign the claim.

---

### User Story 4 - Keep the MCP Toolchain Focused (Priority: P2)

A user receives only the supported Roslyn MCP integration and no setup prompts
or guidance for the retired code graph MCP.

**Why this priority**: Removing unused tooling reduces setup noise and prevents
agents from selecting an integration that the repository no longer recommends.

**Independent Test**: Search all tracked files and confirm there are no retired
MCP references while the Roslyn configuration remains valid.

**Acceptance Scenarios**:

1. **Given** a fresh setup, **When** MCP checks run, **Then** setup checks only
   the supported Roslyn server.
2. **Given** installed templates, **When** a user reads MCP or code-intelligence
   guidance, **Then** it describes Roslyn for C# and built-in search tools for
   other languages.

### Edge Cases

- The machine has Python older than spec-kit's minimum supported version.
- Neither `uv` nor `specify` is installed before setup.
- The network or upstream installer fails partway through setup.
- A todo has multiple prerequisites and only some are complete.
- A todo dependency points to a missing prerequisite row.
- Every pending todo is dependency-blocked or already claimed.
- An agent owns a todo but discovers a genuine external blocker.
- Two nominally parallel tasks modify the same file.

## Requirements

### Functional Requirements

- **FR-001**: The repository MUST contain a spec-kit project initialized from a
  pinned stable release with the GitHub Copilot skills integration.
- **FR-002**: Setup MUST detect an existing `specify` command and skip
  installation when it is present.
- **FR-003**: Setup MUST install the official `specify-cli` when it is absent.
- **FR-004**: The default spec-kit version MUST be pinned and overridable through
  configuration without editing the setup script.
- **FR-005**: Setup MUST verify the resulting `specify` command and surface
  installation failures.
- **FR-006**: Specification guidance MUST use in-repository `.specify/` and
  `specs/` artifacts as the source of truth.
- **FR-007**: Specification guidance MUST direct agents to keep specifications
  factual and update them with delivered behavior.
- **FR-008**: Parallel todo coordination MUST record one active owner and claim
  time per claimed todo.
- **FR-009**: Claiming a todo MUST be atomic so concurrent agents cannot both
  acquire it.
- **FR-010**: Agents MUST claim a todo before starting implementation.
- **FR-011**: Ready-work selection MUST exclude todos with any dependency whose
  status is not `done`.
- **FR-012**: When requested work is dependency-blocked, an agent MUST look for
  another unclaimed ready todo instead of waiting when such work exists.
- **FR-013**: Agents MUST NOT steal active claims; stale-claim recovery requires
  coordinator confirmation.
- **FR-014**: Spec-kit task guidance MUST distinguish parallel eligibility from
  ownership; a `[P]` marker does not itself assign work.
- **FR-015**: Setup, MCP configuration, skills, and documentation MUST contain no
  references to the retired code graph MCP.

### Key Entities

- **Feature Artifact**: A spec-kit constitution, specification, plan, research
  record, quickstart, or task list stored in the repository.
- **Todo**: A unit of executable work with a status and zero or more
  dependencies.
- **Todo Claim**: The exclusive active association between one todo and one
  uniquely identified agent, including claim and heartbeat timestamps.
- **Dependency**: A directed relationship requiring another todo to reach
  `done` before the dependent todo is ready.

## Success Criteria

### Measurable Outcomes

- **SC-001**: A clean setup with `specify` absent results in a callable
  `specify` command without manual spec-kit installation steps.
- **SC-002**: A second setup run performs zero spec-kit installation operations.
- **SC-003**: In a two-agent claim race, exactly one agent owns the target todo.
- **SC-004**: A ready-work query returns zero todos with incomplete dependencies
  and zero todos claimed by another agent.
- **SC-005**: Active setup, MCP configuration, skills, and documentation expose
  only the supported Roslyn MCP integration.
- **SC-006**: Spec-kit prerequisite checks resolve this feature's spec, plan, and
  tasks successfully.

## Assumptions

- Setup targets Linux and WSL, matching the existing Bash setup script.
- Network access is available when a missing external tool must be installed.
- The session SQL database is shared by agents participating in one coordinated
  task.
- A coordinator can provide each agent a unique identifier and isolated git
  worktree.
