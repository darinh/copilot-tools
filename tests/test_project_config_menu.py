"""The Project Configurations screen.

The screen exists because the feature flags are the contract between a project
and every agent that works in it, and until now that contract was editable only
by an agent rewriting prose. So the tests that matter here are the ones about
*not lying*: a catalog that would not open must not render as an empty list of
projects, an unreadable configuration must not render as the defaults, and a
setting written by a newer build must survive a toggle made by this one.
"""

from __future__ import annotations

import json
import posixpath
from pathlib import Path

import pytest

import copilot_operator
import project_features
import project_instructions
from project_features import (
    BACKLOG_GITHUB_ISSUES,
    BACKLOG_NONE,
    FEATURES,
    OFF,
    ON,
    TRACKED_BACKLOG,
)

GUID = "11111111-2222-3333-4444-555555555555"
OTHER_GUID = "99999999-8888-7777-6666-555555555555"


@pytest.fixture
def home(monkeypatch, tmp_path):
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    monkeypatch.setattr(copilot_operator, "HOME", fake)
    return fake


@pytest.fixture
def catalog(monkeypatch, tmp_path, home):
    path = tmp_path / "catalog.csv"
    monkeypatch.setattr(copilot_operator, "project_catalog_path", lambda: path)
    return path


def answers(monkeypatch, *replies):
    """Script the interactive prompts, then refuse to invent more.

    A queue that returned ``""`` forever once exhausted would let a menu that
    asked an extra unexpected question still look like a clean run, because
    blank means "go back" everywhere on this screen.
    """
    queue = list(replies)

    def prompt(text: str) -> str:
        assert queue, f"the menu asked an unscripted question: {text!r}"
        return queue.pop(0)

    monkeypatch.setattr(copilot_operator, "_prompt_line", prompt)
    return queue


# ---------------------------------------------------------------------------
# Reading the catalog
# ---------------------------------------------------------------------------

def test_projects_are_listed_in_catalog_order(catalog):
    catalog.write_text(f'"/a/one",{GUID}\n"/b/two",{OTHER_GUID}\n',
                       encoding="utf-8")
    projects, problems = copilot_operator.catalog_projects()
    assert [p["guid"] for p in projects] == [GUID, OTHER_GUID]
    assert problems == []


def test_an_absent_catalog_is_no_projects_rather_than_an_error(catalog):
    projects, problems = copilot_operator.catalog_projects()
    assert projects == [] and problems == []


def test_a_catalog_that_will_not_open_is_not_an_empty_list(catalog, monkeypatch):
    """The distinction this screen would otherwise get wrong.

    Reporting "no projects registered" on the strength of a permission error
    tells a user every project on the machine is unset up, and the obvious
    next move -- setting one up again -- mints a duplicate GUID and splits
    that project's state in two.
    """
    catalog.write_text(f'"/a/one",{GUID}\n', encoding="utf-8")
    real = open

    def refuse(file, *args, **kwargs):
        if str(file) == str(catalog):
            raise PermissionError(13, "Permission denied")
        return real(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", refuse)
    assert copilot_operator.catalog_projects() is copilot_operator.CATALOG_UNREADABLE


def test_a_readable_catalog_is_not_reported_as_unreadable(catalog):
    """Negative control for the guard above."""
    catalog.write_text(f'"/a/one",{GUID}\n', encoding="utf-8")
    assert copilot_operator.catalog_projects() is not \
        copilot_operator.CATALOG_UNREADABLE


@pytest.mark.parametrize("line, needle", [
    ('"/a/one"\n', "no project id column"),
    ('"",' + GUID + "\n", "no project path"),
    ('"/a/one",../escape\n', "unusable project id"),
    ('"/a/one",\n', "unusable project id"),
])
def test_a_row_that_cannot_be_used_is_reported_not_dropped(catalog, line, needle):
    """A skipped row is not evidence that the project it names is absent.

    Dropping it silently lets a user look at this screen, fail to find their
    project, and conclude it was never set up.
    """
    catalog.write_text(line, encoding="utf-8")
    projects, problems = copilot_operator.catalog_projects()
    assert projects == []
    assert any(needle in p for p in problems), problems


def test_a_well_formed_row_produces_no_complaint(catalog):
    """Negative control: the row checks must not reject everything."""
    catalog.write_text(f'"/a/one",{GUID}\n', encoding="utf-8")
    projects, problems = copilot_operator.catalog_projects()
    assert len(projects) == 1 and problems == []


def test_a_repeated_project_id_is_reported(catalog):
    """Two rows pointing at one directory of state is a split identity."""
    catalog.write_text(f'"/a/one",{GUID}\n"/b/two",{GUID}\n', encoding="utf-8")
    projects, problems = copilot_operator.catalog_projects()
    assert len(projects) == 1
    assert any("repeats project id" in p for p in problems), problems


def test_a_blank_line_is_not_a_problem(catalog):
    catalog.write_text(f'"/a/one",{GUID}\n\n', encoding="utf-8")
    projects, problems = copilot_operator.catalog_projects()
    assert len(projects) == 1 and problems == []


@pytest.mark.parametrize("path, expected", [
    (r"C:\Users\dev\repos\app", "app"),
    ("/home/dev/projects/app", "app"),
    (r"C:\Users\dev\repos\app\\", "app"),
    ("/home/dev/projects/app/", "app"),
    (r"\\server\share\app", "app"),
])
def test_a_project_label_reads_either_platforms_path_syntax(path, expected):
    """``ntpath``, not ``os.path``.

    The catalog stores each path in the native form of the platform that
    created the entry, so a Linux machine reading a catalog synced from
    Windows meets a backslash path -- and ``posixpath.basename`` hands back
    the whole string, because a backslash is an ordinary filename character
    there. This test is parametrised across both syntaxes precisely so it
    cannot pass on the Windows legs alone.
    """
    assert copilot_operator._project_label(path) == expected


def test_a_project_label_never_comes_back_empty():
    assert copilot_operator._project_label("/") == "/"


def test_the_label_cases_would_catch_a_posix_basename():
    """Proof that the parametrisation above is discriminating on POSIX.

    On Windows ``os.path`` *is* ``ntpath``, so mutating the implementation
    back to ``os.path.basename`` cannot be observed on this machine at all --
    the mutation test passes locally and the defect ships to the four POSIX
    legs. That has already happened here once, in the test suite's own
    multiplexer guard. Asserting the naive POSIX reading is *wrong* for these
    inputs is what makes the case above evidence rather than decoration.
    """
    for windows_path in (r"C:\Users\dev\repos\app", r"\\server\share\app"):
        assert posixpath.basename(windows_path) != "app", (
            f"{windows_path} is not a case that separates ntpath from "
            "posixpath, so the parametrisation above proves nothing")


# ---------------------------------------------------------------------------
# The project list screen
# ---------------------------------------------------------------------------

def test_an_unreadable_catalog_refuses_rather_than_showing_nothing(
        catalog, monkeypatch, capsys):
    catalog.write_text(f'"/a/one",{GUID}\n', encoding="utf-8")
    monkeypatch.setattr(copilot_operator, "catalog_projects",
                        lambda *a, **k: copilot_operator.CATALOG_UNREADABLE)
    answers(monkeypatch)
    assert copilot_operator.browse_project_configurations() == 1
    err = capsys.readouterr().err
    assert "Cannot read the project catalog" in err
    assert "not the same as no projects being registered" in err


def test_an_empty_catalog_says_so(catalog, monkeypatch, capsys):
    answers(monkeypatch)
    assert copilot_operator.browse_project_configurations() == 0
    assert "No projects registered" in capsys.readouterr().out


def test_skipped_rows_are_shown_above_the_list(catalog, monkeypatch, capsys):
    catalog.write_text(f'"/a/one",{GUID}\n"/b/two",../escape\n', encoding="utf-8")
    answers(monkeypatch, "")
    copilot_operator.browse_project_configurations()
    out = capsys.readouterr().out
    assert "unusable project id" in out
    assert "missing from this list" in out


def test_the_list_summarises_how_many_features_are_on(catalog, monkeypatch,
                                                      capsys):
    catalog.write_text(f'"/a/one",{GUID}\n', encoding="utf-8")
    project_features.write_config(
        project_features.config_path(GUID),
        {f.slug: next(v for v in f.values if v != f.off_value)
         for f in FEATURES if f.slug != "spec-driven"}
        | {"spec-driven": OFF})
    answers(monkeypatch, "")
    copilot_operator.browse_project_configurations()
    out = capsys.readouterr().out
    assert f"{len(FEATURES) - 1} of {len(FEATURES)} enabled" in out


def test_a_project_whose_configuration_will_not_read_is_marked_in_the_list(
        catalog, monkeypatch, capsys):
    catalog.write_text(f'"/a/one",{GUID}\n', encoding="utf-8")
    path = project_features.config_path(GUID)
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    answers(monkeypatch, "")
    copilot_operator.browse_project_configurations()
    assert "unreadable configuration" in capsys.readouterr().out


@pytest.mark.parametrize("reply", ["nope", "0", "99"])
def test_an_unusable_project_choice_re_asks(catalog, monkeypatch, capsys, reply):
    catalog.write_text(f'"/a/one",{GUID}\n', encoding="utf-8")
    answers(monkeypatch, reply, "")
    assert copilot_operator.browse_project_configurations() == 0
    assert capsys.readouterr().err.strip() != ""


# ---------------------------------------------------------------------------
# The feature screen
# ---------------------------------------------------------------------------

def _project(path="/a/one", guid=GUID) -> dict:
    return {"path": path, "guid": guid,
            "label": copilot_operator._project_label(path)}


def test_a_flag_is_toggled_and_written_immediately(home, monkeypatch):
    """Written per change, not on the way out.

    The way out includes Ctrl-C and a closed terminal, and a screen that shows
    a feature as off while the file still says on is worse than no screen.
    """
    index = [f.slug for f in FEATURES].index("spec-driven") + 1
    answers(monkeypatch, str(index), "")
    assert copilot_operator.show_project_config(_project()) == 0
    values = project_features.resolved_values(
        project_features.read_config(project_features.config_path(GUID)))
    assert values["spec-driven"] == ON
    assert values["spec-driven"] != (
        project_features.FEATURES_BY_SLUG["spec-driven"].default), (
        "one toggle has to move the flag off its default, or this test would "
        "pass against a menu that wrote nothing")


def test_toggling_twice_returns_a_flag_to_where_it_was(home, monkeypatch):
    index = [f.slug for f in FEATURES].index("spec-driven") + 1
    answers(monkeypatch, str(index), str(index), "")
    copilot_operator.show_project_config(_project())
    document = project_features.read_config(project_features.config_path(GUID))
    assert document is not None, "the menu wrote nothing at all"
    assert "spec-driven" in document["features"], (
        "the second toggle has to write the value back explicitly; letting it "
        "fall through to the default would pass this test for the wrong "
        "reason now that the default is off")
    values = project_features.resolved_values(document)
    assert values["spec-driven"] == OFF


def test_a_choice_offers_its_backends_rather_than_toggling(home, monkeypatch,
                                                           capsys):
    """The requirement a toggle list could not express."""
    feature_index = [f.slug for f in FEATURES].index(TRACKED_BACKLOG) + 1
    backlog = project_features.FEATURES_BY_SLUG[TRACKED_BACKLOG]
    option_index = backlog.values.index(BACKLOG_GITHUB_ISSUES) + 1
    answers(monkeypatch, str(feature_index), str(option_index), "")
    copilot_operator.show_project_config(_project())

    out = capsys.readouterr().out
    for value in backlog.values:
        assert value in out
    values = project_features.resolved_values(
        project_features.read_config(project_features.config_path(GUID)))
    assert values[TRACKED_BACKLOG] == BACKLOG_GITHUB_ISSUES


def test_declining_a_choice_changes_nothing(home, monkeypatch):
    feature_index = [f.slug for f in FEATURES].index(TRACKED_BACKLOG) + 1
    answers(monkeypatch, str(feature_index), "", "")
    copilot_operator.show_project_config(_project())
    assert project_features.read_config(
        project_features.config_path(GUID)) is None


def test_the_back_entry_leaves_without_writing(home, monkeypatch):
    answers(monkeypatch, str(len(FEATURES) + 1))
    assert copilot_operator.show_project_config(_project()) == 0
    assert project_features.read_config(
        project_features.config_path(GUID)) is None


def test_an_unreadable_configuration_refuses_rather_than_showing_defaults(
        home, monkeypatch, capsys):
    """The failure this screen must never paper over.

    Resolving an unreadable file to the defaults would render a complete,
    confident answer about a project whose real choices nobody looked at --
    and then write those invented defaults over the file on the first toggle.
    """
    path = project_features.config_path(GUID)
    path.parent.mkdir(parents=True)
    original = "{ not json"
    path.write_text(original, encoding="utf-8")
    answers(monkeypatch)

    assert copilot_operator.show_project_config(_project()) == 1
    assert "Refusing to show or change" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == original


def test_a_setting_from_a_newer_build_survives_a_toggle_here(home, monkeypatch):
    """A downgrade must fail as a duplicate, never as a gap."""
    path = project_features.config_path(GUID)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"features": {"telepathy": "on"}}),
                    encoding="utf-8")
    index = [f.slug for f in FEATURES].index("spec-driven") + 1
    answers(monkeypatch, str(index), "")
    copilot_operator.show_project_config(_project())

    stored = project_features.read_config(path)["features"]
    assert stored["telepathy"] == "on"
    assert stored["spec-driven"] == ON


def test_settings_from_a_newer_build_are_named_on_screen(home, monkeypatch,
                                                         capsys):
    path = project_features.config_path(GUID)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"features": {"telepathy": "on"}}),
                    encoding="utf-8")
    answers(monkeypatch, "")
    copilot_operator.show_project_config(_project())
    assert "telepathy" in capsys.readouterr().out


def test_a_configuration_that_was_never_written_says_so(home, monkeypatch,
                                                        capsys):
    answers(monkeypatch, "")
    copilot_operator.show_project_config(_project())
    assert "not written yet" in capsys.readouterr().out


@pytest.mark.parametrize("reply", ["nope", "0", "999"])
def test_an_unusable_feature_choice_re_asks(home, monkeypatch, capsys, reply):
    answers(monkeypatch, reply, "")
    assert copilot_operator.show_project_config(_project()) == 0
    assert capsys.readouterr().err.strip() != ""
    assert project_features.read_config(
        project_features.config_path(GUID)) is None


def test_a_write_that_fails_is_reported_and_not_claimed(home, monkeypatch,
                                                        capsys):
    """A refused write must not print the change as though it happened."""
    def refuse(*args, **kwargs):
        raise project_features.FeatureConfigError("disk is on fire")

    monkeypatch.setattr(project_features, "write_config", refuse)
    index = [f.slug for f in FEATURES].index("spec-driven") + 1
    answers(monkeypatch, str(index), "")
    copilot_operator.show_project_config(_project())
    captured = capsys.readouterr()
    assert "disk is on fire" in captured.err
    assert "Spec-Driven Development →" not in captured.out


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_the_menu_offers_the_screen(monkeypatch, capsys, home):
    """The entry point a human actually reaches it through.

    The index is read off the rendered menu rather than hard-coded, so
    inserting an entry above it does not silently start testing a different
    action while still passing.
    """
    monkeypatch.setattr(copilot_operator, "active_instances", lambda: [])
    called = []
    monkeypatch.setattr(copilot_operator, "browse_project_configurations",
                        lambda: called.append(True) or 0)

    answers(monkeypatch, "")
    copilot_operator.show_menu()
    rendered = capsys.readouterr().out
    matches = [line for line in rendered.splitlines()
               if "Project configurations" in line]
    assert len(matches) == 1, f"no single menu entry for it:\n{rendered}"
    index = matches[0].strip().split(")")[0]

    answers(monkeypatch, index, "")
    assert copilot_operator.show_menu() == 0
    assert called == [True]


def test_the_projects_subcommand_reaches_the_screen(monkeypatch):
    called = []
    monkeypatch.setattr(copilot_operator, "browse_project_configurations",
                        lambda: called.append(True) or 0)
    monkeypatch.setattr(copilot_operator, "migrate_legacy_state", lambda: None)
    assert copilot_operator._dispatch_command(["projects"]) == 0
    assert called == [True]


def test_projects_is_reserved_so_it_cannot_be_joined_as_an_instance():
    """Otherwise ``operator projects`` would try to attach to a session."""
    assert "projects" in copilot_operator.RESERVED_WORDS


def test_the_help_text_documents_the_subcommand():
    assert "operator projects" in copilot_operator.HELP


# ---------------------------------------------------------------------------
# Retiring the user-scope instructions file
# ---------------------------------------------------------------------------

def _plant_global(home):
    """Put a user-scope instructions file where the operator looks for it."""
    path = home / ".copilot" / project_instructions.GLOBAL_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Conventions\n", encoding="utf-8")
    return path


def test_the_file_this_screen_retires_is_the_one_loaded_everywhere(home):
    """Not a path this test invented: the operator's own accessor."""
    assert copilot_operator.global_instructions_path() == \
        home / ".copilot" / project_instructions.GLOBAL_NAME


def test_a_planted_file_is_seen_and_an_absent_one_is_not(home):
    assert copilot_operator.user_instructions_present() is False
    _plant_global(home)
    assert copilot_operator.user_instructions_present() is True


def test_a_file_that_cannot_be_examined_counts_as_present(monkeypatch, home):
    """A stat that fails is not an answer of "absent".

    The file is still being read into every session on this machine; saying
    nothing about it because a probe raised would hide the exact condition
    this screen exists to end.
    """
    monkeypatch.setattr(copilot_operator, "path_present", lambda p: None)
    assert copilot_operator.user_instructions_present() is True


def test_the_offer_appears_only_while_the_file_is_there(catalog, monkeypatch,
                                                        capsys, home):
    catalog.write_text(f'"/a/one",{GUID}\n', encoding="utf-8")
    answers(monkeypatch, "")
    copilot_operator.browse_project_configurations()
    assert "Retire" not in capsys.readouterr().out

    _plant_global(home)
    answers(monkeypatch, "")
    copilot_operator.browse_project_configurations()
    assert "Retire" in capsys.readouterr().out


def test_the_offer_is_reached_by_the_index_it_prints(catalog, monkeypatch,
                                                     capsys, home):
    """The index is read off the screen rather than assumed.

    Hard-coding it would keep passing while a project row silently moved
    under the number, which on this screen means retiring the file when the
    human asked to edit a project's features.
    """
    catalog.write_text(f'"/a/one",{GUID}\n"/a/two",{OTHER_GUID}\n', encoding="utf-8")
    _plant_global(home)
    called = []
    monkeypatch.setattr(copilot_operator, "retire_user_instructions",
                        lambda: called.append(True) or 0)
    answers(monkeypatch, "")
    copilot_operator.browse_project_configurations()
    rendered = capsys.readouterr().out
    matches = [line for line in rendered.splitlines() if "Retire" in line]
    assert len(matches) == 1, f"no single entry for it:\n{rendered}"
    index = matches[0].strip().split(")")[0]

    answers(monkeypatch, index, "")
    assert copilot_operator.browse_project_configurations() == 0
    assert called == [True]


def test_the_offer_does_not_displace_a_project(catalog, monkeypatch, home):
    """It is appended below the projects, so project 1 is still project 1."""
    catalog.write_text(f'"/a/one",{GUID}\n', encoding="utf-8")
    _plant_global(home)
    opened = []
    monkeypatch.setattr(copilot_operator, "show_project_config",
                        lambda project: opened.append(project["path"]) or 0)
    monkeypatch.setattr(copilot_operator, "retire_user_instructions",
                        lambda: pytest.fail("chose the wrong entry"))
    answers(monkeypatch, "1", "")
    assert copilot_operator.browse_project_configurations() == 0
    assert opened == ["/a/one"]


def test_an_out_of_range_choice_is_still_refused_with_the_offer_shown(
        catalog, monkeypatch, capsys, home):
    """The upper bound moves with the offer; one past it is not the offer."""
    catalog.write_text(f'"/a/one",{GUID}\n', encoding="utf-8")
    _plant_global(home)
    monkeypatch.setattr(copilot_operator, "retire_user_instructions",
                        lambda: pytest.fail("3 is not the retirement entry"))
    answers(monkeypatch, "3", "")
    assert copilot_operator.browse_project_configurations() == 0
    assert "Out of range" in capsys.readouterr().err


def test_the_main_menu_says_so_while_the_file_is_there(monkeypatch, capsys,
                                                       home):
    monkeypatch.setattr(copilot_operator, "active_instances", lambda: [])
    answers(monkeypatch, "")
    copilot_operator.show_menu()
    assert project_instructions.GLOBAL_NAME not in capsys.readouterr().out

    _plant_global(home)
    answers(monkeypatch, "")
    copilot_operator.show_menu()
    assert project_instructions.GLOBAL_NAME in capsys.readouterr().out


def test_the_retire_subcommand_reaches_it(monkeypatch):
    seen = []
    monkeypatch.setattr(copilot_operator, "retire_user_instructions",
                        lambda assume_yes=False: seen.append(assume_yes) or 0)
    monkeypatch.setattr(copilot_operator, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(copilot_operator, "browse_project_configurations",
                        lambda: pytest.fail("retire went to the browser"))
    assert copilot_operator._dispatch_command(["projects", "retire"]) == 0
    assert seen == [False]


def test_the_yes_flag_is_what_carries_consent(monkeypatch):
    """Without it the write is asked about; the flag must reach the function."""
    seen = []
    monkeypatch.setattr(copilot_operator, "retire_user_instructions",
                        lambda assume_yes=False: seen.append(assume_yes) or 0)
    monkeypatch.setattr(copilot_operator, "migrate_legacy_state", lambda: None)
    assert copilot_operator._dispatch_command(
        ["projects", "retire", "--yes"]) == 0
    assert seen == [True]


def test_an_unknown_projects_subcommand_is_refused(monkeypatch, capsys):
    """Otherwise a typo silently opens the browser and looks like success."""
    monkeypatch.setattr(copilot_operator, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(copilot_operator, "browse_project_configurations",
                        lambda: pytest.fail("a typo reached the browser"))
    monkeypatch.setattr(copilot_operator, "retire_user_instructions",
                        lambda assume_yes=False: pytest.fail("not retire"))
    assert copilot_operator._dispatch_command(["projects", "retyre"]) == 1
    assert "retyre" in capsys.readouterr().err


def test_bare_projects_still_opens_the_browser(monkeypatch):
    """The refusal above must not have swallowed the no-argument form."""
    called = []
    monkeypatch.setattr(copilot_operator, "browse_project_configurations",
                        lambda: called.append(True) or 0)
    monkeypatch.setattr(copilot_operator, "migrate_legacy_state", lambda: None)
    assert copilot_operator._dispatch_command(["projects"]) == 0
    assert called == [True]


def test_the_help_text_documents_the_retire_subcommand():
    assert "operator projects retire" in copilot_operator.HELP


def test_the_archive_sits_in_the_toolkits_own_state_directory(home):
    """Under ``~/.operator``, not the repository and not a temp directory.

    This used to assert the opposite -- that the archive sat *beside* the file
    it preserves, in ``~/.copilot`` -- and adjacency was the point: somebody
    looking for the retired file would find it where the original had been.

    That was given up deliberately. ``~/.copilot`` is the Copilot CLI's own
    configuration directory, and a backup kept in a directory this toolkit
    does not own is not a backup. Discoverability is answered by the retire
    screen naming the path it wrote, which is a better answer than adjacency
    because it does not depend on the user guessing where to look.
    """
    archive = copilot_operator.instructions_archive_dir()
    assert archive == home / ".operator" / project_instructions.ARCHIVE_DIRNAME
    assert archive.parent == copilot_operator.operator_home()
    assert archive.parent != copilot_operator.global_instructions_path().parent


def test_the_template_is_the_one_shipped_beside_the_operator():
    path = copilot_operator._repo_template_path()
    assert path.name == project_instructions.TEMPLATE_NAME
    assert path.parent.name == "templates"
    assert copilot_operator.path_present(path) is True


def test_a_catalog_row_that_will_not_parse_stops_the_removal(
        catalog, monkeypatch, capsys, home):
    """A row that cannot be read is not a row naming no project.

    Three reviewers found this independently. The screen printed the skipped
    rows, said the file would stay, and then retired it anyway — so a machine
    with one malformed catalog line lost the conventions for whatever project
    that line named. The failure mode must be a duplicate, never a gap.
    """
    _plant_global(home)
    catalog.write_text(f'"/a/one",{GUID}\nnonsense-with-no-comma\n',
                       encoding="utf-8")
    monkeypatch.setattr(project_instructions, "retire",
                        lambda *a, **k: pytest.fail(
                            "retired despite an unparseable catalog row"))
    assert copilot_operator.retire_user_instructions(assume_yes=True) == 1
    assert copilot_operator.user_instructions_present() is True
    assert "stays" in capsys.readouterr().err


def test_a_clean_catalog_is_not_treated_as_a_partial_read(catalog, monkeypatch,
                                                          home):
    """The control: the refusal above must not block the ordinary case."""
    _plant_global(home)
    catalog.write_text(f'"/a/one",{GUID}\n', encoding="utf-8")
    seen = []

    class _Result:
        removed = True
        archived = None
        problems: list = []
        blockers: list = []
        user_agents: list = []
        placed: list = []
        source_origin = "the repository template"

    monkeypatch.setattr(project_instructions, "retire",
                        lambda *a, **k: seen.append(True) or _Result())
    monkeypatch.setattr(project_instructions, "resolve_source",
                        lambda *a, **k: ("# text\n", "the repository template"))
    assert copilot_operator.retire_user_instructions(assume_yes=True) == 0
    assert seen == [True]


def test_an_unreadable_catalog_removes_nothing(catalog, monkeypatch, home):
    _plant_global(home)
    monkeypatch.setattr(copilot_operator, "catalog_projects",
                        lambda: copilot_operator.CATALOG_UNREADABLE)
    monkeypatch.setattr(project_instructions, "retire",
                        lambda *a, **k: pytest.fail("retired blindly"))
    assert copilot_operator.retire_user_instructions(assume_yes=True) == 1
    assert copilot_operator.user_instructions_present() is True


def test_an_empty_catalog_removes_nothing(catalog, monkeypatch, home):
    """Removing it with no project registered takes it off the machine."""
    _plant_global(home)
    catalog.write_text("", encoding="utf-8")
    monkeypatch.setattr(project_instructions, "retire",
                        lambda *a, **k: pytest.fail("retired with no projects"))
    assert copilot_operator.retire_user_instructions(assume_yes=True) == 1
    assert copilot_operator.user_instructions_present() is True


def test_a_catalog_that_changes_mid_run_stops_the_removal(catalog, monkeypatch,
                                                          home, tmp_path):
    """The list of projects is a snapshot, and this machine has other agents.

    A project registered after the snapshot is never written to. Removing the
    global file anyway is precisely the gap the whole feature exists to
    prevent, so the last thing before anything is removed is a re-read.
    """
    _plant_global(home)
    repo = tmp_path / "one"
    repo.mkdir()
    catalog.write_text(f'"{repo}",{GUID}\n', encoding="utf-8")
    project_features.write_config(project_features.config_path(GUID),
                                  {"session-handoff": ON})
    monkeypatch.setattr(project_instructions, "resolve_source",
                        lambda *a, **k: ("# Conventions\n\n## A\n\nbody\n",
                                         "the repository template"))

    real_retire = project_instructions.retire

    def racing_retire(projects, **kwargs):
        outer = kwargs.pop("recheck")

        def recheck():
            catalog.write_text(
                f'"{repo}",{GUID}\n"/a/two",{OTHER_GUID}\n', encoding="utf-8")
            return outer()

        return real_retire(projects, recheck=recheck, **kwargs)

    monkeypatch.setattr(project_instructions, "retire", racing_retire)
    assert copilot_operator.retire_user_instructions(assume_yes=True) == 1
    assert copilot_operator.user_instructions_present() is True
    assert (repo / project_instructions.AGENTS_NAME).is_file()  # probe-ok: test


def test_an_unchanged_catalog_is_not_mistaken_for_a_race(catalog, monkeypatch,
                                                         home, tmp_path):
    """The control: the re-read must not block the ordinary case."""
    _plant_global(home)
    repo = tmp_path / "one"
    repo.mkdir()
    catalog.write_text(f'"{repo}",{GUID}\n', encoding="utf-8")
    project_features.write_config(project_features.config_path(GUID),
                                  {"session-handoff": ON})
    monkeypatch.setattr(project_instructions, "resolve_source",
                        lambda *a, **k: ("# Conventions\n\n## A\n\nbody\n",
                                         "the repository template"))
    assert copilot_operator.retire_user_instructions(assume_yes=True) == 0
    assert copilot_operator.user_instructions_present() is False
    assert (repo / project_instructions.AGENTS_NAME).is_file()  # probe-ok: test


def test_the_fingerprint_notices_a_same_length_edit(catalog, home):
    """Bytes, not mtime or size.

    Two agents registering a moment apart is the case a coarse timestamp
    misses, and a replaced guid is the case a size check misses.
    """
    catalog.write_text(f'"/a/one",{GUID}\n', encoding="utf-8")
    before = copilot_operator._catalog_fingerprint()
    catalog.write_text(f'"/a/one",{OTHER_GUID}\n', encoding="utf-8")
    assert len(OTHER_GUID) == len(GUID), "the edit must not change the size"
    assert copilot_operator._catalog_fingerprint() != before


def test_a_catalog_that_vanishes_counts_as_a_change(catalog, home):
    catalog.write_text(f'"/a/one",{GUID}\n', encoding="utf-8")
    before = copilot_operator._catalog_fingerprint()
    catalog.unlink()
    assert copilot_operator._catalog_fingerprint() != before
