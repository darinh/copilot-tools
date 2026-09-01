---
id: 19
title: The floor scan cannot tell os.DirEntry.is_dir from pathlib.Path.is_dir
status: proposed
opened: 2026-08-06
spec: none
---

## Evidence

Measured 2026-08-05 while adding the checkout guard to `handoff`.

`tests/test_python_floor_conformance.py` reported two violations in
`handoff_tool.py`:

    handoff_tool.py:840  pathlib.Path.is_dir(follow_symlinks=)  (needs 3.13+)
        if not entry.is_dir(follow_symlinks=False):

Both `entry` values are `os.DirEntry` objects from `os.scandir`, where that
keyword has been legal since **3.6**. The scan is keyword-gated on the method
name alone -- `KEYWORD_GATED["is_dir"]["follow_symlinks"]` -- and never
consults the receiver for this class of construct, so every correct
`os.scandir` traversal in the project reads as a floor violation.

`_is_pathlib_receiver` exists and would answer correctly here (`entry` is an
`ast.Name` not in `PATHLIB_CLASSES`), but the keyword-gated path never calls
it.

## Why it matters

`os.scandir` plus `entry.is_dir(follow_symlinks=False)` is the standard,
correct spelling for a symlink-safe traversal, and it is what a reviewer will
suggest. A scan that rejects the right answer is the failure mode that gets a
scan switched off rather than fixed -- named in that file's own docstring.

The workaround costs a comment paragraph per site explaining why the obvious
spelling was avoided, which is a tax on every traversal written from now on.

## Done when

- A file containing the standard symlink-safe traversal -- `for entry in
  os.scandir(p): ... entry.is_dir(follow_symlinks=False)` -- produces no floor
  violation.
- A file containing `p = Path(x); p.is_dir(follow_symlinks=False)` still
  produces one. This is the negative control and it is the whole difficulty: the
  receiver is an `ast.Name` in both cases, so a fix that only widens the gate
  retires the check it is meant to preserve.
- `handoff_tool.py` can be written in the obvious spelling with no suppression
  comment and no explanatory paragraph.
- Both cases above are pinned by tests in
  `tests/test_python_floor_conformance.py`, and the second one is observed
  failing against the fixed scanner before it is relied on.

## Not in scope

- Other floor rules. Only the keyword-gated path for `is_dir` is in question.
- Rewriting `handoff_tool.py`'s traversal back to the keyword spelling. It uses
  `entry.is_symlink() or not entry.is_dir()` today, which is exactly equivalent
  on every input; changing it back is optional and proves nothing on its own.
- Reusing the existing `# floor-ok:` annotation. It claims the line is
  unreachable on the floor, which would be a false claim here, and a false
  suppression is worse than the false positive it silences.

## Risk

🟡 `tests/test_python_floor_conformance.py`. No shipped behaviour changes, but
this is a guard: a fix that is too permissive silently retires real coverage,
and a retired guard reads exactly like a passing one.

## Notes

Deliberately NOT fixed alongside the guard. Narrowing the keyword-gated path
to require a pathlib receiver would cost the scan every `Path` bound to an
ordinary variable -- `p = Path(x); p.is_dir(follow_symlinks=False)` -- which
is the shape it exists to catch, and whose receiver is an `ast.Name` too.

A fix has to separate "receiver is provably not a Path" from "receiver is
unknown", which is more than the current visitor knows. Two options worth
costing: recognise `os.scandir` loop targets specifically, or accept an
annotation distinct from `# floor-ok:` meaning "not the type you think" --
the existing one claims the line is unreachable on the floor, which would be
a false claim here.

`handoff_tool.py` now uses `entry.is_symlink() or not entry.is_dir()`, which
is exactly equivalent on every input and needs no keyword.
