# copilot-tools

Tools, skills, and workflow conventions for GitHub Copilot CLI power users —
autonomous loop mode, multi-instance session management, agent-to-agent
messaging, and spec-driven workflow conventions.

> An independent personal project. Not affiliated with, endorsed by, or
> supported by GitHub or Microsoft.

## What's Inside

| Component | Description |
|-----------|-------------|
| [`copilot_operator.py`](docs/operator.md) | Cross-platform Copilot CLI wrapper with metrics capture, autonomous loop mode, and multi-instance support |
| [`operator_runner.py`](docs/operator.md#architecture) | In-pane session supervisor: correct process attribution and metrics after detach |
| [`operator_mux.py`](docs/operator.md#platform-support) | Session-backend abstraction (tmux / psmux) |
| [`operator_ingest.py`](operator_ingest.py) | Pure-Python log parser for copilot process logs |
| [`handoff_tool.py`](docs/operator.md) | Atomic session handoff for agents |
| [`operator.sh`](operator.sh), [`handoff.sh`](handoff.sh), [`operator-ingest.py`](operator-ingest.py) | Original bash implementation, retained on disk for rollback but no longer installed fresh by `setup.sh` |
| [`skills/code-intelligence`](skills/code-intelligence/SKILL.md) | Roslyn-backed C# structural analysis |
| [`skills/operator-agents`](skills/operator-agents/SKILL.md) | Starting parallel operator agents and messaging them |
| [`operator_mail.py`](docs/operator.md#parallel-agents-and-messaging) | Message store behind `operator send` / `operator inbox` |
| [`install_manifest.py`](docs/versioning.md) | Records what setup deployed and its hash, so upgrades know what's safe to replace |
| [`extensions/`](extensions/README.md) | Copilot CLI runtime extensions: open-in-vs-code, lint-on-edit, security-shield, test-enforcer, architecture-enforcer, checkout-guard, copy-to-clipboard-tool |
| [`templates/`](templates/) | Configuration templates for copilot-instructions, MCP servers, and per-project setup |
| [`docs/`](docs/) | Documentation for operator, skills, versioning, and spec-kit |
| [`setup.sh`](setup.sh) | Linux/WSL/macOS setup: migrates any legacy bash install to Python, then delegates to `setup_tools.py` |
| [`setup.ps1`](setup.ps1) | Windows setup: locates or installs Python 3.10+, then delegates to `setup_tools.py` |

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

Setup **installs** what's missing rather than telling you to go install it.
Both scripts locate a Python 3.10+ interpreter — installing one via `winget`
or your distro's package manager if the machine has none — and delegate to
`setup_tools.py`, which is itself cross-platform and idempotent. It will:

1. Install any missing prerequisites: a terminal multiplexer (psmux via
   `winget`, or its GitHub release as a fallback / tmux via `apt-get`, `dnf`,
   `pacman`, `zypper`, `apk`, or `brew`), `git`, and the `copilot` CLI (via
   `npm`, pulling in Node.js first if needed)
2. Install the `operator`, `handoff` and `operator-ingest` console scripts
3. Link runtime extensions into `~/.copilot/extensions/`
4. Install configuration templates to `~/.copilot/`
5. Install the Anvil plugin, the Spec Kit CLI (`specify`, via `uv`), and
   optionally `dotnet-roslyn-mcp`

Anything installed to a directory that isn't on `PATH` yet (npm's global bin,
`~/.local/bin`, the psmux download) is added to your user `PATH`, so new
shells pick it up.

Only a genuinely unautomatable failure — no package manager at all, or an
install that errors — stops setup, and it prints the exact manual command.

Useful flags: `--yes` (assume yes to overwrite prompts), `--status` (report
what's installed and whether an update is needed, changing nothing),
`--check-only` (report missing prerequisites and change nothing),
`--no-install-prereqs` (old check-and-bail behavior), `--skip-optional` (skip
Anvil, spec-kit, and the MCP servers), `--skip-package` (skip
`pip install -e .`).

Setup records what it deployed in `~/.operator/install-manifest.json`, so a
later run can tell "the repository moved on and you never touched your copy"
(update silently) from "you customised this" (ask first). After pulling on
another machine, `python setup_tools.py --status` answers whether you need to
re-run setup. See [Versioning](docs/versioning.md).

You can also invoke `python setup_tools.py` / `python3 setup_tools.py`
directly if you don't need the migration step below.

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

That migration is all `setup.sh` still does on its own. Anvil,
`dotnet-roslyn-mcp`, and the Spec Kit CLI used to be installed here, which
meant Windows users never got them; they're now provisioned by
`setup_tools.py` on every platform. `operator.sh`/`handoff.sh` themselves are
left on disk unchanged; they're just no longer the thing installed into
`PATH`.
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

# Start a loop without attaching, then message it
operator --loop --headless --name payments-api --agent=anvil:anvil
operator send --from backend --to payments-api "POST /charges returns {id, status}"
operator inbox backend

# Usage reports
operator report costs
```

Named loop instances persist the active Copilot CLI session ID, so restarting the same loop after a WSL crash or Windows reboot automatically resumes the prior CLI session.

On Windows, `operator` also tracks which Windows Terminal tabs are running named instances. After a reboot or crash, `operator restore` lists tracked tabs so you can pick which to reopen (or `operator restore --all` for everything), replaying each launch command in a fresh tab so every Copilot session resumes where it left off — no manual re-`cd`-ing or hunting for session IDs.

See [Operator Documentation](docs/operator.md) for full details.

## Skills & Plugins

| Skill | Type | Source |
|-------|------|--------|
| **code-intelligence** | User skill (installed to `~/.copilot/skills/`) | Included in this repo |
| **operator-agents** | User skill (installed to `~/.copilot/skills/`) | Included in this repo |
| **Anvil** | Installable plugin | [`burkeholland/anvil`](https://github.com/burkeholland/anvil) |
| **frontend-design** | Built-in CLI skill | Ships with Copilot CLI |
| **find-skills** | Built-in CLI skill | Ships with Copilot CLI |

See [Skills Reference](docs/skills.md) for details and setup.

## Workflow Conventions

The `templates/copilot-instructions.md` file establishes conventions for:
- **Project Configuration System** — per-project settings stored in `~/.copilot/projects/`
- **Session Handoff** — cross-session continuity via `next-session.md` files; an
  unread handoff is archived to `superseded/` rather than overwritten, and that
  archive is [never pruned](docs/operator.md#superseded-handoffs)
- **Session History** — SQL-based audit trail of work across sessions
- **Spec-Driven Development** — specs as the single source of truth (enabled by default)
- **Parallel Agents** — SQL-coordinated parallel task execution via `todo_claims`
- **Operator Agents** — parallel *peer* agents in their own terminals, talking via `operator send` / `operator inbox`
- **Branching Strategy** — feature branches worked on in worktrees, merged to `main`, conventional commits
- **Git Worktrees** — all work happens in `<repoRoot>/.worktrees/`; always on, not optional

Copy to `~/.copilot/copilot-instructions.md` and customize for your workflow.

## MCP Servers

An optional MCP server provides structural code intelligence:

| Server | Language | Install |
|--------|----------|---------|
| **dotnet-roslyn-mcp** | C# | `dotnet tool install -g dotnet-roslyn-mcp` |

Configure in `~/.copilot/mcp-config.json` — see [`templates/mcp-config.json`](templates/mcp-config.json).

## Repository Structure

Every entry at the repository root appears below;
[`tests/test_readme_structure.py`](tests/test_readme_structure.py) fails if this
tree and `git ls-files` ever disagree.

```
copilot-tools/
├── .github/
│   ├── copilot-instructions.md    # Conventions for agents working in this repo
│   ├── skills/speckit-*/          # Copilot skills generated by Spec Kit
│   └── workflows/ci.yml           # 8 jobs: 3 OSes x 2 Pythons, shell syntax, extensions
├── .specify/                      # Spec Kit templates, scripts, and constitution
├── specs/                         # Feature specifications, plans, and tasks
├── .gitignore                     # Caches, packaging output, and /.worktrees/
├── LICENSE                        # MIT
├── README.md                      # This file
├── pyproject.toml                 # Packaging and console scripts
├── copilot_operator.py            # Operator CLI (all platforms)
├── operator_runner.py             # In-pane session supervisor
├── operator_mux.py                # Session-backend abstraction (tmux / psmux)
├── operator_ingest.py             # Pure-Python log parser
├── operator_mail.py               # Agent-to-agent mail, live and queued delivery
├── operator_console.py            # UTF-8 console output
├── project_paths.py               # Project identity: catalog and per-project dirs
├── handoff_tool.py                # Session handoff
├── setup_tools.py                 # Cross-platform environment setup
├── install_manifest.py            # Records what setup deployed; upgrade strategies
├── copilot_tools_version.py       # The single source of the version number
├── backfill_unknown_metrics.py    # One-off repair: fabricated zeros to NULL
├── verify_cross_platform.py       # Stdlib-only verification; runs without pytest
├── e2e_restart_loop.py            # End-to-end restart-loop check, real processes
├── setup.sh                       # POSIX bootstrap; finds Python, runs setup_tools
├── setup.ps1                      # Windows bootstrap; same, winget if no Python
├── operator.sh                    # Legacy bash wrapper (Linux/WSL)
├── operator-ingest.py             # Legacy log parser, used by operator.sh
├── handoff.sh                     # Legacy bash handoff (Linux/WSL)
├── diagnose-restart-deleter.sh    # Diagnostic: who deleted the restart directory
├── extensions/                    # Copilot CLI runtime extensions; see its README
├── skills/
│   ├── code-intelligence/
│   │   └── SKILL.md               # Roslyn routing
│   └── operator-agents/
│       └── SKILL.md               # Parallel operator agents and mail
├── templates/
│   ├── copilot-instructions.md    # Workflow conventions
│   └── mcp-config.json            # MCP server config
├── tests/                         # pytest suite + bash coordination tests
└── docs/
    ├── operator.md                # Operator documentation
    ├── skills.md                  # Skills reference
    ├── versioning.md              # Install manifest and upgrade strategies
    ├── checkout-guard.md          # Stray-artifact guard, and how to tell it is running
    └── spec-kit.md                # GitHub spec-kit documentation
```

## Versioning

The version lives in one place, `copilot_tools_version.py`, and `pyproject.toml`
reads it dynamically. Setup records what it deployed — path, version, and
SHA-256 — in `~/.operator/install-manifest.json`, which is what lets a later run
update a file you never touched without prompting while still asking about one
you customised.

```bash
python setup_tools.py --status   # what's installed, and is it stale?
```

See [Versioning](docs/versioning.md) for the artifact states, the hashing
rationale, and how to write a version-to-version upgrade function.

## License

MIT — see [LICENSE](LICENSE).

This is an independent personal project. It is not affiliated with, endorsed
by, or supported by GitHub or Microsoft, and it is not an official distribution
of the GitHub Copilot CLI. "GitHub", "GitHub Copilot", and "Microsoft" are
trademarks of their respective owners and are used here only to describe what
this toolkit interoperates with.

