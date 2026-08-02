#!/usr/bin/env python3
"""Agent-to-agent messaging for the Copilot operator.

Loops run as independent OS processes in separate terminals, so there is no
shared memory between the agents driving them. This module is the mailbox:
plain JSON files under the operator home, one file per message.

Two delivery paths use the same store:

live
    The recipient's Copilot process is running, so the message is typed into
    its session and recorded straight to the archive as already-read.
queued
    Nothing is running (or the sender asked to queue), so the message waits in
    the inbox and is handed to the recipient at the start of its next session,
    or whenever its agent runs ``operator inbox``.

Every rendering names the sender and spells out the exact reply command,
because an agent that cannot work out how to answer will simply not answer.

This module deliberately takes the operator home as a parameter instead of
importing it: ``copilot_operator`` imports *this*, and the reverse import
would be a cycle.
"""
from __future__ import annotations

import json
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "MailError",
    "new_message",
    "queue",
    "record_delivered",
    "pending",
    "pending_count",
    "consume",
    "archive",
    "history",
    "flatten",
    "reply_hint",
    "render_line",
    "sender_names",
    "render_for_agent",
    "render_for_terminal",
]

# Longest message body typed into a live session in one go. The full text is
# always kept in the archive, so nothing is lost -- this only bounds how much
# is pushed through the multiplexer's input path at once.
LIVE_TEXT_LIMIT = 2000

# C1 (\x80-\x9f) is here as well as C0: a terminal reading UTF-8 may treat
# U+009B as CSI, so "\x9b2J" clears the screen with no ESC anywhere in it.
# The bidirectional overrides go too -- they cannot run anything, but they
# reorder what a human sees, so a message can be made to read as the
# opposite of what it says.
_CONTROL = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")


class MailError(Exception):
    """A message could not be stored or read.

    ``consumed`` carries the messages that were already archived before the
    failure, and is empty for every error that happens before anything is
    moved. :func:`consume` archives one message at a time, so a fault part
    way through the batch leaves the earlier ones genuinely read -- written
    to the archive and unlinked from the inbox -- while the exception
    discards the return value that would have shown them to anybody.

    Without this the caller cannot tell the two situations apart, and
    ``operator inbox`` told the agent "nothing has been marked read" on the
    one path where that sentence is false. Those messages were then archived,
    unread, permanently, and nothing anywhere said so -- the module's own
    defect class, reached through its error handler instead of its happy
    path: a claim about an outcome the code never checked.
    """

    def __init__(self, *args: object,
                 consumed: list[dict] | None = None) -> None:
        super().__init__(*args)
        self.consumed: list[dict] = consumed if consumed is not None else []


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mail_root(root: Path) -> Path:
    return Path(root) / "messages"


def inbox_dir(root: Path, instance_id: str) -> Path:
    return mail_root(root) / instance_id / "inbox"


def archive_dir(root: Path, instance_id: str) -> Path:
    return mail_root(root) / instance_id / "archive"


def new_message(sender: str, recipient: str, recipient_id: str,
                text: str) -> dict:
    """Build a message record. ``sent_at`` leads the id so files sort by time."""
    stamp = _utcnow()
    return {
        "id": f"{stamp.replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}",
        "from": sender,
        "to": recipient,
        "to_id": recipient_id,
        "text": text,
        "sent_at": stamp,
        "delivery": "queued",
        "read_at": None,
    }


def _write_json(path: Path, msg: dict) -> None:
    """Replace `path` with `msg` in one indivisible step.

    Readers of a mailbox are other processes, and two of them archiving the
    same message at once is ordinary rather than exotic. A truncating write
    lets one of them observe half a file and discard it as corrupt, which for
    mail means a message that was sent and is now gone. The temp name carries
    a uuid because a fixed one would let two writers interleave into the very
    file this is meant to keep whole.
    """
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(msg, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _ensure_dir(directory: Path) -> None:
    """Create ``directory``, reporting a refusal as :class:`MailError`.

    ``exist_ok=True`` forgives an existing *directory*, not a plain file
    sitting where one belongs -- that raises ``FileExistsError``. Uncaught,
    those escaped as raw tracebacks from `operator send` and from the
    supervisor loop, both of which only know how to handle a ``MailError``.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MailError(f"could not open mailbox {directory}: {exc}") from exc


def _write(directory: Path, msg: dict) -> Path:
    _ensure_dir(directory)
    path = directory / f"{msg['id']}.json"
    try:
        _write_json(path, msg)
    except OSError as exc:
        raise MailError(f"could not write message: {exc}") from exc
    return path


def queue(root: Path, msg: dict) -> Path:
    """Store a message for the recipient to pick up later."""
    return _write(inbox_dir(root, msg["to_id"]), msg)


def record_delivered(root: Path, msg: dict) -> Path:
    """Record a message that was handed over live, so it is not re-delivered.

    It goes straight to the archive as read: the recipient has already seen
    it, and repeating it in the next session's preamble would read as a second
    message rather than the same one.
    """
    delivered = dict(msg, delivery="live", read_at=_utcnow())
    return _write(archive_dir(root, msg["to_id"]), delivered)


class _Unreadable:
    """The result of a read that failed, as distinct from what it found.

    ``json.loads`` raising ``ValueError`` is knowledge about the file: these
    bytes are not a message and never will be. ``read_text`` raising
    ``OSError`` is knowledge about the *read* -- a sharing violation, a
    scanner holding the file open, a permission that will be there again next
    time -- and says nothing whatsoever about the contents. Spelling both
    "corrupt" lets a file that was never seen be treated as one that was seen
    and found wanting, and for mail the second of those authorises moving it
    out of the inbox. `_write_json` already goes to the trouble of atomic
    writes so a reader cannot mistake a half-written file for a corrupt one;
    this is the same hazard arriving through the other door.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unreadable>"


UNREADABLE = _Unreadable()


def _read_message(path: Path) -> dict | None | _Unreadable:
    """One message file, tri-state.

    Returns the message, ``None`` if the file is genuinely not a message, or
    :data:`UNREADABLE` if it could not be read at all. Callers that destroy
    or move files must branch on all three: only ``None`` is evidence.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        # Not every failed read is transient. A directory named like a
        # message, or a dangling symlink, will fail the same way for ever, so
        # leaving it pending would jam the mailbox permanently rather than
        # until a lock clears -- and the broad `except` this replaced did move
        # those aside. That it is not a regular file is knowledge *about the
        # file*, so it belongs with corruption.
        #
        # There is deliberately no `except FileNotFoundError` ahead of this. A
        # genuinely vanished file needs no special case: `is_file()` is False,
        # so it is called corrupt, and the move that follows fails harmlessly
        # because there is nothing to move. Naming the case separately only
        # read as clearer -- it put a dangling symlink, which is permanent, on
        # the same branch as a race, which is not, and so jammed the mailbox
        # in exactly the way this function exists to prevent.
        try:
            regular = path.is_file()
        except OSError:
            # Not belt-and-braces: `is_file()` is not total. It swallows
            # OSError only for `_IGNORED_ERRNOS` -- ENOENT, ENOTDIR, EBADF,
            # ELOOP, plus three WinErrors -- and re-raises everything else,
            # EACCES included, which is documented nowhere in its signature.
            # An inbox that is readable but not searchable reaches here, and
            # without this the exception escapes through `pending()` on every
            # poll of the supervisor loop.
            #
            # It is also this function's own subject one level down: False is
            # a claim about the object, raising is a claim about the attempt,
            # and pathlib merges them. Nothing has been established, so keep
            # the reading that cannot lose a message.
            return UNREADABLE
        return UNREADABLE if regular else None
    except ValueError:
        # Bytes that are not UTF-8 at all. Like malformed JSON this is
        # knowledge about the file and will not change on a retry, so it is
        # corruption rather than an unreadable state. It has to be caught
        # here and not only around `json.loads`: `read_text` decodes, so
        # `UnicodeDecodeError` -- a `ValueError` -- is raised by the read.
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _blocking_ancestor(directory: Path) -> Path | None:
    """The nearest existing component of ``directory`` that is not a directory.

    ``None`` means the path is merely absent, with nothing in the way -- the
    normal state of a mailbox that has never been written to.

    ``os.stat`` is used rather than ``Path.exists``/``Path.is_dir`` on
    purpose: those swallow a documented set of errnos, which is precisely the
    kind of quiet substitution of "no" for "could not tell" this module is
    being cleaned of. A component that cannot be stat'ed at all is returned as
    a blocker, because an unreadable ancestor is not evidence of an empty
    mailbox either.

    ``os.stat`` alone is not enough, though, and the gap it leaves is this
    module's own defect wearing a different hat. ``os.stat`` *follows*
    symlinks, so it raises ``FileNotFoundError`` both for a component that is
    not there and for one that is very much there as a link whose target has
    gone. Walking past the second as though it were the first reported a
    dangling symlink at the mailbox path as "genuinely absent", so ``pending``
    returned ``[]`` and ``pending_count`` returned ``0`` -- measured on this
    branch before this was added, not deduced.

    That is not a cosmetic hole, because such a mailbox is *undeliverable*
    rather than merely unread: ``_write``'s ``mkdir(exist_ok=True)`` raises
    ``FileExistsError`` on a dangling symlink, so nothing can ever be
    delivered into one while every reader calls it empty -- permanent silence
    that reads exactly like a healthy empty mailbox, which is the single
    outcome this module exists to make impossible.

    ``os.lstat`` asks about the entry itself and separates the two. A link
    whose target is missing is therefore a blocker, in the way, and reported.
    """
    for candidate in (directory, *directory.parents):
        try:
            st = os.stat(candidate)
        except FileNotFoundError:
            try:
                os.lstat(candidate)
            except FileNotFoundError:
                # Nothing here at all: keep walking up.
                continue
            except OSError:
                # The entry cannot even be examined, which is not evidence of
                # absence -- the same reading as the ``OSError`` arm below.
                return candidate
            # The entry exists; only its target does not. It is in the way.
            return candidate
        except OSError:
            return candidate
        return None if stat.S_ISDIR(st.st_mode) else candidate
    return None


def _message_files(directory: Path) -> list[Path]:
    """Message files in ``directory``, oldest name first.

    An absent mailbox is genuinely empty and returns ``[]``: nobody has ever
    written to it, which is a complete answer. Anything else that stops the
    listing -- a permission fault, or a plain file sitting where the mailbox
    belongs -- is not an answer at all, and raises :class:`MailError`.

    A first draft of this separated those two by whether they would clear on a
    retry, and reported ENOTDIR as empty on the grounds that raising would jam
    the supervisor loop for ever. That reasoning was wrong twice over, and the
    second reviewer to look at it said so. It does not jam anything: the loop
    catches :class:`MailError`, logs it and launches anyway. And "will not
    clear on a retry" is an argument for reporting it *harder*, not for
    reporting it as the one state that reads as healthy -- a mailbox that is a
    plain file is permanently deaf, so calling it empty is a silence that
    lasts for ever and that nothing anywhere complains about. Permanence is
    the axis that matters one level down in ``_read_message``, where a bad
    file can be moved aside and a jam is real; at the directory level there is
    nothing to move aside and no jam to avoid.

    That distinction is the whole point of this function, and it cannot be
    made with the ``is_dir()``-then-``glob()`` pair it replaces, because both
    halves of that pair lose it in opposite directions. ``Path.glob`` catches
    ``PermissionError`` internally and yields nothing, so an unreadable inbox
    holding real mail produced exactly the observation an empty one produces
    -- measured, not deduced: ``pending``, ``pending_count`` and ``consume``
    all returned 0 for an inbox holding a message. And ``Path.is_dir`` fails
    the other way, re-raising EACCES rather than swallowing it (it only
    ignores ENOENT, ENOTDIR, EBADF, ELOOP and three WinErrors), so the guard
    meant to make the listing safe was itself the thing that could escape
    through ``pending()`` on every poll of the supervisor loop.

    This is the module's own established reading one level up: ``_read_message``
    already refuses to let a file it could not open pass as a file that said
    nothing. The same care was missing for the directory holding them, where
    the cost is higher -- a message that cannot be seen is a peer waiting on a
    reply that will never come, and silence is what a healthy empty mailbox
    looks like too.

    The listing happens exactly once, and the selection is made from what that
    one listing returned. An earlier draft used ``scandir`` only to establish
    that the directory could be read and then re-listed it with ``glob`` to
    select, on the reasoning that this kept the matching rules untouched. A
    reviewer pointed out what that costs: ``Path.glob`` swallows
    ``PermissionError`` and ``NotADirectoryError`` and yields nothing, so
    anything that went wrong in the window *between* the two listings came
    back as an empty mailbox -- the precise defect this function exists to
    remove, reintroduced in the gap between the check and the use.

    The matching rule is preserved without the second listing.
    ``os.path.normcase`` is the platform's own case rule -- it lower-cases on
    Windows and is the identity on POSIX -- which is exactly the difference
    between ``glob("*.json")`` on the two, so ``normcase(name)`` ending in
    ``.json`` selects precisely what ``glob`` selected. Directory entries are
    deliberately still included: a directory named ``*.json`` is not a
    message, and ``_read_message`` one level down is what recognises it and
    moves it aside, which it can only do if it is listed.
    """
    try:
        with os.scandir(directory) as entries:
            names = [entry.name for entry in entries]
    except FileNotFoundError as exc:
        # Not as obvious as it looks, and a reviewer caught it here. On
        # Windows a *parent* component that is a plain file also arrives as
        # FileNotFoundError -- winerror 3, errno 2 -- identical in type and
        # errno to a mailbox nobody has ever written to. Measured: scandir on
        # `<file>/child` and on a simply-absent path are indistinguishable
        # from the exception alone, so returning [] here would have restored
        # the very defect this function exists to remove, one level up the
        # path. The ancestor walk is what separates them.
        blocker = _blocking_ancestor(directory)
        if blocker is not None:
            raise MailError(
                f"could not read mailbox {directory}: {blocker} is not a "
                f"directory") from exc
        # Genuinely absent: nobody has messaged this instance yet, which is
        # the ordinary state of every mailbox on its first run.
        return []
    except OSError as exc:
        raise MailError(f"could not read mailbox {directory}: {exc}") from exc
    return sorted(directory / name for name in names
                  if os.path.normcase(name).endswith(".json"))


def _load_dir(directory: Path) -> list[tuple[Path, dict]]:
    found: list[tuple[Path, dict]] = []
    for path in _message_files(directory):
        data = _read_message(path)
        # Neither a corrupt file nor an unreadable one can be listed, but
        # nothing is destroyed here, so skipping is safe for both: a message
        # that could not be read this time is simply still there next time.
        if isinstance(data, dict):
            found.append((path, data))
    return found


def pending(root: Path, instance_id: str) -> list[dict]:
    """Unread messages, oldest first.

    Raises :class:`MailError` if the inbox exists but cannot be read, rather
    than reporting the empty list that would mean "nothing is waiting".
    """
    return [msg for _, msg in _load_dir(inbox_dir(root, instance_id))]


def pending_count(root: Path, instance_id: str) -> int:
    """How many messages are waiting. Raises :class:`MailError` if unknown."""
    return len(_message_files(inbox_dir(root, instance_id)))


def consume(root: Path, instance_id: str) -> list[dict]:
    """Return unread messages and archive them in one pass.

    Archiving rather than deleting keeps the conversation auditable, which
    matters when the participants are agents and nobody watched it happen.
    """
    inbox = inbox_dir(root, instance_id)
    archive = archive_dir(root, instance_id)
    messages = _message_files(inbox)
    if not messages:
        return []
    read_at = _utcnow()
    taken: list[dict] = []
    _ensure_dir(archive)
    for path in messages:
        data = _read_message(path)
        if data is UNREADABLE:
            # The read failed, so this message has not been seen by anybody.
            # Leaving it pending means it is offered again; moving it aside
            # would archive it unread and leave a mailbox that looks empty
            # rather than blocked. A jam is visible. A loss is not.
            continue
        if data is None:
            # Genuinely not a message. Move it out of the way rather than
            # leaving it to be reconsidered on every single session start.
            try:
                os.replace(path, archive / path.name)
            except OSError:
                pass
            continue
        data["read_at"] = read_at
        try:
            _write_json(archive / path.name, data)
        except OSError as exc:
            raise MailError(f"could not archive message: {exc}",
                            consumed=taken) from exc
        try:
            path.unlink()
        except FileNotFoundError:
            # Another consumer archived and removed it between the glob and
            # here -- `operator inbox` and a session start can land together.
            # Its archive copy is the same message, so there is nothing to
            # undo and no reason to abandon the rest of the batch.
            pass
        except OSError as exc:
            raise MailError(f"could not archive message: {exc}",
                            consumed=taken) from exc
        taken.append(data)
    return taken


def archive(root: Path, instance_id: str, ids: list[str]) -> int:
    """Mark exactly these messages read, leaving any others pending.

    Separate from :func:`consume` because delivery can fail *after* the
    messages have been read: the loop builds a session preamble from pending
    mail, and if that launch then fails and is retried, the mail must still be
    in the inbox. Only once a session is actually running is it archived --
    and only the ids that went into that preamble, so a message that arrived
    in the meantime is not silently swallowed.

    The ids are read out of the message files, so they are exactly as
    trustworthy as those files are: hand-edited, written by an older version,
    or half-written by something that crashed. Resolving one back into a
    filename therefore does two bad things at once. A separator or a ``..``
    escapes the inbox entirely and moves an unrelated JSON file into the
    archive. Far more likely, an id that simply disagrees with the name of the
    file holding it matches nothing, so the message is never archived and the
    loop puts it in the *next* session's preamble too, and the one after that.
    Matching against the files themselves is both safer and more forgiving:
    nothing outside the inbox can be named, and a message is found by the id
    it carries or by the name it is stored under. Names are settled first
    because everything this module writes stores a message under its own id,
    so in the ordinary case nothing but the matched files is ever opened --
    an inbox that has built up does not turn every archive call into a parse
    of all of it.

    Returns the number of messages that are in the archive once this call
    returns, which is not always the number of files it moved: if a
    concurrent reader archives one first, it still counts, because the
    caller's question is whether the message is read, not who moved it.
    """
    inbox = inbox_dir(root, instance_id)
    destination = archive_dir(root, instance_id)
    if not ids:
        return 0
    wanted = {i for i in ids if isinstance(i, str)}
    if not wanted:
        return 0
    files = _message_files(inbox)
    chosen: set[Path] = set()
    found: set[str] = set()
    for path in files:
        if path.stem not in wanted:
            continue
        ident = _message_id(path)
        if ident is UNREADABLE:
            # A file that could not be opened has not agreed with its name.
            # Leave it pending rather than archiving on the strength of a
            # filename nothing has corroborated.
            continue
        # A name is a claim, not proof. Accept it only when the file agrees
        # with it, or when the file claims no id at all and so contradicts
        # nothing. A file named for one message while holding another is a
        # lie about which message it is, and believing it archives the wrong
        # message *and* leaves the requested one to be re-delivered for ever.
        if ident is None or ident == path.stem:
            chosen.add(path)
            found.add(path.stem)
    if found != wanted:
        # The names did not account for every id, so the rest can only be
        # found by reading. Start over rather than adding to the name
        # matches: a name that lost its claim above must not survive here.
        chosen = set()
        found = set()
        for path in files:
            ident = _message_id(path)
            if ident is UNREADABLE:
                # Same as above: unopened is not unnamed. Falling through
                # would reach the `elif` and choose it on its filename.
                continue
            if ident is not None:
                if ident in wanted:
                    chosen.add(path)
                    found.add(ident)
            elif path.stem in wanted:
                chosen.add(path)
                found.add(path.stem)
    if not chosen:
        return 0
    read_at = _utcnow()
    _ensure_dir(destination)
    return sum(_archive_one(path, destination, read_at)
               for path in files if path in chosen)


def _message_id(path: Path) -> str | None | _Unreadable:
    """The id a message file claims.

    ``None`` when the file claims no usable id, :data:`UNREADABLE` when it
    could not be read -- which is not the same claim, and callers that treat
    "claims nothing" as "contradicts nothing" must not extend that courtesy
    to a file they never opened.
    """
    data = _read_message(path)
    if data is UNREADABLE:
        return UNREADABLE
    if not isinstance(data, dict):
        return None
    ident = data.get("id")
    return ident if isinstance(ident, str) else None


def _archive_one(path: Path, destination: Path, read_at: str) -> int:
    """Move one inbox file into `destination`, stamping it read if it parses."""
    target = destination / path.name
    data = _read_message(path)
    if data is UNREADABLE:
        # Archiving a file whose contents were never read would file away a
        # message nobody has seen. Report nothing archived and leave it
        # pending: the caller offers it again, which costs a re-delivery.
        return 0
    if isinstance(data, dict):
        data["read_at"] = read_at
        try:
            _write_json(target, data)
        except OSError:
            pass
        else:
            try:
                path.unlink()
                return 1
            except FileNotFoundError:
                # Another reader took it between the scan and here. Its copy
                # of the archive file says the same thing, and the message is
                # archived either way, which is what the count means.
                return 1
            except OSError:
                # The stamped copy is written but the inbox copy will not go.
                # Falling through to the move below would overwrite the
                # stamped copy with an unstamped one and, when it failed too,
                # leave the message in the archive AND the inbox -- read and
                # pending at once. Undo the write and report nothing
                # archived: the message stays pending, which is a state the
                # caller can act on, and it will be offered again.
                try:
                    target.unlink()
                except OSError:
                    pass
                return 0
    try:
        os.replace(path, target)
        return 1
    except OSError:
        return 0


def history(root: Path, instance_id: str, limit: int = 20) -> list[dict]:
    """Most recent already-read messages, oldest first."""
    archived = [msg for _, msg in _load_dir(archive_dir(root, instance_id))]
    return archived[-limit:] if limit > 0 else archived


def flatten(text: str) -> str:
    """Collapse a message to one control-character-free line.

    Newlines cannot survive live delivery: the multiplexer would submit the
    line at the first one and drop the rest. The stored copy keeps the
    original text, so this only affects what is typed into a live session.
    """
    return " ".join(_CONTROL.sub(" ", text).split())


def _field(msg: dict, key: str, fallback: str) -> str:
    """One safe, single-line rendering of `msg[key]`.

    Every field here came off disk, where some other process wrote it, so it
    is not guaranteed to be present, to be a string, or to be free of control
    characters -- and each of those is a separate failure.

    Control characters are the dangerous one. The body has been flattened
    since this module was written, because the multiplexer submits the line at
    the first newline and would drop the rest. The *names* travel that same
    keystroke path and never got the same treatment: ``--from`` is taken
    verbatim from the command line (only ``--to`` is checked against known
    instances), so a sender called ``"\\n/exit"`` ends the line early and types
    a command into the recipient's session.

    JSON null is the quiet one. The key is present, so ``.get(key, default)``
    hands back None and every string operation downstream raises -- on the
    session-preamble path, which means one bad file stops sessions starting
    and nothing can clear the mailbox.
    """
    value = msg.get(key)
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    return flatten(value) or fallback


def reply_hint(msg: dict) -> str:
    """The exact command that answers `msg`.

    A missing name becomes a visible placeholder rather than a guess. Filling
    one in would produce a command that runs happily and sends the reply to
    the wrong agent.
    """
    sender = _field(msg, "from", "<sender>")
    recipient = _field(msg, "to", "<your-instance>")
    return f'operator send --from {recipient} --to {sender} "your reply"'


def render_line(msg: dict) -> str:
    """One-line form typed into a live session.

    The bracketed prefix is not decoration: it guarantees the text never
    starts with '/' or '@', which a terminal UI would read as a slash command
    or a mention rather than as a message.
    """
    text = _field(msg, "text", "")
    if len(text) > LIVE_TEXT_LIMIT:
        text = text[:LIVE_TEXT_LIMIT] + " […truncated, see: operator inbox --history]"
    return (f'[operator message from "{_field(msg, "from", "?")}"] {text} '
            f'(To reply, run: {reply_hint(msg)})')


def sender_names(msgs: list[dict]) -> list[str]:
    """The distinct sender names in `msgs`, rendered safely and sorted.

    Callers summarising a batch need this rather than a raw set comprehension
    over ``m["from"]``: a null name makes ``sorted`` raise on comparing None
    with a string, and a name carrying control characters would go straight
    into a log line or a terminal.
    """
    return sorted({_field(m, "from", "?") for m in msgs})


def render_for_agent(msgs: list[dict]) -> str:
    """Block appended to a session preamble for messages that arrived while
    no session was running."""
    if not msgs:
        return ""
    senders = sender_names(msgs)
    lines = [
        f" You have {len(msgs)} operator message(s) waiting from "
        f"{', '.join(repr(s) for s in senders)}. These are from other agents, "
        "not from the human. Read them and act on them as part of this "
        "session, and reply if a reply is warranted."
    ]
    for i, msg in enumerate(msgs, 1):
        lines.append(
            f' [{i}] from "{_field(msg, "from", "?")}" at '
            f'{_field(msg, "sent_at", "?")}: {_field(msg, "text", "")} '
            f'(To reply: {reply_hint(msg)})')
    return " ".join(lines)


def render_for_terminal(msgs: list[dict]) -> str:
    if not msgs:
        return "No messages."
    out: list[str] = []
    for msg in msgs:
        state = _field(msg, "delivery", "queued")
        out.append(f'  from "{_field(msg, "from", "?")}" · '
                   f'{_field(msg, "sent_at", "?")} · {state}')
        body = msg.get("text")
        body = "" if body is None else str(body)
        # Printed for a human, so the line structure is kept and so is the
        # indentation inside each line -- a body is often a code snippet, and
        # flattening it here would make it unreadable. Control characters
        # still go: an escape sequence would repaint the reader's screen.
        for line in body.splitlines() or [""]:
            out.append(f"      {_CONTROL.sub(' ', line)}")
        out.append(f"      reply: {reply_hint(msg)}")
        out.append("")
    return "\n".join(out).rstrip()
