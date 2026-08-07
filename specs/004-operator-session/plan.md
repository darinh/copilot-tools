# Implementation Plan: Operator Session, Assignment, and Liveness

**Spec**: `spec.md` | **Branch**: `feat/operator-session`

## Approach

Phases are ordered so each is independently mergeable, and so that no phase
leaves a rule written-but-unenforced or enforced-but-undocumented.

The one ordering constraint that is not negotiable comes from FR-6/D10:
**enforcement lands paired with the shrink.** The package's own suggested order
put generation before enforcement; that would delete rules from `AGENTS.md`
before their checks existed, opening a window in which the rule is neither
written nor enforced and nothing in the suite would show it.

| Phase | Delivers | Depends on |
|---|---|---|
| A | `specs/004-operator-session/` | — |
| B | Handoff keyed by instance (FR-1) | A |
| C | `work_claims` + liveness cascade (FR-3) | A |
| D | `operator session start` / `end` (FR-2, FR-5) | B, C |
| E | `operator work` / `worktree` / `backlog` / `reply` | C |
| F | Skills + `docs/rationale.md` | E |
| G+H | Enforcement paired with generation (FR-6…FR-9) | F |

## Phase B — handoff re-key

`handoff_tool.py` owns the path today and carries the machinery built on top of
the mis-keying. The order inside the phase matters: **re-key first, delete
second**, so that the deletion is a deletion of something already unreachable
rather than a behaviour change bundled with a path change.

Migration moves every existing `next-session.md` and `superseded/*` into
per-instance files. Where the writing instance cannot be determined from the
file, it is parked under a reserved name rather than discarded — an unreadable
provenance is not a reason to drop context.

`handoff.sh` must stay bash 3.2 clean: no associative arrays, and
`${a[@]+"${a[@]}"}` uniformly.

## Phase C — liveness

`boot_id` first, because it makes the unplanned-reboot case cost nothing: a
differing boot id is conclusive with no timeout, since nothing from the previous
boot is running.

The fourth signal is deliberately *not* conclusive. Heartbeat age with the first
three inconclusive means something unusual — a hung process, a clock problem —
and the correct output is a report, not a steal.

`operator_mux.py` already knows how to ask whether a mux session exists; reuse
it rather than adding a second spelling of the same question.

Delivered as `work_claims.py` (the claim table and its check-and-write, one
`BEGIN IMMEDIATE` transaction) and `operator_liveness.py` (boot identity,
process presence and start token, and `assess()`). The mux question is asked
through a new `Mux.session_present()` beside `has_session()` rather than a
second implementation: `has_session()` answers two-valued, which is right for
the create path — trying again costs nothing — and wrong here, where a failed
call read as "absent" reports a live agent dead.

## Phase D — session lifecycle

`session end` must be atomic across three effects (handoff, claim release,
session-log close). Partial failure leaves a recoverable state, never a released
claim with no handoff — that combination loses the context *and* hands the
worktree to somebody else.

Delivered as `operator_session.py` (`resolve_assignment`, `start_session`,
`end_session`, `describe`, `assignment_values`) and `operator session
start|end` in `copilot_operator.py`. The session log lives in the same
`work.db` as the claims, deliberately: the log close and the claim disposal
share one `BEGIN IMMEDIATE` transaction, and a transaction cannot span two
sqlite files without attaching one to the other — a lock-ordering problem
bought to keep two files apart for no reason anybody named.

FR-5 was amended during this phase: the claim is **retained** by default and
released only with `--done`. See FR-5 for why an unconditional release
contradicts FR-2's resume path.

The supervisor loop resolves the assignment and opens the session log itself,
before it builds the preamble (`_loop_work_db`, `_loop_start_session`), and
refreshes the claim on a throttled poll while the session is up
(`_loop_heartbeat`, `HEARTBEAT_INTERVAL`). Two things follow from putting it
there rather than asking the agent to do it. The answer is in the agent's
first token instead of being re-derived every session from rules that would
have to stay in its context permanently — which is the whole point of the
feature. And liveness is reported by the party that reads it from the process
table: an agent asked to heartbeat itself does so right up to the moment it
stops being able to, which is the only moment the answer mattered.

The loop does *not* call `session end`. That is the agent's own last act — the
loop learns of it through the restart marker it already watches, and a
supervisor that ended sessions on the agent's behalf would be writing handoffs
with nothing to say.

## Phase E — commands

The claim store judges nothing and the liveness cascade changes nothing. This
phase is where the two meet, and it exists so that the decision to move a work
item away from the instance holding it is made in exactly one place.

Delivered as `operator_work.py` (`agent_identity`, `preserve`, `request`,
`release`, `heartbeat`, `listing`, `reclaim`) with `operator work` in
`copilot_operator.py` (E1, E2), `operator backlog` delegating to
`backlog_tool.main` (E3), and `operator worktree` as `operator_worktree.py`
(E4). E5 is `operator reply` plus mail delivered by `operator session start`.

Two properties are load-bearing rather than convenient.

**A claim records only signals confirmed true at the moment it is written.**
Each of the four identity fields can conclude DEAD on its own, so writing a
mux session that is not running — or the pid of the transient `operator`
process that is about to exit — does not merely lose information, it
manufactures proof that the owner is gone and the next sweep hands a live
agent's worktree to somebody else. `agent_identity` probes each signal and
writes `NULL` where it cannot confirm one, which the cascade reads as "no
evidence" rather than "evidence of death"; the claim then rests on boot id and
heartbeat, which is LIVE while fresh and STALE afterwards. This is why
`operator_session.runtime_identity` is not reused here: its `pid=None` default
means "use `os.getpid()`", which is the one value a CLI must never record.

**Reclaim preserves before it reassigns and never issues a mutating git
verb.** Preservation copies `.git/index` to a temp file, points `GIT_INDEX_FILE`
at the copy, and builds the branch with `write-tree` / `commit-tree` /
`branch` — so the working tree, the real index and `HEAD` are byte-identical
afterwards and only a new ref appears. `stash`, `reset`, `clean`, `checkout`,
`restore`, `rm` and `mv` are absent from the module by construction, asserted
by a source scan beside the behavioural tests, because the behavioural ones
can only cover the paths a test reached and FR-4's promise is about every path
including tomorrow's. Failure to preserve refuses the reclaim rather than
proceeding: the two unknowns are not symmetric, and reassigning a tree whose
state could not be read hands somebody an unexplained diff — after which the
first thing they reach for is one of the verbs this module never issues.

Ordering follows from the same asymmetry: no-such-claim, already-mine,
instance-busy, then the cascade, then preservation, then a compare-and-swap
against the whole row the verdict was computed from. Every refusal that can be
decided from the database is decided before any git work, so a reclaim that
was going to be refused never leaves a branch behind. STALE is refused, not
stolen — the cascade's whole point is that "I could not confirm it is alive"
and "I confirmed it is dead" are different answers.

The compare-and-swap is on the row and not on the owner's *name*, because the
name is the one thing that does not change when a dead-judged owner comes
back: `work_claims.reassign` gained an optional `expect_claim`, compared
inside the same `BEGIN IMMEDIATE` as the update, so a refreshed heartbeat, a
new pid, a new boot id or a moved worktree all refuse. Adversarial review
found that hole, then found the hole in the fix: `TS_FORMAT` has no
sub-second field, so a refresh inside the same whole second as the stored
stamp leaves a byte-identical row and a value comparison reads it as "nothing
happened". `work_claims` therefore carries a monotonic `revision` column,
bumped by `claim`, `heartbeat` and `reassign` alike, so a write is visible to
the comparison whether or not it changed any value a reader would notice.

A worktree recorded in the *other* platform's path syntax refuses too, and
that one is worth naming because it fails silently in the dangerous
direction: `Path(r"C:\repos\app")` on POSIX is a relative path — a backslash
is an ordinary filename character there — so a presence probe reports the
worktree absent, preservation concludes there is nothing to save, and the
reclaim reassigns a tree it never looked at. The primary defence is recorded
evidence rather than inference: a claim stores the writer's `os.name` in a
`platform` column, and `reclaim` refuses outright when that differs from the
running one. Inference cannot be made to work at the overlaps — `/srv/app` is
a legal spelling on both platforms, so no shape test can classify it — which
is why the syntax test is the fallback for claims written before the column
existed, not the rule. That fallback, `_foreign_path`, asks `ntpath`, which is
pure syntax and understands both spellings, and takes the platform as a
parameter so both branches are exercised on every CI leg rather than each leg
testing only its own half. It answers "foreign" for a leading `/` on Windows
before consulting the drive, because `ntpath.splitdrive("//home/dev")` reports
the UNC share `//home` and a drive test alone would call a POSIX path native.

**The approval gate is checked where it can be bypassed, not only where it is
advertised.** `backlog ready` filters the queue, and a queue is a thing an
agent can simply not consult; `close` is the write that turns "I filed this"
into "this shipped". So `close_item` asks `why_not_workable` — the gate's
single owner, not a second copy of its reasoning — and refuses anything that
answer keeps out of the queue. That it consults the same function is what
makes the `blocks` hatch work here for free: an item an agent was lawfully
allowed to work is an item it can lawfully close, and the alternative is an
agent with a finished job and no legal way to record it, which is how a status
field gets hand-edited. `--reject` sits outside the check on purpose, since
requiring approval before a rejection would mean approving something in order
to decline it, and it refuses a `--commit` rather than dropping one — a SHA
against a rejection reads as though something had shipped, which is the class
of wrong this repository treats as worst: a record that looks like evidence.

**A worktree is created by the same call that takes the claim, and the claim
is taken first.** The keying model makes a worktree 1:1 with a work item, so
`operator worktree new` does both or neither: it requests the item, then
probes the path, then runs `git worktree add`, and every refusal after the
claim calls a compensating release of the claim it took microseconds earlier.
The order is the load-bearing part. Probing the filesystem first reads as the
cheaper check, but "the directory is absent" is not a reservation — two agents
can both observe it and both proceed — whereas the claim is the one step with
a compare-and-swap behind it. Reversed, a second agent asking for an item
somebody already holds is told `path-exists`, which names the wrong problem
and sends it to delete a live agent's checkout. The compensating release is
safe for the same reason the ordering is: it releases *this call's own* claim,
identified by owner, not whatever claim happens to be there.

The branch defaults to `work/{item}` rather than `feat/`, `fix/` or `docs/`,
because choosing among those from an item reference is a guess that then ships
in the branch name; `--branch` takes the answer when the agent has one. The
directory is `<primaryRoot>/.worktrees/<branch with separators flattened>`,
resolved from the *primary* checkout — `git rev-parse --show-toplevel` inside
a worktree names the worktree, so using it would nest one inside another.

`finish` is the asymmetric half. It refuses when the caller is not the owner,
when no worktree is recorded, when the claim was written on the other
platform, when the cwd is inside the target, when the tree is dirty, when the
directory cannot be read, and when git fails — and it releases the claim
*last*, so any failure leaves the claim held, which is the recoverable
direction. It removes with `git worktree remove` and never `--force`, prunes
only on evidence of *absence* rather than on a `None` probe, and deletes the
branch only when `git merge-base --is-ancestor` proves it merged, with
`git branch -d` rather than `-D`. `recover` reports and removes nothing; its
`--preserve` reuses `operator_work.preserve`, and only for trees whose owner
is UNCLAIMED or provably DEAD — STALE is reported as itself, for the same
reason `reclaim` refuses it.

Adversarial review found three defects in the first cut of this, and the first
is the one worth carrying forward as a rule. `work_claims.claim` *resumes*
rather than refuses when the same instance asks for an item it already holds —
deliberately, so a restarted agent can pick its work back up — which means a
`new` that finds its own live claim did not create it. The unconditional
compensating release therefore had a case where it released a claim the call
had never taken, handing the agent's own checkout to the next sweep as an
ownerless tree. The compensation is now conditional on having actually taken
the claim, and `new` refuses `already-yours` up front when it can confirm a
recorded checkout is still standing. The general form: a compensating action
is only safe against the state the compensating call itself created, and
"I asked for X and got X" does not establish that.

The second is the platform-syntax failure wearing new clothes. A relative
`--path` was recorded verbatim, but every git call here is `git -C <root>`,
where git reads a relative worktree path as root-relative, while a presence
probe resolves the same string against the process cwd. Recorded verbatim the
two disagree the moment anyone runs the command from inside a worktree — and
the dangerous half is `finish`, which probes the wrong place, finds nothing,
concludes the tree is already gone, prunes the registration and releases the
claim over a live checkout. Paths are anchored to the root on the way in, and
legacy relative claims anchored on the way out.

The third is a boundary rather than a bug, and is stated rather than defended:
`git status --porcelain` does not list ignored files, so a tree holding only
ignored content reads as clean and `worktree remove` takes it. Gating on
`--ignored` would refuse on any tree that has run a build or a test suite,
which is every tree, and a command that refuses always is answered with the
force flag this module deliberately does not have. So `finish` still removes
ignored content, exactly as git's own `worktree remove` does, but it names
what it is taking first — with a positive control asserting the note stays
absent when there is nothing to report, because a note that appears either way
trains its reader to skip it.

E5 replaces the polling mailbox, which had two separate costs. The first was
that receiving required the agent to remember `operator inbox`, and forgetting
it produced no signal at all — an agent with unread mail behaved exactly like
an agent with none. Delivery therefore moves to `operator session start`,
which every session runs anyway; the supervisor loop had done this for its own
launches since the mail module was written, so what E5 adds is the same
guarantee for a session started any other way. The second cost was that
answering meant restating both addresses, which is why the old hint was a
`send` command. `operator reply` resolves them instead, and is deliberately
sugar over `send_message` rather than a parallel implementation: the
live-versus-queued decision, the unknown-recipient refusal and the archive
record all carry earned comments, and a second copy is a second place for them
to drift.

Both new lookups refuse rather than default. That asymmetry is the design.
`operator inbox` may fall back to the directory's name because a wrong guess
there costs a peer's mail, which is recoverable from the archive; a reply
carries an assertion its recipient will act on, so a wrong guess signs another
agent's name to it or sends it somewhere it was never owed. For the same
reason "nobody has written to you" and "your mailbox could not be read" are
different messages with different exit codes — they license opposite actions,
and collapsing them tells an agent its peer never wrote when the truth is that
nothing could be read. The reply hint keeps naming `--to` even though the
command would default it, because the hints are printed once per message and
the default is wrong precisely when a batch arrives from several peers at
once.

Three reviewers read the first commit and found seven defects between them,
all now fixed with a test and a mutant each. Two are worth recording because
they would have survived any amount of re-reading. The mailbox id was
sanitized twice at session start — `instance` there is already a safe id, and
wrapping it in `Instance(...)` again turned `beta.test` into
`beta-test-2e02bd` and then `beta-test-2e02bd-1ac43e`, a mailbox nothing ever
writes to: every name needing sanitization would have reported no mail, for
ever, silently. And the session-start header used box-drawing characters,
which raise `UnicodeEncodeError` on a cp1252 console — after `consume` has
archived the messages, making it the one crash that destroys exactly what it
was reporting. This repository already has a console-encoding conformance
test; it did not cover this print, which is an argument for adversarial review
rather than against the test.

A third finding is a rule rather than a bug: a reviewer found a `--queue` test
that could not fail, because every test in that file runs against a
multiplexer with no live sessions, so the mail queues whether the flag is
honoured or dropped entirely. It now asserts against a live recipient, with a
flag-removed control beside it that must produce the opposite outcome.

## Phase F — skills and rationale

The five skills were rewritten against this repository rather than copied from
the package. Two of them would have been wrong as delivered: the worktree skill
named `operator worktree` commands without the refusals they actually make, and
the backlog skill described a three-status vocabulary with no approval gate,
where this repo has four statuses and the gate is the whole point of
`proposed`. A skill is loaded at the moment an agent has decided to act, so it
is the *worst* place for an aspirational description — the measurement in
`docs/rationale.md` is that a tool named in context gets used, which makes
naming a command that does not exist more expensive than saying nothing.

`peer-agents` replaces `operator-agents` (D7), but it is not the package's
64-line draft either. The retired skill carried a mail delivery table, the
refusal rules for unknown recipients and unknown flags, the etiquette list and
a worked example — all earned, all still true. Those moved across and were
corrected for E5: mail is delivered rather than polled for, `operator inbox` is
described as the audit trail it now is, and the draft's bare
`operator reply "<text>"` became `--instance NAME`, because no ambient instance
name exists anywhere in this system and the skill would otherwise name a form
that only works in an environment nobody sets up.

The feature *flag* keeps the slug `operator-agents`. It is persisted in every
enrolled project's `features.json`, and a rename would read as unset — silently
re-enabling the feature for anyone who had deliberately turned it off. D7 is
about the skill; renaming persisted state is D2's deferred class.

F3 is the check that keeps the rest honest, and it is stated as a path rule
rather than a ban on the retired name. The ban was written first and needed an
exemption for prose describing the retirement, at which point it is not a rule
any more. `skills/<name>` either resolves or it does not.

## Phase G+H — the audit

Before the template changes, produce a table classifying every candidate line as
guardrail / procedure / checkable. The expected residue is small; the two lines
that look genuinely irreducible are *commit before you delegate* and *forbid the
mutating git verbs by name when you do*, because both fire while the agent is
thinking "I'll just have a reviewer glance at this".

The budget number is set from what the residue measures, not guessed in advance,
and then made to fail the build.

**Delivered (G1, G2): `specs/004-operator-session/audit.md`.** Both predictions
above turned out to be half right, and the half that was wrong is worth the
paragraph.

The residue *is* small: 4,364 words measured across 13 sections, ~500 of them
classified guardrail or generated data. That is the number G13 sets the budget
from, and the recommendation is 700 — enough headroom that one feature gaining
a sentence is not an emergency, far too little for prose to re-accumulate. A
budget of 1,500 would not be felt for two years, which is indistinguishable
from not having one.

But *commit before you delegate* is not irreducible, and neither is its
companion. The argument for both was that a skill cannot cover them, because
loading a skill requires already knowing you need it — which is true, and does
not reach the conclusion. The trichotomy's third class is **check**, not skill,
and an extension permission hook sees a tool call's arguments *before it runs*:
it can deny `task` outright when `git status --porcelain` is non-empty, at
exactly the moment described, with a message naming the fix. The same hook can
refuse a subagent's `stash`, `reset --hard` or `checkout --` inside a worktree,
which is better than asking the parent agent to remember to write the sentence.

So the guardrail class is smaller than the spec's own example implied. What
survives it is a short list with a shared shape: rules whose right and wrong
readings produce **identical tool calls**. "Tell your subagents by name" —
nothing can inspect a prompt the agent writes. "Never seed a backlog item you
have not verified" — a rumour and a measurement are the same bytes. "Volunteer
a field note" — the trigger is noticing. "An explanation that fits the evidence
is not the explanation" — both readings run the same commands. Those cannot be
checked at any of the five enforcement points, and that is the test, rather
than whether a skill happens to fit.

Three sections survive at **zero** words. Session History pastes DDL and two
SQL statements that `operator session start`/`end` already executes. Parallel
Agents pastes four more, including a `BEGIN IMMEDIATE` claim transaction that
is a copy-paste error surface with no reader — it becomes one atomic `operator`
subcommand, which removes the surface rather than relocating it to a skill.
Common Pitfalls only restates rules above it, and under FR-6 a duplicate is
"anything else".

Git hooks were considered for G7 and rejected, recorded so it is not
re-litigated: `.git/hooks` holds nothing but samples, a hook is per-clone, it
does not travel with the repository, and `--no-verify` removes it. Mechanism 1
is already installed, travels with `operator`, and cannot be turned off by a
flag the agent controls.

**Delivered (G3, G7): three new denials in `checkout-guard`.**

They went into the existing extension rather than a new one because the
parsing they need already lives there — `gitInvocations` for "is this command
really a commit", `primaryCheckoutRoot` for "where is the other checkout" —
and a second copy of "which repository does this command actually address" is
the duplication this repository has already paid for once.

The tests found two defects before either rule shipped, and both are the shape
worth recording. `outsideWorktreeDecision` resolved the *target* path but not
the candidate roots, so a caller passing an unresolved root got `null` for
everything: correct for today's only caller, which resolves them, and one
refactor away from a guard that silently never fires. And it returned the
first containing root rather than the most specific — but the convention nests
worktrees at `<primary>/.worktrees/<name>`, so the primary contains every one
of them, and every write into a peer's worktree was reported as landing in the
primary. The path in the message would have been right and the tree named
beside it wrong, which sends the reader to the wrong checkout to clean up.

The delegation rule counts tracked changes only. Untracked files survive a
`stash` or a `reset --hard`, they are already this guard's other subject, and
counting them would make every session holding one scratch file undelegatable
— a guard that cries wolf gets switched off, which is the failure mode that
costs the most.

Seventeen mutants, all killed. Five of them initially reported "never ran"
rather than passing, because the driver's anchors were written with `\n`
against a file checked out with CRLF. That is the whole reason the driver
distinguishes *unanchored* from *killed*: five silent no-ops would have read
as a clean sheet, and one of the two genuinely surviving mutants — a detached
`HEAD` reported as a branch named `HEAD` — was only visible once they ran.

**Delivered (G4): `/.worktrees/` at `operator worktree new`, not at enroll.**

The plan said "at enroll" and there is no enroll. Nothing in first-party code
writes a row to `~/.operator/projects/catalog.csv`; `operator projects` browses
projects that are already registered, and registration is agent behaviour
driven by instructions. So the trigger this task named does not exist as a code
path, and the choice was between inventing one and finding an honest
substitute.

Worktree creation is the better trigger anyway, on two counts. It fires the
moment the directory the rule protects first exists, rather than at some
earlier point where the rule is a prediction. And it reaches every project that
ever grows a worktree, including the eight already enrolled, where an
enroll-time hook would have reached only projects enrolled after the change —
which is to say, none of the ones with the problem.

The rule goes in the *tracked* `.gitignore` rather than `.git/info/exclude`
because a worktree is a checkout and not repository content, so the rule is
true for every clone, and `info/exclude` is per-clone by construction.

Three deliberate refusals in the implementation:

*It never fails the call.* An unreadable or unwritable `.gitignore` is reported
in `notes` and the checkout still happens. The command's job is creating a
checkout; failing that because a tidiness improvement could not be applied
trades the thing the agent asked for against the favour nobody asked for.

*It never stages.* A generated line sitting in the index is one that gets
committed inside somebody else's change without either of them noticing, and
this edit's entire value is that a human saw it go by.

*It has no comment-stripping branch,* and that absence is a finding rather than
an omission. The first draft skipped lines beginning with `#`, with a docstring
citing this repository's own dependency scan on reading comments as
configuration. Mutation testing killed it: deleting the branch changed no
answer, because the comparison is exact and a `#` prefix already fails it. The
seven comment cases in the test table passed identically with comment handling
removed — the precise shape AGENTS.md warns about, reproduced in a guard
written by someone who had just read the warning. The branch is gone and the
exactness that actually does the work is what the docstring now names.

Eleven mutants, all killed, none unanchored.

**Delivered (G5): the prohibition gets a scan, not a refusal.**

The task said "refuse to offer enrollment for an already-enrolled project",
and there is nothing to refuse. Registering a project is exactly two writes —
a row in `catalog.csv` mapping a directory to an id, and the id itself, minted
fresh — and neither is performed anywhere in first-party production code.
Every catalog write in the repository is a test writing a fixture into
`tmp_path`; every `uuid4()` is a temp-file suffix, a message id, a trace id or
a session claim.

So `tests/test_enrollment_conformance.py` pins the absence rather than adding
a refusal to a path that does not exist. That is a better guarantee than the
prose it replaces: `AGENTS.md` asks an agent not to do something it was
perfectly able to do, and the scan says the machinery is not there, which
holds for an agent that never reads the line.

Three things about the detector are load-bearing, and two of them are scars
from this task.

*It is an AST scan, not a text search.* `"catalog" in line and "write" in
line` matches the scan's own docstring, matches every comment about the rule,
and misses `p = catalog_path(); p.write_text(...)` — wrong in both directions
simultaneously.

*Alias resolution is scoped, and the first draft's was not.* `p = CATALOG`
followed by `open(p, "w")` is enrollment the unscoped draft caught — along
with 200-odd other names in `copilot_operator.py`, because once any name
enters the set an assignment from it *anywhere* in the file adds another and
the transitive closure eats the module. It reported fifteen false positives in
production code on the first run. A scan that reports everything is switched
off exactly as fast as one that reports nothing. Confined to one function body
the same algorithm yields `{catalog, path}`, `{catalog}` and `{found}` across
the three largest modules.

*Three positive controls failed on the first run, and the detector was fixed
rather than the controls.* `self.catalog.write_text(...)` — the obvious way to
write enrollment — was invisible, because the scan looked at bare names and
never at attribute names. Controls stronger than the detector are the only
kind worth writing.

The one surviving mutant was the alias fixed-point loop: cutting it to a
single pass changed no answer, because every control had its definition before
its use. `_own_nodes` yields `ast.walk` order, which is *breadth-first*, so an
assignment inside an `if` is visited after a top-level statement that reads
it. The control is now that shape, and it is the only one that can tell one
pass from convergence.

Fourteen mutants, all killed, none unanchored. 47 tests.

One process lesson, cheap and worth recording: the Phase F/G3 full suite was
running while these edits landed, and reported three failures in
`operator_worktree.py` and `copilot_operator.py` that do not exist. A suite
reads the file as it finds it. Do not edit source while a six-minute suite is
running, and re-run before believing a failure that arrives from one.

### G6 — subproject path ownership, as delivered

`operator ownership check` refuses a branch that changed files outside the
subproject it is working. The decision lives in `operator_ownership.py`,
which touches neither git nor the filesystem beyond reading the declaration;
`manage_ownership` in `copilot_operator.py` is the only part that talks to
git. That split is the reason the rule set could be mutation-tested at all —
57 tests, 18 mutants killed, 0 survived, 0 never ran.

Ownership is declared in `.operator/subprojects.json` at the repository
root, tracked, so CI and a reviewer see the same file the check reads:

```json
{"subprojects": {"api": {"owns": ["services/api"]}},
 "contracts": ["specs/contracts"]}
```

Six decisions carry the design, each pinned by a test:

- **Paths are repository-relative git syntax, never `os.path`.** `git diff
  --name-only` emits forward slashes on every platform. `normalize()`
  accepts either separator and does not fold case, because git is
  case-sensitive about tracked paths and folding would hand one subproject
  another's file on a case-insensitive filesystem only.
- **Containment is segment-wise and runs one way.** `services/api` does not
  own `services/api-v2/` — the `startswith` trap, the same one
  `operator_worktree._is_inside` avoids. Mutation added the other half:
  comparing at the *path's* length rather than the prefix's makes an
  ancestor look owned, which is permissive for ownership and restrictive
  for contracts simultaneously. No test in the suite caught that; the
  mutant did.
- **An unreadable or malformed declaration raises; only an absent one
  returns `None`.** A failed read collapsing to "no rules" reports every
  branch clean, which is this repository's most expensive defect shape.
- **`main...HEAD`, three dots.** Two dots would blame an un-rebased branch
  for everything that landed on `main` behind it, and the gate would refuse
  work nobody on the branch did. `_changed_paths` returns `None`, never
  `[]`, when git refuses: an empty list is a real answer that passes.
- **Contract paths are refused even to a subproject that owns them**, since
  they are the interface between subprojects. `--allow-contracts` waives
  that one rule and does not also grant ownership.
- **Exit 0 allowed, 1 refused, 2 could not tell.** The third is not folded
  into the second, so an author who wants "could not tell" to pass has to
  write `|| true` where a reviewer can see it. `no-declaration` and
  `nothing-changed` are separate passing codes from `owned`, so "the check
  does not apply" never reads as "the check ran and approved", and
  ambiguity — two subprojects that could each own everything changed — is
  refused rather than resolved by picking the first, which would log a
  subproject nobody chose.

### G11 — preservation, as tested

FR-7 promises that everything outside the managed block survives
regeneration byte-for-byte. The promise was already implemented correctly in
`compose`; what was missing was a test that could tell.

`test_a_second_run_is_idempotent` looked like that test and is not. It
regenerates the *same* block, so a `_place_one` that ignored the existing
file entirely and wrote the block alone passes it — the second run produces
the same bytes either way. The regeneration anyone actually runs is one
where the block *changed*: a new toolkit version, a feature turned on, a
section rewritten.

So the four new tests drive `retire` → `render` → `compose` →
`write_text_atomic` end to end and change the block on both axes — a version
bump, and a feature turned **off** so the block shrinks and content below it
could be dragged up with the removed section. The fixture prose is
deliberately ugly: trailing spaces, a doubled blank line, a tab mid-line.
A "preserving" implementation that round-trips the surrounding text through
`splitlines()` and rejoins with `\n` loses exactly those characters and
nothing else, and a test written with tidy prose cannot see it. Both such
mutants are killed by these tests and by nothing else in the file.

Two smaller claims ride along. A project that *documents* the managed block
in a fenced code sample keeps both the sample and everything between it and
the real block — the failure mode being a file that loses the paragraph
explaining the mechanism that ate it. And `merged` is asserted to be a
different outcome from `written`, because `written` over a file that already
held someone's prose reads in the log as "we made this file", which is the
one claim the preservation contract exists to deny.

9 mutants killed, 0 survived, 0 never ran.

### G9 — the marker rename, and why it needed migrating

D3 renames the delimiter to `<!-- BEGIN operator:managed -->`. The rename
itself is trivial; what makes it a task is that the old spelling must stay
*readable*. A writer that knew only the new marker would find no block in a
file carrying the old one, take the "no block here" path, and **append** a
second block below it. The repository then holds two sets of conventions that
disagree — and the disagreement is invisible to `compose`, the one function
whose job is keeping them in step.

So `MARKER_PAIRS` holds both spellings and only the new one is ever written.
A file migrates the first time anything regenerates it and never migrates
again, which is pinned by asserting the second run reports `unchanged`: a
"migration" that runs forever shows a diff in every repository on every run,
and a diff that is always there is a diff nobody reads.

The consent question is not re-asked. A legacy block is one of ours, so
`managed_block_present` must recognise it; otherwise `_place_one` treats a
file this tool wrote as somebody else's, and a non-interactive caller
answering "no" strands the repository on the old spelling permanently.

Two refusals guard the mixed cases, and they catch different files:

- **`spellings_present`** refuses a file carrying whole live blocks in both
  spellings. Replacing one and leaving the other is the doubling the whole
  mechanism exists to prevent, and choosing which to keep is a guess about
  which set of conventions somebody meant.
- **`_marker_offsets` reads through one pair at a time**, so a *legacy begin*
  with a *current end* is not a block. This is the case the first guard does
  not catch, and the reason it matters is that pooling both spellings'
  offsets yields one begin and one end — exactly what well-formed looks like
  — so the malformed check downstream is satisfied and the span is replaced
  silently. Mutation found it; every other test in the file stayed green.

The order of `MARKER_PAIRS` is a **provably equivalent mutant**: since at
most one spelling survives `compose`'s check, reversing the tuple changes no
result. The comment says so rather than implying the order is load-bearing —
a claim in prose that no test can falsify is the same defect as a guard that
cannot fire.

6 mutants killed, 0 survived, 0 never ran.

### G12 delivered — FR-8: defaults off, one platform, `CLAUDE.md`

**The measurement that shaped it.** Before flipping anything: all eight
registered projects under `~/.operator/projects/` have no `features.json`.
Every one is running on the defaults. So "default off" is not a change to
what new projects get — it is a change to what *every existing project* gets
on its next routine regeneration, and resolving an absent configuration to
the new defaults would have stripped every optional section from eight
repositories at once, with the diff attributed to a version bump.

Three consequences, each a deliberate decision:

- **The six `_FLAG` features ship `default=OFF`.** An enabled section is now
  something somebody chose, which is what makes "every enabled section is a
  live requirement" true rather than aspirational.
- **`tracked-backlog` does not move.** `tracked_backlog_backend()` returns
  `FEATURES_BY_SLUG[TRACKED_BACKLOG].default` under *every* uncertainty — no
  catalog in CI, an unreadable file, an unregistered project — so that value
  is the enforcing answer three conformance guards depend on. Flipping it
  would retire those guards on all eight CI legs while every leg stayed
  green, which is the exact shape this plan keeps finding and refusing.
- **`_values_for` refuses a project that never chose**, naming the file and
  `operator projects`. `retire` turns that into a per-project `failed` that
  blocks removal of the global file — the conventions end up in two places
  rather than none, which is the direction every other decision here takes.

**One platform's commands.** `<!-- operator:platform windows -->` …
`<!-- operator:endplatform -->` brackets each variant in the template;
`select_platform` keeps the host's, chosen once per `retire` run so one run
cannot produce files that disagree.

An HTML comment rather than the `**PowerShell (Windows)**` label above the
fence. The label is prose, it is already spelled three different ways in the
template, and a renderer matching on prose would silently keep both variants
the first time somebody reworded a heading — a failure that looks like a
formatting nit and is actually the file asking an agent to choose a shell.

The rules that are load-bearing, each pinned:

- **Markers are hunted outside fences only.** A repository documenting this
  mechanism writes the markers in a fence, and reading the sample as real
  would delete the rest of that section on one platform and nothing at all
  on the other. Same rule, same reason, as the managed-block finder.
- **Unbalanced markers raise.** Recovering silently means a stray begin
  marker is invisible to a run on either machine alone.
- **An unknown platform name is kept**, the same answer an unknown gate slug
  gets: the block is an older build's only copy of that text.
- **The blank-line collapse is confined to the seam.** Removing a block joins
  the blank above it to the blank below, and a doubled blank is the one
  difference a reader does see in a file whose whole claim is that it looks
  the same on both platforms. Collapsing unconditionally passes every other
  test — the shipped template happens to contain no doubled blank line, so
  the two rules agree on it and stop agreeing the moment somebody writes one.
- **`render` takes `platform` with no default.** A default would make every
  test that forgot the argument agree with the machine it ran on, so the
  Windows legs and the POSIX legs would each prove only their own half and
  the suite would look complete.

**`CLAUDE.md`.** It holds `@AGENTS.md` and none of the conventions. Reports on
whether Claude Code reads `AGENTS.md` natively conflict and an import costs
one line, so the import is written rather than the question resolved.
Duplicating the conventions would put two texts that can disagree in front of
one agent in the same turn, with the newer one right and neither file saying
which that is.

It is written *after* `AGENTS.md`, never before — a repository holding an
import of a file that was never written is worse than one holding neither. A
`CLAUDE.md` already there *with* a managed block is regenerated, because a
file this tool wrote is a file this tool keeps true; one *without* is left
alone and is **not** a blocker. That asymmetry is deliberate: consent belongs
at `AGENTS.md`, where the content is, and a second prompt per project for the
file carrying no conventions would spend the operator's attention on the
least important thing in the run.

Both files are staged and committed together, from a pathspec built out of
what is on disk **and carrying a managed block**. `git add` and `git commit`
both treat a pathspec that matches nothing as fatal, so a project that
declined its `AGENTS.md` — and therefore never got a `CLAUDE.md` — would
otherwise have its staging reported as a git failure for correctly not
writing a file.

Existence alone was the first rule and it was wrong. Two of the three
adversarial reviewers independently reproduced the consequence: a repository
where the user keeps their own `CLAUDE.md` — the common case for Claude Code
users, and the one `_place_claude` deliberately leaves alone — had that file
handed to `git add` and then to a commit whose message names only `AGENTS.md`
and whose consent prompt never mentioned it. If it was already tracked, its
uncommitted working-tree edits went in with it. That is the exact failure
`commit_agents_files` passes a pathspec to avoid, reintroduced one argument
further down. The rule is now the same one that decides whether the file may
be written to at all: a name is handed to git only when the file carries a
managed block.

Mutation: 3 killed / 0 survived / 0 never ran on the defaults change; 11 / 0 /
0 on the platform and `CLAUDE.md` work, after the one survivor named above was
turned into a test; 3 / 0 / 0 on the staging rule after the review.

### Answering the FR-8 refusal without answering for anybody

`_values_for` refuses a project that never chose, and that refusal is only
defensible if saying "yes, these" is cheap. It was not: the flags ship off, so
recording the status quo by hand cost one toggle per feature per project —
dozens of keystrokes on a machine with eight registered projects, to write
down an answer somebody already had.

`operator projects` now offers **Record these as chosen** on the feature
screen, and only there — the entry appears when nothing has been written,
which is exactly the state that gets refused, and disappears once a choice
exists so the numbering a reader was taught stays put. It writes the values
already on the screen, so it changes no behaviour at all; what it changes is
that the answer is now somebody's rather than nobody's. Measured end to end:
`_values_for` raises before, resolves after, and the resolved values are
identical to the defaults it refused to assume.

Mutation: 5 mutants, 4 killed, 1 provably equivalent — writing a single slug
is indistinguishable from writing all of them because `write_config`
completes the document from the resolved values either way. Left as prose
rather than pinned with a test that would only assert `write_config`'s
behaviour twice.

## Phase G — the cut, as delivered

G8, G10 and G13 were held for the human because all three delete rules from
the managed block. They were released together and landed together, because
each is load-bearing for the next: the budget is only reachable once the cut
has happened, and the cut is only safe once the deleted lines have somewhere
to go.

**Root template.** 4,332 rendered words to **674**, an 84% cut, against a
`WORD_BUDGET` of 700. Kept: guardrails, procedures, checks. Deleted: the
prose explaining why, which moved to `docs/rationale.md` and the skills —
read on demand instead of on every turn. The block now carries no platform
brackets at all, so both renderings are byte-identical.

**The budget is a mechanism, not an intention.** `render()` raises above 700.
No warning, no override flag: a warning is a line of output nobody is obliged
to act on and the block that produced it still ships, which is how the
predecessor reached 4,332 words with every one of them added for a reason.
The count includes markers and fences, so prose cannot be hidden from it by
moving it into a fence. It fired on its own first run — 756 words — and the
82-word cut that followed took no guardrail with it.

**D10 held throughout.** Twenty-nine tests failed after the rewrite, and not
one was deleted. Each was handled one of four ways: retargeted at the
document that now carries the claim (`docs/operator.md`,
`skills/peer-agents/SKILL.md`); rewritten to measure the *rendered* block
rather than the template; replaced by an absence-check paired with a control
proving the detector fires; or retired against a test asserting the named
replacement suite still exists — because "it's covered elsewhere" is the
sentence under which coverage disappears.

The rewrite found one defect no reader had: the draft documented
`operator work request|list|end`, and there is no `end` work verb. The
replacement test measures every documented `(group, verb)` — in the template
*and* in every `skills/*/SKILL.md` — against the dispatcher's own tables,
which is why `SESSION_VERBS` and `OWNERSHIP_VERBS` now exist. A document
checked against a second copy of the spelling is checked against nothing.

**G10's real finding** is that build/test/lint guidance already never reached
a rendered block: it lived under the one heading `render()` replaces. D11 was
satisfied by accident, by a renderer detail nothing tested. G10 made it
enforced and gave the commands a home — `compose()` seeds a `## Validation`
heading below the block when creating a file from nothing, once, and never
restores a deleted one.

**Subprojects (FR-9)** are generated, not templated, and that is the whole
enforcement: there is no prose file for a rule to be written into, so the
file can only carry resolved facts — name, owned paths, contracts — and a
fact cannot contradict a rule. `_place_subprojects` writes one into every
declared, existing owned directory, after the root file for the same reason
`CLAUDE.md` is written after it. An unreadable declaration fails the project
rather than skipping quietly, because `operator ownership check` refuses on
the same file: swallowing it would leave a repository whose push gate is
broken and whose generation reported success. Budget 120, delivered 63.

## Risks

| Risk | Mitigation |
|---|---|
| **Marker migration appends a second managed block** to the 8 live projects instead of replacing the first, leaving two contradictory rule sets | The writer recognises both spellings and rewrites old→new. Test with a fixture carrying the old marker. |
| A rule is deleted before its check exists | D10; paired commits, and the audit table names the check for every deleted line. |
| Handoff migration drops a banked handoff | Move, never delete. Unknown provenance parks under a reserved name. |
| Reclaim destroys uncommitted work | FR-4. Commit to `wip/` first; the mutating verbs are never issued. |
| `@dataclass` in `copilot_operator.py` breaks the entry point | Plain class + `__slots__`. The module is exec'd from its path with no `sys.modules` entry, where `@dataclass` raises at import. |
| A new guard silently never fires | Mutation-test each one: break it, watch the suite go red, restore. |

## Verification

Full suite every phase — not a subset. The 296-test baseline pair hid four real
failures this week, one of which stopped `operator` starting at all.

Baseline for this branch: **3383 passed, 10 skipped**.
