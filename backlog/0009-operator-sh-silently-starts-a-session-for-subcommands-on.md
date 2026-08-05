---
id: 9
title: operator.sh silently starts a session for subcommands only the Python CLI implements
status: closed
opened: 2026-08-05
closed: 2026-08-05
commit: 75b7731
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

## Delivered

Landed in `75b7731` on `fix/operator-sh-python-only`.

The cheap correct half, as the notes above proposed: a word the Python
operator dispatches and this script does not is now refused by name rather
than answered with a session. `PYTHON_ONLY_SUBCOMMANDS` holds the thirteen --
`version menu projects stop-loop restart-loop stop-session forget send inbox
logs trace tabs restore` -- and a branch in `main()` prints where the
subcommand lives and exits 1 without touching any state. `operator help` grew
an `ELSEWHERE` section listing the same words, printed from the same variable
through an unquoted heredoc, because finding out by being refused means you
already ran it.

The message names three things, and the third is the one that is easy to
forget: what was refused, where it lives, and how to get past the refusal.
`operator --name NAME trace` is still how somebody whose Copilot prompt really
is the word `trace` says so -- the same escape hatch backlog 8's guard offers,
for the same reason. The entry point is named as a runnable command line only
when `${SCRIPT_DIR}/copilot_operator.py` is actually there; `operator.sh` is
installed on its own often enough that quoting a path to a file that does not
exist would send the reader somewhere worse than the bare filename does.

**Refusing rather than forwarding.** `exec python3 copilot_operator.py "$@"`
would have reached parity for free and was rejected: it makes this script a
proxy for the other one, which is exactly the product decision this item says
should be made deliberately, and it fails confusingly wherever the Python file
is absent or its dependencies are not installed. A refusal turns a silent
wrong action into a recoverable one and commits to nothing.

**Placement.** The branch sits inside backlog 8's guard, ahead of the distance
rules and ahead of the tmux/sqlite3/python3 checks -- `operator inbox` on a box
without tmux must say where `inbox` lives rather than "Error: tmux is
required", since `main` exits at that check and it would be the last thing the
reader saw. It sits *after* the positional join shortcut, so an instance
genuinely named `trace` is still attachable: refusing there would take away an
invocation that works today, which is the failure mode this repository keeps
paying for. Matching is exact rather than by distance, because a word the other
program implements is a fact rather than a guess. No word is in both
populations today, so the order is a statement of precedence and
`test_the_two_refusals_cannot_both_claim_a_word` is what keeps it one.

**The list is derived, not transcribed.** A hand-maintained copy of a set that
lives in another file is precisely the arrangement that let
`copilot_operator.py`'s own `RESERVED_WORDS` drift until it was missing `send`
and `inbox` -- and here a missing word is not a silent inconsistency, it is
this defect back again for that word. So
`test_the_python_only_list_is_what_the_two_dispatches_differ_by` computes
`python SUBCOMMANDS - shell SUBCOMMANDS` and requires equality: a subcommand
added to the Python operator is red here until it is either implemented in the
shell or refused by it.

That derivation leans on `SUBCOMMANDS` being what the Python dispatcher
actually answers, and only half of that was pinned. `test_every_subcommand_is_dispatched`
read the tuple and asked whether each word was answered; nothing asked the
reverse, so a word dispatched and left out of the tuple passed silently -- and
would now be a word the shell does not refuse, invisible on both sides.
`test_every_dispatched_head_is_listed` closes it, with a control that runs the
extractor over planted text containing the bug, because a regex that matched
nothing would have reported the whole file clean.

Verified: 8 hand-written mutations of the new branch, 0 survivors, each killed
by a distinct test -- branch deleted, a word dropped from the list, a word
invented, the path branch made unconditional, the help heredoc quoted, the
guard gated on `$# -eq 1`, the escape hatch dropped, and `exit 1` turned into
`exit 0`. Three of those were first written with ambiguous anchors and were
never applied at all; reported as skips rather than as kills, which is the only
reason they were noticed. Also `bash -n`, the bash 3.2 conformance scan, and a
real run of the shipped script against an isolated `COPILOT_OPERATOR_HOME`:
`inbox`, `send`, `trace` and `projects` each refused with exit 1, `list` still
lists. Three adversarial reviewers (gpt-5.3-codex, gemini-3.1-pro,
claude-opus-4.6) found nothing.
