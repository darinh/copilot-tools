---
id: 2
title: The no-change breaker charges externally killed sessions as idle
status: closed
opened: 2026-08-04
closed: 2026-08-05
commit: 981f456f01b495d8091866b173844bcbfb737d49
spec: none
---

## Evidence

Measured 2026-08-04 from `~/.operator/restart/*.nochange`:

```
ac-unreal                0
book-translator          0
copilot-tools            2
discord-invite-manager   0
finances                 0
prism                    0
scripts                  0
snes-ghosts              0
```

`copilot-tools` stands at **2**; every other instance stands at 0. The limit
that retires a loop is 3.

Neither of those two sessions was idle. From `trace.jsonl`, the `copilot-tools`
sessions immediately preceding the count ran 268s and 458s -- 4.5 and 7.6
minutes -- and both ended with `restart=False` and no exit code, which is the
signature of the external kill recorded in item 0001, not of an agent that sat
still.

## Why it matters

The breaker exists to retire a loop that has stopped making progress. It reads
"no commits since last session" as the signal, which is sound for an idle
agent and wrong for a killed one: a session killed at four minutes has usually
not committed yet, so an external kill is indistinguishable from idleness at
the moment the count is incremented.

The consequence is that the breaker retires the loops being killed *fastest*,
which is backwards -- those are the ones most in need of supervision, and
their failure has nothing to do with the agent's behaviour. One more killed
session would have retired this project's loop entirely.

## Notes

`evaluate_progress` is called at `copilot_operator.py:3406` and the counter is
reset at 3412. The signal that would distinguish the two cases already exists
in the same record the breaker could read: a session that ended via handoff
carries `restart=True`, and one killed externally carries `restart=False` with
a null exit code and a short uptime.

No specification covers the breaker: `specs/` contains no mention of
`no-change`, `nochange`, or a progress breaker, so `spec` is `none` rather
than a guess. If this is fixed, consider whether the behaviour is worth
specifying rather than leaving it described only by its implementation.

## Note, 2026-08-05: the signal named above did not exist yet, and now does

The paragraph above says the distinguishing signal "already exists in the same
record the breaker could read". It did not. `_record_session_exit` was only
called from the branch that had already established the restart marker was
absent, so no record could carry `restart=True`, and sessions that ended by
handoff produced no record at all. Writing the breaker against that field as
it stood would have read every ending as an external kill and never fired.

That call site is fixed (see the correction on item 0001), so a session ending
by restart request is now recorded with `restart=True` and no exit code, and a
session found gone unexplained is recorded with `restart=False`. The signal
this item proposes to read now exists — but only for records written from
2026-08-05 onward.

Two things to settle when this is picked up, neither of which the fix decides:

- The breaker runs on the *converged* path, after both endings have collapsed
  into `restart_requested`. Reading the distinction there means carrying it to
  that point, not re-probing the marker, which `remove_file` has already
  cleared by then.
- "Ended by handoff" is not the same question as "made progress". A session
  that hands off having committed nothing is arguably exactly what the breaker
  is for. The defensible rule is probably that an *unexplained* ending is not
  chargeable evidence of idleness either way, rather than that a handoff earns
  a free pass.

The counts in *Evidence* are stale: measured again on 2026-08-05, every
instance reads 0 except `snes-ghosts` at 1. `copilot-tools` has since
committed, which resets it. The mechanism is unchanged; only the numbers moved.

## Note, 2026-08-05: closed, and how the two questions were settled

Re-measured a third time before implementing, from
`~/.operator/restart/*.nochange`: every instance reads 0, `snes-ghosts`
included (it has committed since). Nothing in the numbers argues either way
any more, which is the expected shape — the counters are reset by any
productive session, so their value at any moment says nothing about how often
the mechanism has misfired.

**The converged path.** The distinction is carried, not re-probed. A local
`ending_accounted_for` is set at each of the two break sites out of the poll
loop and read at the restart block, by which point `remove_file` has already
cleared the restart marker. Re-probing there could only ever have answered
"absent".

**"Ended by handoff" is not the same question as "made progress" — right,
and the rule is neither of those.** An ending is *accounted for* when a
handoff asked for the restart, **or** the runner outlived copilot and wrote an
exit code. A handoff earns no free pass: a session that hands off having
committed nothing still charges the idleness streak, which is what the breaker
is for. What is spared is the ending nobody observed — no restart request and
no exit code, the signature of the whole pane going down. That is the case
where an unchanged fingerprint is a fact about *when the session died* rather
than about what the agent was doing.

That reading turned out to be testable rather than merely defensible: the
suite's existing `start_session` double writes an exit file, so every
pre-existing breaker test describes a session that ended cleanly and none of
them changed behaviour. The new double simply omits the exit file.

**Not forgiven, only re-filed.** An unaccounted ending advances a second
streak of its own (`<id>.unaccounted`, allowance 5, `EXIT_UNACCOUNTED = 4`).
The pathology the breaker was built for — fifteen sessions in seventy-eight
minutes, each up for minutes, none changing anything — presents as exactly
this ending, and `crash_failures` cannot bound it because staying up past
`HEALTHY_SESSION_SECONDS` is what resets that counter. Sparing it without
counting it would have left that loop unbounded. Keeping the counts in
separate files is the other half: two killed sessions and one idle one are not
three of anything.

**Left unspecified, deliberately.** `spec` stays `none`. The behaviour is now
documented in `docs/operator.md` under the progress circuit breaker and
covered by 18 new tests in `tests/test_circuit_breaker.py`, but no spec under
`specs/` claims the supervisor loop, and adding one for this alone would put a
single paragraph of the loop's behaviour under a governance the rest of it
does not have.

