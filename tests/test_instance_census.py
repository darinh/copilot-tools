"""A path the census cannot compare must not read as a path that is elsewhere.

``live_instance_ids_under`` is the guard behind ``operator inbox`` with no
name: it answers "who else is working here", and a wrong *empty* answer means
the reader consumes a peer's mail and leaves a mailbox that looks exactly like
an empty one. Every unknown in that function is already routed to ``None`` --
a backend that errors, an unreadable state directory, an unreadable tab
registry, a pane the backend will not place, an ``UNPLACEABLE`` launch spec.

``_dir_matches`` was the one decision that was not. It compares two paths by
resolving both, and ``Path.resolve`` does not answer for every path it is
given:

* a **symlink loop** raises ``RuntimeError`` on the interpreters this project
  supports, and ``OSError(ELOOP)`` on newer ones,
* a path carrying an **embedded NUL** raises ``ValueError`` -- and the
  recorded directory is read straight out of a hand-editable launch-spec JSON,
  which can carry ``\\u0000`` in a string,
* and the ``OSError`` it *did* catch was answered ``False``, which is the
  wrong half of the three-valued question: "I cannot compare these" became
  "that instance is somewhere else", and the instance dropped silently out of
  a list whose whole purpose is to be complete.

The first two are not graceful failures either -- they are an uncaught
exception out of ``operator inbox``. This file is the counterpart of
``test_agent_mail_cli.py``'s census tests, kept separate because it is about
one helper's contract rather than the mail CLI's.

These tests run unmodified against the revision before the fix, where the
symlink-loop and NUL cases raise and the ``OSError`` case returns an empty
census.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import copilot_operator as op
import operator_mail


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
    return tmp_path


class CensusMux:
    """The multiplexer, reduced to what the census asks it."""

    def __init__(self, sessions=(), paths=None):
        self.sessions = set(sessions)
        self.paths = dict(paths or {})

    def available(self):
        return True

    def list_sessions(self):
        return sorted(self.sessions)

    def pane_current_path(self, name):
        return self.paths.get(name)

    def has_session(self, name):
        return name in self.sessions


def _register(name, launch=None, managed=True):
    """Register an instance the way a real launch does.

    The spec is written by ``write_launch_spec`` -- the operator's own writer
    -- so the recorded-directory lookup is exercised against a real file
    rather than a fixture shaped like the code under test.
    """
    inst = op.Instance(name)
    if managed:
        inst.claim("tok")
    if launch is not None:
        op.write_launch_spec(inst, [], Path(launch), 1)
    return inst


def _symlink_loop(tmp_path: Path) -> str:
    """A real two-link cycle, or a skip.

    Not simulated. ``Path.resolve`` is the thing under test here, and a double
    that raises where the real object raises is only as good as the guess
    behind it -- this repository has paid for that guess before. If the
    platform will not make the links, or the interpreter resolves the cycle
    without complaint (``os.path.realpath`` stopped raising for it in some
    versions), the test says so rather than asserting against a mock of it.
    """
    a, b = tmp_path / "loopA", tmp_path / "loopB"
    try:
        os.symlink(str(b), str(a), target_is_directory=True)
        os.symlink(str(a), str(b), target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip(f"cannot create symlinks on this machine: {exc}")
    try:
        a.resolve()
    except (OSError, RuntimeError, ValueError):
        return str(a)
    pytest.skip("this interpreter resolves a symlink loop without raising")


# A launch spec is JSON on disk and hand-edited often enough that its `cwd`
# cannot be trusted to be a usable path. JSON can carry a NUL as `\u0000`, and
# `Path.resolve` answers that with ValueError, not OSError.
NUL_PATH = "nul\x00path"


def _nul_path(_tmp_path: Path) -> str:
    return NUL_PATH


# Both flavours, everywhere, because the two arrive as different exception
# types and a rule enforced against one of them is that one's history. The NUL
# is the one that always runs: whether a symlink loop raises at all depends on
# the platform and the interpreter, so a suite that only used the loop would
# report itself green on the legs where it silently skipped.
UNCOMPARABLE = pytest.mark.parametrize(
    "make_uncomparable", [_symlink_loop, _nul_path],
    ids=["symlink-loop", "embedded-nul"])


# ── the helper's own contract ───────────────────────────────────
def test_a_comparable_path_still_answers_yes_or_no(tmp_path):
    """The negative control. A rule that refused everything would pass every
    test below and break the census completely."""
    project = tmp_path / "proj"
    (project / "sub").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()

    assert op._dir_matches(str(project), project) is True
    assert op._dir_matches(str(project / "sub"), project) is True
    assert op._dir_matches(str(tmp_path / "elsewhere"), project) is False
    # A genuinely absent answer stays False: nothing was asked, so there is no
    # uncertainty to report.
    assert op._dir_matches(None, project) is False
    assert op._dir_matches("", project) is False


@UNCOMPARABLE
def test_a_path_that_will_not_resolve_is_neither_here_nor_elsewhere(
        make_uncomparable, tmp_path):
    """It must not raise, and it must not answer False.

    False is the answer that puts an instance somewhere else, and "somewhere
    else" is the one conclusion this comparison cannot support.
    """
    project = tmp_path / "proj"
    project.mkdir()

    assert op._dir_matches(make_uncomparable(tmp_path), project) is None


def test_an_unresolvable_parent_does_not_place_anyone_elsewhere(tmp_path,
                                                                monkeypatch):
    """The polarity case for the ``OSError`` that *was* caught.

    ``parent`` is resolved too, so a parent that will not resolve made every
    comparison answer False -- a census that confidently reported an empty
    directory. Patched on ``pathlib.Path`` rather than on a symbol belonging
    to the module under test, so this test means the same thing on a revision
    that never imported it.
    """
    project = tmp_path / "proj"
    project.mkdir()
    real_resolve = Path.resolve

    def refusing(self, *a, **kw):
        if str(self) == str(project):
            raise OSError(13, "Permission denied")
        return real_resolve(self, *a, **kw)

    monkeypatch.setattr(Path, "resolve", refusing)
    assert op._dir_matches(str(project / "sub"), project) is None


# ── the census that depends on it ───────────────────────────────
@UNCOMPARABLE
def test_a_pane_the_census_cannot_place_is_not_a_clean_census(
        make_uncomparable, tmp_path, monkeypatch):
    """A hole in the census must be reported, not papered over.

    The backend answered here -- ``pane_failed`` is False -- so the existing
    "the backend refused to say" branch never fires and the instance simply
    was not in the returned list.
    """
    project = tmp_path / "proj"
    project.mkdir()
    where = make_uncomparable(tmp_path)
    peer = _register("agent-x", launch=None)
    monkeypatch.setattr(op, "MUX",
                        CensusMux(sessions=[peer.id], paths={peer.id: where}))

    assert op.live_instance_ids_under(project) is None


def test_a_recorded_directory_that_will_not_resolve_fails_the_census(
        tmp_path, monkeypatch):
    """Same hole, one record further down: the launch spec places the peer at
    a path no comparison can be made against.

    The spec is written and read back through the operator's own writer, so
    this exercises the real JSON round trip rather than a stubbed lookup.
    """
    project = tmp_path / "proj"
    project.mkdir()
    peer = _register("agent-x", launch=Path(NUL_PATH))
    assert op._tracked_cwd_for_id(peer.id) == NUL_PATH, (
        "the spec must survive the round trip, or this asserts nothing")
    monkeypatch.setattr(
        op, "MUX",
        CensusMux(sessions=[peer.id],
                  paths={peer.id: str(tmp_path / "elsewhere")}))

    assert op.live_instance_ids_under(project) is None


@UNCOMPARABLE
def test_a_stranger_session_nobody_can_place_does_not_break_the_census(
        make_uncomparable, tmp_path, monkeypatch):
    """The over-refusal control.

    A plain multiplexer session the user opened is not an operator instance
    and owns no mailbox, so it cannot be the peer whose mail gets eaten.
    Refusing the census over one would make ``operator inbox`` unusable on any
    machine that happens to have an odd directory open somewhere.
    """
    project = tmp_path / "proj"
    project.mkdir()
    where = make_uncomparable(tmp_path)
    monkeypatch.setattr(op, "MUX",
                        CensusMux(sessions=["notes"], paths={"notes": where}))

    assert op.live_instance_ids_under(project) == []


def test_a_peer_placed_here_is_still_counted_beside_an_unplaceable_one(
        tmp_path, monkeypatch):
    """Resolution still happens; it is only the failures that changed."""
    project = tmp_path / "proj"
    project.mkdir()
    peer = _register("agent-x", launch=project)
    monkeypatch.setattr(
        op, "MUX",
        CensusMux(sessions=[peer.id], paths={peer.id: str(project)}))

    assert op.live_instance_ids_under(project) == [peer.id]


# ── what it costs the caller ────────────────────────────────────
@UNCOMPARABLE
def test_the_bare_inbox_refuses_rather_than_eating_the_peers_mail(
        make_uncomparable, tmp_path, monkeypatch, capsys):
    """End to end, because a guard the caller never reaches guards nothing.

    Before the fix the symlink loop raised straight out of ``show_inbox`` and
    the NUL did the same; the ``OSError`` half consumed the mail instead. All
    three are checked the only way that matters -- the message is still in the
    mailbox afterwards.
    """
    project = tmp_path / "proj"
    project.mkdir()
    where = make_uncomparable(tmp_path)
    peer = _register("agent-x", launch=None)
    monkeypatch.setattr(op, "MUX",
                        CensusMux(sessions=[peer.id], paths={peer.id: where}))
    monkeypatch.setattr(op, "is_copilot_running", lambda _i: False)
    monkeypatch.chdir(project)
    operator_mail.queue(
        op.OPERATOR_HOME,
        operator_mail.new_message("someone", "proj", op.Instance("proj").id,
                                  "not yours to read"))
    capsys.readouterr()

    assert op.show_inbox([]) == 2
    err = capsys.readouterr().err
    assert "refusing to consume mail for 'proj'" in err
    assert "No mail was read." in err
    assert operator_mail.pending_count(op.OPERATOR_HOME,
                                       op.Instance("proj").id) == 1

    # And the named read still works, so the refusal costs a name and not the
    # message.
    assert op.show_inbox(["proj"]) == 0
    assert "not yours to read" in capsys.readouterr().out
