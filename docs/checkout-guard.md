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

`./setup.sh --status` and `./setup.ps1 --status` forward the flag, and forward
the exit code back unlabelled. A non-zero `--status` is a report — the machine
is out of date, or its extensions are inert — not a failure of setup, which
could not fix it in any case since this toolkit never writes `settings.json`.
Both entrypoints treat `--status`, `--check-only` and `--help` as questions and
run none of their install machinery for them. They used to call any non-zero
answer `Python setup failed`, and `setup.sh` went further: it ran its
legacy-migration steps for a question, so a `--status` on a machine that also
had `operator` further along `PATH` **deleted** `~/.local/bin/{operator,handoff}`
and reported a successful migration. See scenarios 8–12 in
`tests/test_setup_sh.sh`.

Three states, kept apart on purpose. `experimental mode is on` and
`experimental mode is OFF` are measurements; `could not tell whether extensions
load` means the settings file could not be read, or held an `experimental`
value that is not a boolean. An **unset** setting — no settings file, or a
settings file with no `experimental` key — is reported as `OFF`, because that
was measured on CLI 1.0.77: a probe extension whose module body writes a marker
at evaluation time never ran with the key absent, and ran with the same seeded
settings plus `--experimental`. The negative is only worth anything because of
that matched positive: identical settings, identical probe, only the flag
differing, and the flag deciding. Without it, "the extension did not load" is
explained just as well by the harness having broken the loader. The method, the
full result table and a reproduction are in
[experimental-default.md](experimental-default.md); what the result *means* is
here, and that file deliberately does not restate it.

Two limits on that answer, both deliberate:

**It is a snapshot, not a guarantee.** The setting is global and sticky, so it
can change the moment after the check reads it. During the session that wrote
this file it was observed going `true` → `false` → `true` inside eight minutes,
because another agent on the same machine was reproducing the outage.

Two things bound how much that matters. Passing `--experimental` does not just
apply to the process you pass it to — the CLI writes it back to
`settings.json` — so every `operator` launch repairs the value as a side
effect, and an operator-launched session cannot come up guardless whatever the
file said a moment earlier. The exposed population is bare `copilot` in a
terminal, which takes whatever the last writer left. Conflicting spellings are
not an error: last one wins, so `operator` can inject `--experimental` ahead of
your arguments and a deliberate `--no-experimental` still overrides it.

The other bound is the one worth remembering, because it is invisible: flipping
the setting does **not** unload extensions that are already running. A machine
can sit in a state where every live session has a guard and every new session
silently does not, and no live session can see it. That is this same
silence-with-two-causes, spread across time instead of across processes. If you
need to know what actually happened in a session that has already started, use
the logs below rather than `--status`.

**`could not tell` exits 0; unset does not.** Only a failed read — an
unreadable settings file, or a non-boolean value — exits 0 while declining to
answer. An unset setting exits non-zero along with a measured `false`, because
those two are the same state: extensions do not load, and nothing will change
that on its own.

That is a reversal, and the reasoning it replaced is worth keeping because it
was wrong in an instructive way. This document used to argue that an absent
settings file is the normal state of a machine where the CLI has not run yet,
so failing there would cry wolf on every fresh install — and it named the cost
honestly, as a conditional: *if the CLI's own default turns out to be
experimental-off, a machine with no `experimental` key is inert and this
command will not say so.*

The measurement turned that `if` into an `is`. A fresh machine is not waiting
for an answer, it is inert; and because the CLI writes nothing unless given a
spelling explicitly, it stays inert indefinitely. So the old behaviour was not
caution — it was the true answer withheld, and withheld precisely in the
configuration that is both the most common and the most broken. The hedge was
doing the same job as the silence this whole document is about: it was the
component whose purpose is distinguishing "fine" from "not checked", returning
the second when it could have returned the first.

The scope of the claim is narrow and stated in the code that relies on it
(`_UNSET_IS_OFF` in `setup_tools.py`): measured on CLI 1.0.77, one platform,
and `copilot --help` still documents no default. It is measured behaviour, not
a contract, and a `copilot update` can move it — which is why the reason string
says `measured` and names the version instead of asserting what the CLI
promises.

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
| `extension.mjs` | SDK wiring and the three hook bodies |
| `guard.mjs` | Every decision the guard makes, its per-session state, and all checkout inspection |
| `guard.test.mjs` | `node --test` suite, no live session required |

```bash
node --test extensions/checkout-guard/guard.test.mjs
```

The split is deliberate and load-bearing, but it is a split by *testability*,
not by size. `extension.mjs` calls `joinSession` at import, so importing it has
a side effect and the test suite cannot reach it at all: everything it holds is
covered by `node --check` and by nothing else. The untestability is structural,
not incidental.

That makes the line a real budget rather than a style preference: **anything
moved into `guard.mjs` gains the node suite, and anything that stays gains a
parse check.** So `extension.mjs` holds SDK wiring and the three hook bodies
and nothing else — 179 lines against `guard.mjs`'s 1314. The per-session
tracking state, `observe`, `noteAuthored`, `otherRootToWatch`,
`relativeToCheckout`, `MAX_TRACKED`, the scratch-directory name, the
disable-flag reading and the tool-name sets all live in `guard.mjs`, where they
are tested. Logic accumulating in `extension.mjs` is a debt, not a convenience.

The state is a factory — `createGuardState()` returning fresh `lastSeen` /
`outstanding` / `authored` / `primaryRoots` maps — rather than four
module-level bindings, and that is not a style choice either. A module binding
is shared by every importer for the life of the process, so the moment those
maps live in the tested file they would be shared across every case in
`guard.test.mjs`, and the failures that produces are order-dependent and
intermittent. The extension creates exactly one state, so nothing is lost by
making the lifetime explicit.

Because `guard.mjs` is where the decisions live, `setup_tools.py` parses every
`.mjs` in a deployed extension rather than just the entrypoint: `node --check`
does not follow imports, so a truncated `guard.mjs` leaves `extension.mjs`
parsing perfectly while the guard is dead in every session.

For the same reason, `setup_tools.py` verifies the deployed extension with
`node --check` and never by importing it: the CLI injects its own bundled
`@github/copilot-sdk` into extension subprocesses, so importing a perfectly
healthy extension from outside a session fails with `MODULE_NOT_FOUND`.
