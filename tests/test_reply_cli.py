"""Tests for `operator reply` and for mail delivered by `operator session start`.

Together these are the two halves of retiring the polling mailbox. The old
model had the agent remember a command (`operator inbox`) whose omission was
indistinguishable from having no mail, and answer with a command that
restated both addresses. What replaces it is delivery at session start and a
reply that resolves its own addresses -- so both halves are tested for the
same property: what happens when the resolution *fails* must never be a
plausible-looking guess.

The storage layer is covered in test_mail.py and the send CLI in
test_agent_mail_cli.py; what is new here is the two lookups and the delivery
point.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

import copilot_operator as op
import operator_mail
import operator_session as osess


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
    # No instance name in the environment unless a test puts one there.
    monkeypatch.delenv("OPERATOR_INSTANCE", raising=False)
    return tmp_path


class QuietMux:
    """A multiplexer with no sessions, so every send takes the queue path.

    Live delivery is `operator send`'s behaviour and is covered where that
    lives. Forcing the queue here keeps these tests about resolution: the
    queued file is what makes the resolved sender and recipient inspectable.
    """

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def available(self):
        return True

    def has_session(self, _name):
        return False

    def list_sessions(self):
        return []

    def pane_dead(self, _name):
        return True

    def pane_current_path(self, _name):
        return None

    def send_keys(self, session, text):
        self.sent.append((session, text))


@pytest.fixture
def peers(monkeypatch):
    """`alpha` and `beta`, both known to the operator, neither running."""
    alpha = op.Instance("alpha")
    alpha.claim("tok")
    beta = op.Instance("beta")
    beta.claim("tok")
    mux = QuietMux()
    monkeypatch.setattr(op, "MUX", mux)
    monkeypatch.setattr(op, "is_copilot_running", lambda _i: False)
    return alpha, beta, mux


def _wrote(sender: str, recipient: str, text: str = "question?") -> None:
    """Put a message from `sender` in `recipient`'s inbox."""
    assert op.send_message(["--from", sender, "--to", recipient, text]) == 0


def _queued(name: str) -> list[dict]:
    return operator_mail.pending(op.OPERATOR_HOME, op.Instance(name).id)


# ── last_correspondent ──────────────────────────────────────────
def test_nobody_written_to_yet_has_no_correspondent(peers) -> None:
    assert operator_mail.last_correspondent(
        op.OPERATOR_HOME, op.Instance("beta").id) is None


def test_the_pending_sender_is_the_correspondent(peers) -> None:
    _wrote("alpha", "beta")
    assert operator_mail.last_correspondent(
        op.OPERATOR_HOME, op.Instance("beta").id) == "alpha"


def test_a_read_message_still_names_the_correspondent(peers) -> None:
    """Consuming the inbox must not lose the answer.

    `session start` archives the whole inbox, so if this looked only at
    pending mail then replying would be impossible in exactly the session the
    message was delivered to -- which is every session.
    """
    _wrote("alpha", "beta")
    operator_mail.consume(op.OPERATOR_HOME, op.Instance("beta").id)
    assert not _queued("beta")
    assert operator_mail.last_correspondent(
        op.OPERATOR_HOME, op.Instance("beta").id) == "alpha"


def test_the_most_recent_sender_wins_across_inbox_and_archive(peers) -> None:
    """Ordering is by sent_at, not by which directory a message sits in.

    An archived message is normally older than a pending one, but a live
    delivery is archived immediately -- so a message read at 10:00 can be
    newer than one still queued from 09:00. Concatenating the directories
    would answer with whichever list happened to come last.
    """
    root = op.OPERATOR_HOME
    beta = op.Instance("beta").id
    old = operator_mail.new_message("gamma", "beta", beta, "older")
    old["sent_at"] = "2026-01-01T00:00:00Z"
    operator_mail.queue(root, old)
    new = operator_mail.new_message("alpha", "beta", beta, "newer")
    new["sent_at"] = "2026-06-01T00:00:00Z"
    operator_mail.record_delivered(root, new)

    assert operator_mail.last_correspondent(root, beta) == "alpha"


def test_a_message_with_no_usable_sender_is_skipped(peers) -> None:
    """A blank name must not become the recipient of somebody's reply.

    `reply_hint` can print `<sender>` because a human reads it first. A
    resolved recipient goes straight to delivery.
    """
    root = op.OPERATOR_HOME
    beta = op.Instance("beta").id
    good = operator_mail.new_message("alpha", "beta", beta, "real")
    good["sent_at"] = "2026-01-01T00:00:00Z"
    operator_mail.queue(root, good)
    for i, bad_name in enumerate([None, "", "   ", "\n\r"]):
        broken = operator_mail.new_message("x", "beta", beta, "broken")
        broken["from"] = bad_name
        broken["sent_at"] = f"2026-06-0{i + 1}T00:00:00Z"
        operator_mail.queue(root, broken)

    assert operator_mail.last_correspondent(root, beta) == "alpha"


def test_only_unusable_senders_means_no_correspondent(peers) -> None:
    root = op.OPERATOR_HOME
    beta = op.Instance("beta").id
    broken = operator_mail.new_message("x", "beta", beta, "broken")
    broken["from"] = None
    operator_mail.queue(root, broken)
    assert operator_mail.last_correspondent(root, beta) is None


# ── reply: resolving who is replying ────────────────────────────
def test_reply_uses_the_named_instance(peers) -> None:
    _wrote("alpha", "beta")
    assert op.reply_message(["--instance", "beta", "an answer"]) == 0
    (reply,) = _queued("alpha")
    assert reply["from"] == "beta"
    assert reply["text"] == "an answer"


def test_reply_takes_the_instance_from_the_environment(peers, monkeypatch) -> None:
    _wrote("alpha", "beta")
    monkeypatch.setenv("OPERATOR_INSTANCE", "beta")
    assert op.reply_message(["an answer"]) == 0
    (reply,) = _queued("alpha")
    assert reply["from"] == "beta"


def test_the_flag_beats_the_environment(peers, monkeypatch) -> None:
    _wrote("alpha", "beta")
    monkeypatch.setenv("OPERATOR_INSTANCE", "gamma")
    assert op.reply_message(["--instance", "beta", "an answer"]) == 0
    (reply,) = _queued("alpha")
    assert reply["from"] == "beta"


def test_reply_refuses_when_it_cannot_tell_who_is_replying(peers, capsys) -> None:
    _wrote("alpha", "beta")
    assert op.reply_message(["an answer"]) == 2
    err = capsys.readouterr().err
    assert "--instance" in err and "OPERATOR_INSTANCE" in err
    assert "Nothing was sent" in err
    assert not _queued("alpha")


def test_an_environment_instance_of_only_spaces_is_not_a_name(
        peers, monkeypatch, capsys) -> None:
    """`OPERATOR_INSTANCE="   "` is unset with extra steps.

    Taken literally it would sign the reply with a blank name, which is the
    guess this command exists to refuse.
    """
    _wrote("alpha", "beta")
    monkeypatch.setenv("OPERATOR_INSTANCE", "   ")
    assert op.reply_message(["an answer"]) == 2
    assert "Nothing was sent" in capsys.readouterr().err


# ── reply: resolving who to answer ──────────────────────────────
def test_reply_defaults_to_the_last_correspondent(peers) -> None:
    _wrote("alpha", "beta")
    assert op.reply_message(["--instance", "beta", "an answer"]) == 0
    assert _queued("alpha")


def test_an_explicit_recipient_overrides_the_default(peers) -> None:
    op.Instance("gamma").claim("tok")
    _wrote("alpha", "beta")
    assert op.reply_message(
        ["--instance", "beta", "--to", "gamma", "an answer"]) == 0
    assert _queued("gamma")
    assert not _queued("alpha")


def test_reply_refuses_when_nobody_has_written(peers, capsys) -> None:
    assert op.reply_message(["--instance", "beta", "an answer"]) == 1
    err = capsys.readouterr().err
    assert "nothing to reply to" in err
    assert "Nothing was sent" in err


def test_an_unreadable_mailbox_is_not_an_empty_one(peers, monkeypatch,
                                                   capsys) -> None:
    """The distinction that must survive: "nobody wrote" versus "we could not
    look". Only the first means there is no reply to send, and answering the
    second with the first's message would tell an agent its peer never wrote."""
    def boom(*_a, **_k):
        raise operator_mail.MailError("inbox is jammed")

    monkeypatch.setattr(operator_mail, "last_correspondent", boom)
    assert op.reply_message(["--instance", "beta", "an answer"]) == 1
    err = capsys.readouterr().err
    assert "jammed" in err
    assert "--to" in err
    assert "nothing to reply to" not in err


# ── reply: argument handling ────────────────────────────────────
def test_reply_requires_text(peers, capsys) -> None:
    assert op.reply_message(["--instance", "beta"]) == 2
    assert "no message text" in capsys.readouterr().err


def test_an_unknown_option_is_refused_not_sent_as_text(peers, capsys) -> None:
    """Folding `--dry-run` into the body delivers a message to a sender who
    believes nothing was sent."""
    _wrote("alpha", "beta")
    assert op.reply_message(["--instance", "beta", "--dry-run", "text"]) == 2
    err = capsys.readouterr().err
    assert "unknown option" in err and "Nothing was sent" in err
    assert not _queued("alpha")


def test_dash_leading_text_survives_after_a_double_dash(peers) -> None:
    _wrote("alpha", "beta")
    assert op.reply_message(
        ["--instance", "beta", "--", "--force is the flag you want"]) == 0
    (reply,) = _queued("alpha")
    assert reply["text"] == "--force is the flag you want"


def test_a_body_that_looks_like_a_flag_is_not_re_parsed_by_send(peers) -> None:
    """`reply` hands the body to `send` after a `--` of its own.

    Without it, a reply reading "--queue it for later" would be parsed by
    `send` as the --queue flag plus a shorter message -- silently changing
    both the delivery mode and the text.
    """
    _wrote("alpha", "beta")
    assert op.reply_message(
        ["--instance", "beta", "--", "--queue it for later"]) == 0
    (reply,) = _queued("alpha")
    assert reply["text"] == "--queue it for later"


@pytest.mark.parametrize("args", [
    ["--instance=beta", "an answer"],
    ["--instance", "beta", "--to=alpha", "an answer"],
])
def test_inline_and_separate_values_agree(peers, args) -> None:
    _wrote("alpha", "beta")
    assert op.reply_message(args) == 0
    assert _queued("alpha")


@pytest.mark.parametrize("flag", ["--instance", "--to"])
def test_a_value_flag_at_the_end_of_argv_is_refused(peers, flag) -> None:
    assert op.reply_message([flag]) == 2


def test_help_prints_usage_and_succeeds(peers, capsys) -> None:
    assert op.reply_message(["--help"]) == 0
    assert "operator reply" in capsys.readouterr().out


def test_queue_is_passed_through(peers) -> None:
    _wrote("alpha", "beta")
    assert op.reply_message(["--instance", "beta", "--queue", "an answer"]) == 0
    assert _queued("alpha")


def test_reply_to_an_unknown_name_is_refused_without_force(peers, capsys) -> None:
    _wrote("alpha", "beta")
    assert op.reply_message(
        ["--instance", "beta", "--to", "nobody-here", "an answer"]) == 1
    assert "not sending" in capsys.readouterr().err.lower()


def test_force_reaches_a_name_that_has_not_started(peers) -> None:
    _wrote("alpha", "beta")
    assert op.reply_message(
        ["--instance", "beta", "--to", "not-yet", "--force", "an answer"]) == 0
    assert _queued("not-yet")


# ── the hint is a runnable reply ────────────────────────────────
def test_the_printed_hint_is_a_valid_reply_command(peers) -> None:
    """Parsed the way a shell would, then run. The hint is the whole reason
    an agent never has to remember the command."""
    _wrote("alpha", "beta")
    (msg,) = _queued("beta")
    parts = shlex.split(operator_mail.reply_hint(msg))
    assert parts[0] == "operator" and parts[1] == "reply"
    args = parts[2:]
    args[-1] = "an answer"
    assert op.reply_message(args) == 0
    (reply,) = _queued("alpha")
    assert reply["from"] == "beta" and reply["text"] == "an answer"


def test_each_hint_answers_its_own_message_in_a_mixed_batch(peers) -> None:
    """The default recipient is right for one conversation and wrong for a
    batch from several agents -- which is when the hints are printed. Each
    hint names its own sender rather than relying on the default."""
    op.Instance("gamma").claim("tok")
    _wrote("alpha", "beta", "from alpha")
    _wrote("gamma", "beta", "from gamma")
    hints = [operator_mail.reply_hint(m) for m in _queued("beta")]
    assert any("--to alpha" in h for h in hints)
    assert any("--to gamma" in h for h in hints)


# ── delivery at session start ───────────────────────────────────
@pytest.fixture
def session_db(tmp_path: Path) -> Path:
    path = osess.db_path(tmp_path / "proj")
    path.parent.mkdir(parents=True, exist_ok=True)
    osess.init_db(path)
    return path


def _start(instance: str = "beta", *extra) -> dict:
    opts = op._parse_session_args(["--instance", instance, *extra])
    assert opts is not None
    return opts


def test_session_start_delivers_queued_mail(peers, session_db, capsys) -> None:
    _wrote("alpha", "beta", "the contract is frozen")
    assert op._session_start(_start(), session_db) == 0
    out = capsys.readouterr().out
    assert "the contract is frozen" in out
    assert "alpha" in out


def test_session_start_marks_delivered_mail_read(peers, session_db) -> None:
    """Delivery, not a peek. Showing it again next session would read as a
    second message rather than the same one."""
    _wrote("alpha", "beta")
    op._session_start(_start(), session_db)
    assert not _queued("beta")


def test_a_second_session_start_shows_nothing_new(peers, session_db,
                                                  capsys) -> None:
    _wrote("alpha", "beta", "only once")
    op._session_start(_start(), session_db)
    capsys.readouterr()
    op._session_start(_start(), session_db)
    assert "only once" not in capsys.readouterr().out


def test_session_start_says_nothing_when_there_is_no_mail(peers, session_db,
                                                          capsys) -> None:
    assert op._session_start(_start(), session_db) == 0
    assert "message(s) from" not in capsys.readouterr().out


def test_delivered_mail_is_in_the_json_output(peers, session_db, capsys) -> None:
    _wrote("alpha", "beta", "machine readable")
    capsys.readouterr()
    assert op._session_start(_start("beta", "--json"), session_db) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [m["text"] for m in payload["messages"]] == ["machine readable"]


def test_json_output_carries_an_empty_list_when_there_is_no_mail(
        peers, session_db, capsys) -> None:
    assert op._session_start(_start("beta", "--json"), session_db) == 0
    assert json.loads(capsys.readouterr().out)["messages"] == []


def test_a_jammed_mailbox_does_not_stop_the_session_starting(
        peers, session_db, monkeypatch, capsys) -> None:
    """An unreadable mailbox must not be the thing that stops work, but it
    must not pass for an empty one either -- an agent told nothing is waiting
    stops looking."""
    def boom(*_a, **_k):
        raise operator_mail.MailError("inbox is jammed")

    monkeypatch.setattr(operator_mail, "consume", boom)
    assert op._session_start(_start(), session_db) == 0
    err = capsys.readouterr().err
    assert "jammed" in err
    assert "not an empty mailbox" in err


def test_mail_consumed_before_a_jam_is_still_shown(peers, session_db,
                                                   monkeypatch, capsys) -> None:
    """`consume` archives one message at a time, so a fault part way through
    leaves the earlier ones already marked read. This is the only time they
    will ever be offered."""
    beta = op.Instance("beta").id
    already = operator_mail.new_message("alpha", "beta", beta, "seen once")

    def boom(*_a, **_k):
        raise operator_mail.MailError("jammed", consumed=[already])

    monkeypatch.setattr(operator_mail, "consume", boom)
    assert op._session_start(_start(), session_db) == 0
    assert "seen once" in capsys.readouterr().out


# ── wiring ──────────────────────────────────────────────────────
def test_reply_is_a_known_subcommand() -> None:
    assert "reply" in op.SUBCOMMANDS


def test_the_shell_entrypoint_hands_reply_to_python() -> None:
    """`operator.sh` must not start a session for a word the Python operator
    owns. The two lists are hand-kept, which is why this is checked."""
    text = (Path(op.__file__).parent / "operator.sh").read_text(
        encoding="utf-8")
    line = next(ln for ln in text.splitlines()
                if ln.startswith("PYTHON_ONLY_SUBCOMMANDS="))
    assert " reply " in line
