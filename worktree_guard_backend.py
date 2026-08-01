#!/usr/bin/env python3
"""A PEP 517 backend that refuses an *editable* install of a linked worktree.

``pip install -e <dir>`` does not copy anything. It writes ``<dir>`` into the
interpreter's import path -- an ``__editable___*_finder.py`` mapping every
module of this project at that directory -- and points the ``operator`` and
``handoff`` console scripts there too. Both are machine-wide for that
interpreter, and both outlive the shell that created them.

A git worktree is created in order to be deleted. So an editable install made
from one is a promise that the installing agent is about to break itself, and
**the breakage does not surface when the mistake is made**. It surfaces later,
when somebody else finishes that branch and correctly runs ``git worktree
remove`` -- at which point ``operator`` and ``handoff`` die with
``ModuleNotFoundError`` for every user of that interpreter, and whoever gets
the traceback has no path back to the cause. That has happened twice on this
project; the second occurrence took out every agent on the box, and the repair
was found only because someone recognised the shape from the first.

**Why this is a build backend and not a check in setup.** ``setup_tools`` can
only guard the installs it performs itself, and neither outage came through
it: both were a human or an agent typing ``pip install -e .`` in the directory
they happened to be standing in, which for every agent on this project is a
worktree. A backend hook is the one place that sees *every* editable install
of this project however it was invoked -- pip, uv, ``build``, an IDE -- because
it is the code the frontend must call to produce one.

**Detection is filesystem-local and git-free, deliberately.** A build backend
runs in an isolated environment that is not guaranteed a ``git`` executable, so
shelling out would degrade to "cannot tell" exactly where it matters. It is
also unnecessary: git already records the answer in the checkout. A primary
checkout has a ``.git`` *directory*; a linked worktree has a ``.git`` *file*
holding ``gitdir: <common>/worktrees/<name>``. The ``worktrees`` component is
what makes it a worktree rather than a submodule, whose ``.git`` file is
identical in shape but points into ``<super>/.git/modules/<name>`` -- and a
submodule is a durable checkout that is perfectly reasonable to install from.
Testing only for "``.git`` is a file" would refuse those too.

**The unknown case refuses.** Refusing a checkout this cannot classify costs
one environment variable, named in the refusal itself, on a machine where a
human is watching a command they just typed. Allowing one costs a silent
machine-wide outage discovered days later by someone who did not cause it. The
two are not comparable, so the tie does not go to convenience. What the
refusal must *not* do is claim to know more than it does: an unclassifiable
checkout is reported as unclassifiable, never as a worktree.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

#: Set to any non-empty value to install anyway. It exists because "this
#: scan is wrong about my checkout" must have an answer that is not "edit the
#: build backend", and because the negative controls in the test suite need a
#: supported way to reach the setuptools hooks from a worktree.
OVERRIDE_ENV = "COPILOT_TOOLS_ALLOW_WORKTREE_INSTALL"

#: :func:`classify_checkout` verdicts. ``UNKNOWN`` is a third value rather
#: than a fold into either neighbour: a read that failed has to stay
#: distinguishable from a read that answered, all the way to the decision.
PRIMARY = "primary"
WORKTREE = "worktree"
UNKNOWN = "unknown"


class EditableInstallFromWorktree(Exception):
    """Raised instead of producing an editable install of a worktree."""


def _gitdir_line(text: str) -> str | None:
    """The path from the ``gitdir:`` line of a ``.git`` file, if there is one."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("gitdir:"):
            target = stripped[len("gitdir:"):].strip()
            return target or None
    return None


def _normalise(path: Path) -> Path:
    """Collapse ``..`` without touching the filesystem.

    ``Path.resolve`` is the obvious spelling and the wrong one here: it stats,
    which can raise, and it follows symlinks, which would rewrite a deliberate
    link into its target and change what the message says. Nothing downstream
    needs the real path -- only a printable, comparable one.
    """
    return Path(os.path.normpath(str(path)))


def primary_checkout_of(gitdir: Path) -> Path | None:
    """The primary checkout owning the worktree whose git dir is ``gitdir``.

    Best effort, and used only to make the refusal actionable -- the refusal
    itself never depends on this answering. ``<gitdir>/commondir`` is git's own
    record of where the shared git directory is, so it is tried first and the
    positional guess (``<common>/worktrees/<name>`` -> up two) is the fallback
    for a worktree whose commondir cannot be read.

    Returns ``None`` rather than a guess unless the answer is *corroborated* --
    the candidate must be a directory named ``.git`` that actually holds a git
    directory's own files. A relative ``gitdir:`` under a bind mount, or any
    layout the positional fallback was not written for, otherwise yields a
    plausible path that is not a checkout, printed in a "run this instead"
    line where it will be believed. No line at all is better than a wrong one:
    the caller falls back to telling the reader how to find the checkout
    themselves.
    """
    common: Path | None = None
    try:
        raw = (gitdir / "commondir").read_text(encoding="utf-8",
                                               errors="replace").strip()
    except OSError:
        raw = ""
    if raw:
        candidate = Path(raw)
        common = candidate if candidate.is_absolute() else gitdir / candidate
    if common is None:
        common = gitdir.parent.parent
    common = _normalise(common)
    if common.name != ".git":
        return None
    if _probe(common / "config") is not True and _probe(common / "HEAD") is not True:
        return None
    return common.parent


def _probe(path: Path) -> bool | None:
    """Tri-state presence: ``True``, ``False``, or ``None`` for "cannot tell".

    A read that failed has to stay distinguishable from a read that answered
    "absent", because the two lead to opposite decisions here: an absent
    ``commondir`` is positive evidence that a git directory belongs to a
    durable checkout, while an unreadable one is no evidence at all.
    """
    try:
        return path.exists()  # probe-ok: the OSError case is the None below
    except OSError:
        return None


def _kind_of_git_directory(gitdir: Path) -> str:
    """Classify a git directory that the path shape did not recognise.

    ``git init --separate-git-dir`` and ``git clone --separate-git-dir`` put
    the real git directory anywhere the user likes and leave a ``.git`` *file*
    behind, which is the same shape a worktree and a submodule have and lands
    in none of their well-known locations. Those checkouts are durable, and
    refusing them would be this guard being wrong about somebody else's
    perfectly ordinary layout.

    ``commondir`` is git's own marker and the discriminator used here: a
    *linked* worktree's git directory always has one, naming the shared
    directory it borrows objects and refs from. A main worktree's ``.git``, a
    submodule's git directory and a ``--separate-git-dir`` target never do.
    """
    common = _probe(gitdir / "commondir")
    if common is True:
        return WORKTREE
    if common is False and _probe(gitdir / "HEAD") is True:
        # A git directory that is nobody's linked worktree.
        return PRIMARY
    return UNKNOWN


def classify_checkout(source_dir: str | os.PathLike[str]) -> tuple[str, str]:
    """Classify ``source_dir`` as :data:`PRIMARY`, :data:`WORKTREE`, or
    :data:`UNKNOWN`, with a human-readable detail string.

    The detail is empty for :data:`PRIMARY` and describes what was observed --
    not what it implies -- for the other two.
    """
    source = Path(source_dir)
    dot_git = source / ".git"
    try:
        is_file = dot_git.is_file()
    except OSError as exc:
        return UNKNOWN, f"{dot_git} could not be examined ({exc})"
    if not is_file:
        # A directory (primary checkout) or nothing at all (an unpacked sdist,
        # an export, a vendored copy). Neither is disposable.
        return PRIMARY, ""
    try:
        text = dot_git.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return UNKNOWN, f"{dot_git} is a file that could not be read ({exc})"
    target = _gitdir_line(text)
    if target is None:
        return UNKNOWN, f"{dot_git} is a file with no 'gitdir:' line"
    gitdir = Path(target)
    if not gitdir.is_absolute():
        gitdir = source / gitdir
    gitdir = _normalise(gitdir)
    parent = gitdir.parent.name
    # The path shape answers first: it needs no further filesystem access, so
    # it still answers for a checkout whose git directory has been moved,
    # unmounted or made unreadable -- the cases where refusing on a guess
    # would be least defensible.
    if parent == "worktrees":
        return WORKTREE, str(gitdir)
    if parent == "modules":
        # A submodule. Same shape, opposite lifetime.
        return PRIMARY, ""
    kind = _kind_of_git_directory(gitdir)
    if kind == WORKTREE:
        return WORKTREE, str(gitdir)
    if kind == PRIMARY:
        return PRIMARY, ""
    return UNKNOWN, f"{dot_git} points at {gitdir}, which is neither a " \
                    f"worktree nor a submodule git directory"


def _refusal_message(source: Path, verdict: str, detail: str) -> str:
    """The text of the refusal. Assembled here so a test can read it."""
    if verdict == WORKTREE:
        headline = ("copilot-tools: refusing to build an editable install "
                    "from a linked git worktree.")
        observed = f"  worktree git dir:  {detail}"
    else:
        headline = ("copilot-tools: refusing to build an editable install "
                    "from a checkout that could not be classified.")
        observed = f"  what was observed: {detail}"

    gitdir = Path(detail) if verdict == WORKTREE else None
    primary = primary_checkout_of(gitdir) if gitdir is not None else None
    if primary is not None:
        remedy = ("Install from the primary checkout instead:\n\n"
                  f"    {sys.executable} -m pip install -e \"{primary}\"")
    else:
        remedy = ("Install from the repository's primary checkout instead --\n"
                  "the first record of `git worktree list --porcelain` names "
                  "it.")

    return "\n".join((
        "",
        headline,
        "",
        f"  source directory:  {source}",
        observed,
        "",
        "An editable install does not copy anything: it writes the source",
        "directory into this interpreter's import path and points the",
        "`operator` and `handoff` console scripts at it, machine-wide. A",
        "worktree exists in order to be deleted, so this install would break",
        "every console script the moment somebody else correctly finishes the",
        "branch and removes it -- and it would break for them, not for you,",
        "with no path back to the cause. That has happened twice here.",
        "",
        remedy,
        "",
        "If you really do mean to install from this directory, set",
        f"{OVERRIDE_ENV}=1 and re-run.",
        "",
    ))


def check_editable_source(source_dir: str | os.PathLike[str] | None = None) -> None:
    """Raise :class:`EditableInstallFromWorktree` unless an editable install
    of ``source_dir`` is safe.

    PEP 517 requires the frontend to invoke every hook with the working
    directory set to the root of the source tree, which is why the default is
    ``.`` -- the backend is never told the source directory any other way.
    """
    if os.environ.get(OVERRIDE_ENV, ""):
        return
    source = Path(source_dir) if source_dir is not None else Path.cwd()
    verdict, detail = classify_checkout(source)
    if verdict == PRIMARY:
        return
    message = _refusal_message(source, verdict, detail)
    # Printed as well as raised: a frontend is free to render a backend
    # exception as one line, and the remedy is the whole point of the failure.
    print(message, file=sys.stderr, flush=True)
    raise EditableInstallFromWorktree(message)


def _setuptools():
    """The wrapped backend, imported on use rather than at module scope.

    Import-time would be the ordinary spelling, but this module is also
    imported by ``setup_tools`` for :func:`classify_checkout`, and that runs on
    a machine whose whole problem may be that packaging is not installed yet.
    A guard that cannot be imported is a guard that gets deleted.
    """
    from setuptools import build_meta
    return build_meta


# ── PEP 517: hooks that cannot produce an editable install ──────────────
# Delegated verbatim. Building a wheel or an sdist from a worktree copies
# files out of it and records nothing about where they came from, so it is
# unaffected by the directory later being removed.

def get_requires_for_build_wheel(config_settings=None):
    return _setuptools().get_requires_for_build_wheel(config_settings)


def get_requires_for_build_sdist(config_settings=None):
    return _setuptools().get_requires_for_build_sdist(config_settings)


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return _setuptools().prepare_metadata_for_build_wheel(
        metadata_directory, config_settings)


def build_wheel(wheel_directory, config_settings=None,
                metadata_directory=None):
    return _setuptools().build_wheel(
        wheel_directory, config_settings, metadata_directory)


def build_sdist(sdist_directory, config_settings=None):
    return _setuptools().build_sdist(sdist_directory, config_settings)


# ── PEP 517: the editable hooks, each guarded ───────────────────────────
# All three, rather than only `build_editable`. A frontend may call any of
# them first -- pip starts at `get_requires_for_build_editable`, and a
# metadata-only resolve never reaches `build_editable` at all -- so guarding
# one hook would leave the refusal dependent on which tool asked.

def get_requires_for_build_editable(config_settings=None):
    check_editable_source()
    return _setuptools().get_requires_for_build_editable(config_settings)


def prepare_metadata_for_build_editable(metadata_directory,
                                        config_settings=None):
    check_editable_source()
    return _setuptools().prepare_metadata_for_build_editable(
        metadata_directory, config_settings)


def build_editable(wheel_directory, config_settings=None,
                   metadata_directory=None):
    check_editable_source()
    return _setuptools().build_editable(
        wheel_directory, config_settings, metadata_directory)
