# Skills Reference

Skills extend Copilot CLI agents with specialized capabilities. They're markdown files that get loaded into the agent's context when invoked.

## Included in this repo

These skills install for the **user**, not for one project: `setup.ps1` /
`setup.sh` copy them to `~/.copilot/skills/<name>/`, so they are available in
every project on the machine. Project-level skills (`.github/skills/`) are
shared with everyone who clones that repo; user-level skills follow you.

### peer-agents

**Location**: `skills/peer-agents/SKILL.md`
**Install**: automatic — `setup.ps1` / `setup.sh` install it to `~/.copilot/skills/`

How to use `operator` to run **peer agents** rather than sub-agents: when
delegating a bounded piece of work is worth it, how to start a loop headlessly
so your own terminal is not taken over, why each agent wants its own directory,
and how agents message each other. Messages are delivered — live, or printed at
the start of the recipient's next session — and answered with `operator reply`;
there is no mailbox to poll. Replaces the former `operator-agents` skill, which
described the polling model.

### worktrees

**Location**: `skills/worktrees/SKILL.md`
**Install**: automatic — `setup.ps1` / `setup.sh` install it to `~/.copilot/skills/`

Creating, finishing, recovering and safely delegating git worktrees: resolving
the *primary* root from inside one, the `operator worktree new/finish/recover`
lifecycle, why scratch files never go in a checkout, and what to do when a
subagent has run a mutating git command in your tree.

### backlog

**Location**: `skills/backlog/SKILL.md`
**Install**: automatic — `setup.ps1` / `setup.sh` install it to `~/.copilot/skills/`

The tracked `backlog/` directory: one file per item, the
`proposed → open → closed | rejected` vocabulary and the approval gate it
exists to express, what counts as evidence, and when work belongs in a spec
instead. The `operator-backlog-*` skills below are the slash-command
procedures; this is the format and the reasoning behind it.

### spec-driven

**Location**: `skills/spec-driven/SKILL.md`
**Install**: automatic — `setup.ps1` / `setup.sh` install it to `~/.copilot/skills/`

The spec-kit workflow, what to read before writing code, and the rule that a
code change without a spec update is unfinished. Includes what to do when a
task's requirement is not in `spec.md` — go to the source the plan came from,
rather than inventing an interpretation.

### field-notes

**Location**: `skills/field-notes/SKILL.md`
**Install**: automatic — `setup.ps1` / `setup.sh` install it to `~/.copilot/skills/`

The cross-project journal about working with AI agents: when an insight is
transferable enough to write down, when it belongs in session history instead,
and the format.

### code-intelligence

**Location**: `skills/code-intelligence/SKILL.md`
**Install**: automatic — `setup.ps1` / `setup.sh` install it to `~/.copilot/skills/`

Use Roslyn for C# structural questions. For other languages, use the built-in
code intelligence, LSP, or search tools in your environment.

### operator-backlog-newitem

**Location**: `skills/operator-backlog-newitem/SKILL.md`
**Install**: automatic — `setup.ps1` / `setup.sh` install it to `~/.copilot/skills/`

File something into the tracked backlog the moment it surfaces, rather than
losing it to the end of a session. Items are filed as `proposed` — filing is
not approving — and the skill's rule for when to file unprompted is
asymmetric on purpose: a filed item the owner rejects costs one line of
review, and an unfiled one costs the whole observation.

### operator-backlog-refinement

**Location**: `skills/operator-backlog-refinement/SKILL.md`
**Install**: automatic — `setup.ps1` / `setup.sh` install it to `~/.copilot/skills/`

Walk the queue with the product owner: what is awaiting approval, what each
item actually claims, and `backlog approve` for the ones that should be worked.
Approving, rejecting and prioritising are the owner's decisions; the skill
prepares them and does not make them.

### operator-backlog-scrum

**Location**: `skills/operator-backlog-scrum/SKILL.md`
**Install**: automatic — `setup.ps1` / `setup.sh` install it to `~/.copilot/skills/`

The periodic check-in: commits since the last one, backlog files touched, what
is ready and what is waiting on you. The watermark it measures from lives in
the per-project directory, so it survives the session that wrote it, and
`--peek` reports a period without consuming it.

To install a skill into a single project instead, copy its directory into that
project's `.github/skills/`:

**PowerShell (Windows)**
```powershell
Copy-Item -Recurse copilot-tools\skills\code-intelligence your-project\.github\skills\
```

**bash (Linux/macOS/WSL)**
```bash
cp -r copilot-tools/skills/code-intelligence your-project/.github/skills/
```

## Built-in Copilot CLI Skills

These ship with the Copilot CLI and are available automatically.

### frontend-design

Creates distinctive, production-grade frontend interfaces. Use when building web components, pages, dashboards, React components, or HTML/CSS layouts. Generates polished UI code that avoids generic AI aesthetics.

*Triggered automatically when you ask about building frontend UI.*

### find-skills

Helps discover and install agent skills. Use when looking for functionality that might exist as an installable skill.

*Triggered automatically when you ask "how do I do X" or "is there a skill for..."*

## Installable Plugins

### Anvil

**Source**: [burkeholland/anvil](https://github.com/burkeholland/anvil)
**Install**: `copilot plugin install burkeholland/anvil`

Evidence-first coding agent. Verification loop:
1. Understands and boosts your prompt
2. Surveys codebase for reuse opportunities
3. Captures baseline state
4. Implements changes
5. Verifies with multi-tier checks (syntax, build, lint, tests)
6. Adversarial review by a different AI model
7. Presents evidence bundle with confidence rating

Key features:
- SQL-tracked verification ledger (prevents hallucinated "tests passed" claims)
- Pushback on bad requests (implementation AND requirements level)
- Automatic rollback if verification fails
- Multi-model adversarial code review

## MCP Servers

Skills often depend on MCP (Model Context Protocol) servers for tooling access.

### dotnet-roslyn-mcp

C# code intelligence powered by the Roslyn compiler. Provides:
- `find_references` — all references to a symbol
- `find_implementations` — interface implementations
- `find_callers` — who calls a method
- `get_symbol_info` — type info, documentation
- `search_symbols` — find symbols by name

**Install**: `dotnet tool install -g dotnet-roslyn-mcp`

### Configuration

Roslyn is configured in `~/.copilot/mcp-config.json`. See `templates/mcp-config.json` for the template.
