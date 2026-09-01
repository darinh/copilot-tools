---
id: 34
title: operator launches a session in an uncatalogued repository and never says so
status: proposed
opened: 2026-08-16
spec: none
---

## Evidence

User report, verbatim, 2026-08-16: "i ran operator --loop inside of ~/repos/tiktok-offline and it started a loop but there is no project configuration so it doesnt know anythingnabout worktrees etc"

From ~/.operator/trace.jsonl:
* 2026-08-15T22:50:29Z {"event":"invoke","argv":["--loop"],"cwd":"C:\\Users\\darin\\repos\\tiktok-downloader"} followed by {"event":"supervisor_start","instance":"tiktok-downloader","session":1}. Sessions #1 and #2 ran; session_exit uptime_s 112. The repository is not in the catalog and nothing said so.
* The only hint anywhere is one line in operator.log at 15:50:29: "Progress breaker: inactive - no readable git state in C:\Users\darin\repos\tiktok-downloader". Nothing names the catalog, and nothing distinguishes "not registered" from "registered with nothing to do".

Catalog state: ~/.operator/projects/catalog.csv holds 10 rows. Neither tiktok directory appears in it, nor in any of the five banked copies spanning 2026-08-01 to 2026-08-15 (bak-20260801-002554, bak-before-subloc, pre-test-20260805T132756, bak-preregister, pre-test-20260815T174102). The catalog only ever grew, 6 -> 8 -> 9 -> 10 rows, so nothing removed them; they were never enrolled.

Measured 2026-08-16, which answers the question item 31 ends on: `operator list` reports 11 running instances, and one of them - `repos`, at ~/repos, session #1, up 18h 49m - has no catalog row. So 1 of 11 live instances is uncatalogued. It is a live gap across the fleet, not only a trap for new repositories.

## Why it matters

A session launched in an uncatalogued repository has no project directory, and therefore no features.json, no work.db, no project instructions and no worktree configuration. Every consumer downstream reports what an empty queue reports - "no assignment" - so "this project is not registered" and "there is nothing for you to do" are indistinguishable to the agent and to the human watching it. That is the signal-indistinguishable-from-its-absence failure this toolkit exists to refuse, and here it is on the launch path.

It also guarantees item 31 for that session: an instance in this state cannot hand off, so when its context fills, the context dies with the process. The user in the report above spent the discovery cost themselves, concluded the toolkit was broken, and was looking for what regressed - which is the cost of a state the system knows about and does not mention.

## Done when

- Launching in a repository with no catalog row prints one line, beside the
  existing "Progress breaker: inactive" line, naming
  `~/.operator/projects/catalog.csv` and stating that nothing in this toolkit
  writes it.
- The same statement reaches the agent, in the preamble it reads, so the agent
  knows its own status rather than inferring it from an empty queue.
- A session in a registered project prints neither. That is the control: a
  message that appears in both cases carries no information, which is the defect
  being fixed.
- The state is announced whether or not the launch proceeds. Announcing is the
  whole of this item; whether operator should also *refuse* is argued below and
  is not settled here.
- "not registered" and "nothing to do" are distinguishable from the output alone,
  by someone who does not already know which one they are looking at.

## Not in scope

- Registering the project. Item 0031 owns enrollment; this item must not grow a
  second mechanism for it.

## Risk

🟢 the launch path's reporting and one preamble clause. No behaviour changes.

One constraint if the preamble clause lands in `operator_kernel/preamble.py`
rather than `copilot_operator.py`: item 0038 measures the kernel at 11 total
lines below its ceiling. The boundary test asserts `total <= 9000`, so a clause
whose net cost is 11 lines or fewer still passes and anything larger does not --
measure the delta rather than assuming either way. Sequencing behind 0038 removes
the constraint entirely.

## Needs a decision before this can be worked

- **Whether operator should also refuse to launch into an uncatalogued
  repository.** The item says refusing is "probably the wrong fix and should be
  argued before it is built", and that argument has not been had.
  `work_seam._loop_work_db` is deliberate that a missing store costs the agent a
  hint and never a session, which is the case against. This does **not** block
  the announcement above -- that is wanted under either answer -- but it must not
  be settled by a refinement's silence either.
- **What a `cwd` that is not a git repository at all should do.** Shared with
  item 0031 and with item 0035, and it should be answered once for all three
  rather than three times. `tiktok-downloader` had no readable git state, so it
  was not merely uncatalogued -- it could not have been catalogued meaningfully.

## Notes

Refusing to launch is probably the wrong fix and should be argued before it is built: work_seam._loop_work_db is deliberate that a missing store must cost the agent a hint and never a session, and the loop is supposed to run whether or not the project is registered.

The cheap version is to say it. One launch line beside the existing "Progress breaker: inactive" line, naming ~/.operator/projects/catalog.csv and stating that nothing in this toolkit writes it - which is what docs/operator.md line 1038 and tests/test_enrollment_conformance.py already assert - and the same statement in the preamble the agent reads, so the agent knows its own status rather than inferring it from an empty queue.

Related: item 31, same root cause and a different victim (handoff rather than launch). The self-registration option argued there would close both, and this item should not grow a second enrollment mechanism of its own. If 31 is approved, this one is the announcement half of it.

Open: what a cwd that is not a git repository at all should do. tiktok-downloader had no readable git state, so it was not merely uncatalogued - it could not have been catalogued meaningfully either, and minting a project per arbitrary directory is probably not wanted.
