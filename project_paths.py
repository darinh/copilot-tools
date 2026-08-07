#!/usr/bin/env python3
"""Project identity for repositories that use git worktrees.

All work happens in worktrees under ``<repoRoot>/.worktrees/``, so agents run
with a working directory that is a *checkout* of a project rather than the
project itself. Anything that identifies the project -- the catalog in
``~/.operator/projects/catalog.csv``, the per-project directory, the handoff
file -- must key off the primary checkout, or every worktree looks like an
unregistered project and mints a duplicate GUID.
"""
from __future__ import annotations

import csv
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path

from install_manifest import file_present

__all__ = ["primary_repo_root", "guid_is_usable", "operator_home",
           "projects_root", "project_dir", "resolved_str", "catalog_rows",
           "normalized_key", "catalog_guid", "CatalogLookup",
           "CATALOG_MISSING", "CATALOG_UNREADABLE", "CATALOG_NO_ENTRY",
           "CATALOG_UNUSABLE_ID"]

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

    A path that cannot be *examined* is not one of those cases. ``is_dir()``
    raises on EACCES rather than answering, and treating that as "not a
    repository" hands a worktree path back as though it were a project root --
    which is exactly the duplicate-identity failure this module exists to
    prevent. So the probe is guarded and an unexaminable path still gets the
    git call, which either answers or fails on its own terms. See
    :func:`install_manifest.path_present` for the full polarity argument.

    The encoding is named rather than inherited. ``text=True`` alone decodes
    with the locale's preferred encoding -- cp1252 on Windows -- and git emits
    paths as UTF-8 bytes, so a repository whose path contains a character
    whose UTF-8 encoding includes an undefined cp1252 byte (measured: 0x81,
    from U+0401) killed subprocess's reader thread with UnicodeDecodeError.
    The process still exited 0, so the ``returncode`` guard below let it
    through, and ``proc.stdout`` was None: the failure arrived as an
    AttributeError from the loop, i.e. "the agent does not know what project
    it is in" spelled as a crash. ``errors="replace"`` cannot raise, and the
    explicit None check keeps a read that failed from reading as a repository
    with no worktrees.
    """
    base = Path(start) if start is not None else Path.cwd()
    try:
        if not base.is_dir():
            return base
    except OSError:
        pass
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(base), capture_output=True,
            encoding="utf-8", errors="replace", timeout=10,
            **_POPEN_KWARGS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return base
    if proc.returncode != 0 or proc.stdout is None:
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


def resolved_str(path) -> str:
    """``str`` of ``path`` resolved, falling back to a lexical absolute path.

    ``Path.resolve`` is not total, and it fails three ways rather than one. A
    symlink loop raises ``RuntimeError`` on every interpreter this project
    supports; a component that cannot be traversed raises ``OSError``; an
    embedded NUL raises ``ValueError`` from deep inside ``stat``. Only the
    middle one is an ``OSError``, so a guard written for filesystem trouble
    catches a third of the problem and the other two leave as tracebacks.

    This lives here rather than in one caller for the reason
    :func:`guid_is_usable` does. ``handoff_tool`` resolves a project root to
    write the handoff, ``copilot_operator`` resolves the same root to find it
    again, and ``operator_ingest`` resolves a log path -- and the version that
    had the guard was the one nobody else imported, so both of the others
    re-derived it and one of them re-derived it wrong. A rule that lives in
    one module is that module's history.

    The fallback does not follow links, so it can only be *less* resolved than
    the real answer, never differently resolved. Any comparison that puts both
    sides through this same function therefore matches a path that will not
    resolve literally or not at all; it cannot come to name something else.
    """
    try:
        return str(Path(path).resolve())
    except (OSError, RuntimeError, ValueError):
        return os.path.abspath(str(path))


def operator_home() -> Path:
    """This toolkit's own state directory, ``~/.operator`` by default.

    Lives here, rather than in ``copilot_operator``, because
    :func:`projects_root` needs it and ``copilot_operator`` imports *this*
    module -- so a definition up there is one this module cannot reach.
    ``copilot_operator`` re-exports it under its old name.

    ``COPILOT_OPERATOR_HOME`` overrides it, which is how the tests relocate
    the whole tree without touching ``Path.home``.
    """
    override = os.environ.get("COPILOT_OPERATOR_HOME")
    return Path(override) if override else Path.home() / ".operator"


def projects_root() -> Path:
    """The directory holding one subdirectory per catalogued project.

    Under ``~/.operator``, not ``~/.copilot``. ``~/.copilot`` is the Copilot
    CLI's own configuration directory -- its extensions, skills, settings,
    session store and logs are all in there -- so this toolkit keeping the
    project catalog in it was squatting in another program's directory. Every
    other piece of operator state had already moved out; the catalog and the
    per-project directories had not, and they are the ones that matter most,
    because the catalog is what maps a project to its id. Lose it and you have
    not lost a preference, you have lost every project's identity and with it
    every handoff and ``superseded/`` file keyed to that id.

    Resolved on each call rather than captured at import: the tests, and anyone
    who relocates a home directory, patch ``Path.home`` and expect the writer
    and the reader to follow it to the same place.
    """
    return operator_home() / "projects"


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


def catalog_rows(fh):
    """Yield one parsed row per line, or ``None`` for a line that will not parse.

    This lives here for the same reason :func:`guid_is_usable` does: both the
    writer (``handoff_tool``) and the reader (``copilot_operator``) read this
    file, and two definitions of "what the catalog says" that drift apart is
    the defect, not the inconvenience.

    ``csv.reader`` given the file object aborts the *whole* iteration on the
    first line it refuses, and before Python 3.11 an embedded NUL is exactly
    such a line -- ``_csv.Error: line contains NUL``. Two things follow, and
    both are wrong for a hand-edited file. The error escapes a caller whose
    entire job is to answer "registered or not", and every row *after* the bad
    one is never compared, so one mistyped character silently unregisters
    every project below it.

    Parsing each line on its own keeps the damage the size of the mistake: a
    line that will not parse costs that line and nothing else. ``None`` says
    "this row could not be read", which is not the same as a row that read
    cleanly and did not match -- the caller counts it as undecided rather than
    as evidence of absence.

    The cost is that a quoted field containing a newline is no longer joined
    across lines. The catalog is one entry per line by construction -- the
    format the instructions template documents, and nothing in this repository
    rewrites the file -- and a project path containing a newline cannot
    round-trip through it whichever reader is used, so nothing that works
    today stops working.
    """
    for line in fh:
        try:
            yield next(csv.reader([line]), None)
        except csv.Error:
            yield None


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


IS_WINDOWS = platform.system() == "Windows"

#: Why a catalog lookup produced no project id. Callers phrase their own
#: message from these rather than being handed one, because the two readers
#: (``handoff_tool`` and ``backlog_tool``) say different things to different
#: audiences about the same fact -- and a shared *message* would force them to
#: drift on the fact in order to differ on the wording.
CATALOG_MISSING = "catalog-missing"
CATALOG_UNREADABLE = "catalog-unreadable"
CATALOG_NO_ENTRY = "no-entry"
CATALOG_UNUSABLE_ID = "unusable-id"


@dataclass(frozen=True)
class CatalogLookup:
    """The outcome of one catalog lookup.

    ``guid`` is non-empty exactly when ``reason`` is ``None``. The failures are
    kept apart rather than collapsed into "no guid" because they call for
    opposite actions: an absent catalog wants creating, an unreadable one wants
    a permission fixed, a missing row wants a line added, and an unusable id
    wants the line it already has corrected. One of those instructions
    delivered for another situation is worse than none.
    """

    guid: "str | None" = None
    reason: "str | None" = None
    detail: str = ""


def normalized_key(path, windows=None) -> str:
    """A path in the form two references to the same location compare equal in.

    Case is folded on Windows and kept everywhere else, because that is where
    the filesystem's own comparison differs. Both sides of any comparison must
    come through here; see :func:`resolved_str` for why resolution alone is
    not enough.

    ``windows`` overrides the platform detection. It exists so that the caller
    that owns a module-level ``IS_WINDOWS`` -- and whose tests patch that name
    to exercise the other platform's rule -- can keep doing so without a second
    copy of the folding rule living in that module. A flag passed in is
    testable; a second implementation is only testable separately, and that is
    how the two spellings come to disagree.
    """
    resolved = resolved_str(path)
    fold = IS_WINDOWS if windows is None else windows
    return resolved.lower() if fold else resolved


def catalog_guid(project_root, catalog=None) -> CatalogLookup:
    """The project id ``catalog`` records for ``project_root``, if any.

    The single owner of "what the catalog says about this project". It lived
    in ``handoff_tool`` alone until ``backlog_tool`` needed the same answer,
    and a second match loop is precisely the thing that drifts: this repository
    has already paid for one duplicated discovery rule that let a file escape
    every assertion while every assertion stayed green.

    ``project_root`` must already be the *primary* checkout -- resolve it with
    :func:`primary_repo_root` first. A worktree path will not match, which is
    the correct outcome for a lookup keyed on project identity, but only if
    the caller knows that is what it asked.

    The presence probe is tri-state on purpose. ``file_present`` answers
    ``None`` for a catalog whose parent directory denies a stat, and that is
    *not* reported as missing: ``open`` below would have handed the file over
    without complaint, and "catalog not found" would send the reader off to
    create a file that is already sitting there.
    """
    catalog = Path(catalog) if catalog is not None else (
        projects_root() / "catalog.csv")
    if file_present(catalog) is False:
        return CatalogLookup(reason=CATALOG_MISSING, detail=str(catalog))
    target = normalized_key(project_root)
    try:
        with open(catalog, "r", encoding="utf-8", errors="replace",
                  newline="") as fh:
            for row in catalog_rows(fh):
                if row is None or len(row) < 2:
                    continue
                path, guid = row[0].strip().strip('"'), row[1].strip().strip('"')
                if not path:
                    continue
                try:
                    matched = normalized_key(path) == target
                except OSError:
                    continue
                if not matched:
                    continue
                if not guid_is_usable(guid):
                    return CatalogLookup(reason=CATALOG_UNUSABLE_ID,
                                         detail=guid)
                return CatalogLookup(guid=guid)
    except OSError as exc:
        return CatalogLookup(reason=CATALOG_UNREADABLE, detail=str(exc))
    return CatalogLookup(reason=CATALOG_NO_ENTRY, detail=target)
