---
id: 27
title: operator --version prints a hardcoded 1.0.0, so it cannot confirm which version a machine is running
status: proposed
opened: 2026-08-09
spec: none
---

## Evidence

Measured on this machine, 2026-08-09, immediately after a successful
`setup.ps1` deploying toolkit 1.4.0:

    PS> operator --version
    operator 1.0.0

    PS> python -m pip show copilot-tools
    Version: 1.4.0
    Editable project location: C:\Users\darin\repos\copilot-tools

Two version numbers exist and only one of them moves:

- `copilot_tools_version.py:14` -- `__version__ = "1.4.0"`. Its module
  docstring line 1 reads "The one place the toolkit's version is written
  down."
- `copilot_operator.py:87` -- `__version__ = "1.0.0"`, a second, unrelated
  literal. Line 67 of the same file already imports the first one as
  `TOOLKIT_VERSION`.

`operator --version` prints the second (`copilot_operator.py:7762`).
`TOOLKIT_VERSION` is what actually gets recorded and displayed elsewhere:
the install manifest (`copilot_operator.py:2557`), generated project
instructions (`copilot_operator.py:6380`), and the manifest read-back
(`copilot_operator.py:7617`).

`git log -L 87,87:copilot_operator.py` shows the literal has not been
touched since 7ce67b8, 2026-07-27, "feat: native Windows support via
cross-platform Python operator". The toolkit has released 1.1, 1.2, 1.3 and
1.4 since.

The existing test cannot detect this. `tests/test_operator.py:976` asserts
`op.__version__ in capsys.readouterr().out` -- it scores the output against
the same constant the code prints, so it passes for any value including a
permanently stale one. It is the "unfalsifiable by its input" shape: the
assertion holds for every implementation.

## Why it matters

`operator --version` is the obvious way to answer "did my deployment
actually land on this machine?", and it is the one command that cannot
answer it. It reports 1.0.0 on a machine running 1.4.0 and would report
1.0.0 on a machine running anything else, so it is not merely unhelpful --
it actively confirms a wrong belief. A second machine deployed from a stale
clone looks identical to a correctly deployed one.

This is the defect class this repository keeps finding in other guises: a
check that returns a confident wrong answer. `--version` does not fail, does
not warn, and does not decline to answer. It answers.

The cost is highest exactly when it matters most -- multi-machine deploys
and bug reports, where "what version are you on" is the first question and
the reply is always the same wrong number.

The docstring on `copilot_tools_version.py` states the intended invariant in
so many words: one place the version is written down. There are two.

## Done when

- `operator --version` prints the version of the toolkit that is installed. On
  this machine today that is `1.4.0`, not `1.0.0`.
- The test compares the printed output against `copilot_tools_version.__version__`
  -- a constant the printing code does not define -- and a control proves the
  assertion goes red when the two differ. The present test scores the output
  against the same literal the code printed, so it passes for any value; a fix
  that leaves that shape in place has fixed the number and not the defect.
- Either there is one version literal in the tree, or there are two and each is
  documented as to what it versions and printed where it belongs.

## Not in scope

- Changing the version number itself, the release process, or how `setup`
  records the manifest.

## Risk

🟢 `copilot_operator.py:88` and its `--version` handler, plus the test at
`tests/test_operator.py:976`. Nothing downstream reads
`copilot_operator.__version__`; `TOOLKIT_VERSION` is already what the manifest,
the generated project instructions and the manifest read-back use.

## Needs a decision before this can be worked

- **Is `copilot_operator.__version__` meant to be a CLI-interface version
  distinct from the toolkit release?** If yes, it is not stale and the defect is
  that `--version` prints the wrong one of two things nothing distinguishes. If
  no -- which `copilot_tools_version.py`'s own "the one place the toolkit's
  version is written down" suggests -- the literal is deleted and
  `TOOLKIT_VERSION` re-exported. The two answers produce different diffs.

## Still true on 2026-08-31

    PS> operator --version
    operator 1.0.0

The literal has moved one line, to `copilot_operator.py:88`, and is otherwise
untouched. Three weeks and no release has changed it, which is the point: it
cannot change, because nothing that ships a version reads it.

## Notes

Not fixed on discovery because the fix has a decision in it that should be
made deliberately, not in passing.

If `copilot_operator.__version__` is meant to be a *CLI interface* version,
distinct from the toolkit's release version, then it is not stale and the
defect is that `--version` prints the wrong one of the two, and that nothing
says which is which. If it is not meant to be distinct -- which the
"one place" docstring suggests -- then the literal should be deleted and
`TOOLKIT_VERSION` re-exported.

Either way the test must change: asserting output contains the constant the
code just printed can never catch a stale constant. It should compare
against `copilot_tools_version.__version__`, so that a divergence between
the two is what turns it red.

Candidate fix, if the second reading is right:

    # copilot_operator.py
    from copilot_tools_version import __version__   # replaces line 87

and a test asserting `operator --version` output contains
`copilot_tools_version.__version__`, plus a control proving the assertion
fails when they differ.

Found while verifying a deployment, not by a failing test.
