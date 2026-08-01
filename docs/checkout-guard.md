# checkout-guard

A Copilot CLI runtime extension that keeps agents' ad-hoc scratch files out of
git checkouts. It lives in `extensions/checkout-guard/` and is deployed to
`~/.copilot/extensions/checkout-guard` by setup.

It exists because the rule "probe scripts write to a temp directory, not the
shared checkout" was advisory, and advisory did not hold. Three agents once
spent an evening diagnosing a working-directory bug in the test suite on the
evidence of directories that kept appearing in a shared checkout. There was no
bug: the directories came from the agents' own review subagents reproducing
defects, nine of them from a single review round.

## Check that it is actually running first

**Extensions load only when the CLI is in experimental mode, and an extension
that never loaded cannot report its own absence.** A session with no guard
looks exactly like a session where the guard scanned and found nothing — same
silence, no error, nothing missing from the output that anyone could notice.

This is not hypothetical. Agent sessions on this machine ran for over an hour
with no `checkout-guard` at all, in the shared primary checkout it exists to
protect. Every file was deployed correctly the whole time.

The CLI persists the last spelling it was given — `--experimental` /
`--no-experimental` — into `~/.copilot/settings.json`. Nothing in this
repository writes that file, and it is global: any session, on any project, can
flip it out from under every other one.

```bash
python setup_tools.py --status
```

reports both halves of the question, and exits non-zero if either fails:

```
Extension loading:
  experimental mode is OFF — no extension loads
  start the CLI with --experimental (operator passes it for you)

Deployed extensions:
  extensions/checkout-guard          parses
```

`operator` passes `--experimental` on every launch unless the caller ruled on
it. If you start `copilot` by hand, pass it yourself.

Three states, kept apart on purpose. `experimental mode is on` and
`experimental mode is OFF` are measurements; `could not tell whether extensions
load` means the settings file was absent, unreadable, or held no `experimental`
key. The CLI documents no default for the key, so an absent one is not read as
either answer.

### Other ways to tell, from outside a session

The CLI writes one log per extension per launch to
`~/.copilot/logs/extensions/{source}-{name}-{epoch_ms}-{pid}.log`. It
distinguishes the three cases the session itself cannot:

| What you see | What it means |
|---|---|
| No log for this launch | Never attempted — extensions did not load at all |
| `Failed to load extension: …` and `=== exit … code=1 ===` | Attempted and died |
| A launch line and no exit line | Loaded, still running |

Read the message, not the exit code: a syntax error and a denied permission
both exit 1.

Inside a session, `extensions_manage` with `operation: "list"` reports a broken
extension as `failed`. The summary line printed by `extensions_reload` does
**not** — it counts only what is running, so a reload that says "7 extensions
running" is not evidence that the eighth is healthy.

## What it does

1. **Session start** creates a scratch directory at
   `<tmp>/copilot-scratch/session-<pid>` and names it in the agent's context,
   so the correct place to write is discoverable rather than merely mandated.
2. **After every command that can run arbitrary code** — including `task`,
   because a subagent's writes land in the parent's checkout — it rescans and
   reports newly appeared untracked paths immediately, while the producer is
   still known. In a linked worktree it scans the repository's *primary*
   checkout too.
3. **A blanket `git add -A`, or a `git stash` that takes untracked files, is
   denied** while such artifacts are outstanding. Staging a path by name is
   always allowed: the aim is to stop artifacts being committed *unnoticed*,
   not to stop them being committed.

Files written with the `create` and `edit` tools are never treated as strays.
Those are the sanctioned way to author content, and the distinction between "a
file the agent decided to write" and "a shell command's side effect" is exactly
that.

Only `.git` and `.worktrees` are excluded on the extension's own authority.
Everything else is decided by asking `git check-ignore`, so the guard agrees
with the project's `.gitignore` rather than carrying its own opinion about what
counts as noise.

It fails open everywhere. A guard that breaks a session is worse than the
artifacts it prevents.

## What it prints

Every message is prefixed `[checkout-guard]`.

| Message | When | What it means |
|---|---|---|
| `Active. Scratch directory for this session: …` | Session start, always | Where to write scratch files |
| `INHERITED: N untracked paths were already in …` | Session start, if any | Artifacts that predate the session |
| `UNSCANNED: N checkouts could not be examined …` | Session start, if a scan failed | Nobody looked — not a clean bill |
| `N new untracked paths appeared … during your last command` | After a command | New artifacts in the checkout you are in |
| `N new untracked paths appeared in the PRIMARY checkout …` | After a command, in a worktree | New artifacts in the tree you are *not* looking at |
| `BLOCKED: \`git add -A\` would sweep …` | On a blanket stage or sweeping stash | Refused; stage by name instead |

### INHERITED and UNSCANNED

Both exist because silence had two meanings and only one of them was true.

`observe` reports what is **new**, which is the right question for attribution
and the wrong one for inventory. The baseline is seeded at session start, so
anything already present is folded into it and is never new again — no later
hook can raise it. Six empty directories sat in this repository's primary
checkout across three sessions with nobody told. Session start is not the best
moment to name them; it is the only one.

The report deliberately orders no deletion. From inside a session a peer's live
experiment and a dead subagent's leftovers are indistinguishable, and the
population is the checkout's entire untracked state — which includes your own
uncommitted work and unignored build output. What licenses a deletion is
evidence that the artifacts are inert (unchanged mtimes across a full suite
run, say), never the mere fact that nobody has claimed them.

`UNSCANNED` covers the case where the session-start scan failed outright. A
failed scan that produced the same text as a clean checkout would spend "I
could not look" as "there is nothing to report" — the exact collapse the rest
of the guard exists to catch.

Empty directories are included in both. `git status` does not list them: git
tracks no empty directory, so a checkout can report perfectly clean with
artifacts sitting in its root.

## Turning it off

```bash
COPILOT_CHECKOUT_GUARD_DISABLE=1
```

Disables the extension entirely. The block message names it, so an agent that
hits a genuine false positive can act without hunting.

## Layout and tests

| File | What it is |
|---|---|
| `extension.mjs` | Hook wiring and the SDK import — nothing else |
| `guard.mjs` | All decision logic and checkout inspection |
| `guard.test.mjs` | `node --test` suite, no live session required |

```bash
node --test extensions/checkout-guard/guard.test.mjs
```

The split is deliberate and load-bearing. `extension.mjs` calls `joinSession`
at import, so the test suite cannot import it at all — anything composed there
is covered by `node --check` and nothing else. Wiring that fails silently must
live in `guard.mjs`, where a test can reach it.

For the same reason, `setup_tools.py` verifies the deployed extension with
`node --check` and never by importing it: the CLI injects its own bundled
`@github/copilot-sdk` into extension subprocesses, so importing a perfectly
healthy extension from outside a session fails with `MODULE_NOT_FOUND`.
