---
id: 6
title: Add a Project Configurations screen to the operator menu for per-project feature toggles
status: closed
opened: 2026-08-05
closed: 2026-08-05
commit: 3c531b9
spec: none
---

## Evidence

Measured 2026-08-05 on this machine.

Eight projects are catalogued in `~/.copilot/projects/catalog.csv`, and all
eight have a `copilot-instructions.md` in their per-project directory:

```
catalog rows: 8
108998fd-...  instructions=True     7dbf3eb0-...  instructions=True
1ba0c7c1-...  instructions=True     a8a09276-...  instructions=True
4ca9102c-...  instructions=True     c48add2d-...  instructions=True
7ad67aec-...  instructions=True     dfe1a7fc-...  instructions=True
```

Every one of those files was written by hand, by an agent, during a setup
conversation. There is no command that reads which features a project has
enabled, and none that changes one. The feature list itself exists only as a
markdown table inside the instructions prose:

> | Feature | Description | Default |
> | **Session Handoff** | ... | ON |
> | **Session History** | ... | ON |
> | **Spec-Driven Development** | ... | ON |
> | **Parallel Agents** | ... | ON |
> | **Branching Strategy** | ... | ON |
> | **Tracked Backlog** | ... | ON |

Turning one off today means an agent editing prose in a file no tool parses.

## Why it matters

The feature flags are the contract between a project and every agent that
works in it, and that contract is currently editable only by free-text
agent-authored prose. Two consequences follow:

- **Drift is undetectable.** Nothing reads the table, so nothing can notice a
  project whose enabled-features line disagrees with the sections beneath it.
  This repository already has a conformance test for exactly that failure in
  its own template (`test_instructions_template.py`) -- which is proof the
  failure mode is real, and that the check does not extend to deployed
  per-project copies.
- **The human cannot see or change the configuration without an agent.** The
  operator has a menu; the thing a user most often needs to adjust is not on
  it.

## Notes

Requested shape, from the user: a new **Project Configurations** entry in the
operator menu that lists catalogued projects, lets one be selected, and shows a
submenu of available features. Sketched by the user as:

- Use Spec Kit
- Backlog
  - Use `backlog/` folder
  - Use GitHub Issues
  - No backlog
- Agent Operator Commands (spawning agents, mail between agents)
- ...and the remaining features named in the instructions file

Two observations worth carrying into refinement:

- **Backlog is not a boolean**, unlike the other flags -- it is a choice of one
  of three backends. A toggle list cannot express it. Whatever renders the menu
  has to support at least "flag" and "choice of one", and the enforcement tests
  in `test_backlog_conformance.py` must not fire on a project that chose GitHub
  Issues or none.
- **The feature list needs a single owner**, the same way `backlog_tool` owns
  the status vocabulary. If the menu enumerates features and the template
  enumerates features, the two lists will disagree, and the disagreement will
  surface as a menu that silently cannot toggle something. This repository
  already learned that lesson once: `test_workflow_discovery_conformance.py`
  exists because a duplicated glob let a `.yaml` workflow escape every
  assertion in the suite.
