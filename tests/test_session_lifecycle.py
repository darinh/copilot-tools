"""The session lifecycle: assignment resolved before the first token, and an
end that cannot half-happen.

Two properties carry this file. The first is the *order* of resolution -- an
instance's own claim beats every offer, and an offer is never a taking. The
second is the one combination FR-5 exists to make unreachable: a **released
claim with no handoff**, which loses the context and hands the worktree to
somebody else. That one is asserted at every point the call can fail, not
argued for in a docstring, because a property only ever reasoned about is a
property nothing checks.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import operator_liveness as live  # noqa: E402
import operator_session as osess  # noqa: E402
import work_claims as wc  # noqa: E402


class FakeProbes:
    """Probes with dictated answers, so a verdict needs no reboot or mux.

    ``dead`` names the instances whose pid is to be reported absent; every
    other claim's pid is reported present with the start token it recorded,
    which is what a live owner looks like.
    """

    def __init__(self, *, boot="boot-1", dead=(), unknown=()):
        self._boot = boot
        self._dead = set(dead)
        self._unknown = set(unknown)
        self.by_pid: dict = {}

    def boot_identity(self):
        return self._boot

    def _state(self, pid):
        return self.by_pid.get(pid)

    def process_present(self, pid):
        state = self._state(pid)
        if state in self._dead:
            return False
        if state in self._unknown:
            return None
        return True

    def process_start_token(self, pid):
        return "start-token"

    def session_present(self, session):
        return True


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = osess.db_path(tmp_path)
    osess.init_db(path)
    return path


def _claim(db: Path, item, instance, *, pid=None, when=None, subproject="",
           worktree=None, branch=None):
    return wc.claim(db, item=item, instance=instance, subproject=subproject,
                    worktree=worktree, branch=branch, boot_id="boot-1",
                    mux_session=instance, pid=pid or 1000,
                    pid_start="start-token", now=when)


def _probes(**kw) -> FakeProbes:
    return FakeProbes(**kw)


# ── the database ────────────────────────────────────────────────
def test_session_log_shares_the_claim_database(tmp_path: Path) -> None:
    """One file, because the close and the release share one transaction."""
    assert osess.db_path(tmp_path) == wc.db_path(tmp_path)


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    path = osess.db_path(tmp_path)
    osess.init_db(path)
    _claim(path, "0007", "alpha")
    osess.start_session(path, instance="alpha", session=1)
    osess.init_db(path)
    assert len(osess.sessions(path)) == 1
    assert [c.item for c in wc.claims(path)] == ["0007"]


def test_session_log_is_keyed_by_instance_and_session(db: Path) -> None:
    # One index over *both* columns, in that order -- not two single-column
    # indexes, which would satisfy a union-of-names check while leaving the
    # lookup this schema exists for unindexed.
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        indexes = [r["name"] for r in conn.execute("PRAGMA index_list(session_log)")]
        ordered = []
        for name in indexes:
            rows = sorted(conn.execute(f"PRAGMA index_info({name})"),
                          key=lambda r: r["seqno"])
            ordered.append([r["name"] for r in rows])
    assert ["instance", "session"] in ordered, ordered


# ── FR-2: resolution order ──────────────────────────────────────
def test_no_claims_anywhere_is_no_assignment(db: Path) -> None:
    got = osess.resolve_assignment(db, instance="alpha", probes=_probes())
    assert got.kind == osess.NONE
    assert got.claim is None and got.offers == ()


def test_an_instance_that_holds_an_item_resumes_it(db: Path) -> None:
    _claim(db, "0007", "alpha", worktree="/w/alpha", branch="feat/x")
    got = osess.resolve_assignment(db, instance="alpha", probes=_probes())
    assert got.kind == osess.RESUME
    assert got.item == "0007"
    assert got.worktree == "/w/alpha"
    assert got.branch == "feat/x"


def test_own_claim_resumes_even_when_its_recorded_process_is_gone(db: Path) -> None:
    """The restart case, and the whole reason liveness is not asked here.

    After a restart the recorded pid is the *previous* process of this same
    instance, so the cascade returns DEAD by construction. Consulting it would
    put the agent's own item on the offer list for somebody else to take.
    """
    _claim(db, "0007", "alpha", pid=4242)
    probes = _probes(dead={"gone"})
    probes.by_pid[4242] = "gone"
    got = osess.resolve_assignment(db, instance="alpha", probes=probes)
    assert got.kind == osess.RESUME
    assert got.item == "0007"


def test_a_live_owner_is_never_offered(db: Path) -> None:
    _claim(db, "0007", "beta")
    got = osess.resolve_assignment(db, instance="alpha", probes=_probes())
    assert got.kind == osess.NONE
    assert got.offers == ()


def test_a_dead_owner_is_offered(db: Path) -> None:
    _claim(db, "0007", "beta", pid=4242)
    probes = _probes(dead={"gone"})
    probes.by_pid[4242] = "gone"
    got = osess.resolve_assignment(db, instance="alpha", probes=probes)
    assert got.kind == osess.OFFER
    assert [o.item for o in got.offers] == ["0007"]
    assert "not running" in got.offers[0].reason


def test_an_offer_takes_nothing(db: Path) -> None:
    """Reported, not claimed: taking it is `operator work reclaim`'s job,
    and that has to preserve the dead owner's uncommitted work first (FR-4)."""
    _claim(db, "0007", "beta", pid=4242)
    probes = _probes(dead={"gone"})
    probes.by_pid[4242] = "gone"
    osess.resolve_assignment(db, instance="alpha", probes=probes)
    still = wc.claim_for_item(db, "0007")
    assert still is not None and still.instance == "beta"
    assert wc.claim_for_instance(db, "alpha") is None


def test_offers_are_oldest_first(db: Path) -> None:
    _claim(db, "0009", "gamma", pid=4243, when="2026-08-05T10:00:00Z")
    _claim(db, "0007", "beta", pid=4242, when="2026-08-01T10:00:00Z")
    probes = _probes(dead={"gone"})
    probes.by_pid[4242] = probes.by_pid[4243] = "gone"
    got = osess.resolve_assignment(db, instance="alpha", probes=probes)
    assert [o.item for o in got.offers] == ["0007", "0009"]


def test_offers_tied_on_timestamp_are_ordered_by_item(db: Path, monkeypatch) -> None:
    """One-second resolution makes a tie ordinary, and an unstable order would
    make two reads of an unchanged database disagree.

    The store is made to hand the claims back in the *wrong* order, because it
    currently sorts by ``(claimed_at, item)`` itself -- so a test that took its
    output as given would pass with the tiebreak deleted, and would be proving
    a property of ``work_claims.claims`` rather than of this function.
    """
    _claim(db, "0009", "gamma", pid=4243, when="2026-08-01T10:00:00Z")
    _claim(db, "0007", "beta", pid=4242, when="2026-08-01T10:00:00Z")
    probes = _probes(dead={"gone"})
    probes.by_pid[4242] = probes.by_pid[4243] = "gone"

    real = wc.claims
    monkeypatch.setattr(osess.work_claims, "claims",
                        lambda path: list(reversed(real(path))))
    got = osess.resolve_assignment(db, instance="alpha", probes=probes)
    assert [o.item for o in got.offers] == ["0007", "0009"]


def test_offers_are_ordered_by_this_function_not_by_the_store(
        db: Path, monkeypatch) -> None:
    """Same point for the timestamp, not the tiebreak."""
    _claim(db, "0009", "gamma", pid=4243, when="2026-08-05T10:00:00Z")
    _claim(db, "0007", "beta", pid=4242, when="2026-08-01T10:00:00Z")
    probes = _probes(dead={"gone"})
    probes.by_pid[4242] = probes.by_pid[4243] = "gone"
    real = wc.claims
    monkeypatch.setattr(osess.work_claims, "claims",
                        lambda path: list(reversed(real(path))))
    got = osess.resolve_assignment(db, instance="alpha", probes=probes)
    assert [o.item for o in got.offers] == ["0007", "0009"]


def test_resume_beats_an_available_offer(db: Path) -> None:
    _claim(db, "0007", "alpha")
    _claim(db, "0009", "beta", pid=4242)
    probes = _probes(dead={"gone"})
    probes.by_pid[4242] = "gone"
    got = osess.resolve_assignment(db, instance="alpha", probes=probes)
    assert got.kind == osess.RESUME and got.item == "0007"
    assert got.offers == ()


def test_a_stale_claim_is_reported_and_never_offered(db: Path) -> None:
    """STALE means the cascade could not establish the owner is gone.

    Guessing there is how two agents end up in one tree.
    """
    old = (datetime.now(tz=timezone.utc) - timedelta(hours=4)).strftime(wc.TS_FORMAT)
    _claim(db, "0007", "beta", pid=4242, when=old)
    probes = _probes(unknown={"cannot-tell"})
    probes.by_pid[4242] = "cannot-tell"
    got = osess.resolve_assignment(db, instance="alpha", probes=probes)
    assert got.kind == osess.NONE
    assert got.offers == ()
    assert [o.item for o in got.stale] == ["0007"]


def test_subproject_filters_what_may_be_offered(db: Path) -> None:
    _claim(db, "0007", "beta", pid=4242, subproject="api")
    _claim(db, "0009", "gamma", pid=4243, subproject="web")
    probes = _probes(dead={"gone"})
    probes.by_pid[4242] = probes.by_pid[4243] = "gone"
    got = osess.resolve_assignment(db, instance="alpha", subproject="web",
                                   probes=probes)
    assert [o.item for o in got.offers] == ["0009"]


def test_subproject_does_not_filter_the_resume(db: Path) -> None:
    """Refusing to resume would strand a live claim whose owner was told to
    look somewhere else."""
    _claim(db, "0007", "alpha", subproject="api")
    got = osess.resolve_assignment(db, instance="alpha", subproject="web",
                                   probes=_probes())
    assert got.kind == osess.RESUME and got.item == "0007"


# ── the session log ─────────────────────────────────────────────
def test_start_opens_a_row_carrying_the_assignment(db: Path) -> None:
    _claim(db, "0007", "alpha", worktree="/w/a", branch="feat/x", subproject="api")
    osess.start_session(db, instance="alpha", session=3, probes=_probes())
    row = osess.open_session(db, instance="alpha")
    assert row["session"] == 3
    assert row["item"] == "0007"
    assert row["worktree"] == "/w/a"
    assert row["branch"] == "feat/x"
    assert row["subproject"] == "api"
    assert row["ended_at"] is None


def test_a_second_start_abandons_the_row_the_first_left_open(db: Path) -> None:
    osess.start_session(db, instance="alpha", session=1, probes=_probes())
    osess.start_session(db, instance="alpha", session=2, probes=_probes())
    rows = osess.sessions(db, instance="alpha")
    assert [r["session"] for r in rows] == [1, 2]
    assert rows[0]["outcome"] == osess.ABANDONED
    assert rows[0]["ended_at"] is not None
    assert rows[1]["ended_at"] is None


def test_the_log_is_append_only_across_a_reused_session_number(db: Path) -> None:
    """A run that resets its numbering must not overwrite the earlier run."""
    osess.start_session(db, instance="alpha", session=1, probes=_probes())
    osess.start_session(db, instance="alpha", session=1, probes=_probes())
    rows = osess.sessions(db, instance="alpha")
    assert len(rows) == 2
    assert rows[0]["outcome"] == osess.ABANDONED


def test_another_instances_open_row_is_left_alone(db: Path) -> None:
    osess.start_session(db, instance="beta", session=1, probes=_probes())
    osess.start_session(db, instance="alpha", session=1, probes=_probes())
    beta = osess.open_session(db, instance="beta")
    assert beta is not None and beta["ended_at"] is None


# ── FR-5: ending a session ──────────────────────────────────────
def _end(db: Path, *, instance="alpha", session=1, handoff=None, done=False):
    calls = []

    def default_handoff():
        calls.append("written")
        return "/h/alpha.md"

    result = osess.end_session(db, instance=instance, session=session,
                               write_handoff=handoff or default_handoff,
                               release_claim=done)
    return result, calls


def test_end_writes_the_handoff_closes_the_log_and_keeps_the_claim(db: Path) -> None:
    _claim(db, "0007", "alpha")
    osess.start_session(db, instance="alpha", session=1, probes=_probes())
    result, calls = _end(db)
    assert result.ok and calls == ["written"]
    assert result.handoff_written and result.log_closed
    assert result.claim_retained and not result.claim_released
    assert wc.claim_for_instance(db, "alpha") is not None
    assert osess.open_session(db, instance="alpha") is None
    assert osess.sessions(db)[0]["outcome"] == osess.ENDED


def test_the_claim_is_kept_by_default_so_the_next_session_can_resume(db: Path) -> None:
    """Releasing on every end would empty the claim before FR-2's resume path
    could ever fire.

    ``release_claim`` is not passed at all: the default is the guarantee, and a
    test that spells out ``release_claim=False`` proves nothing about it.
    """
    _claim(db, "0007", "alpha")
    osess.start_session(db, instance="alpha", session=1, probes=_probes())
    result = osess.end_session(db, instance="alpha", session=1,
                              write_handoff=lambda: "/h/alpha.md")
    assert result.ok and result.claim_retained and not result.claim_released
    again = osess.resolve_assignment(db, instance="alpha", probes=_probes())
    assert again.kind == osess.RESUME and again.item == "0007"


def test_done_releases_the_claim(db: Path) -> None:
    _claim(db, "0007", "alpha")
    osess.start_session(db, instance="alpha", session=1, probes=_probes())
    result, _ = _end(db, done=True)
    assert result.claim_released and not result.claim_retained
    assert wc.claim_for_instance(db, "alpha") is None


def test_a_retained_claim_has_its_heartbeat_refreshed(db: Path) -> None:
    _claim(db, "0007", "alpha", when="2026-01-01T00:00:00Z")
    osess.start_session(db, instance="alpha", session=1, probes=_probes())
    _end(db)
    assert wc.claim_for_instance(db, "alpha").heartbeat_at != "2026-01-01T00:00:00Z"


def test_ending_without_a_claim_is_not_an_error(db: Path) -> None:
    osess.start_session(db, instance="alpha", session=1, probes=_probes())
    result, _ = _end(db, done=True)
    assert result.ok and result.item is None
    assert not result.claim_released and not result.claim_retained
    assert any("nothing to release" in n for n in result.notes)


def test_closing_no_open_row_is_reported_rather_than_silent(db: Path) -> None:
    """A log that closes nothing is the shape of a wiring mistake."""
    result, _ = _end(db)
    assert result.ok and not result.log_closed
    assert any("no open session row" in n for n in result.notes)


def test_end_only_closes_its_own_instances_row(db: Path) -> None:
    osess.start_session(db, instance="beta", session=1, probes=_probes())
    osess.start_session(db, instance="alpha", session=1, probes=_probes())
    _end(db)
    assert osess.open_session(db, instance="beta") is not None


def test_end_only_closes_the_row_of_the_session_it_names(db: Path) -> None:
    """A row belonging to a different session of the same instance is somebody
    else's record of somebody else's work, and closing it would date-stamp an
    end that did not happen."""
    osess.start_session(db, instance="alpha", session=1, probes=_probes())
    result, _ = _end(db, session=2)
    assert result.ok and not result.log_closed
    still = osess.open_session(db, instance="alpha")
    assert still is not None and still["session"] == 1


# ── FR-5: the combination that must be unreachable ──────────────
def _boom():
    raise OSError("read-only home")


def test_a_failed_handoff_stops_everything_after_it(db: Path) -> None:
    _claim(db, "0007", "alpha")
    osess.start_session(db, instance="alpha", session=1, probes=_probes())
    result, _ = _end(db, handoff=_boom, done=True)
    assert not result.ok
    assert not result.handoff_written
    assert not result.log_closed
    assert not result.claim_released
    assert wc.claim_for_instance(db, "alpha") is not None
    assert osess.open_session(db, instance="alpha") is not None


def test_a_database_failure_leaves_the_handoff_on_disk_and_the_claim_held(
        db: Path, monkeypatch) -> None:
    _claim(db, "0007", "alpha")
    osess.start_session(db, instance="alpha", session=1, probes=_probes())

    def exploding(_path):
        # A plain function, not a generator: `with connect(path)` fails at the
        # call, which is what a connection failure actually looks like. The
        # `@contextmanager` spelling needed a `yield` after the `raise` to stay
        # a generator, and unreachable code is unreachable code.
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(osess, "connect", exploding)
    result, calls = _end(db, done=True)
    assert calls == ["written"]
    assert result.handoff_written and not result.ok
    assert not result.log_closed and not result.claim_released
    monkeypatch.undo()
    assert wc.claim_for_instance(db, "alpha") is not None
    assert osess.open_session(db, instance="alpha") is not None


def test_the_log_close_and_the_release_are_one_transaction(
        db: Path, monkeypatch) -> None:
    """A release that fails must not leave the log closed, and vice versa.

    Forced by making the DELETE raise after the UPDATE has already run: if the
    two were separate transactions the row would be closed and the claim still
    held, which is a session recorded as ended whose work nobody can account
    for.
    """
    _claim(db, "0007", "alpha")
    osess.start_session(db, instance="alpha", session=1, probes=_probes())
    real = osess.connect

    class Sabotage:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a, **kw):
            if sql.lstrip().upper().startswith("DELETE"):
                raise sqlite3.OperationalError("disk I/O error")
            return self._conn.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def wrapped(path):
        with real(path) as conn:
            yield Sabotage(conn)

    monkeypatch.setattr(osess, "connect", wrapped)
    result, _ = _end(db, done=True)
    monkeypatch.undo()

    assert not result.ok
    assert not result.claim_released
    assert not result.log_closed
    assert osess.open_session(db, instance="alpha") is not None
    assert wc.claim_for_instance(db, "alpha") is not None


@pytest.mark.parametrize("failure", ["handoff", "database", "none"])
def test_a_released_claim_never_exists_without_a_handoff(
        db: Path, monkeypatch, failure) -> None:
    """The one combination FR-5 exists to make unreachable, at every point the
    call can fail."""
    _claim(db, "0007", "alpha")
    osess.start_session(db, instance="alpha", session=1, probes=_probes())

    handoff = _boom if failure == "handoff" else None
    if failure == "database":
        def exploding(_path):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(osess, "connect", exploding)

    result, _ = _end(db, handoff=handoff, done=True)
    monkeypatch.undo()

    assert result.released_without_handoff is False
    held = wc.claim_for_instance(db, "alpha")
    assert (held is None) == (failure == "none")


# ── what the agent is told ──────────────────────────────────────
def test_assignment_values_always_carry_all_four_keys(db: Path) -> None:
    keys = {"instanceName", "workItemRef", "worktreePath", "branchName"}
    assert osess.assignment_values(None) == {k: "" for k in keys}
    none = osess.resolve_assignment(db, instance="alpha", probes=_probes())
    assert osess.assignment_values(none) == {
        "instanceName": "alpha", "workItemRef": "",
        "worktreePath": "", "branchName": ""}


def test_assignment_values_are_filled_from_the_resumed_claim(db: Path) -> None:
    _claim(db, "0007", "alpha", worktree="/w/a", branch="feat/x")
    got = osess.resolve_assignment(db, instance="alpha", probes=_probes())
    assert osess.assignment_values(got) == {
        "instanceName": "alpha", "workItemRef": "0007",
        "worktreePath": "/w/a", "branchName": "feat/x"}


def test_describe_is_silent_when_there_is_no_assignment(db: Path) -> None:
    """An always-present 'you have no assignment' is weight paid on every
    token of every unassigned session."""
    none = osess.resolve_assignment(db, instance="alpha", probes=_probes())
    assert osess.describe(none) == ""


def test_describe_names_the_worktree_it_resolved(db: Path) -> None:
    _claim(db, "0007", "alpha", worktree="/w/a", branch="feat/x")
    got = osess.resolve_assignment(db, instance="alpha", probes=_probes())
    text = osess.describe(got)
    assert "0007" in text and "/w/a" in text and "feat/x" in text
    assert "do not need to discover" in text


def test_describe_of_an_offer_forbids_the_mutating_git_verbs(db: Path) -> None:
    """FR-4 is the expensive one, and it fires while the agent is thinking
    'I'll just tidy this tree up first'."""
    _claim(db, "0007", "beta", pid=4242)
    probes = _probes(dead={"gone"})
    probes.by_pid[4242] = "gone"
    got = osess.resolve_assignment(db, instance="alpha", probes=probes)
    text = osess.describe(got)
    assert "0007" in text and "operator work reclaim" in text
    for verb in ("stash", "reset", "clean", "checkout", "restore"):
        assert verb in text


# ── runtime identity ────────────────────────────────────────────
def test_runtime_identity_carries_every_field_the_cascade_reads(db: Path) -> None:
    """A claim written without these can only be judged by its heartbeat,
    which is the one signal the cascade refuses to act on alone."""
    got = osess.runtime_identity(mux_session="alpha", pid=1234,
                                 probes=_probes())
    assert set(got) == {"boot_id", "mux_session", "pid", "pid_start"}
    assert got["pid"] == 1234 and got["mux_session"] == "alpha"
    assert got["boot_id"] == "boot-1"
    assert got["pid_start"] == "start-token"


def test_runtime_identity_records_a_real_start_token_for_this_process(db: Path) -> None:
    """Against the real probes, not a fake: a ``None`` start token silently
    retires the pid-reuse half of step 2 of the cascade, and every claim
    written after that would read LIVE against a recycled pid."""
    assert osess.runtime_identity(mux_session=None)["pid_start"] is not None


def test_the_recorded_start_token_is_what_detects_pid_reuse(db: Path) -> None:
    ident = osess.runtime_identity(mux_session=None)
    wc.claim(db, item="0007", instance="alpha", **ident)
    held = wc.claim_for_item(db, "0007")
    # The same *kind* of token with a different value. Two kinds are not
    # comparable by design -- `psc:` replaced `ps:` when the macOS probe
    # pinned its rendering -- so a stand-in of the wrong shape would assert
    # nothing here, and a stand-in of no shape at all would be read as a
    # broken probe rather than as a recycled pid.
    kind, _, value = ident["pid_start"].partition(":")
    recycled_token = f"{kind}:{value}0"
    assert live.is_start_token(recycled_token)

    class Recycled:
        def boot_identity(self):
            return ident["boot_id"]

        def process_present(self, pid):
            return True

        def process_start_token(self, pid):
            return recycled_token

        def session_present(self, session):
            return True

    assert live.assess(held, probes=Recycled()).verdict == live.DEAD


def test_a_probe_that_answers_nonsense_does_not_condemn_a_claim(db: Path) -> None:
    """The companion to the case above, and the reason it had to be tightened.
    DEAD is reclaimable, so a probe returning something no version of
    `process_start_token` produces -- a broken `ps`, a rendering this code
    does not know -- must not be spent taking a live agent's worktree."""
    ident = osess.runtime_identity(mux_session=None)
    wc.claim(db, item="0008", instance="alpha", **ident)
    held = wc.claim_for_item(db, "0008")

    class Nonsense:
        def boot_identity(self):
            return ident["boot_id"]

        def process_present(self, pid):
            return True

        def process_start_token(self, pid):
            return "somebody-else"

        def session_present(self, session):
            return True

    assert live.assess(held, probes=Nonsense()).verdict != live.DEAD


def test_runtime_identity_defaults_to_this_process(db: Path) -> None:
    assert osess.runtime_identity(probes=_probes())["pid"] == os.getpid()


def test_a_claim_written_from_runtime_identity_reads_live(db: Path) -> None:
    """End to end against the real probes: the identity this module records is
    the identity the cascade accepts."""
    ident = osess.runtime_identity(mux_session=None)
    wc.claim(db, item="0007", instance="alpha", **ident)
    verdict = live.assess(wc.claim_for_item(db, "0007"))
    assert verdict.verdict == live.LIVE
