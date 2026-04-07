# copilot-tools

Tools, skills, and workflow conventions for GitHub Copilot CLI power users. Built for autonomous and interactive AI-assisted development.

**Cross-platform**: Works on Linux, macOS, and Windows (via [psmux](https://github.com/psmux/psmux)).

## What's Inside

| Component | Description |
|-----------|-------------|
| [`copilot_operator.py`](docs/operator.md) | Cross-platform Copilot CLI wrapper with metrics capture, autonomous loop mode, and multi-instance support |
| [`operator_ingest.py`](operator_ingest.py) | Log parser for copilot process logs |
| [`skills/code-intelligence`](skills/code-intelligence/SKILL.md) | MCP skill routing C#→Roslyn, TypeScript→codebase-memory-mcp |
| [`templates/`](templates/) | Configuration templates for copilot-instructions, MCP servers, and per-project setup |
| [`docs/`](docs/) | Documentation for operator and skills |
| [`setup_tools.py`](setup_tools.py) | Cross-platform environment setup script |
| [`operator.sh`](operator.sh) | Legacy bash wrapper (Linux/macOS only) |

## Quick Start

### Linux / macOS

```bash
git clone https://github.com/darinh/copilot-tools ~/projects/copilot-tools
cd ~/projects/copilot-tools
python3 setup_tools.py
```

### Windows

```powershell
# Install psmux (native tmux for Windows)
winget install psmux

git clone https://github.com/darinh/copilot-tools $HOME\projects\copilot-tools
cd $HOME\projects\copilot-tools
python setup_tools.py
```

### Prerequisites

| Tool | Linux/macOS | Windows |
|------|------------|---------|
| Terminal multiplexer | `tmux` | [`psmux`](https://github.com/psmux/psmux) (`winget install psmux`) |
| Python | `python3` (3.8+) | `python` (3.8+) |
| Git | `git` | `git` |
| Copilot CLI | `copilot` | `copilot` |

The setup script checks for all prerequisites and guides installation.

## Usage

```bash
# Interactive session with Anvil agent
copilot-operator --agent=anvil:anvil --yolo

# Autonomous loop (restarts when agent signals)
copilot-operator --loop --name myproject --agent=anvil:anvil

# Restart later — auto-continues from where it left off
copilot-operator --loop --name myproject --agent=anvil:anvil

# Reset session numbering
copilot-operator --loop --name myproject --fresh --agent=anvil:anvil

# Multiple concurrent loops
copilot-operator --loop --name frontend --agent=anvil:anvil
copilot-operator --loop --name backend --agent=anvil:anvil
copilot-operator list

# Usage reports
copilot-operator report costs

# Or run directly without pip install:
python copilot_operator.py --agent=anvil:anvil --yolo
```

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
├── copilot_operator.py      # Cross-platform operator (Python)
├── operator_ingest.py       # Log parser (Python, cross-platform)
├── operator.sh              # Legacy bash operator (Linux/macOS)
├── setup_tools.py           # Cross-platform setup (Python)
├── setup.sh                 # Legacy bash setup (Linux/macOS)
├── pyproject.toml           # Python package config
├── skills/
│   └── code-intelligence/
│       └── SKILL.md          # Roslyn + codebase-memory routing
├── templates/
│   ├── copilot-instructions.md    # Workflow conventions
│   ├── mcp-config.json            # MCP server config
│   └── project-instructions.md    # Per-project template
├── tests/
│   ├── conftest.py           # Shared test fixtures
│   ├── test_ingest.py        # Ingest tests (57 tests)
│   └── test_operator.py      # Operator tests
├── .github/
│   └── workflows/
│       └── ci.yml            # CI: ubuntu + windows, Python 3.10 + 3.12
└── docs/
    ├── operator.md           # Operator documentation
    └── skills.md             # Skills reference
```
