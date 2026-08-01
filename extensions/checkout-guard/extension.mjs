// checkout-guard — keeps agents' ad-hoc scratch files out of the checkout.
//
// The rule "probe scripts write to a temp dir, not the shared checkout" was
// advisory for a long time, and advisory did not hold: in one evening three
// agents lost hours to a pile of directories nobody could attribute. The
// mechanism was eventually caught in the act -- adversarial review subagents
// reproducing bugs with throwaway scripts that used relative paths, producing
// nine directories from a single review round. It was never the test suite.
//
// This extension makes the rule enforceable at the only place that can see an
// agent's behaviour as it happens:
//
//   1. Session start creates a scratch directory and names it, so the correct
//      place to write is discoverable rather than merely mandated.
//   2. After every command that can run arbitrary code -- including `task`,
//      because a subagent's writes land in the parent's checkout -- the
//      checkout is rescanned and newly appeared untracked paths are reported
//      back immediately, while the producer is still known.
//   3. A blanket `git add -A`, or a `git stash` that takes untracked files, is
//      DENIED while such artifacts are outstanding. Staging a path by name is
//      always allowed: the aim is to stop artifacts being committed
//      *unnoticed*, not to stop them being committed.
//
// Files written with the create/edit tools are never treated as strays. Those
// are the sanctioned way to author content, and the distinction between "a
// file the agent decided to write" and "a shell command's side effect" is
// exactly that.
//
// Fails open everywhere. A guard that breaks a session is worse than the
// artifacts it prevents.
//
// Knobs: COPILOT_CHECKOUT_GUARD_DISABLE=1 turns it off entirely.

import { mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, resolve, isAbsolute } from "node:path";
import { approveAll } from "@github/copilot-sdk";
import { joinSession } from "@github/copilot-sdk/extension";
import {
  blockReason,
  checkoutRoot,
  newEntries,
  scanCheckout,
  sessionBriefing,
  strayReport,
  sweepDecision,
} from "./guard.mjs";

const SHELL_TOOLS = new Set(["bash", "powershell", "shell"]);
// A subagent runs its own shell in this same checkout, so its artifacts land
// here. The parent never sees those commands, only the `task` call, which is
// therefore the only point at which they can be attributed at all.
const SUBAGENT_TOOLS = new Set(["task", "agent"]);
const AUTHORING_TOOLS = new Set(["create", "edit"]);

// An agent that ignores the report would otherwise accumulate an unbounded
// list and be told about the same artifacts after every command it runs.
const MAX_TRACKED = 200;

const DISABLED = process.env.COPILOT_CHECKOUT_GUARD_DISABLE === "1";
const scratchDir = join(tmpdir(), "copilot-scratch", `session-${process.pid}`);

/** Untracked paths at the last observation, keyed by checkout root. */
const lastSeen = new Map();
/** Artifacts reported and not yet cleaned up, keyed by checkout root. */
const outstanding = new Map();
/** Paths authored deliberately with create/edit, keyed by checkout root. */
const authored = new Map();

function setFor(map, root) {
  let value = map.get(root);
  if (!value) {
    value = new Set();
    map.set(root, value);
  }
  return value;
}

function bound(set) {
  if (set.size <= MAX_TRACKED) return;
  const it = set.values();
  while (set.size > MAX_TRACKED) set.delete(it.next().value);
}

/** A create/edit path in the checkout-relative posix form git reports. */
function relativeToCheckout(root, filePath) {
  const absolute = isAbsolute(filePath) ? filePath : resolve(process.cwd(), filePath);
  const rel = relative(root, absolute).replace(/\\/g, "/");
  return rel && !rel.startsWith("../") ? rel : null;
}

/**
 * Refresh the record of what is in the checkout and return anything new.
 *
 * Artifacts that no longer exist are dropped from the outstanding set, so an
 * agent that cleans up is not then blocked by the memory of a file it has
 * already deleted.
 */
async function observe(root) {
  const current = await scanCheckout(root);
  if (current === null) return [];
  const seen = lastSeen.get(root);
  lastSeen.set(root, current);
  // No baseline yet means nothing can honestly be called new. Reporting the
  // whole checkout on first sight would blame this agent for every artifact
  // any previous one left, which is the misattribution the guard exists to
  // prevent.
  if (seen === undefined) return [];

  const alive = new Set(current);
  const pending = setFor(outstanding, root);
  for (const path of [...pending]) {
    if (!alive.has(path)) pending.delete(path);
  }

  const deliberate = setFor(authored, root);
  const fresh = newEntries(seen, current).filter((p) => !deliberate.has(p));
  for (const path of fresh) pending.add(path);
  bound(pending);
  return fresh;
}

const session = await joinSession({
  onPermissionRequest: approveAll,
  hooks: {
    onSessionStart: async () => {
      if (DISABLED) return;
      try {
        mkdirSync(scratchDir, { recursive: true });
      } catch {
        // An unwritable temp directory is not a reason to fail a session; the
        // briefing still names the intended location.
      }
      const root = await checkoutRoot(process.cwd());
      if (root) {
        const initial = await scanCheckout(root);
        if (initial !== null) lastSeen.set(root, initial);
      }
      return { additionalContext: sessionBriefing(scratchDir) };
    },

    onPreToolUse: async (input) => {
      if (DISABLED) return;
      if (!SHELL_TOOLS.has(input.toolName)) return;
      const command = String(input.toolArgs?.command || "");
      // Cheap reject first: this hook runs on every shell command, and the
      // overwhelming majority of them are not git.
      if (!/\bgit\b/i.test(command)) return;
      const root = await checkoutRoot(process.cwd());
      if (!root) return;
      // Re-observe rather than trusting the last scan. Anything this turns up
      // arrived without a tool call of this agent's in between -- a peer agent
      // in the same checkout, or a background process -- so it has never been
      // reported, and the block message says so rather than implying the agent
      // made it. Silently folding it into the outstanding set would produce a
      // block citing artifacts the agent had never been shown.
      const unannounced = await observe(root);
      const decision = sweepDecision(command, [...setFor(outstanding, root)]);
      if (!decision) {
        if (unannounced.length === 0) return;
        return { additionalContext: strayReport(unannounced, scratchDir) };
      }
      return {
        permissionDecision: "deny",
        permissionDecisionReason: blockReason({ ...decision, unannounced }),
      };
    },

    onPostToolUse: async (input) => {
      if (DISABLED) return;
      const root = await checkoutRoot(process.cwd());
      if (!root) return;

      if (AUTHORING_TOOLS.has(input.toolName)) {
        const filePath = String(input.toolArgs?.path || "");
        if (!filePath) return;
        const rel = relativeToCheckout(root, filePath);
        if (!rel) return;
        const deliberate = setFor(authored, root);
        deliberate.add(rel);
        bound(deliberate);
        setFor(outstanding, root).delete(rel);
        // Fold the authored path straight into the baseline instead of
        // rescanning. A full `git status -uall` walks the entire working tree,
        // and the CLI issues parallel edit calls in a single response, so a
        // five-file edit would have fired five concurrent whole-tree
        // traversals -- the guard becoming the most expensive thing in the
        // session. The answer a rescan would give for this path is already
        // known: the agent just wrote it.
        const seen = lastSeen.get(root);
        if (seen && !seen.includes(rel)) lastSeen.set(root, [...seen, rel]);
        return;
      }

      if (!SHELL_TOOLS.has(input.toolName) && !SUBAGENT_TOOLS.has(input.toolName)) return;
      const fresh = await observe(root);
      if (fresh.length === 0) return;
      return { additionalContext: strayReport(fresh, scratchDir) };
    },
  },
  tools: [],
});
