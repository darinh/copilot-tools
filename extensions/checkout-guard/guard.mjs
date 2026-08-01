// checkout-guard — decision logic and checkout inspection.
//
// Kept separate from extension.mjs so it can be exercised with `node --test`
// without a live Copilot session. extension.mjs is then nothing but hook
// wiring and the SDK import, which is the only part that cannot be tested
// here -- deliberately, because a guard whose real git and filesystem work
// lives behind an untestable import is verified only by assertion.

import { execFile } from "node:child_process";
import { readdirSync } from "node:fs";
import { resolve, join, relative, isAbsolute } from "node:path";

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
 * Split a command string into segments of tokens, honouring quotes.
 *
 * Quote awareness is not a nicety here, it is the difference between a guard
 * and the appearance of one. Whitespace splitting alone leaves the quotes
 * attached to the token, so `git add "-A"` arrives as `'"-A"'`, matches no
 * flag pattern, and sails through -- the guard reports allowed and the sweep
 * happens. The same hole hides `"C:\Program Files\Git\cmd\git.exe" add -A`,
 * whose binary token ends in a quote and so fails the `git` test entirely.
 *
 * It cuts the other way too. Splitting a quoted `-m` message on whitespace
 * turns `git stash push -m "wip fix -u handling"` into a bare `-u` token and
 * blocks a stash that sweeps nothing. Both directions are wrong answers
 * delivered confidently, which is the one outcome this guard may not produce.
 *
 * Backslash is deliberately NOT an escape character. This runs on Windows,
 * where `\` is the path separator: treating it as an escape would reduce
 * `C:\cmd\git.exe` to `C:cmdgit.exe` and `.\` to `.`, breaking exactly the
 * detections the quoting fix is here to add. The cost is that POSIX `\*`
 * survives as a literal token, which the blanket-form check handles by name.
 *
 * An unterminated quote closes at end of input, so a truncated command still
 * yields its tokens rather than vanishing.
 */
export function tokenizeCommand(command) {
  const source = String(command ?? "");
  const segments = [];
  let tokens = [];
  let current = "";
  let started = false;
  let quote = null;

  const endToken = () => {
    if (started) tokens.push(current);
    current = "";
    started = false;
  };
  const endSegment = () => {
    endToken();
    if (tokens.length) segments.push(tokens);
    tokens = [];
  };

  for (let i = 0; i < source.length; i++) {
    const ch = source[i];
    if (quote) {
      // A quoted empty string is still a token, hence `started` rather than a
      // length test: `git commit -m ""` must not drop the message argument.
      if (ch === quote) quote = null;
      else current += ch;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      started = true;
      continue;
    }
    if (ch === "&" || ch === "|") {
      // `&&` and `||` are two characters; a single `&` or `|` separates just
      // as surely, so either way the segment ends here.
      if (source[i + 1] === ch) i++;
      endSegment();
      continue;
    }
    if (ch === "$" && source[i + 1] === "(") {
      // Command substitution. Its contents are a command in their own right,
      // so they become their own segment: `$(git add -A)` must be seen.
      i++;
      endSegment();
      continue;
    }
    if (ch === "(" || ch === ")" || ch === "`") {
      // Grouping and legacy substitution are syntax, not arguments. Leaving
      // the closing paren in the token stream made `( git add -A )` parse as
      // an add with a pathspec of `)`, which the scoping rule then read as a
      // deliberately narrowed command and allowed through.
      endSegment();
      continue;
    }
    if (ch === ";" || ch === "\n" || ch === "\r") {
      endSegment();
      continue;
    }
    if (ch === " " || ch === "\t") {
      endToken();
      continue;
    }
    current += ch;
    started = true;
  }
  endSegment();
  return segments;
}

// Commands that run another command, so the real executable is further along.
// `env` and `xargs` also take options, but an option to them is never `git`,
// so skipping non-git leading tokens is enough.
const TRANSPARENT_WRAPPERS = new Set([
  "sudo", "command", "env", "nohup", "time", "nice", "xargs", "exec", "builtin",
]);

// Shells whose `-c` argument is a command string in its own right.
const SHELL_RUNNERS = new Set([
  "sh", "bash", "zsh", "dash", "ksh", "busybox",
  "pwsh", "powershell", "pwsh.exe", "powershell.exe", "cmd", "cmd.exe",
]);

const isGitBinary = (token) => /(?:^|[\\/])git(?:\.exe)?$/i.test(token);
const basename = (token) => token.replace(/^.*[\\/]/, "").toLowerCase();

/**
 * Split a shell command into the argument runs of its `git` invocations.
 *
 * A single tool call routinely chains commands (`git add -A && git commit`),
 * so matching the string as a whole would attribute one subcommand's flags to
 * another. Splitting on the shell operators that separate commands keeps each
 * invocation's arguments to itself.
 *
 * `git` counts only in executable position -- first token of a segment, or
 * after an environment assignment or a wrapper that runs another command.
 * Matching it anywhere in the token list blocked `echo git add -A`, a command
 * that prints a string and touches nothing. A guard that blocks the sentence
 * describing an action, as though it were the action, teaches an agent that
 * its warnings are noise.
 *
 * A shell runner's `-c` string is recursed into, because `bash -c "git add
 * -A"` is a real invocation with a real effect. What is deliberately NOT
 * attempted is evaluation: `$(echo git) add -A` runs git and is not detected,
 * and cannot be without executing the substitution. That limit is stated in
 * the README. This guard is aimed at inattention, not evasion -- nobody
 * reaches for command substitution by accident, and the cost of guessing is
 * blocking legitimate work.
 */
export function gitInvocations(command, depth = 0) {
  const found = [];
  for (const tokens of tokenizeCommand(command)) {
    for (let i = 0; i < tokens.length; i++) {
      const token = tokens[i];
      if (isGitBinary(token)) {
        found.push(tokens.slice(i + 1));
        break;
      }
      const name = basename(token);
      // Brace grouping is syntax in executable position. It is skipped here
      // rather than split in the tokenizer so that brace *expansion*
      // (`file{1,2}.txt`), which is glued to its token, is left intact.
      if (token === "{" || token === "}" || token === "!") continue;
      // `FOO=bar git add -A` -- an assignment prefix keeps executable position.
      if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(token)) continue;
      if (TRANSPARENT_WRAPPERS.has(name)) continue;
      if (SHELL_RUNNERS.has(name) && depth < 2) {
        const flag = tokens.indexOf("-c", i + 1);
        if (flag !== -1 && tokens[flag + 1] !== undefined) {
          found.push(...gitInvocations(tokens[flag + 1], depth + 1));
        }
      }
      // Anything else in executable position means this segment is not git.
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

// Pathspecs that mean "the entire checkout" regardless of where git is run.
// `.\` is here because this runs on Windows, where it is what `.` looks like
// once a shell has completed a directory name. `\*` is the POSIX escaped glob,
// which survives tokenisation intact because backslash is not an escape here.
const ROOT_WIDE_PATHSPECS = new Set([
  ".", "./", ".\\", ":/", ":/.", "*", "\\*", "./*", ".\\*", ":/*", ":(top)",
]);

// Options whose value is a separate following token. The value must never be
// classified as a flag: `git stash push -m "-u"` carries no `-u` option at
// all, and `--chmod +x` is not a pathspec.
const ADD_OPTIONS_WITH_VALUE = new Set(["--chmod", "--pathspec-from-file"]);
const STASH_OPTIONS_WITH_VALUE = new Set(["-m", "--message", "-S", "--gpg-sign"]);

/**
 * Partition an argument list into flags and pathspecs.
 *
 * Everything after `--` is a pathspec by definition, even if it starts with a
 * dash, and a value-taking option swallows the token after it.
 */
function partitionArgs(args, optionsWithValue) {
  const flags = [];
  const pathspecs = [];
  let literal = false;
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (literal) {
      pathspecs.push(arg);
      continue;
    }
    if (arg === "--") {
      literal = true;
      continue;
    }
    if (optionsWithValue.has(arg)) {
      i++;
      continue;
    }
    if (arg.startsWith("-") && arg !== "-") flags.push(arg);
    else pathspecs.push(arg);
  }
  return { flags, pathspecs };
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
 * `-A` alongside a pathspec is therefore not blanket, and that is git's own
 * behaviour rather than a concession: `git add -A keep.txt` stages `keep.txt`
 * and leaves an untracked stray exactly where it was. Blocking it would have
 * punished the most careful form of the command available -- naming the file
 * *and* asking for its deletions -- while the guard's own escape hatch told
 * the agent to name the file.
 *
 * `-u`/`--update` is intentionally absent: it restages already-tracked files
 * and cannot pick up an untracked artifact. A dry run is likewise never
 * blanket -- it changes nothing, and it is the obvious way for an agent to
 * inspect what a sweep would take, so blocking it would obstruct exactly the
 * caution this guard is asking for.
 */
export function addIsBlanket(args) {
  const { flags, pathspecs } = partitionArgs(args, ADD_OPTIONS_WITH_VALUE);
  for (const flag of flags) {
    if (flag === "--dry-run") return false;
    if (/^-[A-Za-z]+$/.test(flag) && flag.includes("n")) return false;
  }
  // A root-wide pathspec sweeps whatever it is combined with.
  if (pathspecs.some((p) => ROOT_WIDE_PATHSPECS.has(p))) return true;
  // Any other named pathspec scopes the command, `-A` included.
  if (pathspecs.length > 0) return false;
  for (const flag of flags) {
    if (flag === "-A" || flag === "--all" || flag === "--no-ignore-removal") return true;
    // Combined short flags (`-Av`, `-fA`). A `-u` in the same cluster does
    // not cancel `-A`; git takes the broader of the two.
    if (/^-[A-Za-z]+$/.test(flag) && flag.includes("A")) return true;
  }
  return false;
}

// `git stash` subcommands that do not create a stash entry, so no `-u`/`-a`
// they carry can sweep anything. `push`/`save` are the creating forms.
// `create` builds a stash commit object and prints its hash without touching
// the working tree at all, and `store` only records one that already exists.
const STASH_NON_CREATING = new Set([
  "apply", "pop", "list", "show", "drop", "branch", "clear", "store", "create",
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
 *
 * The message argument is skipped rather than scanned, because `git stash push
 * -m "wip fix -u handling"` sweeps nothing and blocking it would be a false
 * positive with no escape hatch: unlike `add`, there is no way to name your
 * way out of a stash.
 */
export function stashTakesUntracked(args) {
  const { flags, pathspecs } = partitionArgs(args, STASH_OPTIONS_WITH_VALUE);
  if (pathspecs.some((p) => STASH_NON_CREATING.has(p))) return false;
  for (const flag of flags) {
    if (flag === "--include-untracked" || flag === "--all") return true;
    if (/^-[A-Za-z]+$/.test(flag) && (flag.includes("u") || flag.includes("a"))) return true;
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

/**
 * Format a path list for injection into the agent's context, bounded.
 *
 * An unpacked archive or an `npm install` that escapes its ignore rules can
 * produce thousands of paths, and this text goes straight into the model's
 * context. Emitting all of them would wedge the session the guard is meant to
 * be protecting -- a warning that costs more than the thing it warns about.
 * The count is always exact even when the list is truncated, because the
 * number is what tells the agent something went badly wrong.
 */
export const MAX_LISTED = 10;

export function formatPathList(paths, limit = MAX_LISTED) {
  const shown = paths.slice(0, limit).map((s) => `  - ${s}`);
  const hidden = paths.length - limit;
  if (hidden > 0) shown.push(`  ... and ${hidden} more`);
  return shown.join("\n");
}

/** Message shown to the agent when a sweep is blocked. */
export function blockReason({ verb, strays, unannounced = [] }) {
  const plural = strays.length === 1 ? "" : "s";
  const surprise = unannounced.length
    ? `\n\n${unannounced.length} of these are being named for the first time ` +
      `now -- they appeared without a tool call of yours in between, so a peer ` +
      `agent sharing this checkout or a background process is the likelier ` +
      `author:\n${formatPathList(unannounced)}`
    : "";
  return (
    `[checkout-guard] BLOCKED: \`${verb}\` would sweep ${strays.length} stray ` +
    `artifact${plural} into git. These are untracked paths that were never ` +
    `authored with the create/edit tools:\n` +
    formatPathList(strays) +
    surprise +
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
    formatPathList(strays) +
    `\n\nIf they are probe or scratch artifacts, delete them now and rerun the ` +
    `work under ${scratchDir}. Do it while you still know what produced them: ` +
    `an artifact found later has no provenance, which is how it survives ` +
    `cleanup and gets misdiagnosed as a leak in the test suite.`
  );
}

/**
 * Report for artifacts that appeared in the primary checkout while the agent
 * is working somewhere else.
 *
 * Worded separately from `strayReport` on purpose. "A file appeared" and "a
 * file appeared in the tree you are not looking at" are different findings,
 * and the second is the one that costs a peer their evening: the primary
 * checkout is the tree every other agent resolves as the project.
 *
 * It does not tell the agent to delete anything. From here a subagent's
 * leftover and a peer's live experiment are indistinguishable, and deleting
 * the second on the strength of the first is the mistake this toolkit spends
 * most of its effort removing from code.
 */
export function primaryStrayReport(strays, scratchDir, primaryRoot) {
  const plural = strays.length === 1 ? "" : "s";
  return (
    `[checkout-guard] ${strays.length} new untracked path${plural} appeared in ` +
    `the PRIMARY checkout (${primaryRoot}) during your last command, which is ` +
    `not the checkout you are working in:\n` +
    formatPathList(strays) +
    `\n\nMost likely a subagent: subagents run their own shell and a relative ` +
    `path resolves against whatever directory they happened to start in. If ` +
    `these are yours, delete them now and rerun that work under ${scratchDir}. ` +
    `If you cannot tell whether they are yours, ask the other agents before ` +
    `deleting anything -- a peer's live experiment looks exactly like ` +
    `leftovers from here, and the primary checkout is the one tree where a ` +
    `mystery file costs someone else the most.`
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
    `a subagent starts its own shell in a directory you did not choose, and a ` +
    `relative path resolves against that -- which may be this checkout or the ` +
    `repository's primary one. A blanket \`git add -A\` is ` +
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

/**
 * The repository's primary checkout, or null if it cannot be determined.
 *
 * `--show-toplevel` cannot answer this: inside a linked worktree it returns the
 * worktree. The first record of `git worktree list --porcelain` is always the
 * main working tree, from anywhere in the repository, which is why the rest of
 * this toolkit resolves the project that way too.
 *
 * Returns null for a bare main worktree. There is no working tree there for
 * anything to be left in, and reporting on one would be reporting on `.git`.
 */
export async function primaryCheckoutRoot(cwd) {
  const { ok, stdout } = await git(["worktree", "list", "--porcelain"], cwd);
  if (!ok) return null;
  // Records are newline-separated and separated from each other by a blank
  // line; only the first is read, so a `bare` line later in the output belongs
  // to some other worktree and says nothing about this one.
  const first = stdout.split(/\r?\n\r?\n/)[0] ?? "";
  const lines = first.split(/\r?\n/);
  const worktree = lines.find((line) => line.startsWith("worktree "));
  if (!worktree) return null;
  if (lines.some((line) => line.trim() === "bare")) return null;
  const root = worktree.slice("worktree ".length).trim();
  return root ? resolve(root) : null;
}

/**
 * Checkout-relative prefixes of every linked worktree nested inside `root`.
 *
 * A linked worktree is a checkout, never content of the checkout containing
 * it, but git has no opinion about that: `git status` in the primary reports a
 * nested worktree as an ordinary untracked directory. `INTRINSIC_EXCLUSIONS`
 * does not cover this, because it is consulted for empty-directory candidates
 * and ignore lookups rather than for git's own `??` records -- which is
 * invisible in a repository whose `.gitignore` lists `/.worktrees/`, as this
 * one's does.
 *
 * The paths come from git rather than from the `.worktrees/` naming
 * convention, so a worktree placed anywhere else is still recognised.
 */
export async function nestedWorktreePrefixes(root) {
  const { ok, stdout } = await git(["worktree", "list", "--porcelain"], root);
  if (!ok) return [];
  const prefixes = [];
  for (const record of stdout.split(/\r?\n\r?\n/)) {
    const line = record.split(/\r?\n/).find((l) => l.startsWith("worktree "));
    if (!line) continue;
    const path = resolve(line.slice("worktree ".length).trim());
    if (path === resolve(root)) continue;
    const rel = relative(root, path).replace(/\\/g, "/");
    if (!rel || rel.startsWith("../") || isAbsolute(rel)) continue;
    prefixes.push(`${rel}/`);
  }
  return prefixes;
}

/**
 * `scanCheckout` for a checkout the agent is not working in.
 *
 * Identical except that linked worktrees nested inside it are not reported.
 * Without this, every session that works in `.worktrees/<branch>` would be
 * told its own worktree is a stray artifact in the primary, on every command,
 * for ever -- and a guard that cries wolf gets switched off, which costs more
 * than the artifacts it was watching for.
 */
export async function scanPrimaryCheckout(root) {
  const found = await scanCheckout(root);
  if (found === null) return null;
  const nested = await nestedWorktreePrefixes(root);
  if (nested.length === 0) return found;
  return found.filter((p) => !nested.some((n) => p === n || p.startsWith(n)));
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
 * True when a directory contains no files at any depth.
 *
 * This is the precise question the empty-directory scan exists to ask, and it
 * matters that it is asked separately from the ignore check. `git status` is
 * silent about a directory for two very different reasons: because git cannot
 * represent it (it holds no files, so there is nothing to track), or because
 * everything inside it is ignored and git is deliberately declining to
 * mention it. Only the first is a stray. Reading silence as the first case
 * unconditionally reported any directory holding nothing but ignored output --
 * a build cache the project had explicitly told git to forget -- as a
 * mysterious artifact, which is the guard crying wolf about the project's own
 * configuration.
 *
 * The walk is bounded because it runs on paths chosen by nobody in particular;
 * exceeding the budget returns false, which reports nothing. Failing towards
 * silence is right for a guard whose false positives cost an agent a detour.
 */
export function holdsNoFiles(dir, budget = 512) {
  const stack = [dir];
  let visited = 0;
  while (stack.length) {
    if (++visited > budget) return false;
    const current = stack.pop();
    let entries;
    try {
      entries = readdirSync(current, { withFileTypes: true });
    } catch {
      // Unreadable is not empty, and guessing either way would be a claim the
      // filesystem declined to support.
      return false;
    }
    for (const entry of entries) {
      if (!entry.isDirectory()) return false;
      // Joined from the directory just read, never from `Dirent.parentPath`:
      // that property is absent on older Node runtimes, and the fallback
      // silently rebuilt every deep path against the original root, walking a
      // tree that does not exist and reporting a real stray as absent.
      stack.push(join(current, entry.name));
    }
  }
  return true;
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
  const invisible = emptyDirCandidates(dirs, known).filter((rel) =>
    holdsNoFiles(join(root, rel.replace(/\/$/, ""))),
  );
  return [...untracked, ...invisible];
}
