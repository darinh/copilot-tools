#!/usr/bin/env python3
"""Move the workflow conventions out of user scope and into each project.

``setup`` used to copy ``templates/copilot-instructions.md`` to
``~/.copilot/copilot-instructions.md``. A file at that path is loaded into
**every** Copilot session on the machine, in every directory, whether or not
the directory is a project and whether or not the operator is involved -- and
the first thing its "On Session Start -- Project Lookup" section tells an
agent to do is resolve a project root, read the catalog, and offer to enroll
the working directory when there is no match. At user scope there is no way
for that instruction to *not* run, so opening a terminal anywhere on the
machine started a conversation about setting up a project.

The fix is not to soften the wording. It is to put the conventions where the
consent already exists: a per-repository ``AGENTS.md`` in each project that
was actually catalogued. A project-scoped file also knows something the global
one never could -- *which* project it is -- so the catalog lookup, the
enrollment offer and the feature table all collapse into a handful of resolved
facts.

Three properties are load-bearing, and each of them is a scar:

* **Removal and replacement are not separable.** Every project gets its
  ``AGENTS.md`` before the global file is even archived, and a single project
  that could not be written stops the removal. The failure mode is a machine
  with the conventions in two places, never one with them in none.

* **Nothing is deleted without a preserved copy, verified by reading it
  back.** The archive is written, flushed, re-read and digest-compared before
  the original is unlinked. ``handoff_tool``'s ``superseded/`` directory
  exists because a file nobody had read was overwritten; this is the same
  promise for a file the user may have spent a year editing.

* **An existing ``AGENTS.md`` is never overwritten.** A repository that
  already has one gets the managed block appended, and only with the caller's
  consent. Re-running replaces that block and leaves everything around it
  byte-for-byte, so this is idempotent without being destructive.
"""
from __future__ import annotations

import hashlib
import ntpath
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import install_manifest
import project_features
from install_manifest import file_digest, path_present

__all__ = [
    "InstructionsError", "AGENTS_NAME", "MANAGED_BEGIN", "MANAGED_END",
    "CONFIGURATION_SECTION", "ARCHIVE_DIRNAME", "GATE", "Section",
    "TEMPLATE_KEY", "TEMPLATE_NAME", "GLOBAL_NAME",
    "split_sections", "gate_slug", "render", "compose", "managed_block_present",
    "write_text_atomic", "preserve", "user_scope_agents_files", "resolve_source",
    "WRITTEN", "MERGED", "UNCHANGED", "DECLINED", "MISSING", "FAILED",
    "BLOCKING_STATES", "ProjectOutcome", "RetirementResult", "retire",
]

#: The file this module retires, and the manifest key setup recorded it under.
GLOBAL_NAME = "copilot-instructions.md"
TEMPLATE_NAME = "copilot-instructions.md"
TEMPLATE_KEY = f"templates/{TEMPLATE_NAME}"


class InstructionsError(RuntimeError):
    """A file could not be read, understood or written."""


#: The per-repository file. ``AGENTS.md`` rather than
#: ``.github/copilot-instructions.md`` because it is the spelling every agent
#: tool reads, not only this one, and because a repository that already has
#: one is a repository whose author has already decided where this content
#: goes.
AGENTS_NAME = "AGENTS.md"

MANAGED_BEGIN = "<!-- BEGIN copilot-tools managed conventions -->"
MANAGED_END = "<!-- END copilot-tools managed conventions -->"

#: The template section that is *replaced* rather than copied. It is the
#: enrollment machinery -- catalog lookup, "would you like to set this up",
#: the feature table -- and it is the entire reason a user-scoped instructions
#: file was a problem. A project file answers those questions instead of
#: asking them. ``tests/test_project_instructions.py`` pins this string
#: against the template so a renamed heading fails loudly rather than
#: silently reinstating the section it exists to remove.
CONFIGURATION_SECTION = "Project Configuration System"

#: Where a retired user-scope file is kept. Beside ``superseded/`` in spirit
#: and for the same reason: nothing prunes it, because the only files that
#: arrive here are ones somebody may still need.
ARCHIVE_DIRNAME = "retired"

#: The marker a template section carries to say which feature turns it on.
#: One definition, imported by the renderer *and* by the conformance tests, so
#: the pattern that decides what ships cannot drift from the pattern that
#: checks it.
GATE = re.compile(r"^\*Enabled by feature flag: `(?P<slug>[a-z0-9-]+)`\*\s*$",
                  re.MULTILINE)

_H2 = re.compile(r"^## (?P<title>.+?)\s*$")

#: A fence is a run of at least three backticks or tildes. The *run* is
#: captured, not a fixed three characters: a block opened with ```` may
#: contain ``` lines, and CommonMark closes it only on a run of the same
#: character at least as long, with nothing but whitespace after it. Reading
#: an inner ``` as the close puts the rest of the example back into the
#: document as prose -- and the examples in this template have `## ` headings
#: at column zero, so those fragments then become sections with no gate on
#: them, which is how content for a feature that is *off* gets emitted.
_FENCE_OPEN = re.compile(r"^(?P<run>`{3,}|~{3,})")
_FENCE_INFO_TICK = "`"


# --------------------------------------------------------------------------
# Reading the template
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Section:
    """One ``## `` section: its title, and everything below the heading."""

    title: str
    body: str


def _basename(path: str) -> str:
    """The last component of a path written in *either* platform's syntax.

    ``ntpath`` rather than ``os.path``: a catalog row is written in the native
    form of the machine that created it, and this toolkit runs on both. On
    Linux ``os.path.basename(r"C:\\repos\\app")`` is the whole string, because
    a backslash is an ordinary filename character there — so the label would
    be the full path. ``ntpath`` is pure syntax and understands both
    separators and drive prefixes, so it is the union of the two rather than a
    guess at which one this row came from.
    """
    return ntpath.basename(str(path).rstrip("/\\"))


def _closes_fence(stripped: str, opening: str) -> bool:
    """Whether a line ends the fence opened by ``opening``.

    CommonMark's rule, not ``startswith``: the same character, a run at least
    as long, and nothing but whitespace afterwards. All three matter here.
    Same-character, because ``~~~`` does not close ```` ``` ````. At least as
    long, because a ``` line inside a ```` block is content. And no info
    string, because ```` ```python ```` opens a nested block rather than
    closing the outer one -- which is exactly the shape a document about
    writing Markdown contains.
    """
    opened = _FENCE_OPEN.match(stripped)
    if opened is None:
        return False
    run = opened.group("run")
    if run[0] != opening[0] or len(run) < len(opening):
        return False
    rest = stripped[len(run):]
    if opening[0] == _FENCE_INFO_TICK:
        return rest.strip() == ""
    # A tilde fence may carry an info string on the opening line, so a closing
    # tilde run is only a close when nothing follows it either way.
    return rest.strip() == ""


def outside_fences(text: str):
    """Yield ``(index, line)`` for every line that is not inside a fence.

    Marker hunting has to use this. A repository's own ``AGENTS.md`` may well
    contain this toolkit's marker lines *as an example* -- documentation about
    the managed block is the obvious case -- and a raw substring search reads
    that sample as a real managed block. The consequence is not cosmetic:
    ``_place_one`` skips the consent prompt when it believes a managed block
    is present, and ``compose`` then overwrites everything between the two
    sampled lines. That is user content destroyed without being asked.
    """
    fence = ""
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if fence:
            if _closes_fence(stripped, fence):
                fence = ""
            continue
        opened = _FENCE_OPEN.match(stripped)
        if opened is not None:
            fence = opened.group("run")
            continue
        yield index, line


def split_sections(text: str) -> "tuple[str, list[Section]]":
    """``(preamble, sections)`` for a Markdown document.

    Fence-aware, and that is not a nicety. The template's handoff section
    contains a fenced example of a handoff file, and that example has ``##
    Status``, ``## Next Steps`` and ``## Context`` headings inside it at
    column zero. A splitter that matched ``^## `` on the raw text would cut
    the section in half at its own example, and the tail would become four
    sections with no gate marker on them -- so turning ``session-handoff``
    off would drop the prose and keep the fragments, which is worse than
    keeping all of it. The same shape occurs in the backlog and field-notes
    examples.

    An unterminated fence swallows the rest of the document into one section.
    That is reported by the caller's conformance test rather than guessed at
    here: silently "recovering" would mean the splitter disagrees with every
    Markdown renderer about where a section ends.
    """
    preamble: list[str] = []
    sections: list[Section] = []
    title: "str | None" = None
    body: list[str] = []
    fence = ""
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if fence:
            if _closes_fence(stripped, fence):
                fence = ""
            target = body if title is not None else preamble
            target.append(line)
            continue
        opened = _FENCE_OPEN.match(stripped)
        if opened is not None:
            fence = opened.group("run")
            target = body if title is not None else preamble
            target.append(line)
            continue
        heading = _H2.match(line.rstrip("\n"))
        if heading is None:
            target = body if title is not None else preamble
            target.append(line)
            continue
        if title is not None:
            sections.append(Section(title, "".join(body)))
        title = heading.group("title")
        body = []
    if title is not None:
        sections.append(Section(title, "".join(body)))
    return "".join(preamble), sections


def gate_slug(body: str) -> "str | None":
    """The feature slug a section is gated behind, or ``None`` if it is not.

    Only the first marker counts. A section carrying two would be a section
    with two answers to "may this ship", and the template is pinned against
    the vocabulary in both directions, so the second one has nowhere to come
    from that is not a mistake.
    """
    match = GATE.search(body)
    return match.group("slug") if match else None


# --------------------------------------------------------------------------
# Rendering one project's conventions
# --------------------------------------------------------------------------

def _configuration_section(guid: str, project_dir_path, config_path) -> str:
    """The replacement for the enrollment section.

    Everything the original told an agent to *derive* is written down here
    already, because a file that lives in the project knows which project it
    is. Nothing in it can start an enrollment conversation, which is the
    whole point.
    """
    return (
        f"## {CONFIGURATION_SECTION}\n"
        "\n"
        "This project is already registered. Nothing here needs setting up, and\n"
        "**you must not offer to enroll this directory or write to the project\n"
        "catalog** — that has been done.\n"
        "\n"
        f"- Project id: `{guid}`\n"
        f"- Project directory: `{project_dir_path}`\n"
        f"- Feature settings: `{config_path}`\n"
        "\n"
        "The project directory holds this project's session handoff and any other\n"
        "state that must persist outside the repository. Read or change the\n"
        "feature settings with:\n"
        "\n"
        "```\n"
        "operator projects\n"
        "```\n"
        "\n"
        "The sections below are the ones this project's features turned on;\n"
        "sections for features that are off were left out when this block was\n"
        "generated rather than being gated in prose.\n"
    )


def _header(label: str, project_path: str, values: dict, version: str) -> str:
    return (
        f"# {label} — working conventions\n"
        "\n"
        f"{project_features.enabled_features_line(values)}\n"
        "\n"
        f"Generated by copilot-tools {version} for `{project_path}`.\n"
        "Everything between the markers is regenerated by `operator projects`;\n"
        "write anything of your own outside them.\n"
    )


def render(*, source: str, values: dict, guid: str, project_path: str,
           label: str, project_dir_path, config_path, version: str) -> str:
    """The managed block for one project, markers included.

    Deterministic: the same inputs produce the same bytes, with no timestamp
    anywhere in it. A block that embedded the time it was written would show
    up as a diff in every repository every time anything regenerated it, and
    a diff that is always there is a diff nobody reads.
    """
    _preamble, sections = split_sections(source)
    parts = [_header(label, project_path, values, version)]
    for section in sections:
        if section.title == CONFIGURATION_SECTION:
            parts.append(_configuration_section(guid, project_dir_path,
                                                config_path))
            continue
        slug = gate_slug(section.body)
        if slug is not None:
            feature = project_features.FEATURES_BY_SLUG.get(slug)
            # A slug the vocabulary does not know is kept rather than dropped.
            # It cannot be turned on, so dropping it would silently delete
            # conventions on a build that is merely older than the file it is
            # reading — the same downgrade-as-data-loss that
            # ``project_features.write_config`` refuses.
            if feature is not None and not project_features.is_enabled(values, slug):
                continue
        parts.append(f"## {section.title}\n{section.body}")
    body = "\n".join(part.rstrip("\n") + "\n" for part in parts)
    return f"{MANAGED_BEGIN}\n\n{body}\n{MANAGED_END}\n"


# --------------------------------------------------------------------------
# Placing it in a repository
# --------------------------------------------------------------------------

def _marker_offsets(existing: str) -> "tuple[list[int], list[int]]":
    """Byte offsets of the begin and end markers that are really markers.

    Two narrowings, and both close a way to destroy a repository's own file:

    Markers inside a fenced block do not count. An ``AGENTS.md`` that
    *documents* the managed block quotes these lines in a code sample, and
    reading that sample as a live block means ``_place_one`` skips the consent
    prompt and ``compose`` overwrites the sample and everything between.

    A marker must also be the whole line. ``MANAGED_BEGIN in existing`` is
    true of a sentence that merely mentions it, and prose is not a delimiter.
    """
    begins: list[int] = []
    ends: list[int] = []
    offset = 0
    lines = existing.splitlines(keepends=True)
    plain = {index for index, _ in outside_fences(existing)}
    for index, line in enumerate(lines):
        if index in plain:
            stripped = line.strip()
            if stripped == MANAGED_BEGIN:
                begins.append(offset)
            elif stripped == MANAGED_END:
                ends.append(offset + len(line.rstrip("\r\n")))
        offset += len(line)
    return begins, ends


def managed_block_present(existing: str) -> bool:
    begins, ends = _marker_offsets(existing)
    return bool(begins) or bool(ends)


def compose(existing: "str | None", managed: str) -> str:
    """``existing`` with ``managed`` put in it, keeping everything else.

    An existing file is never replaced. Either it already carries a managed
    block, in which case that block and nothing else is swapped out, or the
    block is appended below whatever is there. A repository's ``AGENTS.md`` is
    its author's document; this only ever rents a paragraph of it.

    Malformed markers raise instead of being repaired. Appending a second
    block "because the first one looked wrong" is how a file ends up with two
    sets of conventions that disagree, and the disagreement would then be
    invisible to the very function meant to keep them in step.
    """
    if existing is None or not existing.strip():
        return managed
    begins, ends = _marker_offsets(existing)
    if not begins and not ends:
        return existing.rstrip("\n") + "\n\n" + managed
    if len(begins) != 1 or len(ends) != 1:
        raise InstructionsError(
            f"found {len(begins)} '{MANAGED_BEGIN}' and {len(ends)} "
            f"'{MANAGED_END}' markers; expected one of each. Fix them by "
            "hand — refusing to guess which block is the live one.")
    start = begins[0]
    stop = ends[0]
    if stop < start:
        raise InstructionsError(
            "the managed-conventions end marker comes before its begin "
            "marker. Fix them by hand.")
    tail = existing[stop:]
    return existing[:start] + managed.rstrip("\n") + tail


def write_text_atomic(path, text: str) -> None:
    """Write ``text`` to ``path`` via a sibling temp file and ``os.replace``.

    The same shape as ``project_features.write_config`` and for the same
    reasons: ``mkstemp`` inside the ``try`` so an interrupt cannot strand a
    file no cleanup can still see, ``fsync`` before the rename so a directory
    entry cannot outlive its data, and cleanup in ``finally`` rather than
    ``except OSError`` because a KeyboardInterrupt is not an OSError.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd: "int | None" = None
        tmp: "str | None" = None
        placed = False
        try:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                                       prefix=f".{path.name}-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fd = None
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            placed = True
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp is not None and not placed:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except OSError as exc:
        raise InstructionsError(f"Cannot write {path}: {exc}") from exc


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def archive_name(path, digest: str, when: datetime) -> str:
    """A name that cannot collide and says what it holds.

    The digest is in the name so two archives of the same bytes are visibly
    the same file, and the timestamp is there so two archives of *different*
    bytes sort. Both, because either alone loses one of those.
    """
    stem = Path(path).stem or "file"
    suffix = Path(path).suffix or ".md"
    return f"{stem}-{when.strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}{suffix}"


def preserve(path, archive_dir, *, when: "datetime | None" = None) -> Path:
    """Copy ``path`` into ``archive_dir`` and prove the copy arrived.

    Returns where it landed. Raises if anything at all went wrong, because
    the only caller unlinks the original next and a preserved copy that was
    never verified is the same as no copy at all — it just reads better in a
    log.

    The read-back is a digest comparison rather than a size check. A short
    write, a full disk and a filesystem that reported success and dropped the
    data all produce a file of plausible length; only the content answers.
    """
    path = Path(path)
    archive_dir = Path(archive_dir)
    try:
        original = path.read_bytes()
    except OSError as exc:
        raise InstructionsError(f"Cannot read {path} to preserve it: {exc}") from exc
    digest = hashlib.sha256(original).hexdigest()
    stamp = when or datetime.now(timezone.utc)
    target = archive_dir / archive_name(path, digest, stamp)
    already = path_present(target)
    if already is None:
        raise InstructionsError(
            f"Something is at {target} and could not be examined, so this "
            "copy cannot be verified. Nothing was removed.")
    if already:
        # Same name means same bytes: the digest is in it. Nothing to do, and
        # rewriting would only risk damaging a copy that is already correct.
        return target
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(archive_dir), prefix=".retiring-")
        placed = False
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(original)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
            placed = True
        finally:
            if not placed:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except OSError as exc:
        raise InstructionsError(
            f"Cannot preserve {path} in {archive_dir}: {exc}") from exc
    written = file_digest(target)
    if written != digest:
        raise InstructionsError(
            f"The preserved copy at {target} does not match {path} "
            f"({written} != {digest}). Nothing was removed.")
    return target


def resolve_source(template_path, global_path, manifest) -> "tuple[str, str]":
    """``(text, origin)`` -- which document this machine's conventions are in.

    Usually the repository's template, which is the newest wording. Not
    always: the deployed copy is an ordinary file the user may have spent a
    year editing, and generating every project's ``AGENTS.md`` from the
    pristine template would delete those edits from the machine in the same
    operation that deletes the file holding them. So the manifest is asked
    the question it exists to answer -- *did the user change this since setup
    wrote it* -- and an edited copy wins.

    This is deliberately the same predicate ``install_templates`` uses to
    decide whether it may overwrite. A file precious enough not to clobber is
    precious enough not to discard.
    """
    template_path = Path(template_path)
    global_path = Path(global_path)
    template_digest = file_digest(template_path)
    state = install_manifest.classify(manifest, TEMPLATE_KEY, global_path,
                                      template_digest)
    prefer_global = state in (install_manifest.MODIFIED,
                              install_manifest.UNTRACKED)
    order = ((global_path, "the deployed file, which has local edits"),
             (template_path, "the repository template")) if prefer_global else (
            (template_path, "the repository template"),
            (global_path, "the deployed file"))
    problems = []
    for path, origin in order:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"{path}: {exc}")
            continue
        if not text.strip():
            problems.append(f"{path}: empty")
            continue
        return text, f"{origin} ({path})"
    raise InstructionsError(
        "No usable source for the conventions: " + "; ".join(problems))


def user_scope_agents_files(home) -> "list[Path]":
    """Any ``AGENTS.md`` already loaded at user scope, for reporting only.

    Never touched. The user put it there and it is not this toolkit's
    artifact; the most useful thing to do with it is say it exists, because
    a machine-wide instructions file is exactly what everything here is
    trying to stop being surprised by.
    """
    home = Path(home)
    found = []
    for candidate in (home / ".copilot" / AGENTS_NAME, home / AGENTS_NAME):
        if path_present(candidate) is not False:
            found.append(candidate)
    return found


# --------------------------------------------------------------------------
# The retirement itself
# --------------------------------------------------------------------------

#: The managed block was added to a repository that had no ``AGENTS.md``.
WRITTEN = "written"
#: A repository that already had one; the block was added or refreshed in it.
MERGED = "merged"
#: The block was already exactly right. Nothing was written.
UNCHANGED = "unchanged"
#: The caller was asked whether to combine with an existing file and said no.
DECLINED = "declined"
#: The project's directory is not on this machine right now.
MISSING = "missing"
#: Something went wrong writing it.
FAILED = "failed"

#: States that stop the user-scope file being removed. ``DECLINED`` and
#: ``FAILED`` are obvious. ``MISSING`` is the interesting one: an unplugged
#: drive or an unsynced clone is a project that will come back, and removing
#: the global file while it is away is precisely the gap this module promises
#: never to open. A caller may override it, but only as something a person
#: chose in front of the list.
BLOCKING_STATES = (DECLINED, MISSING, FAILED)


@dataclass(frozen=True)
class ProjectOutcome:
    guid: str
    path: str
    label: str
    state: str
    detail: str = ""
    agents_path: "Path | None" = None

    @property
    def blocking(self) -> bool:
        return self.state in BLOCKING_STATES


@dataclass
class RetirementResult:
    outcomes: "list[ProjectOutcome]" = field(default_factory=list)
    archived: "Path | None" = None
    removed: bool = False
    user_agents: "list[Path]" = field(default_factory=list)
    source_origin: str = ""
    problems: "list[str]" = field(default_factory=list)

    @property
    def blockers(self) -> "list[ProjectOutcome]":
        return [o for o in self.outcomes if o.blocking]

    @property
    def placed(self) -> "list[ProjectOutcome]":
        return [o for o in self.outcomes
                if o.state in (WRITTEN, MERGED, UNCHANGED)]


def _place_one(project: dict, *, source: str, version: str, projects_root,
               decide) -> ProjectOutcome:
    guid = project["guid"]
    root = Path(project["path"])
    label = project.get("label") or _basename(project["path"]) or project["path"]
    present = path_present(root)
    if present is False:
        return ProjectOutcome(guid, project["path"], label, MISSING,
                              "the project directory is not on this machine")
    if present is None:
        return ProjectOutcome(guid, project["path"], label, FAILED,
                              "the project directory could not be examined")
    project_dir_path = Path(projects_root) / guid
    managed = render(
        source=source,
        values=_values_for(guid, projects_root),
        guid=guid,
        project_path=project["path"],
        label=label,
        project_dir_path=project_dir_path,
        config_path=project_dir_path / project_features.CONFIG_NAME,
        version=version,
    )
    target = root / AGENTS_NAME
    exists = path_present(target)
    if exists is None:
        return ProjectOutcome(guid, project["path"], label, FAILED,
                              f"{target} could not be examined", target)
    existing: "str | None" = None
    if exists:
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError as exc:
            return ProjectOutcome(guid, project["path"], label, FAILED,
                                  f"{target} could not be read: {exc}", target)
        if not managed_block_present(existing) and not decide(project, existing):
            return ProjectOutcome(
                guid, project["path"], label, DECLINED,
                f"{target} already exists and was left alone", target)
    try:
        combined = compose(existing, managed)
    except InstructionsError as exc:
        return ProjectOutcome(guid, project["path"], label, FAILED,
                              str(exc), target)
    if existing is not None and combined == existing:
        return ProjectOutcome(guid, project["path"], label, UNCHANGED,
                              "already up to date", target)
    try:
        write_text_atomic(target, combined)
    except InstructionsError as exc:
        return ProjectOutcome(guid, project["path"], label, FAILED,
                              str(exc), target)
    state = MERGED if existing is not None else WRITTEN
    return ProjectOutcome(guid, project["path"], label, state, "", target)


def _values_for(guid: str, projects_root) -> dict:
    """The project's feature values, defaults when it has never chosen.

    An *unreadable* configuration raises rather than resolving to the
    defaults. Rendering a project's conventions from invented values would
    write a confident document about choices nobody managed to read, and then
    put it in the repository.
    """
    path = Path(projects_root) / guid / project_features.CONFIG_NAME
    try:
        document = project_features.read_config(path)
    except project_features.FeatureConfigError as exc:
        raise InstructionsError(str(exc)) from exc
    return project_features.resolved_values(document)


def retire(projects, *, source: str, source_origin: str, global_path,
           archive_dir, projects_root, home, version: str,
           decide=lambda project, existing: False,
           log=lambda message: None,
           allow_missing: bool = False) -> RetirementResult:
    """Give every project its ``AGENTS.md``, then retire the user-scope file.

    The order is the contract. Every repository is written first; the global
    file is archived only once nothing is blocking, and removed only once the
    archive has been read back and verified. Any interruption at any point
    leaves the conventions in two places, which costs a duplicate paragraph,
    rather than in none, which costs the machine its conventions.

    ``decide`` is consulted exactly once per repository that already has an
    ``AGENTS.md`` without a managed block in it, and never for anything else,
    so a non-interactive caller that answers "no" is refusing to write into
    other people's files rather than refusing the whole operation.
    """
    result = RetirementResult(source_origin=source_origin)
    result.user_agents = user_scope_agents_files(home)
    for project in projects:
        try:
            outcome = _place_one(project, source=source, version=version,
                                 projects_root=projects_root, decide=decide)
        except InstructionsError as exc:
            outcome = ProjectOutcome(project["guid"], project["path"],
                                     project.get("label") or project["path"],
                                     FAILED, str(exc))
        result.outcomes.append(outcome)
        log(f"  {outcome.label}: {outcome.state}"
            + (f" — {outcome.detail}" if outcome.detail else ""))

    blockers = [o for o in result.blockers
                if not (allow_missing and o.state == MISSING)]
    if blockers:
        result.problems.append(
            f"{len(blockers)} project(s) did not get an {AGENTS_NAME}, so "
            f"{global_path} was left in place.")
        return result
    if not result.placed:
        result.problems.append(
            f"No project received an {AGENTS_NAME}, so {global_path} was left "
            "in place. Removing it now would take the conventions off this "
            "machine entirely.")
        return result

    global_path = Path(global_path)
    if path_present(global_path) is False:
        result.removed = True          # already retired; nothing to preserve
        return result
    try:
        result.archived = preserve(global_path, archive_dir)
    except InstructionsError as exc:
        result.problems.append(f"{exc} {global_path} was left in place.")
        return result
    try:
        global_path.unlink()
    except OSError as exc:
        result.problems.append(
            f"Preserved a copy at {result.archived} but could not remove "
            f"{global_path}: {exc}")
        return result
    result.removed = True
    return result
