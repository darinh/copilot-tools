---
id: 4
title: Two feature branches have diverged from main and need a keep-or-drop decision
status: open
opened: 2026-08-04
spec: none
---

## Evidence

Measured 2026-08-04 in this repository, as ahead/behind against `main`:

```
fix/flaky-loop-resilience   4 ahead, 20 behind, last commit 2026-08-01 20:12:54 -0700
fix/mail-unreadable-inbox   7 ahead, 20 behind, last commit 2026-08-01 20:09:50 -0700
```

Both are three days stale and both are 20 commits behind, so neither merges
cleanly as it stands. Contrast `chore/land-peer-fixes` (item 0003), which is 0
behind.

## Why it matters

Eleven commits of work whose value nobody has assessed. The cost of deciding
rises with the divergence: each is 20 commits behind now, and the rebase gets
worse every time `main` moves.

The decision itself is cheap and has three outcomes -- rebase and land, cherry
pick the part worth keeping, or delete the branch. What is expensive is
leaving it undecided, because an undecided branch looks identical to one
somebody is still working on, so nobody touches either.

## Notes

Neither branch has been read. The names suggest they overlap with work already
on `main` (`fix/flaky-loop-resilience` in particular, given that `main`'s tip
is "bound an unattended loop that has stopped making progress"), so check for
duplicate fixes before rebasing either.

If a branch is dropped, close this item with `status: rejected` for that half
only if the other has already been handled -- otherwise leave the item open
and note the decision here.
