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
| `setup.sh` / `setup.ps1` | Environment setup (Linux/WSL / Windows). |
| `upgrade.sh` / `upgrade.ps1` | One-step `git pull --ff-only` + re-run setup. |
| `lib/migrate-operator-state.sh` | Idempotent migration helper from legacy `~/.copilot/projects/` to `~/.operator/projects/`. |

## Quick Start

### Linux / macOS / WSL

```bash
git clone https://github.com/darinh/copilot-tools.git
cd copilot-tools
chmod +x setup.sh operator.sh
./setup.sh
```

### Windows (PowerShell)

```powershell
git clone https://github.com/darinh/copilot-tools.git
cd copilot-tools
.\setup.ps1
```

The Windows setup installs Node extensions and templates natively into
`%USERPROFILE%\.copilot\`, drops `operator.cmd` / `handoff.cmd` shims into
`%USERPROFILE%\.local\bin\` (added to your user PATH) so agent instructions
can just say `operator …` regardless of platform, and — if WSL is present —
shells into WSL to run `bash setup.sh` so the operator/handoff bash scripts
work there too. **WSL is required for `operator` and `handoff` on Windows**
(they're bash + tmux); without it the rest still installs and `operator help`
will tell you what's missing.

The setup script will:
1. Check prerequisites (`node`, `npm`, `git`, `copilot`; plus `tmux`, `sqlite3`, `python3` in WSL)
2. Install/refresh `operator` and `handoff` on your PATH (`~/.local/bin/` on Linux/WSL, `%USERPROFILE%\.local\bin\` on Windows)
3. Install the [Anvil](https://github.com/burkeholland/anvil) agent plugin (best effort)
4. Link runtime extensions (`extensions/`) into `~/.copilot/extensions/` (junction/symlink/copy fallback on Windows)
5. Check/install MCP servers (codebase-memory-mcp, dotnet-roslyn-mcp)
6. Install configuration templates to `~/.copilot/` (smart-upgrade via hash manifest — never clobbers your edits without asking)
7. Migrate any legacy operator state from `~/.copilot/projects/` to `~/.operator/projects/`

## Upgrade

Both upgrade scripts refuse on a dirty working tree, `git fetch && git pull --ff-only`, and re-run the matching setup script (which is idempotent — no-op when nothing changed):

```bash
./upgrade.sh           # Linux / macOS / WSL
.\upgrade.ps1          # Windows
```

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
- **Project Configuration System** — per-project settings stored in `~/.operator/projects/`
- **Session Handoff** — cross-session continuity via `next-session.md` files
- **Session History** — SQL-based audit trail of work across sessions
- **Spec-Driven Development** — specs as the single source of truth (opt-in)
- **Branching Strategy** — develop → feature branches with conventional commits

Copy to `~/.copilot/copilot-instructions.md` and customize for your workflow. On Windows, `setup.ps1` prepends a short note explaining that `operator` / `handoff` commands route through WSL.

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
├── operator.sh              # Copilot CLI wrapper (bash + tmux)
├── handoff.sh               # Session-handoff helper
├── operator-ingest.py       # Log parser
├── setup.sh / setup.ps1     # Environment setup (Linux+WSL / Windows)
├── upgrade.sh / upgrade.ps1 # Pull + re-run setup
├── lib/
│   └── migrate-operator-state.sh   # Legacy-path migration helper
├── skills/
│   └── code-intelligence/
│       └── SKILL.md          # Roslyn + codebase-memory routing
├── templates/
│   ├── copilot-instructions.md     # Workflow conventions
│   ├── mcp-config.json             # MCP server config (Linux paths)
│   ├── mcp-config.windows.json     # MCP server config (Windows paths)
│   └── project-instructions.md     # Per-project template
└── docs/
    ├── operator.md           # Operator documentation
    └── skills.md             # Skills reference
```
