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
    """A message could not be stored or read."""


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


def _write(directory: Path, msg: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
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


def _load_dir(directory: Path) -> list[tuple[Path, dict]]:
    if not directory.is_dir():
        return []
    found: list[tuple[Path, dict]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt file must not jam the mailbox forever. Skip it here;
            # consume() moves it aside so it stops being reconsidered.
            continue
        if isinstance(data, dict):
            found.append((path, data))
    return found


def pending(root: Path, instance_id: str) -> list[dict]:
    """Unread messages, oldest first."""
    return [msg for _, msg in _load_dir(inbox_dir(root, instance_id))]


def pending_count(root: Path, instance_id: str) -> int:
    directory = inbox_dir(root, instance_id)
    if not directory.is_dir():
        return 0
    return sum(1 for _ in directory.glob("*.json"))


def consume(root: Path, instance_id: str) -> list[dict]:
    """Return unread messages and archive them in one pass.

    Archiving rather than deleting keeps the conversation auditable, which
    matters when the participants are agents and nobody watched it happen.
    """
    inbox = inbox_dir(root, instance_id)
    archive = archive_dir(root, instance_id)
    if not inbox.is_dir():
        return []
    read_at = _utcnow()
    taken: list[dict] = []
    archive.mkdir(parents=True, exist_ok=True)
    for path in sorted(inbox.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            # Move unreadable files out of the way rather than leaving them to
            # be re-read on every single session start.
            try:
                os.replace(path, archive / path.name)
            except OSError:
                pass
            continue
        data["read_at"] = read_at
        try:
            _write_json(archive / path.name, data)
        except OSError as exc:
            raise MailError(f"could not archive message: {exc}") from exc
        try:
            path.unlink()
        except FileNotFoundError:
            # Another consumer archived and removed it between the glob and
            # here -- `operator inbox` and a session start can land together.
            # Its archive copy is the same message, so there is nothing to
            # undo and no reason to abandon the rest of the batch.
            pass
        except OSError as exc:
            raise MailError(f"could not archive message: {exc}") from exc
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
    if not inbox.is_dir() or not ids:
        return 0
    wanted = {i for i in ids if isinstance(i, str)}
    if not wanted:
        return 0
    files = sorted(inbox.glob("*.json"))
    chosen: set[Path] = set()
    found: set[str] = set()
    for path in files:
        if path.stem not in wanted:
            continue
        ident = _message_id(path)
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
    destination.mkdir(parents=True, exist_ok=True)
    return sum(_archive_one(path, destination, read_at)
               for path in files if path in chosen)


def _message_id(path: Path) -> str | None:
    """The id a message file claims, or None if it does not claim a usable one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ident = data.get("id")
    return ident if isinstance(ident, str) else None


def _archive_one(path: Path, destination: Path, read_at: str) -> int:
    """Move one inbox file into `destination`, stamping it read if it parses."""
    target = destination / path.name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
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
