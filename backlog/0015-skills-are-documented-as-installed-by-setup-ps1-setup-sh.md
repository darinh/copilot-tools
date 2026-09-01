---
id: 15
title: Skills are documented as installed by setup.ps1/setup.sh, which install no skills at all
status: proposed
opened: 2026-08-05
spec: none
---

## Evidence

The product owner reported on 2026-08-08 that only `/operator-agents` was available to
him, having been told the `/operator-backlog-*` commands had shipped. Item 0005 is
closed (commit 8a5bb8c) and all three SKILL.md files exist under `skills/` and are
correct.

They had never been installed. `~/.copilot/skills/` on his machine held only
`code-intelligence` and `operator-agents` -- both evidently copied by hand at some
point, and `operator-agents` had since drifted from the repo (8721 bytes installed
against 9821 in `skills/`).

docs/skills.md says, for each of the five skills:

    **Install**: automatic -- `setup.ps1` / `setup.sh` install it to `~/.copilot/skills/`

and, above them: "`setup.ps1` / `setup.sh` copy them to `~/.copilot/skills/<name>/`, so
they are available in every project on the machine."

Neither script does this. `setup.ps1` does not contain the string "skill" anywhere.
`setup.sh` mentions skills only at lines 332-333, in an `echo` telling the reader to
copy one of them by hand into a project.

## Why it matters

0005 was closed on the strength of files existing in the repo. Nothing checked they
could reach the machine that runs the agent, so a capability the owner had asked for
specifically -- so that requests become work items instead of interrupts -- was absent
while its item read `closed`, and he discovered it by noticing the command was missing.
The install line in docs/skills.md is the only artefact that would have caught this, and
it asserts the behaviour instead of testing it.

## Done when

- `setup.ps1` and `setup.sh` each install every directory under `skills/` by
  enumerating that directory. Adding a sixth skill requires editing neither
  script, and a test proves it by adding a directory and asserting it installs.
- After running either script on a machine, `~/.copilot/skills/` holds a
  directory for every directory under `skills/`.
- A conformance test compares `docs/skills.md`'s install claim against what the
  scripts do, and goes red when the claim is false. It must be red against the
  current scripts before the fix, which is the control this item exists for.

## Not in scope

- The content of any skill.
- Installing skills for harnesses other than Copilot CLI.
- Removing a skill directory from `~/.copilot/skills/` that no longer exists in
  the repository. That is a deletion outside the checkout and wants deciding on
  its own.

## Risk

🟡 `setup.ps1`, `setup.sh`, `docs/skills.md`. Both scripts write outside the
repository into `~/.copilot/skills/`, so a wrong overwrite rule destroys a
hand-edited skill on a real machine -- which is exactly the state the
`operator-agents` drift left this machine in.

## Needs a decision before this can be worked

- Whether an install overwrites an installed copy that has drifted. Overwrite
  always, and a local edit is destroyed with no warning; leave and warn, and the
  drift that produced this item survives the fix. The two answers produce
  different scripts, so this cannot be left to implementation.

## Notes

Two separable fixes. (1) Have the setup scripts install every directory under `skills/`
by enumeration rather than by name, so a skill added later is not silently omitted --
naming them individually is what failed here even for the two that did get installed.
(2) A conformance test that checks docs/skills.md's install claim against what the
scripts actually do; the claim was false for all five entries, not just the missing
three. Also to be decided: whether an install should overwrite an existing copy, since
`operator-agents` had drifted unnoticed.

The three skills and the stale `operator-agents` were copied into `~/.copilot/skills/`
by hand on 2026-08-08 to unblock the owner. That is not a fix; it is the same manual
step that produced the drift.
