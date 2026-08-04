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
// THIS FILE IS SDK WIRING AND NOTHING ELSE, on purpose -- not one branch, not
// one `if`. `joinSession` is called at import, so importing this module has a
// side effect and no test can reach a single line of it: everything here is
// covered by `node --check` and by nothing else, while everything in guard.mjs
// is covered by the node test suite. That makes the line a budget rather than
// a preference, and `guard.test.mjs` spends it as one -- it parses this file
// and fails if any decision reappears here. The hook BODIES are in guard.mjs
// behind `createGuard`, which is where they can be run. See
// docs/checkout-guard.md.

import { approveAll } from "@github/copilot-sdk";
import { joinSession } from "@github/copilot-sdk/extension";
import { createGuard } from "./guard.mjs";

const guard = createGuard();

const session = await joinSession({
  onPermissionRequest: approveAll,
  hooks: {
    onSessionStart: guard.onSessionStart,
    onPreToolUse: guard.onPreToolUse,
    onPostToolUse: guard.onPostToolUse,
  },
  tools: [],
});
