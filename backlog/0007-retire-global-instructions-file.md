---
id: 7
title: Stop installing a user-global instructions file; give each project its own AGENTS.md
status: closed
opened: 2026-08-05
closed: 2026-08-05
commit: 2038aac
spec: none
---

## Evidence

Measured 2026-08-05 on this machine.

`setup_tools.TEMPLATE_ARTIFACTS` (`setup_tools.py:1311-1315`) copies
`templates/copilot-instructions.md` into `~/.copilot`:

```python
TEMPLATE_ARTIFACTS = (
    ("mcp-config.json", "mcp-config.json", "MCP config"),
    ("copilot-instructions.md", "copilot-instructions.md", "Copilot instructions"),
)
```

The deployed file is present and is 29,621 bytes:

```
C:\Users\darin\.copilot\copilot-instructions.md   29621 bytes
ABSENT: C:\Users\darin\.copilot\AGENTS.md
ABSENT: C:\Users\darin\AGENTS.md
```

Because it lives at user scope it is loaded into **every** Copilot session on
this machine, in every directory, whether or not the operator is involved and
whether or not the directory is a project. The user's report:

> "Every time I run normal copilot without the operator, it is still trying to
> set up a project. I didn't foresee this several months ago when I started
> this project."

That behaviour is the file working as written. Its "On Session Start -- Project
Lookup" section instructs the agent to resolve a project root, read the
catalog, and -- when there is no match -- offer to set the project up. At user
scope there is no way for that instruction to *not* run.

No global `AGENTS.md` exists on this machine, so the merge case described below
is unexercised here and must be tested against a synthetic one.

## Why it matters

A user-scoped instructions file cannot distinguish "a project that opted into
these conventions" from "any directory the user happened to open a terminal
in". Every session pays the cost of conventions it did not ask for, and the
first thing an unrelated session does is try to enroll its working directory
into a system it has nothing to do with.

The eight catalogued projects already have per-project instruction files, so
the global copy is not the only carrier of these conventions -- it is the copy
with the widest blast radius and the least consent.

## Notes

Requested behaviour, from the user:

- On its next run, the operator should offer to set up features for **all**
  detected projects, then remove the user-level instructions file.
- If a global `AGENTS.md` exists, bring it to the user's attention rather than
  touching it.
- Setup should write a **project-level `AGENTS.md`** per project. Where a repo
  already has one, read it and ask whether to combine.

Carry into refinement:

- **Deleting a 29 KB file the user may have edited is destructive and
  irreversible.** Whatever does it must preserve a copy first. This repository
  already has the pattern and the scar tissue for exactly this: the handoff
  tool's `superseded/` directory exists because an unread file was overwritten.
- **Removal and replacement must not be separable.** If the global file is
  deleted before the per-project `AGENTS.md` files are written, every project
  on the machine silently loses its conventions in the window between. Order
  the operation so the failure mode is a duplicate, never a gap.
- **`install_manifest` records what setup deployed**, so retiring an artifact
  is a manifest change and an upgrade-path question, not just a deletion.
  A machine that installed the old artifact must be able to reach the new state.
- Depends on item 0006: "set up features for each project" is the same feature
  vocabulary the Project Configurations menu needs, and the two must read it
  from one place.

## Delivered

Landed in `2038aac` (three commits on `feat/retire-global-instructions`).

Setup no longer deploys the file. `project_instructions.py` renders each
catalogued project an `AGENTS.md` holding only the sections its own features
turned on, with the enrollment section replaced by that project's resolved
facts — its guid, its project directory, its `features.json` — so a project
file has nothing to look up and nothing to enroll. Reached from the projects
screen or as `operator projects retire [--yes]`, both offered only while the
file is still there.

Every carry-into-refinement point is honoured:

- **Preserved first.** The file is copied to `~/.copilot/retired/`, named by
  timestamp and content digest, and the copy is read back and digest-compared
  before the original is unlinked. Nothing prunes that directory.
- **Not separable.** Every project is written before anything is removed, and
  any blocker — a decline, a project directory not on this machine, a failed
  write, an unparseable catalog row, or the catalog changing mid-run — leaves
  the file in place.
- **Manifest and upgrade path.** `RETIRED_ARTIFACTS` keeps reporting the
  leftover until it is gone, and `upgrade_v1_3_0_to_v1_4_0` names it and the
  command. The migration deletes nothing: an upgrade is not consent.
- **A user-scope `AGENTS.md`** is reported and left untouched.
- **Item 0006's vocabulary** is the only source of features; the gate regex
  now lives in `project_instructions.GATE` and `test_project_features` imports
  it rather than keeping a second copy.

Verification: 2966 passed, 10 skipped (baseline 2856/10). Two adversarial
review rounds across three models found four defects, each fixed and each
mutation-tested — a malformed catalog row that let the file be removed anyway,
marker matching that clobbered a repository's own `AGENTS.md`, a Markdown
fence bug that emitted content for features that were off, and a
`Path(...).name` label that returns the whole string for a Windows path read
on Linux. Round 2 found a TOCTOU race on the catalog snapshot; also fixed.
