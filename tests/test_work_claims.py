"""The claim store: one item, one owner, and no write that outruns its check.

The store's whole job is to be the thing two agents cannot both win, so the
tests that matter here are the refusals -- and, just as much, the *resume*,
because an agent coming back from a restart that had to release before it
could re-claim would leave its own item free in the gap.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import work_claims as wc  # noqa: E402


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = wc.db_path(tmp_path)
    wc.init_db(path)
    return path


def _claim(store: Path, item="0007", instance="alpha", **kw):
    return wc.claim(store, item=item, instance=instance, **kw)


# ── shape ───────────────────────────────────────────────────────
def test_db_path_sits_in_the_project_directory(tmp_path: Path) -> None:
    assert wc.db_path(tmp_path) == tmp_path / wc.DB_NAME
    assert wc.db_path(tmp_path).parent == tmp_path


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    path = wc.db_path(tmp_path)
    wc.init_db(path)
    _claim(path)
    wc.init_db(path)
    assert [c.item for c in wc.claims(path)] == ["0007"]


def test_the_item_is_the_primary_key(store: Path) -> None:
    """Keyed by the work item, not by the agent.

    The predecessor keyed on ``agent_id UNIQUE``, which cannot answer "who
    holds this item" -- the question a reclaim asks -- without a scan.
    """
    with sqlite3.connect(store) as conn:
        conn.row_factory = sqlite3.Row
        cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(work_claims)")}
    assert cols["item"]["pk"] == 1
    assert cols["instance"]["pk"] == 0
    with sqlite3.connect(store) as conn:
        indexes = list(conn.execute("PRAGMA index_list(work_claims)"))
    unique_cols = set()
    with sqlite3.connect(store) as conn:
        for row in indexes:
            name = row[1]
            if not row[2]:
                continue
            for info in conn.execute(f"PRAGMA index_info('{name}')"):
                unique_cols.add(info[2])
    assert "instance" in unique_cols, (
        "spec D6: one work item per agent, so the owner column is UNIQUE")


# ── claiming ────────────────────────────────────────────────────
def test_a_claim_records_the_owners_runtime_identity(store: Path) -> None:
    """Without these four the only judgement left is the heartbeat, and the
    spec refuses to act on that alone."""
    claim = _claim(store, boot_id="uuid:abc", mux_session="alpha", pid=4242,
                   pid_start="linux:99", worktree="/w/t", branch="feat/x",
                   subproject="sub")
    stored = wc.claim_for_item(store, "0007")
    assert stored == claim
    assert (stored.boot_id, stored.mux_session, stored.pid, stored.pid_start) \
        == ("uuid:abc", "alpha", 4242, "linux:99")
    assert (stored.worktree, stored.branch, stored.subproject) \
        == ("/w/t", "feat/x", "sub")
    assert stored.claimed_at and stored.heartbeat_at


def test_a_second_instance_cannot_take_a_held_item(store: Path) -> None:
    _claim(store)
    with pytest.raises(wc.ClaimRefused) as caught:
        _claim(store, instance="beta")
    assert caught.value.reason == wc.ITEM_HELD
    assert caught.value.holder.instance == "alpha"
    assert wc.claim_for_item(store, "0007").instance == "alpha"


def test_an_instance_holding_one_item_cannot_take_another(store: Path) -> None:
    """Spec D6. Refused with its own reason: no liveness check will ever make
    this one succeed, so it must not read like a contended item."""
    _claim(store)
    with pytest.raises(wc.ClaimRefused) as caught:
        _claim(store, item="0008")
    assert caught.value.reason == wc.INSTANCE_BUSY
    assert caught.value.holder.item == "0007"
    assert wc.claim_for_item(store, "0008") is None


def test_the_two_refusals_are_told_apart_by_reason(store: Path) -> None:
    """A caller handed a bare failure has to guess which happened, and the two
    call for opposite next moves."""
    assert wc.ITEM_HELD != wc.INSTANCE_BUSY
    _claim(store)
    with pytest.raises(wc.ClaimRefused) as held:
        _claim(store, instance="beta")
    with pytest.raises(wc.ClaimRefused) as busy:
        _claim(store, item="0008")
    assert "held by 'alpha'" in str(held.value)
    assert "already holds '0007'" in str(busy.value)


def test_reclaiming_your_own_item_is_a_resume_not_a_conflict(store: Path) -> None:
    """A restarted agent has a new pid and often a new mux session. Making it
    release first would leave its own item free in the gap."""
    first = _claim(store, pid=1, pid_start="linux:1", mux_session="alpha",
                   now="2026-01-01T00:00:00Z")
    again = _claim(store, pid=2, pid_start="linux:2", mux_session="alpha-2",
                   now="2026-01-01T01:00:00Z")
    assert again.pid == 2 and again.pid_start == "linux:2"
    assert again.mux_session == "alpha-2"
    assert again.heartbeat_at == "2026-01-01T01:00:00Z"
    assert again.claimed_at == first.claimed_at, (
        "a resume keeps the original claim time; only the heartbeat moves")
    assert len(wc.claims(store)) == 1


# ── heartbeat, release, reassign ────────────────────────────────
def test_heartbeat_moves_only_for_the_owner(store: Path) -> None:
    _claim(store, now="2026-01-01T00:00:00Z")
    assert wc.heartbeat(store, item="0007", instance="beta",
                        now="2026-01-01T02:00:00Z") is False
    assert wc.claim_for_item(store, "0007").heartbeat_at == "2026-01-01T00:00:00Z"
    assert wc.heartbeat(store, item="0007", instance="alpha",
                        now="2026-01-01T02:00:00Z") is True
    assert wc.claim_for_item(store, "0007").heartbeat_at == "2026-01-01T02:00:00Z"


def test_heartbeat_for_a_missing_item_is_false(store: Path) -> None:
    assert wc.heartbeat(store, item="nope", instance="alpha") is False


def test_release_is_owner_guarded(store: Path) -> None:
    _claim(store)
    assert wc.release(store, item="0007", instance="beta") is False
    assert wc.claim_for_item(store, "0007") is not None
    assert wc.release(store, item="0007", instance="alpha") is True
    assert wc.claim_for_item(store, "0007") is None


def test_reassign_is_a_compare_and_swap_on_the_current_owner(store: Path) -> None:
    _claim(store, pid=1, mux_session="alpha")
    moved = wc.reassign(store, item="0007", expect_owner="alpha",
                        to_instance="beta", pid=2, mux_session="beta")
    assert (moved.instance, moved.pid, moved.mux_session) == ("beta", 2, "beta")
    assert wc.claim_for_instance(store, "alpha") is None
    assert wc.claim_for_instance(store, "beta").item == "0007"


def test_reassign_refuses_when_the_claim_changed_hands(store: Path) -> None:
    """A claim that moved while the caller was deciding must not be
    overwritten with whatever the caller believed a moment ago."""
    _claim(store, instance="gamma")
    with pytest.raises(wc.ClaimRefused) as caught:
        wc.reassign(store, item="0007", expect_owner="alpha",
                    to_instance="beta")
    assert caught.value.reason == wc.ITEM_HELD
    assert wc.claim_for_item(store, "0007").instance == "gamma"


def test_reassign_refuses_a_new_owner_that_is_already_busy(store: Path) -> None:
    _claim(store)
    _claim(store, item="0008", instance="beta")
    with pytest.raises(wc.ClaimRefused) as caught:
        wc.reassign(store, item="0007", expect_owner="alpha",
                    to_instance="beta")
    assert caught.value.reason == wc.INSTANCE_BUSY
    assert wc.claim_for_item(store, "0007").instance == "alpha"


def test_reassign_refuses_an_unheld_item(store: Path) -> None:
    with pytest.raises(wc.ClaimRefused) as caught:
        wc.reassign(store, item="ghost", expect_owner="alpha",
                    to_instance="beta")
    assert caught.value.holder is None


# ── lookups ─────────────────────────────────────────────────────
def test_claim_for_instance_answers_the_resume_question(store: Path) -> None:
    _claim(store)
    assert wc.claim_for_instance(store, "alpha").item == "0007"
    assert wc.claim_for_instance(store, "beta") is None


def test_claims_lists_every_row(store: Path) -> None:
    _claim(store, now="2026-01-01T00:00:00Z")
    _claim(store, item="0008", instance="beta", now="2026-01-02T00:00:00Z")
    assert [c.item for c in wc.claims(store)] == ["0007", "0008"]


# ── timestamps ──────────────────────────────────────────────────
def test_utcnow_round_trips_through_parse_ts() -> None:
    parsed = wc.parse_ts(wc.utcnow())
    assert parsed is not None and parsed.tzinfo is not None
    assert abs((datetime.now(tz=timezone.utc) - parsed).total_seconds()) < 120


@pytest.mark.parametrize("value", [None, "", "not a time", "2026-13-45"])
def test_an_unreadable_timestamp_is_none_not_a_guess(value) -> None:
    """``None`` is a real answer here. Smoothing it into the epoch would make
    every unreadable heartbeat instantly ancient, and into "now" would make it
    eternally fresh -- opposite wrong verdicts from the same defect."""
    assert wc.parse_ts(value) is None


def test_an_iso_timestamp_with_an_offset_is_read_as_written() -> None:
    parsed = wc.parse_ts("2026-01-01T12:00:00+02:00")
    assert parsed == datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


def test_a_naive_timestamp_is_read_as_utc() -> None:
    assert wc.parse_ts("2026-01-01T12:00:00") == \
        datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


# ── concurrency ─────────────────────────────────────────────────
def test_a_claim_holds_the_write_lock_across_its_whole_check_and_write(
        store: Path, monkeypatch) -> None:
    """Check and write are one transaction, not two visible steps.

    Probed from *inside* the claim, at the moment it has read the table and
    not yet written: a competing writer on its own connection must already be
    locked out. Without the ``BEGIN IMMEDIATE`` nothing has been written yet,
    so no write lock is held and the competitor gets in -- which is exactly
    the interleaving that ends with two owners for one item.
    """
    seen: dict = {}
    original = wc._claim_for_item

    def probing(conn, item):
        result = original(conn, item)
        if "locked" not in seen:
            other = sqlite3.connect(store, timeout=0.2)
            try:
                other.execute("BEGIN IMMEDIATE")
                seen["locked"] = False
                other.rollback()
            except sqlite3.OperationalError:
                seen["locked"] = True
            finally:
                other.close()
        return result

    monkeypatch.setattr(wc, "_claim_for_item", probing)
    wc.claim(store, item="0007", instance="alpha")
    assert seen.get("locked") is True, (
        "a competing writer got a write lock while a claim was mid-flight")
    assert wc.claim_for_item(store, "0007").instance == "alpha"


def test_two_racing_claims_produce_exactly_one_owner(store: Path) -> None:
    """Two threads, one item. Whoever wins, the table must not end up
    describing two owners or none."""
    import threading

    barrier = threading.Barrier(2)
    outcomes: dict = {}

    def attempt(instance: str) -> None:
        barrier.wait(timeout=10)
        try:
            wc.claim(store, item="0007", instance=instance)
            outcomes[instance] = "won"
        except wc.ClaimRefused:
            outcomes[instance] = "refused"
        except sqlite3.OperationalError:
            outcomes[instance] = "locked-out"

    threads = [threading.Thread(target=attempt, args=(name,))
               for name in ("alpha", "beta")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    rows = wc.claims(store)
    assert len(rows) == 1, f"expected one owner, found {rows}"
    winners = [name for name, result in outcomes.items() if result == "won"]
    assert winners == [rows[0].instance]


def test_a_claim_survives_a_reopen(tmp_path: Path) -> None:
    """The store is a file, not a process's memory: a supervisor restart must
    find the claim its predecessor took."""
    path = wc.db_path(tmp_path)
    wc.init_db(path)
    _claim(path, boot_id="uuid:abc", pid=7)
    reopened = wc.claim_for_item(path, "0007")
    assert (reopened.boot_id, reopened.pid) == ("uuid:abc", 7)
    assert wc.parse_ts(reopened.claimed_at) is not None


def test_the_heartbeat_of_a_long_running_claim_can_be_refreshed(store: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _claim(store, now=start.strftime(wc.TS_FORMAT))
    for minutes in (10, 20, 30):
        stamp = (start + timedelta(minutes=minutes)).strftime(wc.TS_FORMAT)
        assert wc.heartbeat(store, item="0007", instance="alpha", now=stamp)
    assert wc.claim_for_item(store, "0007").heartbeat_at == "2026-01-01T00:30:00Z"
