---
id: 3
title: chore/land-peer-fixes is 15 commits ahead of main and has never landed
status: closed
opened: 2026-08-04
closed: 2026-08-05
commit: dd0f342f7faea206b677542adbe9a0e225c3f9a2
spec: none
---

## Resolution

Landed 2026-08-05 as `dd0f342`, at 18 commits ahead and still 0 behind. The
branch was read before it was merged, which is what the note below asked for
and what the arithmetic could not substitute for: it contained two defects,
and one of them was armed.

`tests/conftest.py::_is_a_multiplexer_spawn` ended in
`return name in _MUX_BINARIES and False`. Pinned to a constant the guard is
not weakened but absent, so every test in the suite silently regained access
to the developer's real tmux server -- the exact condition the branch was
written to end. It was also dangerous rather than merely wrong:
`test_the_refusal_names_the_test_and_the_argv` runs
`Mux(binary="tmux")._run("kill-server")` in the expectation of being stopped,
and unstopped that is a real `tmux kill-server`. Measured at review time on
this machine: seven live sessions, six of them peer agents and one the
reviewing session itself. The test asserting that the guard prevents
destruction was the thing that would have caused it.

The second defect is the one this item predicted in general terms.
`test_the_supervisor_does_not_spawn_a_process` poisoned every subprocess
spawn, and while the branch sat unmerged `main` grew the no-change progress
breaker, which fingerprints the repository with read-only `git` probes from
inside `run_loop_mode`. The merge was textually clean and semantically
broken. "Zero divergence is a perishable property" turned out to understate
it: divergence stayed at zero and the branch still broke, because what
changed was the meaning of a test rather than the text of a file.

Verified on Windows before merging: 2700 passed / 10 skipped against a
measured `main` baseline of 2649 / 9, 36/36 cross-platform, 112 node tests,
and a `tmux list-sessions` census taken either side of every run to prove no
peer session was killed. Three adversarial reviews (gpt-5.3-codex,
gemini-3.1-pro, claude-opus-4.6) returned no further findings.

## Evidence

Measured 2026-08-04 in this repository:

```
git rev-list --count main..chore/land-peer-fixes   -> 15
git rev-list --count chore/land-peer-fixes..main   -> 0
git log -1 --format=%ci chore/land-peer-fixes      -> 2026-08-04 02:29:09 -0700
```

Zero commits behind. The branch is a strict superset of `main`, so it merges
fast-forward with no conflict and no rebase.

## Why it matters

Fifteen commits of finished work are sitting outside `main`, and nothing
records that they are waiting. The branch was left by a session that was
killed before it could hand off (see item 0001), so its existence survived
only in git -- which is how it was found, and only because somebody went
looking.

Zero divergence is a perishable property. Every commit that lands on `main`
from any other branch is a chance for this one to acquire a conflict it does
not have today.

## Notes

This branch was surveyed but deliberately **not** reviewed or merged: the
session that found it was told to stop before reading the diff. Treat the
content as unreviewed. Landing it means reading 15 commits first, not
fast-forwarding on the strength of the arithmetic above.
