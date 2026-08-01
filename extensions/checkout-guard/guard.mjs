// checkout-guard — decision logic and checkout inspection.
//
// Kept separate from extension.mjs so it can be exercised with `node --test`
// without a live Copilot session. extension.mjs is then nothing but hook
// wiring and the SDK import, which is the only part that cannot be tested
// here -- deliberately, because a guard whose real git and filesystem work
// lives behind an untestable import is verified only by assertion.

import { execFile } from "node:child_process";
import { readdirSync } from "node:fs";
import { resolve } from "node:path";

// The only directories excluded on this extension's own authority. Everything
// else that should be ignored is decided by asking git (`check-ignore`), so
// the guard agrees with the project's .gitignore instead of carrying its own
// opinion about what counts as noise.
//
// A hardcoded list was tried first and was wrong immediately: it held
// "target", "build" and "dist" as conventional build output, and `target/` is
// one of two real stray artifacts sitting in this very repository -- untracked,
// unignored, and created by an agent's probe script. A guard that disagrees
// with the project about what is noise does so silently, and always in the
// direction of missing things.
//
// `.git` is here because git never reports its own storage as ignored, and
// `.worktrees` because the repository convention puts developer checkouts
// there; both are checkouts or plumbing, never repository content.
export const INTRINSIC_EXCLUSIONS = new Set([".git", ".worktrees"]);

/**
 * Parse `git status --porcelain -uall -z` output into untracked paths.
 *
 * The `-z` form is used rather than the default because it is the only one
 * that is unambiguous: without it git applies `core.quotePath`, wrapping any
 * path containing a space, a quote, a newline or a non-ASCII byte in double
 * quotes with C-style escapes. A parser that splits on newlines and strips
 * quotes gets those paths wrong, and "wrong" here means either missing a real
 * artifact or inventing one -- both of which cost more than they save.
 *
 * Records are NUL-terminated and each begins with a two-character status field
 * followed by a space. Only `??` (untracked) is of interest. Rename records
 * carry a second NUL-terminated path, but a rename cannot be untracked, so
 * `??` records are always single-path and no lookahead is needed.
 */
export function parseUntracked(stdout) {
  const out = [];
  for (const record of String(stdout ?? "").split("\0")) {
    if (record.length < 4) continue;
    if (record.slice(0, 3) !== "?? ") continue;
    out.push(record.slice(3));
  }
  return out;
}

/**
 * Every path `git status` mentioned, whatever its status code.
 *
 * Used to decide which directories git already knows about. Untracked paths
 * alone are not enough: a directory holding staged or modified files is
 * perfectly well known to git and must never be called a stray.
 *
 * Rename and copy records are the one shape needing lookahead -- git emits the
 * destination path, then the source path as a separate NUL-terminated field.
 * Both are consumed, because both name a real path.
 */
export function parseStatusPaths(stdout) {
  const records = String(stdout ?? "").split("\0");
  const out = [];
  for (let i = 0; i < records.length; i++) {
    const record = records[i];
    if (record.length < 4 || record[2] !== " ") continue;
    out.push(record.slice(3));
    if (record[0] === "R" || record[0] === "C") {
      const source = records[++i];
      if (source) out.push(source);
    }
  }
  return out;
}

/** Members of `after` absent from `before`, sorted for a stable report. */
export function newEntries(before, after) {
  const seen = new Set(before);
  return [...new Set(after)].filter((entry) => !seen.has(entry)).sort();
}

/**
 * Directories that git cannot see into, because git does not track empty
 * directories at all.
 *
 * This is not a refinement, it is the majority of the observed evidence: the
 * two artifacts left in this repository by agent probe scripts are both empty
 * directories, and `git status` reports the tree as completely clean with both
 * of them present. A guard built only on `git status` would call that checkout
 * pristine.
 *
 * `known` must list everything git has any knowledge of -- committed entries
 * as well as every path `git status` mentioned. Passing only the untracked
 * paths was tried, and against a real repository it reported `docs/`,
 * `tests/`, `templates/` and every other clean tracked directory as a stray:
 * git says nothing about a tracked directory precisely because there is
 * nothing wrong with it, so "git did not mention it" and "git does not know
 * it" are opposite conclusions from identical evidence.
 */
export function emptyDirCandidates(dirNames, known) {
  const seen = (known ?? []).map((p) => p.replace(/\\/g, "/"));
  return dirNames
    .filter((name) => !INTRINSIC_EXCLUSIONS.has(name))
    .filter((name) => !seen.some((p) => p === name || p.startsWith(`${name}/`)))
    .map((name) => `${name}/`)
    .sort();
}

/**
 * Split a shell command into the argument runs of its `git` invocations.
 *
 * A single tool call routinely chains commands (`git add -A && git commit`),
 * so matching the string as a whole would attribute one subcommand's flags to
 * another. Splitting on the shell operators that separate commands keeps each
 * invocation's arguments to itself. This is deliberately not a shell parser:
 * an operator inside a quoted string splits a segment a real shell would not.
 * That direction is safe -- it can only shorten an argument run, never merge
 * two commands into one.
 */
export function gitInvocations(command) {
  const found = [];
  for (const segment of String(command ?? "").split(/(?:&&|\|\||[;&|\n])/)) {
    const tokens = segment.trim().split(/\s+/).filter(Boolean);
    for (let i = 0; i < tokens.length; i++) {
      // `git`, `/usr/bin/git`, `git.exe` -- but not `github` or `mygit`.
      if (!/(?:^|[\\/])git(?:\.exe)?$/i.test(tokens[i])) continue;
      found.push(tokens.slice(i + 1));
      break;
    }
  }
  return found;
}

// Global options taking a separate value, so the value is not mistaken for the
// subcommand: `git -C some/dir add -A` must resolve to `add`, not `some/dir`.
const GIT_GLOBAL_WITH_VALUE = new Set([
  "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
]);

/**
 * The subcommand and its arguments, with git's global options stripped.
 *
 * `redirected` reports that the invocation was aimed at some other repository
 * (`git -C ../elsewhere add -A`). The guard's knowledge of stray artifacts is
 * specific to one checkout, so applying it to a command about a different one
 * would block on evidence that does not apply -- a confident wrong answer,
 * which is the failure mode this whole guard exists to avoid.
 */
export function gitSubcommand(args) {
  let redirected = false;
  for (let i = 0; i < args.length; i++) {
    const token = args[i];
    if (GIT_GLOBAL_WITH_VALUE.has(token)) {
      if (token !== "-c") redirected = true;
      i++;
      continue;
    }
    if (token.startsWith("--git-dir=") || token.startsWith("--work-tree=")) {
      redirected = true;
      continue;
    }
    if (token.startsWith("-")) continue;
    return { name: token, args: args.slice(i + 1), redirected };
  }
  return null;
}

/**
 * True when this `git add` argument list stages everything, strays included.
 *
 * Only the blanket forms count, and that asymmetry is the whole design. An
 * explicit pathspec is a deliberate, named act -- the agent has said which
 * file it means -- so it stays available as the escape hatch and must never be
 * blocked. What is being prevented is not committing an artifact, it is
 * committing one *without noticing*.
 *
 * `-u`/`--update` is intentionally absent: it restages already-tracked files
 * and cannot pick up an untracked artifact. A dry run is likewise never
 * blanket -- it changes nothing, and it is the obvious way for an agent to
 * inspect what a sweep would take, so blocking it would obstruct exactly the
 * caution this guard is asking for.
 */
export function addIsBlanket(args) {
  let afterPathspecSeparator = false;
  for (const arg of args) {
    if (arg === "--") break;
    if (arg === "--dry-run" || (/^-[A-Za-z]+$/.test(arg) && arg.includes("n"))) return false;
  }
  for (const arg of args) {
    if (arg === "--") {
      afterPathspecSeparator = true;
      continue;
    }
    if (!afterPathspecSeparator) {
      if (arg === "-A" || arg === "--all" || arg === "--no-ignore-removal") return true;
      // Combined short flags (`-Av`, `-fA`). A `-u` in the same cluster does
      // not cancel `-A`; git takes the broader of the two.
      if (/^-[A-Za-z]+$/.test(arg) && arg.includes("A")) return true;
      if (arg.startsWith("-")) continue;
    }
    if (arg === "." || arg === "./" || arg === ":/" || arg === "*") return true;
  }
  return false;
}

// `git stash` subcommands that do not create a stash entry, so no `-u`/`-a`
// they carry can sweep anything. `push`/`save` are the creating forms.
const STASH_NON_CREATING = new Set([
  "apply", "pop", "list", "show", "drop", "branch", "clear", "store",
]);

/**
 * True when this `git stash` argument list sweeps untracked files away.
 *
 * A plain `git stash` leaves untracked files alone. `-u` and `-a` take them,
 * which makes an artifact vanish from the working tree into a stash entry
 * nobody knows to look in -- and a later `git stash drop` deletes it outright.
 * A peer agent lost 454 lines of staged work to a subagent's `git stash` this
 * same evening, recoverable only from dangling objects, so this is an observed
 * failure rather than a hypothetical one.
 */
export function stashTakesUntracked(args) {
  for (const arg of args) {
    if (arg === "--") break;
    if (!arg.startsWith("-")) {
      if (STASH_NON_CREATING.has(arg)) return false;
      continue;
    }
    if (arg === "--include-untracked" || arg === "--all") return true;
    if (/^-[A-Za-z]+$/.test(arg) && (arg.includes("u") || arg.includes("a"))) return true;
  }
  return false;
}

/**
 * Decide whether a shell command would sweep known stray artifacts into git.
 * Returns `null` to allow, or `{ verb, strays }` to block.
 */
export function sweepDecision(command, strays) {
  const pending = [...new Set(strays ?? [])].sort();
  if (pending.length === 0) return null;
  for (const args of gitInvocations(command)) {
    const sub = gitSubcommand(args);
    if (!sub || sub.redirected) continue;
    if (sub.name === "add" && addIsBlanket(sub.args)) return { verb: "git add", strays: pending };
    if (sub.name === "stash" && stashTakesUntracked(sub.args)) return { verb: "git stash", strays: pending };
  }
  return null;
}

/** Message shown to the agent when a sweep is blocked. */
export function blockReason({ verb, strays }) {
  const plural = strays.length === 1 ? "" : "s";
  return (
    `[checkout-guard] BLOCKED: \`${verb}\` would sweep ${strays.length} stray ` +
    `artifact${plural} into git. These appeared in the checkout as a side ` +
    `effect of a shell command in this session and were never authored with ` +
    `the create/edit tools:\n` +
    strays.map((s) => `  - ${s}`).join("\n") +
    `\n\nAd-hoc probe scripts must write to a temp directory, not the shared ` +
    `checkout. Delete these, or -- if one is real work you meant to keep -- ` +
    `stage it by name (\`git add <path>\`), which this guard deliberately ` +
    `allows, because naming it is what makes it a decision. ` +
    `Set COPILOT_CHECKOUT_GUARD_DISABLE=1 to override.`
  );
}

/** Message shown to the agent the moment new artifacts are noticed. */
export function strayReport(strays, scratchDir) {
  const plural = strays.length === 1 ? "" : "s";
  return (
    `[checkout-guard] ${strays.length} new untracked path${plural} appeared in ` +
    `the checkout during your last command (a checkout can be shared with peer ` +
    `agents, so one of these may not be yours):\n` +
    strays.map((s) => `  - ${s}`).join("\n") +
    `\n\nIf they are probe or scratch artifacts, delete them now and rerun the ` +
    `work under ${scratchDir}. Do it while you still know what produced them: ` +
    `an artifact found later has no provenance, which is how it survives ` +
    `cleanup and gets misdiagnosed as a leak in the test suite.`
  );
}

/** Text injected at session start so the scratch directory is discoverable. */
export function sessionBriefing(scratchDir) {
  return (
    `[checkout-guard] Active. Scratch directory for this session: ${scratchDir} ` +
    `(already created). Put ad-hoc probe scripts, throwaway fixtures and ` +
    `experiment output there, never in a git checkout -- the checkout may be ` +
    `shared with peer agents, and an unexplained file in it costs someone hours. ` +
    `This applies to subagents you launch: tell them the same path, because ` +
    `their shell commands land in your checkout. A blanket \`git add -A\` is ` +
    `blocked while stray artifacts are present; staging a path by name is not.`
  );
}

// --- checkout inspection -------------------------------------------------
//
// Everything below shells out to git. It lives here rather than in
// extension.mjs so that the scan an agent's session actually depends on is the
// same code the tests run, against a real git binary and a real repository.

const GIT_TIMEOUT_MS = 5000;

export function git(args, cwd, { stdin = null } = {}) {
  return new Promise((done) => {
    const child = execFile(
      "git", args,
      { cwd, timeout: GIT_TIMEOUT_MS, maxBuffer: 8 * 1024 * 1024 },
      (err, stdout) => {
        // A non-zero exit is routine for some of these calls -- check-ignore
        // returns 1 when nothing matched -- so stdout comes back either way
        // and callers that care about failure read `ok`.
        done({ ok: !err, stdout: String(stdout ?? "") });
      },
    );
    if (stdin !== null) {
      // A child that has already exited leaves a stdin nobody is reading.
      child.stdin?.on("error", () => {});
      child.stdin?.end(stdin);
    }
  });
}

/**
 * The root of the working checkout containing `cwd`, or null.
 *
 * `--show-toplevel` is deliberate here, where most of this toolkit reaches for
 * the primary repository root instead: what is being protected is the working
 * *checkout* that files land in, and inside a linked worktree that is the
 * worktree, not the primary.
 */
export async function checkoutRoot(cwd) {
  const { ok, stdout } = await git(["rev-parse", "--show-toplevel"], cwd);
  if (!ok) return null;
  const root = stdout.trim();
  return root ? resolve(root) : null;
}

/** Top-level directory names, or [] when the checkout cannot be read. */
export function topLevelDirs(root) {
  try {
    return readdirSync(root, { withFileTypes: true })
      .filter((e) => e.isDirectory())
      .map((e) => e.name);
  } catch {
    return [];
  }
}

/** Drop the names covered by the project's own .gitignore. */
export async function withoutIgnored(root, names) {
  const candidates = names.filter((n) => !INTRINSIC_EXCLUSIONS.has(n));
  if (candidates.length === 0) return [];
  // Paths go in over stdin rather than as arguments, and the reason is not
  // ergonomics: `git check-ignore -z` refuses to run at all without `--stdin`
  // ("fatal: -z only makes sense with --stdin"), and because a failed call
  // returns no matches, the failure looks exactly like "nothing is ignored".
  // The guard would then have reported every ignored build directory in the
  // project as a stray artifact, silently and forever. Passing them as
  // arguments also breaks on a name beginning with a dash and has an
  // OS-dependent length limit.
  //
  // check-ignore prints only the paths that matched, so what comes back is the
  // ignored subset. Asking git is the point: an ignore list of this
  // extension's own would disagree with the project silently, and always in
  // the direction of missing things.
  const { stdout } = await git(["check-ignore", "-z", "--stdin"], root, {
    stdin: candidates.join("\0"),
  });
  const ignored = new Set(
    stdout.split("\0").filter(Boolean).map((p) => p.replace(/[\\/]$/, "")),
  );
  return candidates.filter((n) => !ignored.has(n));
}

/**
 * Top-level entries git has committed, or [] when there is no commit yet.
 *
 * `ls-tree` rather than `ls-files` because it returns exactly one level: the
 * question is only which top-level directories exist as far as git is
 * concerned, and `ls-files` would stream the entire index -- every file in the
 * repository -- to answer it, on every scan.
 */
export async function trackedTopLevel(root) {
  const { ok, stdout } = await git(["ls-tree", "--name-only", "-z", "HEAD"], root);
  // An unborn branch has no HEAD. That is not a failure: nothing is committed,
  // so everything present is staged or untracked and `git status` covers it.
  if (!ok) return [];
  return stdout.split("\0").filter(Boolean);
}

/**
 * Every untracked path in the checkout, including the empty directories git
 * refuses to report. Returns null when git could not answer, which callers
 * must treat as "no information" rather than "clean".
 */
export async function scanCheckout(root) {
  const { ok, stdout } = await git(["status", "--porcelain", "-uall", "-z"], root);
  if (!ok) return null;
  const untracked = parseUntracked(stdout);
  const known = [...parseStatusPaths(stdout), ...(await trackedTopLevel(root))];
  const dirs = await withoutIgnored(root, topLevelDirs(root));
  return [...untracked, ...emptyDirCandidates(dirs, known)];
}
