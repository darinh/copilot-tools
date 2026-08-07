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

## Measured after G8, G10 and G13

The audit's estimate was that the block would land near 435 words of
guardrail residue. It delivered at **674 of a 700-word budget**, every flag
on, on both platforms — the template carries no platform brackets, so the two
renderings are byte-identical and the earlier 4,332/4,321 split is gone.
Adversarial review restored two rules the cut had dropped, taking it to
**694**; see the review section at the end of this file.

| | words | note |
|---|---|---|
| before (Windows) | 4,332 | measured after G12 |
| after (either platform) | 694 | 84% cut |
| of which generated | ~106 | `_header` ~30, `_configuration_section` ~76 |
| budget | 700 | 6 words of headroom after review |
| subproject block | 63 | of a 120-word budget |

Three things the estimate got wrong, all recorded because the *direction* of
the error is the useful part:

1. **The residue was bigger than 435.** Guardrails do not compress to their
   verbs; a rule with no procedure attached is a rule nobody can follow, so
   "keep the guardrail, drop the rationale" keeps more than the guardrail's
   own sentence. First honest draft was 756 words and had to be cut by 82.

2. **The headroom is thin, and that is a known cost.** The audit warned that
   a too-tight budget makes the next legitimate line an emergency. It stands
   as accepted: the three places a line can go instead are named in the error
   message, so the emergency has an exit that is not "raise the budget".
   Adversarial review then spent most of it — the block was 674 words on
   delivery and is 694 after two restored rules, having gone *over* at 704
   and been cut back rather than the budget raised. That is the mechanism
   working, and it is also the whole margin gone in one review round.

3. **The generated content is a fixed tax.** ~106 of the 700 are written by
   `render()` itself, so the template's real allowance is ~594. Trimming
   `_configuration_section` was as productive per word as trimming prose.

### The downgrade interaction, unresolved by design

An older build meeting a newer template keeps sections gated behind slugs it
does not know (`test_a_section_gated_behind_an_unknown_slug_is_kept`), so it
can render *over* budget and then refuse to generate at all. FR-8 says error
and that is what it does. The alternative — warn on a slug we cannot
recognise — reintroduces the warning nobody acts on, in the one case where
the block is provably wrong. Recorded here rather than fixed: the fix is to
ship builds in order, and a version skew large enough to trigger this is
already a problem the budget did not cause.

### D11 was satisfied by accident

Build/test/lint guidance never reached a rendered block, because it lived
under the one heading `render()` replaces. Nothing tested that and nothing
recorded it, so the property could have been removed by an edit to the
*generated* section with no test objecting. G10 made it enforced, and gave
the commands a home outside the markers (`VALIDATION_STUB`) so the rule is
followable rather than merely true.

## Adversarial review of the cut (three models, commit `b5a237a`)

Three reviewers ran in parallel over the staged diff. Eight findings, all
fixed in the follow-up commit; the numbers above were re-measured after.

### The one that mattered: a declared prefix could write outside the repository

All three found it independently, and `gpt-5.3-codex` reproduced an actual
write outside the checkout. `operator_ownership.normalize()` strips `.` and
empty segments but keeps `..` -- correct for the check it was written for,
since git never emits a `..` path and the segment is inert there. `G8b` was
the first code to turn a declared prefix into a *destination*, and
`root.joinpath(*prefix)` then honoured it.

Two guards, kept deliberately, because they catch different things:

- `read_declaration()` refuses `..` in an `owns` or a `contracts` path. This
  one names the file a human can edit, so the error is actionable.
- `_place_subprojects` resolves the repository root once and refuses any
  target whose own `resolve()` escapes it. This is what actually stops the
  write, and it is the only one that can catch a symlink or a junction --
  an escape the declaration file cannot express and so the first guard can
  never see.

The generalisable form: **a validator is only valid for the question it was
asked.** `normalize()` was right about comparison and wrong about
construction, and nothing in its name or its tests said which.

### Checking the generator is not checking the shipped file

`compose()` sits between `render_subproject()` and the bytes on disk, and
that gap is exactly how `VALIDATION_STUB` reached `CLAUDE.md` -- a file whose
entire content is one import line -- and every subproject file, without a
single FR-9 test objecting. `compose()` now takes `seed_validation`,
defaulting **off**, and only `_place_one` asks for the stub. The FR-9
assertions now read bytes back off disk after `_retire`.

### The subproject budget was charging data

A subproject legitimately owning ~30 directories overflowed 120 words, so
`operator projects` refused to set the repository up, and the only available
fix was to own fewer directories. A refusal with no action behind it is worse
than no check. `render_subproject` now charges prose only: two words per
`` - `path` `` line and one per inline contract are subtracted before the
comparison. `test_a_subproject_that_owns_many_paths_still_renders` pins that
input as legal; the budget is forced in test with a patched constant instead.

The budget exists to stop *writing* creeping back in. A path list is verified
data, and data was never the thing it was defending against.

### Two rules the cut lost

`gemini-3.1-pro-preview` caught both, and both are the failure D10 names:

- **"Don't commit directly on `main`"** vanished with no check added.
  Restored to the branching section.
- **`operator session end`'s flags** were referenced but never spelled, so
  the block told an agent to run a command it could not construct. Both verbs
  now carry their full argument list.

### Declined: "don't hardcode configuration values"

Universal engineering advice, true everywhere and specific to nothing here.
It survives in no tool, teaches no agent anything about *this* repository,
and the budget is a zero-sum allowance -- 700 words spent on this is 700 not
spent on the rules only this repository can state. Declining it is recorded
because a silent omission and a considered one read identically later.

### Disagreement, recorded not overruled

`gemini-3.1-pro-preview` objected that `test_the_budget_is_the_number_the_human_agreed`
is unfalsifiable: it asserts `WORD_BUDGET == 700`, which restates the source.

The objection is sound in general and wrong here, and the evidence is a
mutation run. `WORD_BUDGET = 70000` **survived** every other budget test,
because every one of them was written *relative* to the constant -- raising
it moved them all and the suite stayed green. That is precisely the "budget
becomes a warning, one exception at a time" failure FR-8 exists to prevent,
and the tautological-looking test is the only thing that kills the mutant.

The generalisable form: **a test that restates a constant is worthless unless
the constant is a decision.** 700 is a decision a human made. The test is
there to make changing it an argument rather than an edit.

### Mutation found two things review did not

Nine mutants over the changed predicates. Six died on the first run; the
three survivors were the interesting output.

**D10 was violated in reverse, and nothing could see it.** Two of the eight
findings were rules the cut had *dropped* -- "never commit to `main`" and the
spelling of `operator session end`. Restoring them satisfied review, and both
mutants survived: `never commit to it directly` → `commit to it freely` left
the whole suite green.

D10 says a rule is never deleted in a commit that does not add its check. The
symmetric half was never stated and is what actually bit: **a rule restored
without a check is one edit from gone again, and the second deletion is
silent because the first one was.** The cut removed the rule and nothing
objected, precisely because nothing had ever asserted it was there. Both now
have guards, and the command one reads the required flags off the tool's own
usage string rather than a list in the test, so a flag that becomes required
later fails here instead of in somebody's session.

**Defence in depth hides which layer is load-bearing.** The `..` refusal in
`read_declaration`'s `owns` loop could be deleted with the suite still green,
because the resolved-containment check caught the same input and the test
asserted only that generation failed. Nothing unsafe would have shipped --
and that is the trap. What the deletion loses is the *message* naming
`subprojects.json`, which is the entire reason the first guard exists; the
containment check can only say a path resolved somewhere bad, not which line
of which file to edit.

An end-to-end assertion cannot distinguish two guards that produce the same
verdict. The test moved to `read_declaration` directly, where only one of
them can answer.

### A conformance guard in this repository caught the security fix

`test_resolve_conformance` refuses a `Path.resolve()` call that does not cover
everything it raises -- `OSError` on a denial, `RuntimeError` on a symlink
loop, `ValueError` on an embedded NUL, and only the first is an `OSError`. The
containment fix added two such calls, both catching one of the three, and the
full suite went red on a file no reviewer had flagged.

The repository's existing answer is `project_paths.resolved_str`, whose
fallback is a lexical absolute path. It is the wrong answer *here*, and the
reason is worth keeping: a lexical path is **less** resolved than the truth,
so a containment gate built on it admits the target it could not check. Every
other caller compares two paths put through the same function, where less
resolved is still consistent. A write gate has no second path to compare
against -- it has a decision to make, and the safe direction is to refuse.
Both calls now catch all three and return a refusal.
