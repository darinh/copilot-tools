---
id: 17
title: operator projects retire leaves AGENTS.md untracked in every project it writes
status: proposed
opened: 2026-08-05
spec: none
---

## Evidence

`operator projects retire` writes an AGENTS.md into every catalogued project and does
not track it. Neither `retire_user_instructions` nor `_report_retirement` mentions
git; nothing in copilot_operator.py runs `git add` or `git commit` for the file it
just wrote.

Measured across the eight catalogued projects on 2026-08-05, after a retire run:

    TRACKED      discord-invite-manager, snes-ghosts, ac-unreal
    untracked    scripts, book-translator, prism, copilot-tools, finances

The three tracked ones are tracked because an agent working in them happened to commit
the file afterwards. Nothing in the command produced that difference.

The run in copilot-tools was immediately flagged by the checkout-guard extension as a
suspected scratch artifact -- "1 new untracked path appeared in the checkout" -- with a
prompt to delete it. That is the correct behaviour for an untracked file appearing
unannounced in a checkout, and it was aimed at the repository's own conventions file.

## Why it matters

An untracked file is one `git clean -fd` from gone, and agents in these repositories
are instructed to keep checkouts free of untracked artifacts -- so the conventions file
is both deletable by routine hygiene and indistinguishable from the scratch that
hygiene is aimed at. It also never reaches anyone who clones, which means "give each
repository its own AGENTS.md" is true only on the machine that ran the command. The
observed 3-of-8 split is the symptom: the command has no opinion about the outcome, so
the outcome is whatever happened next.

## Notes

Committing automatically is the wrong fix and should be rejected on its own merits.
The command would be running `git commit` in eight repositories whose state it does not
own -- any of them may be on `main` (which this project''s conventions forbid committing
to directly), mid-merge, mid-rebase, on a detached HEAD, or carrying unrelated staged
work that would be swept into a commit the user did not write. This repository already
has a worktree belonging to another agent.

`git add` is the middle ground and probably the right one: a staged file survives
`git clean -fd`, appears in `git status` as intentional rather than as debris, and
creates no commit. Its cost is a polluted index -- a later `git commit` by the user
picks the file up -- which is a smaller and more visible failure than silent deletion.

The minimum, whatever is chosen: `_report_retirement` should say per project that a
file was written and whether git knows about it. It currently reports only that the
global file was retired, so the untracked copies are invisible in the one output a user
reads to confirm the command worked.

Consider also whether the five already-untracked projects should be reconciled by
whatever fix lands, rather than left to the same chance that produced the split.
