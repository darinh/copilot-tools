---
id: 24
title: crash_recovery_verdict cannot express unclaimed context
status: proposed
opened: 2026-08-05
spec: specs/004-operator-session/spec.md
---

## Evidence

`crash_recovery_verdict` (copilot_operator.py ~L2041) answers a boolean: either
the previous session crashed, or it did not. After the handoff re-key there is
a third state it cannot express -- "no handoff addressed to you, but there IS
unclaimed context for this project" -- and both readings of that state are
wrong:

- Report a crash: false alarm. The predecessor did write a handoff; it just did
  not name this instance, or predates the layout.
- Report no crash (what ships today, via the `next-session.md` fallback): the
  agent is told nothing, and the context sits in
  `handoff/legacy/` or at the old project path where nothing routes it.

Measured while reviewing the re-key: adding `migrate_project_handoff` to the
launch path -- the obvious fix for the fallback being unbounded in time --
makes the second case worse, not better. An unattributed `next-session.md` gets
parked in `handoff/legacy/`, the instance file is still absent, and the verdict
then reports a crash that did not happen. `test_an_unmigrated_handoff_is_not_
reported_as_a_crash` catches it. That change was reverted.

Two reviewers raised the underlying issue independently:

- gpt-5.3-codex: "If instance handoff is missing and legacy `next-session.md`
  exists, verdict always returns not-crash-recovery. This is unconditional and
  not bounded to the one-time migration window, so stale legacy files can mask
  real crashes." Migration runs on the next *write*, so an instance that keeps
  crashing never reaches one and the suppression never expires.
- gemini-3.1-pro: a `_park` or delivery `os.replace` that fails is warned about
  on stderr and the handoff publishes anyway, leaving the old file where
  nothing reads it.

## Why it matters

Both failures are the same missing state, and the fix is a preamble that can
say "there is unclaimed context at PATH" rather than a better boolean. That
belongs with `operator session start` (Phase D of specs/004-operator-session),
which is the seam that decides what a session is told at launch. Tweaking the
boolean now would ship a third wrong answer and make the real fix harder.

## Done when

- A session that launches with no handoff addressed to it, while unclaimed
  context for the project exists, is told so and told *where* -- by path.
- A real crash is still reported as one.
- An unattributed legacy handoff is still not reported as a crash.
  `test_an_unmigrated_handoff_is_not_reported_as_a_crash` stays green, and it is
  the control that the third state was added rather than the second answer
  swapped for the first.
- The suppression is bounded. Today's `next-session.md` fallback is
  unconditional in time, so a stale legacy file can mask a real crash
  indefinitely; whatever ships must not preserve that.

## Not in scope

- Improving the boolean in place. The item's argument is that a better boolean
  is a third wrong answer.
- Adding `migrate_project_handoff` to the launch path. Measured, and it makes
  the unattributed case report a crash that did not happen.

## Risk

🟡 `copilot_operator.py::crash_recovery_verdict` (~L2041) and whatever composes
the launch preamble. The failure mode is a false crash report, which costs a
session its first minutes and its trust in the readout, not data.

## Sequencing

Blocked on `operator session start` -- Phase D of
`specs/004-operator-session/spec.md` -- which is the seam that decides what a
session is told at launch. This item does not need its own design; it needs that
seam to exist and then to carry one more state. Approving it before Phase D
lands would put it in the queue with nothing to attach to.

## Notes

Found by adversarial review of the handoff re-key (Phase B). Do not fix by adding migrate_project_handoff to the launch path -- measured, it makes the unattributed case report a crash that did not happen.
