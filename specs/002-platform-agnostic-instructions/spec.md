# Feature Specification: Platform-Agnostic Instructions and Documentation

**Feature Branch**: `002-platform-agnostic-instructions`

**Created**: 2026-07-27

**Status**: Draft

**Input**: User description: "the copilot-instructions that are included in this repo to 'setup' the tools need to be OS/platform agnostic. Right now I think there are a bunch of hard-coded linux things in the instructions."

## Context and Phasing

This feature is **phase 1** of making the toolkit usable on Windows. The parent effort was split after an
adversarial review found that a single combined delivery mixed independent concerns and could not be
verified as a unit.

| Phase | Scope | Status |
|---|---|---|
| **1 — this feature** | Make shipped instructions and documentation platform-agnostic | **Delivering now** |
| 2 | Session-backend and process-supervision spike | Deferred → `specs/003-windows-native-operator/` |
| 3 | Windows operator MVP: single session, list/stop, metrics | Deferred → `specs/003-windows-native-operator/` |
| 4 | Loop mode, handoff, resume, concurrency on Windows | Deferred |
| 5 | Windows setup, packaging, extension install | Deferred |

Phase 1 is deliberately first: it is self-contained, carries no runtime risk, needs no new dependency,
and delivers value immediately because an agent reading Linux-only instructions on Windows emits
commands that fail.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An agent on Windows follows the shipped instructions successfully (Priority: P1)

An AI agent working on a Windows machine reads the workflow instructions this repository installs and
performs the session-handoff protocol. Every command it is told to run is valid on Windows, so the
handoff succeeds instead of failing at the moment continuity matters most.

**Why this priority**: The session-end protocol instructs the agent to run `touch` as its *mandatory
final action*. On Windows `touch` does not exist, so the restart signal is never raised and the handoff
silently fails — the single highest-impact defect in the repository.

**Independent Test**: On Windows, follow the session-end protocol in the installed instructions
literally, and verify the restart marker file is created.

**Acceptance Scenarios**:

1. **Given** the installed workflow instructions, **When** an agent on Windows performs the restart-signal
   fallback exactly as written, **Then** the marker file is created successfully.
2. **Given** the installed per-project instructions, **When** an agent on Windows performs the mandatory
   final action of the restart protocol, **Then** it succeeds without a command-not-found error.
3. **Given** any command prescribed by the instructions, **When** an agent selects the variant for its
   platform, **Then** it can do so from an explicit label rather than by guessing.

---

### User Story 2 - A developer on Windows installs the toolkit from the README (Priority: P1)

A developer clones the repository on Windows and follows the README and operator documentation to
install. The instructions either work on their platform or clearly present the Windows variant.

**Why this priority**: Installation is currently impossible to follow on Windows — the documented path
depends on `chmod`, `./setup.sh`, and `ln -s`. A reader cannot get started at all, so this ranks equal to
US1.

**Independent Test**: Read the README and operator docs on Windows and confirm every install step has an
executable Windows form, with no step depending on a POSIX-only command.

**Acceptance Scenarios**:

1. **Given** the README install section, **When** a Windows developer follows it, **Then** no step requires
   `chmod`, `ln -s`, or invoking a `.sh` script.
2. **Given** documentation that lists prerequisites, **When** a developer reads it on any supported
   platform, **Then** the prerequisites for that platform are stated explicitly.
3. **Given** documentation showing file-path examples, **When** a Windows developer reads them, **Then**
   the expected path format is unambiguous.

---

### User Story 3 - Spec-kit initializes correctly on every platform (Priority: P2)

A developer or agent initializes spec-kit for a project. The generated helper scripts are executable on
the platform where initialization ran.

**Why this priority**: Real but narrower than US1/US2 — it affects projects adopting spec-kit rather than
every session. It is separable and independently testable.

**Independent Test**: Confirm that every documented spec-kit initialization command selects the script
variant matching the platform.

**Acceptance Scenarios**:

1. **Given** a spec-kit initialization instruction, **When** it runs on Windows, **Then** it selects the
   PowerShell script variant.
2. **Given** the same instruction on Linux or macOS, **When** it runs, **Then** it selects the POSIX shell
   variant.

---

### Edge Cases

- **A command has no cross-platform equivalent**: The instruction must present a labelled variant per
  platform rather than picking one and leaving other platforms broken.
- **An agent cannot determine its platform**: Instructions must be written so a wrong guess is
  impossible — variants are labelled, not implied by ordering.
- **Home-directory notation**: `~` is understood by PowerShell in path contexts but not universally in
  every command form; path examples must not rely on a reader inferring the convention.
- **Existing Linux users read the revised docs**: The POSIX instructions must remain present and correct,
  not be replaced by Windows-only forms.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The shipped workflow instruction template MUST NOT prescribe any command valid on only one
  platform without also giving the equivalent for the other supported platforms.
- **FR-002**: The shipped workflow instruction template MUST present file-path examples unambiguously for
  Windows as well as Linux and macOS.
- **FR-003**: The per-project instruction template MUST express its session-end and restart-signal
  fallback steps so they work on every supported platform.
- **FR-004**: The repository's own agent instructions MUST describe the spec-kit workflow and
  parallel-agent coordination rules without assuming a specific operating system or shell.
- **FR-005**: Any instruction that initializes spec-kit MUST produce helper scripts executable on the
  platform where initialization runs.
- **FR-006**: Where a command genuinely differs per platform, every instruction MUST label each variant
  with the platform it applies to, so a reader selects rather than guesses.
- **FR-007**: Installation documentation MUST provide a Windows-executable path that requires no
  POSIX-only command.
- **FR-008**: Documentation that lists prerequisites MUST state them per platform.
- **FR-009**: Existing POSIX instructions MUST remain present and correct; this feature MUST NOT replace
  them with Windows-only forms.
- **FR-010**: Documentation MUST state which platforms the toolkit currently supports and MUST NOT claim
  Windows support for runtime components that do not yet have it.

### Key Entities

- **Instruction template**: A file this repository installs into a user's environment to configure agent
  behavior. Consumed by agents, so defects cause wrong commands rather than merely confusing prose.
- **Platform variant**: A labelled form of a command for a specific platform family, presented alongside
  its siblings so a reader selects the correct one.
- **Prerequisite set**: The tools required for a platform, which differ between Windows and POSIX.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Zero commands prescribed by the installed instruction templates fail on Windows because of
  platform mismatch, measured by executing every prescribed command on Windows.
- **SC-002**: An agent on Windows completes the documented session-end restart-signal fallback
  successfully on the first attempt.
- **SC-003**: A developer on Windows can follow the documented installation path end to end without
  encountering a step that requires `chmod`, `ln -s`, `cp -r`, `curl`, or invoking a `.sh` script.
- **SC-004**: 100% of platform-divergent commands in the shipped templates and documentation carry an
  explicit platform label.
- **SC-005**: Existing Linux and WSL instructions remain valid — every POSIX command documented before
  this change is still documented and still correct.
- **SC-006**: Every spec-kit initialization instruction selects the script variant matching the platform
  it runs on.

## Assumptions

- **Supported platforms for instructions**: Windows 10/11 with PowerShell, Linux, WSL, and macOS. macOS
  shares the POSIX variant throughout.
- **PowerShell is the Windows shell**: Windows variants target PowerShell, not `cmd.exe`.
- **Documentation-only change**: This feature changes no executable behavior. The operator's runtime
  Windows support is deferred to `specs/003-windows-native-operator/`.
- **Honest support claims**: Because the operator does not yet run on Windows, documentation must
  describe the Windows path for the parts that work (instructions, spec-kit, conventions) without
  implying the operator runs there.
- **`~` in paths**: Acceptable in path examples since PowerShell resolves it in path contexts, but not
  acceptable as a substitute for showing Windows path format at least once per example set.

## Out of Scope

- Any change to `operator.sh`, `handoff.sh`, `setup.sh`, or `operator-ingest.py` behavior.
- The Python forward-port, session-backend selection, and Windows operator runtime — deferred to
  `specs/003-windows-native-operator/`.
- Windows packaging, console-script installation, and extension linking.
- Validating the operator itself on macOS.
