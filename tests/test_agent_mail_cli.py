"""Tests for the `operator send` / `operator inbox` commands and --headless.

These cover the CLI seam: name validation, the live-vs-queued decision, and
that the loop hands queued mail to the next session. The storage layer itself
is covered in test_mail.py.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
from pathlib import Path

import pytest
from conftest import denied

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

    def __init__(self, sessions=(), dead=False, paths=None, installed=True):
        self.sessions = set(sessions)
        self.dead = dead
        self.sent: list[tuple[str, str]] = []
        # Where each session's pane is currently sitting, as `Mux` reports it.
        self.paths = dict(paths or {})
        self.installed = installed

    def _require_backend(self):
        """The real Mux resolves its binary lazily and raises when there is
        none, so a double that answers questions an uninstalled multiplexer
        could not answer would hide exactly the bugs this models."""
        if not self.installed:
            raise op.MuxNotFoundError("no multiplexer installed")

    def available(self):
        return self.installed

    def has_session(self, name):
        self._require_backend()
        return name in self.sessions

    def list_sessions(self):
        self._require_backend()
        return sorted(self.sessions)

    def pane_current_path(self, name):
        self._require_backend()
        return self.paths.get(name)

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


def test_inbox_that_cannot_be_read_does_not_report_no_messages(idle_recipient,
                                                                capsys,
                                                                monkeypatch):
    """The whole failure, at the seam where a human or an agent reads it.

    The test directly above is the control and the two must not agree: a quiet
    mailbox prints "No messages." and exits 0, and until this was fixed a
    mailbox that could not be opened printed the same words and the same code
    while holding mail. `Path.glob` swallows `PermissionError` and stops
    yielding, so every layer above it saw an ordinary empty sequence.

    That is the one lie this command cannot tell. An agent reads its inbox
    once at the start of a session and believes the answer -- so a peer that
    is blocked on a reply stays blocked, and nothing anywhere reports a fault.
    """
    op.send_message(["--from", "alpha", "--to", "beta", "please reply"])
    assert operator_mail.pending_count(op.OPERATOR_HOME, "beta") == 1
    inbox = operator_mail.inbox_dir(op.OPERATOR_HOME, "beta")
    real_scandir = os.scandir
    denying = {"on": True}

    def denied_scandir(path=".", *args, **kwargs):
        if denying["on"] and Path(path) == inbox:
            raise PermissionError(13, "Permission denied")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(operator_mail.os, "scandir", denied_scandir)
    capsys.readouterr()

    assert op.show_inbox(["beta"]) == 1
    captured = capsys.readouterr()
    assert "No messages." not in captured.out + captured.err
    assert "could not read mail" in captured.err

    # Nothing was consumed on the way past, so the message is still there.
    # The denial is lifted with a flag rather than `monkeypatch.undo()`, which
    # would also revert the autouse fixture's OPERATOR_HOME and quietly assert
    # against the real mailbox instead of this test's.
    denying["on"] = False
    assert operator_mail.pending_count(op.OPERATOR_HOME, "beta") == 1
    assert [m["text"] for m in operator_mail.pending(op.OPERATOR_HOME, "beta")] \
        == ["please reply"]


def test_send_still_reports_success_when_the_pending_count_cannot_be_read(
        idle_recipient, capsys, monkeypatch):
    """The message is already queued by then; the count is a courtesy.

    Letting the refusal escape would turn a delivered message into a reported
    failure, and the caller's repair for that is to send it again.
    """
    inbox = operator_mail.inbox_dir(op.OPERATOR_HOME, "beta")
    real_scandir = os.scandir
    denying = {"on": True}

    def denied_scandir(path=".", *args, **kwargs):
        if denying["on"] and Path(path) == inbox:
            raise PermissionError(13, "Permission denied")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(operator_mail.os, "scandir", denied_scandir)
    assert op.send_message(["--from", "alpha", "--to", "beta", "queued anyway"]) == 0
    out = capsys.readouterr().out
    assert "Pending: unknown" in out
    assert "Pending: 0" not in out

    denying["on"] = False
    assert [m["text"] for m in operator_mail.pending(op.OPERATOR_HOME, "beta")] \
        == ["queued anyway"]


def test_send_reports_a_failure_when_the_recipients_mailbox_is_not_a_directory(
        idle_recipient, capsys):
    """The fault is at the far end, so the sender must be told whose it is.

    `_write`'s `mkdir(exist_ok=True)` forgives a directory but not a plain
    file, so this used to end in a raw `FileExistsError` traceback naming a
    path the sender has no reason to recognise -- and which reads like a bug
    in `operator send` rather than a broken mailbox belonging to somebody
    else. Nothing is stored, so unlike the pending-count case this has to be
    a non-zero exit: a success here would lose the message silently.
    """
    inbox = operator_mail.inbox_dir(op.OPERATOR_HOME, "beta")
    if inbox.exists():
        shutil.rmtree(inbox)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text("not a directory", encoding="utf-8")

    assert op.send_message(["--from", "alpha", "--to", "beta", "undeliverable"]) == 1
    captured = capsys.readouterr()
    assert "Could not queue for" in captured.err
    assert "Queued for" not in captured.out
    assert "Traceback" not in captured.err


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


# ── a derived mailbox name is nobody's name ─────────────────────
# `operator inbox` with no NAME asks default_instance_name(), which answers
# "what would a session started here be called" -- the directory's name. In a
# checkout shared by several agents that names nobody, and reading consumes.
def _register(name, launch=None, managed=True):
    """Register an instance the way a real launch does.

    The launch spec is written by `write_launch_spec`, the same writer the
    operator uses, so the recorded-directory lookup is exercised for real
    rather than against a fixture shaped like the code under test.
    """
    inst = op.Instance(name)
    if managed:
        inst.claim("tok")
    if launch is not None:
        op.write_launch_spec(inst, [], Path(launch), 1)
    return inst


def _shared_checkout(tmp_path, monkeypatch, live, cwd_name="proj"):
    """Stand in a project directory with `live` instances running.

    `live` maps display name -> pane directory, or -> (pane, launch) when the
    two differ. A pane of None models a session the backend cannot place.
    """
    project = tmp_path / cwd_name
    project.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, where in live.items():
        pane, launch = where if isinstance(where, tuple) else (where, where)
        inst = _register(name, launch=launch)
        paths[inst.id] = None if pane is None else str(pane)
    mux = RecordingMux(sessions=list(paths), paths=paths)
    monkeypatch.setattr(op, "MUX", mux)
    monkeypatch.setattr(op, "is_copilot_running", lambda _i: False)
    monkeypatch.chdir(project)
    return project


def _mail_for(name, text):
    operator_mail.queue(
        op.OPERATOR_HOME,
        operator_mail.new_message("someone", name, op.Instance(name).id, text))


def test_bare_inbox_refuses_when_the_derived_name_names_nobody_live(
        tmp_path, monkeypatch, capsys):
    """The case that actually fired: a peer working in a worktree.

    The reader stood in the primary checkout, so the derived name was the
    folder's -- `proj` -- which is not the peer, not the reader, not anyone.
    A rule that only refused when two live instances *answered to* the
    directory would have allowed this read.
    """
    project = tmp_path / "proj"
    (project / ".worktrees" / "feature").mkdir(parents=True)
    _shared_checkout(tmp_path, monkeypatch,
                     {"agent-x": project / ".worktrees" / "feature"})
    _mail_for("proj", "the folder's mail")
    capsys.readouterr()

    assert op.show_inbox([]) == 2
    err = capsys.readouterr().err
    assert "refusing to consume mail for 'proj'" in err
    assert "agent-x" in err
    assert "No mail was read." in err

    # Still deliverable, not merely still counted.
    assert op.show_inbox(["proj"]) == 0
    assert "the folder's mail" in capsys.readouterr().out


def test_bare_inbox_refuses_when_two_instances_are_live_here(tmp_path,
                                                             monkeypatch,
                                                             capsys):
    _shared_checkout(tmp_path, monkeypatch,
                     {"proj": tmp_path / "proj",
                      "agent-x": tmp_path / "proj"})
    _mail_for("proj", "contested mail")
    capsys.readouterr()

    assert op.show_inbox([]) == 2
    err = capsys.readouterr().err
    assert "agent-x" in err
    # The derived instance is live too, and saying otherwise would read as
    # the operator having lost track of a session the user can plainly see.
    assert "proj" in err
    assert "operator inbox --peek" in err

    assert op.show_inbox(["proj"]) == 0
    assert "contested mail" in capsys.readouterr().out


def test_bare_inbox_json_is_refused_too_and_prints_no_payload(tmp_path,
                                                              monkeypatch,
                                                              capsys):
    """consume() runs before the --json branch, so --json archives as well.

    The guard keys off destructiveness rather than output format. A refusal
    must also print nothing on stdout: a caller parsing the output would
    otherwise read a stray `[]` as "you have no mail".
    """
    _shared_checkout(tmp_path, monkeypatch, {"agent-x": tmp_path / "proj"})
    _mail_for("proj", "machine readable mail")
    capsys.readouterr()

    assert op.show_inbox(["--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refusing to consume" in captured.err

    assert op.show_inbox(["proj", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["text"] == "machine readable mail"


def test_bare_inbox_reads_when_only_the_derived_instance_is_live(tmp_path,
                                                                 monkeypatch,
                                                                 capsys):
    """The unambiguous case still works: the one live instance here IS the
    name the directory derives, so nobody else can be the addressee."""
    _shared_checkout(tmp_path, monkeypatch, {"proj": tmp_path / "proj"})
    _mail_for("proj", "mine to read")
    capsys.readouterr()

    assert op.show_inbox([]) == 0
    assert "mine to read" in capsys.readouterr().out
    assert operator_mail.pending_count(op.OPERATOR_HOME,
                                       op.Instance("proj").id) == 0


def test_a_sanitized_directory_name_is_not_mistaken_for_a_stranger(
        tmp_path, monkeypatch, capsys):
    """A folder whose name has to be sanitized must still read its own mail.

    `safe_instance_id` appends a digest when sanitizing changes the name, and
    it is not idempotent. With no `.managed` file the census has only the
    session id, and comparing a re-derived id against the real one makes an
    instance fail to recognize itself -- so the only live instance in the
    directory is refused as if it were somebody else.
    """
    project = tmp_path / "my.repo"
    project.mkdir()
    mine = _register("my.repo", launch=project, managed=False)
    assert mine.id != "my.repo", "test needs a name that sanitizing changes"
    mux = RecordingMux(sessions=[mine.id], paths={mine.id: str(project)})
    monkeypatch.setattr(op, "MUX", mux)
    monkeypatch.setattr(op, "is_copilot_running", lambda _i: False)
    monkeypatch.chdir(project)
    _mail_for("my.repo", "still mine")
    capsys.readouterr()

    assert op.show_inbox([]) == 0
    assert "still mine" in capsys.readouterr().out


def test_a_peer_with_no_ownership_metadata_is_still_counted(tmp_path,
                                                            monkeypatch,
                                                            capsys):
    """A live peer whose `.managed` file is missing must not vanish.

    Without ownership metadata the census has only the session id to work
    with. Treating that id as a display name and re-deriving an id from it
    produces a third, non-existent instance whose recorded directory is
    always unknown -- so the peer drops out of the census and its mail is
    eaten. The peer here is also `cd`'d elsewhere, so only its launch spec
    places it.
    """
    project = tmp_path / "proj"
    project.mkdir()
    peer = _register("agent.x", launch=project, managed=False)
    assert op.safe_instance_id(peer.id) != peer.id, "test needs a digest id"
    mux = RecordingMux(sessions=[peer.id],
                       paths={peer.id: str(tmp_path / "elsewhere")})
    monkeypatch.setattr(op, "MUX", mux)
    monkeypatch.setattr(op, "is_copilot_running", lambda _i: False)
    monkeypatch.chdir(project)
    _mail_for("proj", "not yours to read")
    capsys.readouterr()

    assert op.show_inbox([]) == 2
    assert "refusing to consume" in capsys.readouterr().err
    assert op.show_inbox(["proj"]) == 0
    assert "not yours to read" in capsys.readouterr().out


def test_the_derived_name_is_refused_when_it_is_live_somewhere_else(
        tmp_path, monkeypatch, capsys):
    """Two checkouts can share a folder name, and one global mailbox.

    `proj` here and `proj` in another directory derive the same name and so
    share one mailbox. When the operator cannot say where the live `proj` is
    bound, `default_instance_name` sees no conflict and hands back the plain
    name -- so the reader here would consume the mail of the `proj` running
    over there.
    """
    project = tmp_path / "proj"
    project.mkdir()
    other = tmp_path / "elsewhere" / "proj"
    other.mkdir(parents=True)
    stranger = _register("proj", launch=None)  # live, but nothing records where
    mux = RecordingMux(sessions=[stranger.id], paths={stranger.id: str(other)})
    monkeypatch.setattr(op, "MUX", mux)
    monkeypatch.setattr(op, "is_copilot_running", lambda _i: False)
    monkeypatch.chdir(project)
    _mail_for("proj", "the other proj's mail")
    capsys.readouterr()

    assert op.show_inbox([]) == 2
    assert "not working in this directory" in capsys.readouterr().err
    assert op.show_inbox(["proj"]) == 0
    assert "the other proj's mail" in capsys.readouterr().out


def test_bare_inbox_reads_when_nothing_is_live_here(tmp_path, monkeypatch,
                                                    capsys):
    """Nothing live in this directory means nothing to disambiguate against
    -- the single-user workflow the default was written for."""
    _shared_checkout(tmp_path, monkeypatch, {})
    _mail_for("proj", "quiet checkout")
    capsys.readouterr()

    assert op.show_inbox([]) == 0
    assert "quiet checkout" in capsys.readouterr().out


def test_a_live_instance_in_an_unrelated_directory_does_not_block(
        tmp_path, monkeypatch, capsys):
    _shared_checkout(tmp_path, monkeypatch,
                     {"agent-x": tmp_path / "somewhere-else"})
    _mail_for("proj", "unblocked")
    capsys.readouterr()

    assert op.show_inbox([]) == 0
    assert "unblocked" in capsys.readouterr().out


def test_an_explicit_name_is_read_even_with_peers_live_here(tmp_path,
                                                            monkeypatch,
                                                            capsys):
    """The refusal is about a *guessed* name. Saying who you are always
    works -- otherwise the fix would break the very habit it is teaching."""
    _shared_checkout(tmp_path, monkeypatch,
                     {"agent-x": tmp_path / "proj",
                      "proj": tmp_path / "proj"})
    _mail_for("agent-x", "addressed to me")
    capsys.readouterr()

    assert op.show_inbox(["agent-x"]) == 0
    assert "addressed to me" in capsys.readouterr().out
    assert operator_mail.pending_count(op.OPERATOR_HOME,
                                       op.Instance("agent-x").id) == 0


@pytest.mark.parametrize("flag", ["--peek", "--history"])
def test_non_destructive_reads_are_never_refused(flag, tmp_path, monkeypatch,
                                                 capsys):
    _shared_checkout(tmp_path, monkeypatch, {"agent-x": tmp_path / "proj"})
    _mail_for("proj", "looked at, not eaten")
    capsys.readouterr()

    assert op.show_inbox([flag]) == 0
    assert "refusing" not in capsys.readouterr().err
    assert operator_mail.pending_count(op.OPERATOR_HOME,
                                       op.Instance("proj").id) == 1


def test_the_refusal_is_reachable_from_the_command_line(tmp_path, monkeypatch,
                                                        capsys):
    """Through main(), because a guard the dispatcher bypasses guards nothing."""
    _shared_checkout(tmp_path, monkeypatch, {"agent-x": tmp_path / "proj"})
    _mail_for("proj", "survives the dispatcher")
    capsys.readouterr()

    assert op.main(["inbox"]) == 2
    assert "refusing to consume" in capsys.readouterr().err
    assert op.main(["inbox", "proj"]) == 0
    assert "survives the dispatcher" in capsys.readouterr().out


# ── the census itself ───────────────────────────────────────────
def test_live_instances_under_counts_a_pane_that_has_wandered(tmp_path,
                                                              monkeypatch):
    """An agent that `cd`'d to a temp directory is still that project's agent.

    The launch directory is recorded in the instance's spec file, so it is
    the second source consulted. Only counting the pane's current path would
    let a peer disappear from the census mid-`cd`.
    """
    project = tmp_path / "proj"
    project.mkdir()
    peer = _register("agent-x", launch=project)
    mux = RecordingMux(sessions=[peer.id],
                       paths={peer.id: str(tmp_path / "elsewhere")})
    monkeypatch.setattr(op, "MUX", mux)

    assert op.live_instance_ids_under(project) == [peer.id]


def test_an_operator_session_nobody_can_place_is_counted(tmp_path, monkeypatch):
    """Unknown location is not the same as "somewhere else"."""
    project = tmp_path / "proj"
    project.mkdir()
    peer = _register("agent-x", launch=None)
    mux = RecordingMux(sessions=[peer.id], paths={peer.id: None})
    monkeypatch.setattr(op, "MUX", mux)

    assert op.live_instance_ids_under(project) == [peer.id]


def test_an_unrelated_backend_session_is_not_counted(tmp_path, monkeypatch):
    """A plain tmux session the user opened is not an operator instance and
    has no mailbox, so it must not make the operator refuse everything."""
    project = tmp_path / "proj"
    project.mkdir()
    mux = RecordingMux(sessions=["notes"], paths={"notes": None})
    monkeypatch.setattr(op, "MUX", mux)

    assert op.live_instance_ids_under(project) == []


def test_a_census_that_cannot_be_taken_is_not_an_empty_census(tmp_path,
                                                              monkeypatch):
    class Broken(RecordingMux):
        def list_sessions(self):
            raise op.MuxError("backend gone")

    monkeypatch.setattr(op, "MUX", Broken())
    assert op.live_instance_ids_under(tmp_path) is None


def test_an_instance_the_backend_will_not_place_fails_the_census(tmp_path,
                                                                 monkeypatch):
    """A hole in the census is not a clean bill of health.

    Swallowing a failed pane lookup and carrying on returns a list that looks
    complete and is not: the peer it could not place simply is not in it, and
    a caller reading that list concludes nobody else is here.
    """
    project = tmp_path / "proj"
    project.mkdir()
    peer = _register("agent-x", launch=tmp_path / "elsewhere")

    class Unhelpful(RecordingMux):
        def pane_current_path(self, name):
            raise op.MuxError("cannot reach pane")

    monkeypatch.setattr(op, "MUX", Unhelpful(sessions=[peer.id]))
    assert op.live_instance_ids_under(project) is None


def test_a_failed_pane_lookup_still_counts_a_peer_recorded_here(tmp_path,
                                                                monkeypatch):
    """When the launch spec already places an instance in this directory,
    the pane lookup adds nothing -- there is no uncertainty left to report."""
    project = tmp_path / "proj"
    project.mkdir()
    peer = _register("agent-x", launch=project)

    class Unhelpful(RecordingMux):
        def pane_current_path(self, name):
            raise op.MuxError("cannot reach pane")

    monkeypatch.setattr(op, "MUX", Unhelpful(sessions=[peer.id]))
    assert op.live_instance_ids_under(project) == [peer.id]


def test_no_multiplexer_means_no_live_sessions_rather_than_no_answer(
        tmp_path, monkeypatch):
    monkeypatch.setattr(op, "MUX", RecordingMux(installed=False))
    assert op.live_instance_ids_under(tmp_path) == []


def test_a_launch_spec_that_will_not_open_fails_the_census(tmp_path,
                                                           monkeypatch):
    """The record exists and cannot be read: that is not "unrecorded".

    A peer whose pane sits elsewhere is placed by its launch spec. If reading
    that spec fails and the failure is typed as "no recorded directory", the
    peer drops out of the census and the list that comes back looks complete
    -- the same hole as a swallowed pane lookup, one record further down.
    """
    project = tmp_path / "proj"
    project.mkdir()
    peer = _register("agent-x", launch=project)
    spec = op.RESTART_DIR / f"{peer.id}.launch.json"
    mux = RecordingMux(sessions=[peer.id],
                       paths={peer.id: str(tmp_path / "elsewhere")})
    monkeypatch.setattr(op, "MUX", mux)

    with denied(monkeypatch, spec) as seen:
        assert op.live_instance_ids_under(project) is None
    assert seen["n"] > 0, "the denial never fired; the test proves nothing"


def test_a_pane_that_places_a_peer_here_does_not_need_the_spec(tmp_path,
                                                               monkeypatch):
    """The counterpart: an unreadable spec is only uncertainty when it is the
    thing that would have answered. A pane already in this directory settles
    it, so the census must not refuse."""
    project = tmp_path / "proj"
    project.mkdir()
    peer = _register("agent-x", launch=project)
    spec = op.RESTART_DIR / f"{peer.id}.launch.json"
    mux = RecordingMux(sessions=[peer.id], paths={peer.id: str(project)})
    monkeypatch.setattr(op, "MUX", mux)

    with denied(monkeypatch, spec):
        assert op.live_instance_ids_under(project) == [peer.id]


def test_an_unreadable_tab_registry_fails_the_census(tmp_path, monkeypatch):
    """``load_tabs`` reports an unreadable registry as an empty one. The
    census asks a different question -- who is here -- and an empty answer to
    that is a claim, so it has to consult the tri-state read instead."""
    project = tmp_path / "proj"
    project.mkdir()
    peer = _register("agent-x", launch=None)
    op.TABS_FILE.write_text(json.dumps({peer.id: {"cwd": str(project)}}),
                            encoding="utf-8")
    mux = RecordingMux(sessions=[peer.id],
                       paths={peer.id: str(tmp_path / "elsewhere")})
    monkeypatch.setattr(op, "MUX", mux)

    real_read_text = Path.read_text
    seen = {"n": 0}

    def unreadable(self, *args, **kwargs):
        if str(self) == str(op.TABS_FILE):
            seen["n"] += 1
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    try:
        assert op.live_instance_ids_under(project) is None
    finally:
        monkeypatch.setattr(Path, "read_text", real_read_text)
    assert seen["n"] > 0, "the denial never fired; the test proves nothing"


def test_an_unexaminable_state_directory_fails_the_census(tmp_path,
                                                          monkeypatch):
    """`known` decides whether an unplaceable session is one of ours. If the
    state directory cannot be listed, `known` shrinks silently and the answer
    is drawn from a population that is missing members."""
    project = tmp_path / "proj"
    project.mkdir()
    peer = _register("agent-x", launch=None)
    mux = RecordingMux(sessions=[peer.id], paths={peer.id: None})
    monkeypatch.setattr(op, "MUX", mux)

    with denied(monkeypatch, op.RESTART_DIR) as seen:
        assert op.live_instance_ids_under(project) is None
    assert seen["n"] > 0, "the denial never fired; the test proves nothing"


def test_a_state_directory_that_will_not_list_fails_the_census(tmp_path,
                                                               monkeypatch):
    """Statting the directory is not reading it.

    ``managed_instances`` swallowed a failed ``iterdir`` into an empty map, so
    a directory that stats fine and lists EACCES still produced a confident
    census drawn from a population missing its members.
    """
    project = tmp_path / "proj"
    project.mkdir()
    peer = _register("agent-x", launch=None)
    mux = RecordingMux(sessions=[peer.id], paths={peer.id: None})
    monkeypatch.setattr(op, "MUX", mux)
    assert op.live_instance_ids_under(project) == [peer.id]

    real_iterdir = Path.iterdir
    seen = {"n": 0}

    def unlistable(self):
        if str(self) == str(op.RESTART_DIR):
            seen["n"] += 1
            raise PermissionError(13, "Permission denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", unlistable)
    try:
        assert op.live_instance_ids_under(project) is None
    finally:
        monkeypatch.setattr(Path, "iterdir", real_iterdir)
    assert seen["n"] > 0, "the denial never fired; the test proves nothing"


def test_a_name_bound_somewhere_we_cannot_read_is_treated_as_taken(
        tmp_path, monkeypatch):
    """`_name_conflicts` guards against two agents sharing a name. An
    unreadable binding is not proof the name is free."""
    project = tmp_path / "proj"
    project.mkdir()
    peer = _register("agent-x", launch=project)
    spec = op.RESTART_DIR / f"{peer.id}.launch.json"
    monkeypatch.setattr(op, "MUX", RecordingMux(sessions=[peer.id]))

    assert op._name_conflicts("agent-x", project) is False
    with denied(monkeypatch, spec, op.TABS_FILE):
        assert op._name_conflicts("agent-x", project) is True



def test_a_machine_with_no_multiplexer_can_still_read_its_own_mail(
        tmp_path, monkeypatch, capsys):
    """The no-backend carve-out has to survive the whole guard, not just the
    census. Every probe must agree that "not installed" is knowledge -- one
    that treats it as uncertainty refuses every nameless read on a machine
    with no tmux or psmux, which is most single-user machines. The double
    raises `MuxNotFoundError` exactly where the real `Mux` would, because a
    double that answers anyway cannot fail this test.
    """
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(op, "MUX", RecordingMux(installed=False))
    monkeypatch.setattr(op, "is_copilot_running", lambda _i: False)
    monkeypatch.chdir(project)
    _mail_for("proj", "no mux, still mine")
    capsys.readouterr()

    assert op.show_inbox([]) == 0
    assert "no mux, still mine" in capsys.readouterr().out


@pytest.mark.parametrize("blowup", [
    lambda: op.MuxError("backend gone"),
    lambda: FileNotFoundError(2, "No such file or directory: 'tmux'"),
])
def test_a_backend_that_cannot_be_asked_refuses_rather_than_consumes(
        blowup, tmp_path, monkeypatch, capsys):
    """"I could not look" must not read the same as "nobody is here".

    A missing mux binary raises FileNotFoundError rather than MuxError, and
    an uncaught one used to take the whole command down; a caught-but-ignored
    one let the read proceed under exactly the uncertainty the guard exists
    to handle.
    """
    project = tmp_path / "proj"
    project.mkdir()

    class Broken(RecordingMux):
        def list_sessions(self):
            raise blowup()

    monkeypatch.setattr(op, "MUX", Broken())
    monkeypatch.setattr(op, "is_copilot_running", lambda _i: False)
    monkeypatch.chdir(project)
    _mail_for("proj", "safe under uncertainty")
    capsys.readouterr()

    assert op.show_inbox([]) == 2
    assert "could not be asked" in capsys.readouterr().err
    assert op.show_inbox(["proj"]) == 0
    assert "safe under uncertainty" in capsys.readouterr().out


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


def test_inbox_prints_the_mail_it_already_marked_read_when_consume_fails(
    isolated_state, monkeypatch, capsys
):
    """A partial `consume` must not lose the messages it already archived.

    `consume` archives one message at a time, so a fault mid-batch leaves the
    earlier ones read. The handler used to print "Nothing has been marked
    read" on that path -- a sentence asserting an outcome nobody had checked
    -- and those messages were then archived, unread, permanently. They are
    printed here because it is the only time they will ever be offered.
    """
    tmp = isolated_state
    for text in ("first message", "second message"):
        operator_mail.queue(tmp, operator_mail.new_message(
            "alpha", "beta", "beta", text))

    real_write_json = operator_mail._write_json
    calls: list[dict] = []

    def fail_on_the_second(path, msg):
        calls.append(msg)
        if len(calls) == 2:
            raise OSError("disk full")
        return real_write_json(path, msg)

    monkeypatch.setattr(operator_mail, "_write_json", fail_on_the_second)
    rc = op.show_inbox(["beta"])
    out = capsys.readouterr()
    combined = out.out + out.err

    assert rc == 1, "a failed read is not a success"
    assert "Nothing has been marked read" not in combined, (
        "that claim is false here: a message was archived before the failure")
    assert "HAD already been marked read" in combined
    # The message itself, not merely a count of it.
    consumed_text = calls[0]["text"]
    assert consumed_text in combined, (
        f"{consumed_text!r} was marked read and never shown to anybody")
