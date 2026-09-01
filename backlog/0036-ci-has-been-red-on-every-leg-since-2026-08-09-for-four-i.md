---
id: 36
title: CI has been red on every leg since 2026-08-09 for four independent reasons, none of them visible from this machine
status: proposed
opened: 2026-08-16
spec: none
---

## Evidence

**CI has failed on every run since 2026-08-09.** Last green run is
`19c21ec1`, 2026-08-05T16:11:10Z. Ten consecutive failures since:

```
2026-08-16T17:48:59 failure a61e628f backlog(0034): launching into an uncatalogued repository
2026-08-09T22:44:12 failure 7fcc4cf4 Merge branch 'fix/categorize-strictness'
2026-08-09T22:32:33 failure 44106a89 Merge branch 'feat/hide-scaffolding'
2026-08-09T20:06:37 failure ba092f6d Merge branch 'fix/classification-rules'
2026-08-09T15:39:11 failure 567abdb4 Merge branch 'fix/viewer-fidelity'
2026-08-09T09:48:50 failure 3c2dbcf3 Merge branch 'test/row-behaviour'
2026-08-09T09:36:31 failure 1abdc2fd Merge branch 'fix/module-drift'
2026-08-09T08:38:06 failure 83136f22 Merge branch 'docs/deploy-second-machine'
2026-08-09T08:28:57 failure 068bc883 Merge branch 'docs/version-defect'
2026-08-09T08:26:23 failure 2f14fb46 fix: skill-context blocks were the second wrapper
2026-08-05T16:11:10 SUCCESS 19c21ec1 Merge branch 'docs/correct-0011'
```

The local suite on this machine is green — 4931 passed, 9 skipped on
2026-08-16 — so the whole of it is invisible from here. This machine is
Windows on Python 3.11; **no failing combination below includes 3.11 on
Windows.**

Failures in run `31962728045`, by leg. Four of the eight jobs fail, and they
do not fail for the same reason:

| test | legs |
|---|---|
| `test_handoff_checkout_guard.py::test_only_the_mount_point_tag_counts_as_a_junction[2684354563-True]` | ubuntu 3.10, ubuntu 3.12, macos 3.10, macos 3.12 — **every POSIX leg** |
| `test_supervisor_code_staleness.py::test_an_unreadable_record_is_unknown_not_unrecorded` | ubuntu 3.10, macos 3.10, windows 3.10 — **every 3.10 leg** |
| `test_supervisor_code_staleness.py::test_an_unreadable_record_does_not_report_a_start_instant` | ubuntu 3.10, macos 3.10, windows 3.10 — **every 3.10 leg** |
| `test_mux_isolation.py::test_the_supervisor_does_not_spawn_a_multiplexer` | macos 3.10, macos 3.12 — **macOS only** |

## Cause 1: `_is_junction` cannot answer True on POSIX

`handoff_tool.py:843`:

```python
return tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", object())
```

`stat.IO_REPARSE_TAG_MOUNT_POINT` is Windows-only. On POSIX `getattr` returns
a **fresh `object()`**, which is equal to nothing, so the comparison is
unsatisfiable and the function answers False for every input.

Reproduced directly on this Windows machine by deleting the constant to
simulate a POSIX `stat` module, against an entry carrying a genuine
mount-point tag:

```
windows (attribute present): True
posix   (attribute absent): False
```

The test is right and is doing exactly the job its docstring claims — "a POSIX
leg has no junctions to make, so without this the comparison itself is
exercised nowhere but Windows". It was added with the junction fix in
`185c531` (2026-08-06), three days before the first red run, and no CI ran in
between.

## Cause 2: the denial helper does not bite on Python 3.10

Both `test_an_unreadable_record_*` tests call `_unreadable()`, which patches
`builtins.open` and `io.open` and expects `Path.read_text` to be denied.
**On 3.10 it is not**, so the read succeeds and the function under test
answers `CODE_CURRENT` / a real timestamp instead of "cannot tell".

Reproduced locally in a 3.10 virtualenv:

```
FAILED tests/test_supervisor_code_staleness.py::test_an_unreadable_record_is_unknown_not_unrecorded
FAILED tests/test_supervisor_code_staleness.py::test_an_unreadable_record_does_not_report_a_start_instant
2 failed, 4 passed
```

The mechanism, measured on all three interpreters:

```
3.10.20 Path.read_text SUCCEEDED -> 'hello' (patch did NOT bite)
  pathlib has _NormalAccessor: True
3.11.9  Path.read_text denied (patch bit)
  pathlib has _NormalAccessor: False
3.12.13 Path.read_text denied (patch bit)
  pathlib has _NormalAccessor: False
```

3.10's `pathlib` routes file opening through `_NormalAccessor`, which binds
`io.open` **at import time**, so a later rebinding of the name is never
consulted. 3.11 removed the accessor and calls `io.open` at call time.

The helper's own docstring already tells this story about `os.stat` and about
`builtins` versus `io` — "it asserted the right answer and proved nothing" —
and this is the same defect one layer further down, on the one interpreter the
fleet does not run.

## Cause 3: the macOS multiplexer guard, observed and not diagnosed

`test_the_supervisor_does_not_spawn_a_multiplexer` fails on both macOS legs
and passes on every ubuntu and Windows leg. Not reproduced: there is no macOS
here. It is recorded so it is not mistaken for a symptom of causes 1 or 2,
which it cannot be — it fails on macos 3.12, where neither of those applies.

`AGENTS.md` describes an earlier bug in this exact guard, where
`os.path.basename` missed a Windows-style path on POSIX legs and the guard
spawned. Whether this is the same guard failing again is unestablished.

## Why it matters

A red CI is not one defect, it is the retirement of the instrument that finds
defects. Ten consecutive failures means no change merged since 2026-08-09 has
been checked by anything except a local run on one machine -- Windows, Python
3.11 -- and every combination that currently fails is one this machine cannot
see. That includes every safety fix the freeze exists to allow.

AGENTS.md already names this cost in its own words: "a green local suite is
evidence about one leg", and "before concluding anything from a local run,
check whether main is even green ... because you can otherwise spend an evening
diagnosing a failure you inherited". The file says that because it happened
before. It is now true again and has been for a week, and the next agent to run
the suite locally, see 4931 green, and merge will inherit four failures it had
no way to observe.

Cause 1 is also a live correctness defect, not only a red light. `_is_junction`
answers False for every input on POSIX, so on any non-Windows checkout the
handoff guard walks into junction-like reparse points it was written to refuse
-- which is the dangling-junction case that made a tree read as clean while git
was silent about it on stderr. The fleet runs on Windows today, so nothing is
being lost right now; a second machine on Linux or macOS would lose it silently
and the guard would report success.

## Done when

- Cause 1 is fixed and the fix is proven by a test that fails without it. The
  comparison must be meaningful on a leg with no `stat.IO_REPARSE_TAG_MOUNT_POINT`,
  which is the condition the current `object()` sentinel makes unsatisfiable.
- Cause 2 is fixed, and `_unreadable()` **asserts that it actually bit** before
  the test proceeds. That assertion is worth more than the fix: a denial helper
  that silently fails to deny turns two tests into assertions that pass for the
  wrong reason.
- A CI run is green on all eight legs, or every remaining red leg has an item of
  its own with its own evidence. "Green except for the known ones" without those
  items is how a red CI becomes permanent.
- The count is checked, not just the colour. windows-latest 3.12 is reported as a
  failed job and contributes no FAILED test line, so something outside the test
  run fails there -- possibly the stdlib-only smoke step at `ci.yml:75`. That is
  a fifth thing and is undiagnosed.

## Not in scope

- Cause 3. It needs a macOS leg to diagnose and must not be guessed at; it fails
  on macos 3.12 where neither other cause applies, so it is independent.
- Any change beyond the four causes. A red CI is a tempting place to land
  unrelated work and a terrible one to review it in.

## Risk

🟡 `handoff_tool.py:843` (`_is_junction`) and
`tests/test_supervisor_code_staleness.py`'s `_unreadable()` helper.

Cause 1 is also a live correctness defect, not only a red light: `_is_junction`
answers False for every input on POSIX, so on a non-Windows checkout the handoff
guard walks into the junction-like reparse points it was written to refuse. The
fleet runs on Windows, so nothing is being lost today; a second machine on Linux
or macOS would lose it silently and the guard would report success.

## Needs a decision before this can be worked

- **Whether the freeze permits landing causes 1 and 2.** `FROZEN.md` admits
  "fixes for defects that affect running sessions. Nothing else", and no failing
  combination is Windows-3.11, which is what this fleet runs. The argument for
  landing them anyway -- that a red CI disarms the freeze's own safety net -- is
  exactly the reasoning that erodes a freeze, and the item is explicit that this
  is the owner's judgement rather than an agent's.

## Re-checked 2026-08-31: CI has not run in fifteen days, because nothing has been pushed

    $ gh run list --limit 4 --json workflowName,conclusion,createdAt
    CI               failure  2026-08-16T17:48:59Z
    Commit identity  success  2026-08-16T17:48:59Z
    CI               failure  2026-08-09T22:44:12Z
    Commit identity  success  2026-08-09T22:44:12Z

    $ git log --oneline origin/main..main | wc -l
    9
    $ git log --oneline -1 origin/main
    a61e628 backlog(0034): launching into an uncatalogued repository says nothing

The newest run is still `a61e628`, the run this item was filed from. `main` is
**nine commits ahead of `origin/main`**, so the failure count has not grown --
and the sample has not grown either. Fifteen days of work, including three
backlog items and the changes that produced them, has been checked by nothing
but a local run on Windows 3.11.

This is worse than the item as filed, and in a way the item could not see. Ten
red runs is an instrument reporting failure. Zero runs is no instrument: nothing
is red, nothing is green, and `gh run list` looks superficially unchanged. **Push
before drawing any conclusion about CI's colour** -- the four causes above may
have been joined by others in those nine commits, and no evidence exists either
way.

(The second workflow, "Commit identity", passes on every run and is not part of
this item.)

## Notes

**This is filed rather than fixed, deliberately.** FROZEN.md admits "fixes for
defects that affect running sessions. Nothing else", and none of these four
failures affects the running fleet: it is Windows on 3.11, and no failing
combination is Windows-3.11. Causes 1 and 2 are each a small change, and the
argument for landing them anyway -- that a red CI disarms the freeze's own
safety net -- is a judgement for the owner, not an agent, because it is exactly
the reasoning that erodes a freeze.

**Suggested fixes, both unverified against CI because CI is the only place they
can be verified.**

* Cause 1: compare against the documented constant rather than a never-equal
  sentinel -- `getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)`, which
  is the same literal the test already falls back to. Real POSIX entries never
  reach the comparison, because `st_reparse_tag` is absent and the existing
  `except AttributeError` returns False first, so this changes no behaviour on
  a real filesystem and makes the comparison meaningful on every leg.
* Cause 2: deny at a layer 3.10 also goes through. Patching `os.open`, or
  chmod/ACL on the real file, or having the helper assert that it actually bit
  before the test proceeds. The last is worth doing regardless: a denial helper
  that silently fails to deny turns two tests into assertions that pass for the
  wrong reason, which is the failure mode this file is full of.

**Cause 3 needs a macOS leg to diagnose and should not be guessed at.** It
fails on macos 3.12, where neither other cause applies, so it is independent.

**Check the count, not just the colour, when this is fixed.** windows-latest
3.12 is reported as a failed job but contributes no FAILED test line to the
log, so something outside the test run fails there -- possibly the stdlib-only
smoke step at ci.yml:75. That is a fifth thing and is not diagnosed here.
