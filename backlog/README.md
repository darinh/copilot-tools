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
backlog check                # validate every item; non-zero on failure
backlog html --open          # a self-contained page, opened in a browser
```

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
| `status` | always | `open`, `closed` or `rejected`. |
| `opened` | always | `YYYY-MM-DD`. |
| `closed` | when `closed` or `rejected` | `YYYY-MM-DD`. Absent while open. |
| `commit` | when `closed` | The SHA that closed it. Must resolve here. |
| `spec` | always | A path under `specs/` that exists, or `none`. |
| `requirement` | optional | Text that must occur in that spec file. |

**Front matter carries no inline comments.** A value is the rest of its line.
This is deliberate: YAML's comment rule would read `title: Fix issue #42` as
`Fix issue`, discarding the number, and item titles name defects. The visible
cost is that a `# open | closed | rejected` left in a copied template fails
loudly instead of parsing as `open` -- which is the better failure, and why
the vocabulary is documented here rather than in a comment.

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

**Set `status`, `closed` and `commit` in the same commit that does the work,
and update the linked spec in that commit too.** A close landing separately
from its fix is a window in which the backlog is wrong, and the `commit` field
cannot name a commit that does not exist yet -- so the close goes in with the
work, using the SHA the work lands under. In practice: commit the work, then
`git commit --amend` after filling in the SHA, or close it in the merge commit.

`rejected` means the item was considered and will not be done. It takes a
`closed` date and no `commit`, because nothing shipped -- demanding a SHA
there would force whoever rejects an item to invent one, and an invented SHA
looks exactly like evidence.

## What stops this rotting

`tests/test_backlog_conformance.py` runs `backlog_tool.check()` against this
directory on every test run. It asserts, among other things, that the
directory exists and is non-empty -- because without that, deleting it would
turn every other rule into a loop over an empty list and the suite would
report the backlog perfectly clean at the moment it stopped existing.

Every rule in that module has been proven to fail: each one was violated in a
temporary copy of this directory and the suite was observed going red. A guard
that cannot fail is worse than a missing one, because it reads as coverage.
