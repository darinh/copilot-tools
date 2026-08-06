# Managed-block audit (G1, G2)

Input: `AGENTS.md` as generated today — **4,364 words**, 13 sections, every
feature flag on. FR-6 says each candidate is exactly one of *guardrail*,
*procedure*, or *check*. This file does that classification, and D10 forbids
deleting any line in a commit that does not add its check, so the table names
the check for every deletion.

## G1 — What the harness can actually enforce

Before deciding a rule is checkable, it is worth writing down what "checkable"
means here, because the answer is narrower than "we could write a test" and
wider than the spec assumed.

| # | Mechanism | Fires | Sees | Can refuse? | Precedent |
|---|---|---|---|---|---|
| 1 | Extension permission hook | **before** a tool call runs | tool name, full arguments, cwd | **yes**, with a message the agent reads | `extensions/checkout-guard` denies blanket `git add -A` and an untracked-taking `git stash` |
| 2 | Extension post-tool hook | after a tool call | the tree, diffed before/after | no — but reports while the producer is still known | checkout-guard's stray sweep, including into the primary checkout from a worktree |
| 3 | Session-start context injection | session start | — | no; it writes text the agent reads first | checkout-guard's `sessionBriefing`; `operator`'s own launch preamble |
| 4 | `operator` subcommand refusal | when the agent runs the command | its own arguments and repo state | yes | `operator worktree new`'s refusals; `worktree_guard_backend.py` refusing an editable install from a worktree |
| 5 | Conformance test | at commit / CI | the tree | yes, by going red | `test_shell_bash32_conformance.py`, `test_skill_conformance.py` |

Three facts follow, and they drive the table below.

**Mechanism 1 is stronger than the spec assumed.** A permission hook sees the
*arguments*, so it can refuse `task` when the worktree is dirty, or an `edit`
whose path resolves outside the assigned tree. Both were written up as
guardrails because a skill cannot cover them — which is true, and irrelevant:
the trichotomy's third class is *check*, not *skill*. See the finding below.

**Mechanism 4 only fires if the agent uses the command.** A rule enforced
solely by `operator worktree new` is unenforced against `git worktree add`. So
"`operator` refuses it" is only a check when paired with 1 or 5.

**Git hooks were considered and rejected.** `.git/hooks` holds nothing but
samples today. A hook is per-clone, does not travel with the repository, and
`--no-verify` removes it. For G7 (no commit to `main`) mechanism 1 is strictly
better: it is already installed, it travels with `operator`, and it cannot be
bypassed by a flag the agent controls.

**What nothing can enforce.** A rule about what the agent should *conclude* has
no artifact. "An explanation that fits the evidence is not the explanation"
cannot be checked at any of the five points, because both the right and the
wrong reading produce the same tool calls. That is the guardrail class, and it
is small.

## Finding: the spec's own archetype is checkable

FR-6 introduces the guardrail class with "commit before delegating", arguing it
fires when the agent thinks *I'll have a reviewer glance at this*, not when it
thinks *time to follow the delegation procedure*. That reasoning is correct
about **skills** and does not survive contact with mechanism 1: the hook can
deny the `task` tool outright when `git status --porcelain` is non-empty, at
exactly the moment described, with a message naming the fix.

This does not weaken FR-6; it strengthens it. But it does mean the guardrail
class is smaller than the spec's own example implies, and the archetype should
move to the check class rather than be quietly reclassified. Recorded here
because it changes what G3–G13 have to build.

## G2 — The audit table

Word counts are per section, as generated with every flag on.

| § | Section | Words | Class | Disposition | Check that must land in the same commit |
|---|---|---|---|---|---|
| 1 | Git Worktrees — Always | 378 | mixed | keep ~60 | |
| | · never edit the primary checkout | | check | delete | **G3** hook denies `edit`/`create` resolving outside the assigned worktree |
| | · layout, primary-root resolution, both platforms' snippets | | procedure | delete | `skills/worktrees` (shipped, F1); `operator worktree new` computes the path |
| | · `/.worktrees/` in tracked `.gitignore` | | check | delete | **G4** `operator worktree new` writes it (there is no enroll); tests assert it was written, not doubled, not staged |
| | · one per branch, never nest, `cd` out before removing | | check | delete | `operator worktree new`/`finish` already refuse; test pins the refusals |
| | · leave worktrees you did not create alone | | check | keep 1 sentence | hook denies `git worktree remove` of a tree whose branch the session does not own |
| | · branches merge to `main`, there is no `develop` | | guardrail | keep (once, in §12) | — a negative fact; the failure is *inventing* a branch |
| 2 | Scratch Files — Never in the Checkout | 513 | mixed | keep ~45 | |
| | · the mandate itself | | check | delete | already enforced — `checkout-guard` mechanisms 1–3 |
| | · `mktemp`/`New-Item` snippets | | procedure | delete | session-start briefing already names the scratch dir |
| | · tell your subagents the same thing, **by name** | | guardrail | keep 1 sentence | — nothing can inspect a prompt the agent writes |
| | · the three-agents story (~220 w) | | rationale | move | `docs/rationale.md` (shipped, F2) |
| | · "an explanation that fits the evidence is not the explanation" | | guardrail | keep 1 sentence | — unenforceable by construction |
| | · `git status` will not save you (empty dirs) | | check | delete | `invisibleDirStrays` / `holdsNoFiles` already implement it |
| 3 | Handing a Worktree to a Subagent | 208 | mixed | keep ~55 | |
| | · **commit before you delegate** | | check | delete | **G3b** hook denies `task`/`agent` while tracked changes are uncommitted (see finding above) |
| | · forbid mutating git verbs by name in the prompt | | check | keep 1 sentence | hook denies `stash`/`reset --hard`/`checkout --`/`clean` from a subagent shell |
| | · verify the worktree before reading the findings | | guardrail | keep 1 sentence | — the failure is believing a report |
| | · the 454-line story and `git fsck` recovery | | rationale + procedure | move | `docs/rationale.md`; recovery commands to `skills/worktrees` |
| 4 | Project Configuration System | 106 | data | keep ~60 | ids and paths the agent cannot derive |
| | · "you must not offer to enroll this directory" | | check | delete | **G5** an AST scan proves no production module writes the catalog or mints a project id, so nothing *can* enroll |
| 5 | Session Handoff Protocol | 679 | mixed | keep ~35 | |
| | · on-session-start steps (unmerged work, read handoff, log) | | procedure | delete | mechanism 3 — `operator` prints them at launch, where the trigger is certain |
| | · never write `next-session.md` by hand | | check | delete | hook denies `create`/`edit` targeting the handoff path |
| | · the file-format template (~40 lines) | | procedure | delete | `handoff` writes the file; the agent never authors it |
| | · manual fallback, both platforms | | procedure | delete | contradicts the line above it; FR-8 allows one platform |
| | · superseded/ — what it means, why it fills | | rationale | move | `docs/rationale.md`; operator prints "N unread in superseded/" at launch |
| | · never prune `superseded/` unasked | | guardrail | keep 1 sentence | conformance test forbids an unlink under `superseded/`; the sentence covers the agent, the test covers the code |
| 6 | Session History | 131 | procedure | delete all | `operator session start`/`end` writes the row; the pasted DDL is a copy-paste error surface with no reader |
| 7 | Tracked Backlog | 734 | mixed | keep ~45 | |
| | · one-file-per-item rationale | | rationale | move | `docs/rationale.md` |
| | · front matter, status vocabulary, approval gate | | procedure | delete | `skills/backlog` (shipped, F1); `operator backlog new` writes the front matter |
| | · every item names a spec or says `none` | | check | delete | backlog conformance test (exists) |
| | · evidence is required | | check | delete | backlog conformance test (exists) |
| | · never seed an item you have not verified yourself | | guardrail | keep 1 sentence | — a rumour and a measurement are the same bytes |
| | · "a guard that cannot fire reads exactly like coverage" | | guardrail | promote | keep once, top level — it generalises past the backlog |
| 8 | Field Notes | 420 | procedure | keep ~35 | `skills/field-notes` (shipped, F1) |
| | · **volunteer them — don't wait to be asked** | | guardrail | keep | — the trigger is noticing, which is what a skill cannot be loaded on |
| 9 | Specification-Driven Development | 156 | procedure | keep ~25 | `skills/spec-driven` (shipped, F1); `/speckit-*` skills self-load |
| 10 | Operator — Peer Agents | 260 | mixed | keep ~50 | |
| | · what a peer is, vs a sub-agent | | guardrail | keep | — a concept needed *before* the decision to delegate |
| | · check your inbox at session start and before a handoff | | check | delete | mechanism 3 — launch preamble prints unread count |
| | · always pass your instance name to `operator inbox` | | check | delete | `operator inbox` refuses a nameless **consuming** read; `--peek` stays free |
| | · give a peer its own folder; "there is no enforcement" | | guardrail | keep 1 sentence, rewritten | **G6** `operator ownership check` is the enforcement, where a declaration exists: exit 1 if the branch left its subproject, 2 if it could not tell. The sentence should now say a boundary is enforceable if declared, not that none is. |
| | · delivery semantics, etiquette, worked example | | procedure | delete | `skills/peer-agents` (shipped, F1) |
| 11 | Parallel Agents | 559 | procedure | delete all | the four pasted SQL statements become one `operator` subcommand that claims atomically; that removes the copy-paste surface rather than relocating it |
| 12 | Branching Strategy | 65 | mixed | keep ~25 | |
| | · don't commit to `main` | | check | delete | **G7** hook denies `git commit` with `main`/`master` checked out |
| | · conventional commits | | check | delete | commit-message conformance check |
| | · there is no `develop` | | guardrail | keep | — the failure is inventing one |
| 13 | Common Pitfalls | 113 | duplicate | delete all | every line restates a rule above; under FR-6 a duplicate is "anything else" |

## Measured residue and the budget (input to G13)

| | Words |
|---|---|
| Generated today, all flags on | 4,364 |
| Classified *guardrail* or *generated data* — the keeps | ~435 |
| Section scaffolding, headings, the block's own markers | ~65 |
| **Projected managed block** | **~500** |

An ~89% cut. The budget is set on the **all-features-on** figure, because flags
default off (FR-8) and a sparse project's block is smaller still.

**Recommended budget: 700 words**, and generation *errors* above it. The
headroom is deliberate but small: enough that one feature can gain a sentence
without turning the build red, far too little for prose to re-accumulate. A
budget set at the measured 500 would make the next legitimate line an
emergency; one set at 1,500 would not be felt for two years, which is the same
as not having one.

### Measured again after G12

Flipping the flags off is *not* what shrinks the block on this machine, and
the measurement says why: all eight registered projects under
`~/.operator/projects/` have no `features.json`, so every one of them is
running on the defaults. "Default off" therefore changes what eight existing
repositories get, not what a hypothetical new one gets — which is why
`_values_for` refuses an unconfigured project rather than answering for it.

Two G12 changes move the number in opposite directions and both are small:

| | Words |
|---|---|
| Managed block with both platforms' commands | 4,358 |
| Selecting Windows only | 4,332 (−26) |
| Selecting POSIX only | 4,321 (−37) |
| The `CLAUDE.md` block (a separate file, not the budget) | 60 |

Selecting one platform is the first thing in this feature that removes words
from the managed block without removing a rule. It is a small number today
because the template brackets only four command pairs — the saving scales with
how many commands survive G10 and G13, not with anything measured here. The
700 recommendation stands; it was set on the all-on figure, and the platform
cut only widens the headroom.

## Exit code 2 does not collide (audited after G6)

Flagged as unaudited when `operator ownership check` was written; measured
now. Every pre-existing `return 2` in `copilot_operator.py` is a usage error
in which the command did not act at all — `_send_usage`, `_reply_usage`,
`_inbox_usage`, each printed alongside "Nothing was sent." or "No mail was
read." The convention is therefore **2 = no verdict was reached**, distinct
from **1 = a verdict was reached and it is no**.

The ownership guard uses 2 for an unreadable declaration or a failed `git
diff` — cases where it could not decide — and 1 for an explicit refusal. That
is the existing convention applied, not a new meaning for the same number,
and it is the distinction a hook needs: a hook that treats "could not decide"
as a pass is the failure mode the third code exists to prevent.

## What this changes for G3–G13

- **G3 gains a second guard** — deny `task`/`agent` on a dirty worktree — from
  the finding above. It is the same hook and the same `git status` call.
- **G7 uses mechanism 1, not a git hook.** Recorded so nobody re-litigates it.
- **G11's append-survival test is now load-bearing in a way it was not.** Once
  build/test/lint leaves the block (D11) and the block shrinks by 89%, almost
  everything a project relies on lives *outside* the markers.
- **Deleting §11 changes `test_instructions_template.py`.** That test pins the
  SQL the agents are told to paste. Under D10 the check moves with the rule, so
  the pin is replaced by a test of the new `operator` subcommand in the same
  commit — not deleted.
