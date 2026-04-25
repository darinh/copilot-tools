// security-shield — blocks obvious destructive commands and accidental
// secret commits. Best-effort defense, not a substitute for real review.
//
// Inspects BOTH `bash` and `powershell` (the original only inspected
// `powershell`, which made it a no-op on Linux/macOS dev boxes).

import { approveAll } from "@github/copilot-sdk";
import { joinSession } from "@github/copilot-sdk/extension";

const SHELL_TOOLS = new Set(["bash", "powershell", "shell"]);

// Boundary class includes \ (for `\rm` alias-bypass) and / (for `/bin/rm`).
const RM_PREFIX = String.raw`(?:^|[\s;&|\x60(\\/])(?:command\s+|exec\s+|/[\w/.\-]*?/)?\\?rm\b`;
// rm flags: r/f can appear together (-rf, -fr, -Rf), separately (-r -f),
// or as long-form (--recursive, --force).
const RM_FORCE_RECURSIVE = String.raw`(?=[\s\S]*?(?:-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|-R\b[\s\S]{0,40}-f\b|-r\b[\s\S]{0,40}-f\b|-f\b[\s\S]{0,40}-r\b|--recursive\b[\s\S]{0,40}--force\b|--force\b[\s\S]{0,40}--recursive\b))`;
// Targets: /, /*, ~, ~/*, $HOME, ${HOME}, "/", '/' — possibly preceded by `--` or `--no-preserve-root`.
const RM_TARGET = String.raw`(?:[\s\S]{0,80}?(?:["']?(?:/|~|\$\{?HOME\}?)["']?(?:/?\*+)?(?:\s|$|;|&|\|)))`;

const DANGEROUS_COMMANDS = [
  { pattern: new RegExp(RM_PREFIX + RM_FORCE_RECURSIVE + RM_TARGET, "i"), reason: "Recursive force-delete of root/home" },
  { pattern: /Remove-Item\s+[A-Z]:\\\s+.*-Recurse/i, reason: "Recursive delete of Windows drive root" },
  { pattern: /\bDROP\s+(DATABASE|TABLE|SCHEMA)\b/i, reason: "Destructive database operation" },
  // Force push: explicit flag form.
  { pattern: /git\s+push\s+(?:[^;\n]*\s)?(?:--force(?:-with-lease)?|-f)\b[^;\n]*\b(main|master|production|develop|release)\b/i, reason: "Force push to protected branch" },
  { pattern: /git\s+push\b[^;\n]*\b(main|master|production|develop|release)\b[^;\n]*\s(?:--force(?:-with-lease)?|-f)\b/i, reason: "Force push to protected branch" },
  // Force push: refspec form (git push origin +main, +HEAD:main).
  { pattern: /git\s+push\s+\S+\s+[^;\n]*\+(?:[\w./\-]+:)?(?:refs\/heads\/)?(main|master|production|develop|release)\b/i, reason: "Force push to protected branch (refspec form)" },
  { pattern: /\bmkfs\.[a-z0-9]+\b/i, reason: "Filesystem format command" },
  // Block-device write: any argv order, broader device class (sda, hda, nvme, mmcblk, vda, xvda).
  { pattern: /\bdd\b[^;\n]*\bof=\/dev\/(?:sd[a-z]\d*|hd[a-z]\d*|nvme\d+n\d+(?:p\d+)?|mmcblk\d+(?:p\d+)?|vd[a-z]\d*|xvd[a-z]\d*)\b/i, reason: "Raw write to block device" },
  { pattern: /:\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:/, reason: "Fork bomb" },
];

const SECRET_PATTERNS = [
  { pattern: /(?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}/g, type: "AWS Access Key" },
  { pattern: /ghp_[a-zA-Z0-9]{36}/g, type: "GitHub PAT" },
  { pattern: /gho_[a-zA-Z0-9]{36}/g, type: "GitHub OAuth Token" },
  { pattern: /ghs_[a-zA-Z0-9]{36}/g, type: "GitHub App Token" },
  { pattern: /ghu_[a-zA-Z0-9]{36}/g, type: "GitHub User-to-server Token" },
  { pattern: /github_pat_[A-Za-z0-9_]{22,}/g, type: "GitHub Fine-grained PAT" },
  { pattern: /sk-[a-zA-Z0-9]{20}T3BlbkFJ[a-zA-Z0-9]{20}/g, type: "OpenAI API Key (legacy)" },
  { pattern: /sk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{20,}/g, type: "OpenAI API Key" },
  { pattern: /xox[bpors]-[0-9]{10,13}-[a-zA-Z0-9-]+/g, type: "Slack Token" },
  { pattern: /-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----/g, type: "Private Key" },
];

const session = await joinSession({
  onPermissionRequest: approveAll,
  hooks: {
    onSessionStart: async () => ({
      additionalContext:
        "[repo-shield] Security extension active. Never hardcode secrets — use environment variables for credentials.",
    }),
    onPreToolUse: async (input) => {
      if (SHELL_TOOLS.has(input.toolName)) {
        const cmd = String(input.toolArgs?.command || "");
        for (const { pattern, reason } of DANGEROUS_COMMANDS) {
          if (pattern.test(cmd)) {
            return {
              permissionDecision: "deny",
              permissionDecisionReason: `[repo-shield] BLOCKED: ${reason}.\nCommand: ${cmd}`,
            };
          }
        }
      }

      if (input.toolName === "create" || input.toolName === "edit") {
        const content = String(input.toolArgs?.file_text || input.toolArgs?.new_str || "");
        const detected = [];
        for (const { pattern, type } of SECRET_PATTERNS) {
          pattern.lastIndex = 0;
          if (pattern.test(content)) detected.push(type);
        }
        if (detected.length > 0) {
          return {
            permissionDecision: "deny",
            permissionDecisionReason:
              `[repo-shield] BLOCKED: Potential secrets detected:\n` +
              detected.map((s) => `  - ${s}`).join("\n") +
              `\nUse environment variables instead.`,
          };
        }
      }
    },
  },
  tools: [],
});
