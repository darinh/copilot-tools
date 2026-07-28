# Tasks: Platform-Agnostic Instructions and Documentation

**Feature**: `002-platform-agnostic-instructions` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

`[P]` marks tasks eligible for parallel execution — each touches a distinct file. `[P]` means eligible,
not assigned.

## Phase 1: Instruction templates (highest impact)

These files are installed into the user's environment and consumed by agents, so defects here cause
wrong commands rather than confusing prose.

- [x] **T001** [P] Fix `templates/copilot-instructions.md` (FR-001, FR-002, FR-005, FR-006)
  - Catalog example paths: add a Windows row alongside the POSIX row
  - Handoff fallback `touch <marker>`: replace with labelled PowerShell/bash variants
  - `handoff` example: remove bash `\` continuations, use a shell-neutral single-line form
  - Spec-kit init: replace hard-coded `--script sh` with labelled variants (`ps` on Windows)
  - Field Notes path: show both path forms

- [x] **T002** [P] Fix `templates/project-instructions.md` (FR-003, FR-002, FR-006)
  - Restart protocol final action `touch`: labelled PowerShell/bash variants — **highest-impact defect**
  - Header project-root example: show both path forms

## Phase 2: User-facing documentation

- [x] **T003** [P] Fix `README.md` (FR-007, FR-008, FR-006, FR-010)
  - Split prerequisites per platform
  - Label the POSIX install path; state Windows operator support honestly
  - `chmod`, `./setup.sh`, and symlink steps: label as POSIX, note Windows requirements
  - Clone/`cd` examples: show both path forms

- [x] **T004** [P] Fix `docs/operator.md` (FR-008, FR-006, FR-010)
  - Prerequisites per platform, with current support status
  - Label `./setup.sh` and `ln -sf` install steps as POSIX
  - Path examples: show both forms

- [x] **T005** [P] Fix `docs/spec-kit.md` (FR-005, FR-006)
  - `specify init --script sh`: labelled variants
  - `curl` bootstrap prerequisite: state per platform
  - `SPEC_KIT_VERSION=... ./setup.sh` env-prefix: labelled variants using `$env:` on PowerShell

- [x] **T006** [P] Fix `docs/skills.md` (FR-006)
  - `cp -r`: labelled variants using `Copy-Item -Recurse` on PowerShell

## Phase 3: Verification

- [x] **T007** Verify `.github/copilot-instructions.md` is platform-neutral (FR-004)
  - Audit for platform-specific commands and paths; record the result. No edit expected.

- [x] **T008** Static gate across all changed files (SC-001, SC-004)
  - Search for `touch `, `chmod `, `ln -s`, `cp -r`, `curl `, `/home/`, `--script sh`, `./setup.sh`
  - Every hit must sit inside an explicitly labelled platform block

- [x] **T009** Execute Windows variants on a Windows host (SC-002, SC-003)
  - Run each PowerShell variant and confirm success, especially the restart-signal fallback

- [x] **T010** Regression check (SC-005, FR-009)
  - `bash -n` on `operator.sh`, `handoff.sh`, `setup.sh`
  - Run existing bash test scripts; confirm no behavior changed

## Phase 4: Backlog seeding

- [x] **T011** Seed `specs/003-windows-native-operator/` with the deferred design work
  - Carry forward the verified research, contracts, data model, and quickstart
  - Record the review findings that must be resolved before that feature proceeds

## Dependencies

- T008, T009, T010 depend on T001–T006 being complete.
- T007 and T011 are independent of all others.
- T001–T006 are mutually independent (distinct files).
