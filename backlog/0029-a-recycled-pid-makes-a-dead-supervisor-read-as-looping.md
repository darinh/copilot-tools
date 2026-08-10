---
id: 29
title: A recycled pid makes a dead supervisor read as looping
status: closed
opened: 2026-08-09
closed: 2026-08-09
commit: 5699c3bc380d6bc61f083c050c31ab9466957180
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

## Resolution, 2026-08-09

Done as suggested, with the failure direction chosen explicitly: **only
positive evidence refutes.** `_loop_pid_stamp` writes the pid on the first
line and `pid_start=` / `boot=` on the lines after it, in one write;
`_running_loop_pid` (via `_running_loop_identity`) returns `None` and prunes
only when the live token is a well-formed token of the same kind as the
recorded one and differs from it. An unstamped file, an unreadable token, a
damaged stamp and a token whose kind cannot be compared all answer exactly as
they did before.

Measured on this machine against copies of the ten real
`~/.operator/restart/*.loop.pid` files (copies, so the pruning branch could
not touch live state):

    ac-unreal    pid=21428 alive=True stamps={} -> _running_loop_pid=21428
    ...  9 live unstamped supervisors, all still read as running
    probe        pid=54968 alive=False        -> _running_loop_pid=None
    ac-unreal    stamped win:1343079519667171280 -> None, pruned=True
    ac-unreal    stamped with the true token     -> 21428
    ac-unreal    stamped win:...-damaged         -> 21428

Full suite 4827 passed / 9 skipped, against 4742 / 9 on `main`. Three
mutation harnesses run from a temp directory: 12 mutants over the round-three
guards, 14 still-applicable round-two mutants, and 3 respelled where round
three rewrote the anchor. No survivors.

Three adversarial review rounds across GPT-5.6 Sol, Gemini 3.1 Pro and
Grok 4.5 produced eleven findings, all fixed. Two are worth recording because
they were not visible from the diff:

* **The locale pin nearly cost a worktree.** `ps -o lstart=` renders through
  `LC_TIME` and `TZ`, so the token was a property of the *caller*; pinning it
  was necessary once a mismatch deletes a pid file. But
  `process_start_token` is shared with `operator_liveness.assess`, which
  returns DEAD — *reclaimable* — when a claim's recorded token differs from
  the live one. Every claim already on disk carries the pre-pin rendering, so
  the first sweep after upgrade would have read a live agent's owner as dead
  and offered its worktree to somebody else. Fixed the way `boot_identity`
  already handles its two shapes: the pinned rendering is tagged `psc:`, and
  `same_start_token` answers `None` across kinds rather than `False`. Found
  by Grok 4.5 and, on the pid-file path, independently by Gemini 3.1 Pro.
* **A damaged stamp hid a readable pid.** The first fix for the
  `UnicodeDecodeError` crash decoded the whole file, so invalid UTF-8 in an
  optional stamp threw the pid away — and a reader that finds no pid
  concludes no supervisor, which is what invites a second one. The file is
  read as bytes and decoded a line at a time now. Found by GPT-5.6 Sol,
  reviewing its own round-one finding's fix.

**Left open deliberately:** `_prune_loop_pid_file` re-reads and compares
before unlinking, which narrows the window between deciding and deleting from
seconds (a macOS `ps` probe) to microseconds, but does not close it — nothing
takes a lock. A lock would close it and would also introduce a failure the
current shape cannot have: a stale lock blocks *publication*, and an
unwritten pid file costs the session its `stop`, its `restart-loop` and its
row in the listing. Not worth it for a window that requires a replacement
supervisor to publish inside a parse.
