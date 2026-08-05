---
id: 18
title: Project directories stranded in ~/.copilot when a supervisor is running older code
status: proposed
opened: 2026-08-05
spec: none
---

## Evidence

After the catalog moved to `~/.operator/projects`, one project directory was left
behind by the collision guard and stays behind on every subsequent run.

Measured 2026-08-05 immediately after merging the move. `operator list` logged:

    C:\Users\darin\.operator\projects\1ba0c7c1-... already exists —
    C:\Users\darin\.copilot\projects\1ba0c7c1-... left in place, not merged

`~/.copilot/projects/1ba0c7c1-...` still contains `next-session.md` (7050 bytes,
written 13:51) and a `probes/` directory holding a 185 MB `fleet-0010.db`. The
destination had no `next-session.md` at all, so the handoff was reachable by
nothing: the old-code supervisor writes to the legacy path, and new code reads
only the new one. It was copied across by hand, and the legacy copy retained.

The cause is that `operator list` reported five looping supervisors as
"running older code" / "code unrecorded". A supervisor imports operator once
and keeps it for the whole run, so those five keep writing to `~/.copilot`
until they restart, after the migration has already run.

## Why it matters

The migration correctly refuses to overwrite, which is the right call, but the
result is a split: two directories for one project, with the newer file in the
one nothing reads. That is the failure the move was meant to prevent, arriving
by a different route.

It self-heals for any project whose supervisor restarts before writing again.
It does not self-heal for a long-running loop, and the longer the loop runs the
more likely it is that the only copy of a handoff is in the abandoned half.

## Notes

Not fixed, and deliberately left for review rather than actioned:

- Deleting the legacy directory is not safe unattended; `superseded/` is
  promised never to be pruned, and probes/ holds real data.
- Merging entry-by-entry into the live directory would overwrite a newer
  handoff with an older one, which the guard exists to prevent.
- The honest fix is probably to make the retire/migrate path report the
  stranded directory to the user and let them choose, rather than to pick.

The immediate operational answer is to restart the five looping supervisors so
they pick up the new code.
