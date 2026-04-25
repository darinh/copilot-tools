// architecture-enforcer — surfaces import-boundary violations.
//
// Loads rules from `.copilot-architecture.json` at the project root.
// If the file is absent or malformed, the extension silently does nothing
// (so it's safe to install globally without cross-talk between projects).
//
// Config schema:
//   {
//     "rules": [
//       { "from": "<regex>", "cannotImport": "<regex>", "reason": "..." }
//     ]
//   }
//
// `from` matches the file path relative to the project root.
// `cannotImport` matches the import target string.
//
// Supported languages for import extraction:
//   • TS/JS    — `import ... from "..."` and `require("...")`
//   • C#       — `using X.Y.Z;`
//   • Python   — `from X import Y`, `import X`

import { existsSync, readFileSync, statSync } from "node:fs";
import { resolve, dirname, relative, sep } from "node:path";
import { approveAll } from "@github/copilot-sdk";
import { joinSession } from "@github/copilot-sdk/extension";

function findProjectRoot(startPath) {
  let dir = dirname(startPath);
  for (let i = 0; i < 12; i++) {
    if (existsSync(resolve(dir, ".git"))) return dir;
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return process.cwd();
}

const cache = new Map();

function loadRules(projectRoot) {
  const cfgPath = resolve(projectRoot, ".copilot-architecture.json");
  let mtime = 0;
  try {
    mtime = statSync(cfgPath).mtimeMs;
  } catch {
    // file missing — fall through to empty rules; cache by mtime=0.
  }
  const cached = cache.get(projectRoot);
  if (cached && cached.mtime === mtime) return cached.rules;
  let rules = [];
  if (mtime > 0) {
    try {
      const cfg = JSON.parse(readFileSync(cfgPath, "utf8"));
      rules = (cfg.rules || []).map((r) => ({
        from: new RegExp(r.from),
        cannotImport: new RegExp(r.cannotImport),
        reason: r.reason || `Disallowed import (${r.from} → ${r.cannotImport})`,
      }));
    } catch {
      rules = [];
    }
  }
  cache.set(projectRoot, { mtime, rules });
  return rules;
}

function extractImports(filePath, content) {
  const ext = filePath.match(/\.([^.]+)$/)?.[1]?.toLowerCase();
  const imports = [];
  if (["ts", "tsx", "js", "jsx", "mjs", "cjs"].includes(ext)) {
    // Match: `import "x"`, `import x from "x"`, `import {x} from "x"`,
    // `import * as x from "x"`, `import x, {y} from "x"`, dynamic `import("x")`.
    const importFrom = /\bimport\b\s+(?:[^'"`;()]*?\bfrom\s+)?['"]([^'"]+)['"]/g;
    const requireCall = /\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)/g;
    const dynamicImport = /\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)/g;
    for (const re of [importFrom, requireCall, dynamicImport]) {
      let m;
      while ((m = re.exec(content))) imports.push(m[1]);
    }
  } else if (ext === "cs") {
    const re = /^\s*using\s+(?:static\s+)?([A-Za-z0-9_.]+)\s*;/gm;
    let m;
    while ((m = re.exec(content))) imports.push(m[1]);
  } else if (ext === "py") {
    const re = /^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))/gm;
    let m;
    while ((m = re.exec(content))) imports.push(m[1] || m[2]);
  }
  return imports;
}

const session = await joinSession({
  onPermissionRequest: approveAll,
  hooks: {
    onPostToolUse: async (input) => {
      if (input.toolName !== "edit" && input.toolName !== "create") return;
      const filePath = String(input.toolArgs?.path || "");
      if (!filePath) return;
      const content = String(input.toolArgs?.new_str || input.toolArgs?.file_text || "");
      if (!content) return;

      const projectRoot = findProjectRoot(filePath);
      const rules = loadRules(projectRoot);
      if (rules.length === 0) return;

      const absPath = resolve(filePath);
      const rel = relative(projectRoot, absPath).replace(/\\/g, "/");
      // path.relative returns "..foo" if absPath is outside projectRoot — skip.
      if (rel.startsWith("..") || resolve(projectRoot, rel) !== absPath) return;
      const relPath = rel;

      const imports = extractImports(filePath, content);
      const violations = [];
      for (const rule of rules) {
        if (!rule.from.test(relPath)) continue;
        for (const imp of imports) {
          if (rule.cannotImport.test(imp)) {
            violations.push(`${rule.reason} (import: ${imp})`);
          }
        }
      }

      if (violations.length > 0) {
        return {
          additionalContext:
            `[arch-enforcer] Architecture violations in ${relPath}:\n` +
            violations.map((v) => `  ⚠ ${v}`).join("\n") +
            `\nFix these before proceeding.`,
        };
      }
    },
  },
  tools: [],
});
