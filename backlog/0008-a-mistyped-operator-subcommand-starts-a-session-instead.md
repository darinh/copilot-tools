---
id: 8
title: A mistyped operator subcommand starts a session instead of erroring
status: proposed
opened: 2026-08-05
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
