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

## Done when

- A `proposed` row in `backlog list` carries the reason it is not workable, taken
  from `why_not_workable` rather than restated, so the two cannot drift apart.
- An agent that runs only `list` cannot read an ineligible item as available. The
  test for this asserts the reason text appears on the row for each ineligible
  item, with a control that an `open` item carries no such annotation -- otherwise
  the assertion passes against an implementation that annotates every row.
- `list` still shows every item including `proposed`. Suppression is ruled out
  below and must stay ruled out.
- `backlog ready` output is byte-identical to what it prints today.

## Not in scope

- The gate itself, the `blocks` hatch, and anything about which items are
  workable. This item is about what `list` *says*, not about what is allowed.
- `scrum`, `html` and `show`. If the same annotation is free in them, it is a
  bonus, not a requirement.
- Suppressing `proposed` items from `list`, which is argued against below.

## Risk

🟢 `backlog_tool.py::_cmd_list` and its `--help` text. Presentation only: no
status transitions, no writes to `backlog/`.

## Notes

A fix should make `list` unable to misinform, rather than documenting the difference somewhere an agent has to find first -- e.g. annotating each ineligible row with its `why_not_workable` reason. Suppressing proposed items from `list` would be the wrong direction: `why_not_workable`'s own docstring argues that a queue which silently omits an item teaches an agent nothing.
