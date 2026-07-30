#!/usr/bin/env python3
"""Project identity for repositories that use git worktrees.

All work happens in worktrees under ``<repoRoot>/.worktrees/``, so agents run
with a working directory that is a *checkout* of a project rather than the
project itself. Anything that identifies the project -- the catalog in
``~/.copilot/projects/catalog.csv``, the per-project directory, the handoff
file -- must key off the primary checkout, or every worktree looks like an
unregistered project and mints a duplicate GUID.
"""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path

__all__ = ["primary_repo_root"]

# A background supervisor with no console of its own would otherwise flash a
# real console window for each of these calls on Windows.
_POPEN_KWARGS: dict[str, int] = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if platform.system() == "Windows" else {}
)


def primary_repo_root(start=None) -> Path:
    """The primary checkout of whatever repository ``start`` belongs to.

    ``git rev-parse --show-toplevel`` is unusable here: inside a linked
    worktree it returns the worktree. ``--git-common-dir`` is unusable too --
    it is relative (``.git``) in the primary checkout but absolute in a
    worktree, so the obvious ``dirname`` of it is wrong in one of the two
    cases. The first record of ``git worktree list --porcelain`` is always the
    primary checkout, and it reads the same from anywhere in the repository.

    Returns ``start`` unchanged when git is missing, the call fails, or the
    path is not inside a repository, so callers outside a repo keep their
    previous behaviour.
    """
    base = Path(start) if start is not None else Path.cwd()
    try:
        if not base.is_dir():
            return base
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(base), capture_output=True, text=True, timeout=10,
            **_POPEN_KWARGS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return base
    if proc.returncode != 0:
        return base
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            candidate = line[len("worktree "):].strip()
            return Path(candidate) if candidate else base
    return base
