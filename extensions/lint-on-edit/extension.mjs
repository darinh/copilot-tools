// lint-on-edit — runs the project linter on each edited file.
//
// • Distinguishes "linter not installed" (silent skip) from "linter found
//   issues" (returned as additionalContext to the agent).
// • 15s per-file timeout to bound batch-edit slowdown.
// • Currently supports: ESLint (TS/JS), ruff (Python). dotnet format on a
//   single file is too slow to run inline.

import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { approveAll } from "@github/copilot-sdk";
import { joinSession } from "@github/copilot-sdk/extension";

const isWindows = process.platform === "win32";

function findProjectRoot(startPath) {
  let dir = dirname(startPath);
  for (let i = 0; i < 12; i++) {
    if (
      existsSync(resolve(dir, ".git")) ||
      existsSync(resolve(dir, "package.json")) ||
      existsSync(resolve(dir, "pyproject.toml")) ||
      existsSync(resolve(dir, "Cargo.toml")) ||
      existsSync(resolve(dir, "go.mod"))
    ) {
      return dir;
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return process.cwd();
}

function detectLinter(filePath, projectRoot) {
  const ext = filePath.match(/\.([^.]+)$/)?.[1]?.toLowerCase();
  if (!ext) return null;

  if (["ts", "tsx", "js", "jsx", "mjs", "cjs"].includes(ext)) {
    const hasESLint =
      existsSync(resolve(projectRoot, "eslint.config.mjs")) ||
      existsSync(resolve(projectRoot, "eslint.config.js")) ||
      existsSync(resolve(projectRoot, "eslint.config.cjs")) ||
      existsSync(resolve(projectRoot, ".eslintrc.json")) ||
      existsSync(resolve(projectRoot, ".eslintrc.js")) ||
      existsSync(resolve(projectRoot, ".eslintrc.cjs"));
    if (hasESLint) {
      const npx = isWindows ? "npx.cmd" : "npx";
      return { cmd: npx, args: ["--no-install", "eslint", "--no-error-on-unmatched-pattern", "--", filePath] };
    }
  }
  if (ext === "py" && existsSync(resolve(projectRoot, "pyproject.toml"))) {
    return { cmd: "ruff", args: ["check", "--", filePath] };
  }
  return null;
}

function runLinter(cmd, args, cwd) {
  return new Promise((done) => {
    execFile(cmd, args, { cwd, timeout: 15000, maxBuffer: 4 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (!err) return done({ kind: "clean" });
      if (err.code === "ENOENT") return done({ kind: "missing" });
      if (err.killed) return done({ kind: "timeout" });
      // npx --no-install can't find the package → treat as "missing", not lint issues.
      // It writes errors like "could not determine executable to run" or
      // "npm error could not determine executable" to stderr.
      const combined = (stderr || "") + (stdout || "");
      if (
        /could not determine executable to run/i.test(combined) ||
        /npm (?:err|error)[\s\S]*could not (?:find|resolve|determine)/i.test(combined) ||
        /(^|\s)eslint: not found/i.test(combined)
      ) {
        return done({ kind: "missing" });
      }
      done({ kind: "issues", output: (stdout || stderr || err.message || "").slice(0, 4000) });
    });
  });
}

const session = await joinSession({
  onPermissionRequest: approveAll,
  hooks: {
    onPostToolUse: async (input) => {
      if (input.toolName !== "edit" && input.toolName !== "create") return;
      const filePath = String(input.toolArgs?.path || "");
      if (!filePath) return;
      const projectRoot = findProjectRoot(filePath);
      const linter = detectLinter(filePath, projectRoot);
      if (!linter) return;

      const result = await runLinter(linter.cmd, linter.args, projectRoot);
      if (result.kind === "issues") {
        return {
          additionalContext:
            `[lint-on-edit] Issues in ${filePath}:\n${result.output}\nFix these before proceeding.`,
        };
      }
    },
  },
  tools: [],
});
