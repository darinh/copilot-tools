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
(E4). E5 remains.

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

## Phase G+H — the audit

Before the template changes, produce a table classifying every candidate line as
guardrail / procedure / checkable. The expected residue is small; the two lines
that look genuinely irreducible are *commit before you delegate* and *forbid the
mutating git verbs by name when you do*, because both fire while the agent is
thinking "I'll just have a reviewer glance at this".

The budget number is set from what the residue measures, not guessed in advance,
and then made to fail the build.

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
