# copilot-tools

Tools, skills, and workflow conventions for GitHub Copilot CLI power users. Built by the team at Microsoft for autonomous and interactive AI-assisted development.

## What's Inside

| Component | Description |
|-----------|-------------|
| [`operator.sh`](docs/operator.md) | Copilot CLI wrapper with metrics capture, autonomous loop mode, and multi-instance support |
| [`operator-ingest.py`](operator-ingest.py) | Log parser for copilot process logs |
| [`skills/code-intelligence`](skills/code-intelligence/SKILL.md) | MCP skill routing C#→Roslyn, TypeScript→codebase-memory-mcp |
| [`extensions/`](extensions/README.md) | Copilot CLI runtime extensions: open-in-vs-code, lint-on-edit, security-shield, test-enforcer, architecture-enforcer, copy-to-clipboard-tool |
| [`templates/`](templates/) | Configuration templates for copilot-instructions, MCP servers, and per-project setup |
| [`docs/`](docs/) | Documentation for operator and skills |
| [`setup.sh`](setup.sh) | Automated environment setup script |

## Quick Start

```bash
git clone <this-repo> ~/projects/copilot-tools
cd ~/projects/copilot-tools
chmod +x setup.sh operator.sh
./setup.sh
```

The setup script will:
1. Check prerequisites (`tmux`, `sqlite3`, `python3`, `copilot`)
2. Symlink `operator` into `~/.local/bin/`
3. Install the [Anvil](https://github.com/burkeholland/anvil) agent plugin
4. Symlink runtime extensions (`extensions/`) into `~/.copilot/extensions/`
5. Check/install MCP servers (codebase-memory-mcp, dotnet-roslyn-mcp)
6. Install configuration templates to `~/.copilot/`

## Usage

```bash
# Interactive session with Anvil agent
operator --agent=anvil:anvil --yolo

# Autonomous loop (restarts when agent signals)
operator --loop --name myproject --agent=anvil:anvil

# Restart later — auto-continues from where it left off
operator --loop --name myproject --agent=anvil:anvil

# Reset session numbering
operator --loop --name myproject --fresh --agent=anvil:anvil

# Multiple concurrent loops
operator --loop --name frontend --agent=anvil:anvil
operator --loop --name backend --agent=anvil:anvil
operator list

# Usage reports
operator report costs
```

Named loop instances persist the active Copilot CLI session ID, so restarting the same loop after a WSL crash or Windows reboot automatically resumes the prior CLI session.

See [Operator Documentation](docs/operator.md) for full details.

## Skills & Plugins

| Skill | Type | Source |
|-------|------|--------|
| **code-intelligence** | Project skill (copy to `.github/skills/`) | Included in this repo |
| **Anvil** | Installable plugin | [`burkeholland/anvil`](https://github.com/burkeholland/anvil) |
| **frontend-design** | Built-in CLI skill | Ships with Copilot CLI |
| **find-skills** | Built-in CLI skill | Ships with Copilot CLI |

See [Skills Reference](docs/skills.md) for details and setup.

## Workflow Conventions

The `templates/copilot-instructions.md` file establishes conventions for:
- **Project Configuration System** — per-project settings stored in `~/.copilot/projects/`
- **Session Handoff** — cross-session continuity via `next-session.md` files
- **Session History** — SQL-based audit trail of work across sessions
- **Spec-Driven Development** — specs as the single source of truth (opt-in)
- **Branching Strategy** — develop → feature branches with conventional commits

Copy to `~/.copilot/copilot-instructions.md` and customize for your workflow.

## MCP Servers

Two MCP servers provide structural code intelligence:

| Server | Language | Install |
|--------|----------|---------|
| **codebase-memory-mcp** | TypeScript/JS, git analysis | Go binary (see team distribution) |
| **dotnet-roslyn-mcp** | C# | `dotnet tool install -g dotnet-roslyn-mcp` |

Configure in `~/.copilot/mcp-config.json` — see [`templates/mcp-config.json`](templates/mcp-config.json).

## Repository Structure

```
copilot-tools/
├── operator.sh              # Copilot CLI wrapper
├── operator-ingest.py       # Log parser
├── setup.sh                 # Environment setup
├── skills/
│   └── code-intelligence/
│       └── SKILL.md          # Roslyn + codebase-memory routing
├── templates/
│   ├── copilot-instructions.md    # Workflow conventions
│   ├── mcp-config.json            # MCP server config
│   └── project-instructions.md    # Per-project template
└── docs/
    ├── operator.md           # Operator documentation
    └── skills.md             # Skills reference
```
