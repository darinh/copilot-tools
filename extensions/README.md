# Extensions

Copilot CLI runtime extensions that hook into the agent's tool-use lifecycle.
Installed globally to `~/.copilot/extensions/` by `setup.sh` (symlinked, so
edits here take effect on the next session).

| Extension | Hook | What it does |
|-----------|------|--------------|
| `open-in-vs-code`      | `onPostToolUse` | Opens each edited/created file in VS Code (deduped, filtered, no shell injection). |
| `copy-to-clipboard-tool` | tool + `onUserPromptSubmitted` | Cross-platform `copy_to_clipboard` tool (pbcopy/clip/wl-copy/xclip). |
| `lint-on-edit`         | `onPostToolUse` | Runs ESLint or `ruff` on the changed file when the project supports it. |
| `security-shield`      | `onPreToolUse`  | Blocks destructive shell commands (`rm -rf /`, force-push to `main`, fork bombs, etc.) and obvious secret commits. |
| `test-enforcer`        | `onPreToolUse`  | Blocks `git commit` if source files were modified without tests in the same session. |
| `checkout-guard`       | session start + `onPreToolUse` + `onPostToolUse` | Names a scratch directory, reports untracked artifacts the moment they appear in the checkout, and blocks a blanket `git add -A` / `git stash -u` while they are outstanding. |
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
  because their shell commands land in the parent's checkout.
- **After every command that can run code** — including `task`, since a
  subagent's writes are invisible to the parent otherwise — the checkout is
  rescanned and anything new is reported immediately, while the producer is
  still known. Attribution is the point: an artifact found later fits every
  explanation equally well.
- **A blanket `git add -A`, or a `git stash` taking untracked files, is denied**
  while artifacts are outstanding. Staging a path by name is always allowed.
  The aim is not to stop an artifact being committed, it is to stop one being
  committed *unnoticed* — so the escape hatch is to name the file, which is
  what turns it into a decision.

Two things it does deliberately:

- **It asks git what is ignored** (`check-ignore`) instead of carrying its own
  list of build-output directory names. A first draft hardcoded `target/` as
  conventional build output; `target/` was one of the two real artifacts in
  this repository at the time. A guard that holds its own opinion about what
  counts as noise will disagree with the project silently.
- **It scans for empty directories**, which `git status` cannot see at all —
  git does not track empty directories. Both real artifacts found in this
  repository were empty directories sitting in a tree git called clean.

Files written with the `create`/`edit` tools are never treated as artifacts.
Those are the sanctioned way to author content, and that is the discriminator
between a file the agent decided to write and a shell command's side effect.

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
