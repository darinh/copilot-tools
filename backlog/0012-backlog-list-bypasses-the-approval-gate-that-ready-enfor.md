---
id: 12
title: backlog list bypasses the approval gate that ready enforces
status: proposed
opened: 2026-08-05
spec: none
---

## Evidence

On 2026-08-08 a supervised agent ran `backlog_tool.py list` to orient itself, read the
row `11  proposed  ...` as an available item, and worked it: new branch, a six-minute
full-suite baseline, an implementation, and 28 tests. None of it was approved.

`backlog ready --explain`, on the same tree at the same moment, printed:

       11  not workable: awaiting approval by the product owner, and it names no
           approved item that it blocks

The gate was correct and functioning. It was never consulted. `_cmd_list` does not call
`why_not_workable`, prints proposed and open rows in the same shape, and neither its
output nor its `--help` says it is not the eligibility query. `_cmd_ready` -- "The queue
an agent may work, and why everything else is not in it" -- is a separate subcommand an
agent has to already know to prefer.

## Why it matters

The gate's whole value is refusing an agent that does not already intend to be refused. An agent that knows to run `ready` did not need the gate. The command an agent actually reaches for when orienting is the one that answers as if every item were available.

## Notes

A fix should make `list` unable to misinform, rather than documenting the difference somewhere an agent has to find first -- e.g. annotating each ineligible row with its `why_not_workable` reason. Suppressing proposed items from `list` would be the wrong direction: `why_not_workable`'s own docstring argues that a queue which silently omits an item teaches an agent nothing.
