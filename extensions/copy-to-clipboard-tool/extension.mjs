// copy-to-clipboard-tool — cross-platform clipboard tool.
//
// Detects the right binary at runtime:
//   • macOS    → pbcopy
//   • Windows  → clip
//   • Wayland  → wl-copy
//   • X11      → xclip -selection clipboard
//
// Errors (missing binary, write failure) are returned to the caller as a
// human-readable string instead of throwing.

import { spawn } from "node:child_process";
import { approveAll } from "@github/copilot-sdk";
import { joinSession } from "@github/copilot-sdk/extension";

function detectClipboard() {
  if (process.platform === "darwin") return { cmd: "pbcopy", args: [] };
  if (process.platform === "win32") return { cmd: "clip", args: [] };
  if (process.env.WAYLAND_DISPLAY) return { cmd: "wl-copy", args: [] };
  if (process.env.DISPLAY) return { cmd: "xclip", args: ["-selection", "clipboard"] };
  return null;
}

function copyToClipboard(text) {
  return new Promise((resolveP, rejectP) => {
    const cb = detectClipboard();
    if (!cb) {
      return rejectP(new Error("No clipboard utility available (need pbcopy/clip/wl-copy/xclip)"));
    }
    let proc;
    try {
      proc = spawn(cb.cmd, cb.args, { stdio: ["pipe", "ignore", "pipe"] });
    } catch (err) {
      return rejectP(err);
    }
    let stderr = "";
    let settled = false;
    const settleErr = (err) => {
      if (settled) return;
      settled = true;
      try { proc.kill(); } catch {}
      rejectP(err);
    };
    const settleOk = () => {
      if (settled) return;
      settled = true;
      resolveP();
    };

    // 5s ceiling — wl-copy/xclip can hang forever holding stdin in
    // headless / broken-DISPLAY scenarios.
    const timer = setTimeout(() => settleErr(new Error(`${cb.cmd} timed out after 5s`)), 5000);

    proc.on("error", settleErr);
    proc.stderr.on("data", (chunk) => (stderr += chunk));
    // EPIPE on stdin if child exits before consuming input — handle, don't crash.
    proc.stdin.on("error", settleErr);
    proc.on("close", (code) => {
      clearTimeout(timer);
      if (code === 0) settleOk();
      else settleErr(new Error(`${cb.cmd} exited ${code}: ${stderr.trim() || "(no stderr)"}`));
    });
    try {
      proc.stdin.end(text);
    } catch (err) {
      settleErr(err);
    }
  });
}

const session = await joinSession({
  onPermissionRequest: approveAll,
  hooks: {
    onUserPromptSubmitted: async (input) => {
      // Only nudge when the user is clearly asking for clipboard work —
      // bare "copy" is too noisy ("copy this pattern", "let me copy that down").
      const p = input.prompt || "";
      if (
        /\bclipboard\b/i.test(p) ||
        /\bcopy\b[^.]{0,40}\bclipboard\b/i.test(p)
      ) {
        return {
          additionalContext:
            "[clipboard] User mentioned clipboard. Use the copy_to_clipboard tool for the relevant output.",
        };
      }
    },
  },
  tools: [
    {
      name: "copy_to_clipboard",
      description: "Copy text to the system clipboard (cross-platform: pbcopy/clip/wl-copy/xclip).",
      parameters: {
        type: "object",
        properties: { text: { type: "string", description: "Text to copy." } },
        required: ["text"],
      },
      handler: async (args) => {
        try {
          await copyToClipboard(String(args.text ?? ""));
          return "Copied to clipboard.";
        } catch (err) {
          return `Error: ${err.message}`;
        }
      },
    },
  ],
});
