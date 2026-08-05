"""Moving the project catalog out of the Copilot CLI's directory.

``~/.copilot`` belongs to the Copilot CLI: its extensions, skills, settings,
session store and logs are all in there. This toolkit kept its project
catalog and per-project directories in it too, which was squatting. Every
other piece of operator state -- the restart markers, the log, the metrics
database, the backups -- had already been moved out; the catalog had not, and
it is the one that matters most, because it is what maps a project path to
its id. Lose it and you have not lost a preference, you have lost every
project's identity and with it every handoff keyed to that id.

The migration is therefore judged on one question above all: can it ever
destroy a file? These tests are mostly about the cases where it must refuse.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import copilot_operator as op


@pytest.fixture
def homes(monkeypatch, tmp_path):
    """A legacy ``~/.copilot/projects`` and a fresh ``~/.operator``."""
    legacy = tmp_path / ".copilot" / "projects"
    legacy.mkdir(parents=True)
    monkeypatch.setenv("COPILOT_OPERATOR_HOME", str(tmp_path / ".operator"))
    monkeypatch.setattr(op, "LEGACY_PROJECTS_DIR", legacy)
    return legacy, tmp_path / ".operator" / "projects"


@pytest.fixture
def logged(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(op, "log", lines.append)
    return lines


def _plant(root: Path, guid: str = "a-guid") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "catalog.csv").write_text(f'"C:\\repo",{guid}\n', encoding="utf-8")
    project = root / guid
    project.mkdir(exist_ok=True)
    (project / "next-session.md").write_text("# handoff\n", encoding="utf-8")
    (project / "superseded").mkdir(exist_ok=True)
    (project / "superseded" / "old.md").write_text("older\n", encoding="utf-8")


def test_the_catalog_and_every_project_directory_move(homes, logged):
    legacy, new = homes
    _plant(legacy)

    moved = op._migrate_legacy_projects()

    assert moved == 2
    assert (new / "catalog.csv").read_text(encoding="utf-8").startswith('"C:')
    assert (new / "a-guid" / "next-session.md").exists()
    assert (new / "a-guid" / "superseded" / "old.md").exists()
    assert not (legacy / "catalog.csv").exists()


def test_a_handoff_at_the_destination_is_never_overwritten(homes, logged):
    """The one outcome that would be worse than not migrating at all.

    The new location is the live one by the time this runs, so anything
    already there is newer than the copy left behind in ``~/.copilot``.
    Overwriting would replace a current handoff with a stale one, and a
    handoff is read once and deleted -- so the loss would be silent and total.
    """
    legacy, new = homes
    _plant(legacy)
    new.mkdir(parents=True)
    (new / "a-guid").mkdir()
    (new / "a-guid" / "next-session.md").write_text("CURRENT\n",
                                                    encoding="utf-8")

    op._migrate_legacy_projects()

    assert (new / "a-guid" / "next-session.md").read_text(
        encoding="utf-8") == "CURRENT\n"
    assert (legacy / "a-guid" / "next-session.md").exists(), \
        "the legacy copy must be left where it is, not deleted"
    assert any("left in place" in line for line in logged)


def test_a_collision_does_not_stop_the_other_entries(homes, logged):
    """One occupied destination must not strand the rest of the catalog."""
    legacy, new = homes
    _plant(legacy)
    new.mkdir(parents=True)
    (new / "a-guid").mkdir()

    moved = op._migrate_legacy_projects()

    assert moved == 1
    assert (new / "catalog.csv").exists(), "the catalog still had to move"


def test_nothing_to_migrate_is_silent(homes, logged):
    legacy, new = homes
    legacy.rmdir()

    assert op._migrate_legacy_projects() == 0
    assert logged == []
    assert not new.exists()


def test_an_unexaminable_legacy_directory_is_reported_not_skipped(
        homes, logged, monkeypatch):
    """A skip caused by an unreadable source must not look like an empty one.

    Both leave the destination empty. Only one of them means the catalog is
    still sitting in a directory this toolkit does not own, and the difference
    is invisible unless it is said out loud.
    """
    legacy, new = homes
    monkeypatch.setattr(op, "dir_present", lambda path: None)

    assert op._migrate_legacy_projects() == 0
    assert any("Could not examine" in line for line in logged)


def test_a_legacy_directory_that_will_not_list_is_reported(homes, logged,
                                                           monkeypatch):
    legacy, new = homes
    _plant(legacy)

    def refuse(self):
        raise OSError("denied")

    monkeypatch.setattr(Path, "iterdir", refuse)

    assert op._migrate_legacy_projects() == 0
    assert any("Could not list" in line for line in logged)


def test_migration_is_a_no_op_when_the_two_locations_are_the_same(
        homes, logged, monkeypatch):
    """The override can point ``~/.operator`` at the legacy path itself.

    Asserting only that nothing moved would not test the guard: without it
    every entry is examined, found to already exist at a destination that is
    itself, and skipped -- which also moves nothing. The log is what tells the
    two apart, and a run that reports every project in the catalog as
    "already exists, left in place" reads as a failed migration.
    """
    legacy, new = homes
    _plant(new)
    monkeypatch.setattr(op, "LEGACY_PROJECTS_DIR", op.projects_root())

    assert op._migrate_legacy_projects() == 0
    assert logged == [], "a no-op must be silent, not a page of skips"


def test_the_whole_migration_runs_it(homes, logged, monkeypatch, tmp_path):
    """`_migrate_legacy_projects` is reached by `migrate_legacy_state`.

    The unit tests above call it directly, so none of them would notice if it
    were never wired into the one function that runs at startup.
    """
    legacy, new = homes
    _plant(legacy)
    monkeypatch.setattr(op, "RESTART_DIR", tmp_path / ".operator" / "restart")
    monkeypatch.setattr(op, "OPERATOR_HOME", tmp_path / ".operator")
    for name in ("LEGACY_RESTART_DIR", "LEGACY_BACKUPS_DIR"):
        monkeypatch.setattr(op, name, tmp_path / "absent" / name)
    for name in ("LEGACY_LOG_FILE", "LEGACY_METRICS_DB"):
        monkeypatch.setattr(op, name, tmp_path / "absent" / name)

    op.migrate_legacy_state()

    assert (new / "catalog.csv").exists()
