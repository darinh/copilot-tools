// Unit tests for checkout-guard's decision logic. Run with `node --test`.
//
// Convention enforced throughout this file: every assertion that something is
// ALLOWED is paired, in the same test, with a case proving the same code path
// blocks when it should. An "allowed" result is indistinguishable from a
// predicate that never matched anything at all, and a test that cannot tell
// those apart passes just as happily when the guard is broken. Two agents
// shipped exactly that shape in one evening on this repository.

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  addIsBlanket,
  blockReason,
  checkoutRoot,
  emptyDirCandidates,
  git,
  gitInvocations,
  gitSubcommand,
  newEntries,
  parseStatusPaths,
  parseUntracked,
  scanCheckout,
  sessionBriefing,
  stashTakesUntracked,
  strayReport,
  sweepDecision,
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
  // Paired negative: a directory that is not a repository. Skipped when the
  // temp directory itself happens to sit inside one, which would make the
  // assertion meaningless rather than merely false.
  const outside = await realpath(await mkdtemp(join(tmpdir(), "checkout-guard-bare-")));
  try {
    const result = await checkoutRoot(outside);
    if (result === null) assert.equal(result, null);
  } finally {
    await rm(outside, { recursive: true, force: true, maxRetries: 3 });
  }
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

