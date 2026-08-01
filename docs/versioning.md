# Versioning and the install manifest

Setup copies files out of this repository into your home directory. Once
copied they are ordinary files you can edit. The install manifest is the record
of what setup wrote, which is what lets a later run tell these two apart:

- the repository moved forward and you never touched your copy;
- you customised your copy.

Byte-comparing your file against the repository cannot distinguish them — both
differ. So setup used to prompt on *every* upgrade, which trains you to hit `y`
without reading, which is how customisations get destroyed.

## The version

One number, in `copilot_tools_version.py`:

```python
__version__ = "1.1.1"
```

`pyproject.toml` reads it with `dynamic = ["version"]`, so the packaging
metadata and the manifest cannot disagree about what is installed.

Bump it when something setup **deploys** changes — `templates/`, `skills/`,
`extensions/`, or the layout of what setup writes into `~/.copilot` and
`~/.operator`. Changes confined to the Python modules do not need a bump,
because an editable install picks those up from the repository directly.

## The manifest

`~/.operator/install-manifest.json`:

```json
{
  "manifest_version": 1,
  "package_version": "1.1.0",
  "updated_at": "2026-07-31T21:40:00Z",
  "artifacts": {
    "templates/copilot-instructions.md": {
      "kind": "template",
      "path": "C:\\Users\\you\\.copilot\\copilot-instructions.md",
      "version": "1.1.0",
      "linked": false,
      "sha256": "4d2142341969d9b3...",
      "installed_at": "2026-07-31T21:40:00Z"
    }
  },
  "tools": { "python": "3.12.4", "copilot": "...", "psmux": "psmux 3.3.7" }
}
```

It lives under `~/.operator` rather than `~/.copilot` because the Copilot CLI
owns the latter and has been observed deleting subdirectories there on startup.
The manifest describes files in `~/.copilot` but has to outlive them.

`tools` is recorded for diagnosis only — nothing branches on it. When a machine
misbehaves, which Copilot CLI and multiplexer it was set up against is usually
the first question.

### Hashing

Plain SHA-256 from Python's standard library `hashlib`. Three reasons:

- **It is already there.** No new dependency on any platform setup runs on.
- **It is fast where it counts.** About 0.1 ms for the largest artifact here.
  Shelling out to `certutil` or `sha256sum` to do the same job measured **276×
  slower** — the cost is process creation, not arithmetic. SHA-256 is
  hardware-accelerated on current CPUs (2.7 GB/s on the development machine,
  which is *faster* than BLAKE2b, whose Python build is the portable C
  reference implementation).
- **You can check it by hand.** The digest matches `Get-FileHash` on Windows
  and `sha256sum` on Linux and macOS.

Directories are hashed as a tree: each file's path and contents, in sorted
path order, folded into one digest. Sorting matters because directory
iteration order differs between filesystems, and an unsorted digest would make
a tree look modified after merely being copied. Paths are included so renaming
a file counts as a change.

## Artifact states

| State | Meaning | What setup does |
|-------|---------|-----------------|
| `absent` | nothing deployed | installs it |
| `current` | matches the repository | records it, changes nothing |
| `stale` | differs from the repository, matches what setup wrote | **updates without asking** |
| `modified` | differs from what setup wrote — you edited it | asks first |
| `untracked` | present, differs, no record of us writing it | asks first |
| `unreadable` | something is there but it could not be examined at all | leaves it, reports it |

`stale` is the only state where setup writes without asking, and it is the one
state that proves the bytes on disk are the bytes setup itself wrote.

`unreadable` exists because `absent` is the state that licenses writing, and
`Path.exists()` reaches it for the wrong reason. It **raises** on a permission
denial (verified on 3.11 and 3.12, so across the whole CI matrix), which aborts
a setup run over one artifact and leaves the rest uninstalled; and it **returns
False** for a drive that exists but is not ready (WINERROR 21, what a
disconnected network home looks like), for `ELOOP` and for `EBADF` — reporting
a path that is not absent as absent. It also follows symlinks, so a link whose
target was deleted reads as nothing being there and the install writes through
it into the target's location.

Presence is probed through `install_manifest.path_present()`, which keeps
"cannot tell" as its own answer: only `FileNotFoundError` and
`NotADirectoryError` mean gone, and it uses `lstat`. `--yes` does not override
`unreadable`, because `--yes` answers questions about contents somebody could
look at.

If the manifest is missing or corrupt it loads as empty, so everything
classifies as `untracked` and setup falls back to asking about everything —
exactly the behaviour from before manifests existed. Losing the record costs
prompts, never data.

Declining an overwrite deliberately does **not** record the file, so it stays
flagged and setup will keep asking rather than quietly claiming it next time.

## Checking status

```bash
python setup_tools.py --status
```

```
copilot-tools 1.1.0 (this checkout)
  ⚠️  Installed version 1.0.0 is older — run setup to update.

Manifest: /home/you/.operator/install-manifest.json

Deployed artifacts:
  templates/mcp-config.json           1.0.0  up to date
  templates/copilot-instructions.md   1.0.0  outdated (unmodified — safe to update)
  skills/operator-agents                  —  not installed
```

Exits `1` when anything needs updating, so it works in a shell conditional.
It writes nothing.

This is the answer to "I pulled on my other machine — do I need to re-run
setup?"

## Writing an upgrade

Some version bumps need existing state rewritten, not just replaced. Add a
function to `install_manifest.py` named for the transition:

```python
def upgrade_v1_1_0_to_v1_2_0(ctx):
    """Move project configs out of the old location."""
    old = ctx.copilot_dir / "projects.csv"
    if not old.exists():          # may already be gone, or never have existed
        return
    new = ctx.copilot_dir / "projects" / "catalog.csv"
    new.parent.mkdir(parents=True, exist_ok=True)
    old.replace(new)
    ctx.note(f"Moved {old} -> {new}")
```

Nothing else needs editing — functions are discovered by name.

`ctx` is a `MigrationContext` carrying `copilot_dir`, `operator_home`,
`repo_root`, the `manifest` itself, `from_version`, `to_version`, `assume_yes`,
and `note()` for logging.

Two rules:

- **Be idempotent.** A partially applied upgrade may be run again.
- **Check before you touch.** The state you are migrating may not exist on a
  machine that skipped several versions.

### When they run

An upgrade runs when the installed version is below its target *and* its target
is at or below the version being installed. Selection keys off the **target**
version rather than the source, so a machine jumping 1.0.0 → 1.3.0 still runs
every step in between, in order.

An unknown installed version sorts below everything and therefore runs
everything — correct, because a machine that predates the manifest is exactly
the machine with old state lying around.

Upgrades are skipped entirely on a machine with nothing deployed: there is no
old state to migrate before the first install.

A failing upgrade is reported and skipped rather than aborting setup. Stopping
halfway would leave the machine in a worse state than the one being migrated
from, and the remaining install steps are still worth doing.
