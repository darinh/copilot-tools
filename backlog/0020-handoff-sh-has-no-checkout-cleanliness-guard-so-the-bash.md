---
id: 20
title: handoff.sh has no checkout-cleanliness guard, so the bash rollback path still hands off a mess
status: proposed
opened: 2026-08-06
spec: none
---

## Evidence

Measured 2026-08-05. `handoff_tool.py` now refuses to write a handoff while
the checkout holds uncommitted, untracked or empty-directory strays.
`handoff.sh` is a standalone 276-line bash implementation -- it does not
delegate to Python (no `python`, `handoff_tool` or `exec` line in the file)
-- and contains none of that logic.

It is not installed fresh: `README.md` line 20 records it as "retained on
disk for rollback but no longer installed fresh by `setup.sh`", line 41 marks
it "rollback only", and `setup.sh:243` actively stashes a legacy `handoff`
symlink aside. `Get-Command handoff` on this machine resolves to
`handoff.exe`, the Python console script.

## Why it matters

The whole argument for putting the guard in the tool rather than in prose is
that the tool is the choke point every agent passes through. A second
implementation with no guard is a second door, and the one an agent reaches
by following `docs/operator.md:63`, which still documents symlinking
`handoff.sh` onto `~/.local/bin/handoff`.

The exposure is narrow -- rollback and stale installs -- but "narrow" is what
the empty-directory blind spot looked like before it cost three agents an
evening.

## Notes

Three options, unpriced:

1. Port the scan to bash. It must run on bash 3.2 (see
   `.github/copilot-instructions.md`), so no associative arrays, and
   `${a[@]+"${a[@]}"}` throughout. `git status --porcelain -uall -z` parsing
   in bash 3.2 is doable but the empty-directory traversal is not pleasant.
2. Make `handoff.sh` refuse outright when the Python tool is available, so
   there is one implementation with a guard rather than two without.
3. Delete `handoff.sh`. It is already unmaintained-but-tested; the tests
   would go with it, and the rollback story would need another answer.

Option 2 is probably right and cheapest, but it changes what "rollback"
means, which is the human's call rather than an agent's.

`docs/operator.md:63` should be corrected whichever way this goes.
