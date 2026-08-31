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

## Notes

Distinct from 0033, which is about eviction destroying evidence by count - this is unbounded growth of a single file, and the two interact badly: a size-based sweep that evicts the big file destroys exactly the log 0001 most wants, while a count-based cap leaves it. Whatever is done should keep 0001 and 0030 able to read what they need. Note also that the log filename pid (process-<epoch_ms>-<pid>.log) is the copilot.EXE pid and matched 0 of 1,070 session_pid values in trace.jsonl, so nothing currently maps a log to the instance that owns it - worth fixing alongside, because it is what stopped 0001 filtering exposure to supervised sessions on 2026-08-31.
