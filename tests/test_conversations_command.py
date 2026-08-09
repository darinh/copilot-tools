"""Guards for ``operator conversations``.

Two defects reached a working prototype here and both are pinned below,
because neither could be seen from the code and neither raised anything:

* the command resolved the database from ``OPERATOR_HOME`` but let each
  seeder resolve its own root, so a relocated state tree wrote rows into the
  developer's *real* store while reading mail from the temporary one;
* an absent source was reported as an error, which would make ``seed`` exit
  non-zero on any machine that had never run a peer agent.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import conversation_log as clog
import copilot_operator as op


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A state tree of our own, wired the way the real command wires it."""
    state = tmp_path / "operator"
    state.mkdir()
    monkeypatch.setattr(op, "OPERATOR_HOME", state)
    return state


def run(args, capsys):
    code = op.conversations_command(args)
    return code, capsys.readouterr()


def test_no_verb_prints_usage_and_fails(capsys):
    code, out = run([], capsys)
    assert code == 1
    assert "seed" in out.err and "serve" in out.err


def test_help_prints_usage_and_succeeds(capsys):
    code, out = run(["--help"], capsys)
    assert code == 0
    assert "Usage: operator conversations" in out.out


def test_an_unknown_verb_names_the_real_ones(capsys):
    code, out = run(["bogus"], capsys)
    assert code == 1
    assert "seed, serve, stats" in out.err


def test_stats_on_an_empty_store_reports_zero_rather_than_crashing(home, capsys):
    code, out = run(["stats"], capsys)
    assert code == 0
    assert "0 message(s)" in out.out


def test_seed_writes_into_the_configured_home_only(home, capsys, monkeypatch):
    """The first defect: the store went to one home, the sources to another.

    Every root must be derived from the same ``OPERATOR_HOME``. Without this,
    a test run seeds the developer's real database -- silently, because each
    half of the operation is individually correct.
    """
    monkeypatch.setattr(clog, "copilot_home", lambda: home / "no-copilot")
    mailbox = home / "messages" / "copilot-tools"
    mailbox.mkdir(parents=True)
    (mailbox / "m.json").write_text(json.dumps(
        {"id": "m1", "from": "scripts", "to": "copilot-tools",
         "text": "hello", "sent_at": "2026-08-01T00:00:00Z"}), encoding="utf-8")

    code, _ = run(["seed"], capsys)
    assert code == 0
    assert (home / "conversations.db").exists()

    conn = sqlite3.connect(home / "conversations.db")
    stored = conn.execute("SELECT sender FROM messages").fetchall()
    conn.close()
    assert stored == [("scripts",)]


def test_seed_succeeds_on_a_machine_with_no_sources_at_all(home, capsys,
                                                           monkeypatch):
    """The second defect: absent was reported as an error.

    A clean install has no mail directory and may have no session store. That
    is not a failure, and a seeder that exits 1 over it breaks every script
    that runs it.
    """
    monkeypatch.setattr(clog, "copilot_home", lambda: home / "no-copilot")
    code, out = run(["seed"], capsys)
    assert code == 0
    assert "nothing to read" in out.out


def test_seed_fails_when_a_source_is_present_and_unreadable(home, capsys,
                                                            monkeypatch):
    """The control for the test above; these must not report identically.

    Absent means "not on this machine". Unreadable means "here and broken",
    and a seeder that exits 0 having dropped a whole source is one nobody
    re-runs.
    """
    monkeypatch.setattr(clog, "copilot_home", lambda: home / "no-copilot")
    mailbox = home / "messages" / "copilot-tools"
    mailbox.mkdir(parents=True)
    (mailbox / "broken.json").write_text("{not json", encoding="utf-8")
    code, out = run(["seed"], capsys)
    assert code == 1
    assert "error" in out.out + out.err


def test_seed_is_idempotent_through_the_command(home, capsys, monkeypatch):
    monkeypatch.setattr(clog, "copilot_home", lambda: home / "no-copilot")
    spool = home / "conversation-spool"
    spool.mkdir()
    (spool / "2026-08-09.jsonl").write_text(json.dumps(
        {"id": "e1", "direction": "inbound", "body": "a prompt",
         "sent_at": "2026-08-09T10:00:00Z"}), encoding="utf-8")
    run(["seed"], capsys)
    run(["seed"], capsys)
    conn = sqlite3.connect(home / "conversations.db")
    count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    assert count == 1


def test_an_unknown_source_is_refused(home, capsys):
    with pytest.raises(SystemExit):
        run(["seed", "--source", "nonsense"], capsys)


def test_a_single_source_can_be_selected(home, capsys, monkeypatch):
    """`--source` exists so a source under investigation runs alone."""
    monkeypatch.setattr(clog, "copilot_home", lambda: home / "no-copilot")
    code, out = run(["seed", "--source", "operator-mail"], capsys)
    assert code == 0
    assert "session-store" not in out.out


def test_a_flag_missing_its_value_does_not_eat_the_next_flag():
    """``serve --port --no-browser`` is a typo, not a port called
    ``--no-browser``. Reading it as one raises a ValueError naming something
    the user never typed."""
    assert op._flag_value(["--port", "--no-browser"], "--port") == ""
    assert op._flag_value(["--port", "9000"], "--port") == "9000"
    assert op._flag_value(["--port=9000"], "--port") == "9000"
    assert op._flag_value(["--port"], "--port") == ""
    assert op._flag_value([], "--port") == ""


def test_the_capture_extension_ships_where_setup_looks_for_it():
    """Installation is `setup_tools.install_extensions`' job, not ours.

    It deploys *every* directory under ``extensions/`` to
    ``~/.copilot/extensions``, with manifest tracking and a Windows junction
    fallback. A second installer in this command would be a second place for
    the deployment rules to drift, so the only thing to assert is that the
    extension sits where that one already looks.
    """
    import setup_tools
    sources = setup_tools._extension_sources() or []
    names = [p.name for p in sources]
    assert "conversation-capture" in names, names
    entry = (setup_tools.REPO_ROOT / "extensions" / "conversation-capture"
             / setup_tools.EXTENSION_ENTRYPOINT)
    assert entry.is_file(), f"{entry} is what setup deploys"


def test_the_usage_text_says_who_installs_the_extension(capsys):
    """Dropping the `install` verb without saying what replaced it leaves the
    capture half looking like it needs no deployment at all."""
    import io
    stream = io.StringIO()
    op._conversations_usage(stream)
    assert "setup" in stream.getvalue()


def test_the_verb_list_and_the_dispatcher_agree():
    """A verb accepted by the dispatcher but absent from the list is
    undiscoverable; one in the list the dispatcher refuses is a broken
    promise. The usage text is read rather than restated."""
    import io
    stream = io.StringIO()
    op._conversations_usage(stream)
    text = stream.getvalue()
    for verb in op.CONVERSATIONS_VERBS:
        assert verb in text, f"{verb} is dispatchable but undocumented"
