"""Affiliation is recorded, and never allowed to matter to delivery.

Two things are being defended here, and they pull in opposite directions.

The first is that the metadata is *right*: a worktree resolves to its project,
an unregistered directory resolves to nothing, and "we do not know" never
renders as "same project".

The second is that it is *inert*. Every failure path below -- an unreadable
catalog, a recipient that has never started, a launch record full of junk --
must still send the message. That is the 0025 council's decision, and it is
the half that a later change is most likely to break by accident, because
nothing about a send failing looks wrong from inside the function that failed.
"""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import mail_affiliation as aff
import operator_mail
import project_paths


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@pytest.fixture()
def catalog(tmp_path, monkeypatch):
    """A project catalog with one registered checkout."""
    root = tmp_path / "projects"
    root.mkdir()
    checkout = tmp_path / "repo"
    (checkout / ".git").mkdir(parents=True)
    (root / "catalog.csv").write_text(
        f'"{project_paths.resolved_str(checkout)}","guid-alpha"\n',
        encoding="utf-8")
    monkeypatch.setattr(project_paths, "projects_root", lambda: root)
    return checkout


def test_a_registered_checkout_resolves_to_its_project(catalog):
    found = aff.describe_path(catalog)
    assert found.project == "guid-alpha"
    assert found.status == "known"
    assert found.cwd == str(catalog)


def test_a_worktree_resolves_to_the_project_not_the_worktree(tmp_path,
                                                             monkeypatch):
    """Every agent in this repository works in `.worktrees/<branch>`, and a
    worktree path never matches a catalog row. Without resolving to the
    primary checkout first, the convention the toolkit *requires* would file
    every one of its own agents as unaffiliated.

    A real repository and a real `git worktree add`, because that is the one
    thing a fake `.git` cannot stand in for: `primary_repo_root` shells out to
    git, and against a directory that only looks like a checkout it returns
    the path unchanged -- which is indistinguishable from the bug.
    """
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    checkout = tmp_path / "repo"
    checkout.mkdir()
    run = functools.partial(subprocess.run, cwd=str(checkout),
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    run(["git", "init", "-q", "-b", "main"])
    run(["git", "config", "user.email", "t@example.invalid"])
    run(["git", "config", "user.name", "t"])
    (checkout / "f.txt").write_text("x", encoding="utf-8")
    run(["git", "add", "f.txt"])
    run(["git", "commit", "-qm", "init"])
    made = run(["git", "worktree", "add", "-q", "-b", "feat-x",
                str(tmp_path / "tree")])
    assert made.returncode == 0, made.stderr

    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "catalog.csv").write_text(
        f'"{project_paths.resolved_str(checkout)}","guid-alpha"\n',
        encoding="utf-8")
    monkeypatch.setattr(project_paths, "projects_root", lambda: projects)

    found = aff.describe_path(tmp_path / "tree")
    assert found.project == "guid-alpha", found


def test_an_unregistered_checkout_has_no_project_and_says_why(catalog,
                                                              tmp_path):
    """Positive control for the two above: the lookup must be able to fail,
    or 'known' means nothing."""
    other = tmp_path / "elsewhere"
    (other / ".git").mkdir(parents=True)
    found = aff.describe_path(other)
    assert found.project == ""
    assert found.status == project_paths.CATALOG_NO_ENTRY
    assert found.status != "known"


def test_an_unreadable_catalog_is_not_reported_as_unregistered(catalog,
                                                               monkeypatch):
    """The two call for opposite actions -- add a line, versus fix a
    permission -- and one instruction given for the other situation is worse
    than none."""
    def boom(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(project_paths, "catalog_guid",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            OSError("permission denied")))
    found = aff.describe_path(catalog)
    assert found.status == project_paths.CATALOG_UNREADABLE
    assert found.project == ""


def test_an_instance_with_no_launch_record_is_not_the_same_as_unreadable(
        tmp_path):
    restart = tmp_path / "restart"
    restart.mkdir()
    assert aff.describe_instance("ghost", restart).status == aff.NO_LAUNCH_RECORD


def test_an_instance_resolves_through_its_launch_record(catalog, tmp_path):
    restart = tmp_path / "restart"
    restart.mkdir()
    (restart / "beta.launch.json").write_text(
        json.dumps({"cwd": str(catalog)}), encoding="utf-8")
    found = aff.describe_instance("beta", restart)
    assert found.project == "guid-alpha", found


def test_a_corrupt_launch_record_does_not_raise(tmp_path):
    """This runs inside a send. Anything that raises here would turn a
    malformed state file into an undeliverable message."""
    restart = tmp_path / "restart"
    restart.mkdir()
    (restart / "beta.launch.json").write_text("{not json", encoding="utf-8")
    assert aff.describe_instance("beta", restart).status == aff.NO_LAUNCH_RECORD


# --------------------------------------------------------------------------
# The tri-state
# --------------------------------------------------------------------------

def test_two_known_and_equal_projects_are_the_same_project():
    a = aff.Affiliation(project="g1", status="known")
    b = aff.Affiliation(project="g1", status="known")
    assert aff.relationship(a, b) == aff.SAME_PROJECT


def test_two_known_and_different_projects_are_cross_project():
    a = aff.Affiliation(project="g1", status="known")
    b = aff.Affiliation(project="g2", status="known")
    assert aff.relationship(a, b) == aff.CROSS_PROJECT


@pytest.mark.parametrize("known_side", ["origin", "destination"])
def test_one_known_endpoint_is_not_enough_to_claim_anything(known_side):
    """The single most important line in this module.

    One known endpoint tells you nothing about the *relationship*, and the
    tempting shortcut -- 'we know the sender's project and not the
    recipient's, so assume local' -- is how an unknown becomes a reassuring
    lie. It must be `and`, not `or`.
    """
    known = aff.Affiliation(project="g1", status="known")
    unknown = aff.Affiliation(status=aff.PROJECT_UNKNOWN)
    pair = ((known, unknown) if known_side == "origin"
            else (unknown, known))
    assert aff.relationship(*pair) == aff.PROJECT_UNKNOWN


def test_a_message_written_before_any_of_this_is_unknown_not_same():
    """The 286 messages already on this machine carry no endpoints. They are
    unknowable, and must never render as same-project."""
    legacy = {"from": "a", "to": "b", "text": "hi",
              "sent_at": "2026-08-05T14:26:06Z"}
    assert aff.relationship_label(legacy) == aff.PROJECT_UNKNOWN


def test_endpoints_survive_a_round_trip_through_json():
    msg = {}
    aff.attach(msg,
               aff.Affiliation(cwd="C:/a", project="g1", status="known"),
               aff.Affiliation(cwd="C:/b", project="g2", status="known"))
    reread = json.loads(json.dumps(msg))
    assert aff.relationship_label(reread) == aff.CROSS_PROJECT


def test_a_malformed_endpoint_is_unknown_rather_than_an_exception():
    for junk in (None, [], "same-project", 7, {"project_id": None}):
        assert aff.relationship_label(
            {aff.ORIGIN_KEY: junk, aff.DESTINATION_KEY: junk}
        ) == aff.PROJECT_UNKNOWN


def test_attaching_endpoints_does_not_disturb_the_existing_record():
    """Additive only: a reader that has never heard of affiliation must be
    unaffected."""
    msg = operator_mail.new_message("a", "b", "b-id", "hello")
    before = dict(msg)
    aff.attach(msg, aff.Affiliation(), aff.Affiliation())
    for key, value in before.items():
        assert msg[key] == value


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _msg(origin_guid, destination_guid, **extra):
    msg = operator_mail.new_message("scripts", "copilot-tools", "ct", "hello")
    msg.update(extra)
    aff.attach(
        msg,
        aff.Affiliation(project=origin_guid, status="known" if origin_guid
                        else aff.PROJECT_UNKNOWN),
        aff.Affiliation(project=destination_guid,
                        status="known" if destination_guid
                        else aff.PROJECT_UNKNOWN))
    return msg


def test_the_delivered_line_carries_the_send_time():
    line = operator_mail.render_line(_msg("g1", "g1"))
    assert "at 20" in line, line


def test_the_delivered_line_never_computes_an_age():
    """The council's modification, and the reason for it.

    An age is computed once, at delivery, and then frozen into the text -- so
    a line that says "just now" and is not read for two days goes on saying
    "just now". It would be at its most wrong in exactly the case it exists
    to catch. The absolute stamp cannot rot.
    """
    line = operator_mail.render_line(
        _msg("g1", "g1", sent_at="2020-01-01T00:00:00Z"))
    assert "2020-01-01T00:00:00Z" in line
    for age_word in ("ago", "just now", "minute", "hour", "day"):
        assert age_word not in line.lower(), f"{age_word!r} in {line!r}"


def test_a_message_with_no_timestamp_says_so():
    line = operator_mail.render_line(_msg("g1", "g1", sent_at=""))
    assert "sent time unknown" in line


def test_a_cross_project_message_is_marked_before_its_body():
    line = operator_mail.render_line(_msg("g1", "g2"))
    marker = line.index("cross-project")
    assert marker < line.index("hello"), line


def test_a_same_project_message_is_not_marked():
    """Positive control. A label on every message is a label nobody reads."""
    assert "cross-project" not in operator_mail.render_line(_msg("g1", "g1"))


def test_an_unknown_affiliation_is_not_marked_as_cross_project():
    """It is also not marked as same-project: it simply makes no claim. The
    tri-state is still reported by `relationship_label`, which is what the
    conversation log buckets on."""
    line = operator_mail.render_line(_msg("", ""))
    assert "cross-project" not in line
    assert "same-project" not in line


def test_the_queued_preamble_marks_cross_project_too():
    block = operator_mail.render_for_agent([_msg("g1", "g2")])
    assert "cross-project" in block


def test_the_queued_preamble_does_not_mark_same_project():
    block = operator_mail.render_for_agent([_msg("g1", "g1")])
    assert "cross-project" not in block


def test_the_delivered_line_is_pure_ascii():
    """It is typed into another terminal. A middle dot came back as a
    replacement character on a cp1252 console during end-to-end checking,
    which is a message the recipient reads as corruption rather than as a
    label. The truncation marker on this same line has the same exposure and
    predates this -- which is the argument for not adding a second one, not
    for adding one."""
    line = operator_mail.render_line(_msg("g1", "g2"))
    assert "cross-project" in line
    assert line.isascii(), [c for c in line if not c.isascii()]


def test_the_ascii_check_can_fail():
    """Positive control: `isascii()` on a string that is ASCII by luck proves
    nothing about the assertion above."""
    assert not "a \u00b7 b".isascii()
