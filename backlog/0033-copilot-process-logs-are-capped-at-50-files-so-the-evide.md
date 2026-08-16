---
id: 33
title: Copilot process logs are capped at 50 files, so the evidence for items 0001 and 0030 expires in hours
status: proposed
opened: 2026-08-15
spec: specs/003-windows-native-operator/spec.md
---

## Evidence

Measured on this machine 2026-08-16T01:54Z-02:12Z, while re-measuring
backlog item 0001.

`~/.copilot/logs` holds **exactly 50 `process-*.log` files**. It held exactly
50 at three observations across 25 minutes, and files are evicted while
sessions run: between two runs of the same scan, 20 minutes apart, one file
appeared and `process-1786338840160-82184.log` vanished — confirmed absent
from disk on re-test, 29 agent-active minutes of a supervised session with it.

The scan is a forward pass over every log counting
`Forwarding event for session <uuid>: <type>` markers, restricted to logs
whose pid the trace names as a session. Two runs:

```
scan A  2026-08-16T01:58Z  45 supervised logs  quiet-window exposure 23.63 active session-hours
scan B  2026-08-16T02:12Z  45 supervised logs  quiet-window exposure 23.25 active session-hours
```

Both windows are the *same* interval, 2026-08-10T05:32Z to 08-15T22:17Z. The
0.38-hour difference is not a correction; it is a deleted file. **A
measurement of a fixed past window returns a smaller answer every time it is
run.**

The eviction rule is not established, and the obvious guess is refuted. The
evicted file was created 2026-08-10T05:14:00Z and five retained files were
created before it (four of them supervised sessions), so it is not
oldest-by-creation. Least-recently-written could not be tested: the file's
mtime was never recorded before it was deleted, which is this item's own
subject arriving one level up. No third rule is proposed here.

Rate: the fleet produced 18 successor sessions in the 111 minutes from
2026-08-16T00:09Z, about ten new logs an hour, against a 50-file ceiling.
A working fleet therefore turns over the whole retained history in roughly
five hours.

## Why it matters

Items 0001 and 0030 both rest entirely on these logs. 0030's liveness
classification reads the newest `Forwarding event` marker per session; 0001's
exposure denominator -- active session-hours -- is computed from every marker
in every supervised log. Neither instrument has any other source.

A 50-file ring that a working fleet cycles in about five hours means the
evidence for both items expires faster than the phenomena they describe. The
2026-08-10T00:25 burst is the concrete loss already taken: it is the one burst
in 0001 that survives provenance filtering, and the logs of the seven sessions
it took are gone. The oldest retained file was created 00:26:39Z, 55 seconds
after the last of the seven endings, so what remains are the successor
sessions. Nobody will ever be able to look at what those processes were doing
when they died.

The bias is the bad way round. An idle fleet writes almost no new logs, so
history survives; a working fleet evicts it. So the retention is longest
exactly when there is nothing to see, and shortest during the windows that
carry evidence -- which is precisely the exposure window 0001 has been waiting
five days for.

## Notes

**Not established: the eviction rule.** Measured is the cap (50) and that
eviction happens during normal operation. Oldest-by-creation and
least-recently-written are both refuted by the one eviction observed. One
eviction is a thin basis for any rule, which is why none is proposed here.

**Not established: whether the cap is configurable.** No search was made for a
Copilot setting that changes it. That is the first thing to check, because if
one exists this item is a configuration change rather than a snapshot
mechanism.

**The remedy is probably a marker index, not a log copy.** The retained logs
are 6.3 GB and copying them on a schedule is not reasonable. Everything both
items need is the marker stream -- timestamp, session uuid, event type -- which
for the current 45 supervised logs compresses to a few MB. A periodic
extraction into a durable store would preserve the measurement while the logs
themselves stay disposable.

**Where it belongs is the owner's call.** This repository is frozen to safety
fixes (`FROZEN.md`) and the supervision kernel lives in `../operator`, which is
the same disposition item 0030 records for its own remedy. Filed here because
this is where the two items it protects live.

**Cheap partial mitigation, unverified.** A scheduled copy of the *newest* few
logs would not help; the loss is at the old end. Nothing here has been tested.
