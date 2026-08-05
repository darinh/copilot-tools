"""Migrating project-keyed handoff state into the per-instance layout.

The handoff used to live at ``{project}/next-session.md`` with an unbounded
``{project}/superseded/`` beside it. Both hold context that was written to be
read by an agent and, in the ``superseded/`` case, demonstrably never was.

So the property under test throughout is **nothing is deleted and nothing is
overwritten**. Every test here reads the whole project tree and asks whether
the words still exist somewhere, rather than asserting a destination path: a
migration that moved files to a different place than intended is a bug worth
one failing assertion, while one that dropped them is the failure this module
exists to prevent.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import handoff_tool as ho


@pytest.fixture
def proj(tmp_path):
    d = tmp_path / "project-guid"
    d.mkdir()
    return d


def _all_text(root: Path) -> list[str]:
    return [p.read_text(encoding="utf-8", errors="replace")
            for p in root.rglob("*") if p.is_file()]


def _stamped(instance: str, body: str) -> str:
    return f"# Session Handoff\n\n{ho.author_line(instance)}\n\n## Status\n{body}\n"


def _can_symlink(tmp_path: Path) -> bool:
    try:
        (tmp_path / "_lnk").symlink_to(tmp_path / "_nothing")
    except (OSError, NotImplementedError):
        return False
    (tmp_path / "_lnk").unlink()
    return True


# -- delivering an attributed handoff -----------------------------
def test_an_attributed_handoff_is_delivered_to_its_author(proj):
    """The good case: the file says who wrote it, so it can be handed over.

    Delivery rather than parking is the whole value of keeping the author
    stamp through this change -- it is the difference between the author's
    next session reading its predecessor normally and finding an empty
    mailbox with the context filed under ``legacy/``.
    """
    (proj / "next-session.md").write_text(
        _stamped("peer-x", "peer-x's context"), encoding="utf-8")

    moved = ho.migrate_project_handoff(proj)

    delivered = proj / "handoff" / "peer-x.md"
    assert "peer-x's context" in delivered.read_text(encoding="utf-8")
    assert not (proj / "next-session.md").exists()
    assert moved and "peer-x" in moved[0]


def test_delivery_uses_the_same_name_mangling_as_the_writer(proj):
    """An instance name is free text; a filename is not.

    If the migration spelled the destination differently from
    ``handoff_path``, the delivered file would sit one character away from
    where its author's next session looks -- present on disk, invisible in
    practice, and indistinguishable from a lost handoff.
    """
    (proj / "next-session.md").write_text(
        _stamped("my:proj", "context"), encoding="utf-8")

    ho.migrate_project_handoff(proj)

    expected = ho.handoff_path(proj, ho.safe_instance_id("my:proj"))
    assert expected.is_file()


def test_delivery_never_overwrites_a_handoff_already_waiting(proj):
    """The newer file wins by staying put, and the older one is still kept.

    The instance-keyed file is written by current code and is therefore the
    newer of the two. Letting a pre-migration ``next-session.md`` land on top
    of it would destroy a live handoff in the name of preserving a stale one.
    """
    ho.handoff_path(proj, "solo").parent.mkdir(parents=True)
    ho.handoff_path(proj, "solo").write_text(
        _stamped("solo", "the current handoff"), encoding="utf-8")
    (proj / "next-session.md").write_text(
        _stamped("solo", "the stale handoff"), encoding="utf-8")

    ho.migrate_project_handoff(proj)

    assert "the current handoff" in \
        ho.handoff_path(proj, "solo").read_text(encoding="utf-8")
    surviving = _all_text(proj)
    assert any("the stale handoff" in t for t in surviving)


# -- parking what cannot be attributed ---------------------------
def test_an_unattributed_handoff_is_parked_rather_than_guessed(proj):
    """"Does not say" is not a licence to pick someone.

    A handoff is written in the first person -- my worktree, my branch, check
    my inbox. Handing an unattributed one to an arbitrary instance is not a
    lucky guess that might pay off; it is wrong instructions about a tree that
    agent does not own, which is the exact harm the re-keying removes.
    """
    (proj / "next-session.md").write_text(
        "# Session Handoff\n\n## Status\nnobody signed this\n", encoding="utf-8")

    ho.migrate_project_handoff(proj)

    parked = proj / "handoff" / "legacy" / "next-session.md"
    assert "nobody signed this" in parked.read_text(encoding="utf-8")
    # And nothing was invented: no instance file exists at all.
    assert not [p for p in (proj / "handoff").glob("*.md")]


def test_every_superseded_file_is_kept(proj):
    """Already-banked context is preserved, not delivered.

    A file in ``superseded/`` is one that was replaced before anyone read it,
    so it is at best a duplicate and at worst somebody's only copy. Parking
    all of it keeps the second case safe; delivering it would put an old
    handoff in front of an agent as if it were current.
    """
    old = proj / "superseded"
    old.mkdir()
    for n in range(4):
        (old / f"2026-08-05T0{n}0000Z.md").write_text(
            _stamped("peer-x", f"banked context {n}"), encoding="utf-8")

    ho.migrate_project_handoff(proj)

    surviving = _all_text(proj)
    for n in range(4):
        assert any(f"banked context {n}" in t for t in surviving)
    assert not list(old.iterdir())


def test_parking_does_not_collide_with_an_earlier_migration(proj):
    """A second unattributed ``next-session.md`` can appear after migration.

    Not hypothetical: backlog item 0018 records supervisors still running
    pre-migration code and writing to the old path, so a project can be
    migrated and then handed another project-keyed handoff. A namer that
    simply used the source filename would drop the earlier one -- the whole
    failure class this migration exists to avoid, reintroduced by the
    migration itself.

    Two invocations rather than one directory of same-named files, because
    that is the shape the collision actually has: within a single run the two
    sources are ``next-session.md`` and ``superseded/*``, and the latter is
    prefixed, so nothing collides and this would assert nothing.
    """
    strays = ["the first stray", "the second stray", "the third stray"]
    for body in strays:
        (proj / "next-session.md").write_text(body, encoding="utf-8")
        ho.migrate_project_handoff(proj)

    surviving = _all_text(proj)
    for body in strays:
        assert any(body in t for t in surviving), f"{body!r} was dropped"

    # Three of them, because two cannot tell an incrementing suffix from one
    # that appends to the name it just produced -- both give `x-1.md` on the
    # first collision, and only the third asks for `x-2.md` rather than
    # `x-1-2.md`. The names are what a human reads when deciding whether a
    # parked file is worth opening, so they are worth pinning.
    parked = sorted(pth.name for pth
                    in (proj / "handoff" / ho.LEGACY_DIRNAME).iterdir())
    assert parked == ["next-session-1.md", "next-session-2.md",
                      "next-session.md"], parked


def test_a_parked_superseded_file_is_marked_as_one(proj):
    """``next-session.md`` and an archived copy of it both reach ``legacy/``.

    The prefix is what keeps them apart, and it also keeps the two kinds of
    parked file distinguishable to whoever reads the directory: one was the
    live handoff at migration time, the other was already banked.
    """
    (proj / "next-session.md").write_text("live at migration time",
                                          encoding="utf-8")
    old = proj / "superseded"
    old.mkdir()
    (old / "next-session.md").write_text("already banked", encoding="utf-8")

    ho.migrate_project_handoff(proj)

    legacy = proj / "handoff" / "legacy"
    assert (legacy / "next-session.md").read_text(encoding="utf-8") == \
        "live at migration time"
    assert (legacy / "superseded-next-session.md").read_text(encoding="utf-8") == \
        "already banked"


# -- the ordinary case -------------------------------------------
def test_a_project_with_nothing_to_migrate_is_left_alone(proj):
    """Every handoff runs this. It must be silent and inert when there is
    nothing there, or it becomes a per-write cost and a per-write risk."""
    assert ho.migrate_project_handoff(proj) == []
    assert list(proj.iterdir()) == []


def test_migration_is_idempotent(proj):
    """It runs on every handoff, so running twice must equal running once."""
    (proj / "next-session.md").write_text(
        _stamped("peer-x", "context"), encoding="utf-8")

    ho.migrate_project_handoff(proj)
    before = sorted(str(p.relative_to(proj)) for p in proj.rglob("*"))
    assert ho.migrate_project_handoff(proj) == []
    assert sorted(str(p.relative_to(proj)) for p in proj.rglob("*")) == before


def test_an_unmovable_file_costs_a_warning_and_not_the_handoff(proj, capsys):
    """Migration is housekeeping; publishing is the job.

    It runs inside `main` just before the write, so an exception escaping here
    would cost the session its handoff in order to tidy up an old one.
    """
    (proj / "next-session.md").write_text("stuck", encoding="utf-8")
    real_replace = os.replace

    def refuse(src, dst):
        if "legacy" in str(dst):
            raise PermissionError(13, "denied")
        return real_replace(src, dst)

    original = ho.os.replace
    ho.os.replace = refuse
    try:
        assert ho.migrate_project_handoff(proj) == []
    finally:
        ho.os.replace = original

    assert "Warning" in capsys.readouterr().err
    assert (proj / "next-session.md").read_text(encoding="utf-8") == "stuck"


def test_a_handoff_directory_is_not_mistaken_for_a_superseded_file(proj):
    """``superseded/`` held only files, but it is user-visible state on disk.

    A directory in there would be skipped rather than moved; asserting it
    stops a future `iterdir` loop from raising `IsADirectoryError` halfway
    through and stranding the entries it had not reached yet.
    """
    old = proj / "superseded"
    (old / "a-directory").mkdir(parents=True)
    (old / "real.md").write_text("banked", encoding="utf-8")

    ho.migrate_project_handoff(proj)

    assert any("banked" in t for t in _all_text(proj))
    assert (old / "a-directory").is_dir()


def test_a_linked_superseded_directory_is_left_where_it_points(proj, tmp_path,
                                                               capsys):
    """A link is not something this tool put there, so it is not ours to empty.

    ``is_dir()`` and ``iterdir()`` both follow links. Without the check, a
    ``superseded`` pointing somewhere else would have the migration
    ``os.replace`` that directory's contents into this project's
    ``handoff/legacy/`` -- one project's migration reaching into a directory
    it was never told about and moving files out of it. The elsewhere here is
    outside ``proj`` entirely, so the assertion cannot pass by the files
    merely having been relocated within the project.
    """
    if not _can_symlink(tmp_path):
        pytest.skip("this platform will not let the test create a symlink")
    elsewhere = tmp_path / "someone-elses-archive"
    elsewhere.mkdir()
    (elsewhere / "theirs.md").write_text("not ours to move", encoding="utf-8")
    (proj / "superseded").symlink_to(elsewhere, target_is_directory=True)

    ho.migrate_project_handoff(proj)

    assert (elsewhere / "theirs.md").is_file(), (
        "the migration moved a file out of a directory it only reached "
        "through a link")
    assert "is a link" in capsys.readouterr().err
