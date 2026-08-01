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
import { mkdir, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import {
  addIsBlanket,
  blockReason,
  checkoutRoot,
  emptyDirCandidates,
  formatPathList,
  git,
  gitInvocations,
  gitSubcommand,
  holdsNoFiles,
  newEntries,
  parseStatusPaths,
  parseUntracked,
  nestedWorktreePrefixes,
  primaryCheckoutRoot,
  rootToWatch,
  UNKNOWN_ROOT,
  primaryStrayReport,
  scanCheckout,
  scanCheckoutTree,
  withoutNestedWorktrees,
  sessionBriefing,
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


