#!/usr/bin/env python3
"""Atomic session handoff for Copilot CLI agents.

Writes ``next-session.md`` for the project and raises the operator's restart
marker so the loop picks up a fresh session. Cross-platform.

A handoff that is still sitting unread when the next one is written is copied
into ``superseded/`` beside it rather than overwritten -- see
``preserve_prior_handoff``. A handoff written on a contended path carries a
notice saying so in its own bytes -- see ``NOTICE_UNSERIALISED`` -- because the
stderr warnings belong to the session that is ending, and the party who needs
to know is the one that reads what was left behind.

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
import sys
import time
import uuid
from contextlib import contextmanager
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
# Where a handoff that was never read goes instead of into the bit bucket.
SUPERSEDED_DIRNAME = "superseded"
# The three facts a reader of one of these files cannot otherwise recover.
#
# Everything the tool knows about a contended handoff is currently said on
# stderr, which belongs to the session that is about to end -- so the one
# party who never learns of it is the next session, whose whole job is to read
# what is left behind. A file in `superseded/` looks the same whether it is an
# ordinary unread predecessor, a copy banked while a peer was mid-publish, or
# a handoff that was never published at all; only the last two mean "this may
# be newer than the `next-session.md` next to it".
#
# So the notice goes in the bytes. It survives the session, it travels with
# the file it describes, and it is addressed to the agent who finds it rather
# than to the one who caused it.
#
# **Each notice is written before its own outcome is known, so none of them
# may assert one.** Both are attached ahead of the events they describe: the
# published notice is chosen before the spare copy is attempted, and the
# banked notice is written before the publish is attempted -- and either can
# then fail. Adversarial review found both halves of that, independently: a
# published file claiming "a copy was banked" when the bank had just raised,
# and a banked copy claiming "this session published" when the publish was
# about to be abandoned. They are phrased as what was *attempted* and what the
# reader should therefore check, which is true on every path that reaches
# them.
#
# The same rule reaches backwards: **a notice may not assert the cause of the
# contention either.** ``handoff_lock`` yields False for three different
# reasons -- a lock still held at the deadline, a directory that will not take
# a lock file at all, and a lock file that could not be written and so was
# removed -- and only the first is even possibly a live peer, since a lock
# left by a process that died reads exactly like one held by a process that is
# working. A second review round found notices asserting "another handoff was
# in progress" on all three. What is knowable at the stamp site is that the
# lock was not taken, and that is what they say.
NOTICE_UNSERIALISED = (
    "> **⚠ Published without the handoff lock.** The lock that serialises\n"
    "> handoffs for this project could not be taken, so this file was written\n"
    "> unserialised: it may have overwritten a concurrent handoff — or been\n"
    "> overwritten by one since. Read `superseded/` alongside this file before\n"
    "> deciding what you are picking up: a copy in there marked as banked may\n"
    "> be newer than this one."
)
NOTICE_BANKED_UNSERIALISED = (
    "> **⚠ Banked copy — these words may never have reached\n"
    "> `next-session.md`.** The handoff lock could not be taken, so this\n"
    "> session banked its context here *before* attempting to publish\n"
    "> unserialised. If `next-session.md` does not contain these words, the\n"
    "> publish was abandoned or a concurrent handoff replaced it, and this\n"
    "> copy is the only one there is."
)
NOTICE_BANKED_UNPUBLISHED = (
    "> **⚠ Banked copy — this handoff was never published.** The handoff\n"
    "> already at `next-session.md` could not be preserved first, so it was\n"
    "> not replaced and these words were banked here instead. This copy is\n"
    "> newer than the `next-session.md` beside it."
)
# The fourth fact a reader cannot otherwise recover: **who wrote this**.
#
# `next-session.md` is keyed by PROJECT and the restart marker is keyed by
# INSTANCE, so every operator instance working a shared checkout publishes to
# one mailbox and restarts on its own signal. Whichever instance restarts next
# reads the file, whoever wrote it -- and the protocol then has that reader
# delete it, so the author's own next session finds nothing.
#
# Measured on this repository, not reasoned about: ten handoffs accumulated in
# one project's `superseded/` in a single day, written by at least three
# distinct instances (identified by each one telling its own next session to
# check its own `operator inbox <name>`, which is necessarily the author's).
#
# The document said nothing about which of them wrote it, and a handoff is
# written in the first person -- "my worktree", "I claimed it from a peer",
# "check operator inbox x". Read by the wrong instance those are not merely
# unhelpful, they are wrong instructions about branches it does not own and a
# mailbox whose read CONSUMES a peer's mail. And a reader with no author to
# consult has no way to reach that conclusion: a session that finds a pile in
# `superseded/` infers the documented cause, "these went unread", which fits
# the evidence perfectly and is the wrong explanation.
#
# So the author goes in the bytes, for the same reason the notices do. This
# does not make the mailbox per-instance -- project state genuinely is shared,
# and splitting it would hide a peer's work rather than attribute it. It makes
# the sharing VISIBLE, which is the part that was missing.
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


# A handoff is a few kilobytes of prose. Anything past this is not one, and
# slurping it to "preserve" it would be the denial of service, not the fix.
MAX_PRESERVE_BYTES = 8 * 1024 * 1024
# Preserve-then-publish takes milliseconds, so a peer that holds the lock is
# about to release it. Waiting far longer than the work takes costs nothing
# and closes the window in every realistic case.
LOCK_WAIT_SECONDS = 10.0
# Nothing legitimately holds this for two minutes. A lock older than that
# belongs to a process that died between creating it and removing it.
LOCK_STALE_SECONDS = 120.0
# O_NOFOLLOW: a regular file that turned into a symlink between the check and
# the open must not be followed. O_NONBLOCK: nor may one that turned into a
# fifo block the open forever, before there is any descriptor to inspect.
# Neither exists on Windows, where the size check below is the remaining
# guard; O_BINARY exists only there, and stops newline translation from
# rewriting bytes we promised to preserve verbatim.
_PRIOR_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


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


@contextmanager
def handoff_lock(handoff_file: Path):
    """Serialise preserve-then-publish for one project's handoff file.

    Preserving the old handoff and publishing the new one are two steps, and
    two agents can be inside them at once: A copies the predecessor aside, B
    copies the same predecessor aside and publishes, then A publishes over B.
    B's handoff was never in ``superseded/`` -- it was published after A looked
    -- so it is gone. Preserving without serialising just moves which session
    gets destroyed.

    ``O_CREAT|O_EXCL`` is the one create-if-absent primitive that is atomic on
    both POSIX and Windows. This duplicates the shape of
    ``copilot_operator._restart_handoff_lock`` rather than importing it: that
    module is the supervisor, it does work at import time, and a lock is not
    worth coupling the two.

    Yields True when the lock was taken and False when it was not. A caller
    that could not take it must still go ahead: refusing would discard the
    context of the session that is running *now*, which is a certain loss, to
    avoid a contended write that probably is not happening. What the caller
    owes in that case is insurance -- see ``main`` -- so that even the unlocked
    path cannot destroy a handoff without leaving a copy of it.
    """
    path = handoff_file.with_name(handoff_file.name + ".lock")
    # Whoever holds the lock says so in the file. A holder that unlinks on the
    # way out without checking would remove a lock somebody else has since
    # taken -- which is not theoretical here, because the reclaim below can
    # hand the lock to a second process while the first is still inside.
    token = uuid.uuid4().hex
    acquired = False
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # Age, not a pid: a stale lock here only costs the serialisation
            # this function adds, and asking the OS whether a recorded pid is
            # alive is both platform-specific and wrong after pid reuse. The
            # window is long enough that a lock past it belongs to a process
            # that died rather than one that is slow -- this critical section
            # is a few filesystem calls.
            try:
                stale = (time.time() - path.stat().st_mtime) > LOCK_STALE_SECONDS
            except OSError:
                stale = False
            if stale:
                try:
                    path.unlink()
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                break
            # Deliberately after the deadline check, so an unremovable stale
            # lock cannot spin here forever.
            time.sleep(0.05)
            continue
        except OSError:
            # A directory that will not take a lock file at all. The handoff
            # itself may still be writable; if it is not, the write below
            # reports it.
            break
        else:
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(f"{token} {os.getpid()} "
                             f"{datetime.now(timezone.utc).isoformat()}\n")
            except OSError:
                # An empty lock nobody can prove ownership of would block every
                # writer until it aged out, so it goes rather than stays.
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    path.unlink()
                except OSError:
                    pass
                break
            acquired = True
            break
    try:
        yield acquired
    finally:
        if acquired:
            try:
                held = path.read_text(encoding="utf-8", errors="replace").split()
            except OSError:
                held = []
            # Only our own lock. One we cannot read is left alone too: a lock
            # that ages out costs a wait, and deleting somebody else's costs a
            # handoff.
            if held and held[0] == token:
                try:
                    path.unlink()
                except OSError:
                    pass


def _superseded_name(handoff_file: Path) -> str:
    """A name no existing archive can already hold.

    The timestamp is for the human reading the directory; the random suffix is
    what makes the name unique. Two agents can hand off from one project in the
    same second -- the docstring above this function's caller says so -- and a
    second-resolution timestamp alone would have them archive over each other
    while preserving each other's work, which is the joke that writes itself.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{handoff_file.stem}-{stamp}-{uuid.uuid4().hex[:12]}{handoff_file.suffix}"


def _swapped(before: os.stat_result, after: os.stat_result) -> bool:
    """Whether the descriptor holds a different file than the one examined.

    ``O_NOFOLLOW`` does not exist on Windows, so the flag that is supposed to
    make the open refuse a symlink planted since the ``lstat`` is simply 0
    there and the open follows it. Identity is therefore re-established from
    the descriptor. Windows can report a zero file index, and that unknown is
    not treated as a match -- an unanswered question is not a yes.
    """
    if not before.st_ino or not after.st_ino:
        return False
    return (before.st_ino, before.st_dev) != (after.st_ino, after.st_dev)


def _read_prior_handoff(handoff_file: Path,
                        before: os.stat_result) -> bytes | None:
    """The predecessor's bytes, read through one descriptor it cannot swap.

    Checking with ``lstat`` and then reading with ``read_bytes`` re-opens the
    path by name, so every guarantee the check established is only a guess by
    the time the read happens: the regular file that was measured can be a fifo
    by then, and the open blocks until somebody writes to the other end -- or a
    hundred gigabytes, and the size limit measured a file that no longer
    exists. The type and the size are therefore checked on the descriptor that
    is actually read, which nothing can substitute.

    Returns None when the file has gone, which needs no preserving.
    """
    try:
        fd = os.open(handoff_file, _PRIOR_OPEN_FLAGS)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise PreserveError(
            f"Cannot read the existing handoff {handoff_file}: {exc}\n"
            f"Refusing to overwrite a file that cannot be read first. "
            f"Move it aside and retry.")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PreserveError(
                f"{handoff_file} is not a regular file.\n"
                f"Refusing to replace it. Move it aside and retry.")
        if _swapped(before, info):
            # O_NOFOLLOW does not exist on Windows, so the open can have
            # followed a symlink planted since the lstat. The descriptor is
            # therefore asked what it actually holds, and a different file
            # than the one that was examined is not this session's to archive.
            raise PreserveError(
                f"{handoff_file} was replaced while it was being examined.\n"
                f"Refusing to overwrite it. Retry.")
        if info.st_size > MAX_PRESERVE_BYTES:
            raise PreserveError(
                f"The existing handoff {handoff_file} is "
                f"{info.st_size} bytes, which is not a handoff.\n"
                f"Refusing to overwrite it. Move it aside and retry.")
        # One byte past the limit is enough to tell that it was exceeded, and
        # is all that is ever held beyond it -- the file can still grow between
        # the fstat and the read.
        budget = MAX_PRESERVE_BYTES + 1
        chunks: list[bytes] = []
        while budget > 0:
            try:
                chunk = os.read(fd, min(1 << 20, budget))
            except OSError as exc:
                raise PreserveError(
                    f"Cannot read the existing handoff {handoff_file}: {exc}\n"
                    f"Refusing to overwrite it. Move it aside and retry.")
            if not chunk:
                break
            chunks.append(chunk)
            budget -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > MAX_PRESERVE_BYTES:
        raise PreserveError(
            f"The existing handoff {handoff_file} grew past "
            f"{MAX_PRESERVE_BYTES} bytes while being read, which is not a "
            f"handoff.\nRefusing to overwrite it. Move it aside and retry.")
    return data


def _drop_symlink(handoff_file: Path, archived: Path) -> None:
    """Remove the link now that where it pointed has been written down.

    On POSIX ``os.replace`` would replace the link itself, so removing it first
    changes nothing. On Windows it would not: a symlink to a *directory* is a
    directory entry, ``MoveFileEx`` refuses to replace one, and the handoff
    would die with a bare access-denied -- losing the live session's context to
    protect a link whose target is not even in danger. Removing it first makes
    both platforms do the same thing, and the thing the archive says was done.
    """
    try:
        os.unlink(handoff_file)
    except FileNotFoundError:
        return
    except OSError:
        # Windows again: a directory symlink comes off with rmdir, not unlink.
        try:
            os.rmdir(handoff_file)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PreserveError(
                f"Recorded the handoff symlink at {archived}, but cannot "
                f"remove the link at {handoff_file}: {exc}")


class PreserveError(Exception):
    """The predecessor could not be saved, so it must not be overwritten.

    Raised rather than exiting on the spot: the caller holds this session's
    handoff text and can put it somewhere safe before it gives up, so that
    refusing to destroy the old context does not destroy the new one instead.
    """


def _archive(handoff_file: Path, payload: bytes) -> Path:
    """Write ``payload`` into ``superseded/`` under a name nothing else holds.

    Raises OSError when it cannot, because the two callers want opposite things
    from that: preserving a predecessor must abort the handoff rather than
    proceed to destroy it, while the insurance copy is a second chance and must
    never be the reason a handoff fails.
    """
    dest_dir = handoff_file.parent / SUPERSEDED_DIRNAME
    dest_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        dest = dest_dir / _superseded_name(handoff_file)
        try:
            # "xb" is O_EXCL: it fails rather than truncating, so no archive
            # can ever be written over another one.
            with open(dest, "xb") as fh:
                fh.write(payload)
        except FileExistsError:
            continue
        return dest
    raise FileExistsError(f"no unused name available in {dest_dir}")


def _archived_author(archive: Path) -> str | None:
    """The instance that wrote the handoff now preserved at ``archive``.

    Every failure answers ``None`` -- "cannot say". This runs between the
    preserve and the publish, the one stretch where an escaping exception
    abandons a handoff whose predecessor has *already* been copied aside; the
    phrasing of a warning is not worth that risk. ``None`` is also already the
    value the caller must handle, because a predecessor written before the
    stamp existed carries no name either, so a failed read joins a state that
    is being handled rather than inventing one.

    The archive is read rather than the original: it is the copy this process
    just created, its size is bounded by ``MAX_PRESERVE_BYTES``, and it is the
    file the warning is about to name.
    """
    try:
        text = archive.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return authoring_instance(text)


def preserve_prior_handoff(handoff_file: Path) -> Path | None:
    """Copy an unread handoff aside before it is replaced.
    ``write_atomic`` guarantees a reader never sees *half* a handoff. It says
    nothing about a handoff nobody ever read: ``os.replace`` overwrites the
    destination unconditionally, so a session that hands off while the previous
    handoff is still sitting there destroys it, silently, permanently, and with
    no record that it existed. That is not a hypothetical -- the tool itself
    prints "no running session found ... restart may not trigger" and writes
    anyway, and a handoff nothing restarts to consume is exactly a handoff that
    is still there next time. Two agents sharing one project is the other way
    in.

    The protocol says the reader deletes the file once it has been read, so an
    occupied destination means *nobody has consumed this one yet* -- which has
    two causes, not one, and the difference is invisible here. Either this
    instance's own predecessor went unread, or a peer sharing the project's
    mailbox published while this session worked. Measured on this repository,
    the second is the common case: the mailbox is per-project and the restart
    marker is per-instance. Either way the losing side is a session that has
    already ended and cannot be asked to repeat itself, and refusing to hand
    off would be worse -- it loses the current session's context instead, and
    the current session is the one that still exists. So neither is discarded
    -- the old one is copied into ``superseded/`` and the new one is written.
    Which of the two happened is reported by the caller, from the authorship
    stamp in the preserved bytes; this function does not need to know.

    Copied, not moved: the source stays in place until the copy has succeeded,
    so there is no instant at which the only extant copy is in flight. The
    archive is created with ``O_EXCL``, which is the only way to be sure this
    function -- whose whole purpose is to stop a silent overwrite -- does not
    perform one.

    Nothing is ever pruned from ``superseded/``. A reaper here would be a
    delete path inside a fix for an unwanted delete, and it would be reasoning
    about the age of files whose value it cannot judge. The directory only
    grows when a handoff went unread, which is already the anomaly.

    Returns the archive path, or ``None`` when there was nothing worth saving.
    """
    present = path_present(handoff_file)
    if present is False:
        return None

    # `present` is True, or None for "the path cannot be examined" -- a denied
    # permission, an unready drive, a home that has gone away. Absent is the
    # only answer that licenses an overwrite, so anything else is handled here
    # rather than assumed away.
    try:
        info = os.lstat(handoff_file)
    except (FileNotFoundError, NotADirectoryError):
        # Consumed between the probe and the stat. Nothing to save, and the
        # write that follows now lands on an empty slot, which is the case
        # this function exists to stay out of the way of.
        return None
    except OSError as exc:
        raise PreserveError(
            f"Cannot examine the existing handoff {handoff_file}: {exc}\n"
            f"Refusing to overwrite a file that cannot be read first. "
            f"Move it aside and retry.")

    was_symlink = stat.S_ISLNK(info.st_mode)
    if was_symlink:
        # The link's target is not at risk -- it is a separate file and nothing
        # here opens it. What is lost is the user's redirection, so that -- and
        # only that -- is what gets recorded. Reading through the link instead
        # could block forever on a fifo or swallow a terabyte, to preserve
        # bytes that were never in danger.
        try:
            target = os.readlink(handoff_file)
        except OSError as exc:
            raise PreserveError(
                f"Cannot read the symlink at {handoff_file}: {exc}")
        prior = (f"# Superseded handoff symlink\n\n"
                 f"`{handoff_file}` was a symlink to `{target}`.\n"
                 f"The handoff written at {datetime.now(timezone.utc).isoformat()} "
                 f"removed the link and wrote a regular file in its place. "
                 f"The link's target was not read and not modified.\n"
                 # A symlink target is bytes on POSIX, so it reaches Python
                 # with surrogate escapes when it is not valid UTF-8. Encoding
                 # that strictly raises UnicodeEncodeError, which is not an
                 # OSError and would escape every handler here as a traceback.
                 ).encode("utf-8", errors="backslashreplace")
    elif stat.S_ISREG(info.st_mode):
        prior = _read_prior_handoff(handoff_file, info)
        if prior is None:
            return None
        if not prior.strip():
            # An empty file carries no context, so replacing it loses nothing
            # and archiving it would only add noise to `superseded/`.
            return None
    else:
        # A directory, a fifo, a device node. `os.replace` would happily
        # destroy the last two and fail on the first, and reading a fifo would
        # hang this process for as long as nobody opens the other end.
        raise PreserveError(
            f"{handoff_file} is not a regular file.\n"
            f"Refusing to replace it. Move it aside and retry.")

    try:
        dest = _archive(handoff_file, prior)
    except OSError as exc:
        raise PreserveError(
            f"Cannot preserve the unread handoff beside {handoff_file}: "
            f"{exc}\nRefusing to overwrite it.")
    if was_symlink:
        _drop_symlink(handoff_file, dest)
    return dest


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
        f"Add it with a line such as:\n  \"{resolved_str(project_root)}\",<guid>")


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


def render(status: str, in_progress: str, next_steps: str,
           context: str, prompt: str, notice: str = "",
           instance: str = "") -> str:
    """The handoff document.

    ``notice`` is a block the *tool* has to say about the circumstances of the
    write -- see :data:`NOTICE_UNSERIALISED`. It sits under the title rather
    than above it so the file is still a handoff document to anything that
    keys on the ``# Session Handoff`` header, and above ``## Status`` so a
    reader meets it before the content it qualifies.

    ``instance`` is stamped in the same band, below the notice: the notice
    is about *this write* and may change what the reader does next, while the
    stamp qualifies every word in the file. Both are above ``## Status``, so
    a reader learns whose session this was before reading a document written
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

    guid = resolve_guid(primary_repo_root(project_root))
    proj_dir = project_dir(guid)
    handoff_file = proj_dir / "next-session.md"
    marker = state_dir() / instance_id

    # Even a validated guid can fail here -- a read-only home, a full disk, a
    # revoked permission. Report it the way the rest of the tool reports
    # trouble rather than with a bare traceback.
    try:
        proj_dir.mkdir(parents=True, exist_ok=True)
        state_dir().mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        die(f"Cannot create {proj_dir} or {state_dir()}: {exc}")

    # Rendered before the lock is taken: it cannot fail on the filesystem, and
    # holding a shared lock across work that does not need it is how a
    # millisecond lock becomes a contended one. `render_body` is kept so the
    # contended paths below can re-render the same words carrying a notice --
    # still pure string work, still nothing that can fail under the lock.
    def render_body(notice: str = "") -> str:
        return render(args.status, args.in_progress, args.next_steps,
                      args.context, args.prompt, notice=notice,
                      instance=instance_name)

    body = render_body()
    published = body

    with handoff_lock(handoff_file) as locked:
        if not locked:
            # Another handoff is inside its critical section, or a lock could
            # not be made at all. Going ahead is right -- refusing would throw
            # away this session for certain -- but going ahead unserialised is
            # the one path on which the other writer can still publish between
            # this process's preserve and its rename, and then be overwritten
            # by it. So this session's context is banked first: whatever the
            # race does to `next-session.md`, the words exist on disk.
            #
            # Both copies say so in their own bytes. The warnings below go to
            # stderr, which belongs to the session that is ending; the next
            # session -- the one that has to decide whether the file it is
            # reading is the newest -- would otherwise get no sign at all.
            #
            # `published` is chosen here, before the bank is attempted, and is
            # deliberately not revised when the bank fails: the notice claims
            # nothing about the spare copy, precisely so that it stays true
            # whichever way the next few lines go.
            published = render_body(NOTICE_UNSERIALISED)
            try:
                spare = _archive(
                    handoff_file,
                    render_body(NOTICE_BANKED_UNSERIALISED).encode("utf-8"))
            except OSError as exc:
                spare = None
                print(f"Warning: could not take the handoff lock, and could "
                      f"not bank a spare copy either: {exc}", file=sys.stderr)
            if spare is not None:
                print(f"Warning: could not take the handoff lock for this "
                      f"project. Writing anyway; a copy of this one is banked "
                      f"at {spare}", file=sys.stderr)
        try:
            saved = preserve_prior_handoff(handoff_file)
        except PreserveError as exc:
            # The predecessor cannot be saved, so it must not be replaced --
            # but this session's words must not pay for that. They are banked
            # first, and only then does the tool give up, so the operator is
            # choosing between two files that both still exist rather than
            # being told which one was destroyed on its behalf.
            #
            # On the unlocked path this is the *second* bank of the same
            # words, and that is deliberate rather than tidy: the first copy
            # was insurance against a race, this one records that the publish
            # was abandoned, and the two notices are complementary because
            # neither claims an outcome. Two identical bodies in `superseded/`
            # cost a reader nothing; a missing one costs a session.
            try:
                spare = _archive(
                    handoff_file,
                    render_body(NOTICE_BANKED_UNPUBLISHED).encode("utf-8"))
            except OSError as bank_exc:
                die(f"{exc}\nThis handoff could not be banked either "
                    f"({bank_exc}); it is printed below so it is not lost.\n\n"
                    f"{body}")
            die(f"{exc}\nThis handoff was not lost: it is at {spare}")
        if saved is not None:
            prior_author = _archived_author(saved)
            if prior_author is None:
                why = ("It does not say which instance wrote it, so it is "
                       "either this instance's own previous session going "
                       "unread, or a peer's published while you worked.")
            elif prior_author == instance_name:
                why = (f"It was written by this instance "
                       f"({instance_name!r}), so it reached no reader.")
            else:
                why = (f"It was written by a DIFFERENT instance "
                       f"({prior_author!r}), which shares this project's "
                       f"mailbox. That session's next agent will now find "
                       f"nothing waiting; its context is only in "
                       f"{SUPERSEDED_DIRNAME}/.")
            print(f"Warning: a handoff was already waiting at "
                  f"{handoff_file}.\n"
                  f"         {why}\n"
                  f"         It has been preserved at {saved}",
                  file=sys.stderr)
        write_atomic(handoff_file, published)
    try:
        marker.touch()
    except OSError as exc:
        die(f"Handoff written, but cannot raise restart marker {marker}: {exc}")

    print(f"✅ Handoff written: {handoff_file}")
    print(f"✅ Restart signal: {marker}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
