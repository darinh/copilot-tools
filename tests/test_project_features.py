"""The feature vocabulary, its storage, and its tie to the deployed template.

The module under test exists to be the *single owner* of what features a
project can be configured with. That claim is only worth something if
something checks it, so the first half of this file pins
``templates/copilot-instructions.md`` against :data:`project_features.FEATURES`
in both directions -- a feature the template does not offer, and a section the
template gates behind a slug the vocabulary has never heard of, are both
failures here.

Every guard has a control that proves it fires. A conformance check that
matches nothing reports the whole tree clean, and clean reads exactly like
correct; that is the shape this repository treats as its worst, and the reason
``test_workflow_discovery_conformance.py`` exists at all.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest import mock

import pytest

import project_features
import project_instructions
from project_features import (
    BACKLOG_FOLDER,
    BACKLOG_GITHUB_ISSUES,
    BACKLOG_NONE,
    FEATURES,
    FEATURES_BY_SLUG,
    OFF,
    ON,
    SLUGS,
    TRACKED_BACKLOG,
    Feature,
    FeatureConfigError,
    Option,
)

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "templates" / "copilot-instructions.md"

#: Imported rather than re-spelled. A second definition of "which feature
#: turns this section on" is the duplicated discovery rule this repository has
#: already paid for once -- and here it would be worse than a duplicate, since
#: the renderer in ``project_instructions`` decides what actually ships from
#: this pattern. ``tests/test_project_instructions.py`` carries the positive
#: and negative controls for the pattern itself, so importing it does not cost
#: an independent check of whether it matches anything.
_GATE = project_instructions.GATE
_TABLE_SEPARATOR = re.compile(r"^\|[\s:|-]+\|$")
_EMPHASIS = re.compile(r"[*`]")


@pytest.fixture
def template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _table_names(text: str) -> list[str]:
    """First-column names of the one table under ``### Feature Selection``.

    A private copy of the table reader in ``test_instructions_template.py``
    would be a second definition of "what the table says", which is the exact
    duplication this module exists to forbid -- so this reads the table
    structurally and asserts there is only one, and the *other* module keeps
    checking the template's internal consistency. The two ask different
    questions of the same bytes: that one asks whether the document agrees
    with itself, this one asks whether it agrees with the code.
    """
    start = text.index("### Feature Selection")
    end = text.index("### What to write in a per-project file", start)
    lines = [line.strip() for line in text[start:end].splitlines()]
    separators = [i for i, line in enumerate(lines)
                  if _TABLE_SEPARATOR.match(line)]
    assert len(separators) == 1, (
        f"expected exactly one table under '### Feature Selection', "
        f"found {len(separators)}")
    names = []
    for line in lines[separators[0] + 1:]:
        if not line.startswith("|"):
            break
        name = _EMPHASIS.sub("", line.strip("|").split("|")[0]).strip()
        if name:
            names.append(name)
    return names


# ---------------------------------------------------------------------------
# The vocabulary is the single owner
# ---------------------------------------------------------------------------

def test_the_template_offers_exactly_the_features_the_code_declares(template):
    """The assertion this module exists for.

    If the menu enumerates features and the template enumerates features, the
    two lists will disagree, and the disagreement surfaces as a menu option
    that silently toggles nothing an agent ever reads.
    """
    assert _table_names(template) == [f.name for f in FEATURES]


def test_every_feature_gates_a_section_of_the_template(template):
    """An option that turns on nothing is an option that lies."""
    gated = set(_GATE.findall(template))
    assert set(SLUGS) - gated == set(), (
        "these features are offered by the menu but gate no section of the "
        f"template: {sorted(set(SLUGS) - gated)}")


def test_every_gated_section_names_a_feature_that_exists(template):
    """A section nobody can turn on."""
    gated = set(_GATE.findall(template))
    assert gated - set(SLUGS) == set(), (
        "these template sections are gated behind slugs the feature "
        f"vocabulary does not declare: {sorted(gated - set(SLUGS))}")


def test_the_example_enabled_features_line_matches_the_defaults(template):
    """The line a new project's instructions file is copied from.

    It is the one place the slugs appear as a list, so it is the one place a
    project's own file inherits verbatim -- and it must be a list this module
    would actually produce.
    """
    section = template[template.index("### What to write in a per-project file"):]
    match = re.search(r"Enabled features: ([^.]+)\.", section, re.DOTALL)
    assert match, "no 'Enabled features:' line in the example project file"
    listed = [s.strip() for s in re.split(r",\s*", match.group(1)) if s.strip()]
    defaults = project_features.resolved_values(None)
    assert listed == list(project_features.enabled_slugs(defaults))


def test_a_feature_the_template_does_not_offer_is_reported(template):
    """Positive control for the table check."""
    mutated = template.replace(f"| **{FEATURES[0].name}** |", "| **Telepathy** |", 1)
    assert mutated != template, "the substitution found nothing to replace"
    with pytest.raises(AssertionError):
        test_the_template_offers_exactly_the_features_the_code_declares(mutated)


def test_a_feature_gating_no_section_is_reported(template):
    """Positive control for the gate check."""
    mutated = template.replace(
        f"*Enabled by feature flag: `{FEATURES[0].slug}`*",
        "*Enabled by feature flag: `telepathy`*", 1)
    assert mutated != template, "the substitution found nothing to replace"
    with pytest.raises(AssertionError, match=FEATURES[0].slug):
        test_every_feature_gates_a_section_of_the_template(mutated)


def test_a_gate_naming_no_feature_is_reported(template):
    """Positive control for the other direction."""
    mutated = template.replace(
        f"*Enabled by feature flag: `{FEATURES[0].slug}`*",
        f"*Enabled by feature flag: `{FEATURES[0].slug}`*\n\n"
        "*Enabled by feature flag: `telepathy`*", 1)
    assert mutated != template, "the substitution found nothing to replace"
    with pytest.raises(AssertionError, match="telepathy"):
        test_every_gated_section_names_a_feature_that_exists(mutated)


def test_an_enabled_features_line_that_drifts_is_reported(template):
    mutated = template.replace("Enabled features: session-handoff",
                               "Enabled features: telepathy, session-handoff", 1)
    assert mutated != template, "the substitution found nothing to replace"
    with pytest.raises(AssertionError):
        test_the_example_enabled_features_line_matches_the_defaults(mutated)


# ---------------------------------------------------------------------------
# The vocabulary itself
# ---------------------------------------------------------------------------

def test_slugs_are_unique():
    assert len(set(SLUGS)) == len(SLUGS)
    assert set(FEATURES_BY_SLUG) == set(SLUGS)


def test_the_backlog_is_a_choice_and_everything_else_is_a_flag():
    """The requirement a toggle list could not express.

    Modelling ``tracked-backlog`` as a boolean is what forced this whole
    module to support more than on/off, so asserting it is a choice is
    asserting the reason the design has the shape it does.
    """
    backlog = FEATURES_BY_SLUG[TRACKED_BACKLOG]
    assert not backlog.is_flag
    assert backlog.values == (BACKLOG_FOLDER, BACKLOG_GITHUB_ISSUES, BACKLOG_NONE)
    assert [f.slug for f in FEATURES if not f.is_flag] == [TRACKED_BACKLOG]


def test_every_flag_takes_on_and_off():
    for feature in FEATURES:
        if feature.is_flag:
            assert feature.values == (ON, OFF)
            assert feature.off_value == OFF


def test_a_default_outside_the_options_is_refused():
    with pytest.raises(ValueError, match="default"):
        Feature(slug="x", name="X", description="", options=(Option(ON, "on"),),
                default="nope", off_value=ON)


def test_an_off_value_outside_the_options_is_refused():
    with pytest.raises(ValueError, match="off_value"):
        Feature(slug="x", name="X", description="", options=(Option(ON, "on"),),
                default=ON, off_value="nope")


def test_duplicate_option_values_are_refused():
    with pytest.raises(ValueError, match="duplicate"):
        Feature(slug="x", name="X", description="",
                options=(Option(ON, "on"), Option(ON, "again")),
                default=ON, off_value=ON)


def test_a_well_formed_feature_is_accepted():
    """Negative control: the checks above must not reject everything."""
    feature = Feature(slug="x", name="X", description="",
                      options=(Option(ON, "on"), Option(OFF, "off")),
                      default=ON, off_value=OFF)
    assert feature.is_flag
    assert feature.accepts(ON) and not feature.accepts("maybe")


# ---------------------------------------------------------------------------
# Resolution and the boolean reading of a choice
# ---------------------------------------------------------------------------

def test_an_absent_configuration_resolves_to_the_defaults():
    resolved = project_features.resolved_values(None)
    assert set(resolved) == set(SLUGS)
    assert resolved == {f.slug: f.default for f in FEATURES}


def test_a_partial_configuration_keeps_the_defaults_for_the_rest():
    resolved = project_features.resolved_values({"features": {"spec-driven": OFF}})
    assert resolved["spec-driven"] == OFF
    assert resolved["session-handoff"] == ON


def test_a_backlog_on_github_issues_still_counts_as_enabled():
    """The reason ``off_value`` is declared rather than inferred."""
    values = project_features.resolved_values(
        {"features": {TRACKED_BACKLOG: BACKLOG_GITHUB_ISSUES}})
    assert project_features.is_enabled(values, TRACKED_BACKLOG)

    off = project_features.resolved_values({"features": {TRACKED_BACKLOG: BACKLOG_NONE}})
    assert not project_features.is_enabled(off, TRACKED_BACKLOG)


def test_enabled_features_line_lists_only_what_is_on():
    values = project_features.resolved_values(
        {"features": {"spec-driven": OFF, TRACKED_BACKLOG: BACKLOG_NONE}})
    line = project_features.enabled_features_line(values)
    assert "spec-driven" not in line
    assert TRACKED_BACKLOG not in line
    assert "session-handoff" in line
    assert line.endswith(".")


def test_enabled_features_line_says_none_rather_than_trailing_nothing():
    """An empty list must not render as ``Enabled features: .``"""
    values = {f.slug: f.off_value for f in FEATURES}
    assert project_features.enabled_features_line(values) == (
        "Enabled features: none.")


def test_is_enabled_refuses_a_slug_it_does_not_know():
    with pytest.raises(KeyError):
        project_features.is_enabled(project_features.resolved_values(None), "telepathy")


def test_describe_value_labels_a_choice():
    assert "backlog/" in project_features.describe_value(
        TRACKED_BACKLOG, BACKLOG_FOLDER)
    assert project_features.describe_value("spec-driven", ON) == "on"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_a_configuration_that_was_never_written_reads_as_none(tmp_path):
    assert project_features.read_config(tmp_path / "features.json") is None


def test_a_configuration_that_cannot_be_read_raises(tmp_path, monkeypatch):
    """The distinction the whole module turns on.

    An unreadable file must not resolve to the defaults. It would render a
    complete, confident answer about a project whose real choices nobody
    managed to look at -- and the menu would then write those invented
    defaults back over the file.
    """
    path = tmp_path / "features.json"
    path.write_text("{}", encoding="utf-8")
    real = Path.read_text

    def refuse(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refuse)
    with pytest.raises(FeatureConfigError, match="Cannot read"):
        project_features.read_config(path)


def test_a_readable_configuration_is_not_refused(tmp_path):
    """Negative control for the guard above."""
    path = tmp_path / "features.json"
    path.write_text(json.dumps({"version": 1, "features": {}}), encoding="utf-8")
    assert project_features.read_config(path) == {"version": 1, "features": {}}


def test_a_configuration_that_is_not_json_raises(tmp_path):
    path = tmp_path / "features.json"
    path.write_text("not json at all", encoding="utf-8")
    with pytest.raises(FeatureConfigError, match="not valid JSON"):
        project_features.read_config(path)


def test_a_configuration_that_is_not_an_object_raises(tmp_path):
    path = tmp_path / "features.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(FeatureConfigError, match="not an object"):
        project_features.read_config(path)


def test_a_features_key_that_is_not_an_object_raises(tmp_path):
    path = tmp_path / "features.json"
    path.write_text(json.dumps({"features": ["spec-driven"]}), encoding="utf-8")
    with pytest.raises(FeatureConfigError, match="not an object"):
        project_features.read_config(path)


@pytest.mark.parametrize("value", [True, 1, None, "maybe", ["on"]])
def test_a_value_outside_a_features_vocabulary_raises(tmp_path, value):
    """``{"spec-driven": true}`` is good JSON and a bad configuration.

    A caller comparing it against ``"on"`` would read it as off, which is a
    complete answer derived from a value nobody ever offered.
    """
    path = tmp_path / "features.json"
    path.write_text(json.dumps({"features": {"spec-driven": value}}),
                    encoding="utf-8")
    with pytest.raises(FeatureConfigError, match="spec-driven"):
        project_features.read_config(path)


def test_a_slug_from_a_newer_toolkit_is_kept_rather_than_refused(tmp_path):
    """An unknown slug is what a newer build's configuration looks like."""
    path = tmp_path / "features.json"
    path.write_text(json.dumps({"features": {"telepathy": "on"}}),
                    encoding="utf-8")
    document = project_features.read_config(path)
    assert project_features.unknown_entries(document) == ("telepathy",)
    assert project_features.resolved_values(document) == {f.slug: f.default
                                                  for f in FEATURES}


def test_writing_preserves_settings_this_build_does_not_understand(tmp_path):
    """A downgrade must not silently delete a newer build's choices."""
    path = tmp_path / "features.json"
    path.write_text(json.dumps({"features": {"telepathy": "on"}}),
                    encoding="utf-8")
    document = project_features.read_config(path)
    project_features.write_config(path, {"spec-driven": OFF}, document=document)

    reread = project_features.read_config(path)
    assert reread["features"]["telepathy"] == "on"
    assert reread["features"]["spec-driven"] == OFF


def test_a_written_configuration_reads_back(tmp_path):
    path = tmp_path / "features.json"
    project_features.write_config(path, {TRACKED_BACKLOG: BACKLOG_GITHUB_ISSUES})
    values = project_features.resolved_values(project_features.read_config(path))
    assert values[TRACKED_BACKLOG] == BACKLOG_GITHUB_ISSUES
    assert values["session-handoff"] == ON


def test_writing_one_feature_leaves_the_others_where_they_were(tmp_path):
    path = tmp_path / "features.json"
    project_features.write_config(path, {"spec-driven": OFF})
    document = project_features.read_config(path)
    project_features.write_config(path, {"session-history": OFF},
                                  document=document)
    values = project_features.resolved_values(project_features.read_config(path))
    assert values["spec-driven"] == OFF
    assert values["session-history"] == OFF


def test_a_document_with_no_features_object_is_refused(tmp_path):
    """``{}`` is not "our file with nothing set in it".

    ``write_config`` always emits the key, so a document without it was not
    written by this tool. Resolving it to the defaults would report every
    feature confidently on the strength of a file nobody could interpret, and
    then offer to write those invented values over it. The sibling shape
    ``"features": []`` is refused loudly; these are the same malformation and
    ``.get(key, {})`` is how one of them quietly stopped being.
    """
    path = tmp_path / "features.json"
    path.write_text(json.dumps({"version": 1}), encoding="utf-8")
    with pytest.raises(FeatureConfigError, match="no 'features' object"):
        project_features.read_config(path)


def test_an_empty_object_is_refused(tmp_path):
    """The narrowest case of the above, and the one a truncation looks like."""
    path = tmp_path / "features.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(FeatureConfigError, match="no 'features' object"):
        project_features.read_config(path)


def test_an_empty_features_object_is_still_accepted(tmp_path):
    """Negative control: refusing the key's absence must not refuse emptiness.

    A project that has had every feature reset is a real state and a readable
    one. If this went red with the guard above, the guard would be refusing
    "nothing is set" rather than "this is not our file".
    """
    path = tmp_path / "features.json"
    path.write_text(json.dumps({"version": 1, "features": {}}), encoding="utf-8")
    assert project_features.read_config(path) == {"version": 1, "features": {}}


def test_a_setting_added_since_the_read_survives_this_write(tmp_path):
    """A downgrade must arrive as a conflict, never as a gap.

    The caller's document is a snapshot. Between taking it and writing, the
    user can install a newer toolkit, or a second operator can change
    something. Carrying unknown slugs from that snapshot alone drops anything
    that appeared in the meantime -- and a build that has no name for a
    setting is exactly the build that cannot notice it deleted one.
    """
    path = tmp_path / "features.json"
    project_features.write_config(path, {"spec-driven": OFF})
    stale = project_features.read_config(path)

    fresh = project_features.read_config(path)
    fresh["features"]["telepathy"] = "on"
    path.write_text(json.dumps(fresh), encoding="utf-8")

    project_features.write_config(path, {"session-history": OFF},
                                  document=stale)
    final = project_features.read_config(path)
    assert final["features"]["telepathy"] == "on", (
        "a setting made after the caller's read was dropped by this write")
    assert final["features"]["session-history"] == OFF


def test_a_file_that_became_unreadable_is_refused_not_overwritten(tmp_path):
    """Refuse rather than clobber, at the write end too.

    The menu refuses to *show* a configuration it could not read. Writing over
    one here would destroy the same choices by the other route.
    """
    path = tmp_path / "features.json"
    project_features.write_config(path, {"spec-driven": OFF})
    document = project_features.read_config(path)
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(FeatureConfigError):
        project_features.write_config(path, {"session-history": OFF},
                                      document=document)
    assert path.read_text(encoding="utf-8") == "{ not json", (
        "the refusal overwrote the file it could not read")


def test_the_bytes_reach_the_disk_before_the_rename(tmp_path, monkeypatch):
    """Closing flushes to the OS and no further.

    ``os.replace`` then commits a directory entry that can outlive the data it
    points at, so a power loss leaves a file that is present, empty, and
    refused by ``read_config`` -- the settings destroyed by the write meant to
    preserve them. Asserting the ordering, not just that fsync was called: a
    flush after the rename is the same defect.
    """
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync",
                        lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace",
                        lambda a, b: (events.append("replace"),
                                      real_replace(a, b))[1])
    project_features.write_config(tmp_path / "features.json", {"spec-driven": OFF})
    assert events == ["fsync", "replace"], events


def test_an_interrupt_mid_write_strands_nothing_and_keeps_the_old_file(tmp_path):
    """``KeyboardInterrupt`` is not an ``OSError``.

    Cleanup hangs off ``finally`` rather than ``except OSError`` precisely so
    that an interrupt is covered too. A temp file created and then forgotten
    leaves a ``.features-*.json`` in the project directory that nothing ever
    removes, and the previous configuration must still be the one on disk.
    """
    path = tmp_path / "features.json"
    project_features.write_config(path, {"spec-driven": OFF})
    before = path.read_text(encoding="utf-8")

    def interrupt(_fd):
        raise KeyboardInterrupt

    with mock.patch.object(os, "fsync", interrupt):
        with pytest.raises(KeyboardInterrupt):
            project_features.write_config(path, {"session-history": OFF})

    strays = [p.name for p in tmp_path.iterdir()
              if p.name.startswith(".features-")]
    assert not strays, f"an interrupted write stranded {strays}"
    assert path.read_text(encoding="utf-8") == before, (
        "an interrupted write changed the configuration it did not finish")


def test_writing_records_every_known_feature_explicitly(tmp_path):
    """The file is a record, not a diff.

    A file holding only what differs from today's defaults would silently
    change meaning the day a default changes -- a project that never touched
    a feature and a project that chose today's default would be the same
    bytes, and only one of them wants to follow the new default.
    """
    path = tmp_path / "features.json"
    project_features.write_config(path, {})
    stored = project_features.read_config(path)["features"]
    assert set(stored) == set(SLUGS)


def test_writing_an_unknown_feature_is_refused(tmp_path):
    with pytest.raises(FeatureConfigError, match="No such feature"):
        project_features.write_config(tmp_path / "features.json",
                                      {"telepathy": ON})


def test_writing_a_value_outside_the_vocabulary_is_refused(tmp_path):
    with pytest.raises(FeatureConfigError, match="spec-driven"):
        project_features.write_config(tmp_path / "features.json",
                                      {"spec-driven": "maybe"})


def test_a_refused_write_leaves_the_previous_file_alone(tmp_path):
    path = tmp_path / "features.json"
    project_features.write_config(path, {"spec-driven": OFF})
    before = path.read_text(encoding="utf-8")
    with pytest.raises(FeatureConfigError):
        project_features.write_config(path, {"spec-driven": "maybe"})
    assert path.read_text(encoding="utf-8") == before


def test_writing_leaves_no_temporary_file_behind(tmp_path):
    path = tmp_path / "nested" / "features.json"
    project_features.write_config(path, {"spec-driven": OFF})
    assert [p.name for p in path.parent.iterdir()] == ["features.json"]


def test_an_interrupted_write_leaves_no_temporary_file_behind(tmp_path,
                                                              monkeypatch):
    """A Ctrl-C between the write and the replace is not an ``OSError``.

    An ``except OSError`` cleanup would miss it and strand a ``.features-``
    file in the project directory with nothing that ever removes it.
    """
    path = tmp_path / "features.json"

    def interrupt(src, dst):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        project_features.write_config(path, {"spec-driven": OFF})
    assert list(tmp_path.iterdir()) == []


def test_a_write_to_an_unwritable_place_raises(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", refuse)
    with pytest.raises(FeatureConfigError, match="Cannot write"):
        project_features.write_config(tmp_path / "features.json", {})


def test_config_path_sits_beside_the_handoff(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    path = project_features.config_path("a-guid")
    assert path == tmp_path / ".operator" / "projects" / "a-guid" / "features.json"


# ---------------------------------------------------------------------------
# The backlog backend predicate
# ---------------------------------------------------------------------------

@pytest.fixture
def project(monkeypatch, tmp_path):
    """A catalogued project with a real (empty) git repository behind it."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    root = tmp_path / "repo"
    root.mkdir()
    guid = "11111111-2222-3333-4444-555555555555"
    catalog = tmp_path / "home" / ".operator" / "projects" / "catalog.csv"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(f'"{root}",{guid}\n', encoding="utf-8")
    monkeypatch.setattr(project_features, "primary_repo_root",
                        lambda start=None: Path(start) if start else root)
    return root, guid


def test_a_project_with_no_configuration_enforces_the_folder_backend(project):
    root, _ = project
    backend, source = project_features.tracked_backlog_backend(root)
    assert backend == BACKLOG_FOLDER
    assert "never been written" in source


def test_a_project_that_chose_github_issues_is_reported_as_such(project):
    root, guid = project
    project_features.write_config(project_features.config_path(guid),
                                  {TRACKED_BACKLOG: BACKLOG_GITHUB_ISSUES})
    backend, source = project_features.tracked_backlog_backend(root)
    assert backend == BACKLOG_GITHUB_ISSUES
    assert "features.json" in source


def test_a_project_that_chose_no_backlog_is_reported_as_such(project):
    root, guid = project
    project_features.write_config(project_features.config_path(guid),
                                  {TRACKED_BACKLOG: BACKLOG_NONE})
    backend, _ = project_features.tracked_backlog_backend(root)
    assert backend == BACKLOG_NONE


def test_an_uncatalogued_project_still_enforces(monkeypatch, tmp_path):
    """The case CI is in on all eight legs.

    A predicate that answered "unknown" here, or that let a missing catalog
    stand a guard down, would retire three real assertions everywhere while
    every one of them stayed green.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(project_features, "primary_repo_root",
                        lambda start=None: tmp_path / "repo")
    backend, source = project_features.tracked_backlog_backend(tmp_path / "repo")
    assert backend == BACKLOG_FOLDER
    assert "no project configuration" in source


def test_an_unreadable_configuration_still_enforces(project, monkeypatch):
    """Standing a guard down must be something a person configured.

    Anything else -- a permission error, a truncated file, a half-finished
    write -- has to fall on the enforcing side, or a corrupted byte disables
    three assertions and reports success.
    """
    root, guid = project
    path = project_features.config_path(guid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    backend, source = project_features.tracked_backlog_backend(root)
    assert backend == BACKLOG_FOLDER
    assert "could not be read" in source
