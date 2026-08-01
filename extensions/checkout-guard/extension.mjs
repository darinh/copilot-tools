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
//      back immediately, while the producer is still known. When the session
//      is running inside a linked worktree the repository's primary checkout
//      is rescanned as well: a subagent starts its own shell, so a relative
//      path resolves against whatever directory it began in, and the primary
//      is the tree every other agent resolves as the project. That case had
//      nothing watching it, and it is the one that actually fired -- three
//      artifacts appeared in a primary checkout while their author worked in a
//      worktree, and the guard reported clean in the same words it uses when
//      nothing happened.
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
//
// THIS FILE IS SDK WIRING AND NOTHING ELSE, on purpose. `joinSession` is
// called at import, so importing this module has a side effect and no test can
// reach a single line of it: everything here is covered by `node --check` and
// by nothing else, while everything in guard.mjs is covered by the node test
// suite. That makes the line a budget rather than a preference -- put logic in
// guard.mjs. See docs/checkout-guard.md.

import { mkdirSync } from "node:fs";
import { approveAll } from "@github/copilot-sdk";
import { joinSession } from "@github/copilot-sdk/extension";
import {
  AUTHORING_TOOLS,
  SHELL_TOOLS,
  SUBAGENT_TOOLS,
  blockReason,
  checkoutRoot,
  createGuardState,
  guardDisabled,
  noteAuthored,
  observe,
  otherRootToWatch,
  primaryStrayReport,
  scanCheckoutTree,
  scratchDirFor,
  sessionContext,
  setFor,
  strayReport,
  sweepDecision,
} from "./guard.mjs";

const DISABLED = guardDisabled();
const scratchDir = scratchDirFor();
const state = createGuardState();

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
      const seeds = [];
      if (root) {
        const initial = await scanCheckoutTree(root);
        if (initial !== null) state.lastSeen.set(root, initial);
        // Seeded AND reported. A stray present at seed time is invisible to
        // every later hook by construction -- `observe` answers "what is
        // new", and this never will be again -- so a session not told here is
        // never told at all. `sessionContext` drops a null scan rather than
        // reporting an empty checkout.
        seeds.push({ strays: initial, root });
        // Seed the primary too, or its entire contents would read as new the
        // first time a command is run and every existing file in it would be
        // reported as this agent's doing.
        const other = await otherRootToWatch(state, root);
        if (other) {
          const seedOther = await scanCheckoutTree(other);
          if (seedOther !== null) state.lastSeen.set(other, seedOther);
          // The seed that motivated this: an agent working in a worktree is
          // the one for whom strays in the primary are both invisible and
          // most expensive, and `root` is not the primary for that agent.
          seeds.push({ strays: seedOther, root: other, primary: true });
        }
      }
      return { additionalContext: sessionContext(scratchDir, seeds) };
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
      const unannounced = await observe(state, root);
      const decision = sweepDecision(command, [...setFor(state.outstanding, root)]);
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
        if (filePath) noteAuthored(state, root, filePath);
        return;
      }

      if (!SHELL_TOOLS.has(input.toolName) && !SUBAGENT_TOOLS.has(input.toolName)) return;
      const fresh = await observe(state, root);
      // The primary is scanned as well as, never instead of, the working
      // checkout -- but only after a SUBAGENT call, not after every command.
      //
      // Both real incidents came from subagents, and the restriction is what
      // keeps this from being noise: the primary is shared, so scanning it
      // after every shell command would report every peer agent's artifacts to
      // every other agent, and a guard that cries wolf gets switched off. This
      // agent's own shell commands cannot surprise it in another tree -- it
      // would have had to name the path.
      const other = SUBAGENT_TOOLS.has(input.toolName)
        ? await otherRootToWatch(state, root)
        : null;
      const elsewhere = other
        ? await observe(state, other, { blocking: false, scan: scanCheckoutTree })
        : [];
      if (fresh.length === 0 && elsewhere.length === 0) return;
      const reports = [];
      if (fresh.length > 0) reports.push(strayReport(fresh, scratchDir));
      if (elsewhere.length > 0) {
        reports.push(primaryStrayReport(elsewhere, scratchDir, other));
      }
      return { additionalContext: reports.join("\n\n") };
    },
  },
  tools: [],
});
