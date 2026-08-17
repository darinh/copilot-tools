---
id: 38
title: The kernel total-line ceiling is exhausted; cut the project catalogue out
status: proposed
opened: 2026-08-17
spec: none
---

## Evidence

Measured on 2026-08-17 with the repository's own scanner
(`tests/test_kernel_boundary.py::code_lines` over `kernel_modules()`):

    KERNEL  4,065 / 4,100 code lines   (35 left)
    KERNEL  8,989 / 9,000 total lines  (11 left)

The total ceiling is the binding one and it is effectively exhausted. Eleven
lines is less than one function with a docstring, so the next kernel change of
any size fails `test_the_kernel_as_a_whole_stays_under_its_total_ceiling`.

How it got here: `docs/seat-identity.md` added `paths.project_journal_file`,
`paths.seat_has_journal`, a `has_journal` argument and clause in
`build_preamble`, and one call in `supervisor.py` (commits a174594, e55627d).
That is a deliberately small kernel footprint for the feature -- the substrate
lives in `operator_memory/` and the command in `operator_cli/` -- and it still
consumed 86 of the 97 total lines that were free beforehand. Two rounds of
trimming my own prose recovered ~30 lines; a third would start deleting the
explanation the budget's own comment names as "the most damaging edit available
in this repository".

The cut is already named, twice, in the code:

* `MAX_KERNEL_CODE_LINES`' comment: the next cut is the project catalogue.
* `docs/plan.md` and the previous session's handoff say the same.

The catalogue is roughly 250 lines of `paths.py`: `catalog_guid`,
`catalog_rows`, `project_catalog_path`, `projects_root`, `project_dir`,
`guid_is_usable`, `project_handoff_file`, `project_journal_file`,
`seat_has_journal` and their prose. Reading a hand-edited CSV to map a
directory to a project id is not supervising a process, which is the test
`test_fleet_boundary.py` applies when deciding what may leave.

## Why it matters

Eleven lines of headroom means the next kernel change of any size fails the boundary test, and the cheapest way to pass a budget is to delete explanation - which is the edit this repository can least afford and which its own budget comment names as such. The guard stops being a forcing function and becomes an obstacle people route around. Making the named cut restores real headroom and moves code that was never supervision out of the supervisor.

## Notes

The extraction target is operator_kernel/paths.py's catalogue half. Constraint: the kernel must still resolve a handoff and a journal path, so whatever moves has to be reachable from build_preamble and supervisor.py - which means either the kernel keeps a thin resolver and the catalogue parsing moves, or the supervisor is handed the answers by a seam like work_seam/extension_seam. The second is how gate_change and admit_launch were handled. Note operator_memory and operator_cli already import paths for exactly these functions, so the move has two consumers outside the kernel that must keep working. Follow the arrow rule test_fleet_boundary.py holds: the new home may import the kernel, never the reverse.
