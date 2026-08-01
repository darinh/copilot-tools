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
    # Control, through the SAME calls the assertions below use. Without it
    # both of those pass whenever `pending` returns nothing for any reason --
    # a wrong root, a reader that never found the inbox -- and the test would
    # report isolation working when nothing had been delivered at all.
    assert [m["text"] for m in operator_mail.pending(tmp_path, "beta")] == ["hello"]
    assert operator_mail.pending_count(tmp_path, "beta") == 1

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


# ── unreadable input, which is not corrupt input ────────────────
def _deny_reads(monkeypatch, *paths):
    """Make `read_text` fail for exactly these files, and prove that it did.

    A denial that never fires produces a test asserting against an ordinary
    read, which passes for the wrong reason. The returned list is the record
    of what was actually denied; every test below asserts on it before it
    asserts on anything else.
    """
    wanted = {Path(p).resolve() for p in paths}
    fired: list[str] = []
    real = Path.read_text

    def denied(self, *args, **kwargs):
        if self.resolve() in wanted:
            fired.append(str(self))
            raise PermissionError(13, "The process cannot access the file")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    return fired


def test_an_unreadable_message_is_not_mistaken_for_a_corrupt_one(tmp_path,
                                                                 monkeypatch):
    """A failed read says nothing about the file, so nothing may be concluded.

    Moving it aside archives a message nobody has seen and leaves a mailbox
    that looks empty rather than blocked -- the same signature as mail that
    was never sent.
    """
    msg = _msg(tmp_path, text="important")
    path = operator_mail.inbox_dir(tmp_path, "beta") / f"{msg['id']}.json"
    fired = _deny_reads(monkeypatch, path)

    assert operator_mail.consume(tmp_path, "beta") == []
    assert fired, "the denial never fired; this test proved nothing"
    assert path.exists(), "an unread message was moved out of the inbox"
    assert not (operator_mail.archive_dir(tmp_path, "beta") / path.name).exists()
    assert operator_mail.pending_count(tmp_path, "beta") == 1


def test_a_message_that_could_not_be_read_is_delivered_once_it_can_be(
        tmp_path, monkeypatch):
    msg = _msg(tmp_path, text="important")
    path = operator_mail.inbox_dir(tmp_path, "beta") / f"{msg['id']}.json"
    fired = _deny_reads(monkeypatch, path)
    assert operator_mail.consume(tmp_path, "beta") == []
    assert fired

    monkeypatch.undo()
    assert [m["text"] for m in operator_mail.consume(tmp_path, "beta")] == \
        ["important"]


def test_one_unreadable_message_does_not_hold_up_its_neighbours(tmp_path,
                                                                monkeypatch):
    first = operator_mail.new_message("alpha", "beta", "beta", "blocked")
    first["id"] = "20260731200000-aaaaaaa0"
    operator_mail.queue(tmp_path, first)
    second = operator_mail.new_message("alpha", "beta", "beta", "fine")
    second["id"] = "20260731200001-aaaaaaa1"
    operator_mail.queue(tmp_path, second)
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    fired = _deny_reads(monkeypatch, inbox / f"{first['id']}.json")

    assert [m["text"] for m in operator_mail.consume(tmp_path, "beta")] == \
        ["fine"]
    assert fired
    assert (inbox / f"{first['id']}.json").exists()


def test_archive_does_not_file_away_a_message_it_could_not_read(tmp_path,
                                                                monkeypatch):
    """`archive` is told which ids were shown to the recipient. A file it
    cannot open has not been shown to anybody and has not corroborated its
    own name, so archiving it on the strength of the filename files away
    something unread -- and counts it as delivered."""
    msg = _msg(tmp_path, text="important")
    path = operator_mail.inbox_dir(tmp_path, "beta") / f"{msg['id']}.json"
    fired = _deny_reads(monkeypatch, path)

    assert operator_mail.archive(tmp_path, "beta", [msg["id"]]) == 0
    assert fired
    assert path.exists()
    assert operator_mail.pending_count(tmp_path, "beta") == 1


def test_an_unreadable_file_is_not_listed_as_pending(tmp_path, monkeypatch):
    """Skipping is right here -- nothing is destroyed by omitting it from a
    listing -- but the file must survive to be listed later."""
    msg = _msg(tmp_path, text="important")
    path = operator_mail.inbox_dir(tmp_path, "beta") / f"{msg['id']}.json"
    fired = _deny_reads(monkeypatch, path)

    assert operator_mail.pending(tmp_path, "beta") == []
    assert fired
    assert path.exists()


_POSIX_PERMS = (os.name != "nt" and hasattr(os, "geteuid")
                and os.geteuid() != 0)


@pytest.mark.skipif(not _POSIX_PERMS,
                    reason="needs POSIX permissions enforced against a non-root user")
def test_a_really_unreadable_file_really_survives_consume(tmp_path):
    """The incident on a real filesystem, with no mock in the way.

    `chmod 000` in a writable directory is an ordinary state, and it is the
    exact shape that matters: the read is denied while the rename is still
    permitted, so nothing stops the move-aside from succeeding. A reviewer
    argued the loss existed only in the monkeypatch because a Windows lock
    would deny both operations -- true of a Windows lock, and this is why the
    claim needed a real OS to settle rather than another fixture.
    """
    msg = _msg(tmp_path, text="important")
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    path = inbox / f"{msg['id']}.json"
    os.chmod(path, 0o000)
    try:
        # Prove the premise. Without this the test passes whenever the
        # permission silently fails to bite.
        with pytest.raises(OSError):
            path.read_text(encoding="utf-8")
        probe = inbox / "probe.tmp"
        os.replace(path, probe)
        os.replace(probe, path)

        assert operator_mail.consume(tmp_path, "beta") == []
        assert path.exists(), "an unread message was moved out of the inbox"
        assert operator_mail.pending_count(tmp_path, "beta") == 1
    finally:
        # Restore permissions wherever the file ended up. Chmod'ing a path
        # that a failing run has already moved would raise from the `finally`
        # and mask the assertion that actually failed, so a test that caught
        # the bug would report the wrong reason for catching it.
        for candidate in (path,
                          operator_mail.archive_dir(tmp_path, "beta") / path.name):
            if candidate.exists():
                os.chmod(candidate, 0o600)


@pytest.mark.skipif(not _POSIX_PERMS,
                    reason="needs POSIX permissions enforced against a non-root user")
def test_an_unsearchable_inbox_does_not_crash_the_poll_loop(tmp_path):
    """The `except OSError` around the `is_file()` probe is load-bearing.

    It reads like belt-and-braces, and the next person to see it will be
    tempted to delete it as unreachable. It is not. `pathlib.is_file()` is not
    total: it swallows `OSError` only for an allowlist -- `_IGNORED_ERRNOS` is
    `ENOENT, ENOTDIR, EBADF, ELOOP`, plus three WinErrors -- and *re-raises*
    everything else, `EACCES` included. That is nowhere in its signature or
    its docstring.

    So the errno decides whether `is_file()` answers a question or raises a
    new one, which is the same confusion this function exists to remove:
    returning False is a claim about the object, raising is a claim about the
    attempt, and pathlib merges them.

    An inbox that is readable but not searchable (mode 0o400) is the reachable
    shape: `glob` still lists the names while every `stat` on them fails
    EACCES. Without the guard the exception escapes `_read_message` and out
    through `pending()` -- which the supervisor loop calls on every poll.
    """
    _msg(tmp_path, text="important")
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    os.chmod(inbox, 0o400)
    try:
        # Prove the premise, or this test passes against an ordinary inbox.
        names = sorted(inbox.glob("*.json"))
        assert names, "premise: the names must still be listable"
        with pytest.raises(OSError):
            names[0].read_text(encoding="utf-8")
        with pytest.raises(OSError):
            # The point of the whole test: this is the call that raises.
            names[0].is_file()

        assert operator_mail.pending(tmp_path, "beta") == []
        assert operator_mail.consume(tmp_path, "beta") == []
        assert operator_mail.pending_count(tmp_path, "beta") == 1
    finally:
        os.chmod(inbox, 0o700)


@pytest.mark.skipif(not _POSIX_PERMS,
                    reason="needs POSIX permissions enforced against a non-root user")
def test_an_unlistable_inbox_is_not_reported_as_empty(tmp_path):
    """An inbox that cannot be listed must not read as an inbox with no mail.

    The sibling test above is the *readable but unsearchable* shape (0o400),
    where the names still list. This is one notch worse and it used to be
    silent: 0o100 grants traversal without read, so `opendir` itself fails and
    `Path.glob` -- which catches `PermissionError` internally and simply stops
    yielding -- produced the empty sequence. Every caller then reported the
    same thing an empty mailbox reports.

    That was measured before the fix, not deduced: `pending`, `pending_count`
    and `consume` all returned 0 for an inbox holding one real message. The
    consequence is the one failure this module cannot afford, because the
    whole point of a mailbox is that somebody is waiting for an answer: a peer
    blocked on a reply, and a recipient told in the ordinary words that nobody
    wrote to it.

    So the empty inbox is asserted alongside the blocked one here. "Raises"
    is only meaningful against a case that does not, and the two differ by
    nothing but the permission bits.
    """
    _msg(tmp_path, text="important")
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    operator_mail.inbox_dir(tmp_path, "gamma").mkdir(parents=True)
    os.chmod(inbox, 0o100)
    try:
        # Prove the premise. Without this the test passes on any inbox.
        with pytest.raises(OSError):
            os.listdir(inbox)
        assert sorted(inbox.glob("*.json")) == [], \
            "premise: glob is silent about the denial, which is the bug"

        for call in (lambda: operator_mail.pending(tmp_path, "beta"),
                     lambda: operator_mail.pending_count(tmp_path, "beta"),
                     lambda: operator_mail.consume(tmp_path, "beta")):
            with pytest.raises(operator_mail.MailError):
                call()

        # The matched control: a real, readable, empty inbox answers 0 -- so
        # the refusal above is about the denial and not about emptiness.
        assert operator_mail.pending(tmp_path, "gamma") == []
        assert operator_mail.pending_count(tmp_path, "gamma") == 0
        assert operator_mail.consume(tmp_path, "gamma") == []
    finally:
        os.chmod(inbox, 0o700)

    # Nothing was destroyed while the mailbox was unreadable.
    assert operator_mail.pending_count(tmp_path, "beta") == 1
    assert [m["text"] for m in operator_mail.pending(tmp_path, "beta")] == \
        ["important"]


def test_an_unlistable_inbox_is_not_reported_as_empty_on_any_platform(
        tmp_path, monkeypatch):
    """The same claim where the POSIX permission test cannot run.

    Windows is the platform this toolkit is developed on and the one whose CI
    job would otherwise only ever skip the case above, so the denial is
    injected at the one call that reports it. Scoped to the one directory:
    `operator_mail.os` is the shared `os` module, and a blanket failure would
    take pytest's own machinery with it.
    """
    _msg(tmp_path, text="important")
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    real_scandir = os.scandir
    denying = {"on": True}

    def denied_scandir(path=".", *args, **kwargs):
        if denying["on"] and Path(path) == inbox:
            raise PermissionError(13, "Permission denied")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(operator_mail.os, "scandir", denied_scandir)

    for call in (lambda: operator_mail.pending(tmp_path, "beta"),
                 lambda: operator_mail.pending_count(tmp_path, "beta"),
                 lambda: operator_mail.consume(tmp_path, "beta"),
                 lambda: operator_mail.archive(tmp_path, "beta", ["anything"])):
        with pytest.raises(operator_mail.MailError):
            call()

    # An inbox nobody has ever written to is a complete answer, not a denial;
    # if this raised too, the test above would pass for the wrong reason.
    assert operator_mail.pending(tmp_path, "never-existed") == []
    assert operator_mail.pending_count(tmp_path, "never-existed") == 0

    denying["on"] = False
    assert operator_mail.pending_count(tmp_path, "beta") == 1


def test_an_unreadable_archive_does_not_pass_as_no_history(tmp_path,
                                                           monkeypatch):
    """`history` reads a directory too, and reports the same way.

    Cheaper to get wrong and easy to miss, because history is only ever read
    by a human: an archive that cannot be opened would otherwise render as the
    conversation never having happened.
    """
    msg = _msg(tmp_path, text="said once")
    operator_mail.archive(tmp_path, "beta", [msg["id"]])
    assert [m["text"] for m in operator_mail.history(tmp_path, "beta")] == \
        ["said once"], "premise: the history is there to be lost"

    archive = operator_mail.archive_dir(tmp_path, "beta")
    real_scandir = os.scandir

    def denied_scandir(path=".", *args, **kwargs):
        if Path(path) == archive:
            raise PermissionError(13, "Permission denied")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(operator_mail.os, "scandir", denied_scandir)
    with pytest.raises(operator_mail.MailError):
        operator_mail.history(tmp_path, "beta")


def test_a_plain_file_where_the_inbox_should_be_is_a_fault_not_an_empty_inbox(
        tmp_path):
    """A mailbox that is a plain file is permanently deaf, so it must say so.

    This is the case a reviewer overturned. The first version of this fix
    reported ENOTDIR as empty, reasoning that it would never clear and so
    raising would jam the supervisor loop for ever. Both halves were wrong.
    The loop catches `MailError`, logs it and launches anyway, so nothing
    jams; and permanence is a reason to report a fault more loudly, not to
    give it the one answer that reads as healthy. Reported empty, such a
    mailbox is silently undeliverable for ever with nothing anywhere
    complaining -- the exact shape this whole change exists to remove.

    The sender is asserted here too, because the fault is at the far end from
    whoever trips over it: `_write`'s `mkdir(exist_ok=True)` forgives a
    directory but not a file, so `operator send` used to end in a raw
    `FileExistsError` traceback about somebody else's mailbox.
    """
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text("not a directory", encoding="utf-8")

    for call in (lambda: operator_mail.pending(tmp_path, "beta"),
                 lambda: operator_mail.pending_count(tmp_path, "beta"),
                 lambda: operator_mail.consume(tmp_path, "beta")):
        with pytest.raises(operator_mail.MailError):
            call()

    with pytest.raises(operator_mail.MailError):
        operator_mail.queue(tmp_path,
                            operator_mail.new_message("alpha", "beta", "beta",
                                                      "can you hear me"))

    # The matched control: an ordinary mailbox in the same tree still works,
    # so the refusal is about this path and not about the tree being broken.
    assert operator_mail.pending(tmp_path, "gamma") == []
    operator_mail.queue(tmp_path,
                        operator_mail.new_message("alpha", "gamma", "gamma",
                                                  "delivered"))
    assert [m["text"] for m in operator_mail.pending(tmp_path, "gamma")] == \
        ["delivered"]


def test_a_file_that_can_neither_be_read_nor_moved_is_left_alone(tmp_path,
                                                                 monkeypatch):
    """The Windows sharing-violation shape, where both operations fail.

    This case was already safe: the move-aside raised and was passed over,
    leaving the file pending. It is recorded so the guarantee is pinned
    rather than incidental, and it is the counterpart to the test above --
    together they say the outcome no longer depends on whether the operating
    system happened to also deny the rename.
    """
    msg = _msg(tmp_path, text="important")
    path = operator_mail.inbox_dir(tmp_path, "beta") / f"{msg['id']}.json"
    fired = _deny_reads(monkeypatch, path)

    real_replace = os.replace

    def denied_replace(src, dst, *args, **kwargs):
        if Path(src).resolve() == path.resolve():
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(operator_mail.os, "replace", denied_replace)

    assert operator_mail.consume(tmp_path, "beta") == []
    assert fired
    assert path.exists()
    assert operator_mail.pending_count(tmp_path, "beta") == 1


def test_a_directory_named_like_a_message_does_not_jam_the_mailbox(tmp_path):
    """Not every failed read is transient, and the trade only holds for the
    ones that are.

    Leaving unreadable files pending is right when the cause will clear -- a
    lock, a permission being fixed. A directory named `*.json` fails the read
    identically and for ever, so the same treatment would mean `pending_count`
    reporting a message that can never be delivered, permanently. That it is
    not a regular file is knowledge about the file, which puts it with
    corruption rather than with an unreadable state. The broad `except` this
    replaced did move it aside; narrowing the authority must not narrow the
    reach.
    """
    _msg(tmp_path, text="good")
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    intruder = inbox / "20260731-directory.json"
    intruder.mkdir()

    assert [m["text"] for m in operator_mail.consume(tmp_path, "beta")] == \
        ["good"]
    assert not intruder.exists(), "the mailbox is jammed for ever"
    assert operator_mail.pending_count(tmp_path, "beta") == 0


def test_a_dangling_symlink_does_not_jam_the_mailbox(tmp_path):
    """The other permanent shape, and the one that cost a review round.

    A dangling symlink named `*.json` raises `FileNotFoundError` on read --
    but it is not a message that vanished, it is a directory entry that will
    raise identically for ever. An earlier draft of this fix gave
    `FileNotFoundError` its own branch returning UNREADABLE, on the reasoning
    that gone is not corrupt. That branch read as clearer and was worse: it
    put the permanent case and the racing case together, so the symlink
    stayed pending for ever -- the very jam this function exists to prevent,
    reintroduced inside its fix. The classification has to follow what the
    path IS, not which exception it raised.
    """
    _msg(tmp_path, text="good")
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    link = inbox / "20260731-dangling.json"
    try:
        link.symlink_to(tmp_path / "no-such-target.json")
    except (OSError, NotImplementedError) as exc:  # pragma: no cover
        pytest.skip(f"symlinks not permitted here: {exc}")
    assert link.is_symlink(), "premise: the entry exists"
    assert not link.is_file(), "premise: it does not resolve to a file"

    assert [m["text"] for m in operator_mail.consume(tmp_path, "beta")] == \
        ["good"]
    assert not link.is_symlink(), "the mailbox is jammed for ever"
    assert operator_mail.pending_count(tmp_path, "beta") == 0


def test_a_message_that_vanishes_mid_read_is_not_reported_as_delivered(
    tmp_path, monkeypatch
):
    """A file taken by another reader between the scan and the read.

    Nothing is lost and nothing is claimed: it is not returned as delivered,
    and the move that follows finds nothing to move.
    """
    msg = _msg(tmp_path, text="taken")
    _msg(tmp_path, text="good")
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    path = inbox / f"{msg['id']}.json"
    real = Path.read_text
    fired = []

    def vanish(self, *args, **kwargs):
        if self.resolve() == path.resolve():
            fired.append(str(self))
            Path.unlink(self)
            raise FileNotFoundError(2, "No such file or directory")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", vanish)
    delivered = [m["text"] for m in operator_mail.consume(tmp_path, "beta")]
    assert fired, "premise: the read actually raced"
    assert delivered == ["good"], "a message nobody read was reported read"
    assert not (
        operator_mail.archive_dir(tmp_path, "beta") / path.name
    ).exists()


def test_bytes_that_are_not_utf8_are_corruption_not_an_unreadable_state(
        tmp_path):
    """`read_text` decodes, so it raises `UnicodeDecodeError` -- a
    `ValueError` -- from the READ rather than the parse. Undecodable bytes
    are still permanent knowledge about the file, so they must be moved aside
    like any other corruption and must never escape as an exception: one bad
    file in a mailbox would otherwise take down the supervisor loop, which
    calls `pending()` on every poll.
    """
    _msg(tmp_path, text="good")
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    bad = inbox / "20260731-notutf8.json"
    bad.write_bytes(b"\xff\xfe not utf-8 at all")

    assert [m["text"] for m in operator_mail.pending(tmp_path, "beta")] == \
        ["good"]
    assert [m["text"] for m in operator_mail.consume(tmp_path, "beta")] == \
        ["good"]
    assert not bad.exists()
    assert (operator_mail.archive_dir(tmp_path, "beta") / bad.name).exists()


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
    opened = {name for name in reads}
    assert opened == {f"{i}.json" for i in wanted}, (
        f"opened files beyond the two requested: {sorted(opened)}")


def test_archive_does_not_believe_a_filename_over_its_contents(tmp_path):
    """The counting shortcut that made the scan cheap was itself unsound: if
    the number of name matches happened to equal the number of ids asked
    for, the content scan was skipped, and a file named for one message
    while holding another satisfied the count. The requested message stayed
    pending -- re-delivered on every launch for ever -- and an unrelated
    message was archived as read in its place. Both halves of the fault this
    function exists to prevent, reintroduced by its own optimisation."""
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    inbox.mkdir(parents=True, exist_ok=True)
    a = operator_mail.new_message("alpha", "beta", "beta", "message A")
    a["id"] = "A"
    b = operator_mail.new_message("alpha", "beta", "beta", "message B")
    b["id"] = "B"
    (inbox / "B.json").write_text(json.dumps(a), encoding="utf-8")
    (inbox / "real-b-renamed.json").write_text(json.dumps(b), encoding="utf-8")

    operator_mail.archive(tmp_path, "beta", ["B"])

    assert [m["id"] for m in operator_mail.pending(tmp_path, "beta")] == ["A"]
    assert [m["id"] for m in operator_mail.history(tmp_path, "beta")] == ["B"]


def test_a_message_file_with_no_id_is_still_archived_by_its_name(tmp_path):
    """Confirming names against contents must not make the function less
    forgiving than it was. A file that claims no id contradicts nothing, so
    its name is the only evidence there is -- and refusing it would leave a
    malformed message in the inbox to be re-delivered for ever, which is the
    failure this function exists to stop."""
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "no-id-here.json").write_text(
        json.dumps({"from": "alpha", "text": "hello"}), encoding="utf-8")

    assert operator_mail.archive(tmp_path, "beta", ["no-id-here"]) == 1
    assert operator_mail.pending(tmp_path, "beta") == []


@pytest.mark.parametrize("payload", ["\x9b2J", "\x9bH", "\x9b31m"])
def test_terminal_rendering_strips_c1_controls(payload):
    """U+009B is CSI. A terminal decoding UTF-8 can act on it directly, so a
    body carrying it clears the reader's screen or repaints it without an
    ESC byte anywhere in the message."""
    msg = operator_mail.new_message(
        "alpha", "beta", "beta", f"before{payload}after")
    out = operator_mail.render_for_terminal([msg])
    assert "\x9b" not in out
    assert "before" in out and "after" in out


def test_rendering_strips_bidirectional_overrides():
    """A right-to-left override cannot run anything, but it reverses what a
    human reads, so a message can be displayed as the opposite of what it
    says. Mail between agents is acted on; it has to say what it means."""
    msg = operator_mail.new_message("alpha", "beta", "beta", "safe\u202egnahc")
    assert "\u202e" not in operator_mail.render_for_terminal([msg])
    assert "\u202e" not in operator_mail.render_line(msg)


def test_a_message_that_cannot_leave_the_inbox_is_not_left_read_and_pending(
        tmp_path, monkeypatch):
    """If the stamped archive copy is written but the inbox copy will not
    delete, the message must not end up in both places -- read in the
    archive and still pending in the inbox is the one state the caller
    cannot reason about. Better to undo the write and offer it again."""
    msg = _msg(tmp_path, text="stuck")
    inbox = operator_mail.inbox_dir(tmp_path, "beta")
    real_unlink = Path.unlink

    def refuse_inbox_unlink(self, *args, **kwargs):
        if self.parent == inbox:
            raise PermissionError("file is locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_inbox_unlink)
    assert operator_mail.archive(tmp_path, "beta", [msg["id"]]) == 0
    monkeypatch.undo()

    assert [m["id"] for m in operator_mail.pending(tmp_path, "beta")] == [msg["id"]]
    assert operator_mail.history(tmp_path, "beta") == []


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
