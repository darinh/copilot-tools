---
id: 21
title: The empty-directory scan only looks at the top level of the checkout
status: proposed
opened: 2026-08-06
spec: none
---

## Evidence

Measured 2026-08-05 by reading `_empty_dir_strays` in `handoff_tool.py`: its
candidate list comes from a single `os.scandir(root)`, so only immediate
children of the checkout root are considered.

A reviewer subagent that creates `tests/scratch/` and leaves it empty is
therefore invisible to the guard: git does not report an empty directory at
all, and this pass never looks inside `tests/`.

The behaviour matches the JS reference, `extensions/checkout-guard/guard.mjs`
(`invisibleDirStrays`), so the two implementations agree -- they are both
top-level only.

## Why it matters

The nine artifacts that produced this whole rule were left in the checkout
*root*, so the top-level scan covers the incident on record. But nothing
about the mechanism is specific to the root: an agent reproducing a defect
under `tests/` gets the same silence from `git status`, and the guard's
refusal message would then read as a clean bill of health for a tree that
is not clean.

A guard that is silent on a case it looks like it covers is worse than one
that is loud about its limit.

## Notes

Not a simple widening. A full-tree walk would have to skip ignored
directories to avoid walking `node_modules`, `.venv` and every build output,
and `git check-ignore` can answer that but only per candidate -- the cost
grows with the tree rather than with the number of strays.

`holds_no_files` already carries a 512-directory traversal budget for exactly
this reason, and returns False (not empty, therefore not a finding) when it
trips. Widening the candidate set without rethinking that budget would make
the budget the thing deciding what gets reported.

Worth measuring first: how many directories deep does a real reproduction
usually land? If it is always the root or one level down, a depth-2 scan buys
most of the value for a bounded cost.
