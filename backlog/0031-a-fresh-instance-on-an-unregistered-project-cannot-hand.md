---
id: 31
title: A fresh instance on an unregistered project cannot hand off at all
status: proposed
opened: 2026-08-15
spec: none
---

## Evidence

Measured 2026-08-15 in a scratch repository under `%TEMP%`, using the
`handoff_tool.py` on `work/1`:

```
$ git init; git commit -m init        # any repository not in the catalog
$ handoff --instance probe --status s --next n
Error: No catalog entry for: c:\users\darin\appdata\local\temp\anvil-repro2-06174d43
```

Exit code 1, and nothing is written anywhere. `resolve_guid` in
`handoff_tool.py` dies when `catalog_guid` finds no row, and every path that
writes the handoff or raises the restart marker is downstream of it.

Independently hit by the `subtitle-localizer` instance on 2026-08-15 in a
different repository, which is what surfaced it: that instance could not hand
off at all until it added a catalog row by hand. Reported cross-project via
`operator reply`.

The catalog is not written by anything in this toolkit. `docs/operator.md`
line 1038 states it outright: "The catalog is registration data. No code in
this repository writes it -- both `handoff_tool` and `copilot_operator` only
read it". `tests/test_enrollment_conformance.py` holds that as an invariant.
So the remedy is a human editing a CSV.

## Why it matters

The restart protocol tells a fresh instance to hand off when its context fills
up. On an unregistered repository that instruction fails closed on its first
protocol-mandated action, and the session'"'"'s accumulated context is lost with
the process -- which is the exact harm the backlog'"'"'s oldest item is named for.

The failure is silent from the fleet'"'"'s point of view: nothing records that a
handoff was attempted and refused, so an instance in this state looks like one
that simply never handed off.

## Notes

The refusal message was improved on `work/1` (commit fe1b1a9 and its
follow-up): it now names `catalog.csv`, gives the exact line, and says that
nothing in the toolkit writes the file. That makes the refusal actionable. It
does not make the protocol executable without a human, which is what this item
is about.

A degraded "write the handoff anyway and exit 0" was proposed by the reporting
instance and then withdrawn by it on the following grounds, which are worth
keeping: the catalog row is what resolves the project directory, and that
directory holds *both* the handoff file and the restart marker the supervisor
watches. Writing the text somewhere else and exiting 0 tells the agent it
handed off, raises no marker anything looks for, and leaves the successor
reading nothing -- silent context loss, which is strictly worse than a loud
refusal because nobody learns it happened. Any degraded mode has to keep "the
successor can actually find this" as its invariant.

Two candidates that do keep it, in this order:

1. **Self-registration.** Mint a guid, append the row, hand off normally. It
   makes every invocation a potential writer of a permanent row, so it needs
   an idempotency key or the catalog accumulates junk from typo'd working
   directories and transient temp dirs, and two agents starting in one
   repository at once can race to mint two guids for it. Keying on the
   canonical repository root -- `git rev-parse --show-toplevel`, normalised
   the way `project_paths` already normalises -- makes a double registration
   collapse instead of fork. Decide deliberately what a cwd that is not a git
   repository at all should do; minting a project per arbitrary directory is
   probably not wanted.
2. **Persist on refusal**, as the backstop for when self-registration itself
   cannot run: not a git repository, catalog not writable, read-only mount.
   The composed handoff text should not die with the process.

A third path already exists and was what actually rescued the reporting
instance: the session store rows carry `cwd` and `repository`, and it
recovered by querying prior sessions for that directory and reading the last
turns -- no catalog, no handoff file. That is a discovery path keyed on the
repository rather than on the catalog. If the composed text landed in the
session record on refusal, a successor could find it by the same query, and
the fallback would stop depending on a human noticing.

Not yet measured: how many of the fleet'"'"'s projects are actually uncatalogued.
That number decides whether this is a trap for new repositories only or a
live gap across the fleet.
