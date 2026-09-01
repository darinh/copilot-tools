---
id: 32
title: The handoff guard reports the worktrees directory and peer worktrees as this checkout's litter
status: proposed
opened: 2026-08-15
spec: none
---

## Evidence

Measured 2026-08-15 in scratch repositories under `%TEMP%`.

`git worktree remove` deletes the tree and never its parent:

```
$ git worktree add .worktrees/feat-x -b feat/x
$ git worktree remove .worktrees/feat-x
$ Test-Path .worktrees      -> True, holding 0 entries
$ git status --porcelain -uall --ignored=matching -z   -> (no output)
```

Git reports nothing, because there is no blob in an empty directory. The
handoff guard'"'"'s own walk was the only thing that saw it, and it reported it:

```
$ handoff --instance probe --status s --next n
Error: The checkout is not clean, so this handoff was not written.

    .worktrees/

  ... Commit what belongs to the repository, delete what was scratch,
  and run the same command again.
```

Second, worse half. With a *live* worktree present and `.worktrees/` not
ignored, `scan_checkout` returned:

```
['.worktrees/feat-x/', '.worktrees/feat-x/tests/']
```

`.worktrees/feat-x/` is another agent'"'"'s working tree, reported by `git status`
as untracked; `.worktrees/feat-x/tests/` is an empty directory inside it,
found by the guard'"'"'s walk descending into that tree.

This repository never saw either, because its own `.gitignore` carries
`/.worktrees/`, so git prunes the whole subtree before the walk begins. It was
reported by the `subtitle-localizer` instance, whose repository had no such
rule, and reproduced here from that report.

## Why it matters

The handoff is what carries a session'"'"'s context across a restart, and this
refused it permanently: the directory is created by this toolchain and
recreated by the next `operator worktree new`, so nothing the agent cleaned
could clear the complaint.

The advice attached to the refusal made it worse than a stall. It names the
paths and says to delete what was scratch -- so an agent following it deletes
a peer'"'"'s live working tree, against the one rule this project states about
worktrees ("Leave worktrees you did not create alone"). The safety mechanism
was issuing the instruction the safety rules exist to prevent.

## Done when

The built half is done; what remains is a close and a decision about the residue.

- `backlog close 32` records the commit that finished the work. The last of the
  four is `b7d0d2d88b821772dcd2ff6b9df8e35cbd48edaf`, "fix(handoff): guard every
  exception Path.resolve can raise", 2026-08-15 18:08 -0700.
- The residual general-case gap -- a linked worktree created *outside*
  `.worktrees/` is still reported as this checkout's litter -- has a home: either
  folded into item 0020, which already owns the Python/JS guard divergence, or
  filed as its own item. It must not be closed silently with this one.

## Not in scope

- The general case above, under this id.
- `handoff.sh`, which has no guard at all. That is item 0020.

## Risk

🟢 nothing left to build for the landed half. The residue is 🟡: adopting the JS
approach means calling `git worktree list` on the handoff path and deciding what
an unanswerable call means -- the JS answer is null, "no information", which
callers already treat as not-clean.

## Needs a decision before this can be worked

- **Approval to close**, which is the only thing standing between this item and
  `closed`. `operator backlog close 32` refuses without it, which is the gate
  working as intended rather than an obstacle to route around.
- **Where the residual gap goes** -- 0020, or its own item.

## Verified 2026-08-31: the fix is on `main`, not only on `work/1`

The Status section below says the fix landed on `work/1`. It has since merged:

```
$ git branch --contains fe1b1a9      $ git branch --contains b7d0d2d
  docs/reboot-kill-shape               docs/reboot-kill-shape
* main                               * main
  work/1                               work/1
```

All four commits are ancestors of `main`. So this is an item whose work is
shipped and merged, and which reads as `proposed` -- the queue's most misleading
state, because `backlog ready --explain` reports it as awaiting approval to be
*worked* when what it awaits is approval to be *closed*.

## Notes

The JS reference implementation already had this right: INTRINSIC_EXCLUSIONS in `extensions/checkout-guard/guard.mjs` holds `.git` and `.worktrees`, on the stated grounds that both are 'checkouts or plumbing, never repository content'. The Python guard was the half that lagged, so closing this narrows a divergence rather than widening one. Backlog item 0020 tracks the rest of that divergence.

**Remaining gap, measured while fixing this.** The fix exempts by *name*:
paths whose first segment is the worktrees directory. The JS reference does
something stronger -- `scanCheckoutTree` in
`extensions/checkout-guard/guard.mjs` filters against
`nestedWorktreePrefixes`, the actual registered list from `git worktree
list --porcelain`, so it covers a worktree created *anywhere*, which
`git worktree add <anywhere>` makes easy. Its own comment says why: the
extension ships to projects that have no `.worktrees/` convention at all.

So a peer worktree outside `.worktrees/` is still reported by the Python
guard as this checkout's litter. This project's own convention puts every
worktree under `.worktrees/`, which is why the name-based fix covers the
fleet today, but the two implementations do not agree on the general case.
Adopting the JS approach in Python means a `git worktree list` call on the
handoff path and a decision about what an unanswerable call means -- the JS
answer is null, "no information", which callers already treat as not-clean.
That is the same territory as item 0020.

## Status

**The fix is already landed on `work/1`** — commits fe1b1a9, e29b723,
febd380 and b7d0d2d. This item is filed as the record of the defect and its
measurements, and is left `proposed` because closing it needs the product
owner's approval first (`operator backlog close 32` refuses without it, which
is the gate working as intended).

What landed: the guard exempts linked worktrees by *identity*, taken from
`git worktree list --porcelain`, and exempts the `.worktrees/` container
itself by name only when it is the container. Strays inside the container --
`.worktrees/scratch.txt`, `.worktrees/not-a-worktree/` -- are still reported,
and a modified tracked file under that path is still reported. A git that
cannot list worktrees yields "no information" rather than a clean tree.

Three review rounds across four models found four defects in the first two
drafts; each is described in the commit messages, and each has a test with a
positive and a negative control.
