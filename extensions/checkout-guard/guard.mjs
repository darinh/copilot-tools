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
 * A directory name with any trailing slash removed, which is the one form
 * every comparison in this file is written against.
 *
 * Two conventions meet here and neither is wrong. `topLevelDirs` yields bare
 * names because that is what readdir gives; `emptyDirCandidates` appends a
 * slash because that is how a directory is reported to the agent; git echoes
 * back whichever form it was handed. The bug is not either convention, it is
 * comparing across them.
 *
 * BE PRECISE ABOUT THE STATUS OF THIS, because the temptation is to write it
 * up as a live bug and it is not one. Today the only caller of `withoutIgnored`
 * passes `topLevelDirs` output, which is bare, so all three comparisons below
 * currently agree with themselves and the ignore rule works. Measured, on the
 * pre-change code, with slash-suffixed input:
 *
 *   withoutIgnored(root, ["__pycache__/", "build/", "straydir/"])
 *     -> ["__pycache__/", "build/", "straydir/"]      nothing filtered,
 *        while check-ignore had just answered rc=0 "__pycache__/\0build/\0"
 *
 * That is the shape: not a crash, not an empty result, but a plausible list
 * with the rule silently switched off. `ignored` was built by stripping the
 * slash off git's ANSWER and never off the CANDIDATES, so the Set held
 * "build" and the lookup asked for "build/".
 *
 * Two smaller instances of the same mismatch: INTRINSIC_EXCLUSIONS.has(".git/")
 * is false where .has(".git") is true, and in `emptyDirCandidates` the
 * `p === name` test compares across the two forms -- though that one is
 * rescued today by the `p.startsWith(name + "/")` clause beside it, which
 * matches "build/" against a bare "build". Do not delete that clause thinking
 * this normalisation replaced it; it also does the different job of spotting a
 * directory git already reported a file inside.
 *
 * Normalising now rather than documenting the hazard, because the refactor
 * that makes it live is an obvious one -- feeding `emptyDirCandidates` output,
 * which IS slash-suffixed, back through `withoutIgnored` -- and because every
 * failure here is silent and in one direction: a Set lookup or an `===` that
 * misses always reports "not excluded", "not ignored", "not already known".
 */
export const bareName = (n) => n.replace(/[\\/]$/, "");

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
    // Canonical form FIRST, before anything compares. See `bareName`: every
    // filter below is an equality or prefix test against a bare name, and
    // "build/" silently matches none of them.
    .map(bareName)
    .filter((name) => !INTRINSIC_EXCLUSIONS.has(name))
    .filter((name) => !seen.some((p) => bareName(p) === name || p.startsWith(`${name}/`)))
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

/**
 * Report for artifacts that were already in a checkout when the session began.
 *
 * This exists because the baseline is seeded at session start and everything
 * in that seed is, from then on, indistinguishable from the project's own
 * content. `observe` reports what is NEW, which is the right question for
 * attribution and the wrong one for inventory: an artifact that arrived before
 * anyone was watching is never new again, so no later hook can ever raise it.
 * Session start is not the best moment to name it -- it is the only one.
 *
 * The seeding itself is correct and is deliberately left alone. What it
 * prevents is the sentence "you made this", which would be a fabricated
 * accusation. It should never have been allowed to suppress the different and
 * much weaker sentence "this is here, and nobody knows who made it" -- and the
 * wording below is careful to be only the second. It orders no deletion for
 * the same reason `primaryStrayReport` does not: from here a peer's live
 * experiment and a dead subagent's leftovers look identical.
 *
 * It says UNATTRIBUTABLE rather than unattributed, and the distinction is the
 * difference between this saving a session and costing one. "Unknown owner"
 * sends a reader looking for the owner; there is no owner to find, because the
 * session that made the artifact has ended and nothing on disk records who ran
 * what. Naming the search as futile is what leaves the cheap action -- ask, or
 * leave it -- as the obvious one.
 *
 * `primary` marks the checkout the agent is NOT working in, the same split
 * `primaryStrayReport` draws, minus its "during your last command" clause,
 * which would be false here. Without it the report that matters most reads as
 * a note about the current directory: an agent in a worktree is precisely the
 * one for whom strays in the primary are both invisible and expensive.
 */
export function inheritedStrayReport(strays, root, { primary = false } = {}) {
  const plural = strays.length === 1 ? "" : "s";
  const were = strays.length === 1 ? "was" : "were";
  const it = strays.length === 1 ? "it" : "them";
  const where = primary
    ? `the PRIMARY checkout (${root}), which is not the checkout you are ` +
      `working in,`
    : `${root}`;
  return (
    `[checkout-guard] INHERITED: ${strays.length} untracked path${plural} ` +
    `${were} already in ${where} before this session started:\n` +
    formatPathList(strays) +
    `\n\nNothing will mention ${it} again. The guard reports what appears ` +
    `DURING a session, and anything present at the start is folded into the ` +
    `baseline, so this is the only point at which ${it} can be raised.\n\n` +
    `Empty directories are included, and \`git status\` does not list those -- ` +
    `git tracks no empty directory, so a checkout can report perfectly clean ` +
    `with artifacts sitting in its root.\n\n` +
    `You cannot find out who made ${it} from in here, and neither can anyone ` +
    `else later: the session that produced ${it} is over, and nothing on disk ` +
    `records an author. So do not go hunting, and do not delete on this report ` +
    `alone -- in a checkout shared with peer agents a live experiment looks ` +
    `exactly like leftovers. Ask the other agents, or leave ${it} alone. ` +
    `What licenses a deletion is evidence that ${
      strays.length === 1 ? "it is" : "they are"
    } inert -- unchanged mtimes across a run of the suite, say -- never the ` +
    `mere fact that nobody has claimed ${it}.`
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

/**
 * The full text injected at session start: the briefing, plus a report for
 * each seeded checkout that already held artifacts.
 *
 * Composed here rather than in extension.mjs so the two properties that matter
 * are reachable from a test. extension.mjs calls `joinSession` at import and
 * cannot be loaded by the suite at all, so anything assembled there is covered
 * by `node --check` and nothing else -- and "both seeds reach the output" is
 * exactly the kind of wiring that fails silently while every unit test around
 * it stays green.
 *
 * `seeds` carries the arrays already scanned by the caller. Rescanning here
 * would cost a second traversal of two whole trees and would also reintroduce
 * misattribution pointing the other way: a path created between the caller's
 * scan and this one would be briefed as inherited when this session made it.
 *
 * A seed whose `strays` is null is skipped, never rendered as an empty
 * checkout. Null means the scan failed, and "clean" is the one conclusion a
 * failed scan must not be spent as.
 */
export function sessionContext(scratchDir, seeds = []) {
  const reports = seeds
    .filter((seed) => seed.strays !== null && seed.strays.length > 0)
    .map((seed) =>
      inheritedStrayReport(seed.strays, seed.root, { primary: seed.primary }),
    );
  // The briefing leads and is emitted unchanged when there is nothing to
  // report, so a session in a clean checkout sees exactly the text it saw
  // before this function existed.
  return [sessionBriefing(scratchDir), ...reports].join("\n\n");
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
      (err, stdout, stderr) => {
        // A non-zero exit is routine for some of these calls -- check-ignore
        // returns 1 when nothing matched -- so stdout comes back either way
        // and callers that care about failure read `ok`.
        //
        // `ok` alone is not enough for every caller, and `code` exists for the
        // ones it fails. check-ignore answers "nothing is ignored" with exit 1
        // and "I could not look" with exit 128, and both arrive here as
        // ok:false with empty stdout -- a real answer and the absence of one,
        // rendered identical. See `withoutIgnored`.
        //
        // null means git never ran or was killed: a spawn failure carries a
        // string errno rather than an exit status, and a timeout carries none
        // at all. Neither is an exit code and neither may be compared to one.
        const code = err ? (typeof err.code === "number" ? err.code : null) : 0;
        done({ ok: !err, code, stdout: String(stdout ?? ""), stderr: String(stderr ?? "") });
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
 * Returned when git could not answer a question about the repository layout.
 *
 * Distinct from `null`, which is a real answer meaning "there is no other
 * checkout to watch". Collapsing the two lets a single failed `git worktree
 * list` be recorded as a fact about the repository -- and because the answer
 * is cached for the session, one timeout would silently disable primary-root
 * watching for the rest of it. A failed probe must never be representable as
 * a legitimate value.
 */
export const UNKNOWN_ROOT = Symbol("checkout-guard.unknown-root");

/**
 * The repository's primary checkout, `null` if there is no working tree to
 * watch, or `UNKNOWN_ROOT` if git could not answer.
 *
 * `--show-toplevel` cannot answer this: inside a linked worktree it returns the
 * worktree. The first record of `git worktree list --porcelain` is always the
 * main working tree, from anywhere in the repository, which is why the rest of
 * this toolkit resolves the project that way too.
 *
 * `null` for a bare main worktree is a real answer: there is no working tree
 * there for anything to be left in, and reporting on one would mean reporting
 * on the contents of `.git`.
 */
export async function primaryCheckoutRoot(cwd) {
  const { ok, stdout } = await git(["worktree", "list", "--porcelain"], cwd);
  if (!ok) return UNKNOWN_ROOT;
  // Records are newline-separated and separated from each other by a blank
  // line; only the first is read, so a `bare` line later in the output belongs
  // to some other worktree and says nothing about this one.
  const first = stdout.split(/\r?\n\r?\n/)[0] ?? "";
  const lines = first.split(/\r?\n/);
  const worktree = lines.find((line) => line.startsWith("worktree "));
  // Exit zero with output this parser does not recognise is not a licence to
  // conclude anything either.
  if (!worktree) return UNKNOWN_ROOT;
  if (lines.some((line) => line.trim() === "bare")) return null;
  const root = worktree.slice("worktree ".length).trim();
  return root ? resolve(root) : UNKNOWN_ROOT;
}

/**
 * Decide which second checkout to watch, and whether the decision is worth
 * remembering.
 *
 * Pure so that the caching rule can be tested: a session caches this once, and
 * caching an answer that came from a failed lookup is how a transient error
 * becomes a permanent blind spot.
 */
export function rootToWatch(workingRoot, primary) {
  if (primary === UNKNOWN_ROOT) return { watch: null, cache: false };
  if (!primary || primary === workingRoot) return { watch: null, cache: true };
  return { watch: primary, cache: true };
}

/**
 * Checkout-relative prefixes of every linked worktree nested inside `root`,
 * or null when git could not answer.
 *
 * Null rather than `[]`, because "there are no nested worktrees" and "I could
 * not find out" are different claims and only the first licenses reporting
 * everything found. An empty list from a failed lookup would turn one git
 * failure into a report naming every worktree in the tree as a stray artifact.
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
  if (!ok) return null;
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
 * `scanCheckout` with linked worktrees nested inside `root` left out.
 *
 * A worktree is a checkout, not content of the checkout containing it, and
 * this applies to whichever tree the agent is in: an agent working in the
 * primary would otherwise have its own `git add -A` DENIED because a peer
 * created a worktree beside it. Applying the exclusion to one scan and not the
 * other would be an asymmetry with no justification behind it.
 *
 * How much this matters depends on the project's ignore rules. A repository
 * that lists its worktree directory in `.gitignore` -- as this one does with
 * `/.worktrees/` -- never sees those paths from `git status` in the first
 * place, so there the exclusion is defence in depth. It bites for real where a
 * worktree is created somewhere the ignore rules do not cover, which `git
 * worktree add <anywhere>` makes easy, and this extension ships to projects
 * that have no such convention at all.
 *
 * Returns null when either half could not be answered, which callers already
 * treat as "no information" rather than "clean".
 */
export async function scanCheckoutTree(root) {
  return withoutNestedWorktrees(
    await scanCheckout(root),
    await nestedWorktreePrefixes(root),
  );
}

/**
 * The filtering decision on its own, so it can be tested without needing a git
 * failure and a git success in the same directory at the same moment.
 *
 * Either argument being null means that question was not answered, and an
 * unanswered question is not an answer of "nothing": with no list of
 * worktrees, the honest result is null, not the unfiltered scan. Returning
 * `found` there would turn one transient `git worktree list` failure into a
 * report naming the agent's own worktree as a stray -- in the primary, on
 * every command afterwards.
 */
export function withoutNestedWorktrees(found, nested) {
  if (found === null || nested === null) return null;
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

/**
 * The names in `names` that the project's own .gitignore does not cover, or
 * null when git could not answer.
 *
 * Null is not pedantry. The only two things this can usefully say are "these
 * are ignored" and "none of them are", and the second is also what a broken
 * call looks like -- so without a third answer the failure is indistinguishable
 * from the most consequential success. See the exit-code note below.
 */
export async function withoutIgnored(root, names) {
  const candidates = names.filter((n) => !INTRINSIC_EXCLUSIONS.has(bareName(n)));
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
  //
  // The exit code is load-bearing and the paragraph above used to be the whole
  // defence, which was wrong: it names the hazard, closes one cause of it --
  // the `-z` fatal -- and reads as though it closed all of them. Measured:
  //
  //     nothing is ignored   exit 1    stdout ""
  //     could not look       exit 128  stdout ""
  //
  // Identical through `ok` and `stdout`. Only 1 is an answer. Anything else,
  // including a git that never spawned (code null), is an absence of one, and
  // the caller has to be told which it got -- because here the ambiguity does
  // not collapse onto "skip it", it collapses onto INVENTING strays, which the
  // working-tree report then hands to an agent as an order to delete.
  const { code, stdout, stderr } = await git(["check-ignore", "-z", "--stdin"], root, {
    stdin: candidates.join("\0"),
  });
  if (code !== 0 && code !== 1) return null;
  // Exit 1 is not always "nothing matched". An ignore file git cannot READ
  // also exits 1 with empty output, while `git status` on the same repository
  // still exits 0 -- so the rules are unknown and the exit code says they are
  // empty. Measured on Windows with a deny-read ACL on .gitignore:
  //
  //     git status    ok=true
  //     check-ignore  code=1  stdout=""
  //     stderr        warning: unable to access '.gitignore': Permission denied
  //
  // The warning is the only thing separating the two, and it is git reporting
  // its own failure rather than a second opinion of ours -- which is the whole
  // reason it is admissible here. Stat-ing the ignore files ourselves would be
  // forming a weaker independent answer to a question git already answers, and
  // could only subtract true positives.
  //
  // The match is on git's English wording, so a translated git falls back to
  // trusting exit 1: no worse than before this line existed, never worse than
  // the old behaviour. It errs toward refusing, which costs empty-directory
  // findings and never invents them.
  if (/unable to access|Permission denied/i.test(stderr)) return null;
  // Both sides of the comparison, not just git's. Stripping the answer alone
  // was the bug: candidates carrying a trailing slash then matched nothing in
  // the Set and the ignore rule silently stopped applying. The returned
  // strings are the caller's originals -- only the COMPARISON is normalised,
  // because "build/" and "build" mean different things downstream.
  const ignored = new Set(stdout.split("\0").filter(Boolean).map(bareName));
  return candidates.filter((n) => !ignored.has(bareName(n)));
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
 * The empty directories git cannot see, or [] when `dirs` is null because
 * check-ignore could not answer.
 *
 * Split out from `scanCheckout` so the refusal has a name and one obvious
 * place to be handled. `scanCheckout` reaches it through the `ignoreFilter`
 * seam; see there for why a seam is needed rather than a fixture.
 */
export function invisibleDirStrays(dirs, known, root) {
  if (dirs === null) return [];
  return emptyDirCandidates(dirs, known).filter((rel) =>
    holdsNoFiles(join(root, rel.replace(/\/$/, ""))),
  );
}

/**
 * Every untracked path in the checkout, including the empty directories git
 * refuses to report. Returns null when git could not answer, which callers
 * must treat as "no information" rather than "clean".
 *
 * `ignoreFilter` is a seam and exists for one reason: without it the branch
 * that handles a refusing check-ignore cannot be reached from a test. No
 * fixture reliably makes check-ignore fail while `git status` on the same
 * repository still succeeds -- an excludesFile pointing at a directory, an
 * attributesFile pointing at a directory and a .gitignore that IS a directory
 * were all measured and all exit 0 or 1. The alternative was a test that
 * asserted on `invisibleDirStrays` directly while its name promised something
 * about `scanCheckout`, which is a control on a neighbouring function: it
 * would stay green through any regression in how this function consumes the
 * refusal.
 */
export async function scanCheckout(root, { ignoreFilter = withoutIgnored } = {}) {
  const { ok, stdout } = await git(["status", "--porcelain", "-uall", "-z"], root);
  if (!ok) return null;
  const untracked = parseUntracked(stdout);
  const known = [...parseStatusPaths(stdout), ...(await trackedTopLevel(root))];
  // Only the invisible-directory half depends on check-ignore, so only that
  // half is dropped when check-ignore cannot answer. `git status` applies the
  // ignore rules itself and has already spoken for every path it can see, so
  // discarding its answer too would turn one blind spot into total blindness.
  //
  // Dropping is the point. Empty directories are the one population reported
  // here that git never confirms, so an unfiltered candidate list is not a
  // rougher answer -- it is every ignored build directory in the project,
  // handed to an agent as litter to delete. Fewer findings, never invented
  // ones.
  const dirs = await ignoreFilter(root, topLevelDirs(root));
  return [...untracked, ...invisibleDirStrays(dirs, known, root)];
}
