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

__all__ = ["primary_repo_root", "guid_is_usable", "projects_root", "project_dir"]

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


_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{n}" for n in range(1, 10)),
    *(f"LPT{n}" for n in range(1, 10)),
}
# `<>:"|?*` and the control characters cannot appear in a Windows filename.
# Letting one through does not create a directory, it raises deep inside
# `mkdir` -- and an embedded NUL raises ValueError, which is not an OSError and
# so slips straight through the usual guards.
_UNSAFE_GUID_CHARS = frozenset('<>:"|?*') | frozenset(chr(c) for c in range(32))


def projects_root() -> Path:
    """The directory holding one subdirectory per catalogued project.

    Resolved on each call rather than captured at import: the tests, and anyone
    who relocates a home directory, patch ``Path.home`` and expect the writer
    and the reader to follow it to the same place.
    """
    return Path.home() / ".copilot" / "projects"


def project_dir(guid: str) -> Path:
    """Where one project's handoff, its ``superseded/`` archive and its
    instructions live.

    Here for the same reason :func:`guid_is_usable` is: ``handoff_tool`` writes
    this path and ``copilot_operator`` reads it, and a path spelled out
    separately in the writer and the reader is a path that can drift. The
    deployed instructions quote it too, which makes a third copy -- pinned by
    ``tests/test_instructions_template.py`` against this function rather than
    against a retyped literal.
    """
    return projects_root() / guid


def guid_is_usable(guid: str) -> bool:
    """True when `guid` names exactly one directory under the projects root.

    This lives here, beside the other project-identity logic, because both the
    writer (``handoff_tool``) and the reader (``copilot_operator``) must agree
    on it. Two definitions of a valid project id that drift apart is precisely
    the defect it exists to prevent, so there is one definition and both
    import it.

    A catalog row is hand-edited often enough that its second column cannot be
    trusted to hold a GUID. A blank one is the dangerous case: ``projects /
    ""`` collapses back to the projects root itself, so the handoff lands in a
    single shared ``next-session.md`` that every project overwrites in turn --
    and the next session reads it, deletes it, and never learns it belonged to
    someone else. A separator or a `..` escapes the projects root the same way,
    just further.

    The trailing-dot rule is the subtle one, and it is the same bug wearing a
    disguise: Windows strips trailing dots and spaces from a path component, so
    ``projects/victim.`` and ``projects/victim`` are one directory. Accepting
    ``victim.`` would let a malformed row silently address a *different*
    project's handoff -- exactly the clobbering this function exists to stop.

    One collision is deliberately *not* rejected: ``abc`` and ``ABC`` are one
    directory on a case-insensitive filesystem. That is a different kind of
    fault. ``victim.`` is malformed in isolation -- it does not name what it
    appears to name -- whereas ``ABC`` names exactly ``ABC``, and the problem
    only exists if some *other* row also claims ``abc``. Catching it means
    comparing rows against each other, which belongs in a catalog check rather
    than in a predicate over one value, and rejecting case variants outright
    would break catalogs that are correct today.

    A symlink planted inside the projects root is likewise out of scope. This
    is a predicate over a string; it cannot see the filesystem, and a name that
    happens to be a link escapes the root no matter how well-formed it looks.
    Catching that needs a resolve-time containment check instead. It is not
    done here because the precondition already costs more than the exploit
    yields: anyone who can write a symlink into the projects root can just as
    easily drop a hostile ``next-session.md`` into a legitimate project's
    directory, and a handoff is read as instructions. Rejecting a resolved path
    that leaves the root would also break the user who deliberately symlinks a
    project's state onto another drive, which is a real setup and a correct
    one.
    """
    if not guid or guid != guid.strip():
        return False
    # Rejects ".", ".." and any run of dots, plus anything Windows would trim
    # down to a different name.
    if guid.strip(".") == "" or guid != guid.rstrip("."):
        return False
    if "/" in guid or "\\" in guid:
        return False
    if _UNSAFE_GUID_CHARS & frozenset(guid):
        return False
    if guid.split(".")[0].upper() in _WINDOWS_RESERVED:
        return False
    # Catches the platform-specific leftovers, notably a Windows drive-relative
    # token like `C:x`, whose final component is not the whole string.
    return guid == Path(guid).name
