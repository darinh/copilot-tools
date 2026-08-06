---
name: worktrees
description: Creating, finishing, recovering, and safely delegating git worktrees. Load this skill before creating a worktree, merging one back, cleaning one up, or handing one to a subagent — not at session start.
---

# Worktrees

Operator assigns your worktree before your session starts. You need this skill
only when you are changing the *set* of worktrees, not to work in the one you
were given.

## Resolving the primary root

Inside a worktree, `git rev-parse --show-toplevel` returns the **worktree**. The
first record of `git worktree list --porcelain` is always the primary checkout,
from anywhere in the repo:

```bash
repo_root=$(git worktree list --porcelain | head -1 | cut -d' ' -f2-)
```

```powershell
$repoRoot = (git worktree list --porcelain | Select-Object -First 1) -replace '^worktree '
```

Use that for anything identifying the **project** — project directory, `.specify/`
init, config lookups. Using the worktree path instead mints a duplicate project id
and splits the project's state in two, silently.

It is also why "walk up until you find `AGENTS.md`" is not a way to find the repo
root: in a monorepo it finds a subproject's file, and inside a worktree it finds
the worktree's own tracked copy. Neither is the primary checkout.

## Layout

`<repoRoot>/.worktrees/<name>`, where `<name>` is the branch with `/` replaced by
`-` (`feat/login` → `.worktrees/feat-login`). `/.worktrees/` must be in the
**tracked** `.gitignore` — `.git/info/exclude` does not reach other clones.

## Lifecycle

Prefer the operator commands. They record the worktree against your work item, so
liveness recovery can find it later; a plain `git worktree add` leaves a tree
nobody can attribute to a claim.

```
operator worktree new     --instance NAME --item REF [--project SUB] [--branch NAME] [--path PATH]
operator worktree finish  --instance NAME [--item REF] [--into REF]
operator worktree recover [--preserve]
```

A checkout is 1:1 with a work item: `new` takes the claim and creates the tree
together, and releases the claim again if the tree cannot be made — so a failed
create never leaves an item claimed by nobody.

`finish` **refuses** a tree with uncommitted changes rather than tidying it, and
deletes the branch only when `--into` already contains it. Both refusals are the
point: the alternative to refusing is discarding work whose value only its author
knows.

`recover` reports and removes nothing. `--preserve` commits the uncommitted work
of an unclaimed tree, or one whose owner is provably gone, to a `wip/` branch.

Underlying git, if you need it:

```bash
git -C "$repo_root" worktree add .worktrees/feat-login -b feat/login
cd "$repo_root/.worktrees/feat-login"
# work, commit
cd "$repo_root" && git merge --no-ff feat/login
git worktree remove .worktrees/feat-login && git branch -d feat/login
```

## Rules

- One worktree per branch — git refuses two.
- Never create a worktree inside another worktree. Resolve the primary root first.
- `cd` out before removing.
- Leave worktrees you did not create alone. Another agent may be live in one.
- Worktree branches merge to `main`. There is no integration branch.

## Scratch files never go in a checkout

Every probe script, every reproduction written to confirm a bug, every scratch
copy kept for a diff. A script with a relative path writes wherever the process
happens to be, and for an agent that is almost always somebody's checkout.

```bash
scratch=$(mktemp -d)
```
```powershell
$scratch = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ([guid]::NewGuid()))
```

**Tell your subagents the same thing, by name.** They run their own shell in your
tree and you see only their output.

**`git status` will not save you** — git does not track empty directories, so an
empty stray is invisible to it. A checkout can report perfectly clean with
artifacts sitting in its root. Clean up before you finish, not later: an artifact
found afterwards has no provenance, and every explanation fits it equally well.
Three agents once spent an evening diagnosing a working-directory bug that did not
exist, on the evidence of directories their own review subagents had left behind.

## Handing a worktree to a subagent

**Commit before you delegate. Staging is not enough.** A commit is the only state
a subagent cannot casually destroy. Point reviewers at `git diff main...HEAD`, not
`git diff --staged`.

**Forbid mutating git commands by name**: `stash`, `checkout`, `reset`, `clean`,
`restore`, `rebase`, `commit`, `add`. "Do not write outside tmp" does not cover git
plumbing, which creates no new files.

**Verify the tree before you act on the findings.** If a subagent mentions in
passing that something of yours was lost, stop and check `git status` and
`git stash list` before reading anything else it reported. Work that was
`git add`-ed survives a dropped stash as dangling objects:

```bash
git fsck --unreachable
git cat-file -p <blob>     # grep for a string unique to your change
```

A subagent that ran `git checkout` or `reset --hard` instead leaves nothing to
recover at all. A reviewer once destroyed 454 lines this way and mentioned it in
passing in an otherwise clean review; see `docs/rationale.md`.

## Recovering someone else's worktree

Only after operator confirms the owner is gone — `operator worktree recover`
reports, it does not decide for you. Never `stash`, `reset`, `clean`, or delete.
If there are uncommitted changes, commit them to a `wip/` branch first (that is
what `--preserve` does), then continue. A crashed agent's uncommitted work is the
most expensive thing in the tree.
