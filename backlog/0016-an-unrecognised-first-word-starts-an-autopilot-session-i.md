---
id: 16
title: An unrecognised first word starts an autopilot session in the current directory
status: proposed
opened: 2026-08-05
spec: none
---

## Evidence

Reported 2026-08-05: "when I run `operator` it fucking hangs and doesn't accept input.
I can't CTRL+C out. I have to close the terminal."

`~/.operator/trace.jsonl` records what preceded it:

    17:03:03Z  argv ["setup"]  cwd C:\Users\darin  tty true  source user  rc 0 after 4389ms
    17:03:13Z  argv []         cwd C:\Users\darin  tty true  source user  -- NO EXIT EVENT

and `~/.operator/operator.log`, one second into the first of those:

    [operator 2026-08-05 10:03:04] Starting single session: darin
    [operator 2026-08-05 10:03:04]   Work dir: C:\Users\darin
    [operator 2026-08-05 10:03:04]   Session #1 running (copilot pid=3148)

`setup` is not a subcommand, and was not a running instance.
`_subcommand_suggestions("setup")` returns `[]`, so the typo guard does not fire and
`_dispatch_command` falls through to `run_dispatch(["setup"])`. That starts a session
named after the current directory -- `darin`, from `C:\Users\darin` -- hands `setup` to
copilot as a prompt, and calls `MUX.attach()`, which takes the terminal.

Measured across plausible first words: refused are `ls`, `lst`, `jion`, `status`.
Passed through to a session launch are `setup`, `install`, `start`, `run`, `update`,
`init`, `config`, `doctor`, `stat`.

Both processes were dead by the time this was investigated. Closing the terminal is
how the owner got out.

## Why it matters

The guard''s protection is proportional to how close the mistake is to a real
subcommand, so a confidently typed wrong command -- the kind a person types when they
believe the subcommand exists -- receives none of it. The result is not an error
message but an autopilot copilot session, started in whatever directory the person was
standing in and attached to their terminal. Here that was HOME, which is the case the
operator menu already warns about in its own banner, and the only escape was closing
the terminal.

## Done when

Stated as invariants, because *how* the tie is broken is the open decision below
and a criterion naming one mechanism would settle it by the back door.

- A word typed in the belief that it is a subcommand does not silently become an
  autopilot prompt. `setup` is the measured case; `install`, `start`, `run`,
  `update`, `init`, `config`, `doctor` and `stat` behave the same way today and
  must end up wherever `setup` ends up.
- Whatever happens to such a word, the user can get out of it without closing the
  terminal.
- The documented shape still works. Every input pinned by
  `test_a_word_that_resembles_no_subcommand_is_left_alone` -- `operator implement
  the login fix` and its siblings -- still reaches a session, and that test is
  amended deliberately rather than deleted if the chosen tie-break changes what
  it asserts.
- The outcome is distinguishable from a hang: whatever is declined is declined
  visibly, rather than by attaching the terminal to something.

## Not in scope

- Redesigning the typo guard's suggestion logic. The item's finding is that the
  guard is working correctly and is not the thing that must change.
- The separate HOME/non-repository question below, unless the owner folds it in.

## Risk

🟡 `copilot_operator.py::_dispatch_command` / `run_dispatch`, and
`tests/test_operator.py`. The blast radius is every invocation of the CLI. The
failure mode of a wrong rule is refusing a legitimate prompt, which is loud; the
failure mode of no rule is the one already reported, which is a terminal the user
has to close.

## Needs a decision before this can be worked

- **How to break the tie between a one-word prompt and a mistyped subcommand.**
  The item names one candidate (a bare single word is refused; a multi-word
  prompt is not) and that candidate is cheap, but choosing it is a product
  decision about what `operator <prompt>` means.
- **Separately: whether launching in HOME, or in any directory that is not a git
  repository, should require confirmation however it was reached.** This is
  answerable independently and overlaps items 0034 and 0035, which both argue
  that costing an agent a session is worse than costing it a hint.

## Notes

This is not an untested path, which is the part worth keeping.
`tests/test_operator.py::test_a_word_that_resembles_no_subcommand_is_left_alone`
parametrises `implement`, `review`, `explain`, `summarize`, `investigate`, `resume`,
`triage` and more, asserts each returns no suggestion, and says why: "`operator
[copilot-args...]` is documented; a prompt may be any word. These are the shapes that
must keep working." `setup` is a member of that class. The behaviour is deliberate and
pinned by a passing test.

So the defect is not a missing guard but an ambiguity the guard cannot see: a one-word
prompt and a mistyped subcommand are the same input, and the tie is currently resolved
towards starting an agent. How to break that tie is a product decision, which is why
this is filed rather than fixed. One candidate that costs the documented shape nothing:
a single bare word is a poor prompt, so `operator implement the login fix` would keep
working while `operator setup` is refused.

Worth deciding separately: whether starting a session in HOME, or in any directory that
is not a git repository, should require confirmation however it was reached.
