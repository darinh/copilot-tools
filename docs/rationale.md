# Rationale

Why the rules in `AGENTS.md` and the skills exist. Read this when a rule looks
arbitrary. It is deliberately **not** loaded into every session: the evidence says
narrative content in a context file does not change agent behaviour, while
instructions do — so the stories live here and the imperatives live there.

---

## Why the instruction file is short

Gloaguen, Mündler, Müller, Raychev and Vechev, *Evaluating AGENTS.md: Are
Repository-Level Context Files Helpful for Coding Agents?* (arXiv:2602.11988,
ETH Zurich / LogicStar, Feb 2026, rev. Jun 2026). New benchmark of 138 issues from
12 repos that ship context files, plus SWE-bench Lite; four agent/model pairs; three
conditions (none / LLM-generated / developer-written).

Four findings drive the design here:

1. **Instructions are followed.** Measured directly: a tool named in the context file
   is used ~1.6 times per instance versus under 0.01 when unnamed. So everything
   written gets attempted — the null result is not an instruction-following failure.
2. **Overviews do nothing.** Measured as steps before touching a file in the golden
   patch: context files did not reduce it. Narrative and orientation content is inert.
3. **More instructions make tasks harder.** Files added 2.45–3.92 steps and 20–23%
   cost; reasoning tokens rose up to 22%. The authors conclude human-written files
   should carry only minimal requirements.
4. Developer-written files beat no file by ~4% on average; LLM-generated ones cost
   ~3%. Small, noisy, Python-only. Do not over-update on the sign.

Their corpus averaged **641 words per file across 9.7 sections**, max 2,003. The file
this package replaces was ~6,300 words.

Also relevant, from Claude Code's own documentation: instruction files are treated as
context rather than enforced configuration — to actually block an action, use a
`PreToolUse` hook. And files over ~200 lines consume more context and may reduce
adherence. Hence §6 of the spec: every rule that can be checked should be a check.

---

## Scratch files in the checkout

Three agents spent an evening diagnosing a working-directory bug in a test suite, on
the evidence of directories that kept appearing in a shared checkout. There was no
bug. The directories came from the agents' own review subagents reproducing defects —
nine of them from a single review round, named after a reviewer's loop variable. Two
runs and a grep would have refuted the theory in four minutes. Nobody ran them,
because the artifacts *felt* like proof.

The generalisable form is worth more than the rule: **an explanation that fits the
evidence is not the same as the explanation.** When you find evidence of a problem,
reproduce the mechanism before you explain the artifact — a plausible story will stop
you looking.

Two consequences:

- **Clean up before you finish, not later.** An artifact discovered afterwards has no
  provenance, and everything fits it equally well. That is what makes these
  expensive, not the disk space.
- **`git status` will not save you.** Git does not track empty directories, so an
  empty stray is invisible. A checkout can report perfectly clean with artifacts in
  its root.

The pull is toward the primary checkout specifically, because it is the tree that
*is* the project while a worktree feels like a copy of it. Agents who had already
read this rule left artifacts there three times in a single evening. Knowing the rule
is not what stops you; noticing that you are about to build a path relative to the
wrong root is.

---

## Commit before you delegate

A reviewer subagent ran `git stash` inside another agent's worktree and destroyed 454
lines of uncommitted work, mentioning it in passing in an otherwise clean review.
`git status` came back empty and `git stash list` was empty too — the stash had been
dropped. It was recovered only because the work had been `git add`-ed, so the blobs
survived as dangling objects:

```bash
git fsck --unreachable
git cat-file -p <blob>
```

A reviewer that runs `git checkout` or `reset --hard` instead leaves nothing to
recover at all.

This is also why worktree recovery never touches uncommitted contents: the same class
of loss, arriving via automation instead of a subagent.

---

## Handoffs: what the pile-up actually meant

Ten handoffs accumulated in one project's `superseded/` in a single day, written by at
least three distinct instances. A session that found that pile read it as ten dropped
contexts. That fits the evidence perfectly and is the wrong explanation — the mailbox
was keyed by **project** while the restart signal was keyed by **instance**, so peers
sharing a checkout all published to the same file. The ordinary case was not a missed
read at all; it was a peer publishing while another worked.

The response at the time was to add machinery: author stamps in the file, a lock, a
`superseded/` directory that is never pruned, and several hundred words of prose
teaching agents to tell the two cases apart.

All of that was scaffolding holding up a type error. A handoff is written in the first
person — *my* worktree, *I* claimed this — so it is instance-scoped by construction.
Key it by instance and the race, the directory, the author check and the prose all
disappear:

```
~/.agent-tools/projects/{projectId}/handoff/{instance}.md
```

The lesson generalises past this bug: **when a rule needs a lot of prose to explain
when it applies, check whether the state underneath it is keyed one axis short.**
Most of the length in the file this package replaces came from exactly that.

---

## Why worktree identity is not discoverable

Two traps, both silent:

- Inside a worktree, `git rev-parse --show-toplevel` returns the *worktree*. Using it
  for project identity mints a duplicate project id and splits state in two.
- "Walk up until you find `AGENTS.md`" finds the *nearest* file. In a monorepo that is
  a subproject's; inside a worktree it is the worktree's own tracked copy. Neither is
  the primary checkout.

Both are resolvable in code — `git worktree list --porcelain`, first record — and the
resolution never varies. That makes it a bad thing to teach an agent and a good thing
for operator to do once, before the session starts.
