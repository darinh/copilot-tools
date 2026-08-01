"""A path that cannot be examined is not a path that is absent.

`Path.exists` answers a three-state question with two values and raises on the
third. In this program that mattered three ways:

* the supervisor's poll loop catches only ``KeyboardInterrupt``, so one denied
  stat on a marker file ended the unattended restarts it exists to provide;
* ``migrate_legacy_state`` moves onto a destination it believes is absent;
* the tab registry is rewritten whole from what was read, so reading it as
  empty erases every other tab.

The mock lives in ``conftest.denied`` and is shared with the mail-CLI census
tests. It denies ``os.stat``, ``os.lstat`` and -- on 3.10 -- the private
``pathlib`` accessor that binds ``os.stat`` at import time. All three, because
the old code and the new code reach the filesystem by different routes:
``Path.exists()`` goes through ``os.stat`` (via that accessor on 3.10), while
``path_present`` calls ``os.lstat`` directly. Deny only ``os.lstat`` and the
unfixed code sails through, so the test grades nothing; deny only ``os.stat``
and the fixed code never sees the failure it exists to handle.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from conftest import denied

import copilot_operator as op


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    restart = tmp_path / "restart"
    restart.mkdir(parents=True)
    monkeypatch.setattr(op, "OPERATOR_HOME", tmp_path)
    monkeypatch.setattr(op, "RESTART_DIR", restart)
    monkeypatch.setattr(op, "LOG_FILE", tmp_path / "operator.log")
    monkeypatch.setattr(op, "METRICS_DB", tmp_path / "metrics.db")
    monkeypatch.setattr(op, "COPILOT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(op, "TABS_FILE", tmp_path / "tabs.json")
    monkeypatch.setattr(op, "POLL_INTERVAL", 0)
    monkeypatch.setattr(op, "LAUNCH_BACKOFF_BASE", 0)
    monkeypatch.setattr(op, "RESTART_PAUSE_SECONDS", 0)
    _reset_probe_warnings()
    yield tmp_path
    _reset_probe_warnings()


def _reset_probe_warnings() -> None:
    """Clear the warn-once memo, tolerating its absence.

    This file doubles as a negative control: it is run unmodified against
    older revisions to prove the counterpart tests fail there. A fixture that
    hard-references a symbol the fix introduced turns every test into an
    identical setup error, which looks like a devastating control from a
    distance and in fact never reaches a single assertion.
    """
    getattr(op, "_PROBE_WARNED", {}).clear()


def _can_symlink(tmp_path: Path) -> bool:
    try:
        (tmp_path / "_lnk").symlink_to(tmp_path / "_nothing")
    except (OSError, NotImplementedError):
        return False
    (tmp_path / "_lnk").unlink()
    return True


# ── the probes themselves ───────────────────────────────────────
def test_probes_answer_cannot_tell_rather_than_absent(tmp_path, monkeypatch):
    target = tmp_path / "thing"
    target.write_text("x", encoding="utf-8")
    with denied(monkeypatch, target):
        assert op.path_present(target) is None
        assert op.dir_present(target) is None
        assert op.file_present(target) is None


def test_probes_still_report_a_genuinely_absent_path(tmp_path):
    missing = tmp_path / "nope"
    assert op.path_present(missing) is False
    assert op.dir_present(missing) is False
    assert op.file_present(missing) is False


def test_the_denial_reaches_the_call_the_old_code_made(tmp_path, monkeypatch):
    """Grade the mock, not just the fix.

    Every test below asserts the new probes cope. That is only evidence if the
    denial would really have reached ``Path.exists``, which is what the code
    used to call -- including on 3.10, where reaching it takes an extra patch.
    """
    target = tmp_path / "thing"
    target.write_text("x", encoding="utf-8")
    with denied(monkeypatch, target):
        with pytest.raises(PermissionError):
            target.exists()


def test_marker_set_reports_unset_and_warns_only_once(tmp_path, monkeypatch):
    marker = tmp_path / "restart" / "inst.restart"
    with denied(monkeypatch, marker):
        assert op.marker_set(marker) is False
        assert op.marker_set(marker) is False
        assert op.marker_set(marker) is False
    logged = op.LOG_FILE.read_text(encoding="utf-8")
    assert logged.count("Could not examine") == 1, \
        "a permanently unreadable marker must not log once per poll"


def test_marker_set_warns_again_after_recovering(tmp_path, monkeypatch):
    marker = tmp_path / "restart" / "inst.restart"
    with denied(monkeypatch, marker, limit=1):
        assert op.marker_set(marker) is False
        marker.touch()
        assert op.marker_set(marker) is True
    with denied(monkeypatch, marker):
        assert op.marker_set(marker) is False
    assert op.LOG_FILE.read_text(encoding="utf-8").count("Could not examine") == 2


# ── the supervisor must survive a blinded probe ─────────────────
def test_poll_loop_survives_a_marker_it_cannot_examine(monkeypatch):
    """The whole point of loop mode is that it keeps going.

    A denied stat used to propagate out of ``run_loop_mode`` — past the only
    handler, which catches ``KeyboardInterrupt`` — and the agent stayed dead
    until a human noticed.
    """
    inst = op.Instance("blinded")
    attempts = {"n": 0}

    def capture(instance, args, session_num, remain_on_exit=False, preamble=""):
        attempts["n"] += 1
        instance.exit_file.write_text("0", encoding="utf-8")
        instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", capture)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)

    with denied(monkeypatch, inst.stop_marker, inst.restart_marker,
                inst.detach_marker, limit=3) as seen:
        rc = op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)

    assert seen["n"] == 3, "the denial never fired; the test proves nothing"
    assert rc == 0
    assert attempts["n"] == 1, \
        "a blinded pass must wait, not relaunch; the next pass sees the stop"


def test_a_stopped_session_is_not_resurrected_by_an_unreadable_marker(monkeypatch):
    """The marker is set. The supervisor just cannot read it.

    ``operator stop`` writes the marker and takes the session down. If the
    supervisor reads "cannot examine" as "nobody asked me to stop", it finds a
    dead session, calls it a crash, and starts the agent the human just
    stopped -- an unattended process resurrecting itself. It must run out of
    patience and leave the session alone instead.
    """
    inst = op.Instance("stopped")
    attempts = {"n": 0}

    def capture(instance, args, session_num, remain_on_exit=False, preamble=""):
        attempts["n"] += 1
        instance.exit_file.write_text("0", encoding="utf-8")
        instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", capture)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)

    with denied(monkeypatch, inst.stop_marker, inst.detach_marker) as seen:
        rc = op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)

    assert seen["n"] > 0, "the denial never fired; the test proves nothing"
    assert attempts["n"] == 1, "the stopped session was relaunched"
    assert rc == 1
    assert inst.stop_marker.exists(), \
        "a marker it could not read must not be consumed"


def test_is_copilot_running_does_not_end_a_session_on_a_denied_probe(monkeypatch):
    """An unreadable exit marker says nothing about whether Copilot is alive.

    Reading it as "exited" would tear down and relaunch a healthy session.
    """
    inst = op.Instance("alive")

    class FakeMux:
        def has_session(self, name):
            return True

        def pane_dead(self, name):
            return False

    monkeypatch.setattr(op, "MUX", FakeMux())
    with denied(monkeypatch, inst.exit_file):
        assert op.is_copilot_running(inst) is True


def test_exit_marker_still_ends_the_session_when_it_is_readable(monkeypatch):
    inst = op.Instance("done")
    inst.exit_file.write_text("0", encoding="utf-8")

    class FakeMux:
        def has_session(self, name):
            return True

        def pane_dead(self, name):
            return False

    monkeypatch.setattr(op, "MUX", FakeMux())
    assert op.is_copilot_running(inst) is False


# ── destructive gate: the legacy migration ──────────────────────
def _point_at_legacy(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    monkeypatch.setattr(op, "LEGACY_RESTART_DIR", legacy / "restart")
    monkeypatch.setattr(op, "LEGACY_LOG_FILE", legacy / "operator.log")
    monkeypatch.setattr(op, "LEGACY_METRICS_DB", legacy / "metrics.db")
    monkeypatch.setattr(op, "LEGACY_BACKUPS_DIR", legacy / "backups")
    monkeypatch.setattr(op, "BACKUPS_DIR", tmp_path / "backups")
    return legacy, tmp_path


def test_migration_will_not_move_onto_a_destination_it_cannot_examine(
        tmp_path, monkeypatch):
    """``shutil.move`` replaces a file. The destination has to be *known* absent."""
    legacy, _ = _point_at_legacy(monkeypatch, tmp_path)
    op.LEGACY_METRICS_DB.write_text("legacy metrics", encoding="utf-8")
    op.METRICS_DB.write_text("the real metrics", encoding="utf-8")

    with denied(monkeypatch, op.METRICS_DB):
        op.migrate_legacy_state()

    assert op.METRICS_DB.read_text(encoding="utf-8") == "the real metrics"
    assert op.LEGACY_METRICS_DB.exists(), "the legacy copy must survive too"


def test_migration_still_moves_when_the_destination_is_really_absent(
        tmp_path, monkeypatch):
    _point_at_legacy(monkeypatch, tmp_path)
    op.LEGACY_METRICS_DB.write_text("legacy metrics", encoding="utf-8")

    op.migrate_legacy_state()

    assert op.METRICS_DB.read_text(encoding="utf-8") == "legacy metrics"


def test_migration_will_not_move_onto_a_dangling_symlink(tmp_path, monkeypatch):
    """``exists`` follows the link, finds nothing, and calls it absent.

    The move then lands wherever the link pointed, or destroys the link.
    """
    if not _can_symlink(tmp_path):
        pytest.skip("this platform/user cannot create symlinks")
    _point_at_legacy(monkeypatch, tmp_path)
    op.LEGACY_METRICS_DB.write_text("legacy metrics", encoding="utf-8")
    op.METRICS_DB.symlink_to(tmp_path / "somewhere-else.db")

    op.migrate_legacy_state()

    assert op.METRICS_DB.is_symlink(), "the link should have been left alone"
    assert not (tmp_path / "somewhere-else.db").exists()
    assert op.LEGACY_METRICS_DB.exists()


def test_a_second_operator_does_not_migrate_at_the_same_time(tmp_path,
                                                            monkeypatch):
    """Probing a destination and moving onto it are two syscalls.

    A tri-state probe fixes the first one; it does nothing about the answer
    going stale before the move. On a box running a loop per project the
    operators start together, so the migration takes an exclusive lock and
    the loser finds the work already done -- which is the right outcome for a
    one-time move.
    """
    _point_at_legacy(monkeypatch, tmp_path)
    op.LEGACY_METRICS_DB.write_text("legacy metrics", encoding="utf-8")
    lock = op.OPERATOR_HOME / "migrate.lock"
    lock.write_text(str(os.getpid()), encoding="utf-8")

    op.migrate_legacy_state()

    assert not op.METRICS_DB.exists(), "migrated while another holder had the lock"
    assert op.LEGACY_METRICS_DB.exists()
    lock.unlink()

    op.migrate_legacy_state()
    assert op.METRICS_DB.read_text(encoding="utf-8") == "legacy metrics"
    assert not lock.exists(), "the lock must not outlive the migration"


def test_a_migration_that_fails_says_so(tmp_path, monkeypatch, capsys):
    """A silent failure here loses the user's metrics database with no line
    in the log to say it happened."""
    _point_at_legacy(monkeypatch, tmp_path)
    op.LEGACY_METRICS_DB.write_text("legacy metrics", encoding="utf-8")

    def refuse(src, dest):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(op.shutil, "move", refuse)
    op.migrate_legacy_state()

    assert "Could not migrate" in capsys.readouterr().err


def test_a_lock_being_taken_is_not_mistaken_for_a_stale_one(tmp_path):
    """``os.open`` creates the lock file empty and the pid lands a moment
    later. Reading that empty file as "no owner, therefore stale" hands the
    same lock to two processes -- which is the one thing a lock exists to
    stop."""
    lock = tmp_path / "some.lock"
    lock.write_text("", encoding="utf-8")

    with op._exclusive_lock(lock) as acquired:
        assert acquired is False
    assert lock.exists(), "another process's half-written lock was deleted"


def test_a_lock_whose_owner_is_gone_is_still_reclaimed(tmp_path):
    """The counterpart: refusing everything unparseable must not cost us the
    stale-holder recovery that made the lock usable after a crash."""
    lock = tmp_path / "some.lock"
    lock.write_text("999999999", encoding="utf-8")

    with op._exclusive_lock(lock) as acquired:
        assert acquired is True
        assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())
    assert not lock.exists()


def test_a_lock_that_cannot_be_read_is_treated_as_held(tmp_path, monkeypatch):
    lock = tmp_path / "some.lock"
    lock.write_text("999999999", encoding="utf-8")
    real_read_text = Path.read_text

    def unreadable(self, *args, **kwargs):
        if str(self) == str(lock):
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    try:
        with op._exclusive_lock(lock) as acquired:
            assert acquired is False
    finally:
        monkeypatch.setattr(Path, "read_text", real_read_text)
    assert lock.exists()


def test_migration_survives_an_unreadable_legacy_directory(tmp_path, monkeypatch):
    _point_at_legacy(monkeypatch, tmp_path)
    op.LEGACY_RESTART_DIR.mkdir()
    with denied(monkeypatch, op.LEGACY_RESTART_DIR, op.LEGACY_BACKUPS_DIR,
                op.LEGACY_LOG_FILE, op.LEGACY_METRICS_DB):
        op.migrate_legacy_state()  # must not raise


# ── destructive gate: the tab registry ──────────────────────────
def test_register_tab_refuses_to_rewrite_a_registry_it_could_not_read(
        tmp_path, monkeypatch):
    """The registry is written whole. Reading it as empty erases every tab."""
    registry = {"other": {"display_name": "other", "cwd": "/somewhere",
                          "argv": ["--loop"], "wsl_distro": "",
                          "updated_at": "2026-07-27T10:00:00Z"}}
    op.TABS_FILE.write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setenv("WT_SESSION", "test-tab")

    real_read_text = Path.read_text

    def unreadable(self, *args, **kwargs):
        if str(self) == str(op.TABS_FILE):
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    op.register_tab(op.Instance("newcomer"), True, ["--agent", "a"], tmp_path)
    monkeypatch.setattr(Path, "read_text", real_read_text)

    assert json.loads(op.TABS_FILE.read_text(encoding="utf-8")) == registry


def test_register_tab_still_records_when_the_registry_reads_fine(
        tmp_path, monkeypatch):
    op.TABS_FILE.write_text(json.dumps({"other": {"cwd": "/somewhere"}}),
                            encoding="utf-8")
    monkeypatch.setenv("WT_SESSION", "test-tab")

    op.register_tab(op.Instance("newcomer"), True, ["--agent", "a"], tmp_path)

    entries = json.loads(op.TABS_FILE.read_text(encoding="utf-8"))
    assert set(entries) == {"other", "newcomer"}


def test_read_tabs_separates_absent_from_unreadable(tmp_path, monkeypatch):
    assert op.read_tabs() == {}
    op.TABS_FILE.write_text("{ not json", encoding="utf-8")
    assert op.read_tabs() is None
    assert op.load_tabs() == {}


# ── ownership and state ─────────────────────────────────────────
def test_ownership_refuses_when_the_claim_cannot_be_examined(monkeypatch):
    """Ownership authorizes killing a session. Anything unproven must refuse."""
    inst = op.Instance("claimed")
    inst.claim("tok")
    with denied(monkeypatch, inst.managed_file):
        assert inst.ownership() is None
        assert inst.owns_live_session() is False


def test_ownership_refuses_a_claim_that_is_there_but_unreadable(tmp_path, monkeypatch):
    """Present is not the same as proven.

    A dangling symlink answers ``lstat`` happily and then fails the read. The
    old handler turned that failed read into a tokenless ownership dict, which
    ``owns_live_session`` treats as this operator's claim -- authority to kill
    a session, granted by a claim nobody managed to read.
    """
    inst = op.Instance("ghostclaim")
    if not _can_symlink(tmp_path):
        pytest.skip("symlink creation is not permitted here")
    inst.managed_file.symlink_to(tmp_path / "nothing-here")
    assert op.path_present(inst.managed_file) is True, \
        "the premise: the marker reads as present"
    assert inst.ownership() is None
    assert inst.owns_live_session() is False


def test_ownership_still_honours_a_legacy_marker_with_no_token():
    """The counterpart: a readable marker that simply predates tokens is a
    real claim and must keep working."""
    inst = op.Instance("legacyclaim")
    inst.managed_file.write_text("not json", encoding="utf-8")
    assert inst.ownership() == {"token": None, "display_name": inst.display_name}


def test_is_managed_counts_state_it_cannot_examine_as_present(monkeypatch):
    """Continuity, not authority: reporting "no such instance" for state that
    is really there is the misleading answer, and nothing destructive trusts
    this."""
    inst = op.Instance("shy")
    inst.claim("tok")
    with denied(monkeypatch, inst.managed_file, inst.state_file):
        assert inst.is_managed() is True


def test_is_managed_is_still_false_for_an_instance_with_no_state():
    assert op.Instance("ghost").is_managed() is False


def test_load_state_survives_an_unreadable_state_file(monkeypatch):
    inst = op.Instance("stateful")
    inst.save_state(3, "2026-07-27T10:00:00Z")
    with denied(monkeypatch, inst.state_file):
        assert inst.load_state() == {"SESSION_NUM": "3",
                                     "RUN_STARTED": "2026-07-27T10:00:00Z"}


def test_managed_instances_survives_an_unreadable_state_directory(monkeypatch):
    with denied(monkeypatch, op.RESTART_DIR):
        assert op.managed_instances() == {}


# ── catalog and launch spec ─────────────────────────────────────
def test_handoff_lookup_survives_an_unexaminable_catalog(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.csv"
    catalog.write_text('"/somewhere",abc\n', encoding="utf-8")
    monkeypatch.setattr(op, "project_catalog_path", lambda: catalog)
    with denied(monkeypatch, catalog):
        assert op.project_handoff_file(tmp_path) is None


def test_reload_refuses_to_rewrite_a_spec_it_could_not_read(tmp_path, monkeypatch):
    """``reload`` writes the spec back from what it read. A failed read that
    became ``{}`` would leave an instance that launches nothing."""
    inst = op.Instance("reloadable")
    original = json.dumps({"argv": ["--agent", "a"], "cwd": str(tmp_path)})
    inst.spec_file.write_text(original, encoding="utf-8")

    real_read_text = Path.read_text

    def unreadable(self, *args, **kwargs):
        if str(self) == str(inst.spec_file):
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(SystemExit):
        op.reload_instance("reloadable")
    monkeypatch.setattr(Path, "read_text", real_read_text)

    assert inst.spec_file.read_text(encoding="utf-8") == original


def test_reload_rejects_a_spec_that_is_not_an_object(tmp_path):
    inst = op.Instance("listy")
    inst.spec_file.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(SystemExit):
        op.reload_instance("listy")


# ── metrics: sqlite creates what it cannot find ─────────────────
def test_report_refuses_a_metrics_database_it_cannot_examine(capsys, monkeypatch):
    """``sqlite3.connect`` creates the file when it is missing.

    So "cannot tell" must not fall through to a connect: through a dangling
    symlink that writes a fresh empty database at the link target, and every
    query then fails against it.
    """
    with denied(monkeypatch, op.METRICS_DB):
        assert op.report_metrics("summary") == 1
    assert not op.METRICS_DB.exists()
    assert "Could not examine" in capsys.readouterr().err


def test_report_treats_a_dangling_metrics_symlink_as_no_database(
        tmp_path, capsys):
    if not _can_symlink(tmp_path):
        pytest.skip("symlink creation is not permitted here")
    target = tmp_path / "elsewhere.db"
    op.METRICS_DB.symlink_to(target)
    assert op.report_metrics("summary") == 1
    assert not target.exists(), "sqlite created a database at the link target"


def test_report_reports_a_corrupt_database_instead_of_crashing(capsys):
    op.METRICS_DB.write_text("this is not a database", encoding="utf-8")
    assert op.report_metrics("summary") == 1
    assert "Could not read" in capsys.readouterr().err


# ── deleting is a probe too ─────────────────────────────────────
def test_cleanup_survives_a_file_it_cannot_delete(monkeypatch):
    """``missing_ok=True`` forgives absence, not EACCES.

    Refusing an action gracefully and then crashing in the cleanup that
    follows is the same failure wearing a different hat -- and it lands on
    the supervisor's shutdown path, which only runs when something already
    went wrong.
    """
    inst = op.Instance("sticky")
    inst.claim("tok")
    inst.stop_marker.touch()
    real_unlink = Path.unlink

    def denied_unlink(self, *args, **kwargs):
        if str(self) == str(inst.stop_marker):
            raise PermissionError(13, "Permission denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied_unlink)
    assert op.remove_file(inst.stop_marker) is False
    inst.cleanup_files()
    monkeypatch.setattr(Path, "unlink", real_unlink)

    assert not inst.managed_file.exists(), \
        "one undeletable file must not stop the rest of the cleanup"


def test_remove_file_reports_success_for_an_absent_path():
    inst = op.Instance("clean")
    assert op.remove_file(inst.stop_marker) is True


