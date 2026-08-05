---
id: 9
title: operator.sh silently starts a session for subcommands only the Python CLI implements
status: proposed
opened: 2026-08-05
spec: none
---

## Evidence

2026-08-05, this machine, main at e8ae2f3. `operator.sh` dispatches seven
subcommands -- `stop`, `list`/`sessions`, `report`, `ingest`, `help`, `join`,
`reload`. `copilot_operator.py` dispatches those plus `send`, `inbox`,
`restart-loop`, `trace`, `logs`, `tabs` and `projects`.

Under Git bash, with `run_single_session` stubbed so the probe cannot start a
real session (the harness in `tests/test_operator_sh_typo_guard.py`), each of
these reaches the session-start path, taking the subcommand itself as the
instance name:

    inbox        -> single / inbox        / copilot-tools
    send         -> single / send         / copilot-tools
    restart-loop -> single / restart-loop / copilot-tools
    trace        -> single / trace        / copilot-tools
    logs         -> single / logs         / copilot-tools
    tabs         -> single / tabs         / copilot-tools
    projects     -> single / projects     / copilot-tools

    list         -> handled (exit 0)
    ls           -> refused by the typo guard (exit 1)

So `operator inbox copilot-tools` on POSIX does not read a mailbox and does
not report an error: it starts a copilot session named `inbox` and passes
`copilot-tools` through as a copilot argument.

Backlog 8's typo guard cannot help here, and deliberately does not try: it
only suggests words `operator.sh` itself implements, because suggesting `inbox`
would name a word that also falls through. The guard's subcommand list is a
strict subset for exactly this reason, pinned by
`test_every_alias_points_at_a_subcommand_this_script_implements`.

## Why it matters

The mail commands are the ones that matter. This repository's own conventions
tell every agent to run `operator inbox <instance>` at the start of work and
before writing a handoff, and to answer peers with `operator send`. An agent
following those instructions on Linux or macOS does not get an error -- it
gets a *new copilot session named `inbox`*, which is both a wasted session and
a mailbox that stays unread while looking exactly like an empty one.

It is the same failure direction backlog 8 was about, and it survived that
item's fix because the fix and the item were both written against the Python
entry point.

## Notes

Two shapes are worth distinguishing. `send`, `inbox`, `restart-loop`, `trace`,
`logs`, `tabs` and `projects` are real features the shell does not implement;
implementing them there is a large job and possibly the wrong one, since
`operator.sh` is documented as superseded-but-maintained rather than as the
preferred entry point.

The cheap correct half is to *refuse* them: a word the Python operator
dispatches and this script does not should be met with a message naming the
Python entry point, not with a session. That is a list and a branch, and it
turns a silent wrong action into a recoverable one. It also composes with the
existing guard rather than fighting it -- the guard's `SUBCOMMANDS` stays the
set of things that work, and the new list is the set of things that exist
elsewhere.

Whether `operator.sh` should reach parity at all is the product decision
underneath this, and it should be made deliberately rather than by accretion.
