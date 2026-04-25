// open-in-vs-code — opens edited/created files in VS Code.
//
// Behavior:
//   • Each file is opened at most once per session (dedup by absolute path).
//   • Skips noise: lockfiles, generated files, binaries, node_modules,
//     dist/build/coverage output, generated agent files.
//   • Set COPILOT_AUTO_OPEN_DISABLE=1 to disable entirely.
//   • Uses execFile (no shell), so paths with quotes/spaces are safe.

import { execFile } from "node:child_process";
import { resolve } from "node:path";
import { realpathSync } from "node:fs";
import { approveAll } from "@github/copilot-sdk";
import { joinSession } from "@github/copilot-sdk/extension";

const DISABLE = process.env.COPILOT_AUTO_OPEN_DISABLE === "1";

const SKIP_PATTERNS = [
  /[\\/]node_modules[\\/]/,
  /[\\/]dist[\\/]/,
  /[\\/]build[\\/]/,
  /[\\/]\.git[\\/]/,
  /[\\/](coverage|coverage-results|TestResults)[\\/]/,
  /:Zone\.Identifier$/,
  /[\\/](package-lock\.json|pnpm-lock\.yaml|yarn\.lock|Cargo\.lock|go\.sum|poetry\.lock|composer\.lock)$/,
  // Generated agent files in this team's projects regenerate frequently.
  /[\\/]agents[\\/][^\\/]+\.agent\.md$/,
  // Binary/asset extensions:
  /\.(png|jpg|jpeg|gif|webp|ico|svg|pdf|zip|tar|gz|tgz|bin|exe|dll|so|dylib|class|jar|wasm|woff2?|ttf|otf|mp3|mp4|mov|webm)$/i,
];

const opened = new Set();

function shouldSkip(absPath) {
  return SKIP_PATTERNS.some((p) => p.test(absPath));
}

function canonicalize(absPath) {
  // Resolve symlinks so a file edited via two paths (e.g. the extensions
  // dir and its symlink target in copilot-tools) dedups correctly. Falls
  // back to the resolved-but-unrealpathed absPath if the file doesn't exist
  // yet (create case).
  try {
    return realpathSync.native(absPath);
  } catch {
    return absPath;
  }
}

function openInEditor(absPath) {
  // execFile + `--` so leading-dash filenames aren't parsed as flags.
  execFile("code", ["--", absPath], () => {
    // Silent. If `code` is missing, there's nothing useful to log per file.
  });
}

const session = await joinSession({
  onPermissionRequest: approveAll,
  hooks: {
    onPostToolUse: async (input) => {
      if (DISABLE) return;
      if (input.toolName !== "create" && input.toolName !== "edit") return;
      const raw = input.toolArgs?.path;
      if (!raw) return;
      const absPath = resolve(String(raw));
      const key = canonicalize(absPath);
      if (opened.has(key)) return;
      if (shouldSkip(absPath)) return;
      opened.add(key);
      openInEditor(absPath);
    },
  },
  tools: [],
});

await session.log(
  DISABLE
    ? "[open-in-vs-code] disabled via COPILOT_AUTO_OPEN_DISABLE=1"
    : "[open-in-vs-code] ready — first edit/create per file opens in VS Code"
);
