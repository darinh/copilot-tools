---
id: 2
title: The no-change breaker charges externally killed sessions as idle
status: open
opened: 2026-08-04
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
