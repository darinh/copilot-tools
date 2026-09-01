---
id: 22
title: The toolkit is named after one harness while aiming to be harness-agnostic
status: proposed
opened: 2026-08-05
spec: specs/004-operator-session/spec.md
---

## Evidence

Measured on this branch, 2026-08-05. The toolkit's own state directory is already
harness-neutral (`~/.operator`), but the code around it is not:

- `copilot_operator.py` -- the module every entry point loads
- `copilot_tools_version.py`
- `COPILOT_OPERATOR_HOME` -- the documented override env var
- `templates/copilot-instructions.md` -- named after one harness's file
- `COPILOT_DIR`, `COPILOT_LOG_DIR` and the console-script names
- the repository name itself

The incoming spec package (`copilot-tools-changes.zip`, 2026-08-05) proposed
going further and moving state to `~/.agent-tools/`. That part was declined and
recorded as D1/D2 in `specs/004-operator-session/spec.md`: `~/.operator` already
names the tool rather than the harness, and moving live project identity a third
time in one day risks the one file whose loss costs every project its id.

What is left is real, and it is a rename rather than a migration.

## Why it matters

The stated direction for this project is to be agent- and harness-agnostic. Every
identifier above says the opposite to the next reader, and identifiers are what a
new contributor -- human or agent -- reasons from.

It is filed rather than done because the blast radius is the whole tree: every
module, the console scripts, the install manifest, the env var (which needs a
deprecation path, not a rename), and the repo name. Bundled with functional work
it would hide any bug in it inside a diff nobody can review. It wants to be its
own change, on a quiet branch, with the suite as the only thing moving.

## Done when

- None of the identifiers listed in the evidence names a single harness, except
  where an external contract requires it -- `~/.copilot/` is the Copilot CLI's
  own directory and stays spelled that way.
- `COPILOT_OPERATOR_HOME` keeps working. A machine that sets it today is not
  broken by the rename; it is warned, on a path someone will see, and the
  replacement is documented.
- The diff contains no functional change. The suite is the only thing moving,
  and it is green before and after on the same commit range.
- An installed machine survives the change: `setup.ps1`/`setup.sh` from the
  renamed tree over an old install produces working console scripts, and the
  install manifest names the new ones.

## Not in scope

- Moving state out of `~/.operator`. Declined already and recorded as D1/D2 in
  `specs/004-operator-session/spec.md`; reopening it is a separate argument.
- Renaming the GitHub repository, which only the owner can do and which is not
  required for any of the above to be true.

## Risk

🔴 whole tree. `copilot_operator.py` is loaded by every entry point, the console
script names are what `setup` installs onto a PATH, and the env var is live
configuration on at least one machine. A missed rename does not fail here; it
fails on the next machine to run `setup`.

## Needs a decision before this can be worked

- **Whether to do it at all, and what the new name is.** Both are the owner's:
  the first is a judgement about whether the stated direction is still the
  direction, and the second is naming.
- **Whether the repository itself is renamed in the same pass or left for
  later.** These can be separated, and separating them keeps the code change
  reviewable.
