---
name: backlog
description: Filing, approving, closing, and writing evidence for items in the tracked backlog/ directory. Load this skill before creating a backlog item, closing one, or deciding whether work belongs in the backlog or a spec.
---

# Backlog

Open work belongs in the repository. Handoffs and session logs are handover
mechanisms, not records — closed work is answerable from `git log`, open work is
answerable from `backlog/` and nowhere else.

## One file per item

`backlog/0007-short-kebab-slug.md` — four-digit id, kebab slug.

This is the design constraint, not a style choice. Parallel agents work in
separate worktrees off the same `main`; a single `BACKLOG.md` puts every add and
every close on the same lines of one file, so every concurrent pair conflicts.
One file per item makes an add conflict-free by construction and a close a small
diff.

```
---
id: 7
title: One line, imperative
status: proposed
opened: 2026-08-04
closed:
commit:
spec: specs/003-a-feature/spec.md
requirement:
---

## Evidence
## Why it matters
## Notes
```

Front matter takes **no inline comments** — a value is the rest of its line. YAML
would read `title: Fix issue #42` as `Fix issue`, silently discarding the number,
and a `status: open # or closed` copied from an example would be a status nobody
can parse.

## The approval gate

The vocabulary is `proposed` → `open` → `closed` | `rejected`, and the first
arrow is a human's.

- **`proposed`** — filed, not yet approved. An agent may file one of these
  unprompted; it may not work one. This status exists so the gate can be
  *expressed*: `open` alone conflates "somebody wrote this down" with "somebody
  decided it should be built", and a gate cannot be enforced against a
  distinction the data cannot make.
- **`open`** — approved, outstanding. The one status an agent may pick up freely.
- **`closed`** — shipped. The only status that names a commit.
- **`rejected`** — considered and declined. A decision, recorded as one, which is
  why it is a status and not a deleted file: a deleted item is indistinguishable
  from an item nobody ever filed, so the next agent to notice the same defect
  files it again and the decision is paid for twice.

```
operator backlog new      # always files as proposed
operator backlog ready    # the items you are actually allowed to work
operator backlog approve  # the product owner's act: proposed -> open
operator backlog close    # closed with the commit, or rejected
operator backlog list | show | check | scrum | html
```

**Use `ready`, not `list`.** `list` shows everything including `proposed`;
`ready` is the one that applies the gate.

## Fields

- `spec` — names a spec, or the literal `none`. Writing `none` out makes "this
  changes no specification" a decision somebody recorded rather than a silence
  that could equally mean nobody looked.
- `requirement` — optional. When set, the text must occur in the named spec, so
  renaming or deleting the requirement turns the suite red and somebody has to
  revisit the item. An item pointing at a spec section that no longer exists is
  worse than one pointing nowhere, because it reads as though it had been checked.
- `blocks` — the recorded exception. An unapproved item that blocks approved work
  may be worked; nothing else unapproved may be.

## Closing

Set `status`, `closed` and `commit` **in the same commit that does the work**, and
update the linked spec in that commit too. A close landing separately is a window
in which the backlog is wrong, and `commit` cannot name a SHA that does not exist
yet — so commit the work and amend, or close in the merge commit.

`rejected` takes a `closed` date and **no** `commit`, because nothing shipped.
Demanding a SHA there forces whoever rejects an item to invent one, and an
invented SHA is worse than a blank field because it looks like evidence.

## Evidence

**Evidence is what was measured** — a reproduction, a command and its output, a
mutation that ran green. An item with no evidence is a rumour, and a backlog of
rumours costs every reader the time it takes to find that out.

**Never seed an item you have not verified yourself.** Carried-forward lists go
stale: two of the four items this convention was first written for turned out to
be already fixed, and recording them would have created work that did not exist.

**A plausible mechanism is not a diagnosis.** If you can describe how a defect
works but have not demonstrated it, say which is which in the item. Handing over
an undemonstrated mechanism costs the next reader the time to disprove it, and
they will disprove it *after* believing it.

## Backlog or spec?

The backlog is the queue; `specs/` is the contract. An item is the right place for
work that is *not yet* specified. When it grows into a feature, run
`/speckit-specify` and point the item's `spec` at what that produced.

## Enforcement

`operator backlog check` runs the rules; the suite runs it too. Ids match
filenames and are unique, `status` is in the vocabulary, evidence is non-empty, a
closed item's SHA resolves in this repository, and `backlog/` itself is non-empty.
Without that last check, deleting `backlog/` turns every other rule into a loop
over an empty list and the suite reports clean at the moment it stopped existing.

**Prove each rule can fail.** Violate it in a temp copy and watch the suite go
red. A guard that cannot fire reads exactly like coverage.
