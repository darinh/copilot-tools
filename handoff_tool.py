#!/usr/bin/env python3
"""Atomic session handoff for Copilot CLI agents.

Writes ``next-session.md`` for the project and raises the operator's restart
marker so the loop picks up a fresh session. Cross-platform.

A handoff that is still sitting unread when the next one is written is copied
into ``superseded/`` beside it rather than overwritten -- see
``preserve_prior_handoff``.

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
import csv
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
    file_present,
    path_present,
)
from operator_console import enable_utf8_output               # noqa: E402
from operator_mux import Mux, MuxError, safe_instance_id      # noqa: E402
from project_paths import (                                   # noqa: E402
    guid_is_usable,
    primary_repo_root,
    project_dir,
    projects_root,
)

IS_WINDOWS = platform.system() == "Windows"
CATALOG = projects_root() / "catalog.csv"
# Where a handoff that was never read goes instead of into the bit bucket.
SUPERSEDED_DIRNAME = "superseded"
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
    resolved = str(Path(path).resolve())
    return resolved.lower() if IS_WINDOWS else resolved


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
    occupied destination *means* unread, and the losing side is a session that
    has already ended and cannot be asked to repeat itself. Refusing to hand
    off would be worse: it loses the current session's context instead, and the
    current session is the one that still exists. So neither is discarded --
    the old one is copied into ``superseded/`` and the new one is written.

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
    if file_present(CATALOG) is False:
        # False, and only False. "Cannot tell" gets no branch of its own here
        # because the open below already reports the real errno, and its
        # message ("cannot read") is the true one; sending the operator off to
        # create a catalog that is sitting right there behind a denied parent
        # directory is the wrong instruction delivered confidently.
        die(f"Catalog not found: {CATALOG}")
    target = normalize(project_root)
    try:
        with open(CATALOG, "r", encoding="utf-8", errors="replace", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 2:
                    continue
                path, guid = row[0].strip().strip('"'), row[1].strip().strip('"')
                if not path:
                    continue
                try:
                    matched = normalize(path) == target
                except OSError:
                    continue
                if matched:
                    if not guid_is_usable(guid):
                        die(f"Catalog entry for {target} has an unusable "
                            f"project id: {guid!r}\n"
                            f"The second column must be one plain directory "
                            f"name, such as a GUID. Fix the line to read:\n"
                            f"  \"{Path(project_root).resolve()}\",<guid>")
                    return guid
    except OSError as exc:
        die(f"Cannot read catalog {CATALOG}: {exc}")
    die(f"No catalog entry for: {target}\n"
        f"Add it with a line such as:\n  \"{Path(project_root).resolve()}\",<guid>")


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
    target = str(Path(project_root).resolve())
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
           context: str, prompt: str) -> str:
    parts = ["# Session Handoff", "", "## Status", status, ""]
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
    # millisecond lock becomes a contended one.
    body = render(args.status, args.in_progress, args.next_steps,
                  args.context, args.prompt)

    with handoff_lock(handoff_file) as locked:
        if not locked:
            # Another handoff is inside its critical section, or a lock could
            # not be made at all. Going ahead is right -- refusing would throw
            # away this session for certain -- but going ahead unserialised is
            # the one path on which the other writer can still publish between
            # this process's preserve and its rename, and then be overwritten
            # by it. So this session's context is banked first: whatever the
            # race does to `next-session.md`, the words exist on disk.
            try:
                spare = _archive(handoff_file, body.encode("utf-8"))
            except OSError as exc:
                spare = None
                print(f"Warning: could not take the handoff lock, and could "
                      f"not bank a spare copy either: {exc}", file=sys.stderr)
            if spare is not None:
                print(f"Warning: another handoff is in progress for this "
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
            try:
                spare = _archive(handoff_file, body.encode("utf-8"))
            except OSError as bank_exc:
                die(f"{exc}\nThis handoff could not be banked either "
                    f"({bank_exc}); it is printed below so it is not lost.\n\n"
                    f"{body}")
            die(f"{exc}\nThis handoff was not lost: it is at {spare}")
        if saved is not None:
            print(f"Warning: a handoff was already waiting at {handoff_file} "
                  f"and had not been read.\n"
                  f"         It has been preserved at {saved}",
                  file=sys.stderr)
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
