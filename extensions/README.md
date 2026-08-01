# Extensions

Copilot CLI runtime extensions that hook into the agent's tool-use lifecycle.
Installed globally to `~/.copilot/extensions/` by `setup.sh` (symlinked, so
edits here take effect on the next session).

## They only load in experimental mode

Extensions are an experimental CLI feature. A session that is not in
experimental mode loads **none** of them, and says nothing about it — an
extension that never loaded cannot report its own absence, so the session
looks exactly like one where every guard scanned and found nothing.

The CLI persists the last spelling it was given (`--experimental` /
`--no-experimental`) into `~/.copilot/settings.json`, which makes it sticky
global state that any session, on any project, can flip. This is not
hypothetical: agent sessions on this machine ran for over an hour with no
`checkout-guard` at all, in the shared primary checkout it exists to protect,
and nothing inside them could have told.

`operator` therefore passes `--experimental` on **every** launch, ahead of your
own arguments (see `with_experimental` in `copilot_operator.py`). Passing
`--no-experimental` yourself still wins, because the CLI resolves conflicting
spellings last-wins. If you start `copilot` by hand and want the extensions,
pass it yourself:

```bash
copilot --experimental
```

To check what mode you are in, read the key the CLI persists — an extension
cannot tell you it failed to load:

```bash
grep experimental ~/.copilot/settings.json
```

| Extension | Hook | What it does |
|-----------|------|--------------|
| `open-in-vs-code`      | `onPostToolUse` | Opens each edited/created file in VS Code (deduped, filtered, no shell injection). |
| `copy-to-clipboard-tool` | tool + `onUserPromptSubmitted` | Cross-platform `copy_to_clipboard` tool (pbcopy/clip/wl-copy/xclip). |
| `lint-on-edit`         | `onPostToolUse` | Runs ESLint or `ruff` on the changed file when the project supports it. |
| `security-shield`      | `onPreToolUse`  | Blocks destructive shell commands (`rm -rf /`, force-push to `main`, fork bombs, etc.) and obvious secret commits. |
| `test-enforcer`        | `onPreToolUse`  | Blocks `git commit` if source files were modified without tests in the same session. |
| `checkout-guard`       | session start + `onPreToolUse` + `onPostToolUse` | Names a scratch directory, reports untracked artifacts the moment they appear in the checkout — and in the repository's primary checkout when the session is in a linked worktree — and blocks a blanket `git add -A` / `git stash -u` while they are outstanding. |
| `architecture-enforcer` | `onPostToolUse` | Surfaces import-boundary violations defined in a per-project `.copilot-architecture.json`. |

## Knobs

| Env var | Effect |
|---------|--------|
| `COPILOT_AUTO_OPEN_DISABLE=1` | Disables `open-in-vs-code`. |
| `COPILOT_TEST_ENFORCER_BYPASS=1` | Lets `git commit` through even with untested source changes. |
| `COPILOT_CHECKOUT_GUARD_DISABLE=1` | Turns `checkout-guard` off entirely. |

## checkout-guard

Agents write ad-hoc probe scripts, and a probe script with a relative path
writes into whatever checkout the agent happens to be in. Three agents once
spent an evening on a pile of directories nobody could attribute, concluding
the test suite had a working-directory bug. It did not. The artifacts came from
adversarial review subagents reproducing bugs — nine directories from a single
review round, named after a reviewer's own loop variable.

The rule "probe scripts write to a temp directory" was already written down.
Writing it down did not work, so this extension enforces it:

- **Session start** creates `<tmp>/copilot-scratch/session-<pid>` and tells the
  agent the path, so the correct place to write is discoverable rather than
  merely mandated. The briefing says to pass the same path to subagents,
  because a subagent starts its own shell in a directory the parent did not
  choose, and a relative path resolves against that.
- **After every command that can run code** — including `task`, since a
  subagent's writes are invisible to the parent otherwise — the checkout is
  rescanned and anything new is reported immediately, while the producer is
  still known. Attribution is the point: an artifact found later fits every
  explanation equally well.
- **When the session is inside a linked worktree, the repository's primary
  checkout is watched as well.** `git rev-parse --show-toplevel` answers the
  worktree, so a session in `.worktrees/<branch>` used to be watching only that
  tree — while its subagents, starting in a directory nobody chose, wrote into
  the primary. That is not hypothetical: three artifacts appeared in this
  repository's primary checkout while their author worked in a worktree, and
  the guard reported clean in the same words it uses when nothing happened. It
  is a union and not a replacement; watching only the primary would go blind to
  the commoner case. The second tree is scanned **only after a subagent call**,
  not after every shell command: both real incidents came from subagents, and
  the primary is shared, so scanning it after every command would report every
  peer agent's artifacts to every other agent. This agent's own shell commands
  cannot surprise it in a tree it would have had to name the path of.
- **A blanket `git add -A`, or a `git stash` taking untracked files, is denied**
  while artifacts are outstanding. Staging a path by name is always allowed —
  including `git add -A keep.txt`, because git itself scopes `-A` to the
  pathspec and leaves the stray untracked. The aim is not to stop an artifact
  being committed, it is to stop one being committed *unnoticed* — so the
  escape hatch is to name the file, which is what turns it into a decision.

Four things it does deliberately:

- **A stray in the primary checkout is reported but never blocks.** It cannot
  deny a `git add -A` in the worktree, and the report does not tell anyone to
  delete anything. The primary is shared with peer agents, so from here a
  subagent's leftovers and a peer's live experiment are indistinguishable —
  and refusing this agent's commit over another agent's file is a refusal whose
  cost lands on a different asset than the one being protected.
- **Linked worktrees are excluded from every scan**, using the paths git
  reports rather than the `.worktrees/` name, so a worktree placed elsewhere is
  still recognised. `INTRINSIC_EXCLUSIONS` looks like it covers this and does
  not — it is consulted for empty-directory candidates and ignore lookups, not
  for git's own untracked records. In *this* repository that is invisible,
  because `.gitignore` lists `/.worktrees/` and those paths never reach the
  scan; there the exclusion is defence in depth rather than the thing standing
  between you and a false report. It bites where a worktree lands somewhere the
  ignore rules do not cover — `git worktree add <anywhere>` makes that a
  one-liner — and this extension ships to projects with no such convention at
  all. The exclusion applies to the working checkout as well as the primary:
  an agent in the primary would otherwise have its own `git add -A` *denied*
  because a peer created a worktree beside it.
- **It asks git what is ignored** (`check-ignore`) instead of carrying its own
  list of build-output directory names. A first draft hardcoded `target/` as
  conventional build output; `target/` was one of the two real artifacts in
  this repository at the time. A guard that holds its own opinion about what
  counts as noise will disagree with the project silently.
- **It scans for directories holding no files at any depth**, which `git
  status` cannot see at all — git does not track empty directories. Both real
  artifacts found in this repository were empty directories sitting in a tree
  git called clean. A directory holding only *ignored* files is not reported:
  git is silent about that one on purpose, and reading the two silences the
  same way made the guard complain about the project's own build cache.

Files written with the `create`/`edit` tools are never treated as artifacts.
Those are the sanctioned way to author content, and that is the discriminator
between a file the agent decided to write and a shell command's side effect.

**What it does not catch.** The command inspection is static: it reads the
command string, it does not evaluate it. `$(echo git) add -A` runs git and is
not detected, and cannot be without executing the substitution. Literal
invocations are covered, including through `sudo`/`env` wrappers, environment
assignment prefixes, shell grouping, command substitution and a shell runner's
`-c` string — but an agent determined to evade this can. That is the correct
trade: the guard is aimed at inattention, not evasion. Nobody reaches for
command substitution by accident, and guessing costs a blocked command that
was legitimate. The report on `onPostToolUse` has no such hole — it observes
the filesystem after the fact, whatever produced the change.

It fails open everywhere. A guard that breaks a session is worse than the
artifacts it prevents.

### Tests

```
node --test extensions/checkout-guard/guard.test.mjs
```

Also run from the Python suite by `tests/test_extensions.py`, which skips when
node is absent, and by the `Extension syntax and logic` CI job. The integration
tests build real git repositories under the OS temp directory — a test suite
for a litter guard that littered would be its own counterexample.

## architecture-enforcer config

Drop a `.copilot-architecture.json` at any project root:

```json
{
  "rules": [
    {
      "from": "src/Matrix\\.Domain/",
      "cannotImport": "Matrix\\.(Infrastructure|Operator|Api)",
      "reason": "Domain has zero project references."
    },
    {
      "from": "src/Matrix\\.Infrastructure/",
      "cannotImport": "Matrix\\.(Operator|Api)",
      "reason": "Infrastructure depends only on Domain."
    },
    {
      "from": "src/frontend/src/components/",
      "cannotImport": "/api/internal/",
      "reason": "Components must use the public API client."
    }
  ]
}
```

Without a config file the extension is a no-op — safe to install globally.

## Install

`setup.sh` symlinks each subdirectory here into `~/.copilot/extensions/`.
Run it after a `git pull` to pick up new extensions.

## Authoring

See `~/.cache/copilot/pkg/universal/$VERSION/copilot-sdk/docs/` for the SDK
reference. Each extension is a single `extension.mjs` file with a default
`joinSession({ hooks, tools })` call.
