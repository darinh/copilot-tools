"""The tracked backlog: parse, validate and render ``backlog/`` items.

Why this exists
---------------

Open work in a Copilot-tools project survives only in
``~/.operator/projects/{guid}/handoff/{instance}.md``, which is read-once and
deleted at session start. Closed work is answerable from ``git log``; open work
was answerable from nothing durable at all. It lived in a live agent's context
and was carried forward as one re-summarised sentence per session -- lossy by
construction, and nothing could detect the loss.

``backlog/`` is the fallback. One file per item, under version control, in the
repository the work belongs to.

One file per item, not one file
-------------------------------

``backlog/0007-entra-id-endpoint-drift.md`` -- zero-padded id, kebab slug.

This is the main design constraint, not a stylistic preference. Parallel agents
work in separate worktrees off the same ``main``. A single ``BACKLOG.md`` puts
every add and every close on the same lines of the same file, so every
concurrent pair conflicts. One file per item makes an add conflict-free by
construction, and makes a close read as a small diff against one file.

Why the front-matter reader is hand-rolled
------------------------------------------

Two reasons, and the second is the one that decided it.

PyYAML is declared in this project's ``dev`` extra, not its runtime
dependencies -- and ``backlog`` is a console script, so importing yaml here
would give a dependency-free toolkit its first runtime dependency, payable by
every user who installs without ``[dev]``. This repository already carries a
narrow hand-rolled TOML reader for a comparable reason (``tomllib`` is 3.11+
and the floor is 3.10), so the precedent is established.

The second reason is that YAML's comment rule is actively wrong for this data.
``title: Fix issue #42`` parses in YAML as the value ``Fix issue`` with ``#42``
discarded as a comment, because YAML strips ``#`` wherever whitespace precedes
it. Titles here name defects, and defects have numbers. So this reader does
*no* comment stripping: a value is the rest of its line. The visible
consequence is that an inline ``# open | closed | rejected`` left in a copied
template does not quietly parse as ``open`` -- it fails, loudly, naming the
file. That is the better failure, and ``backlog/README.md`` documents the
vocabulary instead of a comment doing it.

The parser is deliberately flat: no nesting, no multi-line values, no anchors.
Every field is one line. A format that cannot express structure cannot grow a
structural ambiguity for a hand-rolled reader to get wrong.

What stops it rotting
---------------------

A backlog nothing reads decays exactly like any other prose. ``check()`` is the
single owner of every rule, ``tests/test_backlog_conformance.py`` runs it
against the real directory, and ``backlog check`` runs the same code from a
terminal. There is one implementation of the vocabulary, one of the discovery
rule, and one of each check -- a second copy is the thing that drifts.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from install_manifest import path_present
from operator_console import enable_utf8_output
from project_paths import (
    CATALOG_MISSING,
    CATALOG_NO_ENTRY,
    CATALOG_UNREADABLE,
    CATALOG_UNUSABLE_ID,
    catalog_guid,
    primary_repo_root,
    project_dir,
    projects_root,
    resolved_str,
)

__all__ = [
    "STATUSES", "TERMINAL_STATUSES", "COMMIT_REQUIRED_STATUSES", "OPEN_STATUS",
    "PROPOSED_STATUS", "ACTIVE_STATUSES", "APPROVED_STATUSES",
    "NO_SPEC", "BACKLOG_DIRNAME", "EVIDENCE_HEADING", "REQUIRED_FIELDS",
    "KNOWN_FIELDS", "ITEM_FILENAME", "Item", "BacklogFormatError",
    "checkout_root", "item_paths", "split_front_matter", "parse_item", "load",
    "check", "workable", "why_not_workable", "by_filename_id", "next_id",
    "slug_for", "render_item", "create_item", "approve_item",
    "WatermarkError", "watermark_path", "read_watermark", "write_watermark",
    "ScrumReport", "scrum_report", "format_scrum",
    "render_html", "main",
]

#: The complete status vocabulary. This tuple is the only place the legal
#: values are written down; tests read it rather than repeating the literals,
#: because a test that spells its own copy of a vocabulary stops testing the
#: vocabulary the moment the two disagree.
STATUSES = ("proposed", "open", "closed", "rejected")

#: Filed, but not yet approved by the product owner. This value exists so the
#: approval gate can be *expressed*: ``open`` alone conflates "somebody wrote
#: this down" with "somebody decided it should be built", and a gate cannot be
#: enforced against a distinction the data cannot make. An agent may file one
#: of these unprompted; it may not work one.
PROPOSED_STATUS = "proposed"

#: Approved, outstanding work. The one status an agent may pick up freely.
OPEN_STATUS = "open"

#: Statuses that end an item's life. Both require a ``closed`` date: a
#: rejection is a decision with a date, not an absence.
TERMINAL_STATUSES = ("closed", "rejected")

#: Statuses that mean the item is still alive. These carry neither a closing
#: date nor a commit -- the complement of :data:`TERMINAL_STATUSES`, spelled
#: out rather than derived so that adding a status forces a decision about
#: which side it falls on instead of defaulting into one.
ACTIVE_STATUSES = ("proposed", "open")

#: Statuses that carry the product owner's approval. An agent may work an item
#: with one of these, or an item whose ``blocks`` field earns it the exception
#: documented in :func:`workable`. Nothing else.
APPROVED_STATUSES = ("open",)

#: Statuses that require a resolvable ``commit``. Only ``closed`` does --
#: ``rejected`` means nothing shipped, so demanding a SHA for it would force
#: whoever rejects an item to invent one, and an invented SHA is worse than a
#: blank field because it looks like evidence.
COMMIT_REQUIRED_STATUSES = ("closed",)

#: The literal a ``spec`` field carries when an item has no spec-kit impact.
#: Required rather than allowing the field to be blank, so "this changes no
#: specification" is a decision somebody wrote down instead of a silence that
#: could equally mean nobody looked.
NO_SPEC = "none"

BACKLOG_DIRNAME = "backlog"
SPECS_DIRNAME = "specs"
EVIDENCE_HEADING = "## Evidence"

#: Item filenames: four-digit id, a kebab slug, ``.md``. ``README.md`` and any
#: other documentation in the directory does not match, so it is excluded by
#: the discovery rule itself rather than by a special case somewhere that a
#: later reader could fail to notice.
ITEM_FILENAME = re.compile(r"^(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")

REQUIRED_FIELDS = ("id", "title", "status", "opened", "spec")
OPTIONAL_FIELDS = ("closed", "commit", "requirement", "blocks")
KNOWN_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS

_DELIMITER = "---"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FIELD = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):(.*)$")

# A background supervisor with no console of its own would otherwise flash a
# console window for each git call on Windows. Same reasoning as
# ``project_paths._POPEN_KWARGS``; spelled again rather than imported because
# that name is private to that module.
_POPEN_KWARGS: dict[str, int] = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if platform.system() == "Windows" else {}
)


class BacklogFormatError(ValueError):
    """A backlog file could not be parsed at all."""


@dataclass(frozen=True)
class Item:
    """One parsed backlog item.

    ``front`` holds raw string values exactly as written. Typing happens in
    :func:`check`, so that a malformed value is reported as a named problem
    against a named file rather than raised as a bare ``ValueError`` from
    somewhere in the middle of a load.
    """

    path: Path
    front: dict
    body: str

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def status(self) -> str:
        return self.front.get("status", "")

    @property
    def title(self) -> str:
        return self.front.get("title", "")

    @property
    def filename_id(self) -> int:
        """The id encoded in the filename. Callers hold a path that matched
        :data:`ITEM_FILENAME`, so the match cannot be None here."""
        return int(ITEM_FILENAME.match(self.name).group(1))

    def section(self, heading: str) -> str:
        """The text under ``heading``, up to the next heading of any level.

        Returns the empty string when the heading is absent, which is the same
        answer as "present but empty" on purpose: both mean there is nothing
        written there, and both are equally not evidence.
        """
        lines = self.body.splitlines()
        out: list[str] = []
        collecting = False
        for line in lines:
            if line.strip() == heading:
                collecting = True
                continue
            if collecting and line.startswith("#"):
                break
            if collecting:
                out.append(line)
        return "\n".join(out).strip()


def checkout_root(start=None) -> Path:
    """The root of the working tree ``start`` belongs to.

    **This is the one place in this toolkit where ``git rev-parse
    --show-toplevel`` is the correct call, and using
    ``project_paths.primary_repo_root`` here would be the bug.**

    That function answers "which project is this", and it deliberately resolves
    a linked worktree back to the primary checkout so that a worktree cannot
    mint a second project identity. The backlog is not identity: it is tracked
    content, versioned on a branch. In a worktree the content that matters is
    the worktree's, and an agent adding an item on a feature branch must
    validate the items on that branch.

    Resolving to the primary checkout instead would validate whatever `main`
    happens to hold, so a malformed item added on a branch would pass its own
    branch's test run, and a well-formed one would be invisible to the tooling
    that just wrote it. Both were observed before this function existed.

    Falls back to ``start`` (or the cwd) when git is missing, the call fails,
    or the path is not inside a repository.
    """
    base = Path(start) if start is not None else Path.cwd()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(base), capture_output=True,
            encoding="utf-8", errors="replace", timeout=10,
            **_POPEN_KWARGS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return base
    if proc.returncode != 0 or not proc.stdout:
        return base
    top = proc.stdout.strip()
    return Path(top) if top else base


def backlog_dir(repo_root=None) -> Path:
    """The backlog directory of the working tree ``repo_root`` belongs to."""
    root = Path(repo_root) if repo_root is not None else checkout_root()
    return root / BACKLOG_DIRNAME


def item_paths(directory) -> list:
    """Every item file in ``directory``, sorted by name.

    The single owner of "what is a backlog item". A second copy of this glob
    anywhere is how a file with an unexpected name gets excluded from every
    assertion while all of them keep passing green.

    Raises :class:`BacklogFormatError` when the directory exists but cannot be
    listed. Returning an empty list there would be the worst available answer:
    "no items" is what a *clean* backlog directory looks like to every rule
    downstream, so a permission denial would report the backlog as conforming
    at the moment it became unreadable.

    An entry that cannot be examined is *kept*, not skipped, for the same
    reason -- parsing it then fails and names it, where dropping it would
    leave no trace at all.
    """
    directory = Path(directory)
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise BacklogFormatError(
            f"{directory} exists but cannot be listed: {exc}") from exc
    return sorted(
        (p for p in entries
         if ITEM_FILENAME.match(p.name) and path_present(p) is not False),
        key=lambda p: p.name,
    )


def split_front_matter(text: str) -> tuple:
    """Split ``text`` into its front-matter mapping and its body.

    Raises :class:`BacklogFormatError` when the document does not open with a
    ``---`` line or the block is never closed. Values are the rest of the line,
    stripped of surrounding whitespace and nothing else -- see the module
    docstring for why no comment stripping happens.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _DELIMITER:
        raise BacklogFormatError(
            "file does not begin with a '---' front-matter delimiter")
    front: dict = {}
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == _DELIMITER:
            return front, "\n".join(lines[index + 1:]).strip()
        if not line.strip():
            continue
        match = _FIELD.match(line)
        if not match:
            raise BacklogFormatError(
                f"front-matter line {index + 1} is not 'key: value': {line!r}")
        key = match.group(1)
        if key in front:
            raise BacklogFormatError(f"duplicate front-matter key {key!r}")
        front[key] = match.group(2).strip()
    raise BacklogFormatError("front-matter block is never closed with '---'")


def parse_item(path) -> Item:
    """Parse one item file. Raises :class:`BacklogFormatError`."""
    path = Path(path)
    front, body = split_front_matter(path.read_text(encoding="utf-8"))
    return Item(path=path, front=front, body=body)


def load(directory) -> tuple:
    """Parse every item in ``directory``.

    Returns ``(items, problems)``. A file that cannot be parsed becomes a
    problem string rather than an exception, so one broken item reports itself
    by name instead of hiding every other item behind a traceback.
    """
    items: list = []
    problems: list = []
    try:
        paths = item_paths(directory)
    except BacklogFormatError as exc:
        return [], [str(exc)]
    for path in paths:
        try:
            items.append(parse_item(path))
        except (BacklogFormatError, OSError, UnicodeDecodeError) as exc:
            problems.append(f"{path.name}: {exc}")
    return items, problems


def _commit_resolves(sha: str, repo_root) -> bool:
    """True when ``sha`` names a commit that exists in this repository.

    ``^{commit}`` is load-bearing: ``git cat-file -e`` alone succeeds for a
    blob or a tree, so without it any 40-hex object -- including one that is
    not a commit at all -- would certify a close.
    """
    try:
        proc = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=str(repo_root), capture_output=True,
            encoding="utf-8", errors="replace", timeout=30,
            **_POPEN_KWARGS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def check(repo_root=None, *, resolve_commits: bool = True) -> list:
    """Every rule, in one place. Returns a list of problem strings.

    Empty means the backlog is well-formed. Never raises: a rule that crashes
    reports nothing, and reporting nothing is indistinguishable from passing.

    ``resolve_commits=False`` skips the git round trip, for callers rendering a
    view rather than gating a build.
    """
    root = Path(repo_root) if repo_root is not None else checkout_root()
    directory = root / BACKLOG_DIRNAME
    problems: list = []

    # R0. The directory exists and holds at least one item.
    #
    # Without this, deleting backlog/ turns every rule below into a loop over
    # an empty list, and the suite reports the backlog perfectly clean at the
    # exact moment it stopped existing. A guard whose subject can vanish has to
    # assert its subject is there.
    #
    # "Cannot tell" gets its own answer rather than sharing one with "absent":
    # a directory that is occupied but unexaminable is not a repository without
    # a backlog, and reporting it as one would be the same silence in a
    # different costume.
    present = path_present(directory)
    if present is False:
        return [f"{BACKLOG_DIRNAME}/ does not exist at {directory}"]
    if present is None:
        return [f"{BACKLOG_DIRNAME}/ at {directory} exists but cannot be "
                "examined, so its contents were never checked"]
    items, problems = load(directory)
    if not items:
        problems.append(
            f"{BACKLOG_DIRNAME}/ contains no items matching "
            f"{ITEM_FILENAME.pattern!r}; every rule below would pass vacuously")
        return problems

    seen_ids: dict = {}
    # Every item by the id its *filename* carries. Filename ids are used for
    # the cross-references below because they are the ids guaranteed to parse:
    # a front-matter id that disagrees or will not convert is R2's and R3's
    # problem to report, and resolving a reference against it would turn one
    # item's typo into a second, spurious complaint against whoever pointed at
    # it. The first item wins a collision; R3 reports the collision itself.
    by_id: dict = {}
    for item in items:
        by_id.setdefault(item.filename_id, item)
    for item in items:
        name = item.name
        front = item.front

        # R1. Unknown or missing fields.
        for field in REQUIRED_FIELDS:
            if not front.get(field):
                problems.append(f"{name}: required field {field!r} is missing "
                                "or empty")
        for field in front:
            if field not in KNOWN_FIELDS:
                problems.append(
                    f"{name}: unknown front-matter field {field!r}; known "
                    f"fields are {', '.join(KNOWN_FIELDS)}")

        # R2. The id parses and matches the filename.
        raw_id = front.get("id", "")
        try:
            numeric_id = int(raw_id)
        except ValueError:
            problems.append(f"{name}: id {raw_id!r} is not an integer")
            numeric_id = None
        if numeric_id is not None:
            if numeric_id != item.filename_id:
                problems.append(
                    f"{name}: front-matter id {numeric_id} does not match the "
                    f"filename id {item.filename_id}")
            # R3. Ids are unique.
            if numeric_id in seen_ids:
                problems.append(
                    f"{name}: id {numeric_id} is already used by "
                    f"{seen_ids[numeric_id]}")
            else:
                seen_ids[numeric_id] = name

        # R4. The status is one of the legal values.
        status = front.get("status", "")
        if status and status not in STATUSES:
            problems.append(
                f"{name}: status {status!r} is not one of "
                f"{', '.join(STATUSES)}")

        # R5. Dates are well-formed.
        for field in ("opened", "closed"):
            value = front.get(field, "")
            if value and not _DATE.match(value):
                problems.append(
                    f"{name}: {field} {value!r} is not a YYYY-MM-DD date")

        # R6. Evidence is present and non-empty. An item with no evidence is a
        # rumour, and a backlog of rumours is worse than no backlog because it
        # costs a reader time to discover that.
        if not item.section(EVIDENCE_HEADING):
            problems.append(
                f"{name}: the {EVIDENCE_HEADING!r} section is missing or empty")

        # R7. Terminal items carry a closing date; live items carry neither a
        # closing date nor a commit.
        closed_on = front.get("closed", "")
        commit = front.get("commit", "")
        if status in TERMINAL_STATUSES and not closed_on:
            problems.append(
                f"{name}: status is {status!r} but no 'closed' date is set")
        if status in ACTIVE_STATUSES:
            if closed_on:
                problems.append(
                    f"{name}: status is {status!r} but a 'closed' date is set")
            if commit:
                problems.append(
                    f"{name}: status is {status!r} but a 'commit' is set")

        # R8. A closed item names a commit, and that commit resolves here. A
        # close pointing at nothing is a claim that something shipped with
        # nothing behind it.
        if status in COMMIT_REQUIRED_STATUSES:
            if not commit:
                problems.append(
                    f"{name}: status is {status!r} but no 'commit' is named")
            elif resolve_commits and not _commit_resolves(commit, root):
                problems.append(
                    f"{name}: commit {commit!r} does not resolve to a commit "
                    "in this repository")

        # R9. The spec-kit mapping. Either a path that exists under specs/, or
        # the explicit literal 'none'.
        spec = front.get("spec", "")
        spec_path = None
        if spec and spec != NO_SPEC:
            candidate = root / spec
            posix = spec.replace(os.sep, "/")
            spec_here = path_present(candidate)
            if not posix.startswith(f"{SPECS_DIRNAME}/"):
                problems.append(
                    f"{name}: spec {spec!r} is not a path under "
                    f"{SPECS_DIRNAME}/ (use {NO_SPEC!r} when there is no "
                    "specification impact)")
            elif spec_here is False:
                problems.append(
                    f"{name}: spec {spec!r} does not exist at {candidate}")
            elif spec_here is None:
                problems.append(
                    f"{name}: spec {spec!r} at {candidate} cannot be examined")
            else:
                spec_path = candidate

        # R10. A named requirement must actually occur in the spec it names.
        # This is what makes the item-to-spec mapping load-bearing rather than
        # decorative: rename or delete the requirement and this goes red, which
        # is the whole point of recording it.
        requirement = front.get("requirement", "")
        if requirement:
            if spec == NO_SPEC or not spec:
                problems.append(
                    f"{name}: a 'requirement' is named but 'spec' is "
                    f"{spec or 'empty'!r}, so there is nothing to check it "
                    "against")
            elif spec_path is not None:
                try:
                    text = spec_path.read_text(encoding="utf-8",
                                               errors="replace")
                except OSError as exc:
                    problems.append(
                        f"{name}: spec {spec!r} could not be read, so "
                        f"requirement {requirement!r} was never checked: {exc}")
                else:
                    if requirement not in text:
                        problems.append(
                            f"{name}: requirement {requirement!r} does not "
                            f"appear in {spec}")

        # R11. A 'blocks' reference names another item, by id.
        #
        # This field is the approval gate's escape hatch, so it is the field
        # most worth breaking: an agent that finds a defect while working an
        # approved item files it as 'proposed' and names the item it is
        # blocking, and that reference is what lets the agent carry on. A
        # reference that resolves to nothing would hand out the exception for
        # free while looking exactly like an audited one.
        blocks = front.get("blocks", "")
        blocked = None
        if blocks:
            try:
                blocked_id = int(blocks)
            except ValueError:
                problems.append(
                    f"{name}: blocks {blocks!r} is not an integer item id")
            else:
                if blocked_id == item.filename_id:
                    problems.append(
                        f"{name}: blocks names the item itself; an item "
                        "cannot be the reason it may be worked")
                elif blocked_id not in by_id:
                    problems.append(
                        f"{name}: blocks names item {blocked_id}, which does "
                        "not exist")
                else:
                    blocked = by_id[blocked_id]

        # R12. The item named by 'blocks' is not itself awaiting approval.
        #
        # Without this the gate repeals itself in two moves: file A as
        # 'proposed', then file B as 'proposed' blocking A, and B is workable
        # on A's authority -- which A never had. Authority has to come from
        # something the product owner touched, so a chain may not begin at an
        # unapproved item.
        if blocked is not None and blocked.status == PROPOSED_STATUS:
            problems.append(
                f"{name}: blocks item {blocked.filename_id}, which is itself "
                f"{PROPOSED_STATUS!r}; an unapproved item cannot be the "
                "authority for working another")

    return problems


# --------------------------------------------------------------------------
# The approval gate
# --------------------------------------------------------------------------

def why_not_workable(item, by_id) -> "str | None":
    """Why an agent may not work ``item``, or ``None`` when it may.

    The single owner of the gate. It answers with a *reason* rather than a
    boolean because every caller needs to say why: a queue that silently omits
    an item teaches an agent nothing, and an agent that cannot see why its item
    is ineligible is an agent about to edit the status field itself.

    Three answers are possible and they are kept apart:

    * approved -- ``None``;
    * terminal or unrecognised -- there is nothing to work;
    * awaiting approval -- eligible only through the ``blocks`` exception.

    **The exception exists because the gate is unenforceable without it.** An
    agent permitted to touch only approved items has no lawful move when it
    finds a defect while working one. Given none it will either stall, or file
    an item and approve it itself -- which repeals the gate while appearing to
    honour it, and leaves no trace that says so. So the lawful move is written
    down: file the defect, name the approved item it blocks, and carry on. The
    exception is narrow (the blocked item must itself be approved, which
    :func:`check` enforces at rest as R12) and it is *recorded*, which is the
    property that matters. A gate nobody can pass legally is not a stricter
    gate; it is the same gate with the audit trail removed.
    """
    status = item.status
    if status in APPROVED_STATUSES:
        return None
    if status in TERMINAL_STATUSES:
        return f"status is {status!r}"
    if status != PROPOSED_STATUS:
        return f"status {status!r} is not one of {', '.join(STATUSES)}"
    blocks = item.front.get("blocks", "")
    if not blocks:
        return ("awaiting approval by the product owner, and it names no "
                "approved item that it blocks")
    try:
        blocked_id = int(blocks)
    except ValueError:
        return (f"awaiting approval, and blocks {blocks!r} is not an item id, "
                "so it grants nothing")
    blocked = by_id.get(blocked_id)
    if blocked is None:
        return (f"awaiting approval, and it blocks item {blocked_id}, which "
                "does not exist")
    if blocked.status not in APPROVED_STATUSES:
        return (f"awaiting approval, and the item it blocks (#{blocked_id}) "
                f"is {blocked.status!r} rather than approved")
    return None


def by_filename_id(items) -> dict:
    """``{filename id: item}``, first occurrence winning a collision.

    Collisions are R3's to report. Resolving them differently here would make
    a cross-reference mean one thing to the checker and another to the queue.
    """
    out: dict = {}
    for item in items:
        out.setdefault(item.filename_id, item)
    return out


def workable(items) -> list:
    """The items an agent is allowed to pick up, in the order given."""
    index = by_filename_id(items)
    return [item for item in items if why_not_workable(item, index) is None]


# --------------------------------------------------------------------------
# Writing items
# --------------------------------------------------------------------------

_SLUG_SEPARATORS = re.compile(r"[^a-z0-9]+")
#: Long enough to stay recognisable, short enough that the filename survives a
#: deep checkout path on Windows, where MAX_PATH still bites tools that have
#: not opted into long paths.
SLUG_MAX = 56


def slug_for(title: str) -> str:
    """A filename slug for ``title`` that :data:`ITEM_FILENAME` will match.

    Everything outside ``[a-z0-9]`` becomes a separator, runs collapse, and the
    ends are trimmed -- which is exactly the pattern's grammar, so the result
    cannot fail to match. A title with nothing usable in it (an id number, say,
    or a title written in a script this transliterates away) yields ``item``
    rather than an empty string: an empty slug would produce ``0008-.md``,
    which the discovery pattern does not match, so the file would sit in
    ``backlog/`` being validated by nothing.
    """
    slug = _SLUG_SEPARATORS.sub("-", title.lower()).strip("-")
    if len(slug) > SLUG_MAX:
        slug = slug[:SLUG_MAX].rstrip("-")
    return slug or "item"


def next_id(directory) -> int:
    """One past the highest id present.

    Propagates the :class:`BacklogFormatError` from :func:`item_paths` rather
    than catching it. A directory that cannot be listed would otherwise
    allocate id 1 and write over the item that already holds it -- the failure
    mode where "I could not see the backlog" is spent as "the backlog is
    empty".

    Two agents in two worktrees can still allocate the same id; that is
    inherent to a queue with no lock, and it is caught at merge by R3 rather
    than papered over here.
    """
    highest = 0
    for path in item_paths(directory):
        highest = max(highest, int(ITEM_FILENAME.match(path.name).group(1)))
    return highest + 1


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def render_item(*, item_id: int, title: str, evidence: str,
                status: str = PROPOSED_STATUS, opened: "str | None" = None,
                spec: str = NO_SPEC, blocks: "int | None" = None,
                why: str = "", notes: str = "") -> str:
    """One item as text, or :class:`BacklogFormatError` if it cannot be one.

    The refusals are the interesting part. A title spanning two lines would
    put its second line into the front-matter block, where it parses as a
    malformed field -- a file this function wrote that this module's own
    parser rejects. An empty evidence section fails R6 for the same reason a
    human-written one does: an item with no evidence is a rumour, and a tool
    that emits rumours faster than a human can weed them is worse than no tool.
    """
    title = title.strip()
    if not title:
        raise BacklogFormatError("an item needs a title")
    if "\n" in title or "\r" in title:
        raise BacklogFormatError(
            "a title is one line; this one spans several, and the rest would "
            "land in the front-matter block as malformed fields")
    evidence = evidence.strip()
    if not evidence:
        raise BacklogFormatError(
            "an item needs evidence: what was observed, when, and how it is "
            "reproducible. An item with none is a rumour")
    if status not in STATUSES:
        raise BacklogFormatError(
            f"status {status!r} is not one of {', '.join(STATUSES)}")
    if status in TERMINAL_STATUSES:
        raise BacklogFormatError(
            f"{status!r} ends an item's life and needs a closing date and, for "
            "a close, the SHA that did the work -- so it cannot be the status "
            "a new item is filed under")

    front = [f"id: {item_id}", f"title: {title}", f"status: {status}",
             f"opened: {opened or _today()}"]
    if blocks is not None:
        front.append(f"blocks: {blocks}")
    front.append(f"spec: {spec}")

    body = [f"{EVIDENCE_HEADING}\n\n{evidence}\n"]
    if why.strip():
        body.append(f"## Why it matters\n\n{why.strip()}\n")
    if notes.strip():
        body.append(f"## Notes\n\n{notes.strip()}\n")
    return "---\n" + "\n".join(front) + "\n---\n\n" + "\n".join(body)


def _write_new(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, refusing to replace anything already there.

    ``x`` rather than ``w``: the id allocation above is a read followed by a
    write, and two agents racing through it must not have one silently
    overwrite the other's evidence. The loser gets an error naming the file.

    ``newline="\\n"`` is explicit because the default translates to
    ``os.linesep``, which would emit CRLF on Windows for a file every other
    platform writes with LF.
    """
    with open(path, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def create_item(directory, **kwargs) -> Path:
    """Write a new item into ``directory`` and return its path.

    ``kwargs`` are :func:`render_item`'s, minus ``item_id``, plus an optional
    ``slug``.
    """
    directory = Path(directory)
    slug = kwargs.pop("slug", None)
    item_id = next_id(directory)
    text = render_item(item_id=item_id, **kwargs)
    name = f"{item_id:04d}-{slug or slug_for(kwargs['title'])}.md"
    if not ITEM_FILENAME.match(name):
        raise BacklogFormatError(
            f"{name!r} does not match {ITEM_FILENAME.pattern!r}, so nothing "
            "would ever validate it")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    _write_new(path, text)
    return path


#: One line *with* whatever ended it, or the last line if nothing did.
#:
#: All three real endings count -- ``\r\n``, ``\n`` and a lone ``\r`` -- so
#: this splits exactly what :func:`parse_item` splits. Anything narrower makes
#: a CR-only file parse fine and then fail to approve, with an error blaming a
#: missing ``status:`` line that is plainly there.
#:
#: Deliberately not ``str.splitlines``, which additionally splits on every
#: Unicode line boundary -- form feed, vertical tab, NEL, U+2028, U+2029 -- so
#: rejoining its output replaces each of those with an ordinary newline. In
#: prose that is silent corruption of somebody's evidence, performed by a
#: function whose entire claim is that it changed one line.
_LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")


def approve_item(directory, item_id: int) -> Path:
    """Move item ``item_id`` from ``proposed`` to ``open``.

    This is the product owner's act, and the whole point of the gate, so it
    changes exactly one line and refuses everything else. It will not approve
    an item that is already approved or already terminal, because both would
    be silent no-ops that read as an approval having happened.

    Every other byte of the file survives: the rest of each line, its ending,
    the trailing blank lines, and any exotic separator inside the prose. The
    rewrite is assembled from the original pieces rather than re-rendered, so
    a one-line decision is a one-line diff -- and a one-line decision that
    shows up as a hundred-line change is a decision nobody reviews.
    """
    directory = Path(directory)
    match = [p for p in item_paths(directory)
             if int(ITEM_FILENAME.match(p.name).group(1)) == item_id]
    if not match:
        raise BacklogFormatError(f"no backlog item with id {item_id}")
    path = match[0]
    raw = path.read_bytes()
    item = parse_item(path)
    if item.status == OPEN_STATUS:
        raise BacklogFormatError(
            f"{path.name} is already {OPEN_STATUS!r}; nothing to approve")
    if item.status != PROPOSED_STATUS:
        raise BacklogFormatError(
            f"{path.name} is {item.status!r}, not {PROPOSED_STATUS!r}; only a "
            "proposed item can be approved")

    lines = _LINE.findall(raw.decode("utf-8"))
    out, replaced, in_front = [], 0, False
    for index, line in enumerate(lines):
        if line.strip() == _DELIMITER:
            if not in_front and index == 0:
                in_front = True
            elif in_front:
                in_front = False
            out.append(line)
            continue
        if in_front and line.startswith("status:"):
            ending = line[len(line.rstrip("\r\n")):]
            out.append(f"status: {OPEN_STATUS}{ending}")
            replaced += 1
            continue
        out.append(line)
    if replaced != 1:
        raise BacklogFormatError(
            f"{path.name}: expected exactly one 'status:' line in the front "
            f"matter, found {replaced}")
    # Bytes, not text: an encoding-aware write would translate newlines on the
    # way out and undo the preservation above.
    path.write_bytes("".join(out).encode("utf-8"))
    return path


# --------------------------------------------------------------------------
# The check-in watermark
# --------------------------------------------------------------------------

#: Where the watermark lives inside the per-project directory.
WATERMARK_NAME = "backlog-scrum.json"


class WatermarkError(RuntimeError):
    """The check-in watermark could not be located, read or written."""


def watermark_path(start=None) -> Path:
    """Where this project's check-in watermark lives.

    Outside the repository and outside session state, both deliberately.

    Session state is the artifact this repository has *measured* not to
    survive: ``~/.operator/trace.jsonl`` records 940 session exits and not one
    wrote a handoff, so anything a session held died with it. A watermark kept
    there would silently reset to "the beginning of time" at every check-in,
    which reads as a working report.

    Tracked content is wrong for the opposite reason. "Since I last looked" is
    a fact about one reader on one machine; committing it would put every
    parallel agent's check-in on the same line of the same file, and merge it
    into a shared answer that is nobody's.

    So it is the per-project directory -- keyed on the *primary* checkout via
    :func:`primary_repo_root`, so that every worktree of one project shares one
    watermark. A worktree is a second directory for the same project, and two
    watermarks for one project would each report the other's work as new.
    """
    root = primary_repo_root(start)
    found = catalog_guid(root)
    if found.guid is not None:
        return project_dir(found.guid) / WATERMARK_NAME
    catalog = projects_root() / "catalog.csv"
    if found.reason == CATALOG_MISSING:
        raise WatermarkError(
            f"No project catalog at {catalog}, so there is nowhere durable to "
            "record a check-in. Run this repository's setup, or create the "
            f"catalog with a line reading:\n  \"{resolved_str(root)}\",<guid>")
    if found.reason == CATALOG_UNREADABLE:
        raise WatermarkError(
            f"Cannot read the project catalog {catalog}: {found.detail}\n"
            "Refusing to report a check-in rather than report one from a "
            "watermark that may exist and could not be found.")
    if found.reason == CATALOG_UNUSABLE_ID:
        raise WatermarkError(
            f"The catalog entry for {resolved_str(root)} has an unusable "
            f"project id: {found.detail!r}. The second column must be one "
            "plain directory name, such as a GUID.")
    if found.reason == CATALOG_NO_ENTRY:
        raise WatermarkError(
            f"No catalog entry for {resolved_str(root)}, so this project has "
            "no per-project directory to keep a check-in watermark in. Add "
            f"one:\n  \"{resolved_str(root)}\",<guid>")
    raise WatermarkError(
        f"The catalog lookup for {resolved_str(root)} failed for a reason "
        f"this command does not recognise: {found.reason!r} {found.detail!r}")


def read_watermark(path) -> "dict | None":
    """The recorded check-in, or ``None`` when there has never been one.

    ``None`` means *never written*, and nothing else. A watermark that exists
    but cannot be read raises, because the two answers lead opposite ways: an
    absent watermark correctly reports the whole history as new, and an
    unreadable one doing the same thing would look exactly like a first run
    while quietly discarding the boundary -- reporting work as new that the
    reader has already been told about, on a report whose entire value is that
    it says what changed.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WatermarkError(f"Cannot read the watermark {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise WatermarkError(
            f"The watermark {path} is not valid JSON: {exc}. Delete it to "
            "start a fresh check-in history, knowing that the next report "
            "will cover everything.") from exc
    if not isinstance(data, dict):
        raise WatermarkError(
            f"The watermark {path} holds {type(data).__name__}, not an object")
    # Field types, not just the container. A JSON document is not a schema:
    # ``{"commit": 123}`` is a perfectly good object, and the report would
    # carry it as far as ``since[:12]`` before dying with a TypeError -- a
    # traceback where an actionable refusal belongs.
    for field in ("commit", "checked_at"):
        value = data.get(field, "")
        if not isinstance(value, str):
            raise WatermarkError(
                f"The watermark {path} has {field!r} as "
                f"{type(value).__name__}, not a string. Delete it to start a "
                "fresh check-in history, knowing that the next report will "
                "cover everything.")
    return data


def write_watermark(path, commit: "str | None", *, when=None) -> dict:
    """Record a check-in at ``commit``. Raises :class:`WatermarkError`.

    Written to a sibling temp file and moved into place, so an interrupted
    write leaves the previous watermark intact rather than a truncated file
    that :func:`read_watermark` would then refuse.
    """
    path = Path(path)
    payload = {
        "commit": commit or "",
        "checked_at": (when or datetime.now(timezone.utc)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".scrum-",
                                   suffix=".json")
        placed = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp, path)
            placed = True
        finally:
            # ``finally`` rather than ``except OSError``: a Ctrl-C landing
            # between the write and the replace is not an OSError, and it
            # would leave a .scrum-*.json behind in the project directory
            # with nothing that ever cleans it up.
            if not placed:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except OSError as exc:
        raise WatermarkError(
            f"Cannot write the watermark {path}: {exc}. The next check-in "
            "would repeat this one.") from exc
    return payload


# --------------------------------------------------------------------------
# The check-in report
# --------------------------------------------------------------------------

def _git(args, cwd) -> "tuple[int, str]":
    """Run git and return ``(returncode, stdout)``; ``(-1, "")`` if it cannot.

    A git that will not run is reported as a failure, never as empty output.
    Empty output is what "nothing changed" looks like, and a report that
    cannot tell those apart says "no news" on the day it stopped working.
    """
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True,
            encoding="utf-8", errors="replace", timeout=30, **_POPEN_KWARGS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return -1, ""
    return proc.returncode, proc.stdout or ""


@dataclass(frozen=True)
class ScrumReport:
    """What changed since the previous check-in.

    ``notes`` carries anything that went wrong while assembling the report.
    They are part of the report rather than a log line because a check-in that
    silently omits a section is indistinguishable from a quiet week.
    """

    head: str = ""
    since: str = ""
    since_when: str = ""
    first_run: bool = False
    since_resolves: bool = True
    commits: tuple = ()
    #: False when the commit list could not be obtained. Distinct from an
    #: empty ``commits``: "none" is an answer and "unknown" is a failure, and
    #: rendering them identically is how a broken report reads as a quiet week.
    commits_known: bool = True
    item_changes: tuple = ()
    changes_known: bool = True
    counts: tuple = ()
    ready: tuple = ()
    awaiting: tuple = ()
    problems: tuple = ()
    notes: tuple = ()


def scrum_report(repo_root=None, watermark: "dict | None" = None, *,
                 resolve_commits: bool = True) -> ScrumReport:
    """Assemble the check-in for ``repo_root`` against ``watermark``."""
    root = Path(repo_root) if repo_root is not None else checkout_root()
    notes: list = []

    rc, out = _git(["rev-parse", "HEAD"], root)
    head = out.strip() if rc == 0 else ""
    if not head:
        # Stated as the observation, not a diagnosis. A checkout with no
        # commits and a checkout whose git could not be run answer this
        # question identically, and picking one of them to report is how
        # "I could not look" gets spent as "there was nothing there".
        notes.append("HEAD could not be read, so nothing is dated against a "
                     "revision; a checkout with no commits yet reads this "
                     "way too")

    since = (watermark or {}).get("commit", "") or ""
    since_when = (watermark or {}).get("checked_at", "") or ""
    first_run = watermark is None
    since_resolves = bool(since) and _commit_resolves(since, root)
    if since and not since_resolves:
        notes.append(
            f"the last check-in recorded commit {since[:12]}, which does not "
            "resolve here -- a rewritten history, or another clone. Reporting "
            "everything instead of silently reporting nothing.")

    commits: list = []
    item_changes: list = []
    commits_known = changes_known = True
    if not head and not first_run:
        # HEAD could not be read, so nothing was listed and nothing is known.
        # Leaving the flags true here renders "Commits: none." on a report
        # whose git is broken -- the same conflation the flags were added to
        # end, one branch further out.
        commits_known = changes_known = False
    # A first run has no boundary to measure from and says so in prose; every
    # other case lists commits, including the one where the recorded boundary
    # has gone. Skipping the section there would render an unresolvable
    # watermark as a quiet week, which is precisely what the note above
    # promises it is not doing -- and a promise the report then breaks is
    # worse than no note, because the reader has been told to trust it.
    if head and not first_run:
        span = f"{since}..{head}" if since_resolves else head
        rc, out = _git(["log", "--no-merges", "--format=%h %s", span], root)
        if rc != 0:
            commits_known = False
            notes.append("could not list commits since the last check-in")
        else:
            commits = [line for line in out.splitlines() if line.strip()]
        if since_resolves:
            changed = ["diff", "--name-status", span, "--", BACKLOG_DIRNAME]
        else:
            changed = ["log", "--no-merges", "--name-status", "--format=",
                       span, "--", BACKLOG_DIRNAME]
        rc, out = _git(changed, root)
        if rc != 0:
            changes_known = False
            notes.append("could not list backlog changes since the last "
                         "check-in")
        else:
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and ITEM_FILENAME.match(
                        Path(parts[-1]).name):
                    item_changes.append((parts[0][:1], Path(parts[-1]).name))

    items, parse_problems = load(root / BACKLOG_DIRNAME)
    del parse_problems  # check() reports these, with the rest
    problems = check(root, resolve_commits=resolve_commits)
    index = by_filename_id(items)
    counts = tuple((status, sum(1 for i in items if i.status == status))
                   for status in STATUSES)
    ready = tuple((i.filename_id, i.title) for i in items
                  if why_not_workable(i, index) is None)
    awaiting = tuple((i.filename_id, i.title) for i in items
                     if i.status == PROPOSED_STATUS
                     and why_not_workable(i, index) is not None)
    return ScrumReport(
        head=head, since=since, since_when=since_when, first_run=first_run,
        since_resolves=since_resolves, commits=tuple(commits),
        commits_known=commits_known, changes_known=changes_known,
        item_changes=tuple(item_changes), counts=counts, ready=ready,
        awaiting=awaiting, problems=tuple(problems), notes=tuple(notes),
    )


def format_scrum(report: ScrumReport) -> str:
    """The check-in as text. One function, so the CLI cannot render a second
    version of it that says something else."""
    sections: list = []
    if report.first_run:
        sections.append("First check-in for this project: there is no earlier "
                        "one to measure against, so the git history below is "
                        "left out and everything else is current state.")
    elif report.since_when:
        sections.append(f"Since the check-in of {report.since_when} "
                        f"({report.since[:12] or 'no commit recorded'}).")
    else:
        sections.append("Since the previous check-in, which recorded no "
                        "commit to measure from.")

    if report.first_run:
        # The opening prose already says the history is left out; a first run
        # has no boundary, so neither "none" nor "unknown" would be true.
        pass
    elif not report.commits_known:
        # Not "none". The list could not be obtained, and saying "none" here
        # would report the failure as a quiet period -- which is the one thing
        # a check-in must never do, because both look like good news.
        sections.append("Commits: could not be listed (see the caveats "
                        "below); this is not the same as none.")
    elif report.commits:
        sections.append("\n".join(
            [f"Commits ({len(report.commits)}):"]
            + [f"  {line}" for line in report.commits]))
    else:
        # Said out loud rather than omitted. A missing section reads as a
        # rendering slip; "none" is an answer the reader can act on.
        sections.append("Commits: none.")

    if not report.first_run and not report.changes_known:
        sections.append("Backlog files touched: could not be listed (see the "
                        "caveats below); this is not the same as none.")
    elif report.item_changes:
        sections.append("\n".join(
            ["Backlog files touched:"]
            + [f"  {code} {name}" for code, name in report.item_changes]))

    sections.append("Backlog: " + ", ".join(
        f"{count} {status}" for status, count in report.counts))

    sections.append("\n".join(
        [f"Ready to work ({len(report.ready)}):"]
        + ([f"  {item_id:>4}  {title}" for item_id, title in report.ready]
           or ["  (nothing; every item is done, or waiting on you)"])))

    sections.append("\n".join(
        [f"Awaiting your approval ({len(report.awaiting)}):"]
        + ([f"  {item_id:>4}  {title}" for item_id, title in report.awaiting]
           or ["  (nothing)"])))

    if report.problems:
        sections.append("\n".join(
            [f"Conformance problems ({len(report.problems)}):"]
            + [f"  {problem}" for problem in report.problems]))
    if report.notes:
        sections.append("\n".join(
            ["Caveats on this report:"]
            + [f"  - {note}" for note in report.notes]))
    return "\n\n".join(sections)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_HTML_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;",
                 '"': "&quot;", "'": "&#39;"}


def _escape(text: str) -> str:
    return "".join(_HTML_ESCAPES.get(ch, ch) for ch in text)


def _embed_json(payload) -> str:
    """Serialise ``payload`` for a ``<script>`` block it cannot escape from.

    ``</script>`` inside a JSON string ends the block as far as an HTML parser
    is concerned, no matter that JSON considers it ordinary text -- so the
    three characters that could begin such a sequence are emitted as ``\\uXXXX``
    escapes. JSON and JavaScript both read those back as the original
    characters, so nothing is lost, and no item title can break the page or
    inject markup into it.
    """
    return (json.dumps(payload, ensure_ascii=False)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026"))


def render_html(repo_root=None, *, resolve_commits: bool = True) -> str:
    """A self-contained HTML view of the backlog.

    Self-contained is the requirement, not a nicety. A page that fetched its
    items on load would work when served over HTTP and fail silently when
    opened from disk: browsers treat every ``file://`` document as an opaque
    origin, so ``fetch('0001-x.md')`` is refused by CORS and the page renders
    an empty backlog with no error a reader would see. Embedding the data at
    generation time removes the failure mode entirely, and generating on demand
    means the page cannot be stale the way a committed copy would be.
    """
    root = Path(repo_root) if repo_root is not None else checkout_root()
    items, parse_problems = load(root / BACKLOG_DIRNAME)
    problems = check(root, resolve_commits=resolve_commits)
    index = by_filename_id(items)

    payload = {
        "repo": root.name,
        "root": str(root),
        "problems": problems,
        "items": [
            {
                "id": item.front.get("id", ""),
                "file": item.name,
                "title": item.title,
                "status": item.status,
                "opened": item.front.get("opened", ""),
                "closed": item.front.get("closed", ""),
                "commit": item.front.get("commit", ""),
                "spec": item.front.get("spec", ""),
                "requirement": item.front.get("requirement", ""),
                "blocks": item.front.get("blocks", ""),
                # Why an agent may not pick this up, in the same words the
                # `ready --explain` queue uses. A view that showed status but
                # not eligibility would leave a reader to re-derive the gate,
                # and a re-derived rule is a second rule.
                "blocked_because": why_not_workable(item, index) or "",
                "body": item.body,
            }
            for item in items
        ],
        "statuses": list(STATUSES),
    }
    del parse_problems  # already folded into `problems` by check()

    return _PAGE.replace("__DATA__", _embed_json(payload))


_PAGE = """<!DOCTYPE html>
<meta charset="utf-8">
<title>Backlog</title>
<style>
 :root { color-scheme: light dark; }
 body { font: 15px/1.5 ui-sans-serif, system-ui, sans-serif;
        margin: 0 auto; max-width: 60rem; padding: 2rem 1rem; }
 h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
 .sub { opacity: .6; font-size: .85rem; margin-bottom: 1.5rem; }
 .problems { border-left: 3px solid #c0392b; background: #c0392b18;
             padding: .75rem 1rem; margin-bottom: 1.5rem; border-radius: 3px; }
 .problems ul { margin: .5rem 0 0; padding-left: 1.2rem; }
 .ok { border-left: 3px solid #27ae60; background: #27ae6018;
       padding: .6rem 1rem; margin-bottom: 1.5rem; border-radius: 3px; }
 nav { display: flex; gap: .5rem; margin-bottom: 1rem; flex-wrap: wrap; }
 button { font: inherit; padding: .3rem .8rem; border-radius: 999px;
          border: 1px solid currentColor; background: transparent;
          cursor: pointer; opacity: .55; }
 button.on { opacity: 1; font-weight: 600; }
 .item { border: 1px solid #8884; border-radius: 5px; margin-bottom: .6rem; }
 .head { display: flex; gap: .7rem; align-items: baseline; cursor: pointer;
         padding: .7rem .9rem; }
 .head:hover { background: #8881; }
 .id { font-variant-numeric: tabular-nums; opacity: .5; }
 .title { flex: 1; font-weight: 600; }
 .tag { font-size: .72rem; text-transform: uppercase; letter-spacing: .06em;
        padding: .12rem .5rem; border-radius: 999px; border: 1px solid; }
 .open { color: #d68910; } .closed { color: #27ae60; } .rejected { opacity: .5; }
 .proposed { color: #8e44ad; }
 .meta { display: flex; gap: 1.2rem; flex-wrap: wrap; font-size: .8rem;
         opacity: .7; padding: 0 .9rem .5rem; }
 .body { padding: 0 .9rem 1rem; border-top: 1px solid #8884; margin-top: .3rem; }
 .body h4 { margin: 1rem 0 .3rem; font-size: .95rem; }
 .body p { margin: .3rem 0; white-space: pre-wrap; }
 .hidden { display: none; }
 code { background: #8882; padding: .05rem .3rem; border-radius: 3px; }
</style>
<body>
<h1>Backlog</h1>
<div class="sub" id="sub"></div>
<div id="health"></div>
<nav id="filters"></nav>
<div id="items"></div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const esc = s => { const d = document.createElement('div');
                   d.textContent = s; return d.innerHTML; };

document.getElementById('sub').textContent =
  D.repo + ' \\u2014 ' + D.items.length + ' item' +
  (D.items.length === 1 ? '' : 's') + ' \\u2014 ' + D.root;

const health = document.getElementById('health');
if (D.problems.length) {
  health.className = 'problems';
  health.innerHTML = '<strong>' + D.problems.length +
    ' conformance problem' + (D.problems.length === 1 ? '' : 's') +
    '</strong><ul>' +
    D.problems.map(p => '<li>' + esc(p) + '</li>').join('') + '</ul>';
} else {
  health.className = 'ok';
  health.textContent = 'All conformance checks pass.';
}

// Markdown is shown, not interpreted: headings become headings and everything
// else is escaped text with its line breaks kept. Rendering it properly would
// mean shipping a parser, and an item body is prose in a table, not a document.
function bodyHtml(md) {
  return md.split(/\\n{2,}/).map(block => {
    const m = block.match(/^(#{1,6})\\s+(.*)$/s);
    if (m) {
      const rest = m[2].split('\\n');
      return '<h4>' + esc(rest.shift()) + '</h4>' +
             (rest.length ? '<p>' + esc(rest.join('\\n')) + '</p>' : '');
    }
    return '<p>' + esc(block) + '</p>';
  }).join('');
}

let active = 'all';
const counts = {all: D.items.length};
for (const s of D.statuses) counts[s] = D.items.filter(i => i.status === s).length;

const filters = document.getElementById('filters');
for (const key of ['all', ...D.statuses]) {
  const b = document.createElement('button');
  b.textContent = key + ' (' + (counts[key] || 0) + ')';
  b.onclick = () => { active = key; draw(); };
  b.dataset.key = key;
  filters.appendChild(b);
}

function draw() {
  for (const b of filters.children) b.className = b.dataset.key === active ? 'on' : '';
  const host = document.getElementById('items');
  host.innerHTML = '';
  const shown = D.items.filter(i => active === 'all' || i.status === active);
  if (!shown.length) {
    host.innerHTML = '<p style="opacity:.6">Nothing with this status.</p>';
    return;
  }
  for (const i of shown) {
    const el = document.createElement('div');
    el.className = 'item';
    const meta = [];
    meta.push('opened ' + esc(i.opened));
    if (i.closed) meta.push('closed ' + esc(i.closed));
    if (i.commit) meta.push('commit <code>' + esc(i.commit.slice(0, 12)) + '</code>');
    meta.push('spec <code>' + esc(i.spec) + '</code>');
    if (i.requirement) meta.push('requirement \\u201c' + esc(i.requirement) + '\\u201d');
    if (i.blocks) meta.push('blocks #' + esc(i.blocks));
    if (i.blocked_because) meta.push('not workable: ' + esc(i.blocked_because));
    meta.push('<code>' + esc(i.file) + '</code>');
    el.innerHTML =
      '<div class="head"><span class="id">#' + esc(i.id) + '</span>' +
      '<span class="title">' + esc(i.title) + '</span>' +
      '<span class="tag ' + esc(i.status) + '">' + esc(i.status) + '</span></div>' +
      '<div class="meta">' + meta.map(m => '<span>' + m + '</span>').join('') + '</div>' +
      '<div class="body hidden">' + bodyHtml(i.body) + '</div>';
    const body = el.querySelector('.body');
    el.querySelector('.head').onclick = () => body.classList.toggle('hidden');
    host.appendChild(el);
  }
}
draw();
</script>
</body>
"""


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _default_html_path(root: Path) -> Path:
    """A stable, bookmarkable path outside the checkout.

    Outside is the point: this repository's test suite fails on stray untracked
    files in a checkout, and a generated view is exactly that. A stable name
    beats a fresh temp directory because a human can bookmark it once.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", root.name) or "repo"
    return Path(tempfile.gettempdir()) / f"backlog-{safe}.html"


def _cmd_list(args, root: Path) -> int:
    items, problems = load(root / BACKLOG_DIRNAME)
    for problem in problems:
        print(f"unparsed: {problem}", file=sys.stderr)
    wanted = args.status
    shown = [i for i in items if not wanted or i.status == wanted]
    if not shown:
        print("(no matching items)")
        return 0
    width = max(len(i.status) for i in shown)
    for item in shown:
        print(f"{item.filename_id:>4}  {item.status:<{width}}  {item.title}")
    return 0


def _cmd_show(args, root: Path) -> int:
    try:
        paths = item_paths(root / BACKLOG_DIRNAME)
    except BacklogFormatError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for item in paths:
        if int(ITEM_FILENAME.match(item.name).group(1)) == args.id:
            print(item.read_text(encoding="utf-8"))
            return 0
    print(f"no backlog item with id {args.id}", file=sys.stderr)
    return 1


def _cmd_check(args, root: Path) -> int:
    problems = check(root)
    if not problems:
        items, _ = load(root / BACKLOG_DIRNAME)
        count = len(items)
        print(f"backlog ok: {count} item{'' if count == 1 else 's'}")
        return 0
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"{len(problems)} problem(s)", file=sys.stderr)
    return 1


def _cmd_html(args, root: Path) -> int:
    out = Path(args.out) if args.out else _default_html_path(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(root), encoding="utf-8")
    print(out)
    if args.open:
        try:
            webbrowser.open(Path(resolved_str(out)).as_uri())
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"wrote {out} but could not open a browser: {exc}",
                  file=sys.stderr)
    return 0


def _cmd_ready(args, root: Path) -> int:
    """The queue an agent may work, and why everything else is not in it."""
    items, problems = load(root / BACKLOG_DIRNAME)
    for problem in problems:
        print(f"unparsed: {problem}", file=sys.stderr)
    index = by_filename_id(items)
    ready = [i for i in items if why_not_workable(i, index) is None]
    for item in ready:
        print(f"{item.filename_id:>4}  {item.status:<8}  {item.title}")
    if not ready:
        print("(nothing is ready to work)")
    if args.explain:
        print("", file=sys.stderr)
        for item in items:
            reason = why_not_workable(item, index)
            if reason is not None:
                print(f"{item.filename_id:>4}  not workable: {reason}",
                      file=sys.stderr)
    return 0


def _cmd_new(args, root: Path) -> int:
    evidence = args.evidence or ""
    if args.evidence_file:
        try:
            evidence = Path(args.evidence_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"cannot read {args.evidence_file}: {exc}", file=sys.stderr)
            return 1
    try:
        path = create_item(
            root / BACKLOG_DIRNAME, title=args.title, evidence=evidence,
            status=PROPOSED_STATUS, spec=args.spec, blocks=args.blocks,
            why=args.why or "", notes=args.notes or "", slug=args.slug)
    except BacklogFormatError as exc:
        print(f"refusing to file this item: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"cannot write the item: {exc}", file=sys.stderr)
        return 1
    print(path)
    # Validate what was just written, in the same breath. A tool that files an
    # item the checker then rejects has moved the failure to whoever runs the
    # suite next, with no clue that this command caused it.
    remaining = [p for p in check(root) if p.startswith(path.name)]
    if remaining:
        for problem in remaining:
            print(problem, file=sys.stderr)
        print("the item was written, and it does not conform; fix it in place",
              file=sys.stderr)
        return 1
    return 0


def _cmd_approve(args, root: Path) -> int:
    try:
        path = approve_item(root / BACKLOG_DIRNAME, args.id)
    except BacklogFormatError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"cannot rewrite the item: {exc}", file=sys.stderr)
        return 1
    print(f"{path.name}: {PROPOSED_STATUS} -> {OPEN_STATUS}")
    return 0


def _cmd_scrum(args, root: Path) -> int:
    try:
        mark = watermark_path(root)
        recorded = read_watermark(mark)
    except WatermarkError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    report = scrum_report(root, recorded)
    print(format_scrum(report))
    if args.peek:
        print("\n(--peek: the watermark was left where it was, so the next "
              "check-in covers this period again)")
        return 0
    try:
        write_watermark(mark, report.head)
    except WatermarkError as exc:
        # Loudly, and with a non-zero exit. A check-in that reported fine and
        # failed to move its watermark repeats itself next time, and a repeated
        # report looks like a week in which nothing happened.
        print(f"\n{exc}", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    enable_utf8_output()
    parser = argparse.ArgumentParser(
        prog="backlog",
        description="Read and validate the repository's tracked backlog.")
    parser.add_argument("-C", "--repo", default=None,
                        help="a path inside the repository (default: cwd)")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="one line per item")
    p_list.add_argument("--status", choices=STATUSES, default=None)
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="print one item in full")
    p_show.add_argument("id", type=int)
    p_show.set_defaults(func=_cmd_show)

    sub.add_parser("check", help="validate every item").set_defaults(
        func=_cmd_check)

    p_ready = sub.add_parser(
        "ready", help="the items an agent is allowed to work")
    p_ready.add_argument("--explain", action="store_true",
                         help="say why each other item is not in the queue")
    p_ready.set_defaults(func=_cmd_ready)

    p_new = sub.add_parser(
        "new", help=f"file a new item, always as {PROPOSED_STATUS}")
    p_new.add_argument("--title", required=True)
    p_new.add_argument("--evidence", default=None,
                       help="what was observed, when, and how to reproduce it")
    p_new.add_argument("--evidence-file", default=None,
                       help="read the evidence section from this file")
    p_new.add_argument("--why", default=None, help="the 'Why it matters' body")
    p_new.add_argument("--notes", default=None, help="the 'Notes' body")
    # There is deliberately no --status. Filing is not approving, and a flag
    # that let this command write 'open' would be the tool publishing the
    # bypass in its own --help: an agent needs no cunning to find a documented
    # option. Approval is `backlog approve`, which is one act, by one person,
    # and shows up as one line in a diff.
    p_new.add_argument("--spec", default=NO_SPEC)
    p_new.add_argument("--blocks", type=int, default=None,
                       help="the approved item this one is blocking")
    p_new.add_argument("--slug", default=None,
                       help="override the slug derived from the title")
    p_new.set_defaults(func=_cmd_new)

    p_approve = sub.add_parser(
        "approve", help=f"the product owner's act: {PROPOSED_STATUS} -> "
                        f"{OPEN_STATUS}")
    p_approve.add_argument("id", type=int)
    p_approve.set_defaults(func=_cmd_approve)

    p_scrum = sub.add_parser(
        "scrum", help="what changed since the previous check-in")
    p_scrum.add_argument("--peek", action="store_true",
                         help="report without advancing the watermark")
    p_scrum.set_defaults(func=_cmd_scrum)

    p_html = sub.add_parser("html", help="write a self-contained HTML view")
    p_html.add_argument("--out", default=None)
    p_html.add_argument("--open", action="store_true",
                        help="open the page in a browser")
    p_html.set_defaults(func=_cmd_html)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["list"] + (["-C", args.repo] if args.repo
                                             else []))
    root = checkout_root(args.repo) if args.repo else checkout_root()
    return args.func(args, Path(root))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
