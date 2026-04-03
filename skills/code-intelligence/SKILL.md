---
name: code-intelligence
description: Use when exploring codebase structure, finding callers/callees, understanding relationships between files or classes, checking what code depends on what, or assessing the blast radius of a change. Prefer this over grep, glob, or file reads for structural questions. Use before planning or implementing any Medium or Large task.
allowed-tools: mcp__codebase-memory-mcp__search_graph, mcp__codebase-memory-mcp__trace_call_path, mcp__codebase-memory-mcp__get_architecture, mcp__codebase-memory-mcp__detect_changes, mcp__codebase-memory-mcp__query_graph, mcp__roslyn__find_references, mcp__roslyn__find_implementations, mcp__roslyn__find_callers, mcp__roslyn__get_symbol_info, mcp__roslyn__search_symbols
---

# Code Intelligence Tools

Two MCP servers provide structural code intelligence. Each has a specific
strength — use the right one for the job.

**Key constraint**: `codebase-memory-mcp` uses tree-sitter for parsing. Tree-sitter
handles TypeScript cross-file resolution well, but **cannot reliably resolve C#
cross-file callers/callees**. For any C# structural question, always use Roslyn.

**Never use grep/glob/file reads for structural questions** when either tool can
answer it — graph and Roslyn queries return precise results in a single tool call
(~500 tokens) vs file-by-file exploration (~80K tokens).

## Decision Rules

**Use `roslyn` (dotnet-roslyn-mcp) when:**
- The question involves C# code
- You need to find callers or callees of a C# method
- You need to find all implementations of a C# interface
- You need to find all references to a C# symbol
- You need accurate cross-file type resolution in C#
- You're assessing blast radius of a C# change

**Use `codebase-memory-mcp` when:**
- You need TypeScript/JavaScript/frontend structural queries — callers,
  callees, and cross-file resolution work correctly via tree-sitter
- You need git coupling history (`FILE_CHANGES_WITH` edges — files that change together)
- You need to map a git diff to affected symbols (`detect_changes`)
- You need file/folder structure overview (`get_architecture`)
- You want to find files/symbols by name pattern across the whole repo (`search_graph`)
- You want grep results ranked by structural importance (`search_code`)
- You need a custom graph traversal (`query_graph` with Cypher)

**Never use `codebase-memory-mcp` for:**
- C# caller/callee resolution — use Roslyn instead
- C# cross-file type relationships — use Roslyn instead

## Common Workflows

**Before planning a Medium/Large task:**
1. `detect_changes` (codebase-memory-mcp) — what's already in flight in the git diff
2. `roslyn:find_references` on the C# symbol(s) you plan to change — blast radius
3. `roslyn:find_implementations` if changing a C# interface
4. Include results in the plan before implementing

**"What calls X?" (C#)**
→ `roslyn:find_callers` or `roslyn:find_references`

**"What calls X?" (TypeScript)**
→ `codebase-memory-mcp: trace_call_path`

**"What does this git diff affect?"**
→ `codebase-memory-mcp: detect_changes`

**"Where is X implemented?" (C# interface)**
→ `roslyn:find_implementations`

**"Which files tend to change together?"**
→ `codebase-memory-mcp: query_graph` with `FILE_CHANGES_WITH` edges

**"Find all classes/functions matching a pattern"**
→ `roslyn:search_symbols` (C# backend)
→ `codebase-memory-mcp: search_graph` (frontend or cross-repo)

**"What's the overall architecture?"**
→ `codebase-memory-mcp: get_architecture`

## Setup

1. Install both MCP servers (see `setup.sh` in the copilot-tools repo)
2. Copy this skill into your project:
   ```
   cp -r copilot-tools/skills/code-intelligence your-project/.github/skills/
   ```
3. Ensure your `~/.copilot/mcp-config.json` has entries for both servers
   (see `templates/mcp-config.json`)
