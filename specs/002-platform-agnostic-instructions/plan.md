# Implementation Plan: Platform-Agnostic Instructions and Documentation

**Branch**: `002-platform-agnostic-instructions` | **Date**: 2026-07-27 | **Spec**: [spec.md](./spec.md)

## Summary

Remove Linux-only assumptions from every instruction and documentation file this repository ships, so an
agent or developer on Windows can follow them successfully. Documentation-only: no executable code
changes.

The defect inventory came from an audit verified line-by-line against the repository. The governing rule
is FR-006 — where a command genuinely differs by platform, present **labelled variants** rather than
picking one.

## Technical Context

**Language/Version**: Markdown only.

**Primary Dependencies**: None. No runtime dependency is added or changed.

**Testing**: A static check asserting no unlabelled POSIX-only construct remains, plus manual execution
of the Windows variants. Existing bash test scripts must continue to pass unchanged.

**Target Platform**: Documentation consumed on Windows (PowerShell), Linux, WSL, and macOS.

**Project Type**: Documentation and configuration templates.

**Constraints**: Must not alter behavior of any script. Must not remove or invalidate existing POSIX
instructions (FR-009). Must not claim Windows support for the operator runtime, which does not exist yet
(FR-010).

**Scale/Scope**: 2 templates, 4 documentation files, 1 repository instruction file audited.

## Constitution Check

| Principle | Status | Evidence |
|---|---|---|
| **I. Specifications Are Executable Inputs** | **PASS** | `spec.md` precedes implementation; docs will describe delivered behavior only, and FR-010 explicitly forbids aspirational Windows claims. |
| **II. Setup Is Idempotent and Recoverable** | **PASS** | No setup behavior changes. Documented install steps gain a Windows form without altering the POSIX path. |
| **III. Parallel Work Is Explicitly Owned** | **PASS** | Isolated worktree `copilot-tools-002-windows` on branch `002-platform-agnostic-instructions`. Tasks are grouped per file so no two touch the same file concurrently. |
| **IV. Tooling Must Earn Its Place** | **PASS** | No tooling added. |
| **V. Verification Precedes Completion** | **PASS** | Static grep gate plus execution of Windows variants; bash syntax checks and existing tests confirm nothing regressed. |

**Gate result: PASS.** No constitution amendment is required for this phase — it introduces no new
implementation language. The amendment discussed during the combined-scope planning belongs to
`specs/003-windows-native-operator/`.

## Project Structure

```text
copilot-tools/
├── templates/
│   ├── copilot-instructions.md    # Installed into ~/.copilot/ — highest impact
│   └── project-instructions.md    # Per-project template
├── .github/copilot-instructions.md # Repository's own agent instructions (audit only)
├── README.md                       # Install path
└── docs/
    ├── operator.md                 # Install + prerequisites + path examples
    ├── spec-kit.md                 # Spec-kit init + bootstrap
    └── skills.md                   # Skill installation
```

**Structure Decision**: Edit files in place. No new files, no reorganization — the defects are localized
and the surrounding structure is sound.

## Design

### D1. Presentation convention for platform variants

One convention used everywhere, satisfying FR-006:

- Where a command differs, present both under explicit **PowerShell (Windows)** and **bash (Linux/macOS/WSL)**
  labels.
- Never rely on ordering or context to imply which variant applies.
- Prefer a genuinely cross-platform command where one exists, so a variant pair is not needed at all.
- Keep the POSIX form byte-identical to today wherever possible, so existing users see no change (FR-009).

### D2. Defect inventory

Verified against the repository. Grouped by file; each maps to a task.

**`templates/copilot-instructions.md`** — installed into the user's environment, so highest impact:

| Defect | Fix |
|---|---|
| Catalog example paths are POSIX-only (`/home/user/...`) | Show a Windows and a POSIX row |
| Handoff fallback prescribes `touch <marker>` | Labelled variant pair; PowerShell uses `New-Item -ItemType File -Force` |
| `handoff` example uses bash `\` line continuations in a ```bash fence | Single-line invocation, shell-neutral fence |
| Spec-kit init hard-codes `--script sh` | Labelled variants selecting `ps` on Windows, `sh` elsewhere |
| Field Notes location is a hard-coded POSIX path | Express relative to a documented root, showing both forms |

**`templates/project-instructions.md`**:

| Defect | Fix |
|---|---|
| Restart protocol prescribes `touch` as the mandatory final action | Labelled variant pair — the single highest-impact defect |
| Header shows `/path/to/project` only | Show both path forms |

**`README.md`**:

| Defect | Fix |
|---|---|
| `chmod +x setup.sh operator.sh` | Not needed on Windows; label the POSIX step |
| `./setup.sh` | Label as POSIX; state the Windows position honestly |
| Clone/`cd` into `~/projects/...` | Show both path forms |
| Symlink steps for operator and extensions | Label as POSIX; note Windows requires Developer Mode or elevation |
| Prerequisite list is POSIX-only | Split per platform |

**`docs/operator.md`**:

| Defect | Fix |
|---|---|
| Prerequisites list `tmux`, `sqlite3` unconditionally | State per platform and record current support status |
| `./setup.sh` and `ln -sf ... ~/.local/bin/operator` | Label as POSIX |
| `~/projects/my-project` example | Show both path forms |

**`docs/spec-kit.md`**:

| Defect | Fix |
|---|---|
| `specify init ... --script sh` | Labelled variants |
| Bootstrap "requires `curl`" | State the per-platform prerequisite |
| `SPEC_KIT_VERSION=vX.Y.Z ./setup.sh` env-prefix syntax | Labelled variants; PowerShell uses `$env:` |

**`docs/skills.md`**:

| Defect | Fix |
|---|---|
| `cp -r ...` | Labelled variants; PowerShell uses `Copy-Item -Recurse` |

**`.github/copilot-instructions.md`**: audited, **clean**. Contains only SQL and git-worktree guidance
with no platform-specific commands or paths. FR-004 is satisfied by verification, not by edits — recorded
explicitly so the requirement is traceable rather than silently skipped.

### D3. Honest support status (FR-010)

The operator does not run on Windows yet. Documentation must therefore:

- Describe the Windows form of instructions, conventions, and spec-kit usage, which do work.
- State plainly that the operator runtime currently requires Linux, WSL, or macOS.
- Not present a Windows operator install path that cannot succeed.

This avoids the failure mode where a reader follows a Windows install that silently cannot work.

### D4. Verification

1. **Static gate** — search all changed files for `touch `, `chmod `, `ln -s`, `cp -r`, `curl `,
   `/home/`, `--script sh`, and `./setup.sh`. Every remaining hit must sit inside an explicitly labelled
   platform block.
2. **Execution** — run each Windows variant on this machine and confirm it succeeds.
3. **Regression** — `bash -n` on all shell scripts plus the existing bash test scripts, confirming this
   documentation change altered no behavior.

## Complexity Tracking

No constitution violations. No complexity to justify — the change adds no abstraction, no dependency, and
no new file.
