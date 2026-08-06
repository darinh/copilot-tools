"""Moving the conventions out of user scope and into each repository.

The module under test deletes a file the user may have spent a year editing,
so most of what is asserted here is about the *order* things happen in and
about what survives an interruption. Three properties carry the risk:

* every repository is written before anything is removed;
* nothing is removed without a preserved copy that was read back and verified;
* a repository's own ``AGENTS.md`` is never overwritten.

Each has a control that proves the guard fires -- a test that only ever
exercises the happy path would report this module clean at the moment it
started deleting files it had not replaced.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import install_manifest
import project_features
import project_instructions as pi
from project_instructions import (
    AGENTS_NAME,
    DECLINED,
    FAILED,
    MANAGED_BEGIN,
    MANAGED_END,
    MERGED,
    MISSING,
    UNCHANGED,
    WRITTEN,
    InstructionsError,
)

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "templates" / "copilot-instructions.md"


@pytest.fixture
def template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture
def defaults() -> dict:
    return project_features.resolved_values(None)


def _render(source: str, values: dict, *, guid: str = "GUID-1",
            path: str = "/repo/app", label: str = "app") -> str:
    return pi.render(source=source, values=values, guid=guid,
                     project_path=path, label=label,
                     project_dir_path=f"/home/.operator/projects/{guid}",
                     config_path=f"/home/.operator/projects/{guid}/features.json",
                     version="9.9.9")


# ---------------------------------------------------------------------------
# The gate marker
# ---------------------------------------------------------------------------

def test_the_gate_pattern_matches_the_spelling_the_template_uses():
    """A control for the one regex both the renderer and the template tests use.

    ``tests/test_project_features.py`` imports this pattern rather than
    carrying its own copy, because a second definition of "which feature turns
    this section on" is exactly the duplicated discovery rule this repository
    has already paid for once. That makes an independent check of the pattern
    itself necessary: if it silently matched nothing, the renderer would ship
    every section regardless of configuration *and* the conformance tests
    would report the template perfectly clean.
    """
    assert pi.GATE.search("*Enabled by feature flag: `spec-driven`*\n")
    assert pi.gate_slug("*Enabled by feature flag: `spec-driven`*\n") == "spec-driven"


@pytest.mark.parametrize("line", [
    "Enabled by feature flag: `spec-driven`",          # no emphasis
    "  *Enabled by feature flag: `spec-driven`*",      # indented, not a marker
    "*Enabled by feature flag: spec-driven*",          # no backticks
    "*enabled by feature flag: `spec-driven`*",        # wrong case
])
def test_the_gate_pattern_rejects_near_misses(line):
    """The negative control. A pattern that matched these would gate sections
    on prose that merely mentions a feature."""
    assert pi.gate_slug(line + "\n") is None


def test_an_ungated_section_has_no_slug():
    assert pi.gate_slug("Some prose about worktrees.\n") is None


# ---------------------------------------------------------------------------
# Splitting the document
# ---------------------------------------------------------------------------

def test_headings_inside_a_fence_do_not_start_a_section():
    """The reason the splitter tracks fences at all.

    The real template's handoff section contains a fenced example of a handoff
    file, and that example has ``## Status`` and ``## Next Steps`` in it at
    column zero. A splitter that matched ``^## `` on raw text would cut the
    section at its own example, and the tail would carry no gate marker -- so
    turning the feature off would drop the prose and keep the fragments.
    """
    text = (
        "# Title\n\nintro\n\n"
        "## Real Section\n\n"
        "*Enabled by feature flag: `session-handoff`*\n\n"
        "```markdown\n"
        "## Status\n"
        "## Next Steps\n"
        "```\n\n"
        "tail of the real section\n"
    )
    preamble, sections = pi.split_sections(text)
    assert "intro" in preamble
    assert [s.title for s in sections] == ["Real Section"]
    assert "tail of the real section" in sections[0].body
    assert pi.gate_slug(sections[0].body) == "session-handoff"


def test_a_tilde_fence_is_tracked_too():
    text = "## A\n\n~~~\n## Not A Heading\n~~~\n\n## B\n\nbody\n"
    _preamble, sections = pi.split_sections(text)
    assert [s.title for s in sections] == ["A", "B"]


def test_the_real_template_splits_into_sections_that_carry_every_gate(template):
    """The positive control against the document that actually ships.

    Every feature in the vocabulary must land on exactly one section here. If
    the splitter broke, sections would fragment and gates would go missing --
    and the visible symptom would be a project's ``AGENTS.md`` quietly
    containing conventions it had turned off.
    """
    _preamble, sections = pi.split_sections(template)
    gates = [pi.gate_slug(s.body) for s in sections]
    found = [g for g in gates if g is not None]
    assert sorted(found) == sorted(project_features.SLUGS)
    assert len(found) == len(set(found)), "a feature gates two sections"


def test_the_template_still_has_the_section_the_renderer_replaces(template):
    """Pins :data:`CONFIGURATION_SECTION` against the document.

    That section is the enrollment machinery -- catalog lookup, "would you
    like to set this up", the feature table -- and replacing it is the entire
    point of this module. A renamed heading would silently reinstate it, and
    every other test here would still pass, because nothing else looks at it.
    """
    _preamble, sections = pi.split_sections(template)
    assert pi.CONFIGURATION_SECTION in [s.title for s in sections]


def test_every_line_of_the_template_survives_the_split(template):
    """A splitter that dropped lines would be invisible in a 30 KB document."""
    preamble, sections = pi.split_sections(template)
    rebuilt = preamble + "".join(f"## {s.title}\n{s.body}" for s in sections)
    assert rebuilt == template


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_a_disabled_feature_drops_its_section(template, defaults):
    on = _render(template, defaults)
    off = _render(template, {**defaults, "session-handoff": "off"})
    assert "## Session Handoff Protocol" in on
    assert "## Session Handoff Protocol" not in off
    assert len(off) < len(on)


def test_a_backlog_backend_of_none_drops_the_backlog_section(template, defaults):
    """The feature that is a choice rather than a toggle.

    ``github-issues`` still counts as *having* a tracked backlog, so only
    ``none`` removes the section. A renderer that treated every non-default
    value as off would delete the section from projects that chose the other
    backend.
    """
    issues = _render(template, {**defaults, "tracked-backlog": "github-issues"})
    none = _render(template, {**defaults, "tracked-backlog": "none"})
    assert "## Tracked Backlog" in issues
    assert "## Tracked Backlog" not in none


def test_the_enrollment_instructions_are_replaced_not_copied(template, defaults):
    """The defect the whole item exists to fix.

    A user-scope file told every session -- in every directory on the machine
    -- to resolve a project root, read the catalog and offer to enroll the
    working directory. A project file knows which project it is, so those
    instructions become resolved facts.
    """
    rendered = _render(template, defaults, guid="GUID-1")
    assert f"## {pi.CONFIGURATION_SECTION}" in rendered
    assert "isn't in the catalog yet" not in rendered
    assert "Would you like to set it up" not in rendered
    assert "must not offer to enroll this directory" in rendered
    assert "GUID-1" in rendered


def test_the_enabled_features_line_is_the_one_the_vocabulary_produces(
        template, defaults):
    values = {**defaults, "spec-driven": "off"}
    rendered = _render(template, values)
    assert project_features.enabled_features_line(values) in rendered
    assert "spec-driven" not in rendered.split("\n\n")[2]


def test_rendering_is_deterministic(template, defaults):
    """No timestamp anywhere in the block.

    A block that recorded when it was written would show up as a diff in every
    repository every time anything regenerated it, and a diff that is always
    there is a diff nobody reads -- which is how a real change to the
    conventions would go unnoticed.
    """
    assert _render(template, defaults) == _render(template, defaults)


def test_a_section_gated_behind_an_unknown_slug_is_kept(defaults):
    """A downgrade must arrive as a duplicate, never as a deletion.

    An older build meeting a section gated on a feature it has no name for
    cannot know whether the project turned it on. Dropping it would delete
    conventions on the strength of not understanding them, which is the same
    collapse ``project_features.write_config`` refuses for stored settings.
    """
    source = ("# T\n\nintro\n\n"
              "## From The Future\n\n"
              "*Enabled by feature flag: `time-travel`*\n\nbody\n")
    assert "## From The Future" in _render(source, defaults)


def test_the_block_is_delimited_by_both_markers(template, defaults):
    rendered = _render(template, defaults)
    assert rendered.startswith(MANAGED_BEGIN)
    assert rendered.rstrip().endswith(MANAGED_END)


# ---------------------------------------------------------------------------
# Composing it into a repository's own file
# ---------------------------------------------------------------------------

def test_composing_into_nothing_is_just_the_block():
    assert pi.compose(None, "BLOCK\n") == "BLOCK\n"
    assert pi.compose("   \n\n", "BLOCK\n") == "BLOCK\n"


def test_an_existing_file_keeps_everything_it_had():
    existing = "# My project\n\nMy own notes.\n"
    out = pi.compose(existing, f"{MANAGED_BEGIN}\nx\n{MANAGED_END}\n")
    assert out.startswith(existing.rstrip("\n"))
    assert MANAGED_BEGIN in out


def test_a_second_pass_replaces_the_block_and_touches_nothing_else():
    """Idempotence, and the reason the markers exist.

    Everything above and below the block is compared byte for byte, because
    the failure that matters is not "the block is wrong" -- it is a
    regeneration that eats the paragraph its author wrote underneath.
    """
    first = pi.compose("# Mine\n\nabove\n",
                       f"{MANAGED_BEGIN}\nv1\n{MANAGED_END}\n")
    first += "\nbelow, written by hand\n"
    second = pi.compose(first, f"{MANAGED_BEGIN}\nv2\n{MANAGED_END}\n")
    assert "v1" not in second
    assert "v2" in second
    assert second.startswith("# Mine\n\nabove\n")
    assert second.endswith("\nbelow, written by hand\n")
    assert second.count(MANAGED_BEGIN) == 1


def test_recomposing_the_same_block_changes_nothing():
    block = f"{MANAGED_BEGIN}\nv1\n{MANAGED_END}\n"
    once = pi.compose("# Mine\n\nabove\n", block)
    assert pi.compose(once, block) == once


@pytest.mark.parametrize("broken", [
    f"{MANAGED_BEGIN}\na\n{MANAGED_END}\n{MANAGED_BEGIN}\nb\n{MANAGED_END}\n",
    f"{MANAGED_BEGIN}\nno end marker\n",
    f"{MANAGED_END}\nno begin marker\n",
    f"{MANAGED_END}\ninverted\n{MANAGED_BEGIN}\n",
])
def test_malformed_markers_raise_rather_than_being_repaired(broken):
    """Guessing which block is live is how a file ends up with two sets of
    conventions that disagree -- and the disagreement would then be invisible
    to the function meant to keep them in step."""
    with pytest.raises(InstructionsError):
        pi.compose(broken, f"{MANAGED_BEGIN}\nnew\n{MANAGED_END}\n")


# ---------------------------------------------------------------------------
# Writing and preserving
# ---------------------------------------------------------------------------

def test_atomic_write_leaves_no_temp_file_behind(tmp_path):
    target = tmp_path / "sub" / "AGENTS.md"
    pi.write_text_atomic(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"
    assert [p.name for p in target.parent.iterdir()] == ["AGENTS.md"]


def test_atomic_write_reports_a_failure_as_an_instructions_error(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(InstructionsError):
        pi.write_text_atomic(blocker / "sub" / "AGENTS.md", "x")


def test_preserving_copies_the_bytes_and_names_them_by_digest(tmp_path):
    source = tmp_path / "copilot-instructions.md"
    source.write_bytes(b"the user's edited conventions")
    archive = tmp_path / "retired"
    landed = pi.preserve(source, archive)
    assert landed.parent == archive
    assert landed.read_bytes() == b"the user's edited conventions"
    digest = hashlib.sha256(b"the user's edited conventions").hexdigest()
    assert digest[:12] in landed.name
    assert landed.name.endswith(".md")


def test_preserving_the_same_bytes_twice_reuses_the_archive(tmp_path):
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    first = pi.preserve(source, archive)
    second = pi.preserve(source, archive)
    assert first == second
    assert len(list(archive.iterdir())) == 1


def test_the_same_bytes_a_second_apart_are_still_one_archive(tmp_path):
    """The version of the test above that does not depend on the clock.

    The one above passes whenever both calls land in the same second, which is
    almost always, so it went green on seven CI legs and eight local runs and
    failed on the eighth leg with a same-content pair one second apart. Naming
    the two instants is the whole point: reuse must be keyed on the digest,
    and the timestamp in the name must not get a vote.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    first = pi.preserve(
        source, archive, when=datetime(2026, 8, 5, 9, 57, 51, tzinfo=timezone.utc))
    second = pi.preserve(
        source, archive, when=datetime(2026, 8, 5, 9, 57, 52, tzinfo=timezone.utc))
    assert first == second
    assert first.name == "f-20260805T095751Z-0967115f2813.md"
    assert len(list(archive.iterdir())) == 1


def test_different_bytes_a_second_apart_are_two_archives(tmp_path):
    """The other direction, which reuse-by-digest must not break.

    Without this, keying on the digest and keying on nothing at all are
    indistinguishable: both make the test above pass.
    """
    source = tmp_path / "f.md"
    archive = tmp_path / "retired"
    source.write_bytes(b"first")
    one = pi.preserve(
        source, archive, when=datetime(2026, 8, 5, 9, 57, 51, tzinfo=timezone.utc))
    source.write_bytes(b"second")
    two = pi.preserve(
        source, archive, when=datetime(2026, 8, 5, 9, 57, 52, tzinfo=timezone.utc))
    assert one != two
    assert one.read_bytes() == b"first"
    assert two.read_bytes() == b"second"
    assert len(list(archive.iterdir())) == 2


def test_two_files_with_identical_bytes_get_their_own_archives(tmp_path):
    """An archive is named after the file it came from, not just its bytes.

    Two different files can hold the same bytes, and reuse keyed on the digest
    alone would hand back the first one's archive as the second one's
    preserved copy. No bytes would be lost -- they are the same bytes -- but
    ``retired/`` would carry no record that the second file was ever retired,
    and its only caller unlinks the original next.

    The two stems are the same length deliberately. With stems of different
    lengths the stamp-width check rejects the mismatch on its own, so a
    version of this test using ``a.md`` and ``bbbb.md`` passes even when the
    stem is not compared at all.
    """
    archive = tmp_path / "retired"
    one = tmp_path / "aaa.md"
    one.write_bytes(b"identical")
    two = tmp_path / "bbb.md"
    two.write_bytes(b"identical")
    kept_one = pi.preserve(one, archive)
    kept_two = pi.preserve(two, archive)
    assert kept_one != kept_two
    assert kept_one.name.startswith("aaa-")
    assert kept_two.name.startswith("bbb-")
    assert len(list(archive.iterdir())) == 2


def test_two_files_with_identical_bytes_and_different_suffixes_do_too(tmp_path):
    """The same argument for the other end of the name.

    The two suffixes are the same length deliberately, for the reason the
    equal-length stems above are: with ``.md`` against ``.txt`` the stamp
    check rejects the mismatch by itself, and the test passes without the
    suffix ever being compared. Two reviewers found that independently.
    """
    archive = tmp_path / "retired"
    md = tmp_path / "AGENTS.md"
    md.write_bytes(b"identical")
    py = tmp_path / "AGENTS.py"
    py.write_bytes(b"identical")
    kept_md = pi.preserve(md, archive)
    kept_py = pi.preserve(py, archive)
    assert kept_md != kept_py
    assert kept_md.name.endswith(".md")
    assert kept_py.name.endswith(".py")
    assert len(list(archive.iterdir())) == 2


def test_an_archive_that_no_longer_holds_its_own_bytes_is_refused(tmp_path):
    """A corrupted archive must not be handed back as a preserved copy.

    ``preserve``'s only caller unlinks the original next, so returning a file
    that no longer matches its digest would delete the last good copy and
    report success.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    landed = pi.preserve(source, archive)
    landed.write_bytes(b"corrupted after the fact")
    with pytest.raises(InstructionsError):
        pi.preserve(source, archive)


def test_a_stem_with_glob_characters_matches_only_itself(tmp_path):
    """The scan is not a glob, and a caller-controlled stem is why.

    ``Path.glob`` would read ``a?c`` as "any single character in the middle"
    and hand back the archive of ``abc.md`` as the preserved copy of
    ``a?c.md`` -- different bytes, reported as kept.

    The metacharacter has to be ``?`` and the decoy stem has to be the same
    length. ``[ab]`` -- the obvious choice, and the one this test used first
    -- cannot show the difference at all: a character class matches exactly
    one character, so any name a glob wrongly matched would have a
    three-characters-shorter stem and the stamp check would reject it on its
    own. That version passed against a glob implementation, and two reviewers
    said so independently.

    ``a?c.md`` is not a legal filename on Windows, so ``existing_archive`` is
    called directly. Nothing here needs the source to exist on disk: only its
    stem and suffix are read.
    """
    archive = tmp_path / "retired"
    decoy = tmp_path / "abc.md"
    decoy.write_bytes(b"the decoy's bytes")
    kept_decoy = pi.preserve(decoy, archive)
    assert kept_decoy.name.startswith("abc-")
    digest = hashlib.sha256(b"the decoy's bytes").hexdigest()
    assert pi.existing_archive(archive, "a?c.md", digest) is None
    assert pi.existing_archive(archive, "abc.md", digest) == kept_decoy


def test_a_symlink_is_never_accepted_as_a_preserved_copy(tmp_path):
    """The reviewers' finding, and the worst shape in this function.

    ``file_digest`` follows links, so a link in the archive directory reads
    back as whatever it points at. A link pointing at the source being
    retired digests as a perfect match -- it *is* the source -- so it would be
    returned as the preserved copy, and the caller unlinking the original
    would turn it into a dangling link. The bytes would be gone and the run
    would report success.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"the only copy")
    archive = tmp_path / "retired"
    archive.mkdir()
    digest = hashlib.sha256(b"the only copy").hexdigest()
    link = archive / f"f-20260805T095751Z-{digest[:12]}.md"
    try:
        link.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")
    with pytest.raises(InstructionsError):
        pi.preserve(source, archive)
    assert source.read_bytes() == b"the only copy"


def test_a_directory_named_like_an_archive_is_refused(tmp_path):
    """The other non-regular candidate, and the one that needs no privileges.

    ``file_digest`` returns None for a directory, so this was already
    refused -- but by a check that cannot tell "not a file" from "wrong
    bytes", and the message said the bytes were wrong.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    archive.mkdir()
    digest = hashlib.sha256(b"same").hexdigest()
    (archive / f"f-20260805T095751Z-{digest[:12]}.md").mkdir()
    with pytest.raises(InstructionsError) as caught:
        pi.preserve(source, archive)
    assert "not a regular file" in str(caught.value)


def test_duplicates_already_on_disk_are_settled_on_deterministically(tmp_path):
    """Directories retired before the fix already hold the duplicate pairs.

    Reuse has to pick one, pick the same one every time, and not add a third.
    Sorting the names is what makes the choice stable, and because the stamp
    is fixed-width and zero-padded, sorted order is chronological: the
    earliest copy wins, on every platform.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    archive.mkdir()
    digest = hashlib.sha256(b"same").hexdigest()[:12]
    for stamp in ("20260805T095752Z", "20260805T095751Z", "20260805T095753Z"):
        (archive / f"f-{stamp}-{digest}.md").write_bytes(b"same")
    picked = {pi.preserve(source, archive).name for _ in range(3)}
    assert picked == {f"f-20260805T095751Z-{digest}.md"}
    assert len(list(archive.iterdir())) == 3


def test_the_stamp_pattern_matches_what_archive_name_writes():
    """The two halves of the naming scheme, pinned against each other.

    ``archive_name`` writes the stamp with ``strftime`` and
    ``existing_archive`` recognises it with a regex. Nothing else makes them
    agree, and if they stopped agreeing every archive would look unrecognised
    and reuse would silently stop working -- which is the bug this whole
    change is about, returned in a new spelling.
    """
    digest = "0" * 64
    built = pi.archive_name("f.md", digest, datetime(
        2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc))
    middle = built[len("f-"):-len(f"-{digest[:12]}.md")]
    assert pi._STAMP.fullmatch(middle)
    assert middle == "20261231T235959Z"


def test_sixteen_characters_of_anything_is_not_a_stamp(tmp_path):
    """Length alone is not the shape, and getting this wrong fails a preserve.

    A file whose middle is sixteen arbitrary characters would be taken for an
    archive, read back, and -- since it is not one -- refused. That refuses
    the whole preserve, so a stray file in the archive directory would stop
    the retire it was unrelated to. Sixteen was what the first version
    checked; a reviewer pointed out that it is not the same question.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    archive.mkdir()
    digest = hashlib.sha256(b"same").hexdigest()[:12]
    reference = pi.archive_name("f.md", "0" * 64, datetime(
        2026, 8, 5, 9, 57, 51, tzinfo=timezone.utc))
    stamp_length = len(reference) - len("f-") - len("-000000000000.md")
    middle = "notatimestamp!!!"
    assert len(middle) == stamp_length
    stray = archive / f"f-{middle}-{digest}.md"
    stray.write_bytes(b"nothing like the source")
    landed = pi.preserve(source, archive)
    assert landed != stray
    assert landed.read_bytes() == b"same"
    assert stray.read_bytes() == b"nothing like the source"


def test_a_name_with_no_room_for_a_stamp_is_not_an_archive(tmp_path):
    """Both ends can match at once when there is nothing between them.

    ``f-0967115f2813.md`` starts with the stem and ends with the digest and
    suffix, sharing characters between the two. There is no stamp in it, so
    it is not an archive -- and reading it back would refuse a preserve it
    has nothing to do with.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    archive.mkdir()
    digest = hashlib.sha256(b"same").hexdigest()[:12]
    stray = archive / f"f-{digest}.md"
    stray.write_bytes(b"nothing like the source")
    landed = pi.preserve(source, archive)
    assert landed != stray
    assert landed.read_bytes() == b"same"
    assert stray.read_bytes() == b"nothing like the source"


def test_an_unrelated_file_ending_the_same_way_is_not_an_archive(tmp_path):
    """The middle of the name has to be stamp-shaped to count.

    Matching on the two ends alone would accept any file that happens to
    start with the stem and end with the digest, and hand it back unread.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    archive.mkdir()
    digest = hashlib.sha256(b"same").hexdigest()[:12]
    impostor = archive / f"f-notatimestamp-{digest}.md"
    impostor.write_bytes(b"nothing like the source")
    landed = pi.preserve(source, archive)
    assert landed != impostor
    assert landed.read_bytes() == b"same"
    assert impostor.read_bytes() == b"nothing like the source"


def test_an_archive_directory_that_cannot_be_listed_is_an_error(tmp_path):
    """Not being able to look is not the same answer as nothing being there.

    Treating a failed listing as "no existing copy" would write a second
    archive of bytes already kept, and on a directory that stayed unreadable
    it would keep doing so on every run. The denial is injected rather than
    staged on disk because the errno a blocked listing produces differs by
    platform, and a test that passes through a different branch on Windows
    than on Linux is not evidence about either.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    archive.mkdir()

    def denied(self):
        raise PermissionError(13, "Permission denied")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "iterdir", denied)
        with pytest.raises(InstructionsError) as caught:
            pi.preserve(source, archive)
    assert "Nothing was removed" in str(caught.value)


def test_an_absent_archive_directory_is_not_an_error(tmp_path):
    """The control for the test above: absent really is 'nothing there'.

    Every first preserve hits this, so mapping FileNotFoundError onto the
    refusal above would break the ordinary path rather than a rare one.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "never-created-yet"
    assert not archive.exists()
    landed = pi.preserve(source, archive)
    assert landed.read_bytes() == b"same"


def test_preserving_an_unreadable_source_raises(tmp_path):
    with pytest.raises(InstructionsError):
        pi.preserve(tmp_path / "missing.md", tmp_path / "retired")


def test_preserving_verifies_the_copy_by_reading_it_back(tmp_path, monkeypatch):
    """The read-back is what makes the promise a promise.

    Its only caller unlinks the original next, so a copy that was written and
    never checked is the same as no copy at all -- it just reads better in a
    log. Simulated here by corrupting the file between the write and the
    check, which is what a short write or a lying filesystem looks like from
    the outside.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"important")
    archive = tmp_path / "retired"
    real_digest = install_manifest.file_digest

    def corrupt(path):
        Path(path).write_bytes(b"truncated")
        monkeypatch.setattr(install_manifest, "file_digest", real_digest)
        return real_digest(path)

    monkeypatch.setattr(pi, "file_digest", corrupt)
    with pytest.raises(InstructionsError):
        pi.preserve(source, archive)


def test_user_scope_agents_files_are_found_and_never_touched(tmp_path):
    (tmp_path / ".copilot").mkdir()
    globals_ = tmp_path / ".copilot" / AGENTS_NAME
    globals_.write_text("theirs", encoding="utf-8")
    found = pi.user_scope_agents_files(tmp_path)
    assert found == [globals_]
    assert globals_.read_text(encoding="utf-8") == "theirs"


def test_no_user_scope_agents_file_is_no_finding(tmp_path):
    assert pi.user_scope_agents_files(tmp_path) == []


# ---------------------------------------------------------------------------
# Choosing where the conventions come from
# ---------------------------------------------------------------------------

@pytest.fixture
def source_pair(tmp_path):
    template = tmp_path / "templates" / "copilot-instructions.md"
    template.parent.mkdir(parents=True)
    template.write_text("# repository template\n", encoding="utf-8")
    deployed = tmp_path / "copilot" / "copilot-instructions.md"
    deployed.parent.mkdir(parents=True)
    return template, deployed


def test_an_untouched_deployment_takes_the_repository_wording(source_pair):
    template, deployed = source_pair
    deployed.write_text("# repository template\n", encoding="utf-8")
    manifest = install_manifest.empty_manifest()
    install_manifest.record(manifest, pi.TEMPLATE_KEY, deployed, kind="template",
                            digest=install_manifest.file_digest(template))
    text, origin = pi.resolve_source(template, deployed, manifest)
    assert text == "# repository template\n"
    assert "repository template" in origin


def test_an_edited_deployment_wins(source_pair):
    """The user's edits are the conventions this machine actually follows.

    Generating every project's ``AGENTS.md`` from the pristine template would
    delete a year of edits from the machine in the same operation that deletes
    the file holding them. The predicate is the manifest's -- the same one
    ``install_templates`` uses to decide it may not overwrite -- because a file
    precious enough not to clobber is precious enough not to discard.
    """
    template, deployed = source_pair
    deployed.write_text("# MY EDITS\n", encoding="utf-8")
    manifest = install_manifest.empty_manifest()
    install_manifest.record(manifest, pi.TEMPLATE_KEY, deployed, kind="template",
                            digest="0" * 64)
    install_manifest.entry(manifest, pi.TEMPLATE_KEY)["sha256"] = "0" * 64
    text, origin = pi.resolve_source(template, deployed, manifest)
    assert text == "# MY EDITS\n"
    assert "local edits" in origin


def test_an_untracked_deployment_wins(source_pair):
    """A machine that predates the manifest. Its file is precious by default."""
    template, deployed = source_pair
    deployed.write_text("# from an older setup\n", encoding="utf-8")
    text, _origin = pi.resolve_source(template, deployed,
                                      install_manifest.empty_manifest())
    assert text == "# from an older setup\n"


def test_a_missing_template_falls_back_to_the_deployed_copy(source_pair):
    template, deployed = source_pair
    template.unlink()
    deployed.write_text("# deployed\n", encoding="utf-8")
    manifest = install_manifest.empty_manifest()
    install_manifest.record(manifest, pi.TEMPLATE_KEY, deployed, kind="template",
                            digest=install_manifest.file_digest(deployed))
    text, _origin = pi.resolve_source(template, deployed, manifest)
    assert text == "# deployed\n"


def test_no_source_at_all_raises(source_pair):
    template, deployed = source_pair
    template.unlink()
    with pytest.raises(InstructionsError):
        pi.resolve_source(template, deployed, install_manifest.empty_manifest())


def test_an_empty_source_is_not_a_source(source_pair):
    """An empty template would render an ``AGENTS.md`` with a header and no
    conventions in it, into every repository, and then delete the file that
    had them."""
    template, deployed = source_pair
    template.write_text("", encoding="utf-8")
    deployed.write_text("# deployed\n", encoding="utf-8")
    manifest = install_manifest.empty_manifest()
    install_manifest.record(manifest, pi.TEMPLATE_KEY, deployed, kind="template",
                            digest=install_manifest.file_digest(template))
    text, _origin = pi.resolve_source(template, deployed, manifest)
    assert text == "# deployed\n"


# ---------------------------------------------------------------------------
# The retirement
# ---------------------------------------------------------------------------

SOURCE = (
    "# Conventions\n\nintro\n\n"
    "## Always On\n\nprose\n\n"
    f"## {pi.CONFIGURATION_SECTION}\n\nenrollment prose\n\n"
    "## Session Handoff Protocol\n\n"
    "*Enabled by feature flag: `session-handoff`*\n\nhandoff prose\n"
)


@pytest.fixture
def machine(tmp_path):
    """A home with a global instructions file and two registered projects."""
    home = tmp_path / "home"
    copilot = home / ".copilot"
    projects_root = copilot / "projects"
    projects_root.mkdir(parents=True)
    global_path = copilot / "copilot-instructions.md"
    global_path.write_text("the global conventions\n", encoding="utf-8")
    projects = []
    for index, name in enumerate(("alpha", "beta"), 1):
        guid = f"GUID-{index}"
        root = tmp_path / "repos" / name
        root.mkdir(parents=True)
        (projects_root / guid).mkdir()
        projects.append({"guid": guid, "path": str(root), "label": name})
    return {
        "home": home,
        "global_path": global_path,
        "archive": copilot / pi.ARCHIVE_DIRNAME,
        "projects_root": projects_root,
        "projects": projects,
    }


def _retire(machine, **overrides):
    kwargs = dict(
        source=SOURCE,
        source_origin="a test",
        global_path=machine["global_path"],
        archive_dir=machine["archive"],
        projects_root=machine["projects_root"],
        home=machine["home"],
        version="9.9.9",
    )
    kwargs.update(overrides)
    return pi.retire(machine["projects"], **kwargs)


def test_every_project_gets_a_file_and_the_global_one_is_retired(machine):
    result = _retire(machine)
    assert [o.state for o in result.outcomes] == [WRITTEN, WRITTEN]
    for project in machine["projects"]:
        text = (Path(project["path"]) / AGENTS_NAME).read_text(encoding="utf-8")
        assert MANAGED_BEGIN in text and MANAGED_END in text
        assert "enrollment prose" not in text
    assert result.removed
    assert not machine["global_path"].exists()
    assert result.archived.read_text(encoding="utf-8") == "the global conventions\n"


def test_each_project_gets_only_the_sections_its_own_features_turned_on(machine):
    config = (machine["projects_root"] / "GUID-1"
              / project_features.CONFIG_NAME)
    config.write_text(json.dumps(
        {"version": 1, "features": {"session-handoff": "off"}}),
        encoding="utf-8")
    _retire(machine)
    alpha = (Path(machine["projects"][0]["path"]) / AGENTS_NAME).read_text(
        encoding="utf-8")
    beta = (Path(machine["projects"][1]["path"]) / AGENTS_NAME).read_text(
        encoding="utf-8")
    assert "handoff prose" not in alpha
    assert "handoff prose" in beta


def test_a_project_that_cannot_be_written_stops_the_removal(machine):
    """The contract this module exists for.

    A removal that went ahead anyway would leave the machine with the
    conventions in no place at all, which is the one failure the ordering is
    chosen to make impossible. The state it settles on instead is a duplicate.
    """
    blocked = Path(machine["projects"][1]["path"]) / AGENTS_NAME
    blocked.mkdir()                      # a directory where the file must go
    result = _retire(machine)
    assert result.blockers
    assert not result.removed
    assert machine["global_path"].exists()
    assert result.archived is None
    # ...and the project that *could* be written still was.
    assert (Path(machine["projects"][0]["path"]) / AGENTS_NAME).is_file()


def test_a_project_directory_that_is_not_here_stops_the_removal(machine):
    """An unplugged drive or an unsynced clone is a project that comes back.

    Removing the global file while it is away opens the gap on a delay.
    """
    machine["projects"].append({"guid": "GUID-3", "label": "gamma",
                                "path": str(machine["home"] / "not-here")})
    result = _retire(machine)
    assert [o.state for o in result.outcomes][-1] == MISSING
    assert not result.removed
    assert machine["global_path"].exists()


def test_allow_missing_is_the_deliberate_override(machine):
    machine["projects"].append({"guid": "GUID-3", "label": "gamma",
                                "path": str(machine["home"] / "not-here")})
    result = _retire(machine, allow_missing=True)
    assert result.removed
    assert not machine["global_path"].exists()


def test_an_existing_agents_file_is_not_touched_without_consent(machine):
    theirs = Path(machine["projects"][0]["path"]) / AGENTS_NAME
    theirs.write_text("# their own conventions\n", encoding="utf-8")
    result = _retire(machine)                       # decide defaults to "no"
    assert result.outcomes[0].state == DECLINED
    assert theirs.read_text(encoding="utf-8") == "# their own conventions\n"
    assert not result.removed
    assert machine["global_path"].exists()


def test_consenting_appends_below_what_was_already_there(machine):
    theirs = Path(machine["projects"][0]["path"]) / AGENTS_NAME
    theirs.write_text("# their own conventions\n", encoding="utf-8")
    result = _retire(machine, decide=lambda project, existing: True)
    assert result.outcomes[0].state == MERGED
    text = theirs.read_text(encoding="utf-8")
    assert text.startswith("# their own conventions\n")
    assert MANAGED_BEGIN in text
    assert result.removed


def test_the_consent_question_is_asked_only_for_files_we_did_not_write(machine):
    """A repository that already carries a managed block has already
    consented; asking again on every regeneration would train the answer."""
    asked = []
    _retire(machine, decide=lambda project, existing: asked.append(project) or True)
    assert asked == []
    second = _retire(machine, decide=lambda p, e: asked.append(p) or True)
    assert asked == []
    assert [o.state for o in second.outcomes] == [UNCHANGED, UNCHANGED]


def test_a_second_run_is_idempotent(machine):
    first = _retire(machine)
    assert first.removed
    before = {p["path"]: (Path(p["path"]) / AGENTS_NAME).read_text(
        encoding="utf-8") for p in machine["projects"]}
    second = _retire(machine)
    assert second.removed
    assert [o.state for o in second.outcomes] == [UNCHANGED, UNCHANGED]
    after = {p["path"]: (Path(p["path"]) / AGENTS_NAME).read_text(
        encoding="utf-8") for p in machine["projects"]}
    assert before == after
    assert len(list(machine["archive"].iterdir())) == 1


# ---------------------------------------------------------------------------
# FR-7 -- a project's own content survives regeneration, byte for byte
#
# `test_a_second_run_is_idempotent` above is a weaker claim than it looks:
# it regenerates the *same* block, so a `_place_one` that ignored the
# existing file entirely and wrote the block alone would still pass it on
# the second run. What FR-7 promises is that content survives a block that
# genuinely changed -- a new toolkit version, a feature turned on, a section
# rewritten -- because that is the only regeneration anyone will ever run.
# These tests drive the whole path (`render` -> `compose` -> the atomic
# write), not `compose` on its own, since every one of those steps is
# somewhere the surrounding bytes could be dropped.
# ---------------------------------------------------------------------------

APPENDED_ABOVE = (
    "# alpha\n"
    "\n"
    "Build: `make check`. Run one test with `pytest -k name`.\n"
    "\n"
)

APPENDED_BELOW = (
    "\n"
    "## Notes we wrote ourselves\n"
    "\n"
    "Trailing whitespace matters here:   \n"
    "So do blank lines.\n"
    "\n"
    "\n"
    "And a tab\tin the middle.\n"
)


def _regenerate_around_project_content(machine, **second_run):
    """Place, append the project's own prose above and below, regenerate.

    Returns the block-stripped remainder before and after, plus the two
    managed blocks, so a caller can assert both halves: what changed, and
    what did not.
    """
    _retire(machine)
    target = Path(machine["projects"][0]["path"]) / AGENTS_NAME
    written = target.read_text(encoding="utf-8")
    target.write_text(APPENDED_ABOVE + written.rstrip("\n") + "\n"
                      + APPENDED_BELOW, encoding="utf-8")
    before = target.read_text(encoding="utf-8")
    result = _retire(machine, **second_run)
    after = target.read_text(encoding="utf-8")
    return before, after, result


def _split_out_the_block(text: str) -> "tuple[str, str, str]":
    """`text` as (above, the managed block, below)."""
    assert text.count(MANAGED_BEGIN) == 1, text
    assert text.count(MANAGED_END) == 1, text
    head, rest = text.split(MANAGED_BEGIN, 1)
    block, tail = rest.split(MANAGED_END, 1)
    return head, block, tail


def test_project_content_survives_a_block_that_actually_changed(machine):
    """The regeneration FR-7 is about: the toolkit version moved on.

    Everything outside the markers is compared byte for byte -- including
    the trailing spaces, the doubled blank line and the tab, because a
    "preserving" implementation that round-trips through a line list and
    rejoins with `\\n` loses exactly those and nothing else, and no test
    written with tidy fixture prose would see it.
    """
    before, after, result = _regenerate_around_project_content(
        machine, version="10.0.0")
    assert [o.state for o in result.outcomes] == [MERGED, MERGED]

    old_head, old_block, old_tail = _split_out_the_block(before)
    new_head, new_block, new_tail = _split_out_the_block(after)

    assert old_block != new_block, (
        "the block did not change, so this test proved only what the "
        "idempotence test already proves")
    assert "9.9.9" in old_block and "10.0.0" in new_block
    assert (new_head, new_tail) == (old_head, old_tail)
    assert new_head == APPENDED_ABOVE
    assert new_tail.endswith(APPENDED_BELOW)


def test_project_content_survives_a_feature_being_turned_off(machine):
    """The other axis: the block shrinks. Content below it must not be
    dragged up with the removed section."""
    config = (machine["projects_root"] / "GUID-1"
              / project_features.CONFIG_NAME)
    _retire(machine)
    target = Path(machine["projects"][0]["path"]) / AGENTS_NAME
    target.write_text(
        APPENDED_ABOVE + target.read_text(encoding="utf-8").rstrip("\n")
        + "\n" + APPENDED_BELOW, encoding="utf-8")
    before = target.read_text(encoding="utf-8")
    config.write_text(json.dumps(
        {"version": 1, "features": {"session-handoff": "off"}}),
        encoding="utf-8")
    _retire(machine)
    after = target.read_text(encoding="utf-8")

    old_head, old_block, old_tail = _split_out_the_block(before)
    new_head, new_block, new_tail = _split_out_the_block(after)
    assert "handoff prose" in old_block
    assert "handoff prose" not in new_block
    assert (new_head, new_tail) == (old_head, old_tail)


def test_appended_content_that_quotes_the_markers_is_not_eaten(machine):
    """A project documenting the managed block writes these lines into a
    fenced sample. Read as live markers, the sample and everything between
    it and the real block is what `compose` replaces -- so the file loses
    the paragraph explaining the very thing that ate it."""
    _retire(machine)
    target = Path(machine["projects"][0]["path"]) / AGENTS_NAME
    documented = (
        target.read_text(encoding="utf-8").rstrip("\n") + "\n"
        + "\n## How the managed block is delimited\n\n"
        + "```markdown\n" + MANAGED_BEGIN + "\n...\n" + MANAGED_END
        + "\n```\n\nKeep your own notes outside it.\n")
    target.write_text(documented, encoding="utf-8")
    _retire(machine, version="10.0.0")
    after = target.read_text(encoding="utf-8")
    assert "Keep your own notes outside it." in after
    assert "## How the managed block is delimited" in after
    assert after.count(MANAGED_BEGIN) == 2
    assert "10.0.0" in after


def test_regeneration_reports_merged_not_written(machine):
    """The state is what a caller logs, and `written` over a file that
    already held someone's prose reads as "we made this file" -- which is
    the claim the whole preservation contract denies."""
    _, _, result = _regenerate_around_project_content(machine,
                                                      version="10.0.0")
    assert result.outcomes[0].state == MERGED
    assert result.outcomes[0].state != WRITTEN


def test_no_projects_at_all_refuses_to_remove_anything(machine):
    result = pi.retire(
        [], source=SOURCE, source_origin="a test",
        global_path=machine["global_path"], archive_dir=machine["archive"],
        projects_root=machine["projects_root"], home=machine["home"],
        version="9.9.9")
    assert not result.removed
    assert machine["global_path"].exists()
    assert result.problems


def test_a_failed_archive_leaves_the_original_alone(machine, monkeypatch):
    """Preservation is not best-effort. If the copy cannot be proven to exist,
    the original is what is left holding the conventions."""
    def refuse(*_a, **_k):
        raise InstructionsError("disk full")

    monkeypatch.setattr(pi, "preserve", refuse)
    result = _retire(machine)
    assert not result.removed
    assert machine["global_path"].exists()
    assert any("disk full" in p for p in result.problems)
    # Every project still got its file: the replacement happens first.
    for project in machine["projects"]:
        assert (Path(project["path"]) / AGENTS_NAME).is_file()


def test_an_unreadable_feature_configuration_fails_that_project(machine):
    """Rendering from invented defaults would write a confident document about
    choices nobody managed to read, and put it in the repository."""
    config = (machine["projects_root"] / "GUID-1"
              / project_features.CONFIG_NAME)
    config.write_text("{ not json", encoding="utf-8")
    result = _retire(machine)
    assert result.outcomes[0].state == FAILED
    assert not (Path(machine["projects"][0]["path"]) / AGENTS_NAME).exists()
    assert not result.removed
    assert machine["global_path"].exists()


def test_a_user_scope_agents_file_is_reported_and_left_alone(machine):
    theirs = machine["home"] / ".copilot" / AGENTS_NAME
    theirs.write_text("theirs\n", encoding="utf-8")
    result = _retire(machine)
    assert result.user_agents == [theirs]
    assert theirs.read_text(encoding="utf-8") == "theirs\n"
    assert result.removed


def test_an_already_absent_global_file_is_not_an_error(machine):
    machine["global_path"].unlink()
    result = _retire(machine)
    assert result.removed
    assert result.archived is None
    assert not result.problems


def test_the_archive_directory_is_never_pruned_by_this_module():
    """A promise, asserted against the source rather than by behaviour.

    ``superseded/`` grows only when a handoff went unread, and this directory
    grows only when a machine's conventions were replaced. Both are symptoms
    to read, not messes to clear -- and a reaper hidden inside the fix for an
    unwanted delete is the same bug wearing the fix's clothes.
    """
    source = (REPO / "project_instructions.py").read_text(encoding="utf-8")
    for destructive in ("rmtree", "shutil.rmtree", "os.removedirs"):
        assert destructive not in source
    unlinks = re.findall(r"\.unlink\(|os\.unlink\(|os\.remove\(", source)
    # One for the interrupted-temp-file cleanup in `write_text_atomic`, one in
    # `preserve`, and one for the global file itself. Anything more is a
    # deletion nobody argued for.
    assert len(unlinks) == 3, f"unexpected deletions: {unlinks}"


# ---------------------------------------------------------------------------
# Fences, markers and labels: the review round
#
# Every test below is a defect three adversarial reviewers found in code that
# already had 55 passing tests over it. They are kept apart from the sections
# above so the next reader can see what the first pass did not think of.
# ---------------------------------------------------------------------------

def test_a_four_tick_fence_is_not_closed_by_an_inner_three_tick_line():
    """CommonMark closes a fence on a run at least as long, not any run.

    A document about writing Markdown -- which this template is -- wraps
    triple-tick examples in a four-tick fence. Reading the inner ``` as the
    close puts the rest of the example back into the document as prose, and
    the examples here carry `## ` headings at column zero, so those fragments
    become sections with no gate marker on them. Content for a feature that
    is *off* is then emitted.
    """
    text = (
        "## Kept\n"
        "````markdown\n"
        "```python\n"
        "x = 1\n"
        "```\n"
        "## Not a heading\n"
        "````\n"
        "tail\n"
    )
    _preamble, sections = pi.split_sections(text)
    assert [s.title for s in sections] == ["Kept"]
    assert "## Not a heading" in sections[0].body


def test_the_four_tick_case_would_fail_a_three_character_fence_tracker():
    """The control for the test above.

    A tracker that stored only ``stripped[:3]`` closes on the inner ``` and
    produces a second section. Asserting that here means the test above is
    pinned to the fix rather than to an implementation that never had the bug.
    """
    text = (
        "## Kept\n````\n```\n## Not a heading\n```\n````\n"
    )
    naive = [line for line in text.splitlines() if line.startswith("## ")]
    assert len(naive) == 2, "the input must contain the trap being tested"


def test_a_tilde_fence_is_not_closed_by_backticks():
    text = "## One\n~~~\n```\n## Inside\n```\n~~~\n"
    _preamble, sections = pi.split_sections(text)
    assert [s.title for s in sections] == ["One"]


def test_a_closing_run_may_be_longer_than_the_opening_one():
    text = "## One\n```\nbody\n`````\n## Two\n"
    _preamble, sections = pi.split_sections(text)
    assert [s.title for s in sections] == ["One", "Two"]


def test_a_fence_line_with_an_info_string_does_not_close_a_fence():
    """```` ```python ```` opens; it never closes."""
    text = "## One\n```\n```python\n## Inside\n```\n## Two\n"
    _preamble, sections = pi.split_sections(text)
    assert [s.title for s in sections] == ["One", "Two"]


def test_the_real_template_still_splits_into_its_gated_sections(template):
    """The fence rewrite must not have changed the document it exists for."""
    _preamble, sections = pi.split_sections(template)
    slugs = [pi.gate_slug(s.body) for s in sections]
    assert set(s for s in slugs if s) == set(project_features.SLUGS)


def test_markers_quoted_in_a_code_sample_are_not_a_managed_block():
    """An AGENTS.md that documents this toolkit is not one it wrote.

    Treating the sample as a live block is not cosmetic: it makes
    ``managed_block_present`` true, which skips the consent prompt, and then
    ``compose`` replaces everything between the two sampled lines. That is
    the user's own file destroyed without being asked.
    """
    existing = (
        "# My conventions\n\n"
        "copilot-tools writes a block delimited like this:\n\n"
        "```\n"
        f"{pi.MANAGED_BEGIN}\n"
        "...generated conventions...\n"
        f"{pi.MANAGED_END}\n"
        "```\n\n"
        "Keep the rest.\n"
    )
    assert pi.managed_block_present(existing) is False
    out = pi.compose(existing, "MANAGED\n")
    assert "...generated conventions..." in out, "the sample was clobbered"
    assert out.startswith(existing.rstrip("\n"))
    assert out.rstrip("\n").endswith("MANAGED")


def test_a_marker_merely_mentioned_in_prose_is_not_a_delimiter():
    existing = (
        f"We do not use the {pi.MANAGED_BEGIN} marker here.\n"
    )
    assert pi.managed_block_present(existing) is False
    out = pi.compose(existing, "MANAGED\n")
    assert existing.rstrip("\n") in out


def test_a_real_marker_pair_is_still_found_and_replaced():
    """The control: the narrowing above must not have switched detection off."""
    existing = (
        "# Mine\n\n"
        f"{pi.MANAGED_BEGIN}\n"
        "OLD\n"
        f"{pi.MANAGED_END}\n\n"
        "# Also mine\n"
    )
    assert pi.managed_block_present(existing) is True
    out = pi.compose(existing, "NEW\n")
    assert "OLD" not in out
    assert "NEW" in out
    assert out.startswith("# Mine\n")
    assert out.endswith("# Also mine\n")


def test_a_marker_line_with_trailing_whitespace_still_counts():
    existing = (
        f"{pi.MANAGED_BEGIN}  \nOLD\n"
        f"  {pi.MANAGED_END}\t\n"
    )
    out = pi.compose(existing, "NEW\n")
    assert "OLD" not in out and "NEW" in out


def test_a_lone_begin_marker_is_refused_rather_than_appended_to():
    existing = f"# Mine\n\n{pi.MANAGED_BEGIN}\nOLD\n"
    with pytest.raises(pi.InstructionsError):
        pi.compose(existing, "NEW\n")


def test_a_windows_path_label_survives_a_posix_basename(tmp_path):
    """``Path("C:\\a\\b").name`` is the whole string on Linux.

    Catalog rows are written in the native form of the machine that made
    them, so a Windows row read on Linux must still label as ``b``. These
    assertions can only *fail* on a POSIX leg — on Windows ``pathlib`` agrees
    with ``ntpath`` for these inputs — which is why the source scan below
    exists as well.
    """
    assert pi._basename(r"C:\repos\my-app") == "my-app"
    assert pi._basename("/home/dev/my-app") == "my-app"
    assert pi._basename("/home/dev/my-app/") == "my-app"
    assert pi._basename(r"C:\repos\my-app\\") == "my-app"


def test_the_label_cases_would_catch_an_os_path_basename():
    """The control for the test above, on this platform's os.path."""
    import posixpath
    assert posixpath.basename(r"C:\repos\my-app") == r"C:\repos\my-app"


def test_no_path_string_is_split_with_the_running_platforms_syntax():
    """A scan, because the behavioural test above is blind on Windows.

    ``os.path`` is an alias for whichever of ``posixpath``/``ntpath`` is
    running, so it is the wrong tool for a string that may name the *other*
    platform's syntax — and every catalog row here is such a string. On
    Windows ``pathlib`` and ``ntpath`` agree, so a green local suite says
    nothing; this fires on every leg.

    It reads the parsed tree rather than the text, because the text also
    contains the *explanation* of why not to do this — a substring scan
    would trip on the docstring that documents the rule.
    """
    source = (Path(__file__).resolve().parent.parent
              / "project_instructions.py").read_text(encoding="utf-8")
    banned = {("os", "path", "basename"), ("os", "path", "dirname"),
              ("os", "path", "split"), ("posixpath", "basename")}
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            parts = _dotted(node)
            if parts in banned:
                found.add(".".join(parts))
    assert not found, (
        f"{sorted(found)} read the running platform's syntax; catalog paths "
        "may be written in the other one. Use ntpath.")
    assert "ntpath" in {alias.name for node in ast.walk(ast.parse(source))
                        if isinstance(node, ast.Import)
                        for alias in node.names}


def _dotted(node) -> tuple:
    """``os.path.basename`` -> ``("os", "path", "basename")``; else ``()``."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ()
    parts.append(node.id)
    return tuple(reversed(parts))


def test_the_scan_above_would_notice_the_wrong_spelling():
    """Positive control: a detector that matches nothing reports clean.

    The banned call is fed through the same tree walk, so this fails if the
    walk stops finding anything — which is the way that scan dies silently.
    """
    tree = ast.parse("import os\nname = os.path.basename(p)\n")
    hits = {".".join(_dotted(n)) for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)}
    assert "os.path.basename" in hits


def test_the_scan_above_does_not_fire_on_the_portable_spelling():
    """Negative control: ``ntpath.basename`` must pass the same walk."""
    tree = ast.parse("import ntpath\nname = ntpath.basename(p)\n")
    hits = {".".join(_dotted(n)) for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)}
    assert hits == {"ntpath.basename"}
