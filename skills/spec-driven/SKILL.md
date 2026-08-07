---
name: spec-driven
description: The GitHub spec-kit workflow — constitution, specify, clarify, plan, tasks, implement, analyze — and deciding whether a change needs a spec update. Load this skill before running any /speckit-* command or writing to specs/.
---

# Spec-driven development

GitHub spec-kit is authoritative. Specifications live in `.specify/` and `specs/`.

## Workflow

`/speckit-constitution` → `/speckit-specify` → `/speckit-clarify` → `/speckit-plan`
→ `/speckit-tasks` → `/speckit-implement` → `/speckit-analyze`

`/speckit-converge` re-reads the codebase against the feature's artifacts and
appends whatever is still unbuilt to `tasks.md`. Use it when you inherit a
part-finished feature rather than re-deriving its state by reading code.

## Discovery

Read `.specify/memory/constitution.md` and the relevant spec **first**. Read code
when implementing, or when the spec has a known gap. Reading code to answer a
question the spec already answers is how specs drift out of use: the spec stops
being consulted, then stops being updated, then stops being true, in that order.

## Rules

- Specs describe what **is**, never what should be. No aspirational claims.
- Every spec claim references real code: file paths, function names, types.
- A code change without a corresponding spec update is unfinished.
- Update the spec in the same commit as the code, not a follow-up.
- Record the *reasons*, not just the outcomes. A decision table that says what was
  chosen and not why is re-litigated by the next agent at full price.

## When the spec is silent

A task whose requirement is not in `spec.md` is not a task without a requirement —
it is a task whose requirement lives somewhere you have not looked. Go to the
source the plan was derived from before inventing an interpretation. Inventing one
is how a plan quietly becomes a different plan.

If the requirement genuinely does not exist anywhere, that is a finding: say so,
decide, and write the decision into the spec in the same commit.

## Known gaps

State them rather than faking them. A spec that lists what it does not yet cover
is usable; one that reads as complete while being partial costs the next reader
their confidence in all of it.

## Validation

When reviewing completed work:

- Every code change is reflected in the specification artifacts.
- Every spec claim references actual code.
- No aspirational claims.
- `tasks.md` checkboxes match what was actually delivered, and the prose beside a
  ticked task says what it delivered, not what it intended to.
