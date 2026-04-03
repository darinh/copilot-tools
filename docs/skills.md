# Skills Reference

Skills extend Copilot CLI agents with specialized capabilities. They're markdown files that get loaded into the agent's context when invoked.

## Included in this repo

### code-intelligence

**Location**: `skills/code-intelligence/SKILL.md`
**Install**: Copy into your project's `.github/skills/` directory

Routes structural code questions to the right MCP server:
- **Roslyn** (dotnet-roslyn-mcp) for C# — callers, references, implementations, symbol info
- **codebase-memory-mcp** for TypeScript/JS, git coupling, architecture overview, change detection

Key rule: never use `codebase-memory-mcp` for C# cross-file resolution — tree-sitter can't handle it. Always use Roslyn for C#.

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
**Install**: `copilot install burkeholland/anvil`

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

### codebase-memory-mcp

Knowledge graph built from your codebase via tree-sitter parsing. Provides:
- `search_graph` — find symbols by name/pattern
- `trace_call_path` — callers and callees (TypeScript/JS)
- `get_architecture` — project structure overview
- `detect_changes` — map git diffs to affected symbols
- `query_graph` — custom Cypher queries

**Install**: Get the Go binary from your team's distribution, place in `~/.local/bin/`

### dotnet-roslyn-mcp

C# code intelligence powered by the Roslyn compiler. Provides:
- `find_references` — all references to a symbol
- `find_implementations` — interface implementations
- `find_callers` — who calls a method
- `get_symbol_info` — type info, documentation
- `search_symbols` — find symbols by name

**Install**: `dotnet tool install -g dotnet-roslyn-mcp`

### Configuration

Both servers are configured in `~/.copilot/mcp-config.json`. See `templates/mcp-config.json` for the template.
