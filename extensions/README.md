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
| `architecture-enforcer` | `onPostToolUse` | Surfaces import-boundary violations defined in a per-project `.copilot-architecture.json`. |

## Knobs

| Env var | Effect |
|---------|--------|
| `COPILOT_AUTO_OPEN_DISABLE=1` | Disables `open-in-vs-code`. |
| `COPILOT_TEST_ENFORCER_BYPASS=1` | Lets `git commit` through even with untested source changes. |

## architecture-enforcer config

Drop a `.copilot-architecture.json` at any project root:

```json
{
  "rules": [
    {
      "from": "src/MyApp\\.Domain/",
      "cannotImport": "MyApp\\.(Infrastructure|Api)",
      "reason": "Domain has zero project references."
    },
    {
      "from": "src/MyApp\\.Infrastructure/",
      "cannotImport": "MyApp\\.Api",
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

Run the setup script from the repo root:

- Linux / WSL / macOS: `./setup.sh` symlinks each subdirectory here into `~/.copilot/extensions/`.
- Windows (PowerShell): `./setup.ps1` installs each subdirectory as a directory junction (or symlink/copy fallback) under `%USERPROFILE%\.copilot\extensions\`. If WSL is also installed, the bash setup runs inside WSL too, so both environments see the same extensions.

Re-run setup after a `git pull` to pick up new extensions, or use `./upgrade.sh` / `./upgrade.ps1` to do both in one step.

## Authoring

See `~/.cache/copilot/pkg/universal/$VERSION/copilot-sdk/docs/` for the SDK
reference. Each extension is a single `extension.mjs` file with a default
`joinSession({ hooks, tools })` call.
