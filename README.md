# copilot-tools

Tools, skills, and workflow conventions for GitHub Copilot CLI power users. Built by the team at Microsoft for autonomous and interactive AI-assisted development.

## What's Inside

| Component | Description |
|-----------|-------------|
| [`operator.sh`](docs/operator.md) | Copilot CLI wrapper with metrics capture, autonomous loop mode, and multi-instance support |
| [`operator-ingest.py`](operator-ingest.py) | Log parser for copilot process logs |
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
| `operator.sh` / `handoff.sh` | ❌ not yet | ✅ | ✅ |
| `setup.sh` | ❌ not yet | ✅ | ✅ |

The operator currently requires a POSIX shell and `tmux`. Native Windows support is specified in
[`specs/003-windows-native-operator/`](specs/003-windows-native-operator/) and is not yet implemented —
on Windows, run the operator inside WSL.

## Quick Start

**bash (Linux/macOS/WSL)**

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
5. Check/install MCP servers (dotnet-roslyn-mcp)
6. Conditionally install the Spec Kit CLI if not already present
7. Install configuration templates to `~/.copilot/`

**PowerShell (Windows)**

`setup.sh` is POSIX-only, and the operator it installs does not run natively on Windows yet. Clone the
repository and install the configuration templates directly — these are the parts that work on Windows
today:

```powershell
git clone <this-repo> $HOME\repos\copilot-tools
cd $HOME\repos\copilot-tools
New-Item -ItemType Directory -Force $HOME\.copilot | Out-Null
Copy-Item templates\copilot-instructions.md $HOME\.copilot\copilot-instructions.md
Copy-Item templates\mcp-config.json $HOME\.copilot\mcp-config.json
```

To use the operator on Windows, run it inside WSL following the bash instructions above.

See [Spec Kit Workflow](docs/spec-kit.md) for project initialization, commands,
upgrades, and parallel-agent coordination.

## Usage

> The `operator` command requires Linux, WSL, or macOS. See [Platform Support](#platform-support).

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
├── operator.sh              # Copilot CLI wrapper
├── operator-ingest.py       # Log parser
├── setup.sh                 # Environment setup
├── skills/
│   └── code-intelligence/
│       └── SKILL.md          # Roslyn routing
├── templates/
│   ├── copilot-instructions.md    # Workflow conventions
│   ├── mcp-config.json            # MCP server config
│   └── project-instructions.md    # Per-project template
├── tests/                      # Setup and todo-coordination tests
└── docs/
    ├── operator.md           # Operator documentation
    ├── skills.md             # Skills reference
    └── spec-kit.md           # GitHub spec-kit documentation
```
