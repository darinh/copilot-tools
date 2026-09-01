"""The cross-platform harness's two waits, executed rather than parsed.

`verify_cross_platform.py` is a CI step in its own right and the only runtime
exercise of `operator_runner` against a real multiplexer, so nothing else
would notice it going wrong -- and it did go wrong, reporting a working
runner as broken on every platform for as long as anyone looked.

The whole harness cannot run here: it wants a multiplexer and spends a
minute. Its two decision points can, and they are where the bug was. An
earlier version of this file matched the source with `ast` instead; an
adversarial reviewer pointed out that inverting the wait's condition to the
broken opposite satisfied every one of those assertions, which is a test that
cannot fail for the reason it exists. These call the code.
"""
from __future__ import annotations

import sqlite3

import pytest

import operator_ingest
import verify_cross_platform as vcp


def _record_session(db, session_num):
    """Insert one committed, non-no_op session row, through the real schema.

    Written with an explicit column list against the live schema rather than
    a canned row: `sessions` carries several NOT NULL columns, and a helper
    that drifted from them would fail loudly here instead of quietly
    inserting nothing and letting every assertion below pass for free.
    """
    operator_ingest.init_db(db)
    with operator_ingest.connect(db) as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
        assert {"session_num", "no_op", "started_at", "ended_at"} <= columns, (
            "the columns this helper writes are gone from the schema; the "
            "tests in this file would be measuring nothing")
        conn.execute(
            "INSERT INTO sessions (session_num, no_op, started_at, ended_at) "
            "VALUES (?, 0, ?, ?)",
            (session_num, "2026-07-27T10:00:00Z", "2026-07-27T10:30:00Z"))


# ── wait_until ──────────────────────────────────────────────────

def test_a_predicate_that_is_already_true_does_not_wait(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(vcp.time, "sleep", slept.append)

    assert vcp.wait_until(lambda: True, timeout=30) is True
    assert slept == [], "nothing should be waited for when it already holds"


def test_a_predicate_that_becomes_true_is_polled_until_it_does(monkeypatch):
    """The behaviour the harness actually needs: keep asking."""
    slept: list[float] = []
    monkeypatch.setattr(vcp.time, "sleep", slept.append)
    answers = iter([False, False, True])

    assert vcp.wait_until(lambda: next(answers), timeout=30) is True
    assert len(slept) == 2, "one sleep between each re-ask, and no more"


def test_a_predicate_that_never_holds_gives_up_at_the_deadline(monkeypatch):
    """The property that keeps a real failure a failure.

    An unbounded wait would turn a genuinely absent database -- the thing the
    check exists to catch -- into a hung CI job with no output instead of a
    red one with a reason.
    """
    slept: list[float] = []
    monkeypatch.setattr(vcp.time, "sleep", slept.append)
    clock = iter([0.0, 10.0, 20.0, 30.0, 40.0])
    monkeypatch.setattr(vcp.time, "monotonic", lambda: next(clock))

    assert vcp.wait_until(lambda: False, timeout=30) is False
    assert slept, "it must actually have waited before giving up"


def test_the_wait_is_measured_on_a_monotonic_clock(monkeypatch):
    """A CI runner is a fresh VM and its wall clock is often corrected mid-run.
    A backwards step in `time.time` would end a wait early and reintroduce the
    race; `time.monotonic` cannot step backwards."""
    monkeypatch.setattr(vcp.time, "sleep", lambda _s: None)
    monkeypatch.setattr(vcp.time, "time", lambda: pytest.fail(
        "wait_until must not read the wall clock"))
    answers = iter([False, True])

    assert vcp.wait_until(lambda: next(answers), timeout=30) is True


# ── metrics_recorded ────────────────────────────────────────────

def test_an_absent_database_has_recorded_nothing(tmp_path):
    assert vcp.metrics_recorded(tmp_path / "missing.db", 9) is False


def test_a_database_created_but_not_yet_written_has_recorded_nothing(tmp_path):
    """The race the file-existence wait could not see.

    `operator_ingest.connect` opens the database with `sqlite3.connect`, which
    creates the file the moment ingestion *begins* -- before the schema, the
    log parse, and a git subprocess carrying its own five-second timeout. A
    wait that stopped at "the file is there" would go on to query this exact
    state and report a working runner as broken.
    """
    db = tmp_path / "m.db"
    sqlite3.connect(str(db)).close()

    assert db.exists(), "the precondition: the file is there"
    assert vcp.metrics_recorded(db, 9) is False


def test_an_initialised_database_with_no_session_has_recorded_nothing(tmp_path):
    """One step further along: schema applied, row not inserted yet."""
    db = tmp_path / "m.db"
    operator_ingest.init_db(db)

    assert vcp.metrics_recorded(db, 9) is False


def test_a_committed_session_is_what_counts_as_recorded(tmp_path):
    """The positive case. Without this the three negatives above are satisfied
    by a function that always answers False."""
    db = tmp_path / "m.db"
    _record_session(db, session_num=9)

    assert vcp.metrics_recorded(db, 9) is True


def test_another_session_number_is_not_this_run(tmp_path):
    """The harness asks about the run it launched. A row from any other
    session must not answer for it, or the check would pass on a database
    left behind by something else."""
    db = tmp_path / "m.db"
    _record_session(db, session_num=8)

    assert vcp.metrics_recorded(db, 9) is False
