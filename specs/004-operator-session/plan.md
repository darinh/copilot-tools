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
`copilot_operator.py` (E1, E2). E3–E5 remain.

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
found that hole; the residual window is a refresh that leaves every column
identical, which needs a heartbeat inside the same whole second as the one
already stored and publishes no new evidence of life anyway.

A worktree recorded in the *other* platform's path syntax refuses too, and
that one is worth naming because it fails silently in the dangerous
direction: `Path(r"C:\repos\app")` on POSIX is a relative path — a backslash
is an ordinary filename character there — so a presence probe reports the
worktree absent, preservation concludes there is nothing to save, and the
reclaim reassigns a tree it never looked at. `_foreign_path` asks `ntpath`,
which is pure syntax and understands both spellings, and takes the platform
as a parameter so both branches are exercised on every CI leg rather than
each leg testing only its own half.

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
