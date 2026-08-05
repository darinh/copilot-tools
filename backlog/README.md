# Backlog

Open work in this repository, under version control.

Closed work is answerable from `git log`. Open work used to be answerable from
nothing: it lived in `~/.copilot/projects/{guid}/next-session.md`, which is
read-once and deleted at session start, and was carried between sessions as one
re-summarised sentence. That carry-forward is lossy by construction and nothing
could detect the loss. This directory is the durable fallback.

## Reading it

```
backlog list                 # one line per item
backlog list --status open
backlog show 1               # one item in full
backlog ready --explain      # what an agent may work, and why the rest is not
backlog check                # validate every item; non-zero on failure
backlog html --open          # a self-contained page, opened in a browser
```

## Writing it

```
backlog new --title "..." --evidence "..."   # files it as 'proposed'
backlog approve 3                            # the owner's act: proposed -> open
backlog scrum [--peek]                       # what changed since the last check-in
```

`backlog new` files an item **awaiting approval**, never `open` -- see
[the approval gate](#the-approval-gate). It refuses an item with no evidence,
and re-validates what it just wrote, because a tool that files items the
checker then rejects has moved the failure to whoever runs the suite next.

`backlog scrum` is the periodic check-in: commits since last time, backlog
files touched, what is ready, and what is waiting on you. The watermark it
measures from lives in `~/.copilot/projects/{guid}/backlog-scrum.json` --
outside the repository, because "since I last looked" is a fact about one
reader, and outside session state, because session state does not survive.
`--peek` reports without consuming the period, which is what a handoff draft
wants.

`backlog html` writes a single file with the data embedded in it, to a stable
path outside the checkout, and prints where it went. It is generated on demand
rather than committed, so it cannot go stale, and it embeds rather than fetches
because a page that fetched its items would render an empty backlog with no
visible error when opened from disk -- browsers treat every `file://` document
as an opaque origin, so the fetch is refused by CORS.

Everything above is also readable as plain files: one Markdown document per
item, rendered by GitHub without any tooling at all.

## One file per item

`backlog/0007-entra-id-endpoint-drift.md` -- four-digit id, kebab slug.

This is the main design constraint. Parallel agents work in separate worktrees
off the same `main`, and a single `BACKLOG.md` would put every add and every
close on the same lines of the same file, so every concurrent pair would
conflict. One file per item makes an add conflict-free by construction and a
close a small diff against one file.

Files that do not match `NNNN-slug.md` -- this README, for instance -- are not
items and are not validated.

## Format

```
---
id: 7
title: Declared endpoints do not match what is listening
status: open
opened: 2026-08-04
closed: 2026-08-11
commit: 4f2a1c9e8b7d6a5c4b3a2918f7e6d5c4b3a29187
spec: specs/003-windows-native-operator/spec.md
requirement: User Story 2 - Autonomous loop mode and handoff on Windows
---

## Evidence
## Why it matters
## Notes
```

| Field | Required | Meaning |
|---|---|---|
| `id` | always | Integer. Must equal the id in the filename. Unique. |
| `title` | always | One line. |
| `status` | always | `proposed`, `open`, `closed` or `rejected`. |
| `opened` | always | `YYYY-MM-DD`. |
| `closed` | when `closed` or `rejected` | `YYYY-MM-DD`. Absent while live. |
| `commit` | when `closed` | The SHA that closed it. Must resolve here. |
| `spec` | always | A path under `specs/` that exists, or `none`. |
| `requirement` | optional | Text that must occur in that spec file. |
| `blocks` | optional | The id of the approved item this one is blocking. |

**Front matter carries no inline comments.** A value is the rest of its line.
This is deliberate: YAML's comment rule would read `title: Fix issue #42` as
`Fix issue`, discarding the number, and item titles name defects. The visible
cost is that a `# open | closed | rejected` left in a copied template fails
loudly instead of parsing as `open` -- which is the better failure, and why
the vocabulary is documented here rather than in a comment.

## The approval gate

`proposed` means *filed, not approved*. It is the status every item an agent
files is born with, and it is the whole point of the vocabulary: an agent that
could file work as `open` would be approving its own work, and the queue would
fill with what the agent felt like doing rather than what the product owner
chose.

`backlog ready` lists only what is workable, so the gate is enforced where it
matters rather than merely documented. `backlog ready --explain` prints why
each other item is not in the queue.

### The escape hatch

A gate with no exception is a gate that gets bypassed. An agent that finds a
real defect halfway through an approved task has three options if `proposed`
is a hard stop -- stall, self-approve, or fix it silently outside the backlog
-- and all three are worse than a narrow legal path.

So: **a `proposed` item carrying `blocks: <id>` is workable while the item it
names is `open`.** The reasoning is that the owner already approved that work,
and this is the road to it.

Two properties keep it narrow, and both are enforced by `backlog check`:

- The target may not itself be `proposed` (R12). Otherwise two moves repeal
  the gate entirely -- file A, then file B blocking A, and B is workable on an
  authority A never had.
- The hatch lapses when the blocked item reaches `closed` or `rejected`.
  Nothing is being unblocked once the blocked item is finished, so the item
  goes back to needing approval like anything else.

### `spec` and `requirement`

`spec` is what ties an item to spec-kit. It is required on every item, and
`none` must be written out, so "this changes no specification" is a decision
somebody recorded rather than a silence that could equally mean nobody looked.

`requirement` is what makes that tie load-bearing. When set, the text must
actually appear in the named spec file, so renaming or deleting the
requirement turns the suite red and someone has to look at the item again. An
item pointing at a spec section that no longer exists is worse than one
pointing nowhere, because it reads as though it were checked.

## Closing an item

**Set `status`, `closed` and `commit` in the same change that does the work,
and update the linked spec there too.** A close landing separately from its
fix is a window in which the backlog is wrong.

The `commit` field cannot name a commit that does not exist yet, so in
practice the close is the last commit of the branch that does the work:

```
git commit -m "feat: the work"            # this is the SHA to record
git rev-parse HEAD                        # -> put it in the item's commit:
git commit -m "docs: close backlog item N"
git merge --no-ff                         # both land on main together
```

**Do not fill in the SHA and then `git commit --amend`.** The amend rewrites
the very commit whose SHA was just recorded, so the item names an object that
survives only as a dangling one until the next `gc` -- and `backlog check`
passes locally, in the one clone where that object still exists, while
failing for everybody else. That is the shape this project treats as its
worst: a check that reads as evidence and is not.

`rejected` means the item was considered and will not be done. It takes a
`closed` date and no `commit`, because nothing shipped -- demanding a SHA
there would force whoever rejects an item to invent one, and an invented SHA
looks exactly like evidence.

There is no `backlog close` and no `backlog reject`. Closing is tied to a SHA
that does not exist until the work commits, and rejecting is the owner's
judgement -- neither is a mechanical edit a tool should make on its own.

## What stops this rotting

`tests/test_backlog_conformance.py` runs `backlog_tool.check()` against this
directory on every test run. It asserts, among other things, that the
directory exists and is non-empty -- because without that, deleting it would
turn every other rule into a loop over an empty list and the suite would
report the backlog perfectly clean at the moment it stopped existing.

`tests/test_backlog_workflow.py` covers the writing side -- filing, approving,
the watermark and the check-in report -- with the same shape of control.

Every rule in that module has been proven to fail: each one was violated in a
temporary copy of this directory and the suite was observed going red. A guard
that cannot fail is worse than a missing one, because it reads as coverage.
