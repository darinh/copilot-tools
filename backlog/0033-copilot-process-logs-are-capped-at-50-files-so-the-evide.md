---
id: 33
title: Copilot process logs are capped at 50 files and evict while sessions run, destroying evidence items 0001 and 0030 depend on
status: proposed
opened: 2026-08-15
spec: specs/003-windows-native-operator/spec.md
---

## Evidence

Measured on this machine 2026-08-16T01:54Z-02:03Z, while re-measuring
backlog item 0001.

`~/.copilot/logs` holds **exactly 50 `process-*.log` files**. It held exactly
50 at every observation, and files are evicted while sessions run: between two
runs of the same scan, one file appeared and
`process-1786338840160-82184.log` vanished — confirmed absent from disk on
re-test, 29 eventful minutes of a supervised session with it.

The scan is a forward pass over every log counting
`Forwarding event for session <uuid>: <type>` markers, restricted to logs
whose pid the trace names as a session. Two runs, timed by the mtime of the
artefacts they wrote:

```
scan A  finished 2026-08-16T01:59:15Z  45 supervised logs  quiet-window total 23.63 eventful log-hours
scan B  finished 2026-08-16T02:03:12Z  45 supervised logs  quiet-window total 23.25 eventful log-hours
```

Both windows are the *same* interval, 2026-08-10T05:32Z to 08-15T22:17Z. The
0.38-hour difference is not a correction; it is a deleted file. **A measurement
of a fixed past window can return a smaller answer when it is re-run.** One
eviction was observed, so nothing stronger than "can" is claimed.

The eviction rule is not established, and the obvious guess is refuted. The
evicted file was created 2026-08-10T05:14:00Z and five retained files were
created before it (four of them supervised sessions), so it is not
oldest-by-creation. Least-recently-written could not be tested: the file's
mtime was never recorded before it was deleted, which is this item's own
subject arriving one level up. No third rule is proposed here.

Rate: the fleet produced 18 successor sessions in the 111 minutes from
2026-08-16T00:09Z, about ten new logs an hour, against a 50-file ceiling. That
is eviction pressure equal to the size of the whole cache in about five hours.
It is **not** a claim that every file now present will be gone in five hours —
that needs the oldest-first rule the paragraph above refuses to assume.

## Why it matters

Items 0001 and 0030 both depend on these logs, though not equally, and the
first draft of this section overstated it. 0030's liveness classification reads
the newest `Forwarding event` marker per session and has no other source at
all. 0001's kill *count* does not come from here — it comes from `session_exit`
records in `~/.operator/trace.jsonl`, which is an append-only file and not a
50-entry ring, so the count is durable. What the logs uniquely hold for 0001 is
the exposure denominator and **the shape of an ending**.

That second one is the real loss, and it is the one this item is about. Every
ending in 0001 that was ever diagnosed was diagnosed from a log: the original
wave by seven extension hosts exiting `0xC000013A` with an orderly teardown
following, and the 2026-08-16T00:09 access violation by its log stopping inside
a token stream with no shutdown sequence at all. The trace records that both
happened. Only the log distinguishes a kill from a crash.

The 2026-08-10T00:25 burst is the concrete loss already taken: it is the one
burst in 0001 that survives provenance filtering, and the logs of the seven
sessions it took are gone. The oldest retained file was created 00:26:39Z, 55
seconds after the last of the seven endings, so what remains are the successor
sessions. That burst can now never be classified further than the trace already
classifies it.

The bias is the bad way round. An idle fleet writes almost no new logs, so
history survives; a working fleet evicts it. So the retention is longest
exactly when there is nothing to see, and shortest during the windows that
carry evidence -- which is precisely the exposure window 0001 has been waiting
five days for.

## Notes

**Not established: the eviction rule.** Measured is the cap (50) and that
eviction happens during normal operation. Oldest-by-creation is refuted by the
one eviction observed; least-recently-written could not be tested at all,
because the evicted file's mtime was never recorded before it went. One
eviction is a thin basis for any rule, which is why none is proposed here.

**Not established: whether the cap is configurable.** No search was made for a
Copilot setting that changes it. That is the first thing to check, because if
one exists this item is a configuration change rather than a snapshot
mechanism.

**The marker index is not a sufficient remedy, and calling it one would be the
same error twice.** The retained logs are 6.3 GB and copying them on a schedule
is not reasonable; a `{timestamp, session uuid, event type}` index of the same
material is a few MB and would preserve 0030's liveness instrument and 0001's
exposure denominator completely. It would **not** preserve the thing 0001 most
needs: an extension host exiting `0xC000013A` with an orderly teardown, or a
file stopping mid-token-stream, are not events in the marker stream. An index
retires the cheap uses and silently drops the diagnostic one. Whatever is built
here has to decide that deliberately.

**Where it belongs is the owner's call.** This repository is frozen to safety
fixes (`FROZEN.md`) and the supervision kernel lives in `../operator`, which is
the same disposition item 0030 records for its own remedy. Filed here because
this is where the two items it protects live.

**Cheap partial mitigation, unverified.** A scheduled copy of the *newest* few
logs would not help; the loss is at the old end. Nothing here has been tested.
