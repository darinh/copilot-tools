---
id: 7
title: Stop installing a user-global instructions file; give each project its own AGENTS.md
status: open
opened: 2026-08-05
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
