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
    "render_for_agent",
    "render_for_terminal",
]

# Longest message body typed into a live session in one go. The full text is
# always kept in the archive, so nothing is lost -- this only bounds how much
# is pushed through the multiplexer's input path at once.
LIVE_TEXT_LIMIT = 2000

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


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


def _write(directory: Path, msg: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{msg['id']}.json"
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(msg, indent=2), encoding="utf-8")
        os.replace(tmp, path)
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
            (archive / path.name).write_text(
                json.dumps(data, indent=2), encoding="utf-8")
            path.unlink()
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
    """
    inbox = inbox_dir(root, instance_id)
    destination = archive_dir(root, instance_id)
    if not inbox.is_dir() or not ids:
        return 0
    read_at = _utcnow()
    moved = 0
    destination.mkdir(parents=True, exist_ok=True)
    for ident in ids:
        path = inbox / f"{ident}.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["read_at"] = read_at
                (destination / path.name).write_text(
                    json.dumps(data, indent=2), encoding="utf-8")
                path.unlink()
                moved += 1
                continue
        except (OSError, ValueError):
            pass
        try:
            os.replace(path, destination / path.name)
            moved += 1
        except OSError:
            pass
    return moved


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


def reply_hint(msg: dict) -> str:
    return (f'operator send --from {msg["to"]} --to {msg["from"]} "your reply"')


def render_line(msg: dict) -> str:
    """One-line form typed into a live session.

    The bracketed prefix is not decoration: it guarantees the text never
    starts with '/' or '@', which a terminal UI would read as a slash command
    or a mention rather than as a message.
    """
    text = flatten(msg.get("text", ""))
    if len(text) > LIVE_TEXT_LIMIT:
        text = text[:LIVE_TEXT_LIMIT] + " […truncated, see: operator inbox --history]"
    return (f'[operator message from "{msg["from"]}"] {text} '
            f'(To reply, run: {reply_hint(msg)})')


def render_for_agent(msgs: list[dict]) -> str:
    """Block appended to a session preamble for messages that arrived while
    no session was running."""
    if not msgs:
        return ""
    senders = sorted({m.get("from", "?") for m in msgs})
    lines = [
        f" You have {len(msgs)} operator message(s) waiting from "
        f"{', '.join(repr(s) for s in senders)}. These are from other agents, "
        "not from the human. Read them and act on them as part of this "
        "session, and reply if a reply is warranted."
    ]
    for i, msg in enumerate(msgs, 1):
        lines.append(
            f' [{i}] from "{msg.get("from", "?")}" at {msg.get("sent_at", "?")}: '
            f'{flatten(msg.get("text", ""))} '
            f'(To reply: {reply_hint(msg)})')
    return " ".join(lines)


def render_for_terminal(msgs: list[dict]) -> str:
    if not msgs:
        return "No messages."
    out: list[str] = []
    for msg in msgs:
        state = msg.get("delivery", "queued")
        out.append(f'  from "{msg.get("from", "?")}" · {msg.get("sent_at", "?")} · {state}')
        for line in str(msg.get("text", "")).splitlines() or [""]:
            out.append(f"      {line}")
        out.append(f"      reply: {reply_hint(msg)}")
        out.append("")
    return "\n".join(out).rstrip()
