---
id: 8
title: A mistyped operator subcommand starts a session instead of erroring
status: closed
opened: 2026-08-05
closed: 2026-08-05
commit: 2d0e84d
spec: none
---

## Evidence

2026-08-05, this machine, current main (6d2385c). Ran `operator ls` intending the listing (the subcommand is `list`). Instead of reporting an unknown subcommand, the CLI fell through to the session-start path and printed:

    Session 'copilot-tools' is already running.
    Stop it and start a new one? [y/N]
    Aborted.

Reproduced identically with `python copilot_operator.py ls`. Exit status 1.

The dispatcher in `main()` tests `head` against each known subcommand and treats anything unmatched as the argument to a start, so every typo is a start request. Nothing was destroyed here only because the instance already existed and the path happens to confirm before stopping a live session; the same typo against a *name that is not running* starts a real session unprompted.

## Why it matters

The failure is silent in the direction that costs the most. An unknown subcommand is a typing mistake, and the response to a typing mistake should be a message, not a state change. An agent scripting the operator non-interactively gets no prompt to abort at -- and this repository's whole point is agents driving this tool unattended.

It also degrades the tool's own error reporting: `operator ls` cannot ever tell you that `ls` is spelled `list`, so the discoverable path out of the mistake does not exist.

## Notes

Found incidentally while smoke-testing the supervisor code fingerprint against the real machine (merge 6d2385c); unrelated to that change and present well before it.

The fix is not simply 'reject unknown heads': bare `operator NAME` starting or joining an instance is a deliberate and useful shorthand. The distinction worth drawing is probably that a head which looks like a subcommand typo -- close to a known one by edit distance, or not a valid instance name, or matching no existing instance while stdin is not a tty -- should be refused with a suggestion, while an unambiguous instance name keeps working.

## Delivered

Landed in `2d0e84d` (three commits on `fix/operator-subcommand-typo`).

`_dispatch_command` no longer falls through to `run_dispatch` for a head that
looks like a mistyped subcommand. It prints what was meant, names the escape
hatch, and returns 1 without touching any state -- mirroring the refusal
`operator projects <typo>` already gave one level down. It applies whatever
follows the head, because `operator stopp NAME` is the more dangerous shape,
not a lesser one.

The item asked for the harder half of this, and it is where all the work went:
refusing a typo without refusing a prompt. `operator [copilot-args...]` is
documented, so every word claimed wrongly is a working invocation taken away.
Three rules, each modelling one class of mistake:

- **A prefix of at least three characters** -- a truncation (`sto`, `log`).
  Two characters is not evidence: `in`, `re`, `he` and `me` all prefix a
  subcommand and all start ordinary sentences.
- **One Damerau-Levenshtein edit** -- the single-slip model. Damerau because a
  transposition is one finger slip and two ordinary edits, and `jion`, `sedn`
  and `verison` are all transpositions.
- **A word another tool spells for the same job**, enumerated rather than
  guessed at. `ls` is not a typo of `list`; it is correct spelling from a
  different program, and no distance measure reaches it.

Together the first two give the property that keeps project names safe: a
prefix is never longer than what it prefixes and one edit cannot span a length
gap of two, so **a name two or more characters longer than a subcommand can
never be refused**. That is the shape of almost every real instance name.

Measured over 63 realistic prompts and 30 typos: 2 refused (`lint` and `end`,
each genuinely one keystroke from a subcommand and recoverable through the
escape hatch the message names), 0 typos missed.

`SUBCOMMANDS` is now the single source of truth and `RESERVED_WORDS` derives
from it. The hand-maintained second copy had already drifted -- `send` and
`inbox` were dispatched and missing from it -- and nothing broke, which is
exactly the silence that lets the next omission be a real one.

Verification: 3103 passed, 10 skipped (baseline 3017/10), 36/36
cross-platform. Two adversarial review rounds across three models, and the
first round was the valuable one: all three independently rejected the first
implementation, which scored `difflib` similarity ratios and refused ten of
thirty ordinary one-word prompts -- including `operator myproject`, the
documented quick-join, at 0.82. A guard against an unwanted session start that
instead refuses the documented shorthand has taken more away than it gave.

Round two found a length floor missing from the replacement (`operator in the
parser ...` was refused), two aliases that gave actively bad advice --
`quit`/`exit` pointed at `stop`, and bare `operator stop` kills every managed
instance without asking; `cat`/`tail` pointed at `logs`, which cannot display
a log at all -- and a property test whose inputs were five characters longer
than the boundary it claimed to pin, so widening the edit threshold left it
green. Each is fixed, and the predicate is now mutation-tested: five mutants,
all killed, where two survived before.

## Addendum, 2026-08-05 (later the same day): the POSIX half was never delivered

The section above says "the fix landed" without saying *where*. It landed in
`copilot_operator.py` only. `operator.sh` -- the program Linux, WSL and macOS
users actually run -- kept the old fall-through for another day, and its
hand-maintained `RESERVED_WORDS` kept the drift the Python side had just
stopped having. `bash operator.sh ls` still started a session. Closing the item
on one of two entry points is the same silence the item is about, one level up:
nothing failed, so nothing said so.

Ported in `e8ae2f3` (branch `fix/operator-sh-typo-guard`). The shell now
carries the same three rules, the same `SUBCOMMANDS`-derived `RESERVED_WORDS`,
and a Damerau-Levenshtein implementation over a flat indexed array, since bash
3.2 has neither associative nor two-dimensional arrays. The refusal sits after
the positional-join shortcut and *before* the tmux/sqlite3/python3 dependency
checks, so a typo gets a suggestion rather than "tmux is required".

The shell's `SUBCOMMANDS` is deliberately a strict subset of the Python one.
`operator.sh` implements none of `send`, `inbox`, `restart-loop`, `trace`,
`logs` or `tabs`, so naming them in a suggestion would advertise words that
themselves fall through to a session -- the defect reappearing inside its own
fix. A test pins the subset to what the `case` actually dispatches.

Verification: 3222 passed, 10 skipped (baseline 3135/10), 36/36
cross-platform, `bash -n` clean, and the real script run end to end under bash.
The shell predicate was differentially tested against the Python one over 1998
generated words -- every single-edit neighbour, prefix and alias -- with zero
disagreements. Eleven mutants, ten killed; the survivor was real, not
cosmetic: dropping the `$# -eq 1` gate changed nothing, because `operator ls
-la` was claimed in a comment and tested nowhere. A test now covers it. Three
adversarial reviews found nothing, and all three independently re-derived the
one equivalent mutant left standing.

Two traps worth recording, both of which produced confident wrong answers
before they were caught. Python's `subprocess` with `encoding=` translates
`\n` to `\r\n` on Windows, so a bash probe fed through it reads every word
with a trailing carriage return; the first differential run reported 1967
mismatches that did not exist. And this repository's own shell harness stubs
*every* top-level function, which silently included the function under test --
the first end-to-end probe reported no refusals at all from a guard that
worked perfectly.
