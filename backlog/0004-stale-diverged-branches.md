---
id: 4
title: Two feature branches have diverged from main and need a keep-or-drop decision
status: closed
opened: 2026-08-04
closed: 2026-08-05
commit: dd0f342f7faea206b677542adbe9a0e225c3f9a2
spec: none
---

## Resolution

Decided 2026-08-05: **keep both**, and both are now in `main`. Neither needed
the rebase this item was budgeting for, because neither had to be landed
directly -- `chore/land-peer-fixes` (item 0003) already contained both as
merge commits, so landing that one branch resolved all three at once.
`git branch --merged main` now lists `fix/flaky-loop-resilience` and
`fix/mail-unreadable-inbox`, and `git branch --no-merged main` is empty.

The note below guessed that `fix/flaky-loop-resilience` might duplicate work
already on `main`, given that `main`'s tip was "bound an unattended loop that
has stopped making progress". Read rather than inferred, they turned out to be
unrelated and complementary: the breaker on `main` decides when to *stop* a
loop that is making no progress, while the branch stops the unit suite driving
the developer's real multiplexer. They do interact, though, and not in the
direction the guess pointed -- the breaker's git probes are what broke the
branch's supervisor test on merge. See item 0003.

The 20-behind measurement was also read too pessimistically. It was taken
against the two branches directly; the copy of each inside
`chore/land-peer-fixes` was 0 behind, which is why no rebase was ever needed.
When the same work exists on more than one ref, "behind" is a fact about the
ref you happened to measure, not about the work.

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
