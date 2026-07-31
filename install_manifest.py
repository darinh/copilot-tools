"""A record of what setup deployed, so the next run can reason about it.

Setup copies files out of this repository into ``~/.copilot`` (instructions,
MCP config, skills, extensions). Once copied they are ordinary files the user
may edit, and without a record of what was written the installer cannot tell
these two situations apart:

* the repository moved forward and the user never touched their copy — the
  update is safe and should just happen;
* the user customised their copy — overwriting would destroy their work.

Byte-comparing the deployed file against the repository answers "are these
different", which is the wrong question; both cases differ. This module stores
the digest of what setup *wrote*, so the question becomes "did the user change
it since we wrote it", which is the one that decides whether to prompt.

The same record carries the version each artifact was installed at, which tells
a later run whether an update is needed at all, and gives version-to-version
upgrade functions something to key off. See :func:`pending_migrations`.

Hashing uses :mod:`hashlib` from the standard library. It is present wherever
Python is, needs no external binary, and costs about 0.1 ms for the largest
artifact here — spawning ``certutil`` or ``sha256sum`` to do the same job
measured 276x slower, because the cost is process creation rather than
arithmetic. SHA-256 is hardware-accelerated on current CPUs and its digest
matches ``Get-FileHash`` and ``sha256sum``, so a manifest entry can be checked
by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from copilot_tools_version import __version__ as CURRENT_VERSION

#: Schema version of the manifest file itself. Bumped only when the shape of
#: the JSON changes in a way older readers would misinterpret; the toolkit
#: version in ``package_version`` moves far more often.
MANIFEST_VERSION = 1

MANIFEST_NAME = "install-manifest.json"

#: Digest algorithm. See the module docstring for why this one.
ALGORITHM = "sha256"

_CHUNK = 1024 * 1024

# ── artifact states ──────────────────────────────────────────────
#: Nothing is deployed at the destination.
ABSENT = "absent"
#: Deployed content matches the repository. Nothing to do.
CURRENT = "current"
#: Deployed content differs from the repository but matches what setup wrote,
#: so the user never touched it and it can be replaced without asking.
STALE = "stale"
#: Deployed content differs from what setup wrote. The user edited it.
MODIFIED = "modified"
#: Something is deployed but the manifest has no record of writing it, so its
#: provenance is unknown and it is treated as precious.
UNTRACKED = "untracked"


# ── hashing ──────────────────────────────────────────────────────
def file_digest(path: Path) -> str | None:
    """Digest of one file, or None when it cannot be read.

    ``hashlib.file_digest`` (3.11+) reads straight into the hash object's
    buffer; the fallback below is the same thing done by hand for 3.10.
    """
    try:
        with open(path, "rb") as handle:
            if hasattr(hashlib, "file_digest"):
                return hashlib.file_digest(handle, ALGORITHM).hexdigest()
            digest = hashlib.new(ALGORITHM)
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except OSError:
        return None


def tree_digest(root: Path) -> str | None:
    """Digest of a directory tree, or None when it cannot be read.

    Relative paths are folded into the hash alongside contents so that renaming
    a file changes the digest, and they are sorted so the result does not depend
    on directory iteration order — which differs between filesystems and would
    otherwise make a tree look modified after merely being copied.
    """
    if not root.is_dir():
        return None
    digest = hashlib.new(ALGORITHM)
    try:
        files = sorted(
            (p for p in root.rglob("*") if p.is_file()),
            key=lambda p: p.relative_to(root).as_posix(),
        )
        for path in files:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(_CHUNK), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    except OSError:
        return None
    return digest.hexdigest()


def digest_for(path: Path) -> str | None:
    """Digest of a file or a directory, whichever ``path`` names."""
    if path.is_dir():
        return tree_digest(path)
    if path.exists():
        return file_digest(path)
    return None


# ── version ordering ─────────────────────────────────────────────
def parse_version(text: str | None) -> tuple[int, ...]:
    """Dotted version as a comparable tuple.

    Unparseable or missing versions sort below everything, so a machine with no
    manifest is treated as older than any release — which is what it is.
    """
    if not text:
        return (0,)
    parts: list[int] = []
    for chunk in str(text).split("."):
        match = re.match(r"\d+", chunk.strip())
        if not match:
            break
        parts.append(int(match.group()))
    return tuple(parts) or (0,)


def is_older(left: str | None, right: str | None) -> bool:
    return parse_version(left) < parse_version(right)


# ── manifest file ────────────────────────────────────────────────
def manifest_path(home: Path) -> Path:
    """Location of the manifest inside the operator home.

    It lives under ``~/.operator`` rather than ``~/.copilot`` because the
    Copilot CLI owns the latter and has been observed deleting subdirectories
    there wholesale on startup. The manifest describes files in ``~/.copilot``
    but must outlive them to be worth anything.
    """
    return Path(home) / MANIFEST_NAME


def empty_manifest() -> dict:
    return {
        "manifest_version": MANIFEST_VERSION,
        "package_version": None,
        "updated_at": None,
        "artifacts": {},
        "tools": {},
    }


def load(home: Path) -> dict:
    """Read the manifest, returning an empty one when absent or unreadable.

    A corrupt manifest is treated as no manifest: every artifact then reads as
    ``UNTRACKED``, which makes setup ask before touching anything. Losing the
    record costs prompts, never data.
    """
    path = manifest_path(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_manifest()
    if not isinstance(raw, dict):
        return empty_manifest()
    manifest = empty_manifest()
    manifest.update({k: v for k, v in raw.items() if k in manifest})
    if not isinstance(manifest.get("artifacts"), dict):
        manifest["artifacts"] = {}
    if not isinstance(manifest.get("tools"), dict):
        manifest["tools"] = {}
    return manifest


def save(home: Path, manifest: dict) -> Path:
    """Write the manifest atomically and return where it landed."""
    path = manifest_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest["manifest_version"] = MANIFEST_VERSION
    manifest["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
    )
    try:
        with handle as fh:
            fh.write(payload)
        os.replace(handle.name, path)
    except OSError:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return path


# ── artifact bookkeeping ─────────────────────────────────────────
def record(
    manifest: dict,
    key: str,
    dest: Path,
    *,
    kind: str,
    digest: str | None,
    version: str = CURRENT_VERSION,
    linked: bool = False,
) -> None:
    """Note that ``key`` was deployed to ``dest`` at ``version``."""
    manifest.setdefault("artifacts", {})[key] = {
        "kind": kind,
        "path": str(dest),
        "version": version,
        "linked": linked,
        ALGORITHM: digest,
        "installed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def entry(manifest: dict, key: str) -> dict | None:
    value = manifest.get("artifacts", {}).get(key)
    return value if isinstance(value, dict) else None


def classify(manifest: dict, key: str, dest: Path, source_digest: str | None) -> str:
    """Decide how ``dest`` relates to the repository and to what setup wrote.

    ``source_digest`` is the digest of the repository copy. The deployed file is
    hashed here so callers cannot pass a stale value.
    """
    if not dest.exists():
        return ABSENT
    known = entry(manifest, key)
    if known and known.get("linked"):
        # A junction or symlink points at the repository, so it is current by
        # construction and has no independent content to compare.
        return CURRENT
    deployed_digest = digest_for(dest)
    if deployed_digest is None:
        return UNTRACKED
    if source_digest is not None and deployed_digest == source_digest:
        return CURRENT
    if known is None:
        return UNTRACKED
    return STALE if deployed_digest == known.get(ALGORITHM) else MODIFIED


def may_overwrite(state: str) -> bool:
    """True when setup can replace the artifact without asking.

    Only ``STALE`` qualifies: it is the one state that proves the bytes on disk
    are the bytes setup itself wrote.
    """
    return state == STALE


def describe(state: str) -> str:
    return {
        ABSENT: "not installed",
        CURRENT: "up to date",
        STALE: "outdated (unmodified — safe to update)",
        MODIFIED: "modified locally",
        UNTRACKED: "present but not tracked",
    }.get(state, state)


# ── upgrade strategies ───────────────────────────────────────────
@dataclass
class MigrationContext:
    """What an upgrade function is handed.

    Migrations act on state already on disk, so they get the destinations
    rather than the repository alone.
    """

    copilot_dir: Path
    operator_home: Path
    repo_root: Path
    manifest: dict
    from_version: str | None
    to_version: str
    assume_yes: bool = False
    log: Callable[[str], None] = print
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.notes.append(message)
        self.log(message)


_MIGRATION_NAME = re.compile(r"^upgrade_v(\d+(?:_\d+)*)_to_v(\d+(?:_\d+)*)$")


def _name_version(chunk: str) -> str:
    return chunk.replace("_", ".")


# Add upgrade strategies below as plain functions named for the transition they
# perform, e.g. ``upgrade_v1_1_0_to_v1_2_0``. They are discovered by name, so
# nothing else needs editing. Two rules:
#
#   * be idempotent — a partially applied upgrade may be run again;
#   * check before you touch — the state you are migrating may not exist on a
#     machine that skipped several versions.
#
# A migration runs when the installed version is below its target and the
# target is at or below the version being installed, so a machine that jumps
# from 1.0.0 to 1.3.0 still runs every step in between, in order.


def discover_migrations(namespace: dict | None = None) -> list[tuple[str, str, Callable]]:
    """Find upgrade functions by name, sorted by the version they upgrade to."""
    found = []
    for name, value in (namespace if namespace is not None else globals()).items():
        match = _MIGRATION_NAME.match(name)
        if match and callable(value):
            found.append((_name_version(match.group(1)),
                          _name_version(match.group(2)),
                          value))
    return sorted(found, key=lambda item: parse_version(item[1]))


def pending_migrations(
    installed: str | None,
    target: str = CURRENT_VERSION,
    namespace: dict | None = None,
) -> list[tuple[str, str, Callable]]:
    """Migrations needed to get from ``installed`` to ``target``.

    Selection keys off each migration's *target* version rather than its source,
    so upgrading across several releases at once still runs every step. An
    unknown installed version sorts below everything and therefore runs them
    all — correct for a machine that predates the manifest, which is exactly
    the machine that has old state lying around.
    """
    return [
        item for item in discover_migrations(namespace)
        if is_older(installed, item[1]) and not is_older(target, item[1])
    ]


def run_migrations(ctx: MigrationContext, namespace: dict | None = None) -> list[str]:
    """Apply pending migrations in order and return the names that ran.

    A failing migration is reported and skipped rather than aborting setup: the
    remaining install steps are still worth doing, and stopping halfway would
    leave the machine in a worse state than the one being migrated from.
    """
    ran: list[str] = []
    for source, target, func in pending_migrations(ctx.from_version, ctx.to_version, namespace):
        ctx.note(f"Upgrading {source} -> {target}...")
        try:
            func(ctx)
        except Exception as exc:  # noqa: BLE001 - a bad migration must not abort setup
            ctx.note(f"  upgrade {source} -> {target} failed: {exc}")
            continue
        ran.append(func.__name__)
    return ran


# ── reporting ────────────────────────────────────────────────────
@dataclass
class ArtifactStatus:
    key: str
    kind: str
    dest: Path
    state: str
    installed_version: str | None


def status(
    manifest: dict,
    artifacts: Iterable[tuple[str, str, Path, Path]],
) -> list[ArtifactStatus]:
    """Classify each ``(key, kind, source, dest)`` against the manifest."""
    report = []
    for key, kind, source, dest in artifacts:
        known = entry(manifest, key)
        report.append(ArtifactStatus(
            key=key,
            kind=kind,
            dest=dest,
            state=classify(manifest, key, dest, digest_for(source)),
            installed_version=(known or {}).get("version"),
        ))
    return report


def needs_update(report: Iterable[ArtifactStatus]) -> bool:
    return any(item.state in (ABSENT, STALE, MODIFIED, UNTRACKED) for item in report)
