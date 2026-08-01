// Unit tests for checkout-guard's decision logic. Run with `node --test`.
//
// Convention enforced throughout this file: every assertion that something is
// ALLOWED is paired, in the same test, with a case proving the same code path
// blocks when it should. An "allowed" result is indistinguishable from a
// predicate that never matched anything at all, and a test that cannot tell
// those apart passes just as happily when the guard is broken. Two agents
// shipped exactly that shape in one evening on this repository.
//
// The same rule, in the direction that is easier to miss: an assertion that a
// scan OMITS something needs a positive assertion through THE SAME CALL, not
// merely somewhere in the same test. A control on a neighbouring function
// proves that function ran; it says nothing about the one being asserted
// about, so `!found.includes(x)` still passes when `found` is empty for a
// reason nobody intended. Two tests in this file were vacuous for exactly that
// reason and were caught by mutation, not by reading -- for a negative claim,
// the premise is that the mechanism can fire at all.

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  addIsBlanket,
  blockReason,
  bound,
  checkoutRoot,
  createGuardState,
  emptyDirCandidates,
  formatPathList,
  git,
  gitInvocations,
  gitSubcommand,
  guardDisabled,
  holdsNoFiles,
  invisibleDirStrays,
  newEntries,
  noteAuthored,
  observe,
  otherRootToWatch,
  parseStatusPaths,
  parseUntracked,
  nestedWorktreePrefixes,
  primaryCheckoutRoot,
  relativeToCheckout,
  rootToWatch,
  scratchDirFor,
  setFor,
  AUTHORING_TOOLS,
  MAX_TRACKED,
  SHELL_TOOLS,
  SUBAGENT_TOOLS,
  UNKNOWN_ROOT,
  inheritedStrayReport,
  primaryStrayReport,
  scanCheckout,
  scanCheckoutTree,
  unscannedRootsNotice,
  withoutNestedWorktrees,
  withoutIgnored,
  sessionBriefing,
  sessionContext,
  stashTakesUntracked,
  strayReport,
  sweepDecision,
  tokenizeCommand,
} from "./guard.mjs";

const NUL = "\0";
const record = (...records) => records.map((r) => r + NUL).join("");

test("parseUntracked returns only untracked records", () => {
  const stdout = record("?? probe.py", " M tracked.py", "?? .test_root4/x", "A  staged.py");
  assert.deepEqual(parseUntracked(stdout), ["probe.py", ".test_root4/x"]);
});

test("parseUntracked keeps paths that the non-z format would have mangled", () => {
  // core.quotePath wraps these in quotes with C escapes in the default format.
  // The -z form emits them raw, which is the entire reason for using it.
  const awkward = ['?? my probe dir/run.py', '?? "quoted".txt', "?? café/naïve.py"];
  assert.deepEqual(parseUntracked(record(...awkward)), [
    "my probe dir/run.py",
    '"quoted".txt',
    "café/naïve.py",
  ]);
});

test("parseUntracked tolerates trailing separators and empty output", () => {
  assert.deepEqual(parseUntracked(""), []);
  assert.deepEqual(parseUntracked(NUL), []);
  assert.deepEqual(parseUntracked(undefined), []);
  assert.deepEqual(parseUntracked(record("?? a.py")), ["a.py"]);
});

test("newEntries reports additions only, sorted and deduplicated", () => {
  assert.deepEqual(newEntries(["a"], ["a", "c", "b", "c"]), ["b", "c"]);
  // Paired negative: an unchanged listing must produce nothing, so the result
  // above is not just "everything in the second argument".
  assert.deepEqual(newEntries(["a", "b"], ["b", "a"]), []);
});

test("emptyDirCandidates finds the directories git cannot report", () => {
  // The real case: t_src/ and target/ exist in this repository, are untracked
  // and unignored, and `git status` calls the tree clean because git does not
  // track empty directories.
  assert.deepEqual(emptyDirCandidates(["t_src", "target"], []), ["t_src/", "target/"]);
});

test("emptyDirCandidates drops directories git already knows about, and its own plumbing", () => {
  const dirs = ["t_src", "skills", "docs", ".git", ".worktrees"];
  // `known` is everything git has any knowledge of: paths from `git status`
  // and committed top-level entries.
  const known = ["skills/demo/SKILL.md", "docs"];
  // Presence first: t_src survives, so a shrinking result below is the filter
  // working rather than the whole function returning nothing.
  const got = emptyDirCandidates(dirs, known);
  assert.ok(got.includes("t_src/"));
  assert.ok(!got.includes("skills/"), "a directory whose file git reported is redundant");
  assert.ok(!got.includes("docs/"), "a committed directory is not a stray");
  assert.ok(!got.includes(".git/"));
  assert.ok(!got.includes(".worktrees/"));
});

test("parseStatusPaths returns paths of every status, both sides of a rename", () => {
  const stdout = record("?? probe.py", " M tracked.py", "A  staged.py", "R  new.py", "old.py");
  assert.deepEqual(parseStatusPaths(stdout), [
    "probe.py", "tracked.py", "staged.py", "new.py", "old.py",
  ]);
  // Paired control: the untracked-only parser sees just the one record, which
  // is exactly why it is insufficient for deciding what git knows about.
  assert.deepEqual(parseUntracked(stdout), ["probe.py"]);
});

test("gitInvocations splits chained commands and ignores lookalikes", () => {
  assert.deepEqual(gitInvocations("mkdir probe && git add -A && git commit -m x"), [
    ["add", "-A"],
    ["commit", "-m", "x"],
  ]);
  assert.deepEqual(gitInvocations("/usr/bin/git status"), [["status"]]);
  assert.deepEqual(gitInvocations("git.exe status"), [["status"]]);
  // Paired negative control: names merely containing "git" are not git.
  assert.deepEqual(gitInvocations("github status; mygit add -A; legit add -A"), []);
});

test("gitSubcommand skips global options and flags repository redirection", () => {
  assert.equal(gitSubcommand(["-c", "user.name=x", "add", "-A"]).name, "add");
  assert.equal(gitSubcommand(["--no-pager", "add"]).name, "add");
  assert.deepEqual(gitSubcommand(["add", "-A", "src"]).args, ["-A", "src"]);
  assert.equal(gitSubcommand(["status"]).redirected, false);
  assert.equal(gitSubcommand(["-C", "../other", "add", "-A"]).redirected, true);
  assert.equal(gitSubcommand(["--git-dir=/tmp/x/.git", "add", "-A"]).redirected, true);
  assert.equal(gitSubcommand(["--not-a-subcommand"]), null);
});

test("addIsBlanket recognises every form that stages untracked files", () => {
  for (const args of [["-A"], ["--all"], ["."], ["./"], [":/"], ["*"], ["-Av"], ["-fA"], ["-A", "."]]) {
    assert.equal(addIsBlanket(args), true, `${args.join(" ")} should be blanket`);
  }
});

test("addIsBlanket leaves named paths, tracked-only updates and dry runs alone", () => {
  // Presence first: the blanket form is detected by this same function, so a
  // false below cannot be a predicate that matches nothing.
  assert.equal(addIsBlanket(["-A"]), true);
  for (const args of [["src/main.py"], ["-u"], ["--update"], ["-p"], ["-N", "x.py"], ["--", "-A"]]) {
    assert.equal(addIsBlanket(args), false, `${args.join(" ")} should not be blanket`);
  }
  // A dry run stages nothing, and it is how an agent inspects what a sweep
  // would take -- blocking it would obstruct the caution being asked for.
  assert.equal(addIsBlanket(["-A", "--dry-run"]), false);
  assert.equal(addIsBlanket(["-An"]), false);
});

test("stashTakesUntracked distinguishes creating forms carrying -u from the rest", () => {
  assert.equal(stashTakesUntracked(["-u"]), true);
  assert.equal(stashTakesUntracked(["--include-untracked"]), true);
  assert.equal(stashTakesUntracked(["push", "-u", "-m", "wip"]), true);
  assert.equal(stashTakesUntracked(["--all"]), true);
  // Paired negatives: forms that cannot sweep an untracked file.
  assert.equal(stashTakesUntracked([]), false);
  assert.equal(stashTakesUntracked(["push", "-m", "wip"]), false);
  assert.equal(stashTakesUntracked(["pop"]), false);
  assert.equal(stashTakesUntracked(["apply"]), false);
  assert.equal(stashTakesUntracked(["list"]), false);
});

test("sweepDecision blocks a blanket add while artifacts are outstanding", () => {
  const strays = ["target/", ".test_root_logic1/"];
  const decision = sweepDecision("git add -A && git commit -m wip", strays);
  assert.ok(decision, "a blanket add with outstanding strays must be blocked");
  assert.equal(decision.verb, "git add");
  assert.deepEqual(decision.strays, [".test_root_logic1/", "target/"]);
});

test("sweepDecision allows the same command once the checkout is clean", () => {
  // Presence first. Without this pairing, the null below would also be
  // produced by a sweepDecision that never matches `git add -A` at all.
  assert.ok(sweepDecision("git add -A", ["stray.py"]), "control: blocks when strays exist");
  assert.equal(sweepDecision("git add -A", []), null);
  assert.equal(sweepDecision("git add -A", undefined), null);
});

test("sweepDecision leaves a named pathspec alone, which is the escape hatch", () => {
  const strays = ["probe.py"];
  assert.ok(sweepDecision("git add -A", strays), "control: the blanket form is blocked");
  assert.equal(sweepDecision("git add src/main.py", strays), null);
  assert.equal(sweepDecision("git add probe.py", strays), null,
    "naming the artifact is a decision, and decisions are allowed");
});

test("sweepDecision ignores commands aimed at another repository", () => {
  const strays = ["probe.py"];
  assert.ok(sweepDecision("git add -A", strays), "control: blocked in this checkout");
  assert.equal(sweepDecision("git -C ../other-repo add -A", strays), null,
    "strays are specific to one checkout; blocking elsewhere is a wrong answer");
});

test("sweepDecision blocks an untracked-sweeping stash but not a plain one", () => {
  const strays = ["probe.py"];
  const blocked = sweepDecision("git stash -u", strays);
  assert.ok(blocked);
  assert.equal(blocked.verb, "git stash");
  assert.equal(sweepDecision("git stash", strays), null);
  assert.equal(sweepDecision("git stash pop", strays), null);
});

test("sweepDecision is not fooled by git appearing mid-command", () => {
  const strays = ["probe.py"];
  assert.ok(sweepDecision("cd repo && git add -A", strays), "control: chained git is still git");
  assert.equal(sweepDecision("echo 'git add -A is blocked' > note.txt", strays), null,
    "the phrase inside a quoted argument is not an invocation of git");
});

test("messages name the artifacts and the way out", () => {
  const reason = blockReason({ verb: "git add", strays: ["target/", "probe.py"] });
  assert.match(reason, /BLOCKED/);
  assert.match(reason, /target\//);
  assert.match(reason, /probe\.py/);
  assert.match(reason, /git add <path>/, "the escape hatch must be stated, not implied");
  assert.match(reason, /COPILOT_CHECKOUT_GUARD_DISABLE=1/);

  const report = strayReport(["target/"], "/tmp/copilot-scratch/session-1");
  assert.match(report, /target\//);
  assert.match(report, /\/tmp\/copilot-scratch\/session-1/);

  const briefing = sessionBriefing("/tmp/copilot-scratch/session-1");
  assert.match(briefing, /\/tmp\/copilot-scratch\/session-1/);
  assert.match(briefing, /subagents/i, "subagent writes land in the parent's checkout");
});

test("message pluralisation is correct at one and many", () => {
  assert.match(blockReason({ verb: "git add", strays: ["a"] }), /1 stray artifact /);
  assert.match(blockReason({ verb: "git add", strays: ["a", "b"] }), /2 stray artifacts /);
  assert.match(strayReport(["a"], "/tmp/s"), /1 new untracked path /);
  assert.match(strayReport(["a", "b"], "/tmp/s"), /2 new untracked paths /);
  assert.match(primaryStrayReport(["a"], "/tmp/s", "/repo"), /1 new untracked path /);
  assert.match(primaryStrayReport(["a", "b"], "/tmp/s", "/repo"), /2 new untracked paths /);
});

/**
 * The first line of a report, which is the only part of it no stray path can
 * author. Everything below the headline is `formatPathList` output, and that
 * interpolates names raw -- so a file named after the marker puts the marker
 * in the report. `includes` over the whole string is therefore not an
 * attribution, and the test that used one was green while the property it
 * claimed was false.
 */
const headline = (report) => report.split("\n")[0];
test("the primary-checkout report names the other tree and does not order a deletion", () => {
  const report = primaryStrayReport(["test_order.py"], "/tmp/scratch", "/repo/primary");
  assert.match(report, /test_order\.py/);
  assert.match(report, /\/repo\/primary/, "the agent cannot act on a report that does not say where");
  assert.match(report, /PRIMARY/, "the whole finding is that this is not the tree in use");
  assert.match(report, /\/tmp\/scratch/, "somewhere to put the work instead");
  // A subagent's leftovers and a peer's live experiment are indistinguishable
  // from here. Telling the agent to delete on that evidence is the exact
  // destroy-on-a-guess move this toolkit exists to remove from code.
  assert.match(report, /ask the other agents before/i);
  // Control, through THE SAME call. The working-checkout report is a
  // different message, so a wiring mistake that returned the wrong one cannot
  // pass the assertions above. The positive is required: the assertions in
  // this test so far all go through `primaryStrayReport`, and an absence
  // asserted about `strayReport` proves nothing until something establishes
  // that `strayReport` produces text at all.
  const working = strayReport(["test_order.py"], "/tmp/scratch");
  assert.match(working, /test_order\.py/, "control: the working report does report the path");
  assert.match(working, /appeared in the checkout/, "control: and has its own wording");
  assert.ok(!headline(working).includes("PRIMARY"), "the two reports must not be the same text");
});


test("PRIMARY in the headline is a marker only the primary report can produce", () => {
  // This is what lets a caller -- or a test -- attribute a line in the
  // combined context to one report rather than the other. Both are joined
  // into a single `additionalContext` string, so without an exclusive marker
  // any assertion of the form `context.includes(file)` has two possible
  // authors and cannot tell a working-tree report from a primary one. A
  // wiring probe of mine asserted exactly that and passed while the
  // primary-watching path did nothing at all.
  //
  // The exclusivity is HEADLINE-scoped and that qualifier is the whole point.
  // An earlier version of this test claimed the phrase was exclusive to the
  // report anywhere in its text. That was false: stray names are interpolated
  // raw, so a path can put any string into the body. The first line is the
  // only region a path cannot reach, because the header is prepended before
  // any name is formatted.
  const withPrimary = primaryStrayReport(["a.py"], "/tmp/s", "/repo/primary");
  const withoutPrimary = strayReport(["a.py"], "/tmp/s");
  assert.ok(headline(withPrimary).includes("PRIMARY checkout"), headline(withPrimary));
  assert.ok(!headline(withoutPrimary).includes("PRIMARY checkout"), headline(withoutPrimary));

  // The forgery the earlier test was too weak to catch. Asserting that a
  // stray named "PRIMARY checkout" cannot forge the marker was true and
  // useless -- the marker relied on is the longer phrase, and a name carrying
  // THAT does land in the body. So the forged input has to be the exact
  // substring the attribution keys off, not a shorter relative of it.
  const forgery = "in the PRIMARY checkout (";
  const forged = strayReport([forgery], "/tmp/s");
  assert.ok(forged.includes(forgery), "premise: the forged name really does reach the report body");
  assert.ok(!headline(forged).includes("PRIMARY checkout"),
    "a path reaches the body, never the headline");
});


// --- inherited strays: what was already there when the session began -----

test("the inherited report names the paths, the tree, and why now is the only chance", () => {
  const report = inheritedStrayReport(["archivedir3/", "no_read/"], "/repo/primary");
  assert.match(report, /archivedir3\//);
  assert.match(report, /no_read\//);
  assert.match(report, /\/repo\/primary/, "an agent cannot act on a report that does not say where");
  // The justification for the feature existing at all. Without it the reader
  // has no reason to act during this session rather than "later", and later
  // is precisely when the information is gone.
  assert.match(report, /only point at which/i);
  assert.match(report, /git status.*does not list/is,
    "empty directories are the population git structurally cannot report");
});

test("the inherited report does not order a deletion and does not send anyone hunting", () => {
  const report = inheritedStrayReport(["probe.py"], "/repo");
  // Same rule as primaryStrayReport: a peer's live experiment and a dead
  // subagent's leftovers are indistinguishable from here.
  assert.match(report, /do not delete on this report\s+alone/i);
  assert.match(report, /ask the other agents/i);
  // Unattributable, not merely unattributed -- the distinction the wording is
  // carrying. "Unknown owner" starts a search; there is no owner to find,
  // because the session that made the artifact has ended. A reader told the
  // search is futile does the cheap thing instead.
  assert.match(report, /cannot find out who made/i);
  assert.match(report, /neither can anyone\s+else later/i);
  // And it says what WOULD license a deletion, so "nobody claimed it" cannot
  // quietly become the standard of evidence.
  assert.match(report, /inert/i);
  assert.match(report, /mtimes/i);
});

test("the inherited report does not assert that its own contents are agent leftovers", () => {
  // The population is the checkout's ENTIRE untracked state, which contains a
  // human's uncommitted work and unignored build output as readily as it
  // contains a dead subagent's probe script. An earlier draft told the reader
  // flatly that "the session that produced it is over" -- a confident cause
  // asserted over an ambiguous observation, which is the exact collapse this
  // guard exists to catch, committed by the guard itself. A developer told
  // their own work in progress is an orphaned artifact switches the guard off.
  const report = inheritedStrayReport(["my_wip.js"], "/repo");
  assert.match(report, /may be your own uncommitted work/i);
  assert.match(report, /not a list of suspects/i);
  // The futility claim must survive, but SCOPED: it applies to whichever of
  // these are not the reader's, and the report must say it cannot tell which.
  assert.match(report, /for any that are NOT yours/i);
  // The specific overclaim must not come back. Asserted over the whole text
  // rather than the headline on purpose: this is a claim about what the
  // report never says, and the body is where it used to say it.
  assert.ok(!/the session that produced (it|them) is over/i.test(report), report);
});

test("INHERITED in the headline is a marker only the inherited report can produce", () => {
  // Same exclusivity rule as the PRIMARY marker above, and needed for the
  // same reason: all of these reports are joined into one additionalContext
  // string, so an assertion of the form `context.includes(path)` cannot tell
  // which report produced the line without a marker no other report emits.
  const inheritedReport = inheritedStrayReport(["a.py"], "/repo");
  assert.ok(headline(inheritedReport).startsWith("[checkout-guard] INHERITED:"),
    headline(inheritedReport));
  // Controls through the neighbouring calls, positive first: an absence
  // asserted about a function that produced no text proves nothing.
  const during = strayReport(["a.py"], "/tmp/s");
  const elsewhere = primaryStrayReport(["a.py"], "/tmp/s", "/repo/primary");
  assert.match(during, /a\.py/, "control: the during-session report does report the path");
  assert.match(elsewhere, /a\.py/, "control: so does the primary report");
  assert.ok(!headline(during).includes("INHERITED"), headline(during));
  assert.ok(!headline(elsewhere).includes("INHERITED"), headline(elsewhere));

  // A path cannot forge the marker, because the headline is built before any
  // name is formatted. The forged input must be the exact substring the
  // attribution keys off, not a shorter relative of it.
  const forgery = "[checkout-guard] INHERITED:";
  const forged = strayReport([forgery], "/tmp/s");
  assert.ok(forged.includes(forgery), "premise: the forged name really does reach the body");
  assert.ok(!headline(forged).startsWith(forgery), "a path reaches the body, never the headline");
});

test("the primary variant says which tree, and the working variant does not claim to be it", () => {
  // An agent in a worktree is the whole reason this distinction exists: for
  // them the primary is a different tree, and a report that reads as a note
  // about the current directory gets skimmed past.
  const other = inheritedStrayReport(["a.py"], "/repo/primary", { primary: true });
  const here = inheritedStrayReport(["a.py"], "/repo/wt");
  assert.ok(headline(other).includes("PRIMARY checkout"), headline(other));
  assert.match(other, /not the checkout you are working in/);
  assert.match(here, /a\.py/, "control: the working-tree variant produces a report at all");
  assert.ok(!headline(here).includes("PRIMARY checkout"), headline(here));
  // It must not inherit primaryStrayReport's "during your last command",
  // which is false of everything this function reports.
  assert.ok(!other.includes("during your last command"), other);
});

test("inherited pluralisation is correct at one and many", () => {
  assert.match(inheritedStrayReport(["a"], "/r"), /1 untracked path was/);
  assert.match(inheritedStrayReport(["a", "b"], "/r"), /2 untracked paths were/);

  // Pronoun CASE, not just number. Every other pronoun in the report is in
  // object position ("mention them", "leave them alone"), so a single
  // object-case variable reads correctly nearly everywhere -- and then reads
  // as "the only point at which them can be raised" at the one place the
  // pronoun is a subject. A plural-number-only assertion passes straight
  // through that, which is how it shipped: the singular branch is grammatical
  // either way, so nothing that tests one stray can see it.
  const one = inheritedStrayReport(["a"], "/r");
  const many = inheritedStrayReport(["a", "b"], "/r");
  assert.match(one, /point at which it can be raised/, one);
  assert.match(many, /point at which they can be raised/, many);
  assert.ok(!/which them can/.test(many), many);

  // The control: object position stays object-case in the same report, so
  // this is not just "every pronoun was renamed to they".
  assert.match(many, /Nothing will mention them again/, many);
});

test("UNSCANNED is a headline marker of its own, and a mixed session gets both reports", () => {
  // The realistic failure: one tree answers and the other does not. The
  // answered one must still be reported, and the unanswered one must not be
  // quietly folded into it -- otherwise a partial session start reads as a
  // complete one.
  const context = sessionContext("/tmp/s", [
    { strays: ["wip.js"], root: "/repo/wt" },
    { strays: null, root: "/repo/primary", primary: true },
  ]);
  assert.match(context, /wip\.js/, context);
  assert.match(context, /UNSCANNED/, context);
  assert.match(context, /\/repo\/primary/, context);
  const lines = context.split("\n");
  assert.equal(lines.filter((l) => l.startsWith("[checkout-guard] INHERITED:")).length, 1, context);
  assert.equal(lines.filter((l) => l.startsWith("[checkout-guard] UNSCANNED:")).length, 1, context);

  // Exclusivity, with positive controls first: each neighbouring report must
  // produce text, or an absence asserted about it proves nothing.
  const inheritedOnly = inheritedStrayReport(["a.py"], "/r");
  const unscannedOnly = unscannedRootsNotice(["/r"]);
  assert.match(inheritedOnly, /a\.py/, "control: the inherited report produces text");
  assert.match(unscannedOnly, /\/r/, "control: the unscanned notice produces text");
  assert.ok(!headline(inheritedOnly).includes("UNSCANNED"), headline(inheritedOnly));
  assert.ok(!headline(unscannedOnly).includes("INHERITED"), headline(unscannedOnly));

  // A root name cannot forge the marker: the headline is built before any
  // name is formatted, so a path reaches the body and never line 0.
  const forgery = "[checkout-guard] UNSCANNED:";
  const forged = inheritedStrayReport([forgery], "/r");
  assert.ok(forged.includes(forgery), "premise: the forged name really does reach the body");
  assert.ok(!headline(forged).startsWith(forgery), headline(forged));
});

test("the unscanned notice pluralises at one and many, in number and in case", () => {
  // Symmetric to the inherited pluralisation test, and added because the
  // inherited one caught a real pronoun-case defect while this function --
  // which has three independent singular/plural ternaries -- had no plural
  // coverage at all. Every existing UNSCANNED assertion passes exactly one
  // root, so a swapped ternary arm survives the whole suite. The count is
  // almost always 1 in practice (a single primary that failed to scan),
  // which is precisely why nothing would ever surface the other branch.
  const one = unscannedRootsNotice(["/a"]);
  const many = unscannedRootsNotice(["/a", "/b"]);

  assert.match(one, /1 checkout could not be examined/, one);
  assert.match(many, /2 checkouts could not be examined/, many);

  // The load-bearing sentence: it must not read as a clean bill of health at
  // either arity.
  assert.match(one, /not a report that it is clean/, one);
  assert.match(many, /not a report that they are clean/, many);

  assert.match(one, /sitting in it will go unmentioned/, one);
  assert.match(many, /sitting in them will go unmentioned/, many);

  assert.match(one, /when it was taken/, one);
  assert.match(many, /when they were taken/, many);

  // Controls against a swapped arm: each arity must NOT carry the other's
  // wording. Without these, a ternary with both arms set to the same string
  // would satisfy every assertion above at one of the two counts.
  assert.ok(!/they are clean/.test(one), one);
  assert.ok(!/it is clean/.test(many), many);
  assert.ok(!/when they were taken/.test(one), one);
  assert.ok(!/when it was taken/.test(many), many);
});

test("sessionContext emits the briefing byte-identically when nothing was inherited", () => {
  // The change is additive or it is a rewrite. A clean checkout must see the
  // exact text it saw before this function existed -- and an empty array and
  // a checkout that was never scanned must both count as nothing to report.
  const bare = sessionBriefing("/tmp/s");
  assert.equal(sessionContext("/tmp/s"), bare);
  assert.equal(sessionContext("/tmp/s", []), bare);
  assert.equal(sessionContext("/tmp/s", [{ strays: [], root: "/repo" }]), bare);
});

test("a scan that failed is not reported as a clean checkout", () => {
  // null means the scan established nothing. Rendering it as an empty
  // checkout is the collapse this whole toolkit exists to remove: an
  // unanswered question spent as the confident answer. Silence would BE that
  // collapse here, because a clean checkout is silent too -- so the failed
  // scan has to produce text a clean one never produces.
  const context = sessionContext("/tmp/s", [{ strays: null, root: "/repo" }]);
  assert.notEqual(context, sessionBriefing("/tmp/s"), context);
  assert.match(context, /UNSCANNED/, context);
  assert.match(context, /\/repo/, context);
  assert.match(context, /not a report that it is clean/i, context);
  // It must not be reported as an INHERITED finding either -- there are no
  // findings, that is the point.
  assert.ok(!context.includes("INHERITED"), context);
  // Control through the same call: a non-null scan at the same position
  // produces the other report, so the assertions above are about null and not
  // about sessionContext emitting this text unconditionally.
  const answered = sessionContext("/tmp/s", [{ strays: ["a.py"], root: "/repo" }]);
  assert.ok(answered.includes("INHERITED"), answered);
  assert.ok(!answered.includes("UNSCANNED"), answered);
  // And a scan that succeeded and found nothing is silent, which is what
  // makes the notice above informative rather than decorative.
  assert.equal(sessionContext("/tmp/s", [{ strays: [], root: "/repo" }]),
    sessionBriefing("/tmp/s"));
});

test("both seeded checkouts reach the session context", () => {
  // The failure this pins is silent and was predicted before it could
  // happen: the strays that motivated the feature live in the PRIMARY, and
  // an agent working in a worktree seeds the primary through a second,
  // separate scan. Wire only the first and the feature ships with its own
  // motivating case still unreported, with every unit test around it green.
  const context = sessionContext("/tmp/s", [
    { strays: ["wt-probe.py"], root: "/repo/wt" },
    { strays: ["archivedir3/"], root: "/repo/primary", primary: true },
  ]);
  assert.match(context, /wt-probe\.py/);
  assert.match(context, /archivedir3\//);
  // Two reports, not one merged list: the primary finding must keep its own
  // headline or the reader cannot tell which tree either path is in.
  const headlines = context.split("\n").filter((l) => l.startsWith("[checkout-guard] INHERITED:"));
  assert.equal(headlines.length, 2, context);
  assert.equal(headlines.filter((l) => l.includes("PRIMARY checkout")).length, 1, context);
  // The briefing still leads, because it names the scratch directory that
  // makes the reports actionable rather than merely alarming.
  assert.ok(context.startsWith(sessionBriefing("/tmp/s")), context.slice(0, 200));
});

// --- integration: a real git binary against a real repository ------------
//
// The pure tests above cannot tell whether `git status --porcelain -uall -z`
// actually emits what parseUntracked expects, or whether check-ignore's output
// really lines up with the names fed to it. Those are assumptions about
// another program, and the only honest way to check an assumption about
// another program is to run it.
//
// Every temporary repository is created under the OS temp directory, which is
// the same rule this guard exists to enforce. A test for a litter guard that
// litters would be its own counterexample.

async function withRepo(body) {
  const root = await mkdtemp(join(tmpdir(), "checkout-guard-test-"));
  try {
    // `git init` is enough: `status` and `check-ignore` both work in a
    // repository with no commits, so no identity configuration is needed.
    await git(["init", "-q"], root);
    await body(await realpath(root));
  } finally {
    await rm(root, { recursive: true, force: true, maxRetries: 3 });
  }
}

test("scanCheckout reports a fresh repository as clean", async () => {
  await withRepo(async (root) => {
    assert.deepEqual(await scanCheckout(root), []);
  });
});

test("scanCheckout does not call a committed directory a stray", async () => {
  // The regression this test exists for was found by running the scan against
  // a real repository rather than a fixture, and it was not subtle: the guard
  // reported docs/, tests/, templates/ and every other clean tracked directory
  // as an agent artifact. `git status` says nothing about a tracked directory
  // precisely because there is nothing wrong with it.
  await withRepo(async (root) => {
    await mkdir(join(root, "docs"));
    await writeFile(join(root, "docs", "readme.md"), "# docs\n");
    await git(["add", "docs/readme.md"], root);
    await git(
      ["-c", "user.email=test@example.invalid", "-c", "user.name=test",
       "commit", "-q", "-m", "initial"],
      root,
    );
    // Presence first: an empty stray beside it is still reported, so a clean
    // result for docs/ is the fix working and not the scan going blind.
    await mkdir(join(root, "t_src"));
    const found = await scanCheckout(root);
    assert.ok(found.includes("t_src/"), `expected t_src/ in ${JSON.stringify(found)}`);
    assert.ok(!found.includes("docs/"), `docs/ is committed, not a stray: ${JSON.stringify(found)}`);
  });
});

test("scanCheckout does not call a directory with staged content a stray", async () => {
  await withRepo(async (root) => {
    await mkdir(join(root, "src"));
    await writeFile(join(root, "src", "main.py"), "print(1)\n");
    await git(["add", "src/main.py"], root);
    await mkdir(join(root, "t_src"));
    const found = await scanCheckout(root);
    assert.ok(found.includes("t_src/"), "control: the empty stray is still found");
    assert.ok(!found.includes("src/"), "a directory holding staged files is known to git");
  });
});

test("scanCheckout finds an untracked file and an EMPTY untracked directory", async () => {
  await withRepo(async (root) => {
    // Presence of the clean result first: without it, the assertions below
    // could be satisfied by a scan that reports everything unconditionally.
    assert.deepEqual(await scanCheckout(root), [], "control: clean before littering");

    await writeFile(join(root, "probe.py"), "print('scratch')\n");
    await mkdir(join(root, "t_src"));

    const found = await scanCheckout(root);
    assert.ok(found.includes("probe.py"), `expected probe.py in ${JSON.stringify(found)}`);
    // The one git cannot see. Both real artifacts left in the copilot-tools
    // checkout by agent probe scripts were empty directories, and `git status`
    // called that tree completely clean with both of them present.
    assert.ok(found.includes("t_src/"), `expected t_src/ in ${JSON.stringify(found)}`);
  });
});

test("scanCheckout confirms git alone cannot see an empty directory", async () => {
  // The premise the empty-directory scan rests on, checked against git itself
  // rather than assumed. If a future git learns to report these, this test
  // fails and the extra scan can be deleted.
  await withRepo(async (root) => {
    await mkdir(join(root, "target"));
    const { stdout } = await git(["status", "--porcelain", "-uall", "-z"], root);
    assert.deepEqual(parseUntracked(stdout), [],
      "git reported an empty directory; the premise of emptyDirCandidates has changed");
    assert.ok((await scanCheckout(root)).includes("target/"),
      "but the guard must still see it");
  });
});

test("scanCheckout defers to the project's .gitignore, not its own opinions", async () => {
  await withRepo(async (root) => {
    await mkdir(join(root, "build"));
    await mkdir(join(root, "target"));
    // Presence first: both are reported while nothing is ignored, so the
    // narrower result below is the ignore rule working rather than the scan
    // failing.
    const before = await scanCheckout(root);
    assert.ok(before.includes("build/"));
    assert.ok(before.includes("target/"));

    await writeFile(join(root, ".gitignore"), "/build/\n");
    const after = await scanCheckout(root);
    assert.ok(!after.includes("build/"), "the project said build/ is ignored");
    assert.ok(after.includes("target/"),
      "and said nothing about target/, which a hardcoded ignore list would have swallowed");
  });
});

test("withoutIgnored refuses rather than calling an unanswerable question 'nothing is ignored'", async () => {
  await withRepo(async (root) => {
    await mkdir(join(root, "build"));
    await mkdir(join(root, "target"));
    await writeFile(join(root, ".gitignore"), "/build/\n");
    // Positive, through THE SAME call. Without it the refusal below is
    // satisfied by a `withoutIgnored` that can no longer answer anything.
    assert.deepEqual(await withoutIgnored(root, ["build", "target"]), ["target"],
      "control: it does still classify when git can answer");
    // And a genuine "none of these are ignored" is an answer, not a refusal.
    assert.deepEqual(await withoutIgnored(root, ["target"]), ["target"],
      "control: exit 1 means nothing matched, which is information");

    // A directory that is not a repository at all. check-ignore exits 128
    // here, and 128 and 1 arrive identically through `ok` and `stdout`: both
    // are ok:false with empty output. Only the exit code separates "nothing
    // is ignored" from "I could not look".
    const outside = dirname(root);
    assert.equal(await withoutIgnored(outside, ["build", "target"]), null,
      "an unanswerable check-ignore is not a verdict of 'nothing is ignored'");
  });
});

test("withoutIgnored's refusal is what keeps scanCheckout from inventing strays", async () => {
  // The consequence, at the seam where it would be paid. `null` reaches
  // `invisibleDirStrays`, which is the only consumer of check-ignore's answer.
  //
  // This is the destructive direction and it is the reason the branch exists.
  // Everywhere else in this toolkit an unreadable answer collapsing onto a
  // confident one causes something real to be SKIPPED. Here it collapses onto
  // "nothing is ignored", which fabricates work: every ignored build directory
  // in the project, reported as litter, on the working-tree path -- the report
  // that orders a deletion.
  await withRepo(async (root) => {
    await mkdir(join(root, "build"));
    await mkdir(join(root, "target"));
    const dirs = ["build", "target"];
    // Positive, through THE SAME call: it reports these when it has an answer.
    assert.deepEqual(invisibleDirStrays(dirs, [], root), ["build/", "target/"],
      "control: a real candidate list is still reported");
    assert.deepEqual(invisibleDirStrays(null, [], root), [],
      "no ignore information means no invisible-directory findings, not all of them");
  });
});

test("scanCheckout keeps reporting what git could see when check-ignore is the only casualty", async () => {
  // Refusing costs findings, so it has to cost as few as possible. `git
  // status` applies the ignore rules itself and has already spoken for every
  // path it can see; only the empty directories -- which git never reports --
  // depend on check-ignore. Dropping the whole scan would turn one blind spot
  // into total blindness.
  //
  // Both assertions go through `scanCheckout`. An earlier version put the
  // positive through `scanCheckout` and the negative through
  // `invisibleDirStrays`, which proves nothing about how `scanCheckout`
  // consumes the refusal -- it would have stayed green through any regression
  // in the line under test. Caught on review.
  await withRepo(async (root) => {
    await writeFile(join(root, "loose.py"), "x\n");
    await mkdir(join(root, "empty"));

    const whole = await scanCheckout(root);
    assert.ok(whole.includes("loose.py"), "control: the file half is reported");
    assert.ok(whole.includes("empty/"), "control: and so is the invisible half");

    const degraded = await scanCheckout(root, { ignoreFilter: async () => null });
    assert.ok(degraded.includes("loose.py"),
      "git status never consulted check-ignore, so its findings survive");
    assert.ok(!degraded.includes("empty/"),
      "and the half that did depend on it is dropped rather than fabricated");
  });
});

test("a refusing check-ignore costs findings and never invents them", async () => {
  // The failure mode being bought off. With no ignore information the
  // candidate list is every top-level directory in the project, and passing it
  // through unfiltered would report each one as litter for an agent to delete.
  await withRepo(async (root) => {
    for (const d of ["build", "target", "node_modules"]) await mkdir(join(root, d));
    await writeFile(join(root, ".gitignore"), "/build/\n/node_modules/\n");

    const normal = await scanCheckout(root);
    assert.ok(normal.includes("target/"), "control: a genuine stray is still found");
    assert.ok(!normal.includes("build/"), "control: and the ignore rule applies");

    const degraded = await scanCheckout(root, { ignoreFilter: async () => null });
    assert.deepEqual(degraded.filter((p) => p.endsWith("/")), [],
      "no directory findings at all -- not build/, and not target/ either");
  });
});

test("withoutIgnored gives the same verdict whichever way a directory is spelled", async () => {
  // LATENT, not live: today's only caller passes `topLevelDirs` output, which
  // is bare, so the rule works. This pins the refactor a peer flagged --
  // feeding `emptyDirCandidates` output, which IS slash-suffixed, back through
  // this filter. Measured on the pre-change code, that returned every name
  // unfiltered while check-ignore had just answered rc=0 naming two of them
  // ignored, because the slash was stripped from git's ANSWER and not from the
  // CANDIDATES.
  //
  // What makes it worth fixing ahead of a caller is that nothing about the
  // result looks wrong. The call succeeds, the list is plausible, and the
  // ignore rule is simply off.
  await withRepo(async (root) => {
    for (const d of ["__pycache__", "build", "straydir"]) await mkdir(join(root, d));
    await writeFile(join(root, ".gitignore"), "__pycache__/\n/build/\n");

    const bare = await withoutIgnored(root, ["__pycache__", "build", "straydir"]);
    assert.deepEqual(bare, ["straydir"], "control: the filter does filter");

    const slashed = await withoutIgnored(root, ["__pycache__/", "build/", "straydir/"]);
    assert.deepEqual(slashed, ["straydir/"], "the same question, spelled the other way");

    // The returned strings keep the caller's spelling. Only the comparison is
    // normalised -- "build/" and "build" mean different things downstream.
    assert.ok(slashed[0].endsWith("/"), "normalising the comparison must not rewrite the answer");
  });
});

test("the intrinsic exclusions hold however the name is spelled", () => {
  // Also latent, and the reachability matters: `.git` never gets this far
  // today, because `withoutIgnored` runs first and excludes it while the name
  // is still bare. So this is not a historical escape, it is the same
  // slashed-vs-bare mismatch pinned at the second of the two places it lives.
  //
  // Worth pinning anyway because the failure is silent and one-directional: a
  // Set lookup that misses always reports "not excluded", never the reverse.
  assert.deepEqual(emptyDirCandidates([".git", ".worktrees", "src"], []), ["src/"],
    "control: bare names are excluded");
  assert.deepEqual(emptyDirCandidates([".git/", ".worktrees/", "src/"], []), ["src/"],
    "and a trailing slash does not smuggle git's own storage through");
});

/**
 * Make `file` unreadable and return a function that undoes it, or null if this
 * platform/user cannot deny itself. Returned rather than thrown so the caller
 * can skip explicitly: a denial that silently did not take would leave the
 * test asserting against an ordinary readable file and passing for the wrong
 * reason.
 *
 * The undo is not tidiness. On Windows the deny ACE also blocks unlink, so
 * without it `withRepo`'s cleanup throws EPERM and the test fails after every
 * assertion in it has already passed -- a red that says nothing about the
 * behaviour under test.
 */
async function denyRead(file) {
  const { execFile } = await import("node:child_process");
  const { chmod, readFile } = await import("node:fs/promises");
  const run = (cmd, args) => new Promise((d) => execFile(cmd, args, () => d()));
  const win = process.platform === "win32";
  if (win) {
    await run("icacls", [file, "/deny", `${process.env.USERNAME}:(R)`]);
  } else {
    await chmod(file, 0o000);
  }
  const restore = async () => {
    if (win) await run("icacls", [file, "/remove:d", `${process.env.USERNAME}`]);
    else await chmod(file, 0o644);
  };
  try {
    await readFile(file);
    await restore();
    return null;
  } catch {
    return restore;
  }
}

test("an ignore file git cannot READ is not a project that ignores nothing", async (t) => {
  // The hole left by the exit-code fix, one layer down. check-ignore exits 1
  // both when nothing matched and when it could not read the ignore rules at
  // all, and `git status` still exits 0, so the repository looks healthy while
  // the rules are unknown. Trusting exit 1 there rebuilds the original bug:
  // unknown rules read as empty rules, and every top-level directory becomes
  // a candidate stray for an agent to delete.
  //
  // The only signal separating the two is git's own warning on stderr. That is
  // admissible precisely because it is git reporting its own failure -- stat-ing
  // the ignore files ourselves would be a second, weaker opinion on a question
  // git already answers, and could only subtract true positives.
  await withRepo(async (root) => {
    await mkdir(join(root, "build"));
    await writeFile(join(root, ".gitignore"), "/build/\n");

    // Positive, through THE SAME call, before the denial.
    assert.deepEqual(await withoutIgnored(root, ["build", "src"]), ["src"],
      "control: while the file is readable the rule applies");

    const restore = await denyRead(join(root, ".gitignore"));
    if (restore === null) {
      t.skip("this platform/user cannot deny itself read access");
      return;
    }
    try {
      // Premise: git is still healthy apart from the ignore file. Without this
      // the refusal below could be any failure at all.
      assert.ok((await git(["status", "--porcelain", "-uall", "-z"], root)).ok,
        "premise: the repository itself is fine");

      assert.equal(await withoutIgnored(root, ["build", "src"]), null,
        "unknown rules are not empty rules");
    } finally {
      await restore();
    }
  });
});

test("scanCheckout never reports git's own storage or a nested worktree root", async () => {
  await withRepo(async (root) => {
    await mkdir(join(root, ".worktrees"));
    await mkdir(join(root, "probe"));
    const found = await scanCheckout(root);
    assert.ok(found.includes("probe/"), "control: an ordinary new directory is still reported");
    assert.ok(!found.includes(".git/"));
    assert.ok(!found.includes(".worktrees/"));
  });
});

test("checkoutRoot resolves a real repository and returns null outside one", async () => {
  await withRepo(async (root) => {
    assert.equal(await checkoutRoot(root), root);
  });
  // Paired negative: a directory that is not a repository. The skip condition
  // is decided BEFORE the call and by different means -- walking the ancestor
  // chain for a .git -- so it can never be satisfied by the same bug it is
  // guarding. Folding the skip into the assertion (`if (r === null) assert...`)
  // is the vacuous shape this file's header forbids: a broken checkoutRoot
  // returning cwd would take the false branch and the test would pass having
  // asserted nothing. Three independent reviewers caught it here.
  const outside = await realpath(await mkdtemp(join(tmpdir(), "checkout-guard-bare-")));
  try {
    let insideARepo = false;
    for (let dir = outside; ; ) {
      if (existsSync(join(dir, ".git"))) { insideARepo = true; break; }
      const parent = dirname(dir);
      if (parent === dir) break;
      dir = parent;
    }
    const result = await checkoutRoot(outside);
    if (insideARepo) {
      assert.ok(result !== null, "the temp dir is inside a repo, so a root is the right answer");
    } else {
      assert.strictEqual(result, null, "no repository above the temp dir, so there is no root");
    }
  } finally {
    await rm(outside, { recursive: true, force: true, maxRetries: 3 });
  }
});

test("scanCheckout ignores a directory holding only ignored files", async () => {
  await withRepo(async (root) => {
    await writeFile(join(root, ".gitignore"), "*.tmp\n");
    await mkdir(join(root, "cache"));
    await mkdir(join(root, "probe"));
    await writeFile(join(root, "probe", "keep.py"), "x = 1\n");
    // Control first: with a real file in it, `cache/` is reported.
    await writeFile(join(root, "cache", "real.txt"), "content\n");
    const before = await scanCheckout(root);
    assert.ok(before.includes("cache/real.txt"), "control: a real file inside cache/ is seen");

    await rm(join(root, "cache", "real.txt"));
    await writeFile(join(root, "cache", "a.tmp"), "scratch\n");
    const after = await scanCheckout(root);
    assert.ok(!after.includes("cache/"),
      "git is silent about cache/ because its contents are ignored, not because it is empty");
    assert.ok(!after.some((p) => p.startsWith("cache/")), "and nothing inside it either");
    assert.ok(after.includes("probe/keep.py"),
      "control: an unignored file elsewhere is still reported, so the scan ran");
  });
});

test("scanCheckout still reports a directory git cannot represent at all", async () => {
  await withRepo(async (root) => {
    await mkdir(join(root, "t_src"));
    await mkdir(join(root, "nested", "deeper"), { recursive: true });
    const found = await scanCheckout(root);
    // These are the two real artifacts from the primary checkout: empty, so
    // `git status --porcelain` returns absolutely nothing for them.
    assert.ok(found.includes("t_src/"), "an empty directory is invisible to git and must be reported");
    assert.ok(found.includes("nested/"), "a directory holding only empty directories holds no files");
  });
});

test("end to end: a real stray blocks a real blanket add, and cleanup unblocks it", async () => {
  await withRepo(async (root) => {
    await mkdir(join(root, ".test_root_logic1"));
    await writeFile(join(root, "current_operator_mail.py"), "# scratch copy\n");

    // These two names are not invented. They are what an adversarial review
    // subagent left in a peer's checkout while reproducing a bug -- nine
    // directories from one review round.
    const strays = await scanCheckout(root);
    const decision = sweepDecision("git add -A && git commit -m wip", strays);
    assert.ok(decision, "a real checkout with real strays must block a blanket add");
    assert.deepEqual(decision.strays, [".test_root_logic1/", "current_operator_mail.py"]);

    // Naming one is allowed even while it is outstanding: that is the escape
    // hatch, and it must work against a real scan and not just a literal.
    assert.equal(sweepDecision("git add current_operator_mail.py", strays), null);

    await rm(join(root, ".test_root_logic1"), { recursive: true });
    await rm(join(root, "current_operator_mail.py"));
    assert.deepEqual(await scanCheckout(root), [], "cleanup must actually clear the strays");
    assert.equal(sweepDecision("git add -A", await scanCheckout(root)), null,
      "an agent that cleans up must not stay blocked");
  });
});

// --- regressions found by adversarial review ----------------------------
//
// Every test below corresponds to a bypass or false positive that three
// reviewer models found in code that already had 29 passing tests. The theme
// is uniform: the tokenizer was whitespace-only, so a quote turned a detected
// command into an undetected one, and a message argument turned an innocent
// command into a blocked one. Both directions are confident wrong answers.

test("tokenizeCommand strips quotes and keeps quoted whitespace together", () => {
  assert.deepEqual(tokenizeCommand('git add "-A"'), [["git", "add", "-A"]]);
  assert.deepEqual(tokenizeCommand("git add '*'"), [["git", "add", "*"]]);
  assert.deepEqual(
    tokenizeCommand('git stash push -m "wip fix -u handling"'),
    [["git", "stash", "push", "-m", "wip fix -u handling"]],
    "a quoted message is one token, so its contents are never read as flags",
  );
  assert.deepEqual(
    tokenizeCommand('git commit -m ""'),
    [["git", "commit", "-m", ""]],
    "a quoted empty string is still an argument and must not be dropped",
  );
  assert.deepEqual(
    tokenizeCommand('git add "-A'),
    [["git", "add", "-A"]],
    "an unterminated quote closes at end of input rather than losing the token",
  );
});

test("tokenizeCommand splits on operators but not on operators inside quotes", () => {
  assert.deepEqual(tokenizeCommand("git add -A && git commit"), [
    ["git", "add", "-A"],
    ["git", "commit"],
  ]);
  assert.deepEqual(tokenizeCommand("a | b ; c & d"), [["a"], ["b"], ["c"], ["d"]]);
  assert.deepEqual(
    tokenizeCommand('git commit -m "fix a && b"'),
    [["git", "commit", "-m", "fix a && b"]],
    "an operator inside a message must not split the command",
  );
});

test("tokenizeCommand leaves backslashes alone because Windows paths need them", () => {
  // Treating backslash as a POSIX escape would turn the git binary's own path
  // into `C:ProgramFilesGitcmdgit.exe` and defeat the detection entirely.
  assert.deepEqual(
    tokenizeCommand('"C:\\Program Files\\Git\\cmd\\git.exe" add -A'),
    [["C:\\Program Files\\Git\\cmd\\git.exe", "add", "-A"]],
  );
  assert.deepEqual(tokenizeCommand("git add .\\"), [["git", "add", ".\\"]]);
});

test("gitInvocations finds a quoted absolute Windows path to git.exe", () => {
  assert.deepEqual(
    gitInvocations('"C:\\Program Files\\Git\\cmd\\git.exe" add -A'),
    [["add", "-A"]],
  );
  assert.deepEqual(gitInvocations("'/usr/bin/git' add -A"), [["add", "-A"]]);
  assert.deepEqual(gitInvocations('"github-cli" pr list'), [],
    "control: a quoted binary that is not git is still not git");
});

test("a quoted flag or pathspec cannot smuggle a blanket add past the guard", () => {
  const strays = ["probe.py"];
  for (const command of [
    'git add "-A"',
    "git add '-A'",
    'git add "*"',
    "git add '.'",
    'git add ".\\"',
    '"C:\\Program Files\\Git\\cmd\\git.exe" add -A',
  ]) {
    assert.ok(sweepDecision(command, strays), `${command} must still be blocked`);
  }
  assert.equal(sweepDecision('git add "probe.py"', strays), null,
    "control: a quoted explicit pathspec is still the escape hatch and stays allowed");
});

test("git add -A with an explicit pathspec is scoped, so it is not blanket", () => {
  // Verified against real git: `git add -A keep.txt` stages keep.txt and
  // leaves an untracked stray exactly where it was. Blocking it punished the
  // most careful available form of the command.
  assert.equal(addIsBlanket(["-A", "keep.txt"]), false);
  assert.equal(addIsBlanket(["--all", "src/"]), false);
  assert.equal(addIsBlanket(["-A"]), true, "control: with no pathspec it really is blanket");
  assert.equal(addIsBlanket(["-A", "."]), true,
    "a root-wide pathspec sweeps whatever it is combined with");
  assert.equal(addIsBlanket(["-A", ".\\"]), true, "including the Windows spelling of it");
  assert.equal(addIsBlanket(["--", "-weird-name.txt"]), false,
    "after -- a dashed token is a filename, not a flag");
});

test("git add -A with a pathspec really does leave the stray untracked", async () => {
  await withRepo(async (root) => {
    await writeFile(join(root, "keep.txt"), "wanted\n");
    await writeFile(join(root, "probe.py"), "stray\n");
    await git(["add", "-A", "keep.txt"], root);
    const { stdout } = await git(["status", "--porcelain", "-uall", "-z"], root);
    assert.ok(parseUntracked(stdout).includes("probe.py"),
      "git itself scopes -A to the pathspec, which is why the guard must too");
    assert.ok(parseStatusPaths(stdout).includes("keep.txt"), "control: keep.txt was staged");
  });
});

test("a stash message mentioning -u does not block a stash that sweeps nothing", () => {
  const strays = ["probe.py"];
  assert.equal(sweepDecision('git stash push -m "wip fix -u handling in cli"', strays), null);
  assert.equal(sweepDecision('git stash push -m "note about -a flag"', strays), null);
  assert.equal(sweepDecision("git stash push -m tidy -u", strays) !== null, true,
    "control: a real -u outside the message still blocks");
});

test("git stash create and store do not sweep the working tree", () => {
  assert.equal(stashTakesUntracked(["create", "-u"]), false);
  assert.equal(stashTakesUntracked(["store", "-u", "deadbeef"]), false);
  assert.equal(stashTakesUntracked(["push", "-u"]), true, "control: push -u does sweep");
});

test("git stash create leaves untracked files in place", async () => {
  await withRepo(async (root) => {
    await writeFile(join(root, "tracked.txt"), "one\n");
    await git(["add", "tracked.txt"], root);
    await git(["commit", "-m", "base"], root);
    await writeFile(join(root, "tracked.txt"), "two\n");
    await writeFile(join(root, "probe.py"), "stray\n");
    await git(["stash", "create", "-u"], root);
    assert.ok(existsSync(join(root, "probe.py")),
      "create builds an object and prints its hash; the working tree is untouched");
  });
});

test("formatPathList bounds what is injected into the agent's context", () => {
  const many = Array.from({ length: 10_000 }, (_, i) => `artifact-${i}.tmp`);
  const text = formatPathList(many);
  assert.ok(text.length < 500, "10k paths must not become 10k lines of context");
  assert.ok(text.includes("... and 9990 more"));
  assert.equal(formatPathList(["a.py", "b.py"]).includes("more"), false,
    "control: a short list is shown in full with no truncation notice");
  // The count in the headline stays exact even when the list is cut, because
  // the number is the part that tells the agent something went badly wrong.
  assert.ok(blockReason({ verb: "git add", strays: many }).includes("10000 stray"));
  assert.ok(strayReport(many, "/tmp/scratch").includes("10000 new untracked"));
});

test("blockReason names artifacts the agent was never told about", () => {
  const text = blockReason({
    verb: "git add",
    strays: ["mine.py", "peers.py"],
    unannounced: ["peers.py"],
  });
  assert.ok(text.includes("first time"), "an unannounced stray must be flagged as such");
  assert.ok(text.includes("peer agent"), "and attributed honestly rather than blamed on the agent");
  const plain = blockReason({ verb: "git add", strays: ["mine.py"] });
  assert.equal(plain.includes("first time"), false,
    "control: when everything was already reported, no surprise notice appears");
});

test("holdsNoFiles distinguishes an empty tree from one with content", async () => {
  await withRepo(async (root) => {
    await mkdir(join(root, "hollow", "deeper"), { recursive: true });
    assert.equal(holdsNoFiles(join(root, "hollow")), true,
      "directories all the way down is what git cannot represent");
    await writeFile(join(root, "hollow", "deeper", "x.txt"), "content\n");
    assert.equal(holdsNoFiles(join(root, "hollow")), false,
      "control: one file at any depth makes it visible to git");
    assert.equal(holdsNoFiles(join(root, "does-not-exist")), false,
      "unreadable is not empty; the guard reports nothing rather than guessing");
  });
});

test("holdsNoFiles finds a file buried several levels down", async () => {
  await withRepo(async (root) => {
    // The walk must join each child against the directory it was just read
    // from. Rebuilding deep paths against the original root instead makes
    // every descendant path nonexistent, the readdir throws, and the function
    // returns false -- suppressing a real stray while looking like a careful
    // answer. Three levels is the shallowest depth that catches it.
    await mkdir(join(root, "deep", "a", "b", "c"), { recursive: true });
    assert.equal(holdsNoFiles(join(root, "deep")), true, "control: still no files anywhere");
    await writeFile(join(root, "deep", "a", "b", "c", "buried.txt"), "x\n");
    assert.equal(holdsNoFiles(join(root, "deep")), false,
      "a file four levels down must still be found");
  });
});

// --- regressions found by adversarial review, round 2 -------------------

test("shell grouping syntax cannot disguise a blanket add", () => {
  const strays = ["probe.py"];
  // `)` used to survive as an argument, which the pathspec-scoping rule then
  // read as a deliberately narrowed command and allowed straight through --
  // a bypass introduced by the round-1 fix for a different bypass.
  assert.deepEqual(tokenizeCommand("( git add -A )"), [["git", "add", "-A"]]);
  assert.ok(sweepDecision("( git add -A )", strays), "grouped blanket add must block");
  assert.ok(sweepDecision("{ git add -A ; }", strays), "brace grouping too");
  assert.ok(sweepDecision("$(git add -A)", strays),
    "command substitution containing a real git invocation must block");
  assert.equal(sweepDecision("( git add probe.py )", strays), null,
    "control: grouping does not turn a named pathspec into a blanket add");
});

test("git only counts in executable position", () => {
  const strays = ["probe.py"];
  assert.equal(sweepDecision("echo git add -A", strays), null,
    "a command that prints the words does not run them");
  assert.equal(sweepDecision('echo "run git add -A to stage"', strays), null);
  assert.deepEqual(gitInvocations("echo git add -A"), []);
  // Controls: the forms that really do put git in executable position.
  assert.deepEqual(gitInvocations("git add -A"), [["add", "-A"]]);
  assert.deepEqual(gitInvocations("FOO=1 BAR=2 git add -A"), [["add", "-A"]],
    "an environment assignment prefix keeps executable position");
  assert.deepEqual(gitInvocations("sudo git add -A"), [["add", "-A"]]);
  assert.deepEqual(gitInvocations("/usr/bin/env git add -A"), [["add", "-A"]]);
  for (const command of ["FOO=1 git add -A", "sudo git add -A", "nohup git add -A"]) {
    assert.ok(sweepDecision(command, strays), `${command} must still block`);
  }
});

test("a shell runner's -c string is inspected", () => {
  const strays = ["probe.py"];
  assert.deepEqual(gitInvocations('sh -c "git add -A"'), [["add", "-A"]]);
  assert.ok(sweepDecision('bash -c "git add -A"', strays));
  assert.ok(sweepDecision("sh -c 'git stash -u'", strays));
  assert.equal(sweepDecision('sh -c "git add probe.py"', strays), null,
    "control: the escape hatch still works through a shell runner");
  assert.equal(sweepDecision('sh -c "echo hello"', strays), null,
    "control: a -c string with no git in it is not blocked");
});

test("recursion into -c strings is depth-bounded and terminates", () => {
  // A pathological nest must not hang the hook that runs before every shell
  // command in the session.
  const nested = 'sh -c "sh -c \'sh -c "git add -A"\'"';
  assert.doesNotThrow(() => gitInvocations(nested));
  assert.ok(Array.isArray(gitInvocations(nested)));
});



// --- integration: linked worktrees ---------------------------------------
//
// The defect these cover was found in the field, not in a fixture. Three
// artifacts appeared in a repository's PRIMARY checkout while the agent whose
// subagents wrote them was working in `.worktrees/<branch>`. The guard was
// installed, it handles the `task` tool deliberately, and it said nothing --
// because `checkoutRoot` resolves `--show-toplevel`, which inside a linked
// worktree is the worktree. The population was one root; the phenomenon spans
// two; and "clean" was reported in the same words used when nothing happened.

/** A repository with one commit and a linked worktree at `.worktrees/feat`. */
async function withWorktree(body) {
  const base = await mkdtemp(join(tmpdir(), "checkout-guard-wt-"));
  try {
    const primary = await realpath(base);
    await git(["init", "-q", "-b", "main"], primary);
    await writeFile(join(primary, "README.md"), "# repo\n");
    await git(["add", "README.md"], primary);
    await git(
      ["-c", "user.email=test@example.invalid", "-c", "user.name=test",
       "commit", "-q", "-m", "initial"],
      primary,
    );
    const worktree = join(primary, ".worktrees", "feat");
    const { ok } = await git(["worktree", "add", "-q", worktree, "-b", "feat"], primary);
    assert.ok(ok, "premise: git could create the linked worktree");
    await body({ primary, worktree: await realpath(worktree) });
  } finally {
    await rm(base, { recursive: true, force: true, maxRetries: 3 });
  }
}

test("primaryCheckoutRoot answers the primary from inside a linked worktree", async () => {
  await withWorktree(async ({ primary, worktree }) => {
    // Premise first: --show-toplevel really does answer the worktree here. If
    // it answered the primary there would be no bug and this whole file of
    // assertions would be measuring nothing.
    assert.equal(await checkoutRoot(worktree), worktree,
      "premise: --show-toplevel resolves to the worktree, which is the bug");
    assert.equal(await primaryCheckoutRoot(worktree), primary);
    // And from the primary itself both resolvers agree, so a session that is
    // not in a worktree never watches the same tree twice.
    assert.equal(await primaryCheckoutRoot(primary), primary);
    assert.equal(await checkoutRoot(primary), primary);
  });
});

test("a stray in the primary is invisible to a worktree scan and visible to the primary scan", async () => {
  await withWorktree(async ({ primary, worktree }) => {
    // Control: the scan is working and finds a stray in the tree it is aimed
    // at. Without this, the `false` below is satisfied by a scan that is
    // simply broken.
    await writeFile(join(worktree, "in_worktree.py"), "scratch\n");
    const wtScan = await scanCheckoutTree(await checkoutRoot(worktree));
    assert.ok(wtScan.includes("in_worktree.py"),
      `control: the worktree scan finds its own stray: ${JSON.stringify(wtScan)}`);

    // The incident: a subagent writes into the primary while the agent is here.
    await writeFile(join(primary, "test_order.py"), "scratch\n");
    const blind = await scanCheckoutTree(await checkoutRoot(worktree));
    assert.ok(!blind.includes("test_order.py"),
      "the old single-root scan cannot see the primary -- this is the defect");
    // Deliberately the shipped call: root resolution AND the scan that the
    // extension actually invokes. Reaching for `scanCheckout` here would leave
    // this test green with the new scan function deleted, testing git's
    // behaviour rather than any code in this change.
    const watched = await scanCheckoutTree(await primaryCheckoutRoot(worktree));
    assert.ok(watched.includes("test_order.py"),
      `the primary scan sees it: ${JSON.stringify(watched)}`);
  });
});

test("the motivating case, end to end: an invisible empty directory in the primary reaches the session briefing", async () => {
  // Everything above tests a piece. This tests the sentence the feature
  // exists to produce, against a real git binary, with the exact fixture
  // that survived three sessions unreported: an agent in a worktree, an
  // empty directory in the primary, and a name that stats as a directory
  // while reading as a file.
  await withWorktree(async ({ primary, worktree }) => {
    await mkdir(join(primary, "archivedir3", "testfile.txt"), { recursive: true });
    await mkdir(join(primary, "no_read"), { recursive: true });

    // Premise, through the shipped call: git itself reports NEITHER planted
    // path, which is why nobody has ever been told about them. Without this
    // the assertions below are satisfied by a report about paths git would
    // have surfaced anyway. (Its output is not empty -- the fixture repo has
    // no `/.worktrees/` ignore rule, so git names the nested worktree, which
    // `scanCheckoutTree` filters out. That is a different population.)
    const status = await git(["status", "--porcelain", "-uall"], primary);
    assert.ok(!status.stdout.includes("archivedir3"),
      `premise: git cannot see the empty tree -- got ${JSON.stringify(status.stdout)}`);
    assert.ok(!status.stdout.includes("no_read"),
      `premise: git cannot see the empty directory -- got ${JSON.stringify(status.stdout)}`);

    // Exactly what onSessionStart does: seed the working root, then the
    // primary, and hand both arrays to the briefing.
    const root = await checkoutRoot(worktree);
    const other = rootToWatch(root, await primaryCheckoutRoot(worktree)).watch;
    assert.ok(other && other !== root, `premise: the primary is a second tree: ${other}`);
    const context = sessionContext("/tmp/s", [
      { strays: await scanCheckoutTree(root), root },
      { strays: await scanCheckoutTree(other), root: other, primary: true },
    ]);

    assert.match(context, /archivedir3\//, context);
    assert.match(context, /no_read\//, context);
    // Attributed to the primary report specifically, not merely present
    // somewhere in a string that concatenates several reports.
    const primaryReport = context
      .split("\n\n")
      .find((part) => part.startsWith("[checkout-guard] INHERITED:") &&
        part.includes("PRIMARY checkout"));
    assert.ok(primaryReport, context);
    assert.match(primaryReport, /archivedir3\//, primaryReport);

    // Control through the same call: with the primary clean, the same
    // pipeline says nothing, so the report above is caused by the artifacts
    // and not by the pipeline reporting unconditionally.
    await rm(join(primary, "archivedir3"), { recursive: true, force: true });
    await rm(join(primary, "no_read"), { recursive: true, force: true });
    const clean = sessionContext("/tmp/s", [
      { strays: await scanCheckoutTree(root), root },
      { strays: await scanCheckoutTree(other), root: other, primary: true },
    ]);
    assert.equal(clean, sessionBriefing("/tmp/s"), clean);
  });
});

test("watching the primary does not report the worktrees directory itself", async () => {
  // Without this the fix is unusable: every session that creates a worktree
  // would be told its own worktree is a stray artifact in the primary, on
  // every command, for ever. A guard that cries wolf gets switched off.
  //
  // This was caught by the test failing, not by review. INTRINSIC_EXCLUSIONS
  // looks like it already covers `.worktrees`, and it does not cover git's
  // untracked records at all -- which is invisible in this repository because
  // its .gitignore lists `/.worktrees/`, so git never mentions it here.
  await withWorktree(async ({ primary }) => {
    // Premise: the plain scan really does report the nested worktree, so the
    // filtered result below is an exclusion working and not a scan seeing
    // nothing. If this ever stops being true the test still holds, but it is
    // no longer testing what it was written for.
    const raw = await scanCheckout(primary);
    assert.ok(raw.some((p) => p.startsWith(".worktrees")),
      `premise: git reports a nested worktree as untracked: ${JSON.stringify(raw)}`);

    const found = await scanCheckoutTree(primary);
    assert.ok(!found.some((p) => p.startsWith(".worktrees")),
      `.worktrees is a checkout, not repository content: ${JSON.stringify(found)}`);
    // Control: this scan does find real strays in the same tree, so the clean
    // result above is an exclusion working rather than a scan seeing nothing.
    await writeFile(join(primary, "probe.py"), "scratch\n");
    assert.ok((await scanCheckoutTree(primary)).includes("probe.py"),
      "control: the primary scan is not simply blind");
  });
});

test("the worktree exclusion is derived from git, not from the .worktrees name", async () => {
  // A worktree placed somewhere else is still a checkout. Keying the exclusion
  // off the directory name would work in this repository and quietly fail in
  // any project that puts its worktrees elsewhere -- a selector that admits
  // only the cases already known about.
  await withWorktree(async ({ primary }) => {
    const odd = join(primary, "scratch-checkout");
    const { ok } = await git(["worktree", "add", "-q", odd, "-b", "other"], primary);
    assert.ok(ok, "premise: git could create a worktree outside .worktrees/");

    const prefixes = await nestedWorktreePrefixes(primary);
    assert.ok(prefixes.includes("scratch-checkout/"), JSON.stringify(prefixes));
    assert.ok(prefixes.includes(".worktrees/feat/"), JSON.stringify(prefixes));
    assert.ok(!prefixes.some((p) => p === "/" || p === ""), "the primary is not its own nested worktree");

    const found = await scanCheckoutTree(primary);
    assert.ok(!found.some((p) => p.startsWith("scratch-checkout")),
      `a worktree is a checkout wherever it lives: ${JSON.stringify(found)}`);
    // Control, through the SAME call. Without it this test passes when
    // `scanCheckoutTree` returns [] for any reason at all -- the positives
    // above are on `nestedWorktreePrefixes`, a different function, and cannot
    // establish that the scan being asserted about ran.
    await writeFile(join(primary, "probe.py"), "scratch\n");
    assert.ok((await scanCheckoutTree(primary)).includes("probe.py"),
      "control: this scan does report a real stray in this same tree");
  });
});

test("primaryCheckoutRoot returns null for a bare main worktree", async () => {
  // There is no working tree to leave anything in, and reporting on one would
  // mean reporting on the contents of .git.
  //
  // The env override is load-bearing, and this test did not have it at first:
  // a machine configured with `safe.bareRepository = explicit` makes git refuse
  // the command outright, so the function returned null down the "git failed"
  // path and the assertion below passed without ever reaching the `bare`
  // branch. Deleting that branch left the suite green. The premise assertions
  // exist so that can never silently happen again.
  const base = await mkdtemp(join(tmpdir(), "checkout-guard-bare-"));
  const saved = {
    count: process.env.GIT_CONFIG_COUNT,
    key: process.env.GIT_CONFIG_KEY_0,
    value: process.env.GIT_CONFIG_VALUE_0,
  };
  try {
    const root = await realpath(base);
    await git(["init", "-q", "--bare", "-b", "main"], root);
    process.env.GIT_CONFIG_COUNT = "1";
    process.env.GIT_CONFIG_KEY_0 = "safe.bareRepository";
    process.env.GIT_CONFIG_VALUE_0 = "all";

    const probe = await git(["worktree", "list", "--porcelain"], root);
    assert.ok(probe.ok, "premise: git must ANSWER, or null proves nothing");
    assert.match(probe.stdout, /^bare$/m, "premise: git reports the main worktree as bare");
    assert.match(probe.stdout, /^worktree /m, "premise: there is a path to be tempted by");

    assert.equal(await primaryCheckoutRoot(root), null);
  } finally {
    for (const [name, key] of [["GIT_CONFIG_COUNT", "count"], ["GIT_CONFIG_KEY_0", "key"], ["GIT_CONFIG_VALUE_0", "value"]]) {
      if (saved[key] === undefined) delete process.env[name];
      else process.env[name] = saved[key];
    }
    await rm(base, { recursive: true, force: true, maxRetries: 3 });
  }
});

test("an unanswered worktree list is not an answer of 'no worktrees'", () => {
  // The propagation, which the two halves being individually correct does not
  // establish: a null from either question has to arrive at the caller as
  // null. `found` returned here would name the agent's own worktree as a stray
  // in the primary, on every command after one transient git failure.
  assert.equal(withoutNestedWorktrees(["a.py", ".worktrees/x/"], null), null,
    "no list of worktrees: the honest result is 'I do not know'");
  assert.equal(withoutNestedWorktrees(null, [".worktrees/x/"]), null,
    "no scan: nothing to filter and nothing to claim");
  // Controls, so the assertions above are about null and not about the filter
  // being broken in general.
  assert.deepEqual(withoutNestedWorktrees(["a.py", ".worktrees/x/"], [".worktrees/x/"]),
    ["a.py"], "a real list really does filter");
  assert.deepEqual(withoutNestedWorktrees(["a.py"], []), ["a.py"],
    "a real answer of 'no nested worktrees' passes everything through");
});

test("primaryCheckoutRoot reports a failed lookup as UNKNOWN_ROOT, not as null", async () => {
  const base = await mkdtemp(join(tmpdir(), "checkout-guard-norepo-"));
  try {
    const root = await realpath(base);
    // Premise: git must actually FAIL here, or this is testing the parser
    // rather than the failure path it claims to cover.
    const probe = await git(["worktree", "list", "--porcelain"], root);
    assert.equal(probe.ok, false, "premise: git refuses outside a repository");
    assert.equal(await primaryCheckoutRoot(root), UNKNOWN_ROOT);
  } finally {
    await rm(base, { recursive: true, force: true, maxRetries: 3 });
  }
});

test("a failed lookup is never cached, a real answer always is", () => {
  // The distinction this encodes: `null` is a claim about the repository and
  // UNKNOWN_ROOT is a claim about the attempt. The session caches this once,
  // so remembering an answer derived from one timed-out `git worktree list`
  // would disable primary-root watching for the rest of the session -- and the
  // result would be indistinguishable from a session with nothing to watch.
  assert.deepEqual(rootToWatch("/repo/.worktrees/x", UNKNOWN_ROOT),
    { watch: null, cache: false }, "a failure must be retried, not remembered");
  // Controls: every answer that is a real answer IS remembered, so the
  // assertion above is about failure and not about caching being broken.
  assert.deepEqual(rootToWatch("/repo/.worktrees/x", "/repo"),
    { watch: "/repo", cache: true });
  assert.deepEqual(rootToWatch("/repo", "/repo"),
    { watch: null, cache: true }, "already in the primary: nothing else to watch");
  assert.deepEqual(rootToWatch("/repo", null),
    { watch: null, cache: true }, "bare: a real answer that there is no working tree");
});

test("a failed worktree lookup does not become an empty list of worktrees", async () => {
  // The census bug: `[]` from a failed lookup reads as "there are no nested
  // worktrees", and every worktree in the tree is then reported as a stray.
  const base = await mkdtemp(join(tmpdir(), "checkout-guard-norepo2-"));
  try {
    const root = await realpath(base);
    const probe = await git(["worktree", "list", "--porcelain"], root);
    assert.equal(probe.ok, false, "premise: the lookup really does fail here");
    assert.equal(await nestedWorktreePrefixes(root), null,
      "null means 'could not find out', which is not the same as 'none'");
  } finally {
    await rm(base, { recursive: true, force: true, maxRetries: 3 });
  }
});

test("the worktree exclusion applies to the tree the agent is working in too", async () => {
  // Not just to the primary. An agent working in the primary would otherwise
  // have its own `git add -A` denied because a peer created a worktree beside
  // it -- the same false positive, with a blocking consequence instead of an
  // advisory one.
  await withWorktree(async ({ primary }) => {
    const odd = join(primary, "peer-checkout");
    const { ok } = await git(["worktree", "add", "-q", odd, "-b", "peer"], primary);
    assert.ok(ok, "premise: the peer worktree was created");
    const raw = await scanCheckout(primary);
    assert.ok(raw.some((p) => p.startsWith("peer-checkout")),
      `premise: the raw scan does report it: ${JSON.stringify(raw)}`);
    const found = await scanCheckoutTree(primary);
    assert.ok(!found.some((p) => p.startsWith("peer-checkout")),
      `the scan the extension uses excludes it: ${JSON.stringify(found)}`);
    // Control, through the SAME call. The positive above is on `scanCheckout`;
    // an assertion about what `scanCheckoutTree` omits proves nothing until
    // something establishes that `scanCheckoutTree` reports anything.
    await writeFile(join(primary, "probe.py"), "scratch\n");
    assert.ok((await scanCheckoutTree(primary)).includes("probe.py"),
      "control: the filtered scan is not simply blind");
  });
});

// ---------------------------------------------------------------------------
// Per-session tracking state.
//
// All of this lived in extension.mjs, where `joinSession` runs at import and
// no test could reach it. Everything below was covered by `node --check` and
// nothing else until it moved.
// ---------------------------------------------------------------------------

/** A `scanCheckoutTree` double: yields each listing in turn, then repeats. */
function scanner(...listings) {
  let i = 0;
  return async () => listings[Math.min(i++, listings.length - 1)];
}

test("createGuardState hands out fresh state, never a shared module binding", () => {
  const a = createGuardState();
  const b = createGuardState();
  a.lastSeen.set("/repo", ["probe.py"]);
  setFor(a.outstanding, "/repo").add("probe.py");
  setFor(a.authored, "/repo").add("src.js");
  a.primaryRoots.set("/repo", null);
  // The trap the factory exists to avoid. As module-level Maps these would be
  // shared by every importer for the life of the process, so each test in this
  // file would inherit the previous one's state -- and the failures that
  // produces are order-dependent and intermittent, the most expensive shape a
  // test failure has.
  assert.equal(b.lastSeen.size, 0);
  assert.equal(b.outstanding.size, 0);
  assert.equal(b.authored.size, 0);
  assert.equal(b.primaryRoots.size, 0);
  // Control: the writes above really did land somewhere, so the emptiness of
  // `b` is isolation and not four writes that were no-ops.
  assert.deepEqual(a.lastSeen.get("/repo"), ["probe.py"]);
  assert.deepEqual([...setFor(a.outstanding, "/repo")], ["probe.py"]);
  assert.deepEqual([...setFor(a.authored, "/repo")], ["src.js"]);
  assert.equal(a.primaryRoots.get("/repo"), null);
});

test("setFor creates one set per root and keeps returning it", () => {
  const map = new Map();
  const first = setFor(map, "/repo");
  first.add("a.py");
  assert.equal(setFor(map, "/repo"), first, "the same root gets the same set, not a fresh one");
  assert.deepEqual([...setFor(map, "/repo")], ["a.py"]);
  // Control: two roots do not share a set, which is what makes the identity
  // above a statement about keying rather than about there being only one set.
  assert.notEqual(setFor(map, "/other"), first);
  assert.deepEqual([...setFor(map, "/other")], []);
});

test("bound drops the oldest entries and leaves a set within the limit alone", () => {
  const over = new Set(["a", "b", "c", "d"]);
  bound(over, 2);
  assert.deepEqual([...over], ["c", "d"], "insertion order decides: the oldest go first");
  // Control: a set already within the limit is untouched, so the trimming
  // above is the limit acting rather than the function always deleting.
  const under = new Set(["x", "y"]);
  bound(under, 2);
  assert.deepEqual([...under], ["x", "y"]);
  // And the default is the limit the extension actually runs with.
  const many = new Set(Array.from({ length: MAX_TRACKED + 5 }, (_, i) => `p${i}`));
  bound(many);
  assert.equal(many.size, MAX_TRACKED);
  assert.ok(!many.has("p0"), "the oldest is gone");
  assert.ok(many.has(`p${MAX_TRACKED + 4}`), "the newest is kept");
});

test("relativeToCheckout reports checkout-relative posix paths, and null outside", () => {
  const root = join(tmpdir(), "cg-rel-root");
  assert.equal(relativeToCheckout(root, join(root, "sub", "probe.py")), "sub/probe.py",
    "posix separators, because that is the form git reports and the sets hold");
  assert.equal(relativeToCheckout(root, join("sub", "probe.py"), root), "sub/probe.py",
    "a relative path resolves against the cwd it is given");
  // Paired negatives. Both must be null rather than a `../` path: a file
  // outside the checkout is not this checkout's artifact, and folding one in
  // would record an authored path that no scan of this root can ever match.
  assert.equal(relativeToCheckout(root, join(tmpdir(), "elsewhere", "probe.py")), null);
  assert.equal(relativeToCheckout(root, root), null, "the root is not a path inside itself");
});

test("only an exact 1 disables the guard", () => {
  assert.equal(guardDisabled({ COPILOT_CHECKOUT_GUARD_DISABLE: "1" }), true);
  // The values someone types meaning "leave it on". A truthiness test would
  // disable the guard on the first two -- in the one case where the operator
  // believed it was running.
  assert.equal(guardDisabled({ COPILOT_CHECKOUT_GUARD_DISABLE: "0" }), false);
  assert.equal(guardDisabled({ COPILOT_CHECKOUT_GUARD_DISABLE: "false" }), false);
  assert.equal(guardDisabled({}), false);
});

test("scratchDirFor names a per-process directory under the temp dir", () => {
  assert.equal(scratchDirFor(4242), join(tmpdir(), "copilot-scratch", "session-4242"));
  // Control: it is per process, so two sessions on one machine are never
  // handed the same scratch directory to write into.
  assert.notEqual(scratchDirFor(4243), scratchDirFor(4242));
  assert.ok(scratchDirFor().endsWith(`session-${process.pid}`), "defaults to this process");
});

test("the tool sets keep shell, subagent and authoring tools apart", () => {
  for (const name of ["bash", "powershell", "shell"]) {
    assert.ok(SHELL_TOOLS.has(name), `${name} runs arbitrary commands`);
  }
  assert.ok(SUBAGENT_TOOLS.has("task"),
    "the only call at which a subagent's writes can still be attributed");
  for (const name of ["create", "edit"]) assert.ok(AUTHORING_TOOLS.has(name));
  // The separations, each of which changes what a hook does. `task` in
  // SHELL_TOOLS would have the pre-hook read a subagent PROMPT as a git
  // command line; an authoring tool in either set would have the agent's own
  // deliberate writes rescanned and reported back to it as strays.
  assert.ok(!SHELL_TOOLS.has("task"));
  assert.ok(!SHELL_TOOLS.has("edit") && !SUBAGENT_TOOLS.has("edit"));
  assert.ok(!AUTHORING_TOOLS.has("bash"));
});

test("observe establishes a baseline before it calls anything new", async () => {
  const state = createGuardState();
  const scan = scanner(["old.py"], ["old.py", "probe.py"]);
  assert.deepEqual(await observe(state, "/repo", { scan }), [],
    "first sight: reporting here would blame this agent for every earlier artifact");
  // Control through the SAME call: the mechanism can fire at all, so the empty
  // result above is the baseline rule and not a scan double that never works.
  assert.deepEqual(await observe(state, "/repo", { scan }), ["probe.py"]);
});

test("a scan that failed is not a checkout that is clean", async () => {
  const state = createGuardState();
  const scan = scanner(["old.py"], null, ["old.py", "probe.py"]);
  await observe(state, "/repo", { scan });
  assert.deepEqual(await observe(state, "/repo", { scan }), [],
    "a failed scan reports nothing, because it measured nothing");
  // And it must not have overwritten the baseline with that nothing, or the
  // pre-existing file returns as this agent's new artifact on the next command.
  assert.deepEqual(await observe(state, "/repo", { scan }), ["probe.py"],
    "the surviving baseline still knows old.py was already there");
});

test("observe never reports a path the agent authored deliberately", async () => {
  const state = createGuardState();
  const scan = scanner(["old.py"], ["old.py", "written.py", "probe.py"]);
  await observe(state, "/repo", { scan });
  setFor(state.authored, "/repo").add("written.py");
  // Paired inside one call: both paths are new by the same measurement, and
  // only the authored one is dropped.
  assert.deepEqual(await observe(state, "/repo", { scan }), ["probe.py"]);
});

test("only the checkout the agent works in accumulates blocking artifacts", async () => {
  const state = createGuardState();
  const here = scanner(["a"], ["a", "mine.py"]);
  await observe(state, "/repo", { scan: here });
  assert.deepEqual(await observe(state, "/repo", { scan: here }), ["mine.py"]);
  assert.deepEqual([...setFor(state.outstanding, "/repo")], ["mine.py"],
    "an artifact here can deny this agent's own blanket stage");

  const there = scanner(["b"], ["b", "peer.py"]);
  await observe(state, "/primary", { blocking: false, scan: there });
  assert.deepEqual(await observe(state, "/primary", { blocking: false, scan: there }),
    ["peer.py"], "still reported: it is worth telling the agent about");
  assert.deepEqual([...setFor(state.outstanding, "/primary")], [],
    "but it cannot block -- refusing this agent's commit over a peer's artifact "
    + "lands the cost on the wrong asset");
});

test("an artifact that was cleaned up stops blocking", async () => {
  const state = createGuardState();
  const scan = scanner(["a"], ["a", "probe.py"], ["a"]);
  await observe(state, "/repo", { scan });
  await observe(state, "/repo", { scan });
  assert.deepEqual([...setFor(state.outstanding, "/repo")], ["probe.py"],
    "premise: it is outstanding, or the next assertion proves nothing");
  await observe(state, "/repo", { scan });
  assert.deepEqual([...setFor(state.outstanding, "/repo")], [],
    "an agent that cleans up is not then blocked by the memory of a deleted file");
});

test("the outstanding set stays bounded however long an agent ignores it", async () => {
  const state = createGuardState();
  const many = Array.from({ length: MAX_TRACKED + 10 }, (_, i) => `p${i}.py`);
  const scan = scanner([], many);
  await observe(state, "/repo", { scan });
  const fresh = await observe(state, "/repo", { scan });
  assert.equal(fresh.length, many.length, "premise: every one of them really is new");
  assert.equal(setFor(state.outstanding, "/repo").size, MAX_TRACKED,
    "the report is complete; only the memory of it is capped");
});

test("noteAuthored folds a deliberate write into the baseline and clears the block", async () => {
  const state = createGuardState();
  const root = join(tmpdir(), "cg-authored-root");
  const scan = scanner(["a"], ["a", "written.py", "unrelated.py"]);
  await observe(state, root, { scan });
  setFor(state.outstanding, root).add("written.py");

  assert.equal(noteAuthored(state, root, join(root, "written.py")), "written.py");
  assert.deepEqual([...setFor(state.outstanding, root)], [],
    "a path the agent adopted with create/edit no longer denies its own stage");
  assert.ok(state.lastSeen.get(root).includes("written.py"),
    "folded into the baseline rather than rescanned: a five-file edit would "
    + "otherwise fire five concurrent whole-tree traversals");
  // The suppression and a positive control THROUGH THE SAME CALL. Both paths
  // are new by the same measurement; asserting only that the authored one is
  // absent would pass just as happily against an `observe` that had stopped
  // reporting anything at all.
  assert.deepEqual(await observe(state, root, { scan }), ["unrelated.py"],
    "the next observation does not hand the agent back its own writing, and is "
    + "not simply blind: a real stray in the same call is still reported");
});

test("noteAuthored ignores a write outside the checkout", () => {
  const state = createGuardState();
  const root = join(tmpdir(), "cg-authored-root2");
  assert.equal(noteAuthored(state, root, join(tmpdir(), "elsewhere.py")), null);
  assert.equal(state.authored.size, 0, "nothing recorded against a checkout it is not in");
  // Control: the same call with a path inside the checkout does record, so the
  // null above is the boundary check and not the function doing nothing at all.
  assert.equal(noteAuthored(state, root, join(root, "inside.py")), "inside.py");
  assert.deepEqual([...setFor(state.authored, root)], ["inside.py"]);
});

test("otherRootToWatch remembers a real answer and retries a failed lookup", async () => {
  const state = createGuardState();
  const worktree = "/repo/.worktrees/x";
  const failed = [];
  const failing = async (arg) => { failed.push(arg); return UNKNOWN_ROOT; };
  assert.equal(await otherRootToWatch(state, worktree, { lookup: failing }), null);
  assert.equal(await otherRootToWatch(state, worktree, { lookup: failing }), null);
  assert.equal(failed.length, 2,
    "a failure is an unanswered question: caching it blinds the whole session, "
    + "and the result looks exactly like a session with nothing to watch");
  assert.deepEqual(failed, [worktree, worktree],
    "asked about the root being watched, never about the process cwd");

  const answered = [];
  const answering = async (arg) => { answered.push(arg); return "/repo"; };
  assert.equal(await otherRootToWatch(state, worktree, { lookup: answering }), "/repo");
  assert.equal(await otherRootToWatch(state, worktree, { lookup: answering }), "/repo");
  assert.equal(answered.length, 1,
    "control: a real answer IS remembered, so the retry above is about failure "
    + "and not about caching being broken");
});

test("an agent already in the primary has no second checkout to watch", async () => {
  const state = createGuardState();
  let calls = 0;
  const lookup = async () => { calls++; return "/repo"; };
  assert.equal(await otherRootToWatch(state, "/repo", { lookup }), null,
    "the two roots are never scanned as if they were separate places");
  assert.equal(await otherRootToWatch(state, "/repo", { lookup }), null);
  assert.equal(calls, 1,
    "and that negative answer is cached -- it cannot change within a session, "
    + "and an agent in the primary would otherwise pay for the lookup forever");
  // Control: the cache is keyed by root, so a different root is asked afresh.
  assert.equal(await otherRootToWatch(state, "/repo/.worktrees/x", { lookup }), "/repo");
  assert.equal(calls, 2);
});

test("every name extension.mjs imports from guard.mjs is really exported", async () => {
  // The one property of extension.mjs reachable from here. A named ESM import
  // that does not resolve is a link error raised when the module is FIRST
  // imported -- which for extension.mjs is inside a live Copilot session, at
  // which point the failure is a `Failed to load extension` line in a log
  // nobody is reading and a session that silently has no guard. `node --check`
  // does not catch it: it parses one file and resolves nothing.
  const source = await readFile(join(dirname(fileURLToPath(import.meta.url)), "extension.mjs"), "utf8");
  const block = source.match(/import\s*\{([^}]*)\}\s*from\s*"\.\/guard\.mjs"/);
  assert.ok(block, "premise: extension.mjs still imports from guard.mjs by name");
  const imported = block[1]
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean);
  // Premise, because a regex that matched an empty list would satisfy every
  // assertion below without checking anything.
  assert.ok(imported.length > 5, `premise: names were extracted: ${imported.length}`);
  assert.ok(imported.includes("observe"), "premise: the extracted list is the real one");

  const exported = Object.keys(await import("./guard.mjs"));
  const missing = imported.filter((name) => !exported.includes(name));
  assert.deepEqual(missing, [], `extension.mjs imports names guard.mjs does not export`);
  // Control: the comparison can fail at all. Without this, an `exported` list
  // that somehow held everything -- or a `missing` computed from an empty
  // `imported` -- passes identically.
  assert.ok(!exported.includes("noSuchExport"),
    "control: the export list is a real list, not a set that answers yes");
});


