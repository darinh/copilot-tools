"""Tests for agent-to-agent messaging storage and rendering."""
from __future__ import annotations

import json
import os
from pathlib import Path

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


def test_archive_refuses_an_id_that_escapes_the_inbox(tmp_path):
    """The ids come from the *content* of inbox files, so they are no more
    trustworthy than the files themselves. Building a path out of one lets a
    hand-edited or hostile message delete a JSON file anywhere on disk."""
    victim = tmp_path / "victim.json"
    victim.write_text('{"keep": "me"}', encoding="utf-8")
    kept = _msg(tmp_path, text="still pending")
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    escape = os.path.relpath(tmp_path / "victim", inbox)

    assert operator_mail.archive(tmp_path, "beta", [escape]) == 0
    assert victim.is_file()
    assert not (operator_mail.archive_dir(tmp_path, "beta") / "victim.json").exists()
    # The unrelated message it scanned past is left exactly where it was.
    assert [m["id"] for m in operator_mail.pending(tmp_path, "beta")] == [kept["id"]]


def test_archive_finds_a_message_whose_id_does_not_match_its_filename(tmp_path):
    """`archive` is handed ids read out of the files; resolving them back to a
    filename means a message whose id and name disagree is never archived, so
    it is re-injected into every session preamble from then on."""
    msg = operator_mail.new_message("alpha", "beta", "beta", "deliver me once")
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "20260731T000000Z-renamed.json").write_text(
        json.dumps(msg), encoding="utf-8")

    assert operator_mail.archive(tmp_path, "beta", [msg["id"]]) == 1
    assert operator_mail.pending(tmp_path, "beta") == []
    assert [m["text"] for m in operator_mail.history(tmp_path, "beta")] == \
        ["deliver me once"]


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


def test_archive_reads_only_the_messages_it_was_asked_for(tmp_path, monkeypatch):
    """A performance guard, not a security one -- the traversal is proved by
    test_archive_refuses_an_id_that_escapes_the_inbox, and the original
    direct-lookup code would have passed this too. It is here because the
    scan replaced that direct lookup, and an inbox is unbounded: without it
    a two-message archive call is free to grow into a parse of the whole
    mailbox. Ids and filenames agree for everything this module writes, so
    the names alone settle it and nothing else needs opening."""
    wanted = [_msg(tmp_path, text=f"take {i}")["id"] for i in range(2)]
    for i in range(20):
        _msg(tmp_path, text=f"leave {i}")
    reads: list[str] = []
    real_read_text = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        reads.append(self.name)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    assert operator_mail.archive(tmp_path, "beta", wanted) == 2
    assert len(reads) == 2, f"read {len(reads)} files to archive 2: {reads}"


def test_archive_does_not_let_a_filename_stand_in_for_an_id(tmp_path):
    """Settling matches by name is a shortcut, not a proof. A file named for
    one message while holding another satisfies neither: counting the name as
    the id would tick that id off and leave the file genuinely carrying it in
    the inbox, re-delivered in every preamble from then on -- which is the
    fault this function exists to stop."""
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    inbox.mkdir(parents=True, exist_ok=True)
    first = operator_mail.new_message("alpha", "beta", "beta", "message A")
    first["id"] = "20260731T000000Z-aaaa"
    second = operator_mail.new_message("alpha", "beta", "beta", "message B")
    second["id"] = "20260731T000000Z-bbbb"
    # The name of one file is the id of the other.
    (inbox / f"{second['id']}.json").write_text(json.dumps(first), encoding="utf-8")
    (inbox / "renamed.json").write_text(json.dumps(second), encoding="utf-8")

    wanted = [m["id"] for m in operator_mail.pending(tmp_path, "beta")]
    assert sorted(wanted) == [first["id"], second["id"]]
    assert operator_mail.archive(tmp_path, "beta", wanted) == 2
    assert operator_mail.pending(tmp_path, "beta") == []
    assert sorted(m["text"] for m in operator_mail.history(tmp_path, "beta")) == \
        ["message A", "message B"]


def test_consume_tolerates_a_message_another_consumer_already_took(tmp_path,
                                                                   monkeypatch):
    """`operator inbox` and the loop's own session-start read can run at the
    same moment. Losing the race must not abort the whole batch: the archive
    copy is already written, so the only thing left to do is nothing."""
    _msg(tmp_path, text="racy")
    real_unlink = Path.unlink

    def racing_unlink(self, *args, **kwargs):
        real_unlink(self, *args, **kwargs)
        raise FileNotFoundError(2, "No such file or directory", str(self))

    monkeypatch.setattr(Path, "unlink", racing_unlink)
    assert [m["text"] for m in operator_mail.consume(tmp_path, "beta")] == ["racy"]


def test_consume_leaves_no_temporary_files_behind(tmp_path):
    _msg(tmp_path, text="tidy")
    operator_mail.consume(tmp_path, "beta")
    archive = operator_mail.archive_dir(tmp_path, "beta")
    assert [p.name for p in archive.iterdir() if p.suffix == ".tmp"] == []


def test_renderers_survive_a_message_missing_its_addresses(tmp_path):
    """Every other field is read with .get() precisely because a mailbox file
    can be hand-edited or written by an older version. A reply hint that
    subscripts the message throws that tolerance away, and it is on the
    session-preamble path, so one bad file would break session start."""
    malformed = {"id": "20260731T000000Z-bad", "text": "no addresses"}
    assert "no addresses" in operator_mail.render_for_terminal([malformed])
    assert "no addresses" in operator_mail.render_for_agent([malformed])
    assert "no addresses" in operator_mail.render_line(malformed)
    # The hint must not invent a recipient that would silently misdirect a
    # reply -- it has to be visibly a placeholder.
    assert "operator send" in operator_mail.reply_hint(malformed)


def test_renderers_survive_fields_that_are_json_null(tmp_path):
    """`.get(key, default)` does not help when the key is present and null,
    which is what an older writer or a hand-edited file produces. Every
    renderer is on the session-start path, so a TypeError here jams the
    mailbox for good: the loop cannot start a session to clear it."""
    null_fields = {"id": "20260731T000000Z-null", "from": None, "to": None,
                   "text": None, "sent_at": None, "delivery": None}
    assert operator_mail.render_line(null_fields)
    assert operator_mail.render_for_agent([null_fields])
    assert operator_mail.render_for_terminal([null_fields])
    assert operator_mail.reply_hint(null_fields)
    # Mixed with a well-formed message, because render_for_agent sorts the
    # sender names against each other.
    good = operator_mail.new_message("alpha", "beta", "beta", "fine")
    assert operator_mail.render_for_agent([null_fields, good])


@pytest.mark.parametrize("hostile", [
    "\n/exit",
    "\r/clear",
    "alpha\x1b[2J",
    "alpha\nrm -rf /",
])
def test_a_sender_name_cannot_smuggle_keystrokes_into_a_live_session(hostile):
    """`--from` is taken verbatim from the command line -- only the recipient
    is checked against known instances -- and the rendered line is typed into
    the recipient's session. The multiplexer submits the line at the first
    newline, so a name carrying one ends the message early and types whatever
    follows into another agent's prompt. The message body has been flattened
    against exactly this since the module was written; the names reach the
    same keystroke stream by the same route."""
    msg = operator_mail.new_message(hostile, "beta", "beta", "hello")
    line = operator_mail.render_line(msg)
    assert not any(c in line for c in "\n\r\x1b\x00")
    # The reply hint is rendered into the same line, so it needs it too.
    assert not any(c in operator_mail.reply_hint(msg) for c in "\n\r\x1b\x00")


def test_sender_names_are_safe_to_join_into_a_line(tmp_path):
    """The loop logs "queued message(s) from <senders>" before every launch.
    Building that from raw fields makes a null name raise inside sorted() --
    aborting the supervisor, not one log line -- and lets a name carrying a
    newline write whatever it likes into the operator log."""
    msgs = [{"from": None}, {"from": "\n/exit"}, {"from": "alpha"}, {}]
    names = operator_mail.sender_names(msgs)
    joined = ", ".join(names)
    assert not any(c in joined for c in "\n\r\x1b\x00")
    assert "alpha" in names
    assert names == sorted(names)


def test_terminal_rendering_strips_escape_sequences_from_a_message_body(tmp_path):
    """`operator inbox` prints the body to a human's terminal. It keeps the
    line structure deliberately, which is why it cannot simply flatten the
    whole body -- but an escape sequence still must not reach the terminal."""
    msg = operator_mail.new_message("alpha", "beta", "beta",
                                    "line one\n\x1b[2Jcleared\nline three")
    out = operator_mail.render_for_terminal([msg])
    assert "\x1b" not in out
    assert "cleared" in out
    assert "line three" in out


def test_terminal_rendering_keeps_the_shape_of_a_body(tmp_path):
    """Agents mail each other code and command output. Collapsing whitespace
    is right for the one-line live form, where a newline would submit the
    message early, and wrong here: `operator inbox` is read by a human and an
    unindented snippet is a worse message, not a safer one."""
    msg = operator_mail.new_message(
        "alpha", "beta", "beta", "def f():\n    return 1\n\nrun with:  f()")
    out = operator_mail.render_for_terminal([msg])
    assert "    return 1" in out
    assert "run with:  f()" in out


def test_duplicate_ids_in_the_inbox_are_all_archived(tmp_path):
    """Two files claiming one id is malformed, but leaving the second behind
    means it is re-delivered on every launch for ever."""
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    inbox.mkdir(parents=True, exist_ok=True)
    msg = operator_mail.new_message("alpha", "beta", "beta", "twice")
    msg["id"] = "20260731T000000Z-dupe"
    for name in ("copy-one.json", "copy-two.json"):
        (inbox / name).write_text(json.dumps(msg), encoding="utf-8")

    assert operator_mail.archive(tmp_path, "beta", [msg["id"]]) == 2
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
