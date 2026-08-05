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

import hashlib
import json
import re
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
                     project_dir_path=f"/home/.copilot/projects/{guid}",
                     config_path=f"/home/.copilot/projects/{guid}/features.json",
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
