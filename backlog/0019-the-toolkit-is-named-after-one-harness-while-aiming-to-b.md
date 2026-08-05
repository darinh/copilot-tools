---
id: 19
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
