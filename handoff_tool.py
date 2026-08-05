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
           context: str, prompt: str, instance: str = "") -> str:
    """The handoff document.

    ``instance`` is stamped under the title rather than above it, so the file
    is still a handoff document to anything that keys on the
    ``# Session Handoff`` header, and above ``## Status`` so a reader learns
    whose session this was before reading a document written throughout in
    the first person. An empty ``instance`` emits no stamp at all rather than
    an empty one -- see :func:`authoring_instance` on why "does not say" must
    stay distinct from a name.
    """
    parts = ["# Session Handoff", ""]
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

    body = render(args.status, args.in_progress, args.next_steps,
                  args.context, args.prompt, instance=instance_name)

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
