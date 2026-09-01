---
id: 39
title: Copilot debug logs are unbounded in size; one session wrote 12.33 GB in 11 days
status: proposed
opened: 2026-08-31
spec: none
---

## Evidence

Measured 2026-08-31 on this machine, while taking the exposure denominator for
item 0001.

`~/.copilot/logs` holds **16.69 GB across 21 files**. Two files are 15.6 GB of
that:

| file | size | last write |
|---|---|---|
| `process-1786909174577-54460.log` | **12.33 GB** | 2026-08-27 20:03 |
| `process-1786834628244-24048.log` | 2.88 GB | 2026-08-27 19:58 |
| `process-1786848399237-50656.log` | 243 MB | 2026-08-27 19:57 |
| `process-1786992294116-70072.log` | 229 MB | 2026-08-27 20:03 |
| `process-1786845076616-58856.log` | 214 MB | 2026-08-27 20:02 |

The 12.33 GB file is a **single session** (`cfabc021-bedf-49b6-a787-56d87c8d5d0e`)
running 2026-08-16T19:39Z to 2026-08-28T03:03Z — 11.3 days — and holding
16,208,424 `Forwarding event for session` markers, which is about 17 events per
second sustained for the whole period.

It is not a spin loop. Sampled at five offsets, the event mix is:

    45.7%  assistant.streaming_delta
    42.2%  assistant.tool_call_delta
     3.9%  session.background_tasks_changed
     2.3%  assistant.reasoning_delta
     0.8%  assistant.message_delta

88% is token-level streaming, and the sampled content is an agent reasoning
about equivalent mutants in a mutation-testing table — ordinary work. **The
volume is the logging level, not the workload**: every streamed token of every
turn is written to disk at DEBUG, so a busy session costs roughly 1 GB/day.

Reproduce with:

    Get-ChildItem ~/.copilot/logs -Filter *.log |
      Sort-Object Length -Descending | Select-Object -First 5 Name, Length

C: currently has 305 GB free of 1,862 GB, so this is not yet an outage. At
1 GB/day/busy-session across nine instances it is a matter of weeks, and the
failure mode when a machine fills is not a clean one.

## Why it matters

The retention item 0033 caps the log directory at 50 FILES and says nothing about bytes, so a single long-running session can grow one file without limit and the cap never fires. Two files now hold 15.6 GB. The cost is not only disk: item 0001 reads these logs to measure exposure, and a 12 GB file takes minutes per scan, so the instrument that item depends on gets slower as the fault gets worse. A machine that fills up loses the sessions, the logs and the evidence together.

## Done when

- No single Copilot debug log can grow without bound. Either the streaming
  deltas that make up 88% of the volume stop reaching disk at DEBUG, or a size
  bound exists that fires before a file reaches gigabytes.
- The accumulation rate is **observed**, not derived. The 1 GB/day/busy-session
  figure comes from one file's size over its own lifetime; two readings 120
  seconds apart showed zero growth. A remedy sized against a derived rate is
  sized against nothing.
- Items 0001 and 0030 can still read what they need afterwards: 0030's newest
  marker per running session, and 0001's ability to tell a kill from a crash by
  the *shape* of a log's ending.
- Whoever closes this states which of the two it is: a configuration change in
  the Copilot CLI, or a fleet-side sweep. See the decision below -- they are not
  the same item and one of them may not be this project's to make.

## Not in scope

- A size-based sweep that evicts the largest file. That destroys exactly the log
  item 0001 most wants, and it is the obvious remedy, which is why it is ruled
  out here in writing.
- Whether an 11-day session is itself a defect. Named and explicitly not asserted
  by this item.
- Item 0033's count-based eviction. Distinct fault, and the two interact badly:
  a count cap never fires on one 12 GB file, and a size sweep evicts what the
  count cap correctly kept.

## Risk

🔴 by consequence. Every remedy here either deletes evidence or changes what gets
written, and two live items read these files. There is no rollback for a deleted
log. Anything that truncates a file *in place* is worse than deleting it, because
a truncated log looks like a session that stopped.

## Needs a decision before this can be worked

- **Whether this is ours to fix at all.** `~/.copilot/logs` is written by the
  Copilot CLI, not by this toolkit. If the log level or a size cap is
  configurable, this is a settings change and a line of documentation. If it is
  not, the only lever this project has is deletion -- which is the lever the
  scope section above rules out. **Check for the setting first**; it is the same
  unanswered prior question item 0033 has, and one search answers both.

## Re-measured 2026-08-31T23:45Z, later the same day

    22 files, 18.17 GB (decimal) = 16.92 GiB
    process-1786909174577-54460.log  13,238.0 MB  last write 2026-08-28T03:03Z
    process-1786834628244-24048.log   3,096.8 MB  last write 2026-08-28T02:58Z

The two large files were unchanged in size and neither had been written to since
2026-08-28. The directory total moves between readings because live sessions are
still writing small logs and starting new ones -- three readings within the hour
gave 22 files / 18.17 GB, 22 / 18.351 GB and 23 / 18.353 GB -- so any total here
is a snapshot and not a level. This is the same trap as the units correction
below: quote a reading, not a trend, unless the trend was measured.

**What the timestamps do and do not establish.** They establish that those two
files stopped growing. They do not establish that the sessions writing them
ended: item 0030 exists precisely because a session can fall silent for days with
its process alive, and a stale mtime is the signature of both. Nor is "nothing is
reading them" a measurement -- items 0001 and 0030 read these logs by design, and
file metadata says nothing about readers.

What can be said is narrower and still worth saying: the 15.6 GB at the centre of
this item is not currently growing, so the disk pressure is not accelerating
today. The mechanism that produced a 12 GB file is untouched, so it **can** recur
whenever a session runs that long again. Whether it will is not predicted here.

## Notes

Distinct from 0033, which is about eviction destroying evidence by count - this is unbounded growth of a single file, and the two interact badly: a size-based sweep that evicts the big file destroys exactly the log 0001 most wants, while a count-based cap leaves it. Whatever is done should keep 0001 and 0030 able to read what they need. Note also that the log filename pid (process-<epoch_ms>-<pid>.log) is the copilot.EXE pid and matched 0 of 1,070 session_pid values in trace.jsonl, so nothing currently maps a log to the instance that owns it - worth fixing alongside, because it is what stopped 0001 filtering exposure to supervised sessions on 2026-08-31.

## Ownership, added 2026-08-31: the two large logs belong to ac-unreal and repos

Resolved by joining the session uuid in each log against the one named in
`~/.operator/restart/<instance>.state`, which works where the pid in the log
filename does not (that is the `copilot.EXE` pid and matches nothing the trace
records). 20 of 21 logs resolved.

| owner | log | size |
|---|---|---|
| **ac-unreal** | `process-1786909174577-54460.log` | **12,624.8 MB** |
| **repos** | `process-1786834628244-24048.log` | 2,953.3 MB |
| snes-ghosts | `process-1786848399237-50656.log` | 242.9 MB |
| operator | `process-1786992294116-70072.log` | 229.1 MB |
| copilot-tools | `process-1786845076616-58856.log` | 214.3 MB |

Every other log is under 200 MB. So this is not a fleet-wide accumulation to be
managed in aggregate — **two instances hold 93% of the bytes**, and `ac-unreal`
alone holds 73%.

`ac-unreal`'s session ran 2026-08-16T19:39Z to 2026-08-28T03:03Z, 11.3 days
without ending, and `ac-unreal` is no longer in `operator list`. `repos` is
still running. Whether an 11-day session is itself the thing to fix is a
separate question from the logging volume and is not asserted here.

**Correction to a figure this item nearly carried.** A first pass reported the
directory growing from 16.69 GB to 18.06 GB during one session, which would
have been about 80 GB/day extrapolated. It is not growing: the two numbers are
the same quantity in binary and decimal units (18.06e9 bytes = 16.82 GiB). Two
readings 120 seconds apart showed **0 MB of growth**. The rate at which this
accumulates is therefore still unmeasured — the 1 GB/day/busy-session figure in
the evidence above is derived from one file's size over its own lifetime, not
observed live.

