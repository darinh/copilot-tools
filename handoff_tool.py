#!/usr/bin/env python3
"""Atomic session handoff for Copilot CLI agents.

Writes this instance's handoff and raises the operator's restart marker so the
loop picks up a fresh session. Cross-platform.

The handoff is keyed by **instance**, at
``~/.operator/projects/{guid}/handoff/{instance}.md``. It used to be one
``next-session.md`` per project, and that single mis-keying is what most of
this module used to be about: a lock, an unbounded ``superseded/`` archive,
three notice banners, and a rule for telling an unread predecessor from a
peer's mid-publish copy. A handoff is written in the first person -- *my*
worktree, *I* claimed this, check *my* inbox -- so it is instance-scoped by
construction; storing it at a project key meant peers sharing a checkout all
published to one mailbox while restarting on their own signals.

One writer per file removes the race rather than arbitrating it. What survives
is one bounded case: a handoff still sitting unread when the next one is
written is moved aside to ``{instance}.prev.md`` first. That cannot happen in
the ordinary flow, because the reader deletes the file -- so finding one there
means a session ended without its predecessor's context ever being picked up.

Differences from the bash predecessor:

* Catalog paths are compared case-insensitively on Windows, where
  ``C:\\Users\\x`` and ``c:\\users\\x`` denote the same directory.
* Instance inference compares real paths rather than string prefixes, so
  ``/srv/app2`` is no longer treated as living under ``/srv/app``.
* The transitional dual-write to the legacy ``~/.copilot/restart`` path is not
  carried over; operator state has lived under ``~/.operator`` for some time.
"""
from __future__ import annotations

import argparse
import os
import platform
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# An editable install freezes the module list into its import finder, so a
# module added to this directory after the last `pip install -e .` is invisible
# to the installed `handoff` entry point even though the file sits right here.
# Making our own directory importable turns that stale-install failure into a
# no-op instead of a traceback on startup.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from install_manifest import (                                # noqa: E402
    dir_present,
    path_present,
)
from operator_console import enable_utf8_output               # noqa: E402
from operator_mux import Mux, MuxError, safe_instance_id      # noqa: E402
from project_paths import (                                   # noqa: E402
    CATALOG_MISSING,
    CATALOG_UNREADABLE,
    CATALOG_UNUSABLE_ID,
    catalog_guid,
    # Re-exported, not used here: `copilot_operator` and the suite both reach
    # for `handoff_tool.guid_is_usable`, and one test asserts the two names are
    # the same object. Dropping it would move the definition, not remove it.
    guid_is_usable,  # noqa: F401
    normalized_key,
    primary_repo_root,
    project_dir,
    projects_root,
    resolved_str,
)

IS_WINDOWS = platform.system() == "Windows"
# `projects_root()` resolves `Path.home()` on every call, but this line runs
# once, at IMPORT time -- so the home directory this path was built from is
# whichever one was in effect when the module was first imported, not the one
# in effect when the catalog is read. A test that relocates home afterwards
# (monkeypatching `Path.home`, or `HOME`/`USERPROFILE`) moves the *functions*
# and leaves this constant pointing at the real one. Patch `handoff_tool.CATALOG`
# itself, which is what the tests here do and what conftest's guard tells you
# to do when it catches a write to the live catalog.
CATALOG = projects_root() / "catalog.csv"

#: One handoff per *instance*, under this directory inside the project dir.
#:
#: The handoff used to be one ``next-session.md`` per project, and almost every
#: piece of machinery that used to live in this module existed to compensate
#: for that: a lock, a ``superseded/`` archive nothing pruned, three notice
#: banners, an authorship stamp and a rule for interpreting it, and several
#: hundred words of instructions teaching agents to tell two cases apart.
#:
#: None of it was wrong. All of it was scaffolding holding up a type error. A
#: handoff is written in the first person -- *my* worktree, *I* claimed this,
#: check *my* inbox -- so it is instance-scoped by construction, and storing it
#: at a project key meant every instance sharing a checkout published to one
#: mailbox while restarting on its own signal. Whichever instance restarted
#: next read whatever was there, and the protocol then had that reader delete
#: it.
#:
#: Keyed by instance there is one writer per file. The race is gone, "is this
#: mine?" is answered by the filename, and a read can no longer consume a
#: peer's context.
HANDOFF_DIRNAME = "handoff"

#: Where a handoff goes when it is still sitting unread as the next one is
#: written. Exactly one slot per instance, replaced each time.
#:
#: This is the one preservation case that survives the re-keying, and it is a
#: different animal from the pile-up that ``superseded/`` was built for. That
#: one was routine -- peers overwriting each other was the *ordinary* flow, so
#: the archive grew without bound and needed a promise never to prune it. This
#: one cannot happen in the ordinary flow at all: the reader deletes the file,
#: so finding one still there means a session ended without its predecessor's
#: handoff ever being picked up. That is an anomaly worth keeping, and keeping
#: one of is enough -- a second consecutive miss is a broken loop, not a
#: context to rescue.
PREV_SUFFIX = ".prev.md"

#: Where the project-keyed files are moved to when this layout first appears.
#: Nothing is deleted: a banked handoff is dropped context, which is the exact
#: failure this module spends its length avoiding.
LEGACY_DIRNAME = "legacy"

# The one fact a reader of one of these files cannot recover from its *bytes*:
# **who wrote this**.
#
# The filename answers it now -- `handoff/{instance}.md` -- so this is no
# longer load-bearing for the reader who finds the file in place, and the
# whole apparatus that used to hang off it is gone: the "is this mine?" check,
# the three notice banners, and the prose teaching agents to tell an unread
# predecessor from a peer's mid-publish copy.
#
# It is kept for two narrower reasons, both measured rather than imagined.
# First, the migration below needs it: a project-keyed `next-session.md`
# written before this change has nothing *but* the stamp to say which
# instance's mailbox it belongs in. Second, a handoff that gets copied or
# rescued out of its directory loses the filename and keeps the bytes -- which
# happened on this machine on 2026-08-05, when a supervisor running older code
# wrote to the pre-migration path and its handoff had to be carried across by
# hand.
#
# It cannot drift from the filename: both are derived from one instance name
# at one moment, in `main` below.
AUTHOR_PREFIX = "*Written by operator instance:"


def author_line(instance: str) -> str:
    """The authorship stamp for a handoff written by ``instance``."""
    return f"{AUTHOR_PREFIX} `{instance}`*"


def authoring_instance(text: str) -> str | None:
    """The instance named in ``text``'s authorship stamp, or ``None``.

    ``None`` is "this document does not say", which is a real state and not a
    failure: every handoff written before the stamp existed is unattributed,
    and so is the synthetic document that records a replaced symlink. Callers
    must keep it distinct from a name -- an unattributed predecessor licenses
    no claim about who wrote it, in either direction.

    Only the header is searched -- the lines above the first ``##`` section.
    The stamp is metadata about the document, so a line in the *body* that
    happens to begin with the prefix is a quotation, not a claim of
    authorship, and a handoff quoting this very paragraph must not be able to
    rewrite its own attribution.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            return None
        if not stripped.startswith(AUTHOR_PREFIX):
            continue
        name = stripped[len(AUTHOR_PREFIX):].strip()
        if name.endswith("*"):
            name = name[:-1].strip()
        if len(name) >= 2 and name.startswith("`") and name.endswith("`"):
            # Delimited, so what sits between the ticks is exactly the name
            # and nothing further may be trimmed. `str.strip` is greedy and
            # would eat a backtick or a space the name legitimately carries --
            # `--instance` is free text -- and every consumer compares this
            # for equality against a live instance name, so a silently
            # shortened one attributes the handoff to an agent that does not
            # exist while looking exactly like a successful read.
            return name[1:-1] or None
        return name or None
    return None


def state_dir() -> Path:
    override = os.environ.get("COPILOT_OPERATOR_HOME")
    root = Path(override) if override else Path.home() / ".operator"
    return root / "restart"


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def normalize(path) -> str:
    """This module's spelling of :func:`project_paths.normalized_key`.

    The folding rule has one implementation; ``IS_WINDOWS`` is passed rather
    than read there so that a test patching this module's flag still steers it.
    """
    return normalized_key(path, windows=IS_WINDOWS)


def same_or_within(child: str, parent: str) -> bool:
    """True when child is parent or lives beneath it, comparing path parts."""
    try:
        cp = Path(child)
        pp = Path(parent)
    except (TypeError, ValueError):
        return False
    if cp == pp:
        return True
    try:
        cp.relative_to(pp)
        return True
    except ValueError:
        return False


def handoff_dir(proj_dir: Path) -> Path:
    """Where this project's per-instance handoffs live."""
    return proj_dir / HANDOFF_DIRNAME


def handoff_path(proj_dir: Path, instance_id: str) -> Path:
    """This instance's handoff file.

    ``instance_id`` must already have been through
    :func:`operator_mux.safe_instance_id`. It is the same identifier the
    restart marker is named for, deliberately: the marker and the mailbox are
    now keyed the same way, which is precisely what they were not before.
    """
    return handoff_dir(proj_dir) / f"{instance_id}.md"


def bank_prior_handoff(handoff_file: Path) -> Path | None:
    """Move an unread handoff aside, returning where it went.

    Returns ``None`` when there was nothing to move, which is the ordinary
    case: the reader deletes the file, so a handoff sitting here as the next
    one is written means a session ended without its predecessor's context
    being picked up.

    One slot, replaced each time, rather than an archive that grows. The
    unbounded version of this needed a promise never to prune it, because
    under project keying peers overwrote each other constantly and every file
    in there might have been somebody's only copy. Under instance keying a
    second consecutive miss means the read side is broken, and keeping the
    older of two undelivered handoffs does not fix that.

    ``os.replace`` rather than a copy: the point is that the bytes are never
    in one place only, and a copy leaves a window where they are.
    """
    try:
        if not handoff_file.is_file():
            return None
    except OSError:
        # Unexaminable is not absent. Falling through to the move is safe --
        # it either works or raises below -- whereas concluding "nothing to
        # bank" here would let the write overwrite a file we could not read.
        pass
    prev = handoff_file.with_name(handoff_file.stem + PREV_SUFFIX)
    try:
        os.replace(handoff_file, prev)
    except FileNotFoundError:
        return None
    except OSError as exc:
        die(f"A handoff is already waiting at {handoff_file} and could not be "
            f"moved aside ({exc}). Refusing to overwrite it.")
    return prev


def migrate_project_handoff(proj_dir: Path) -> list[str]:
    """Move project-keyed handoff state into the per-instance layout.

    Returns one human-readable line per thing moved, for the caller to report.

    Nothing is deleted and nothing is overwritten. A banked handoff is dropped
    context -- that is the failure this whole module is built around -- so a
    migration that tidied one away would be the same bug wearing the fix's
    clothes.

    ``next-session.md`` is *delivered* where it can be: if it names its author
    and that instance has no handoff waiting, it becomes that instance's
    handoff and the next session picks it up normally. Otherwise it goes to
    ``handoff/legacy/`` with everything from ``superseded/``, where it is
    preserved but not claimed to belong to anyone. Guessing a recipient would
    put a document written in the first person in front of an agent it is not
    about, which is the exact harm the re-keying exists to end.
    """
    moved: list[str] = []
    legacy_root = handoff_dir(proj_dir) / LEGACY_DIRNAME

    def _park(src: Path, name: str) -> None:
        try:
            legacy_root.mkdir(parents=True, exist_ok=True)
            dest = legacy_root / name
            # Held from before the loop. Re-reading them from `dest` reads back
            # the suffix just appended, so a third collision produces
            # `x-1-2.md` rather than `x-3.md` -- names that still avoid the
            # collision, and still say nothing about which file they came
            # from, which is the only thing a parked file has left.
            stem, ext = dest.stem, dest.suffix
            suffix = 1
            while dest.exists():
                dest = legacy_root / f"{stem}-{suffix}{ext}"
                suffix += 1
            os.replace(src, dest)
        except OSError as exc:
            print(f"Warning: could not move {src} to {legacy_root}: {exc}",
                  file=sys.stderr)
            return
        moved.append(f"{src.name} -> {dest}")

    old = proj_dir / "next-session.md"
    try:
        present = old.is_file()
    except OSError:
        present = False
    if present:
        author = None
        try:
            author = authoring_instance(
                old.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
        delivered = False
        if author:
            target = handoff_path(proj_dir, safe_instance_id(author))
            try:
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(old, target)
                    moved.append(f"next-session.md -> {target} "
                                 f"(attributed to {author!r})")
                    delivered = True
            except OSError as exc:
                print(f"Warning: could not deliver {old} to {target}: {exc}",
                      file=sys.stderr)
        if not delivered:
            _park(old, "next-session.md")

    old_archive = proj_dir / "superseded"
    try:
        # ``is_dir()`` and ``iterdir()`` both follow links, so a ``superseded``
        # that is a symlink or a junction would have this loop move files out
        # of whatever directory it points at -- a migration of one project's
        # state reaching into somewhere it was never told about. A link is not
        # something this tool put there, so the safe reading is that it is not
        # ours to move.
        if old_archive.is_symlink():
            print(f"Warning: {old_archive} is a link, not a directory; "
                  f"leaving it alone.", file=sys.stderr)
            entries: list[Path] = []
        else:
            entries = sorted(old_archive.iterdir()) if old_archive.is_dir() else []
    except OSError as exc:
        print(f"Warning: could not read {old_archive}: {exc}", file=sys.stderr)
        entries = []
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        _park(entry, f"superseded-{entry.name}")
    return moved


def write_atomic(path: Path, text: str) -> None:
    """Replace `path` with `text` in one indivisible step.

    The reader of this file deletes it once it has been read, so a torn write
    is not cosmetic: it is context the next agent can never recover. Writing a
    sibling temp file and renaming over the target means a reader sees the
    whole old file or the whole new one, never half of either.

    The temp file is a sibling because ``os.replace`` is only atomic within one
    filesystem, and its name is random because two agents working the same
    project can hand off at the same moment -- a shared temp name would let
    them interleave into one corrupt file, which is the very failure this
    function exists to prevent. A pid would be *nearly* unique; it repeats
    across containers sharing a mounted home, which is precisely where two
    agents are most likely to collide.
    """
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        die(f"Cannot write handoff {path}: {exc}")


def resolve_guid(project_root) -> str:
    """The project id the catalog records for ``project_root``, or exit.

    The lookup itself lives in :func:`project_paths.catalog_guid`, which
    ``backlog_tool`` also uses. Only the phrasing is here: each failure gets
    the instruction that fits it, and an instruction delivered for the wrong
    situation is worse than none -- "catalog not found" against a catalog that
    is merely unreadable sends the reader off to create a file that is already
    sitting there.
    """
    found = catalog_guid(project_root, CATALOG)
    if found.guid is not None:
        return found.guid
    target = normalize(project_root)
    if found.reason == CATALOG_MISSING:
        die(f"Catalog not found: {CATALOG}")
    if found.reason == CATALOG_UNREADABLE:
        die(f"Cannot read catalog {CATALOG}: {found.detail}")
    if found.reason == CATALOG_UNUSABLE_ID:
        die(f"Catalog entry for {target} has an unusable "
            f"project id: {found.detail!r}\n"
            f"The second column must be one plain directory "
            f"name, such as a GUID. Fix the line to read:\n"
            f"  \"{resolved_str(project_root)}\",<guid>")
    die(f"No catalog entry for: {target}\n"
        f"This project is not registered, so there is nowhere to write the "
        f"handoff. Register it by adding a line to\n"
        f"  {CATALOG}\n"
        f"such as:\n"
        f"  \"{resolved_str(project_root)}\",<guid>\n"
        f"The second column is the project directory name under "
        f"{projects_root()}; nothing in this toolkit writes the catalog, so "
        f"the line has to be added by hand.")


class StateUnreadable(Exception):
    """The set of managed instances could not be determined.

    Distinct from "there are none", and raised rather than returned so that no
    caller can spend it as an empty set. A census that could not be taken is
    the failure mode that removes a live peer from a list.
    """


def managed_ids() -> set[str]:
    found: set[str] = set()
    d = state_dir()
    kind = dir_present(d)
    if kind is None:
        raise StateUnreadable(f"cannot examine {d}")
    if kind is False:
        # Not usable as a directory -- but that covers two different worlds.
        # `dir_present` follows symlinks and reports a dangling one as False,
        # and an exception type describes what the call did, not what is on
        # disk. Only a path with nothing at it at all is an empty population;
        # anything else is a restart directory we failed to read.
        if path_present(d) is False:
            return found
        raise StateUnreadable(f"{d} exists but is not a usable directory")
    try:
        entries = list(d.iterdir())
    except OSError as exc:
        # Listing can fail after the probe said "directory" -- a revoked
        # permission, a disconnected network home. Returning what we had read
        # so far would report a partial census as a complete one.
        raise StateUnreadable(f"cannot list {d}: {exc}") from exc
    for path in entries:
        if path.suffix in (".managed", ".state"):
            found.add(path.name.rsplit(".", 1)[0])
    return found


def infer_instance(project_root, mux: Mux) -> str | None:
    target = resolved_str(project_root)
    try:
        managed = managed_ids()
    except StateUnreadable as exc:
        die(f"Cannot tell which operator instances are managed: {exc}\n"
            f"Guessing here would either invent an instance or declare that "
            f"none is running, and both write the restart marker to the "
            f"wrong name.\nName it explicitly with --instance NAME")
    matches = []
    try:
        sessions = mux.list_sessions()
    except MuxError:
        return None
    for session in sessions:
        if session not in managed:
            continue
        cwd = mux.pane_current_path(session)
        if cwd and same_or_within(cwd, target):
            matches.append(session)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print("Multiple operator instances found for this project:", file=sys.stderr)
        for m in matches:
            print(f"  {m}", file=sys.stderr)
        print("Specify one with --instance NAME", file=sys.stderr)
        sys.exit(1)
    return None


CLEAN_OVERRIDE = "--allow-dirty"

#: How many leftover paths either message names before it starts counting.
#: One constant, not two literals: the refusal and the recorded notice must
#: truncate at the same point, or the handoff quietly claims a shorter mess
#: than the agent was just shown.
LEFTOVER_LIMIT = 20

#: Control characters, escaped before a path is shown to anyone.
_CONTROL_ESCAPES = {c: f"\\x{c:02x}" for c in range(0x20)}
_CONTROL_ESCAPES[0x7F] = "\\x7f"


def display_path(path: str) -> str:
    """A leftover path, safe to put in a message.

    ``-z`` gives paths unquoted and literal, which is the point -- a
    line-based reader turns one newline-containing filename into two paths
    that do not exist. But it means a filename is arbitrary bytes arriving
    in a refusal on stderr and in a markdown blockquote in the published
    handoff, and a newline there forges a whole line: another bullet in the
    list, or an escape from the blockquote into text that reads like the
    tool's own words. POSIX permits every byte but ``/`` and NUL in a
    filename, so this is a real input, not a hypothetical one.

    Escaping control characters closes the forgery, because every markdown
    structure this text uses is line-initial. A backtick can still end its
    code span early, which is a visual defect confined to one line and
    cannot manufacture structure -- stated rather than silently accepted.
    """
    return path.translate(_CONTROL_ESCAPES)


#: Refusing costs a session its context, so the bar for refusing has to be
#: something the tool can *see*, not something it infers. These two are:
#: uncommitted tracked changes, and untracked paths. Both are recoverable by
#: the agent in the seconds before it hands off, and neither is recoverable
#: afterwards -- the successor inherits a tree it did not create, cannot tell
#: which of the mess is load-bearing, and (measured, twice, in this project's
#: own history) either commits somebody else's work or stashes it away.
NOTICE_DIRTY = (
    "> **This handoff was written with `--allow-dirty`.** The checkout was "
    "not clean when the session ended, and the paths below were left behind "
    "by the previous agent rather than by you. Nothing here is necessarily "
    "safe to delete, commit or stash -- ask before you act on any of it."
)


def _git(root, *args: str, stdin: str = "",
         ok_codes: "tuple[int, ...]" = (0,)) -> "tuple[bool, str]":
    """Run git in ``root``. Returns (it answered, stdout).

    A git that could not answer is never reported as a clean tree. "No
    information" and "nothing to report" are the same string to a caller that
    only looks at the length of a list, and they are opposite facts.

    ``ok_codes`` exists because a non-zero exit is not always a failure:
    ``git check-ignore`` exits 1 to mean "nothing matched", which is a real
    answer with empty output, and treating it as a failure is how the ignore
    filter silently stops filtering.
    """
    try:
        proc = subprocess.run(("git", *args), cwd=str(root),
                              input=stdin, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return False, ""
    if proc.returncode not in ok_codes:
        return False, ""
    return True, proc.stdout


def holds_no_files(directory: Path, budget: int = 512) -> bool:
    """True when ``directory`` contains directories and nothing else.

    Ported deliberately from ``extensions/checkout-guard/guard.mjs``
    (``holdsNoFiles``), including the traversal budget and the decision that
    an *unreadable* directory is not an empty one. The extension is loaded
    per CLI session, so it speaks only for agents whose runtime happened to
    load it; this tool is the choke point every agent passes through on the
    way out. If the two ever disagree the extension is the reference.
    """
    stack = [directory]
    visited = 0
    while stack:
        visited += 1
        if visited > budget:
            return False
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            # Unreadable is not empty, and guessing either way would be a
            # claim the filesystem declined to support.
            return False
        for entry in entries:
            try:
                # `entry.is_symlink() or not entry.is_dir()` rather than
                # `is_dir(follow_symlinks=False)`: identical on every input
                # (a symlink, dangling or not, is never descended), and the
                # kwarg spelling is 3.13+ on `pathlib.Path`. The floor scan
                # is keyword-gated on the method name alone, so it cannot
                # tell this `os.DirEntry` -- where the kwarg has been legal
                # since 3.6 -- from a `Path`. Narrowing the scan to let this
                # through would cost it every `Path` bound to a variable,
                # which is the shape it exists to catch.
                if entry.is_symlink() or not entry.is_dir():
                    return False
            except OSError:
                return False
            stack.append(Path(entry.path))
    return True


def scan_checkout(root) -> "list[str] | None":
    """Every path in ``root`` that the next session should not inherit.

    ``None`` means git could not answer, which a caller must treat as "no
    information" rather than "clean".

    ``-uall`` matters: the default collapses an untracked directory to its
    name, so a scratch directory holding fifty files reports as one entry and
    a *reproduction* holding none reports identically to a typo. ``-z``
    matters because a filename may contain a newline, and a line-based reader
    turns one such path into two paths that do not exist.

    Empty directories are handled separately because **git does not report
    them at all** -- there is no blob, so there is nothing to report. That is
    the blind spot that let this project's own agents leave nine artifacts in
    a shared checkout while `git status` said clean, and it is precisely the
    class of failure prose cannot close.

    ``--ignored=matching`` is asked for so that the same single call supplies
    the prune set for that pass: git reports an ignored directory collapsed
    to its own name (``!! node_modules/``) without descending into it, and it
    reports an ignored directory that is *empty* too. Deriving the prune set
    any other way means one ``check-ignore`` per candidate, which is what
    kept the empty-directory pass to the top level of the checkout. Old git
    that does not know the option answers nothing, so the plain form is tried
    after it -- losing the empty-directory half rather than the whole guard.
    """
    answered, out = _git(root, "status", "--porcelain", "-uall",
                         "--ignored=matching", "-z")
    ignored_known = answered
    if not answered:
        answered, out = _git(root, "status", "--porcelain", "-uall", "-z")
    if not answered:
        return None
    records = [r for r in out.split("\0") if r]
    trees = _linked_worktrees(root)
    if trees is None:
        # An unanswered question is not an answer of "none". Without the list
        # there is no way to tell a peer's checkout from a stray, and guessing
        # either way is worse than saying so: guessing "none" reports a peer's
        # tree as litter to delete, and guessing "everything" hides real
        # artifacts. The caller already treats None as "no information".
        return None
    paths: "list[str]" = []
    ignored: "list[str]" = []
    skip_next = False
    for record in records:
        if skip_next:            # the second half of a rename/copy record
            skip_next = False
            continue
        if len(record) < 4:
            continue
        code, path = record[:2], record[3:]
        # `R` and `C` are the two status letters whose record occupies *two*
        # NUL-separated fields, and the second arrives bare -- no status
        # prefix. Slicing three characters off it reports `name.txt` for
        # `oldname.txt`: a path that never existed, handed to an agent as
        # litter to go and remove.
        #
        # The index column only, measured rather than assumed: an *unstaged*
        # rename is not detected as one, it is reported as ` D old` plus
        # `?? new`, two ordinary one-field records. Testing the worktree
        # column as well looks like defence and is the opposite -- a stray
        # `R` there would consume the following real record, turning a
        # false positive into a silent miss. `C` needs `status.renames=copies`,
        # which is somebody's config away.
        if code[0] in "RC":
            skip_next = True
        if code == "!!":
            ignored.append(path)
        elif code == "??" and _under_any(path, trees):
            # A linked worktree is untracked from the parent's point of view,
            # so `??` is the only code a peer's checkout can arrive under, and
            # exempting just that one keeps the rest of the namespace visible.
            # Filtering every code instead was measured hiding
            # ` M .worktrees/owned.txt` -- tracked repository content, modified
            # and then reported clean. A repository is free to track something
            # under this name, and a guard that goes quiet about a modification
            # is the failure it exists to prevent.
            continue
        else:
            paths.append(path)
    if not ignored_known:
        return paths
    return paths + _empty_dir_strays(root, ignored, trees)


#: The directory the operator toolchain keeps linked worktrees in, relative to
#: the checkout root.
#:
#: Duplicated from ``operator_worktree.WORKTREES_DIR`` rather than imported:
#: this module is the handoff, and it is reached on the path where a session is
#: already ending. Importing the worktree command would pull the work database
#: and its dependencies in behind it, so a failure anywhere in that import
#: chain would take the handoff with it -- trading the thing that preserves a
#: session's context against a constant. ``test_handoff_checkout_guard.py``
#: asserts the two spellings are equal, so the copy cannot drift silently.
WORKTREES_DIR = ".worktrees"


def _in_worktrees(rel: str) -> bool:
    """Whether ``rel`` names the worktrees directory itself.

    Anchored at the checkout root, exactly like the ``/.worktrees/`` ignore
    rule ``operator worktree new`` writes: a ``docs/.worktrees/`` that somebody
    meant to track is a different directory and is still reported.

    This answers for the *container* only, and nothing inside it. Everything
    inside is judged by :func:`_linked_worktrees`, which asks git which paths
    are really checkouts -- so a stray an agent leaves at
    ``.worktrees/scratch.txt``, or an empty ``.worktrees/not-a-worktree/``, is
    still reported. An earlier draft exempted the whole namespace by name and
    hid exactly those, which is the direction this guard cannot afford: it
    reads as a clean tree.

    Only a path with no separator in it can be the container, so the check
    rejects those first and ``normcase`` never sees a separator. That matters
    because ``ntpath.normcase`` rewrites ``/`` to ``\\`` as well as lowering
    case, and a comparison that let the two concerns meet would answer wrongly
    on exactly one platform.

    Case is left to ``os.path.normcase`` -- the running platform's own answer,
    and the right one here because this string came from this machine's own
    ``scandir``, not from a record naming some other platform's syntax.
    """
    norm = rel.strip("/")
    if "/" in norm:
        return False
    return os.path.normcase(norm) == os.path.normcase(WORKTREES_DIR)


def _linked_worktrees(root) -> "set[str] | None":
    """Every linked worktree registered inside ``root``, relative to it.

    ``None`` means git could not answer, which a caller must treat as "no
    information" rather than "there are none" -- the same rule the rest of
    this module follows. Spending an unanswered question as an empty set here
    would report a peer's checkout as litter on every command afterwards,
    which is the failure this function exists to prevent.

    Identity, not name. The reason is that a name test cannot tell a checkout
    from an ordinary directory that happens to sit in the same place, so it
    exempts strays as readily as peers; git knows which paths are actually
    worktrees and is the only thing that does. It also removes any dependence
    on how the path is spelled or cased, and covers a worktree created
    somewhere the convention does not reach -- ``git worktree add <anywhere>``
    makes that easy, and this is what
    ``extensions/checkout-guard/guard.mjs`` (``nestedWorktreePrefixes``)
    already did on the JS side.

    The primary checkout is the first record and is dropped: it is ``root``
    itself, and keeping it would exempt the entire tree.
    """
    answered, out = _git(root, "worktree", "list", "--porcelain")
    if not answered:
        return None
    try:
        root = Path(root).resolve()
    except (OSError, RuntimeError, ValueError):
        # Every relative path below is computed against this one, so a root
        # that cannot be resolved is not a shorter answer -- it is no answer.
        # None rather than an empty set, for the reason in the docstring.
        return None
    found: "set[str]" = set()
    for line in out.splitlines():
        if not line.startswith("worktree "):
            continue
        try:
            tree = Path(line[len("worktree "):].strip()).resolve()
            rel = tree.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            # `relative_to` raises `ValueError` for a worktree outside this
            # checkout, which is not an error: it is simply not ours to
            # exempt. `resolve` adds `OSError` on a denial and `RuntimeError`
            # on a symlink loop -- all three mean this record cannot be placed,
            # and an unplaceable record is one this checkout does not own.
            continue
        if not rel.parts:
            continue                      # the primary checkout, i.e. `root`
        found.add("/".join(rel.parts))
    return found


def _under_any(rel: str, prefixes: "set[str]") -> bool:
    """Whether ``rel`` is one of ``prefixes`` or sits inside one.

    Compared segment-wise rather than with ``startswith`` so that
    ``.worktrees/feat-a2`` is not read as living under ``.worktrees/feat-a``.
    """
    parts = tuple(p for p in rel.strip("/").split("/") if p)
    for prefix in prefixes:
        pp = tuple(p for p in prefix.split("/") if p)
        if parts[:len(pp)] == pp:
            return True
    return False


#: How many directories the candidate walk will visit before it gives up.
#: Exceeding it costs findings, never invents them -- an unvisited subtree is
#: never reported as empty.
WALK_BUDGET = 4096


#: Windows' junction reparse tag, spelled out rather than read from ``stat``
#: alone. ``stat.IO_REPARSE_TAG_MOUNT_POINT`` exists only on Windows, so a
#: ``getattr`` default of ``object()`` made the comparison below unequal to
#: everything on POSIX -- not because the entry was not a junction, but
#: because the constant was absent. That is a *different rule on each leg*,
#: and it kept `test_only_the_mount_point_tag_counts_as_a_junction` red on
#: every Linux and macOS run while passing on Windows: the test exists
#: precisely to exercise this comparison where no real junction can be made.
#: The value is fixed in the Windows ABI and cannot drift.
IO_REPARSE_TAG_MOUNT_POINT = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT",
                                     0xA0000003)


def _is_junction(entry) -> bool:
    """Whether a directory entry is a Windows junction.

    ``is_symlink()`` answers False for one and ``is_dir()`` answers True, so
    the walk descends a junction as though it were an ordinary directory --
    which is how a *dangling* one became invisible. Git emits ``warning:
    could not open directory`` on **stderr** and reports nothing on stdout, so
    ``git status`` calls the tree clean; the walk then hit ``OSError`` and, by
    the rule that unreadable is not empty, dropped it too. Both halves agreed,
    and neither had looked.

    ``os.DirEntry.is_junction()`` would say this in one call but arrived in
    3.12, above this project's floor. ``st_reparse_tag`` is Windows-only and
    3.8, so ``AttributeError`` is the POSIX answer, not a failure.
    """
    try:
        tag = entry.stat(follow_symlinks=False).st_reparse_tag
    except (OSError, AttributeError, ValueError):
        return False
    return tag == IO_REPARSE_TAG_MOUNT_POINT


def _empty_dir_strays(root, ignored: "list[str]",
                      trees: "set[str]") -> "list[str]":
    """Untracked directories holding no files, which git never reports.

    ``ignored`` is git's own collapsed answer (``!! node_modules/``), used to
    prune. An unfiltered walk is not a rougher answer -- it is every ignored
    build directory in the project, handed to an agent as litter, plus the
    time to walk `node_modules`.

    ``trees`` is the registered linked worktrees, which are neither reported
    nor descended: they are other checkouts, and each one's own handoff sees
    its own contents.

    Only the *outermost* empty directory is reported. Naming ``a/``, ``a/b/``
    and ``a/b/c/`` for one stray describes one mistake three times, and the
    two inner entries vanish the moment the outer one is removed.
    """
    root = Path(root)
    prune = {p.rstrip("/") for p in ignored}
    candidates: "list[str]" = []
    links: "list[str]" = []
    visited = 0
    stack = [""]
    while stack:
        rel = stack.pop()
        visited += 1
        if visited > WALK_BUDGET:
            break
        try:
            entries = list(os.scandir(root / rel if rel else root))
        except OSError:
            # Unreadable is not empty. Its children are unknown, so it is not
            # a candidate and neither is anything under it.
            continue
        for entry in entries:
            # `.git` is excluded belt-and-braces. It cannot actually reach a
            # finding: in an ordinary repository it holds files from the
            # moment `git init` returns (HEAD, config, description), so
            # `holds_no_files` rejects it, and in a linked worktree it is a
            # *file* and never becomes a candidate at all.
            if entry.name == ".git":
                continue
            try:
                if entry.is_symlink() or not entry.is_dir():
                    continue
            except OSError:
                continue
            child = f"{rel}/{entry.name}" if rel else entry.name
            if child in prune:
                continue
            if _is_junction(entry):
                # Reported, never descended. Git cannot store a junction --
                # there is no tree entry for one -- so a junction inside a
                # checkout is always something a process left behind, and it
                # is the one artifact git can be silent about while the
                # filesystem still holds it. Descending it would also walk
                # out of the checkout, or round a cycle if it points at an
                # ancestor. A junction that is legitimate infrastructure
                # (`node_modules` pointed at a shared cache) is in
                # `.gitignore`, so `prune` above has already dropped it.
                #
                # Ordered before the worktrees exemption below, deliberately.
                # `git worktree add` makes an ordinary directory, so a
                # junction wearing that name is not the thing the exemption is
                # for -- and exempting by name first would let any junction be
                # hidden from this guard by choosing what to call it, a wider
                # hole than the one the exemption closes.
                links.append(child)
                continue
            if _in_worktrees(child):
                # The container itself, and only the container. `git worktree
                # remove` never removes this parent, so it outlives the trees
                # it held and -- in any repository whose `.gitignore` has not
                # got the rule yet -- becomes an empty untracked directory
                # that refuses every handoff from here on. It is created by
                # this toolchain, so the refusal is self-inflicted, and the
                # refusal text tells the agent to delete what it names.
                #
                # The walk still descends: anything inside that is not a
                # registered worktree is an ordinary stray and is reported as
                # one, so `.worktrees/not-a-worktree/` is still found.
                stack.append(child)
                continue
            if _under_any(child, trees):
                # A live worktree belongs to a peer. Reporting a scratch
                # directory inside it hands one agent another's tree as litter
                # to remove -- against the one rule this project states about
                # worktrees -- inside a refusal whose stated remedy is
                # deletion. Each checkout's own handoff sees its own contents,
                # so nothing goes unwatched.
                continue
            candidates.append(child)
            stack.append(child)
    found: "list[str]" = []
    for candidate in sorted(candidates):
        # The walk already appends a directory before any of its descendants,
        # so this sort is belt-and-braces rather than the thing that makes the
        # dedupe below correct -- mutating it away changes no observable
        # behaviour. It is kept because the invariant then lives on this line
        # instead of in a property of the loop above, which a later edit to
        # the traversal could quietly retire.
        if any(candidate.startswith(f"{f.rstrip('/')}/") for f in found):
            continue
        if holds_no_files(root / candidate):
            found.append(f"{candidate}/")
    return found + links


def checkout_complaints(root) -> "tuple[list[str], str]":
    """What is wrong with ``root``, and a one-line summary of how sure we are.

    Separated from the refusal so the refusal text, the override notice and
    the tests all read the same list rather than three re-derivations of it.
    """
    found = scan_checkout(root)
    if found is None:
        return [], "unknown"
    return sorted(found), "measured"


def render(status: str, in_progress: str, next_steps: str,
           context: str, prompt: str, notice: str = "",
           instance: str = "") -> str:
    """The handoff document.

    ``notice`` is a block the *tool* has to say about the circumstances of the
    write -- currently only :data:`NOTICE_DIRTY`, since instance keying
    removed the lock whose contended paths used to have their own. It sits
    under the title rather than above it so the file is still a handoff
    document to anything that keys on the ``# Session Handoff`` header, and
    above ``## Status`` so a reader meets it before the content it qualifies.

    ``instance`` is stamped in the same band, below the notice: the notice is
    about *this write* and may change what the reader does next, while the
    stamp qualifies every word in the file. Both are above ``## Status``, so a
    reader learns whose session this was before reading a document written
    throughout in the first person. An empty ``instance`` emits no stamp at
    all rather than an empty one -- see :func:`authoring_instance` on why
    "does not say" must stay distinct from a name.
    """
    parts = ["# Session Handoff", ""]
    if notice:
        parts += [notice, ""]
    if instance:
        parts += [author_line(instance), ""]
    parts += ["## Status", status, ""]
    if in_progress:
        parts += ["## In Progress", in_progress, ""]
    parts += ["## Next Steps", next_steps, ""]
    if context:
        parts += ["## Context", context, ""]
    if prompt:
        parts += ["## Prompt", prompt, ""]
    return "\n".join(parts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="handoff",
        description="Atomic session handoff for Copilot CLI agents.",
    )
    parser.add_argument("--instance", default="",
                        help="Operator instance name. Inferred when omitted.")
    parser.add_argument("--status", default="", help="What was just completed")
    parser.add_argument("--next", dest="next_steps", default="",
                        help="Prioritized next steps")
    parser.add_argument("--in-progress", dest="in_progress", default="",
                        help="What was actively being worked on")
    parser.add_argument("--context", default="",
                        help="Key decisions, gotchas, architectural notes")
    parser.add_argument("--prompt", default="",
                        help="Ready-to-execute prompt for the next session")
    parser.add_argument("--project-root", dest="project_root", default=None,
                        help="Project root (default: cwd), used for GUID lookup")
    parser.add_argument("--allow-dirty", dest="allow_dirty",
                        action="store_true",
                        help="Hand off even though the checkout is not clean. "
                             "Records the leftover paths in the handoff.")
    return parser


def main(argv: list[str] | None = None) -> int:
    enable_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.status:
        die("Missing required: --status")
    if not args.next_steps:
        die("Missing required: --next")

    project_root = Path(args.project_root or Path.cwd())
    root = dir_present(project_root)
    if root is False:
        die(f"Directory not found: {project_root}")
    if root is None:
        # Unexaminable, which is not the same as missing. Refusing here would
        # discard this session's context for certain, and that is the one
        # outcome this tool spends the rest of its length avoiding. Proceeding
        # cannot misfile the handoff: the destination comes from an exact
        # catalog match, so an unusable root fails the lookup instead of
        # matching the wrong row.
        print(f"Warning: cannot examine {project_root}; continuing on the "
              f"assumption it is there. If the catalog lookup below fails, "
              f"this is why.", file=sys.stderr)

    mux = Mux()
    instance_name = args.instance
    if not instance_name:
        inferred = infer_instance(project_root, mux) if mux.available() else None
        if not inferred:
            die("Cannot infer instance. Use --instance NAME")
        instance_name = inferred
        instance_id = inferred
    else:
        instance_id = safe_instance_id(instance_name)

    # A missing session is a warning, not a failure: the handoff file is still
    # worth writing so the next session can pick it up.
    if mux.available() and not mux.has_session(instance_id):
        print(f"Warning: no running session '{instance_name}' found. "
              "Handoff file will be written but restart may not trigger.",
              file=sys.stderr)

    # Checked here: after the arguments are known to be good and the instance
    # is named, and before anything at all has been written or locked. A
    # refusal at this point has cost nothing and changed nothing, so the
    # session that hits it can still fix the tree and run the same command
    # again.
    leftovers, certainty = checkout_complaints(project_root)
    dirty_notice = ""
    if leftovers and not args.allow_dirty:
        shown = "\n".join(f"    {display_path(p)}"
                          for p in leftovers[:LEFTOVER_LIMIT])
        more = (f"\n    ... and {len(leftovers) - LEFTOVER_LIMIT} more"
                if len(leftovers) > LEFTOVER_LIMIT else "")
        die(f"The checkout is not clean, so this handoff was not written.\n\n"
            f"{shown}{more}\n\n"
            f"  Handing off now makes these somebody else's problem, and they "
            f"will have no way\n"
            f"  to tell which of them matters. Commit what belongs to the "
            f"repository, delete\n"
            f"  what was scratch, and run the same command again.\n\n"
            f"  If they genuinely must be left, re-run with "
            f"{CLEAN_OVERRIDE} -- the handoff will\n"
            f"  say so and list them, so the next session is told rather than "
            f"left to guess.")
    if leftovers:
        listed = "\n".join(f"> - `{display_path(p)}`"
                           for p in leftovers[:LEFTOVER_LIMIT])
        if len(leftovers) > LEFTOVER_LIMIT:
            # The refusal text says "and N more"; this must too. A successor
            # handed twenty of sixty paths, with nothing saying so, will read
            # the list as complete and clean up two thirds of a mess.
            listed += (f"\n> - ... and {len(leftovers) - LEFTOVER_LIMIT} more")
        dirty_notice = f"{NOTICE_DIRTY}\n>\n{listed}"
    elif certainty == "unknown":
        print("Warning: could not ask git whether this checkout is clean; "
              "handing off without that check.", file=sys.stderr)

    guid = resolve_guid(primary_repo_root(project_root))
    proj_dir = project_dir(guid)
    handoff_file = handoff_path(proj_dir, instance_id)
    marker = state_dir() / instance_id

    # Even a validated guid can fail here -- a read-only home, a full disk, a
    # revoked permission. Report it the way the rest of the tool reports
    # trouble rather than with a bare traceback.
    try:
        handoff_file.parent.mkdir(parents=True, exist_ok=True)
        state_dir().mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        die(f"Cannot create {handoff_file.parent} or {state_dir()}: {exc}")

    for line in migrate_project_handoff(proj_dir):
        print(f"Migrated handoff state: {line}", file=sys.stderr)

    # The dirty-checkout notice is joined inside this closure rather than
    # passed at the call site. It arrived on `main` alongside a lock whose
    # contended paths each re-rendered with their own notice; instance keying
    # removed the lock, so there is one caller left. The closure is kept
    # anyway, because the argument for it never depended on the lock: a caller
    # that forgets to re-add `dirty_notice` drops the leftovers list from
    # exactly the handoff whose reader most needs it, and a closure that
    # cannot be called without it is the only arrangement where forgetting is
    # not possible.
    def render_body(notice: str = "") -> str:
        both = "\n>\n".join(n for n in (dirty_notice, notice) if n)
        return render(args.status, args.in_progress, args.next_steps,
                      args.context, args.prompt, notice=both,
                      instance=instance_name)

    body = render_body()

    banked = bank_prior_handoff(handoff_file)
    if banked is not None:
        # This instance's own previous handoff was never read. Under project
        # keying that was ambiguous -- it might have been a peer's -- and the
        # tool needed several paragraphs to help a reader tell which. Keyed by
        # instance there is only one explanation left: a session of this
        # instance ended without picking up what the one before it left.
        print(f"Warning: {instance_name!r} had an unread handoff at "
              f"{handoff_file}; a session ended without reading it.\n"
              f"         It has been moved to {banked}", file=sys.stderr)

    write_atomic(handoff_file, body)
    try:
        marker.touch()
    except OSError as exc:
        die(f"Handoff written, but cannot raise restart marker {marker}: {exc}")

    print(f"✅ Handoff written: {handoff_file}")
    print(f"✅ Restart signal: {marker}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
