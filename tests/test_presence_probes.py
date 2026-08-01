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
import handoff_tool as ho


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


def test_migration_survives_an_unreadable_legacy_directory(
        tmp_path, monkeypatch, capsys):
    """Not raising is necessary and not sufficient.

    ``~/.copilot`` is deleted by the CLI, so legacy state that was not moved
    because it could not be examined is state that is about to be destroyed.
    A silent skip is indistinguishable from "there was nothing to move" -- in
    the log and in the outcome both -- and saying so is the only warning
    anyone gets before it is gone.
    """
    _point_at_legacy(monkeypatch, tmp_path)
    op.LEGACY_RESTART_DIR.mkdir()
    with denied(monkeypatch, op.LEGACY_RESTART_DIR, op.LEGACY_BACKUPS_DIR,
                op.LEGACY_LOG_FILE, op.LEGACY_METRICS_DB) as seen:
        op.migrate_legacy_state()  # must not raise

    assert seen["n"], "the denial never fired; the test proves nothing"
    err = capsys.readouterr().err
    assert str(op.LEGACY_RESTART_DIR) in err, (
        f"legacy state was left behind, and about to be deleted, without "
        f"saying so: {err!r}")


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
def test_a_catalog_it_cannot_stat_still_resolves_a_registered_project(
        tmp_path, monkeypatch):
    """A denied ``stat`` is not a denied ``read``.

    The lookup gated its ``open`` on ``file_present(catalog)``, so a catalog
    sitting behind an unsearchable parent made a registered project look
    unregistered -- while ``open`` would have handed the bytes over. Measured:
    ``file_present`` returns None on the same catalog whose 61 bytes ``open``
    reads out. A clause that can only ever subtract a lookup that would have
    succeeded is not a guard. The sibling reader of this very file,
    ``handoff_tool.resolve_guid``, already spends the probe on ``is False``
    and only False, with a comment saying why; this is the twin that did not.

    The test that used to stand here denied the same catalog but looked up a
    project that was never in it, so ``None`` came back whether the denial
    fired or not. It proved the call did not raise -- a true thing about the
    call, spent as though it established the answer.
    """
    project = tmp_path / "proj"
    project.mkdir()
    catalog = tmp_path / "catalog.csv"
    catalog.write_text(f'"{project}",guid-eacces\n', encoding="utf-8")
    monkeypatch.setattr(op, "project_catalog_path", lambda: catalog)

    expected = op.project_handoff_file(project)
    assert expected is not None, \
        "the row does not resolve even when readable; the test proves nothing"

    with denied(monkeypatch, catalog) as seen:
        found = op.project_handoff_file(project)

    assert seen["n"], "the denial never fired; the test proves nothing"
    assert found == expected, (
        f"a registered project came back as {found!r} because the catalog's "
        f"stat was denied, though the read would have succeeded")


def test_a_catalog_that_will_not_open_is_not_an_unregistered_project(
        tmp_path, monkeypatch):
    """The two answers must not share a return value.

    ``restart_loop`` spends ``None`` as "this project is not registered, so no
    handoff file is expected here" -- a claim about what the catalog contains.
    A read that failed has not established it.

    The sharp part is downstream: the loop *already* refuses to call an
    unexaminable handoff file crash recovery. Laundering the catalog failure
    into ``None`` one layer up meant that deliberate handling was never
    reached, because the value arrived indistinguishable from a real answer.

    Asserted as "not None" rather than against the sentinel by name, so this
    file stays runnable unmodified against revisions that predate it -- the
    control it is also used as.
    """
    project = tmp_path / "proj"
    project.mkdir()
    catalog = tmp_path / "catalog.csv"
    catalog.write_text(f'"{project}",guid-unopenable\n', encoding="utf-8")
    monkeypatch.setattr(op, "project_catalog_path", lambda: catalog)

    import builtins
    real_open = builtins.open
    seen = {"n": 0}

    def open_denied(path, *args, **kwargs):
        if str(path) == str(catalog):
            seen["n"] += 1
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    builtins.open = open_denied
    try:
        found = op.project_handoff_file(project)
    finally:
        builtins.open = real_open

    assert builtins.open is real_open, "the real open was not restored"
    assert seen["n"], "the open was never attempted; the test proves nothing"
    assert found is not None, (
        "a catalog that could not be opened was reported as 'no entry for "
        "this project', which is a claim about its contents")


def test_a_row_that_cannot_be_compared_is_not_a_missing_entry(
        tmp_path, monkeypatch):
    """"No row matched" is only an answer if every row was compared.

    A row whose path will not resolve is skipped, which is right. Returning
    ``None`` afterwards is not: the verdict rests on rows that were never
    examined.
    """
    project = tmp_path / "proj"
    project.mkdir()
    catalog = tmp_path / "catalog.csv"
    catalog.write_text('"C:\\\\elsewhere\\\\other",guid-other\n', encoding="utf-8")
    monkeypatch.setattr(op, "project_catalog_path", lambda: catalog)

    assert op.project_handoff_file(project) is None, \
        "a readable catalog with a comparable non-matching row must say None"

    real_resolve = Path.resolve
    seen = {"n": 0}

    def resolve_denied(self, *args, **kwargs):
        if "elsewhere" in str(self):
            seen["n"] += 1
            raise PermissionError(13, "Permission denied")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_denied)
    found = op.project_handoff_file(project)
    monkeypatch.setattr(Path, "resolve", real_resolve)

    assert seen["n"], "the resolve never failed; the test proves nothing"
    assert found is not None, (
        "a catalog whose only row could not be compared was reported as "
        "'not registered'")


def test_an_uncomparable_row_does_not_shadow_a_registered_project(
        tmp_path, monkeypatch):
    """A reviewer's worry, pinned: refusing to guess must not cost a lookup.

    Treating an uncomparable row as "undecided" only governs what is said when
    *nothing* matched. A project that is genuinely registered still has to
    resolve to its own handoff file, even when a broken row sits beside it in
    the same catalog -- otherwise the caution would have bought safety by
    disabling the feature.
    """
    project = tmp_path / "proj"
    project.mkdir()
    target = str(Path(project).resolve())
    catalog = tmp_path / "catalog.csv"
    catalog.write_text(
        '"C:\\\\elsewhere\\\\broken",guid-broken\n'
        '"' + target + '",guid-real\n', encoding="utf-8")
    monkeypatch.setattr(op, "project_catalog_path", lambda: catalog)

    real_resolve = Path.resolve
    seen = {"n": 0}

    def resolve_denied(self, *args, **kwargs):
        if "elsewhere" in str(self):
            seen["n"] += 1
            raise PermissionError(13, "Permission denied")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_denied)
    found = op.project_handoff_file(project)
    monkeypatch.setattr(Path, "resolve", real_resolve)

    assert seen["n"], "the resolve never failed; the test proves nothing"
    assert isinstance(found, Path), (
        "a registered project stopped resolving because another row in the "
        "catalog could not be compared")
    assert "guid-real" in str(found), found


def test_the_loop_does_not_call_an_unreadable_catalog_unregistered(
        tmp_path, monkeypatch, capsys):
    """The claim that actually reaches the agent.

    Everything above is about a return value; this is about what the operator
    tells the session. "Project is not registered in the catalog" is a
    confident statement, and a catalog that would not open cannot support it.
    """
    project = tmp_path / "proj"
    project.mkdir()
    catalog = tmp_path / "catalog.csv"
    catalog.write_text(f'"{project}",guid-loop\n', encoding="utf-8")
    monkeypatch.setattr(op, "project_catalog_path", lambda: catalog)
    monkeypatch.chdir(project)

    seen_preambles = []

    def capture(instance, args, session_num, remain_on_exit=False, preamble=""):
        seen_preambles.append(preamble)
        instance.exit_file.write_text("0", encoding="utf-8")
        instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", capture)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)

    inst = op.Instance("denied-catalog")
    inst.save_state(1, "2026-07-27T10:00:00Z",
                    "3f2a9c1e-1111-2222-3333-444455556666")

    import builtins
    real_open = builtins.open
    seen = {"n": 0}

    def open_denied(path, *args, **kwargs):
        if str(path) == str(catalog):
            seen["n"] += 1
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    builtins.open = open_denied
    try:
        op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False)
    finally:
        builtins.open = real_open

    assert builtins.open is real_open, "the real open was not restored"
    assert seen["n"], "the catalog was never opened; the test proves nothing"
    err = capsys.readouterr().err
    assert "not registered in the catalog" not in err, (
        f"the loop told the session this project is unregistered on the "
        f"strength of a catalog it could not read: {err!r}")
    assert "crash" not in seen_preambles[0].lower(), \
        "an unreadable catalog was reported to the agent as crash recovery"


def test_an_unexaminable_log_directory_is_not_an_empty_one(
        tmp_path, monkeypatch, capsys):
    """"No Copilot logs found" is a census, and a denied stat is not one.

    Logs are the only record of usage, and the report is what tells the user
    whether any exist. Rendering a directory that could not be read as one
    that holds nothing is the failed-read-becomes-empty-population bug
    wearing a friendly message -- the same shape as ``managed_ids``, which
    refuses outright rather than returning an empty set.
    """
    logs = tmp_path / "copilot-logs"
    logs.mkdir()
    (logs / "process-1-1.log").write_text("x", encoding="utf-8")
    monkeypatch.setattr(op, "COPILOT_LOG_DIR", logs)

    with denied(monkeypatch, logs) as seen:
        code = op.manage_logs([])

    assert seen["n"], "the denial never fired; the test proves nothing"
    out = capsys.readouterr().out
    assert "No Copilot logs found" not in out, (
        f"a log directory that could not be examined was reported as holding "
        f"nothing: {out!r}")
    assert code != 0, \
        "an unexaminable log directory was reported as a successful census"


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




# ── the handoff tool asks the same questions ────────────────────
# `handoff_tool` runs once, by hand, at the end of a session, so its probes
# do not have a poll loop to survive. They have something narrower to protect:
# the words the agent just wrote. A wrong "absent" here either refuses a
# handoff that could have been written or writes it under the wrong name, and
# the session's context is gone either way.


@pytest.fixture
def handoff_env(tmp_path, monkeypatch):
    home = tmp_path / "ho_home"
    (home / ".copilot" / "projects").mkdir(parents=True)
    restart = tmp_path / "ho_operator" / "restart"
    restart.mkdir(parents=True)
    catalog = home / ".copilot" / "projects" / "catalog.csv"
    monkeypatch.setattr(ho, "CATALOG", catalog)
    monkeypatch.setattr(ho, "state_dir", lambda: restart)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    project = tmp_path / "ho_proj"
    project.mkdir()
    return {"home": home, "catalog": catalog,
            "restart": restart, "project": project}


def _refusal_name(call) -> str:
    """Run ``call`` and name how it refused, or what it returned instead.

    Written to survive being run against a revision where the refusal does not
    exist yet: a test that hard-references a new symbol fails on the reference
    rather than on the behaviour, and a file of those is a control that never
    reaches an assertion.

    ``Exception`` and not ``BaseException``: pytest steers the run with
    ``BaseException`` subclasses -- ``KeyboardInterrupt``, ``SystemExit``,
    ``Skipped`` -- and catching those turns a Ctrl-C into a string this
    function then compares for equality. The refusals being graded here are
    all ordinary exceptions.
    """
    try:
        return f"returned {call()!r}"
    except Exception as exc:  # noqa: BLE001 - the type is the finding
        return type(exc).__name__


def test_a_catalog_it_cannot_examine_is_not_a_missing_catalog(
        handoff_env, monkeypatch):
    """The stat is denied; the read is not.

    A denied parent directory makes ``is_file()`` raise, and the tool used to
    end there -- on a catalog that ``open`` would have handed over without
    complaint. "Catalog not found" would have been the wrong instruction as
    well as the wrong diagnosis: it sends the operator off to create a file
    that is already sitting there.
    """
    handoff_env["catalog"].write_text(
        f'"{handoff_env["project"].resolve()}",guid-probe\n', encoding="utf-8")
    with denied(monkeypatch, handoff_env["catalog"]) as seen:
        assert ho.resolve_guid(handoff_env["project"]) == "guid-probe"
    assert seen["n"], "the denial never fired; the test proves nothing"


def test_a_genuinely_absent_catalog_still_says_so(handoff_env, capsys):
    with pytest.raises(SystemExit):
        ho.resolve_guid(handoff_env["project"])
    assert "Catalog not found" in capsys.readouterr().err


def test_the_census_refuses_when_it_cannot_examine_the_restart_dir(
        handoff_env, monkeypatch):
    """An unreadable registry is not an empty one.

    ``managed_ids`` decides which sessions this tool is allowed to name. Read
    as empty it reports that nothing is running, which is indistinguishable
    from the truth and arrives with no warning at all.
    """
    (handoff_env["restart"] / "alpha.managed").write_text("{}", encoding="utf-8")
    with denied(monkeypatch, handoff_env["restart"]) as seen:
        outcome = _refusal_name(ho.managed_ids)
    assert seen["n"], "the denial never fired; the test proves nothing"
    assert outcome == "StateUnreadable", \
        f"a census that could not be taken came back as: {outcome}"


def test_the_census_reports_nothing_managed_when_nothing_is_there(
        handoff_env):
    """The other half of the same claim: absent really does mean empty."""
    for entry in handoff_env["restart"].iterdir():
        entry.unlink()
    handoff_env["restart"].rmdir()
    assert ho.managed_ids() == set()


def test_a_dangling_restart_symlink_is_not_an_empty_registry(
        handoff_env, tmp_path):
    """``dir_present`` follows links, and a broken one raises FileNotFoundError.

    An exception type is a claim about what the call did, not about what is on
    disk. The link's own directory entry is right there; something replaced or
    moved the registry it points at. Reading that as "no instances are
    managed" is the empty-population bug arriving through the probe that was
    supposed to prevent it.

    This is the real-filesystem version and it skips where symlinks are not
    permitted, which on Windows is most machines. The differential below
    covers the same branch everywhere; keep both, because this one is the only
    evidence that a real dangling link produces the errno the other one
    simulates.
    """
    if not _can_symlink(tmp_path):
        pytest.skip("symlink creation is not permitted here")
    for entry in handoff_env["restart"].iterdir():
        entry.unlink()
    handoff_env["restart"].rmdir()
    handoff_env["restart"].symlink_to(tmp_path / "moved_away")
    outcome = _refusal_name(ho.managed_ids)
    assert outcome == "StateUnreadable", \
        f"a registry that is present but unusable came back as: {outcome}"


def test_a_registry_that_stats_absent_but_lstats_present_is_not_empty(
        handoff_env, monkeypatch):
    """The dangling-symlink branch, on every platform.

    The whole decision rests on one differential: ``stat`` follows the link
    and reports ENOENT, ``lstat`` does not follow it and reports the entry.
    Simulating exactly that -- rather than a symlink -- runs the branch on
    Windows CI without developer mode, which is where the skip above would
    otherwise leave it ungraded on the platform whose path handling is the
    most brittle.
    """
    target = str(handoff_env["restart"])
    real_stat = os.stat

    def stat_says_gone(path, *args, **kwargs):
        try:
            key = str(Path(path))
        except TypeError:
            key = None
        if key == target:
            raise FileNotFoundError(2, "No such file or directory")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat_says_gone)
    accessor = getattr(sys.modules["pathlib"], "_NormalAccessor", None)
    if accessor is not None and hasattr(accessor, "stat"):
        monkeypatch.setattr(accessor, "stat", staticmethod(stat_says_gone))

    assert op.dir_present(handoff_env["restart"]) is False, \
        "the simulation did not reach dir_present; the test proves nothing"
    assert op.path_present(handoff_env["restart"]) is True, \
        "lstat was patched too; the differential the branch needs is gone"
    outcome = _refusal_name(ho.managed_ids)
    assert outcome == "StateUnreadable", \
        f"present-but-unusable came back as: {outcome}"


def test_a_listing_that_fails_midway_is_not_a_short_registry(
        handoff_env, monkeypatch):
    """The probe said "directory"; the listing still failed.

    Returning the names gathered so far would report a partial census as a
    complete one, which is the same lie with better manners.
    """
    real_iterdir = Path.iterdir

    def denied_iterdir(self, *args, **kwargs):
        if str(self) == str(handoff_env["restart"]):
            raise PermissionError(13, "Permission denied")
        return real_iterdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", denied_iterdir)
    outcome = _refusal_name(ho.managed_ids)
    assert outcome == "StateUnreadable", \
        f"a listing that failed came back as: {outcome}"


def test_inference_names_the_real_problem_instead_of_blaming_the_user(
        handoff_env, monkeypatch, capsys):
    """"Cannot infer instance" is true but useless when the cause is EACCES.

    The operator is told to supply a name, does, and hits the same denial
    somewhere else. The message has to say which thing could not be read.
    """
    (handoff_env["restart"] / "alpha.managed").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "list_sessions", lambda self: ["alpha"])
    monkeypatch.setattr(ho.Mux, "pane_current_path",
                        lambda self, s: str(handoff_env["project"]))
    with denied(monkeypatch, handoff_env["restart"]) as seen:
        with pytest.raises(SystemExit):
            ho.infer_instance(handoff_env["project"], ho.Mux())
    assert seen["n"], "the denial never fired; the test proves nothing"
    err = capsys.readouterr().err
    assert "which operator instances are managed" in err, \
        f"refused, but without naming what could not be read: {err!r}"


def test_a_project_root_it_cannot_examine_still_gets_its_handoff_written(
        handoff_env, monkeypatch):
    """Refusing here throws the session away for certain.

    Everything downstream of this probe fails safe: the destination comes from
    an exact catalog match, so an unusable root misses the lookup rather than
    matching the wrong row. There is no wrong file to write, and one lost
    handoff is worse than one confusing warning.
    """
    handoff_env["catalog"].write_text(
        f'"{handoff_env["project"].resolve()}",guid-root\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    with denied(monkeypatch, handoff_env["project"]) as seen:
        rc = ho.main(["--instance", "proj", "--status", "s", "--next", "n",
                      "--project-root", str(handoff_env["project"])])
    assert seen["n"], "the denial never fired; the test proves nothing"
    assert rc == 0
    written = (handoff_env["home"] / ".copilot" / "projects" / "guid-root"
               / "next-session.md")
    assert "## Status" in written.read_text(encoding="utf-8")


def test_a_genuinely_missing_project_root_is_still_refused(handoff_env, capsys):
    with pytest.raises(SystemExit):
        ho.main(["--instance", "proj", "--status", "s", "--next", "n",
                 "--project-root", str(handoff_env["project"] / "nope")])
    assert "Directory not found" in capsys.readouterr().err


def test_resolved_str_survives_a_path_that_will_not_resolve(tmp_path,
                                                            monkeypatch):
    """``Path.resolve`` is not total, and the new code reaches it.

    A symlink loop raises ``RuntimeError`` -- not ``OSError``, so nothing that
    guards for filesystem trouble catches it. It was unreachable while an
    unexaminable root was refused up front; letting that root through made it
    reachable, which is the fix for one bug opening the door to another.
    """
    real_resolve = Path.resolve

    def loops(self, *args, **kwargs):
        if str(self) == str(tmp_path):
            raise RuntimeError(f"Symlink loop from {self}")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", loops)
    with pytest.raises(RuntimeError):
        Path(tmp_path).resolve()
    answer = ho.resolved_str(tmp_path)
    assert os.path.isabs(answer), \
        f"the fallback must still yield an absolute path, got {answer!r}"


def test_a_project_root_that_will_not_resolve_reaches_a_named_refusal(
        handoff_env, monkeypatch, capsys):
    """The refusal must survive being unable to resolve the thing it names.

    Both branches that give up on the catalog interpolate the resolved root
    into the message that tells the operator how to fix it. Raising there
    replaces an actionable refusal with a traceback, at the exact moment the
    session's words are still unwritten.
    """
    handoff_env["catalog"].write_text('"/somewhere/else",other\n',
                                      encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    real_resolve = Path.resolve
    doomed = str(handoff_env["project"])

    def loops(self, *args, **kwargs):
        if str(self) == doomed:
            raise RuntimeError(f"Symlink loop from {self}")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", loops)
    with pytest.raises(SystemExit):
        ho.main(["--instance", "proj", "--status", "s", "--next", "n",
                 "--project-root", doomed])
    err = capsys.readouterr().err
    assert "No catalog entry" in err, \
        f"expected an actionable refusal, got: {err!r}"


# ── the same bug in the modules the rule had not reached ────────
# `copilot_operator` and `handoff_tool` were taught this one call at a time.
# The three below answered the same question in the same wrong way and were
# never looked at, which is what `tests/test_presence_probe_conformance.py`
# now scans for. Each test here uses a *dangling symlink* rather than a denied
# stat wherever it can, because that is the silent half of the defect: the
# probe returns False and answers confidently, where a denial at least raises.
def _skills_repo(tmp_path, monkeypatch, names=("alpha", "beta")):
    """A fake REPO_ROOT holding `skills/<name>/SKILL.md` for each name."""
    import setup_tools as st

    root = tmp_path / "repo"
    for name in names:
        (root / "skills" / name).mkdir(parents=True)
        (root / "skills" / name / "SKILL.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(st, "REPO_ROOT", root)
    return st, root


def test_a_skill_whose_manifest_is_a_dangling_link_is_still_deployed(
        tmp_path, monkeypatch):
    """One unexaminable SKILL.md must not delete that skill from the install.

    `is_file()` follows the link, finds nothing and says False, so the skill
    left the deploy set silently -- and `deployed_artifacts` reads from the
    same function, so `--status` agreed with the installer and the absence was
    invisible from either side.
    """
    if not _can_symlink(tmp_path):
        pytest.skip("no symlink privilege on this machine")
    st, root = _skills_repo(tmp_path, monkeypatch)
    manifest = root / "skills" / "beta" / "SKILL.md"
    manifest.unlink()
    manifest.symlink_to(root / "skills" / "beta" / "gone.md")

    names = [p.name for p in st._skill_sources()]

    assert "alpha" in names, "premise: the readable skill is still found"
    assert "beta" in names, (
        f"a skill whose SKILL.md could not be examined was dropped from "
        f"everything setup deploys, silently: {names}"
    )


def test_an_unlistable_skills_directory_says_so_instead_of_shipping_nothing(
        tmp_path, monkeypatch, capsys):
    """`return []` here is a claim that the repository ships no skills.

    The directory is a link whose target is gone, which is the silent half of
    the defect: `is_dir()` follows it, finds nothing, and answers False with
    no error anywhere. Setup then installed no skills and reported success.
    """
    if not _can_symlink(tmp_path):
        pytest.skip("no symlink privilege on this machine")
    import setup_tools as st

    root = tmp_path / "repo"
    root.mkdir()
    (root / "skills").symlink_to(tmp_path / "gone", target_is_directory=True)
    monkeypatch.setattr(st, "REPO_ROOT", root)

    found = st._skill_sources()

    out = capsys.readouterr().out
    assert found is None, (
        f"an unlistable skills/ answered the same as a repository that ships "
        f"none, and the caller cannot tell them apart from the return: {found!r}"
    )
    assert "could not be listed" in out, (
        f"a skills/ that could not be read was reported as a repository that "
        f"ships no skills, with nothing said about it: {out!r}"
    )


def test_an_unreadable_extensions_directory_is_not_reported_as_absent(
        tmp_path, monkeypatch, capsys):
    """The skip message is the only trace either state leaves behind.

    ``_extension_sources`` returned ``[]`` for a directory it could not read,
    so ``install_extensions`` printed "No extensions/ directory found" — a
    cause nobody measured, for a directory that was found and was unreadable.
    A wrong sentence here is worse than none, because it closes the question.
    """
    if not _can_symlink(tmp_path):
        pytest.skip("no symlink privilege on this machine")
    import setup_tools as st

    root = tmp_path / "repo"
    root.mkdir()
    (root / "extensions").symlink_to(tmp_path / "gone", target_is_directory=True)
    monkeypatch.setattr(st, "REPO_ROOT", root)

    st.install_extensions(assume_yes=True)

    out = capsys.readouterr().out
    assert "could not be read" in out, (
        f"the run said nothing about an extensions/ it could not examine: "
        f"{out!r}"
    )
    assert "absent" not in out, (
        f"an unreadable extensions/ was reported as an absent one: {out!r}"
    )
    assert "No extensions/ directory found" not in out, (
        f"the original false sentence came back: {out!r}"
    )


def test_an_absent_extensions_directory_is_still_reported_as_absent(
        tmp_path, monkeypatch, capsys):
    """Negative control for the test above.

    Without this, wording the unreadable branch so that it never says "absent"
    would pass by making *both* branches say "could not be read", which is the
    same defect with the polarity reversed.
    """
    import setup_tools as st

    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(st, "REPO_ROOT", root)

    st.install_extensions(assume_yes=True)

    out = capsys.readouterr().out
    assert "absent" in out, (
        f"a genuinely missing extensions/ did not say so: {out!r}"
    )
    assert "could not be read" not in out, (
        f"a missing extensions/ was reported as an unreadable one: {out!r}"
    )


def test_backup_path_will_not_hand_back_a_name_that_is_occupied(
        tmp_path, monkeypatch):
    """The function's first line promises never to overwrite an earlier backup.

    A backup that is there but unexaminable -- here a link whose target is
    gone -- answers False to `exists()`, so the name was reused and the file
    it destroyed was the one this function exists to preserve.
    """
    if not _can_symlink(tmp_path):
        pytest.skip("no symlink privilege on this machine")
    import backfill_unknown_metrics as bf

    db = tmp_path / "metrics.db"
    db.write_text("x", encoding="utf-8")
    taken = db.with_suffix(db.suffix + ".bak-prezero")
    taken.symlink_to(tmp_path / "gone")

    chosen = bf.backup_path(db)

    assert chosen != taken, (
        f"backup_path chose a name that is already occupied by something it "
        f"could not examine; writing there destroys it: {chosen}"
    )


def test_an_unexaminable_log_directory_is_not_an_empty_ingest(
        tmp_path, monkeypatch, capsys):
    """"No Copilot logs found" plus exit 0 is a census, and this is not one.

    `manage_logs` learned this and got a test. `ingest_all` answered the same
    question about the same directory with a bare `is_dir()` and reported a
    successful run that ingested nothing -- which is what a machine that has
    silently stopped recording metrics also looks like.
    """
    if not _can_symlink(tmp_path):
        pytest.skip("no symlink privilege on this machine")
    import operator_ingest as oi

    logs = tmp_path / "logs-link"
    logs.symlink_to(tmp_path / "gone", target_is_directory=True)
    monkeypatch.setattr(op, "COPILOT_LOG_DIR", logs)

    assert oi.ingest_all(logs, tmp_path / "m.db") is None, (
        "ingest_all reported a census of a directory it never read"
    )
    code = op.ingest_all_logs()

    out = capsys.readouterr().out
    assert "No Copilot logs found" not in out, (
        f"a log directory that could not be examined was reported as holding "
        f"nothing: {out!r}"
    )
    assert code != 0, \
        "an unexaminable log directory was reported as a successful ingest"
