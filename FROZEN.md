# Frozen: safety fixes only

This repository runs the live fleet. The supervision kernel has been extracted
to `C:\Users\darin\repos\operator` and development continues there.

**What may still land here:** fixes for defects that affect running sessions.
Nothing else.

**Why the line is drawn hard.** Two systems that both accept features is how you
get two systems forever: the new one never becomes sufficient because the old
one keeps closing the gap, and the old one never gets retired because work keeps
arriving in it. The freeze is what makes the extraction finishable.

Anything that is not a safety fix belongs in the kernel repository, or in the
backlog here until the kernel can take it.

Frozen 2026-08-12, the day the kernel repository took its first commit
(`1c46cff`), per the plan at `../operator/docs/plan.md`.
