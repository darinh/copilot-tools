---
name: code-intelligence
description: Use for C# structural questions such as callers, references, implementations, dependencies, and change blast radius. For other languages, use the environment's built-in code intelligence, LSP, or search tools.
allowed-tools: mcp__roslyn__find_references, mcp__roslyn__find_implementations, mcp__roslyn__find_callers, mcp__roslyn__get_symbol_info, mcp__roslyn__search_symbols
---

# Code Intelligence Tools

Use Roslyn for C# structural questions. For other languages, use the built-in
code intelligence, LSP, or search tools available in your environment.

**Never use grep/glob/file reads for structural questions** when Roslyn or the
built-in tools can answer it faster.

## Decision Rules

**Use `roslyn` (dotnet-roslyn-mcp) when:**
- The question involves C# code
- You need to find callers or callees of a C# method
- You need to find all implementations of a C# interface
- You need to find all references to a C# symbol
- You need accurate cross-file type resolution in C#
- You're assessing blast radius of a C# change

**For non-C# languages:**
- Use the built-in code intelligence, LSP, or search tools in your environment
- Fall back to `rg`, `glob`, and `view` for targeted inspection when needed

## Common Workflows

**Before planning a Medium/Large C# task:**
1. `roslyn:find_references` on the C# symbol(s) you plan to change — blast radius
2. `roslyn:find_implementations` if changing a C# interface
3. Include results in the plan before implementing

**"What calls X?" (C#)**
→ `roslyn:find_callers` or `roslyn:find_references`

**"Where is X implemented?" (C# interface)**
→ `roslyn:find_implementations`

**"Find all classes/functions matching a pattern"**
→ `roslyn:search_symbols` (C# backend)
→ use your environment's built-in code intelligence or search tools for other
  languages

**"What's the overall architecture?"**
→ use your environment's built-in code intelligence or search tools

## Setup

1. Install `dotnet-roslyn-mcp` (see `setup.sh` in the copilot-tools repo)
2. Copy this skill into your project's `.github/skills/` directory:
   ```
   cp -r copilot-tools/skills/code-intelligence your-project/.github/skills/
   ```
3. Ensure your `~/.copilot/mcp-config.json` has the Roslyn server entry
   (see `templates/mcp-config.json`)
