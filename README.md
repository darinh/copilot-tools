# copilot-tools

Tools, skills, and workflow conventions for GitHub Copilot CLI power users. Built by the team at Microsoft for autonomous and interactive AI-assisted development.

## What's Inside

| Component | Description |
|-----------|-------------|
| [`copilot_operator.py`](docs/operator.md) | Cross-platform Copilot CLI wrapper with metrics capture, autonomous loop mode, and multi-instance support |
| [`operator_runner.py`](docs/operator.md#architecture) | In-pane session supervisor: correct process attribution and metrics after detach |
| [`operator_mux.py`](docs/operator.md#platform-support) | Session-backend abstraction (tmux / psmux) |
| [`operator_ingest.py`](operator_ingest.py) | Pure-Python log parser for copilot process logs |
| [`handoff_tool.py`](docs/operator.md) | Atomic session handoff for agents |
| [`operator.sh`](operator.sh), [`handoff.sh`](handoff.sh), [`operator-ingest.py`](operator-ingest.py) | Original bash implementation, retained unchanged for existing Linux/WSL users |
| [`skills/code-intelligence`](skills/code-intelligence/SKILL.md) | Roslyn-backed C# structural analysis |
| [`extensions/`](extensions/README.md) | Copilot CLI runtime extensions: open-in-vs-code, lint-on-edit, security-shield, test-enforcer, architecture-enforcer, copy-to-clipboard-tool |
| [`templates/`](templates/) | Configuration templates for copilot-instructions, MCP servers, and per-project setup |
| [`docs/`](docs/) | Documentation for operator, skills, and spec-kit |
| [`setup.sh`](setup.sh) | Automated environment setup script |

## Platform Support

| Component | Windows | Linux / WSL | macOS |
|-----------|---------|-------------|-------|
| Workflow conventions & templates | ✅ | ✅ | ✅ |
| Spec Kit workflow | ✅ | ✅ | ✅ |
| Runtime extensions | ✅ | ✅ | ✅ |
| `operator` / `handoff` (Python) | ✅ | ✅ | ✅ |
| `operator.sh` / `handoff.sh` (bash, legacy) | ❌ | ✅ | ✅ |

The Python implementation is the supported entry point on every platform. The
original bash scripts are retained unchanged for existing Linux and WSL users
and will be retired once the Python path has proven parity in daily use.

Session management uses a terminal multiplexer: **psmux** on Windows, **tmux**
elsewhere. See [Operator](docs/operator.md) for details.

## Quick Start

**PowerShell (Windows)**

```powershell
winget install --id marlocarlo.psmux
git clone <this-repo> $HOME\repos\copilot-tools
cd $HOME\repos\copilot-tools
python setup_tools.py
```

**bash (Linux/macOS/WSL)**

```bash
git clone <this-repo> ~/projects/copilot-tools
cd ~/projects/copilot-tools
python3 setup_tools.py
```

`setup_tools.py` is cross-platform. It will:

1. Check prerequisites (multiplexer, Python 3.10+, `copilot`, `git`)
2. Install the `operator`, `handoff` and `operator-ingest` console scripts
3. Link runtime extensions into `~/.copilot/extensions/`
4. Install configuration templates to `~/.copilot/`

`sqlite3` is **not** required — the toolkit uses Python's standard-library
`sqlite3` module.

<details>
<summary>Legacy bash setup (Linux/WSL only)</summary>

```bash
chmod +x setup.sh operator.sh
./setup.sh
```

This installs the original bash `operator.sh`, symlinks it into
`~/.local/bin`, and additionally installs the Anvil plugin, MCP servers and the
Spec Kit CLI.
</details>

See [Spec Kit Workflow](docs/spec-kit.md) for project initialization, commands,
upgrades, and parallel-agent coordination.

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

On Windows, `operator` also tracks which Windows Terminal tabs are running named instances. After a reboot or crash, `operator restore` lists tracked tabs so you can pick which to reopen (or `operator restore --all` for everything), replaying each launch command in a fresh tab so every Copilot session resumes where it left off — no manual re-`cd`-ing or hunting for session IDs.

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
- **Spec-Driven Development** — specs as the single source of truth (enabled by default)
- **Parallel Agents** — SQL-coordinated parallel task execution via `todo_claims`
- **Branching Strategy** — develop → feature branches with conventional commits

Copy to `~/.copilot/copilot-instructions.md` and customize for your workflow.

## MCP Servers

An optional MCP server provides structural code intelligence:

| Server | Language | Install |
|--------|----------|---------|
| **dotnet-roslyn-mcp** | C# | `dotnet tool install -g dotnet-roslyn-mcp` |

Configure in `~/.copilot/mcp-config.json` — see [`templates/mcp-config.json`](templates/mcp-config.json).

## Repository Structure

```
copilot-tools/
├── .github/
│   ├── copilot-instructions.md
│   └── skills/speckit-*/       # Copilot skills generated by Spec Kit
├── .specify/                   # Spec Kit templates, scripts, and constitution
├── specs/                      # Feature specifications, plans, and tasks
├── copilot_operator.py      # Operator CLI (all platforms)
├── operator_runner.py       # In-pane session supervisor
├── operator_mux.py          # Session-backend abstraction (tmux / psmux)
├── operator_ingest.py       # Pure-Python log parser
├── operator_console.py      # UTF-8 console output
├── handoff_tool.py          # Session handoff
├── setup_tools.py           # Cross-platform environment setup
├── pyproject.toml           # Packaging and console scripts
├── operator.sh              # Legacy bash wrapper (Linux/WSL)
├── operator-ingest.py       # Legacy log parser, used by operator.sh
├── handoff.sh               # Legacy bash handoff (Linux/WSL)
├── setup.sh                 # Legacy bash setup (Linux/WSL)
├── skills/
│   └── code-intelligence/
│       └── SKILL.md          # Roslyn routing
├── templates/
│   ├── copilot-instructions.md    # Workflow conventions
│   ├── mcp-config.json            # MCP server config
│   └── project-instructions.md    # Per-project template
├── tests/                      # pytest suite + bash coordination tests
└── docs/
    ├── operator.md           # Operator documentation
    ├── skills.md             # Skills reference
    └── spec-kit.md           # GitHub spec-kit documentation
```
