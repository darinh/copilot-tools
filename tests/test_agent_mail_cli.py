"""Tests for the `operator send` / `operator inbox` commands and --headless.

These cover the CLI seam: name validation, the live-vs-queued decision, and
that the loop hands queued mail to the next session. The storage layer itself
is covered in test_mail.py.
"""
from __future__ import annotations

import json
import os
import shlex

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


class RecordingMux:
    """Stands in for the multiplexer and records what was typed."""

    def __init__(self, sessions=(), dead=False):
        self.sessions = set(sessions)
        self.dead = dead
        self.sent: list[tuple[str, str]] = []

    def available(self):
        return True

    def has_session(self, name):
        return name in self.sessions

    def list_sessions(self):
        return sorted(self.sessions)

    def pane_dead(self, _name):
        return self.dead

    def send_keys(self, session, text, enter=True, literal=True):
        assert literal is True, "message text must never be parsed as key names"
        self.sent.append((session, text))

    def attach(self, _session):  # pragma: no cover - tests assert it is unused
        raise AssertionError("headless paths must never attach")


@pytest.fixture
def live_recipient(monkeypatch):
    """A 'beta' instance whose Copilot process is running."""
    inst = op.Instance("beta")
    inst.claim("tok")
    mux = RecordingMux(sessions=[inst.id])
    monkeypatch.setattr(op, "MUX", mux)
    monkeypatch.setattr(op, "is_copilot_running", lambda i: i.id == inst.id)
    return inst, mux


@pytest.fixture
def idle_recipient(monkeypatch):
    """A known 'beta' instance with no running Copilot process."""
    inst = op.Instance("beta")
    inst.claim("tok")
    mux = RecordingMux(sessions=[])
    monkeypatch.setattr(op, "MUX", mux)
    monkeypatch.setattr(op, "is_copilot_running", lambda _i: False)
    return inst, mux


# ── argument handling ───────────────────────────────────────────
@pytest.mark.parametrize("args", [
    [],
    ["--from", "alpha", "hello"],
    ["--to", "beta", "hello"],
    ["--from", "alpha", "--to", "beta"],
])
def test_send_requires_from_to_and_a_body(args, idle_recipient, capsys):
    assert op.send_message(args) == 2
    assert "Usage: operator send" in capsys.readouterr().err


def test_send_rejects_a_flag_with_no_value(capsys):
    assert op.send_message(["--from"]) == 2
    assert "--from requires a value" in capsys.readouterr().err


def test_send_accepts_equals_form(idle_recipient):
    assert op.send_message(["--from=alpha", "--to=beta", "hi"]) == 0
    (msg,) = operator_mail.pending(op.OPERATOR_HOME, "beta")
    assert msg["from"] == "alpha"


def test_unquoted_message_words_are_joined(idle_recipient):
    op.send_message(["--from", "alpha", "--to", "beta", "several", "words"])
    (msg,) = operator_mail.pending(op.OPERATOR_HOME, "beta")
    assert msg["text"] == "several words"


# ── unknown options are refused, not absorbed ───────────────────
def test_send_refuses_an_unknown_option_rather_than_sending_it_as_text(
        idle_recipient, capsys):
    """A typo'd flag used to become message body.

    The sender who wrote `--dry-run` believed nothing was delivered; the
    recipient got a message. Refusing is the only outcome that matches both
    of their expectations.
    """
    assert op.send_message(
        ["--from", "alpha", "--to", "beta", "--dry-run", "hi"]) == 2
    err = capsys.readouterr().err
    assert "unknown option '--dry-run'" in err
    assert "Nothing was sent." in err
    assert "Usage: operator send" in err
    assert operator_mail.pending_count(op.OPERATOR_HOME, "beta") == 0


def test_send_takes_flag_shaped_text_after_a_double_dash(idle_recipient):
    """`--force` inside a message is text, and does not also arm the flag."""
    assert op.send_message(
        ["--from", "alpha", "--to", "beta", "--", "--force", "is", "the",
         "flag"]) == 0
    (msg,) = operator_mail.pending(op.OPERATOR_HOME, "beta")
    assert msg["text"] == "--force is the flag"


def test_send_does_not_mistake_message_text_for_a_help_request(idle_recipient,
                                                               capsys):
    """`-h` is only help in first position; anywhere else it is refused.

    Swallowing it as a help request would be the same silent non-send this
    change exists to stop, so the dash-leading word is refused and `--`
    delivers it.
    """
    assert op.send_message(["--from", "alpha", "--to", "beta", "-h", "means",
                            "help"]) == 2
    assert "unknown option '-h'" in capsys.readouterr().err
    assert operator_mail.pending_count(op.OPERATOR_HOME, "beta") == 0

    assert op.send_message(["--from", "alpha", "--to", "beta", "--", "-h",
                            "means", "help"]) == 0
    (msg,) = operator_mail.pending(op.OPERATOR_HOME, "beta")
    assert msg["text"] == "-h means help"


def test_send_refuses_an_unknown_short_option_too(idle_recipient, capsys):
    """A short-flag typo is exactly as silent as a long one."""
    assert op.send_message(["--from", "alpha", "--to", "beta", "-q",
                            "hello"]) == 2
    assert "unknown option '-q'" in capsys.readouterr().err
    assert operator_mail.pending_count(op.OPERATOR_HOME, "beta") == 0


def test_send_help_prints_usage_on_stdout_and_sends_nothing(idle_recipient,
                                                            capsys):
    assert op.send_message(["--help"]) == 0
    out = capsys.readouterr()
    assert "Usage: operator send" in out.out
    assert operator_mail.pending_count(op.OPERATOR_HOME, "beta") == 0


# ── recipient validation ────────────────────────────────────────
def test_unknown_recipient_is_refused_and_lists_real_names(monkeypatch, capsys):
    monkeypatch.setattr(op, "MUX", RecordingMux())
    assert op.send_message(["--from", "a", "--to", "typo", "hi"]) == 1
    out = capsys.readouterr()
    assert "No operator instance 'typo'" in out.err
    assert operator_mail.pending_count(op.OPERATOR_HOME, "typo") == 0


def test_force_queues_for_an_instance_that_has_not_started(monkeypatch):
    monkeypatch.setattr(op, "MUX", RecordingMux())
    assert op.send_message(["--from", "a", "--to", "future", "hi", "--force"]) == 0
    assert operator_mail.pending_count(op.OPERATOR_HOME, "future") == 1


def test_a_tracked_tab_counts_as_a_known_recipient(monkeypatch):
    """An instance registered as a tab is real even with no live session."""
    monkeypatch.setattr(op, "MUX", RecordingMux())
    monkeypatch.setattr(op, "is_copilot_running", lambda _i: False)
    op.TABS_FILE.write_text(json.dumps({"beta": {"display_name": "beta"}}),
                            encoding="utf-8")
    assert op.send_message(["--from", "a", "--to", "beta", "hi"]) == 0


# ── live vs queued ──────────────────────────────────────────────
def test_live_session_is_typed_into_and_not_queued(live_recipient):
    inst, mux = live_recipient
    assert op.send_message(["--from", "alpha", "--to", "beta", "ping"]) == 0
    assert len(mux.sent) == 1
    session, text = mux.sent[0]
    assert session == inst.id
    assert "ping" in text
    assert '"alpha"' in text
    assert "operator send --from beta --to alpha" in text
    # Delivered live, so it must not also arrive in the next preamble.
    assert operator_mail.pending(op.OPERATOR_HOME, inst.id) == []
    assert operator_mail.history(op.OPERATOR_HOME, inst.id)[0]["delivery"] == "live"


def test_queue_flag_forces_the_mailbox_even_when_live(live_recipient):
    inst, mux = live_recipient
    assert op.send_message(["--from", "a", "--to", "beta", "later", "--queue"]) == 0
    assert mux.sent == []
    assert operator_mail.pending_count(op.OPERATOR_HOME, inst.id) == 1


def test_idle_recipient_is_queued(idle_recipient):
    inst, mux = idle_recipient
    assert op.send_message(["--from", "a", "--to", "beta", "later"]) == 0
    assert mux.sent == []
    assert operator_mail.pending_count(op.OPERATOR_HOME, inst.id) == 1


def test_dead_pane_is_queued_not_typed_into(monkeypatch):
    """Between sessions the mux session survives but the pane is dead --
    anything typed there would vanish silently."""
    inst = op.Instance("beta")
    inst.claim("tok")
    mux = RecordingMux(sessions=[inst.id], dead=True)
    monkeypatch.setattr(op, "MUX", mux)
    monkeypatch.setattr(op, "is_copilot_running", lambda _i: True)
    assert op.send_message(["--from", "a", "--to", "beta", "hi"]) == 0
    assert mux.sent == []
    assert operator_mail.pending_count(op.OPERATOR_HOME, inst.id) == 1


def test_live_delivery_failure_falls_back_to_queueing(live_recipient, monkeypatch):
    inst, mux = live_recipient

    def boom(*_a, **_k):
        raise op.MuxError("backend gone")

    monkeypatch.setattr(mux, "send_keys", boom)
    assert op.send_message(["--from", "a", "--to", "beta", "hi"]) == 0
    assert operator_mail.pending_count(op.OPERATOR_HOME, inst.id) == 1


def test_a_backend_that_fails_the_keystroke_queues_rather_than_loses(monkeypatch):
    """End to end with the real Mux, because the fallback above is only as
    good as `send_keys` actually raising. A session can die between the
    liveness check and the keystroke; if the backend's failure is swallowed
    the message is filed to the archive as delivered and never shown to
    anyone -- silently lost mail rather than late mail."""
    import subprocess

    import operator_mux

    inst = op.Instance("beta")
    inst.claim("tok")

    def fake_run(cmd, **_kwargs):
        rc = 1 if cmd[1] == "send-keys" else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="lost pane")

    monkeypatch.setattr(operator_mux.subprocess, "run", fake_run)
    monkeypatch.setattr(op, "MUX", operator_mux.Mux(binary="tmux"))
    monkeypatch.setattr(op, "is_copilot_running", lambda i: i.id == inst.id)

    assert op.send_message(["--from", "a", "--to", "beta", "important"]) == 0
    (queued,) = operator_mail.pending(op.OPERATOR_HOME, inst.id)
    assert queued["text"] == "important"
    assert operator_mail.history(op.OPERATOR_HOME, inst.id) == []


# ── inbox ───────────────────────────────────────────────────────
def test_inbox_reads_and_marks_read(idle_recipient, capsys):
    op.send_message(["--from", "alpha", "--to", "beta", "hello there"])
    assert op.show_inbox(["beta"]) == 0
    out = capsys.readouterr().out
    assert "hello there" in out
    assert "alpha" in out
    assert operator_mail.pending_count(op.OPERATOR_HOME, "beta") == 0


def test_inbox_peek_leaves_messages_pending(idle_recipient):
    op.send_message(["--from", "alpha", "--to", "beta", "hello"])
    assert op.show_inbox(["beta", "--peek"]) == 0
    assert operator_mail.pending_count(op.OPERATOR_HOME, "beta") == 1


def test_inbox_json_is_machine_readable(idle_recipient, capsys):
    op.send_message(["--from", "alpha", "--to", "beta", "hello"])
    capsys.readouterr()
    op.show_inbox(["beta", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["from"] == "alpha"
    assert payload[0]["text"] == "hello"


def test_inbox_history_shows_already_read_messages(idle_recipient, capsys):
    op.send_message(["--from", "alpha", "--to", "beta", "old news"])
    op.show_inbox(["beta"])
    capsys.readouterr()
    op.show_inbox(["beta", "--history"])
    assert "old news" in capsys.readouterr().out


def test_inbox_is_empty_not_an_error_for_a_quiet_instance(idle_recipient, capsys):
    assert op.show_inbox(["beta"]) == 0
    assert "No messages." in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["--peak", "--unraed", "--help-me", "-x"])
def test_inbox_refuses_an_unknown_option_and_keeps_the_mail(bad, idle_recipient,
                                                            capsys):
    """Reading archives what it shows, so a typo must not fall through.

    `--peak` for `--peek` used to be indistinguishable from a plain read: the
    mailbox was consumed and the next reader saw an empty inbox with no way
    to tell that from never having had mail.
    """
    op.send_message(["--from", "alpha", "--to", "beta", "do not eat me"])
    assert operator_mail.pending_count(op.OPERATOR_HOME, "beta") == 1
    capsys.readouterr()

    assert op.show_inbox(["beta", bad]) == 2
    err = capsys.readouterr().err
    assert f"unknown option '{bad}'" in err
    assert "No mail was read." in err

    # Still deliverable, not merely still counted.
    assert op.show_inbox(["beta"]) == 0
    assert "do not eat me" in capsys.readouterr().out


def test_inbox_refuses_two_mailboxes_rather_than_silently_picking_one(
        idle_recipient, capsys):
    op.send_message(["--from", "alpha", "--to", "beta", "still here"])
    assert operator_mail.pending_count(op.OPERATOR_HOME, "beta") == 1
    capsys.readouterr()
    assert op.show_inbox(["beta", "gamma"]) == 2
    assert "one mailbox at a time" in capsys.readouterr().err
    assert op.show_inbox(["beta"]) == 0
    assert "still here" in capsys.readouterr().out


def test_inbox_help_prints_usage_and_reads_nothing(idle_recipient, capsys):
    op.send_message(["--from", "alpha", "--to", "beta", "unread still"])
    assert operator_mail.pending_count(op.OPERATOR_HOME, "beta") == 1
    capsys.readouterr()
    assert op.show_inbox(["--help"]) == 0
    help_out = capsys.readouterr().out
    assert "Usage: operator inbox" in help_out
    assert "unread still" not in help_out
    assert op.show_inbox(["beta"]) == 0
    assert "unread still" in capsys.readouterr().out


def test_inbox_help_from_main_does_not_touch_this_directorys_mailbox(
        idle_recipient, monkeypatch, capsys):
    """`operator inbox --help` used to read (and archive) the default mailbox."""
    monkeypatch.setattr(op, "default_instance_name", lambda: "beta")
    op.send_message(["--from", "alpha", "--to", "beta", "survives help"])
    assert operator_mail.pending_count(op.OPERATOR_HOME, "beta") == 1
    capsys.readouterr()
    assert op.main(["inbox", "--help"]) == 0
    help_out = capsys.readouterr().out
    assert "Usage: operator inbox" in help_out
    assert "survives help" not in help_out
    assert op.main(["inbox"]) == 0
    assert "survives help" in capsys.readouterr().out


def test_inbox_reads_a_mailbox_whose_name_starts_with_a_dash(monkeypatch,
                                                             capsys):
    """Nothing stops an instance being named `-beta`, so `--` must reach it.

    Refusing every dash-leading token is what keeps a typo from eating mail,
    but it would otherwise make such a mailbox unreadable by name.
    """
    monkeypatch.setattr(op, "MUX", RecordingMux())
    monkeypatch.setattr(op, "is_copilot_running", lambda _i: False)
    op.send_message(["--from", "alpha", "--to", "-beta", "--force",
                     "dash named"])
    assert operator_mail.pending_count(op.OPERATOR_HOME, op.Instance("-beta").id) == 1
    capsys.readouterr()

    assert op.show_inbox(["-beta"]) == 2
    assert "operator inbox -- -beta" in capsys.readouterr().err

    assert op.show_inbox(["--", "-beta"]) == 0
    assert "dash named" in capsys.readouterr().out


def test_inbox_refuses_an_empty_mailbox_name(idle_recipient, capsys):
    """An empty name resolves to a real mailbox, so it must not be read.

    Asserting that some *other* instance's mail survived would prove
    nothing: an empty name never pointed at beta in the first place. The
    mailbox at risk is the one `Instance("")` resolves to, so that is the
    one this fills, refuses, and then reads back.
    """
    blank = op.Instance("").id
    operator_mail.queue(op.OPERATOR_HOME,
                        operator_mail.new_message("alpha", "", blank,
                                                  "nobody's mail"))
    assert operator_mail.pending_count(op.OPERATOR_HOME, blank) == 1
    capsys.readouterr()

    assert op.show_inbox(["--", ""]) == 2
    assert "name is empty" in capsys.readouterr().err

    (survivor,) = operator_mail.consume(op.OPERATOR_HOME, blank)
    assert survivor["text"] == "nobody's mail"


def test_inbox_defaults_to_the_instance_for_this_directory(idle_recipient,
                                                           monkeypatch, capsys):
    monkeypatch.setattr(op, "default_instance_name", lambda: "beta")
    op.send_message(["--from", "alpha", "--to", "beta", "implicit"])
    assert op.show_inbox([]) == 0
    assert "implicit" in capsys.readouterr().out


# ── dispatch ────────────────────────────────────────────────────
def test_send_and_inbox_are_reachable_from_main(idle_recipient, capsys):
    assert op.main(["send", "--from", "alpha", "--to", "beta", "via main"]) == 0
    assert op.main(["inbox", "beta"]) == 0
    assert "via main" in capsys.readouterr().out


# ── round trip ──────────────────────────────────────────────────
def test_reply_command_from_a_message_actually_works(idle_recipient, monkeypatch):
    """The reply line printed to the recipient must be a valid command.

    Parsed the way a shell would, so this fails if the hint is not something
    an agent can literally paste.
    """
    alpha = op.Instance("alpha")
    alpha.claim("tok")
    op.send_message(["--from", "alpha", "--to", "beta", "question?"])
    (msg,) = operator_mail.pending(op.OPERATOR_HOME, "beta")
    hint = operator_mail.reply_hint(msg)

    parts = shlex.split(hint)
    assert parts[0] == "operator" and parts[1] == "send"
    args = parts[2:]
    args[-1] = "an answer"
    assert op.send_message(args) == 0
    (reply,) = operator_mail.pending(op.OPERATOR_HOME, "alpha")
    assert reply["from"] == "beta"
    assert reply["text"] == "an answer"


# ── --headless ──────────────────────────────────────────────────
def test_headless_loop_spawns_a_supervisor_without_attaching(monkeypatch, capsys):
    inst = op.Instance("proj")
    mux = RecordingMux(sessions=[inst.id])
    monkeypatch.setattr(op, "MUX", mux)
    monkeypatch.setattr(op, "_running_loop_pid", lambda _i: None)
    spawned = {}

    def fake_spawn(instance, args, is_fresh):
        spawned["name"] = instance.display_name
        spawned["args"] = args
        spawned["fresh"] = is_fresh
        return 4242

    monkeypatch.setattr(op, "_spawn_background_loop", fake_spawn)

    assert op.start_loop_headless(inst, ["--agent", "anvil"], False) == 0
    assert spawned == {"name": "proj", "args": ["--agent", "anvil"], "fresh": False}
    out = capsys.readouterr().out
    assert "4242" in out
    assert "operator join proj" in out


def test_headless_loop_reports_a_launch_that_never_starts(monkeypatch, capsys):
    inst = op.Instance("proj")
    monkeypatch.setattr(op, "MUX", RecordingMux(sessions=[]))
    monkeypatch.setattr(op, "_running_loop_pid", lambda _i: None)
    monkeypatch.setattr(op, "_spawn_background_loop", lambda *_a: 1)
    monkeypatch.setattr(op, "SESSION_ID_WAIT", 0)
    assert op.start_loop_headless(inst, [], False) == 1
    assert "Timed out" in capsys.readouterr().err


def test_headless_loop_does_not_start_a_second_supervisor(monkeypatch, capsys):
    inst = op.Instance("proj")
    monkeypatch.setattr(op, "MUX", RecordingMux(sessions=[inst.id]))
    monkeypatch.setattr(op, "_running_loop_pid", lambda _i: 99)
    monkeypatch.setattr(op, "_spawn_background_loop",
                        lambda *_a: pytest.fail("must not respawn"))
    assert op.start_loop_headless(inst, [], False) == 0
    assert "already running" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("flag", ["--headless", "--detached"])
def test_dispatch_routes_headless_loops(monkeypatch, flag):
    seen = {}

    def fake_headless(instance, args, is_fresh):
        seen["headless"] = (instance.display_name, args, is_fresh)
        return 0

    monkeypatch.setattr(op, "MUX", RecordingMux())
    monkeypatch.setattr(op, "register_tab", lambda *_a: None)
    monkeypatch.setattr(op, "start_loop_headless", fake_headless)
    monkeypatch.setattr(op, "start_and_attach_loop",
                        lambda *_a: pytest.fail("headless must not attach"))
    assert op.run_dispatch(["--loop", flag, "--name", "proj", "--agent", "anvil"]) == 0
    assert seen["headless"] == ("proj", ["--agent", "anvil"], False)


def test_dispatch_still_attaches_without_the_flag(monkeypatch):
    seen = {}

    def fake_attach(instance, args, is_fresh):
        seen["attached"] = instance.display_name
        return 0

    monkeypatch.setattr(op, "MUX", RecordingMux())
    monkeypatch.setattr(op, "register_tab", lambda *_a: None)
    monkeypatch.setattr(op, "start_and_attach_loop", fake_attach)
    monkeypatch.setattr(op, "start_loop_headless",
                        lambda *_a: pytest.fail("must not go headless"))
    assert op.run_dispatch(["--loop", "--name", "proj"]) == 0
    assert seen["attached"] == "proj"


def test_headless_is_not_passed_through_to_copilot(monkeypatch):
    seen = {}

    def fake_headless(_instance, args, _is_fresh):
        seen["args"] = args
        return 0

    monkeypatch.setattr(op, "MUX", RecordingMux())
    monkeypatch.setattr(op, "register_tab", lambda *_a: None)
    monkeypatch.setattr(op, "start_loop_headless", fake_headless)
    op.run_dispatch(["--loop", "--headless", "--name", "proj"])
    assert seen["args"] == []


def test_single_session_headless_starts_without_attaching(monkeypatch, capsys):
    monkeypatch.setattr(op, "MUX", RecordingMux())
    monkeypatch.setattr(op, "register_tab", lambda *_a: None)
    monkeypatch.setattr(op, "handle_existing_session", lambda _i: None)
    monkeypatch.setattr(op.operator_ingest, "init_db", lambda _p: None)
    monkeypatch.setattr(op, "start_session", lambda *_a, **_k: None)
    assert op.run_dispatch(["--headless", "--name", "solo"]) == 0
    assert "operator join solo" in capsys.readouterr().out


# ── queued mail reaches the next session ────────────────────────
def test_queued_mail_is_appended_to_the_next_session_preamble(idle_recipient):
    inst, _ = idle_recipient
    op.send_message(["--from", "alpha", "--to", "beta", "do the thing"])
    waiting = operator_mail.pending(op.OPERATOR_HOME, inst.id)
    block = operator_mail.render_for_agent(waiting)
    base = op.build_preamble("anvil", inst)
    combined = base + block
    assert "do the thing" in combined
    assert "operator send --from beta --to alpha" in combined
    assert "\n" not in block


def test_mail_is_only_archived_after_the_session_starts(idle_recipient):
    """A launch that fails is retried, so its mail must still be pending."""
    inst, _ = idle_recipient
    op.send_message(["--from", "alpha", "--to", "beta", "survive the retry"])
    waiting = operator_mail.pending(op.OPERATOR_HOME, inst.id)
    assert operator_mail.pending_count(op.OPERATOR_HOME, inst.id) == 1
    operator_mail.archive(op.OPERATOR_HOME, inst.id, [m["id"] for m in waiting])
    assert operator_mail.pending_count(op.OPERATOR_HOME, inst.id) == 0
