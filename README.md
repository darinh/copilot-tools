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
| [`operator.sh`](operator.sh), [`handoff.sh`](handoff.sh), [`operator-ingest.py`](operator-ingest.py) | Original bash implementation, retained unchanged on disk for rollback but no longer installed fresh by `setup.sh` |
| [`skills/code-intelligence`](skills/code-intelligence/SKILL.md) | Roslyn-backed C# structural analysis |
| [`extensions/`](extensions/README.md) | Copilot CLI runtime extensions: open-in-vs-code, lint-on-edit, security-shield, test-enforcer, architecture-enforcer, copy-to-clipboard-tool |
| [`templates/`](templates/) | Configuration templates for copilot-instructions, MCP servers, and per-project setup |
| [`docs/`](docs/) | Documentation for operator, skills, and spec-kit |
| [`setup.sh`](setup.sh) | Linux/WSL/macOS setup: migrates any legacy bash install to Python, then delegates to `setup_tools.py` |
| [`setup.ps1`](setup.ps1) | Windows setup: locates Python 3.10+ and delegates to `setup_tools.py` |

## Platform Support

| Component | Windows | Linux / WSL | macOS |
|-----------|---------|-------------|-------|
| Workflow conventions & templates | ✅ | ✅ | ✅ |
| Spec Kit workflow | ✅ | ✅ | ✅ |
| Runtime extensions | ✅ | ✅ | ✅ |
| `operator` / `handoff` (Python) | ✅ | ✅ | ✅ |
| `operator.sh` / `handoff.sh` (bash, legacy, unmaintained) | ❌ | rollback only | rollback only |
| `operator restore` (Windows Terminal tabs) | ✅ (native tabs) | ✅ (WSL-hosted tabs only) | ❌ |

The Python implementation is the supported entry point on every platform.
`setup.sh` migrates existing Linux/WSL/macOS installs off the bash scripts
automatically (see [Quick Start](#quick-start)); the bash scripts themselves
are left on disk, untouched, purely so a failed migration can never strand a
user without a working `operator`/`handoff` command.

`operator restore` re-launches tracked Windows Terminal tabs. It works from
both native Windows PowerShell and from inside WSL (via `wt.exe`/`wsl.exe`
interop), but a restore invoked from WSL only sees tabs tracked by that WSL
distro and its siblings, not tabs tracked by a native Windows session, and
vice versa — the two sides don't share a tab registry.

Session management uses a terminal multiplexer: **psmux** on Windows, **tmux**
elsewhere. See [Operator](docs/operator.md) for details.

## Quick Start

**PowerShell (Windows)**

```powershell
winget install --id marlocarlo.psmux
git clone <this-repo> $HOME\repos\copilot-tools
cd $HOME\repos\copilot-tools
./setup.ps1
```

**bash (Linux/macOS/WSL)**

```bash
git clone <this-repo> ~/projects/copilot-tools
cd ~/projects/copilot-tools
chmod +x setup.sh
./setup.sh
```

Both scripts locate a Python 3.10+ interpreter and delegate to
`setup_tools.py`, which is itself cross-platform and idempotent. It will:

1. Check prerequisites (multiplexer, Python 3.10+, `copilot`, `git`)
2. Install the `operator`, `handoff` and `operator-ingest` console scripts
3. Link runtime extensions into `~/.copilot/extensions/`
4. Install configuration templates to `~/.copilot/`

You can also invoke `python setup_tools.py` / `python3 setup_tools.py`
directly if you don't need the extra steps below.

`sqlite3` is **not** required — the toolkit uses Python's standard-library
`sqlite3` module.

<details>
<summary>What <code>setup.sh</code> does beyond <code>setup_tools.py</code> (Linux/WSL/macOS)</summary>

If a previous run of the *original bash* installer left an `operator` and/or
`handoff` symlink in `~/.local/bin` pointing at this checkout's
`operator.sh`/`handoff.sh`, `setup.sh` sets those symlinks aside, runs the
Python install, and confirms `operator`/`handoff` resolve to the new
console scripts before deleting the old symlinks. If anything else occupies
those paths instead — a symlink pointing elsewhere, a different checkout, or
an unrelated command with the same name — it's moved aside the same way but
**never auto-deleted**, since `pip install -e .` would otherwise silently
overwrite it with no backup of its own; look for a
`~/.local/bin/{operator,handoff}.copilot-tools-preexisting-bak` file if that
happens to you. If the Python install fails, the new commands don't resolve
on `PATH`, or setup is interrupted (Ctrl-C) mid-install, everything is
restored automatically and `setup.sh` exits non-zero — you're never left
without a working command.

`setup.sh` then additionally installs the Anvil plugin, `dotnet-roslyn-mcp`,
and the Spec Kit CLI (`specify`) via `uv` — none of which `setup_tools.py`
manages. `operator.sh`/`handoff.sh` themselves are left on disk unchanged;
they're just no longer the thing installed into `PATH`.
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
