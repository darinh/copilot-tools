"""Tests for agent-to-agent messaging storage and rendering."""
from __future__ import annotations

import json

import pytest

import operator_mail


def _msg(root, sender="alpha", recipient="beta", text="hello"):
    msg = operator_mail.new_message(sender, recipient, recipient, text)
    operator_mail.queue(root, msg)
    return msg


# ── storage ─────────────────────────────────────────────────────
def test_queued_message_is_pending_for_the_recipient(tmp_path):
    _msg(tmp_path)
    assert operator_mail.pending_count(tmp_path, "beta") == 1
    (got,) = operator_mail.pending(tmp_path, "beta")
    assert got["from"] == "alpha"
    assert got["text"] == "hello"
    assert got["read_at"] is None


def test_messages_are_isolated_per_recipient(tmp_path):
    _msg(tmp_path, recipient="beta")
    assert operator_mail.pending(tmp_path, "gamma") == []
    assert operator_mail.pending_count(tmp_path, "gamma") == 0


def test_unknown_recipient_reads_as_empty_not_an_error(tmp_path):
    assert operator_mail.pending(tmp_path, "never-existed") == []
    assert operator_mail.history(tmp_path, "never-existed") == []


def test_pending_is_oldest_first(tmp_path):
    for i in range(5):
        msg = operator_mail.new_message("alpha", "beta", "beta", f"m{i}")
        # Ids embed the timestamp, which has one-second resolution, so force
        # distinct ids to prove ordering rather than accidentally relying on
        # the clock ticking between writes.
        msg["id"] = f"2026073120000{i}-aaaaaaa{i}"
        operator_mail.queue(tmp_path, msg)
    assert [m["text"] for m in operator_mail.pending(tmp_path, "beta")] == \
        ["m0", "m1", "m2", "m3", "m4"]


def test_consume_returns_and_archives(tmp_path):
    _msg(tmp_path, text="first")
    taken = operator_mail.consume(tmp_path, "beta")
    assert [m["text"] for m in taken] == ["first"]
    assert taken[0]["read_at"] is not None
    assert operator_mail.pending(tmp_path, "beta") == []
    assert [m["text"] for m in operator_mail.history(tmp_path, "beta")] == ["first"]


def test_consume_twice_does_not_redeliver(tmp_path):
    _msg(tmp_path)
    assert len(operator_mail.consume(tmp_path, "beta")) == 1
    assert operator_mail.consume(tmp_path, "beta") == []


def test_live_delivery_is_recorded_without_queueing(tmp_path):
    msg = operator_mail.new_message("alpha", "beta", "beta", "live one")
    operator_mail.record_delivered(tmp_path, msg)
    assert operator_mail.pending(tmp_path, "beta") == []
    (archived,) = operator_mail.history(tmp_path, "beta")
    assert archived["delivery"] == "live"
    assert archived["read_at"] is not None


# ── archive-by-id (the failed-launch path) ──────────────────────
def test_archive_only_touches_the_named_ids(tmp_path):
    """A message that arrives while a session is starting must not be
    swallowed by the archiving of the batch that preceded it."""
    first = _msg(tmp_path, text="in the preamble")
    later = _msg(tmp_path, text="arrived during launch")
    moved = operator_mail.archive(tmp_path, "beta", [first["id"]])
    assert moved == 1
    assert [m["text"] for m in operator_mail.pending(tmp_path, "beta")] == \
        ["arrived during launch"]
    assert later["id"] not in [m["id"] for m in operator_mail.history(tmp_path, "beta")]


def test_archive_is_a_no_op_for_ids_that_are_gone(tmp_path):
    assert operator_mail.archive(tmp_path, "beta", ["nope"]) == 0


def test_pending_survives_when_nothing_archives_it(tmp_path):
    """Reading for a preamble must not consume: a failed launch is retried."""
    _msg(tmp_path, text="retry me")
    operator_mail.pending(tmp_path, "beta")
    operator_mail.pending(tmp_path, "beta")
    assert [m["text"] for m in operator_mail.pending(tmp_path, "beta")] == ["retry me"]


# ── corrupt input ───────────────────────────────────────────────
def test_corrupt_message_does_not_jam_the_mailbox(tmp_path):
    _msg(tmp_path, text="good")
    bad = operator_mail.inbox_dir(tmp_path, "beta") / "20260731-bad.json"
    bad.write_text("{not json", encoding="utf-8")

    assert [m["text"] for m in operator_mail.pending(tmp_path, "beta")] == ["good"]
    taken = operator_mail.consume(tmp_path, "beta")
    assert [m["text"] for m in taken] == ["good"]
    # The unreadable file is moved aside rather than reconsidered forever.
    assert not bad.exists()
    assert (operator_mail.archive_dir(tmp_path, "beta") / bad.name).exists()
    assert operator_mail.consume(tmp_path, "beta") == []


def test_non_object_json_is_ignored(tmp_path):
    directory = operator_mail.inbox_dir(tmp_path, "beta")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "20260731-list.json").write_text("[1,2,3]", encoding="utf-8")
    assert operator_mail.pending(tmp_path, "beta") == []


# ── rendering ───────────────────────────────────────────────────
def test_flatten_removes_newlines_and_control_characters():
    assert operator_mail.flatten("a\nb\tc") == "a b c"
    assert operator_mail.flatten("bell\x07here") == "bell here"
    assert operator_mail.flatten("  spaced   out  ") == "spaced out"


def test_rendered_line_never_starts_with_a_slash_or_mention():
    """A leading '/' or '@' would be read by the UI as a command or mention."""
    for text in ("/exit", "@agent do this", "  /clear"):
        msg = operator_mail.new_message("alpha", "beta", "beta", text)
        line = operator_mail.render_line(msg)
        assert line.startswith("[operator message from")
        assert "\n" not in line


def test_rendered_line_names_sender_and_reply_command():
    msg = operator_mail.new_message("alpha", "beta", "beta", "ping")
    line = operator_mail.render_line(msg)
    assert '"alpha"' in line
    assert "operator send --from beta --to alpha" in line


def test_rendered_line_is_bounded(monkeypatch):
    monkeypatch.setattr(operator_mail, "LIVE_TEXT_LIMIT", 20)
    msg = operator_mail.new_message("alpha", "beta", "beta", "x" * 500)
    line = operator_mail.render_line(msg)
    assert "truncated" in line
    assert len(line) < 300


def test_preamble_block_is_single_line_and_names_every_sender():
    msgs = [
        operator_mail.new_message("alpha", "beta", "beta", "one\ntwo"),
        operator_mail.new_message("gamma", "beta", "beta", "three"),
    ]
    block = operator_mail.render_for_agent(msgs)
    assert "\n" not in block
    assert "'alpha'" in block and "'gamma'" in block
    assert "one two" in block
    assert "operator send --from beta --to alpha" in block
    assert "operator send --from beta --to gamma" in block


def test_preamble_block_is_empty_without_messages():
    assert operator_mail.render_for_agent([]) == ""


def test_terminal_rendering_keeps_original_line_breaks():
    msg = operator_mail.new_message("alpha", "beta", "beta", "line1\nline2")
    out = operator_mail.render_for_terminal([msg])
    assert "line1" in out and "line2" in out
    assert "reply: operator send --from beta --to alpha" in out


def test_terminal_rendering_says_so_when_empty():
    assert operator_mail.render_for_terminal([]) == "No messages."


def test_stored_text_is_never_flattened(tmp_path):
    """Flattening is a live-delivery concern only; the record stays verbatim."""
    _msg(tmp_path, text="line1\nline2")
    (stored,) = operator_mail.pending(tmp_path, "beta")
    assert stored["text"] == "line1\nline2"


def test_message_files_are_valid_json_on_disk(tmp_path):
    msg = _msg(tmp_path)
    path = operator_mail.inbox_dir(tmp_path, "beta") / f"{msg['id']}.json"
    assert json.loads(path.read_text(encoding="utf-8"))["from"] == "alpha"


def test_no_temp_files_are_left_behind(tmp_path):
    _msg(tmp_path)
    assert list(operator_mail.inbox_dir(tmp_path, "beta").glob("*.tmp")) == []


def test_write_failure_raises_mail_error(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(operator_mail.Path, "write_text", boom)
    with pytest.raises(operator_mail.MailError):
        operator_mail.queue(tmp_path,
                            operator_mail.new_message("a", "b", "b", "t"))
