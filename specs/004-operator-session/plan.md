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
