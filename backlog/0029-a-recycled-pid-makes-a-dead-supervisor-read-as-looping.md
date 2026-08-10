---
id: 29
title: A recycled pid makes a dead supervisor read as looping
status: open
opened: 2026-08-09
spec: specs/003-windows-native-operator/spec.md
requirement: User Story 2 - Autonomous loop mode and handoff on Windows
---

## Evidence

`_running_loop_pid` (`copilot_operator.py`) decides whether an instance has a
live supervisor by reading `{instance}.looppid` and asking `_pid_alive(pid)`:

    pid = int(instance.loop_pid_file.read_text(encoding="utf-8").strip())
    if _pid_alive(pid):
        return pid

`_pid_alive` answers "is *some* process holding this pid", not "is the
supervisor that wrote this file still running". On Windows pids are recycled
aggressively, so once a supervisor dies, any unrelated process that is later
handed its pid makes the pid file read as live. `operator list` then prints
that row as `looping`, with a session number and an `up` age, for an instance
whose supervisor is gone.

Measured on this machine 2026-08-09: `process_start_token` returns a real,
stable, per-run value here (`win:134308020110986193` for the running
interpreter), so the discriminator this needs is available and already
working on the platform where the problem exists.

The repository already treats a bare pid as insufficient identity in three
places. `operator_session.py:220` and `operator_work.py:221` both record
`pid_start=probes.process_start_token(pid)` alongside the pid, and
`operator_liveness.process_start_token` documents exactly this hazard:

    Compared only for equality, and only against a token recorded for the same
    pid. That is what makes the pid probe safe across reuse: a recycled pid
    carries a different start time [...]

`_running_loop_pid` is the remaining reader that does not.

## Why it matters

This is backlog 0001's failure shape rather than a new one. A row that says
`looping` for a dead supervisor is not a wrong answer the reader can catch;
it is byte-identical to a healthy row, so the instrument reports the machine
as fine at precisely the moment it is not. 0001 exists because that pattern
has now recurred six times.

It also outranks the parts of 0001 already fixed, because it gates them.
`_instance_summary` and `list_instances` only say anything about staleness,
unrecorded code, a mismatched record or a supervisor restart *when
`snap["loop_pid"]` is truthy* — every one of those notices is downstream of
this predicate. A pid-reuse false positive keeps the notices switched on for
a supervisor that cannot be described, and a false negative switches all four
off at once.

## Notes

Found by adversarial review (Gemini 3.1 Pro, rated High) while reviewing the
fix on `fix/supervisor-restart-visible`, and corroborated independently by
GPT-5.6 Sol and Grok 4.5 in the same pass. That branch closed the sibling
hole — `_record_describes` now compares `pid_start` as well as `pid`, so a
recycled pid no longer lets a predecessor's *record* pass as the live
supervisor's — but deliberately stopped short of this one, which is a wider
change: `_running_loop_pid` is read by `active_instances`,
`_instance_summary`, `instance_snapshot`, `stop`, `restart-loop` and the
liveness cascade, so every caller has to be considered before the predicate
gets stricter.

Consequence of *this* item being open, and why the branch was still worth
landing: a dead-but-recycled supervisor now reports `[supervisor record is
not its own]` rather than nothing at all, because its leftover record fails
the token check. That is a symptom, not the diagnosis, and it depends on the
recycled process not having rewritten the record.

Suggested approach, matching what the two other readers already do: stamp the
supervisor's start token beside its pid when the pid file is written in
`_publish_supervisor_records`, and have `_running_loop_pid` require both. The
failure direction needs choosing explicitly and is not obvious — a pid file
predating the stamp, or a token that cannot be read, must not silently
convert a live supervisor into a stopped one, because that would make
`active_instances` drop it and take every notice about it with it.
