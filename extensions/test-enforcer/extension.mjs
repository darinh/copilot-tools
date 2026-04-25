// test-enforcer — blocks `git commit` if source files were modified
// without corresponding test changes in the same session.
//
// • Inspects BOTH `bash` and `powershell` (original only checked
//   powershell, which was a no-op on Linux/macOS).
// • Tracking sets are bounded to avoid unbounded growth.
// • Set COPILOT_TEST_ENFORCER_BYPASS=1 to skip the check (use sparingly).

import { approveAll } from "@github/copilot-sdk";
import { joinSession } from "@github/copilot-sdk/extension";

const SHELL_TOOLS = new Set(["bash", "powershell", "shell"]);

const TEST_PATTERNS = [
  /\.test\.[jt]sx?$/, /\.spec\.[jt]sx?$/,
  /(^|[\\/])test_[^\\/]+\.py$/, /_test\.py$/,
  /_test\.go$/, /Tests?\.cs$/,
];
const SOURCE_EXTENSIONS = /\.(ts|tsx|js|jsx|mjs|cjs|py|go|cs|java|rb)$/;
const IGNORE_PATTERNS = [
  /[\\/]node_modules[\\/]/, /[\\/]\.git[\\/]/,
  /[\\/](dist|build|out|coverage)[\\/]/,
  /\.config\.[jt]s$/, /\.d\.ts$/, /\.generated\./,
];

const isTestFile = (p) => TEST_PATTERNS.some((r) => r.test(p));
const isSourceFile = (p) =>
  SOURCE_EXTENSIONS.test(p) && !isTestFile(p) && !IGNORE_PATTERNS.some((r) => r.test(p));

const MAX_TRACKED = 200;
const modifiedSourceFiles = new Set();
const modifiedTestFiles = new Set();

function bound(set) {
  if (set.size <= MAX_TRACKED) return;
  const it = set.values();
  while (set.size > MAX_TRACKED) set.delete(it.next().value);
}

function baseName(p) {
  const idx = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
  return p.slice(idx + 1).replace(/\.[^.]+$/, "");
}

// Normalise a test basename to its underlying "subject" by stripping the
// suffixes/prefixes test conventions add. Used to require an *exact* subject
// match instead of substring overlap, which is trivially fooled (e.g.
// "capital.test" satisfying "api"). Also strips a final extension if one
// remains after the convention strip (e.g. "auth.test" → "auth").
function testSubject(testBase) {
  let s = testBase;
  s = s.replace(/\.(test|spec)$/i, "");
  s = s.replace(/^test_/i, "");
  s = s.replace(/_test$/i, "");
  s = s.replace(/Tests?$/, "");
  return s;
}

const session = await joinSession({
  onPermissionRequest: approveAll,
  hooks: {
    onPostToolUse: async (input) => {
      if (input.toolName !== "edit" && input.toolName !== "create") return;
      const filePath = String(input.toolArgs?.path || "");
      if (!filePath) return;
      if (isTestFile(filePath)) {
        modifiedTestFiles.add(filePath);
        bound(modifiedTestFiles);
      } else if (isSourceFile(filePath)) {
        modifiedSourceFiles.add(filePath);
        bound(modifiedSourceFiles);
        return {
          additionalContext:
            `[test-enforcer] Source file modified: ${filePath}. ` +
            `Write or update tests before committing.`,
        };
      }
    },
    onPreToolUse: async (input) => {
      if (!SHELL_TOOLS.has(input.toolName)) return;
      const cmd = String(input.toolArgs?.command || "");
      // Match `git ... commit` allowing global options like
      // `git -c user.name=x` (option + positional value), `git -C path`,
      // `git --git-dir=...`, etc. We do this by allowing any non-`commit`
      // token between `git` and `commit`. False positives (e.g. random text
      // before `commit`) are acceptable for a pre-commit gate.
      if (!/\bgit\b(?:\s+(?!commit\b)\S+)*\s+commit(?:\s|$)/.test(cmd)) return;

      if (process.env.COPILOT_TEST_ENFORCER_BYPASS === "1") return;

      const testSubjects = new Set([...modifiedTestFiles].map((p) => testSubject(baseName(p))));
      const untestedFiles = [...modifiedSourceFiles].filter((src) => {
        const srcBase = baseName(src);
        return !testSubjects.has(srcBase);
      });

      if (untestedFiles.length > 0) {
        return {
          permissionDecision: "deny",
          permissionDecisionReason:
            `[test-enforcer] BLOCKED: Source files modified without tests:\n` +
            untestedFiles.map((f) => `  - ${f}`).join("\n") +
            `\n\nWrite or update tests for these files first, ` +
            `or set COPILOT_TEST_ENFORCER_BYPASS=1 to override.`,
        };
      }
    },
  },
  tools: [],
});
