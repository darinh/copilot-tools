"""Guards for the conversation store, its seeders and its viewer.

Every check here is paired with a positive control -- a case that makes the
guard fire -- because a guard that cannot fail reads exactly like coverage.
The classification tests in particular are scored against text captured
verbatim from this machine's own session store rather than against text
invented to match the regex, since a detector written and tested against its
own author's idea of the input is a detector that has agreed with itself.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import conversation_log as clog

# Captured verbatim from ~/.copilot/session-store.db and ~/.operator/messages.
# Shortened, but not reworded: the prefixes are the load-bearing part.
REAL_PREAMBLE = (
    "You are running under an automated operator wrapper that a human set up. "
    "Key facts: (1) You have blanket human approval for ALL decisions")
REAL_PEER = (
    '[operator message from "scripts"] Ack - nothing outstanding on my side. '
    "Agreed on the framing for 0011")
REAL_HUMAN = "search the session history for my previous instructions."


@pytest.fixture()
def conn():
    connection = clog.connect(Path(":memory:"))
    yield connection
    connection.close()


def _add(connection, **kwargs):
    kwargs.setdefault("source", clog.SOURCE_HOOK)
    kwargs.setdefault("direction", clog.INBOUND)
    kwargs.setdefault("sent_at", "2026-08-09T12:00:00Z")
    return clog.record(connection, **kwargs)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def test_a_human_prompt_is_filed_as_a_human_speaking():
    assert clog.classify(REAL_HUMAN) == (clog.HUMAN, clog.HUMAN_AGENT, "")


def test_the_operator_preamble_is_not_filed_as_the_human_speaking():
    actor, channel, sender = clog.classify(REAL_PREAMBLE)
    assert actor == clog.SYSTEM
    assert sender == "operator"
    assert channel == clog.HUMAN_AGENT


def test_a_peer_message_is_filed_as_an_agent_and_names_the_sender():
    assert clog.classify(REAL_PEER) == (clog.AGENT, clog.AGENT_AGENT, "scripts")


def test_the_preamble_marker_is_not_matched_arbitrarily_far_in():
    """A human quoting the preamble mid-message is still a human.

    The marker is only consulted in the opening characters because that is
    where the wrapper puts it. Without the bound, asking an agent *about* its
    preamble reclassifies the question as machine text -- and the question is
    exactly the kind this store exists to find again later.
    """
    quoted = "why does " + ("x" * 500) + " " + REAL_PREAMBLE
    assert clog.classify(quoted)[0] == clog.HUMAN


def test_a_peer_prefix_must_be_at_the_start():
    quoted = 'I got a message that said [operator message from "scripts"] hello'
    assert clog.classify(quoted)[0] == clog.HUMAN
    assert clog.peer_sender(quoted) is None


def test_curly_quotes_in_a_peer_prefix_still_name_the_sender():
    """Terminals and copy-paste substitute typographic quotes.

    The control below is the same string with straight quotes; if the pattern
    stopped accepting either spelling one of the two would fail.
    """
    assert clog.peer_sender('[operator message from \u201cscripts\u201d] hi') == "scripts"
    assert clog.peer_sender('[operator message from "scripts"] hi') == "scripts"


@pytest.mark.parametrize("text", ["", "   ", "just a sentence"])
def test_ordinary_text_names_no_peer(text):
    assert clog.peer_sender(text) is None


# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------

def test_a_question_mark_marks_a_question():
    assert clog.asks_question("does the seeder dedupe?")


def test_a_question_mark_inside_a_code_fence_does_not():
    """The filter answers "was I asked something", not "does this contain ?".

    Agent replies are full of fenced code, and a regex or a ternary in one
    would otherwise flag every message that quoted code as a question.
    """
    assert not clog.asks_question("here you go:\n```\nre.compile(r'a?b')\n```\n")
    assert clog.asks_question("here you go:\n```\nx?y\n```\nis that right?")


# --------------------------------------------------------------------------
# Project naming
# --------------------------------------------------------------------------

def test_a_worktree_is_grouped_with_the_repository_it_belongs_to():
    """Every agent here works in ``<repo>/.worktrees/<branch>``.

    Taking the last path segment would file each branch as its own project and
    scatter one project's history across a dozen entries in the sidebar.
    """
    assert clog.project_of("", r"C:\Users\d\repos\prism\.worktrees\feat-x") == "prism"
    assert clog.project_of("", "/home/d/repos/prism/.worktrees/feat-x") == "prism"


def test_a_plain_checkout_is_named_after_its_directory():
    assert clog.project_of("", r"C:\Users\d\repos\prism") == "prism"


def test_the_repository_name_wins_over_the_path():
    """The same project is checked out at many paths; its name is one thing."""
    assert clog.project_of("darinh/copilot-tools", r"C:\anywhere") == "copilot-tools"


# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------

def test_both_timestamp_spellings_normalise_to_one():
    """The two sources disagree, and the viewer sorts on this column.

    The session store writes ``'YYYY-MM-DD HH:MM:SS'`` and operator mail writes
    ISO-8601 with a ``Z``. Stored as written, a lexical sort interleaves them
    wrongly -- ``' '`` sorts before ``'T'`` -- so a day's messages come back
    in two separate runs rather than in order.
    """
    assert clog._utc("2026-08-09 12:00:00") == "2026-08-09T12:00:00Z"
    assert clog._utc("2026-08-09T12:00:00Z") == "2026-08-09T12:00:00Z"
    assert clog._utc("2026-08-09T12:00:00+00:00") == "2026-08-09T12:00:00Z"


def test_an_offset_timestamp_is_converted_rather_than_truncated():
    assert clog._utc("2026-08-09T14:00:00+02:00") == "2026-08-09T12:00:00Z"


def test_an_unreadable_timestamp_reports_itself_rather_than_raising():
    assert clog._utc("not a date") == ""
    assert clog._utc("") == ""


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def test_the_same_message_twice_is_stored_once(conn):
    """Idempotence is the property that makes re-seeding safe.

    The user seeds manually, per machine, which means the seeder *will* be run
    twice over the same data. One that appends on the second run is one nobody
    can safely repeat.
    """
    assert _add(conn, source_id="x", body="hello") is True
    assert _add(conn, source_id="x", body="hello") is False
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_two_sources_may_share_an_id(conn):
    """The natural key is (source, source_id), not source_id alone.

    Mail ids and session turn ids are minted by different systems and neither
    knows about the other.
    """
    assert _add(conn, source=clog.SOURCE_HOOK, source_id="1", body="a")
    assert _add(conn, source=clog.SOURCE_OPERATOR_MAIL, source_id="1", body="b")
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_an_empty_message_is_not_stored(conn):
    assert _add(conn, source_id="e", body="   ") is False
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_an_agent_reply_is_never_reclassified_by_its_own_text(conn):
    """An agent quoting the preamble back is still the agent speaking.

    Outbound direction settles the speaker; the text does not get a vote.
    Without this, an agent explaining its own preamble would be filed as the
    operator, in a store whose whole purpose is knowing who said what.
    """
    _add(conn, source_id="r", body=REAL_PREAMBLE, direction=clog.OUTBOUND)
    row = conn.execute("SELECT actor, channel FROM messages").fetchone()
    assert row["actor"] == clog.AGENT
    assert row["channel"] == clog.HUMAN_AGENT


# --------------------------------------------------------------------------
# Querying
# --------------------------------------------------------------------------

def test_an_unknown_filter_value_is_refused_rather_than_ignored(conn):
    """Silently dropping an unrecognised filter returns *more* than asked for.

    A viewer that answers a filtered question with unfiltered data is worse
    than one that errors, because the extra rows look like results.
    """
    with pytest.raises(clog.ConversationError):
        clog.query(conn, actor="nonsense")
    with pytest.raises(clog.ConversationError):
        clog.query(conn, channel="nonsense")
    clog.query(conn, actor=clog.HUMAN)  # control: a real value is accepted


def test_filter_values_cannot_reach_the_sql(conn):
    _add(conn, source_id="q", body="hello")
    with pytest.raises(clog.ConversationError):
        clog.query(conn, direction="inbound'; DROP TABLE messages; --")
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_a_search_term_of_pure_punctuation_does_not_raise(conn):
    """FTS5 treats ``-``, ``*``, ``:`` and ``(`` as query syntax.

    Searching for a flag or a path is the normal case here, and an unescaped
    ``--force`` raises ``sqlite3.OperationalError`` from inside MATCH.
    """
    _add(conn, source_id="p", body="pass --force to override")
    for term in ("--force", 'a"b', "x:y", "(", "*", "NOT", r"C:\path\to.py"):
        clog.query(conn, search=term)


def test_search_finds_a_word_by_content(conn):
    _add(conn, source_id="s1", body="the seeder deduplicates rows")
    _add(conn, source_id="s2", body="something else entirely")
    found = clog.query(conn, search="deduplicates")
    assert [r["source_id"] for r in found] == ["s1"]


def test_search_survives_a_deleted_row(conn):
    """The FTS mirror is trigger-maintained; a stale index over-reports."""
    _add(conn, source_id="d1", body="ephemeral content here")
    conn.execute("DELETE FROM messages WHERE source_id = 'd1'")
    conn.commit()
    assert clog.query(conn, search="ephemeral") == []


def test_dates_filter_on_the_day(conn):
    _add(conn, source_id="d-early", body="early", sent_at="2026-08-01T09:00:00Z")
    _add(conn, source_id="d-late", body="late", sent_at="2026-08-05T09:00:00Z")
    got = clog.query(conn, date_from="2026-08-05", date_to="2026-08-05")
    assert [r["source_id"] for r in got] == ["d-late"]


def test_messages_come_back_newest_first(conn):
    _add(conn, source_id="t1", body="first", sent_at="2026-08-01T09:00:00Z")
    _add(conn, source_id="t2", body="second", sent_at="2026-08-02T09:00:00Z")
    assert [r["source_id"] for r in clog.query(conn)] == ["t2", "t1"]


def test_the_agent_mail_view_excludes_human_conversation(conn):
    _add(conn, source_id="h", body=REAL_HUMAN)
    _add(conn, source_id="a", body=REAL_PEER, source=clog.SOURCE_OPERATOR_MAIL,
         channel=clog.AGENT_AGENT, actor=clog.AGENT, sender="scripts")
    mail = clog.query(conn, channel=clog.AGENT_AGENT)
    assert [r["source_id"] for r in mail] == ["a"]


def test_the_limit_is_bounded(conn):
    """A viewer request cannot ask for the whole store in one response.

    Both ends are scored, and the fixture is deliberately larger than the cap.
    An earlier version stored five rows and asserted ``limit=10**9`` returned
    five -- true whether the limit is clamped, mis-clamped or passed straight
    through, so the assertion held however the code was written. Below the cap
    nothing can distinguish them; the store has to be bigger than 1000.
    """
    for i in range(1001):
        _add(conn, source_id=f"n{i}", body=f"message {i}")
    assert len(clog.query(conn, limit=0)) == 1
    assert len(clog.query(conn, limit=-5)) == 1
    assert len(clog.query(conn, limit=10 ** 9)) == 1000


# --------------------------------------------------------------------------
# Seeding: the spool
# --------------------------------------------------------------------------

def _spool(tmp_path: Path, *events) -> Path:
    directory = tmp_path / "conversation-spool"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "2026-08-09.jsonl").write_text(
        "\n".join(json.dumps(e) if isinstance(e, dict) else e
                  for e in events), encoding="utf-8")
    return directory


def test_a_half_written_line_is_counted_not_fatal(conn, tmp_path):
    """The writer is a live session and may be mid-append.

    Refusing the whole file over its last line would drop a day of capture at
    the moment it is being written.
    """
    directory = _spool(
        tmp_path,
        {"id": "ok", "direction": "inbound", "body": "a real prompt",
         "sent_at": "2026-08-09T10:00:00Z"},
        '{"id": "trunc", "direct')
    report = clog.ingest_spool(conn, directory)
    assert report.added == 1
    assert report.skipped.get("unparseable line") == 1
    assert not report.errors


def test_the_spool_is_not_consumed_by_ingesting_it(conn, tmp_path):
    """Re-reading is free; a crash between read and commit must not lose it."""
    directory = _spool(tmp_path, {"id": "k", "direction": "inbound",
                                  "body": "keep me",
                                  "sent_at": "2026-08-09T10:00:00Z"})
    clog.ingest_spool(conn, directory)
    assert list(directory.glob("*.jsonl"))
    again = clog.ingest_spool(conn, directory)
    assert again.added == 0 and again.duplicate == 1


def test_a_peer_message_in_the_spool_is_declined(conn, tmp_path):
    """`operator send` already persists every message it delivers.

    284 of the 286 mail files on the machine this was written on are
    ``delivery: "live"``. The copy landing in the recipient's prompt stream is
    a second record with worse fields -- no recipient, no delivery state, and
    a timestamp describing when it was read.
    """
    directory = _spool(tmp_path, {"id": "p", "direction": "inbound",
                                  "body": REAL_PEER,
                                  "sent_at": "2026-08-09T10:00:00Z"})
    report = clog.ingest_spool(conn, directory)
    assert report.added == 0
    assert report.skipped.get("peer message, held by operator mail") == 1


def test_an_event_with_an_unknown_direction_is_refused(conn, tmp_path):
    directory = _spool(tmp_path, {"id": "u", "direction": "sideways",
                                  "body": "x"})
    assert clog.ingest_spool(conn, directory).skipped.get("unknown direction") == 1


def test_an_absent_spool_is_not_an_error(conn, tmp_path):
    report = clog.ingest_spool(conn, tmp_path / "never-captured")
    assert report.absent and not report.failed


# --------------------------------------------------------------------------
# Seeding: operator mail
# --------------------------------------------------------------------------

def _mailbox(tmp_path: Path, *messages) -> Path:
    root = tmp_path / "messages" / "copilot-tools"
    root.mkdir(parents=True, exist_ok=True)
    for i, msg in enumerate(messages):
        (root / f"2026080{i}T000000Z-{i}.json").write_text(
            json.dumps(msg), encoding="utf-8")
    return tmp_path / "messages"


MAIL = {"id": "76a8c038", "from": "scripts", "to": "copilot-tools",
        "to_id": "copilot-tools", "text": "Ack - nothing outstanding.",
        "sent_at": "2026-08-01T02:23:02Z", "delivery": "live"}


def test_mail_is_filed_as_an_agent_conversation(conn, tmp_path):
    clog.seed_operator_mail(conn, _mailbox(tmp_path, MAIL))
    row = conn.execute("SELECT * FROM messages").fetchone()
    assert row["channel"] == clog.AGENT_AGENT
    assert row["sender"] == "scripts"
    assert row["recipient"] == "copilot-tools"
    assert row["sent_at"] == "2026-08-01T02:23:02Z"


def test_a_message_that_moved_to_the_archive_is_not_counted_twice(conn, tmp_path):
    """Inbox to archive is the normal life of every message here.

    Keying on the path rather than the id would double-count each one the
    first time it was read.
    """
    root = _mailbox(tmp_path, MAIL)
    archive = root / "copilot-tools" / "archive"
    archive.mkdir(parents=True)
    (archive / "copy.json").write_text(json.dumps(MAIL), encoding="utf-8")
    report = clog.seed_operator_mail(conn, root)
    assert report.added == 1
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_an_unreadable_mail_file_is_an_error_not_a_silent_skip(conn, tmp_path):
    root = _mailbox(tmp_path, MAIL)
    (root / "copilot-tools" / "broken.json").write_text("{not json",
                                                        encoding="utf-8")
    report = clog.seed_operator_mail(conn, root)
    assert report.added == 1
    assert report.failed and len(report.errors) == 1


def test_an_absent_mailbox_is_not_a_failure(conn, tmp_path):
    """A machine that never ran a peer agent has no mail directory.

    Reported as absent, not as an error, so `operator conversations seed`
    does not exit non-zero on a clean install.
    """
    report = clog.seed_operator_mail(conn, tmp_path / "no-mail")
    assert report.absent
    assert not report.failed


def test_a_present_but_unreadable_source_is_distinguished_from_an_absent_one(conn, tmp_path):
    """The control for the test above. These must not report identically.

    Absent means "not on this machine" and is fine; unreadable means "here and
    broken" and must fail the run. Collapsing the two either fails clean
    installs or swallows real corruption.
    """
    root = _mailbox(tmp_path, MAIL)
    (root / "copilot-tools" / "broken.json").write_text("{", encoding="utf-8")
    present = clog.seed_operator_mail(conn, root)
    absent = clog.seed_operator_mail(conn, tmp_path / "gone")
    assert present.failed and not present.absent
    assert absent.absent and not absent.failed


# --------------------------------------------------------------------------
# Seeding: the Copilot session store
# --------------------------------------------------------------------------

def _session_store(tmp_path: Path, turns) -> Path:
    path = tmp_path / "session-store.db"
    src = sqlite3.connect(str(path))
    src.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT,"
        " repository TEXT, host_type TEXT, branch TEXT, summary TEXT,"
        " created_at TEXT, updated_at TEXT);"
        "CREATE TABLE turns (session_id TEXT, turn_index INTEGER,"
        " user_message TEXT, assistant_response TEXT, timestamp TEXT);")
    src.execute("INSERT INTO sessions (id, cwd, repository, branch)"
                " VALUES ('s1', ?, 'darinh/copilot-tools', 'main')",
                (r"C:\repos\copilot-tools",))
    for i, (prompt, reply) in enumerate(turns):
        src.execute("INSERT INTO turns VALUES ('s1', ?, ?, ?, ?)",
                    (i, prompt, reply, "2026-08-09 12:00:00"))
    src.commit()
    src.close()
    return path


def test_a_turn_becomes_a_prompt_and_a_reply(conn, tmp_path):
    store = _session_store(tmp_path, [(REAL_HUMAN, "Here is what I found.")])
    report = clog.seed_session_store(conn, store)
    assert report.added == 2
    rows = {r["direction"]: r for r in conn.execute("SELECT * FROM messages")}
    assert rows[clog.INBOUND]["actor"] == clog.HUMAN
    assert rows[clog.OUTBOUND]["actor"] == clog.AGENT
    assert rows[clog.INBOUND]["project"] == "copilot-tools"
    assert rows[clog.INBOUND]["branch"] == "main"


def test_a_turn_with_no_reply_still_records_the_prompt(conn, tmp_path):
    """76% of turns in the real store have a NULL ``assistant_response``.

    Dropping the whole turn would discard three quarters of what the human
    actually typed, which is the half of the record that cannot be recovered
    from anywhere else.
    """
    store = _session_store(tmp_path, [(REAL_HUMAN, None)])
    report = clog.seed_session_store(conn, store)
    assert report.added == 1
    assert report.skipped.get("empty response") == 1


def test_the_preamble_is_seeded_but_not_as_the_human(conn, tmp_path):
    """It is kept, because it is the record of what the agent was told.

    It is not kept as human speech, because 39% of turns are preambles and
    filing them that way makes "what did I ask?" unanswerable.
    """
    store = _session_store(tmp_path, [(REAL_PREAMBLE, None)])
    clog.seed_session_store(conn, store)
    row = conn.execute("SELECT actor FROM messages").fetchone()
    assert row["actor"] == clog.SYSTEM
    assert clog.query(conn, actor=clog.HUMAN) == []


def test_peer_turns_are_left_to_the_mail_store(conn, tmp_path):
    store = _session_store(tmp_path, [(REAL_PEER, None)])
    report = clog.seed_session_store(conn, store)
    assert report.added == 0
    assert report.skipped.get("peer message, held by operator mail") == 1


def test_reseeding_the_session_store_adds_nothing(conn, tmp_path):
    store = _session_store(tmp_path, [(REAL_HUMAN, "a reply")])
    clog.seed_session_store(conn, store)
    again = clog.seed_session_store(conn, store)
    assert again.added == 0 and again.duplicate == 2


def test_an_absent_session_store_is_not_a_failure(conn, tmp_path):
    report = clog.seed_session_store(conn, tmp_path / "nothing.db")
    assert report.absent and not report.failed


def test_seeding_never_writes_to_the_session_store(conn, tmp_path, monkeypatch):
    """It is somebody else's live database, opened ``mode=ro``.

    The first version of this test opened its *own* read-only connection and
    asserted a write failed. That is a fact about SQLite, true whatever this
    module does -- it passed with the seeder opening the file read-write, and
    a mutation proved it. The connection string is now taken from the code
    under test and *then* shown to refuse writes, so the assertion depends on
    the thing it claims to be checking.
    """
    seen: list[object] = []
    real_connect = sqlite3.connect

    def spy(target, *args, **kwargs):
        seen.append(target)
        return real_connect(target, *args, **kwargs)

    store = _session_store(tmp_path, [(REAL_HUMAN, "a reply")])
    monkeypatch.setattr(clog.sqlite3, "connect", spy)
    assert clog.seed_session_store(conn, store).added == 2

    used = [t for t in seen if isinstance(t, str) and "session-store" in t]
    assert used, "the seeder did not open the session store"
    src = sqlite3.connect(used[0], uri=True)
    with pytest.raises(sqlite3.OperationalError):
        src.execute("DELETE FROM turns")
    src.close()


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def test_a_report_distinguishes_nothing_new_from_nothing_accepted():
    """Both end with zero added; only one of them is fine."""
    quiet = clog.SeedReport("x")
    quiet.duplicate = 10
    rejected = clog.SeedReport("x")
    rejected.skip("empty response")
    assert "already present" in quiet.describe()
    assert "empty response" in rejected.describe()
    assert quiet.describe() != rejected.describe()


def test_summary_reports_which_search_is_in_force(conn):
    """FTS5 is a compile-time option and the two modes answer differently.

    A viewer that quietly returned fewer results on one machine would be
    indistinguishable from one holding fewer messages.
    """
    assert clog.summary(conn)["search_mode"] in ("fts", "substring")
    assert clog.summary(conn)["search_mode"] == clog.search_mode(conn)


# --- The capture half lives in JavaScript. These pin the seam. ---------------

EXTENSION = (Path(__file__).resolve().parents[1] / "extensions"
             / "conversation-capture" / "extension.mjs")


def _extension_source() -> str:
    return EXTENSION.read_text(encoding="utf-8")


def test_the_spool_directory_is_spelled_the_same_in_both_languages():
    """The one string this feature must agree on across a language boundary.

    The extension appends to a directory named in JavaScript; the ingester
    reads one named in Python. Nothing at runtime compares them -- a rename on
    either side produces a spool nobody reads and an ingest that finds
    nothing, and *both* halves report success, because "no new events" is
    indistinguishable from "no events happened".
    """
    name = clog.spool_dir(Path("/anywhere")).name
    assert f'"{name}"' in _extension_source(), (
        f"conversation_log.spool_dir writes {name!r}; the extension does not "
        "name that directory")


def test_the_spool_directory_check_can_fail():
    """Positive control for the guard above."""
    assert '"conversation-spool-that-nothing-writes"' not in _extension_source()


def test_the_extension_reads_the_same_home_variable_the_toolkit_does():
    """`OPERATOR_HOME` is an invention; the toolkit uses this one. Getting it
    wrong sends captured events to a directory the ingester never reads."""
    assert "COPILOT_OPERATOR_HOME" in _extension_source()
    assert "process.env.OPERATOR_HOME" not in _extension_source()


def test_the_extension_records_finished_replies_and_not_reasoning():
    """The ask was for what was said, not how it was arrived at. Subscribing
    to the delta events would multiply the volume and store thinking the user
    explicitly did not want."""
    source = _extension_source()
    assert 'session?.on("assistant.message"' in source
    for noisy in ("assistant.reasoning_delta", "assistant.message_delta"):
        assert f'on("{noisy}"' not in source, f"{noisy} is subscribed to"


def test_the_ingester_reads_the_fields_the_extension_writes(tmp_path):
    """The cheap half of the seam check, kept for machines without node.

    Scored against the *builders* in `events.mjs`, which is where the key
    names now live. This asserts only that the spellings exist; that the code
    produces them is asserted by executing it, below. Keeping both is
    deliberate -- this one runs everywhere, and a missing key name here is a
    clearer failure than a mismatched row count.
    """
    source = (EXTENSION.parent / "events.mjs").read_text(encoding="utf-8")
    keys = ["id", "direction", "body", "cwd", "session_id", "sent_at"]
    for key in keys:
        assert f"{key}:" in source, f"the extension no longer writes {key}"
    spool = tmp_path / "conversation-spool"
    spool.mkdir()
    (spool / "2026-08-09.jsonl").write_text(
        json.dumps({"id": "abc", "direction": "inbound",
                    "body": "what did I ask you yesterday?",
                    "cwd": str(tmp_path), "session_id": "s1",
                    "sent_at": "2026-08-09T10:00:00Z"}) + "\n",
        encoding="utf-8")
    conn = clog.connect(tmp_path / "db.sqlite")
    report = clog.ingest_spool(conn, spool)
    assert report.added == 1, report
    rows = clog.query(conn)
    assert rows[0]["body"] == "what did I ask you yesterday?"
    assert rows[0]["source"] == "hook"


def test_a_spool_record_the_ingester_cannot_read_is_skipped_not_fatal(tmp_path):
    """One truncated line -- a session killed mid-append -- must not cost the
    whole day's capture. Positive control for the loop above."""
    spool = tmp_path / "conversation-spool"
    spool.mkdir()
    (spool / "2026-08-09.jsonl").write_text(
        '{"id": "trunc", "direction": "inbou\n'
        + json.dumps({"id": "ok", "direction": "inbound", "body": "hello",
                      "cwd": str(tmp_path), "session_id": "s1",
                      "sent_at": "2026-08-09T10:00:00Z"}) + "\n",
        encoding="utf-8")
    conn = clog.connect(tmp_path / "db.sqlite")
    report = clog.ingest_spool(conn, spool)
    assert report.added == 1
    assert sum(report.skipped.values()) == 1, report.skipped


# --- Findings from adversarial review, each pinned by the reproduction ------

def test_a_punctuation_search_filters_rather_than_returning_everything(conn):
    """The failure that does not look like one.

    ``(`` tokenises to nothing under FTS5, which left an empty MATCH
    expression, which made the search predicate disappear -- so every row came
    back and a result set meaning "no filter was applied" was indistinguishable
    from one meaning "everything matched". The earlier fuzz test asserted only
    that no exception escaped, which this bug satisfies perfectly.
    """
    clog.record(conn, source="hook", source_id="p1", body="a call (here)",
                direction="inbound", sent_at="2026-08-09T10:00:00Z",
                channel="human-agent", actor="human")
    clog.record(conn, source="hook", source_id="p2", body="no punctuation",
                direction="inbound", sent_at="2026-08-09T10:01:00Z",
                channel="human-agent", actor="human")
    hits = [r["source_id"] for r in clog.query(conn, search="(")]
    assert hits == ["p1"], hits


def test_the_punctuation_guard_can_fail(conn):
    """Positive control: a search matching neither row returns neither."""
    clog.record(conn, source="hook", source_id="p1", body="a call (here)",
                direction="inbound", sent_at="2026-08-09T10:00:00Z",
                channel="human-agent", actor="human")
    assert clog.query(conn, search="{") == []


def test_a_like_wildcard_in_a_search_is_not_treated_as_syntax(conn,
                                                              monkeypatch):
    """``%`` and ``_`` are LIKE's metacharacters, so an unescaped search for
    ``100%`` matches every body containing ``100`` followed by anything --
    more rows than were asked for, silently.

    ``search_mode`` is forced to the substring path, because that is the only
    path ``_like_term`` is on. The first version of this test did not, and on
    a build with FTS5 -- which is every build here -- ``100%`` tokenised to
    ``100`` and took the MATCH branch instead. It asserted the right rows,
    for the right reason, through code that was not the code under test, and
    would have passed with the escaping deleted.

    The *second* version forced the mode but proved nothing more, because FTS
    returns the same two answers for this fixture: an implementation that
    called ``search_mode`` and ignored it passed both. ``_fts_query`` is
    therefore replaced with something that raises, so taking the FTS branch
    is an error rather than a coincidence.
    """
    def unreachable(_text):
        raise AssertionError("took the FTS branch; _like_term was never run")

    monkeypatch.setattr(clog, "search_mode", lambda _conn: "substring")
    monkeypatch.setattr(clog, "_fts_query", unreachable)
    clog.record(conn, source="hook", source_id="w1", body="it hit 100% today",
                direction="inbound", sent_at="2026-08-09T10:00:00Z",
                channel="human-agent", actor="human")
    clog.record(conn, source="hook", source_id="w2", body="it hit 1000 today",
                direction="inbound", sent_at="2026-08-09T10:01:00Z",
                channel="human-agent", actor="human")
    hits = [r["source_id"] for r in clog.query(conn, search="100%")]
    assert hits == ["w1"], hits
    under = [r["source_id"] for r in clog.query(conn, search="t_day")]
    assert under == [], under


def test_the_wildcard_test_is_actually_on_the_substring_path(conn,
                                                             monkeypatch):
    """Control for the control.

    If `query` ever stops consulting `search_mode`, the monkeypatch above
    becomes decorative and the test silently returns to proving nothing.
    """
    seen = []
    monkeypatch.setattr(clog, "search_mode",
                        lambda _conn: seen.append(1) or "substring")
    clog.query(conn, search="100%")
    assert seen, "query() no longer consults search_mode"


def test_an_id_less_message_that_moves_and_is_renamed_is_filed_once(conn,
                                                                    tmp_path):
    """A filename is not an identity.

    Mail moves inbox -> archive as its normal life, and the move may rename.
    Keying an id-less message by ``path.stem`` files the same message twice.
    Scored by moving *and* renaming between two seeds, which is what the
    original test's plain copy never exercised.
    """
    root = tmp_path / "messages"
    inbox = root / "copilot-tools"
    inbox.mkdir(parents=True)
    body = {k: v for k, v in MAIL.items() if k != "id"}
    (inbox / "0001-original.json").write_text(json.dumps(body),
                                              encoding="utf-8")
    assert clog.seed_operator_mail(conn, root).added == 1

    archive = inbox / "archive"
    archive.mkdir()
    (inbox / "0001-original.json").rename(archive / "9999-renamed.json")
    assert clog.seed_operator_mail(conn, root).added == 0

    stored = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert stored == 1, "the same message was filed under two keys"


def test_an_id_less_message_is_keyed_by_content_not_by_path(conn, tmp_path):
    """Positive control for the guard above: two *different* id-less messages
    must still be two rows, or the fix has collapsed them instead."""
    root = tmp_path / "messages"
    inbox = root / "copilot-tools"
    inbox.mkdir(parents=True)
    first = {k: v for k, v in MAIL.items() if k != "id"}
    second = dict(first, text="a completely different message")
    (inbox / "a.json").write_text(json.dumps(first), encoding="utf-8")
    (inbox / "b.json").write_text(json.dumps(second), encoding="utf-8")
    assert clog.seed_operator_mail(conn, root).added == 2


def test_a_nul_in_a_search_matches_nothing_rather_than_everything(conn):
    """A NUL in a bound LIKE parameter truncates the pattern at the NUL, so
    ``%\x00%`` degenerates to ``%`` and every row comes back -- the same
    match-everything failure the punctuation fix removed, reachable through
    the fix itself."""
    clog.record(conn, source="hook", source_id="n1", body="ordinary text",
                direction="inbound", sent_at="2026-08-09T10:00:00Z",
                channel="human-agent", actor="human")
    clog.record(conn, source="hook", source_id="n2", body="more text",
                direction="inbound", sent_at="2026-08-09T10:01:00Z",
                channel="human-agent", actor="human")
    assert clog.query(conn, search="\x00") == []
    assert clog.query(conn, search="text\x00") == []


def test_the_nul_guard_does_not_swallow_ordinary_searches(conn):
    """Positive control: a guard that refused every search would satisfy the
    test above while making search useless, and nothing else would say so."""
    clog.record(conn, source="hook", source_id="n1", body="ordinary text",
                direction="inbound", sent_at="2026-08-09T10:00:00Z",
                channel="human-agent", actor="human")
    assert [r["source_id"] for r in clog.query(conn, search="ordinary")] == ["n1"]


def test_two_id_less_messages_do_not_collide_through_the_separator(conn,
                                                                    tmp_path):
    """A separator a field can contain is not a separator.

    Joining on ``\\x00`` made from="a" to="b\\0c" and from="a\\0b" to="c"
    hash identically, so two different messages became one row and one was
    lost. Losing a message is worse than duplicating one.
    """
    root = tmp_path / "messages"
    inbox = root / "inst"
    inbox.mkdir(parents=True)
    base = {"sent_at": "2026-08-09T10:00:00Z", "text": "hi"}
    (inbox / "a.json").write_text(
        json.dumps(dict(base, **{"from": "a", "to": "b\x00c"})),
        encoding="utf-8")
    (inbox / "b.json").write_text(
        json.dumps(dict(base, **{"from": "a\x00b", "to": "c"})),
        encoding="utf-8")
    report = clog.seed_operator_mail(conn, root)
    assert report.added == 2, report
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_the_same_id_less_message_still_keys_identically_twice(conn, tmp_path):
    """Positive control: the length prefix must not make the key depend on
    anything that changes between runs, or every seed duplicates the corpus."""
    root = tmp_path / "messages"
    inbox = root / "inst"
    inbox.mkdir(parents=True)
    body = {k: v for k, v in MAIL.items() if k != "id"}
    (inbox / "a.json").write_text(json.dumps(body), encoding="utf-8")
    assert clog.seed_operator_mail(conn, root).added == 1
    assert clog.seed_operator_mail(conn, root).added == 0


# --------------------------------------------------------------------------
# Round three: what the second round of repairs left behind
# --------------------------------------------------------------------------

def test_a_search_for_an_underscore_finds_the_rows_containing_one(conn):
    """Python's ``\\w`` includes ``_``; FTS5's tokeniser treats it as a
    separator. So ``_fts_query("_")`` returned the non-empty expression
    ``'"_"'``, the caller read that as "FTS can answer this", and FTS then
    tokenised it to an empty phrase and matched nothing. No error, no
    fallback, and two rows on the machine that contain ``_``.
    """
    _add(conn, source_id="u1", body="has_under here", channel="human-agent",
         actor="human")
    _add(conn, source_id="u2", body="a_b there", channel="human-agent",
         actor="human")
    _add(conn, source_id="u3", body="nothing at all", channel="human-agent",
         actor="human")
    hits = sorted(r["source_id"] for r in clog.query(conn, search="_"))
    assert hits == ["u1", "u2"], hits


def test_the_underscore_split_agrees_with_what_fts_will_keep(conn):
    """Control for the line above: the decision to fall back is made by
    ``_fts_query`` returning empty, so that is what has to be asserted. A
    search that survives tokenisation must still take the FTS path."""
    assert clog._fts_query("_") == ""
    assert clog._fts_query("___") == ""
    assert clog._fts_query("a_b") == '"a" AND "b"'


def test_an_index_created_after_the_rows_is_populated(tmp_path):
    """A store written by a Python whose sqlite3 lacks FTS5, then opened by
    one that has it. The triggers only fire on rows inserted after they
    exist, so without a rebuild the index is empty and every search returns
    nothing -- most convincingly on the machine with the most history.
    """
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(clog.SCHEMA)
    raw.execute(
        "INSERT INTO messages (source, source_id, channel, direction, actor,"
        " body, sent_at, captured_at) VALUES"
        " ('hook','legacy','human-agent','inbound','human','legacy needle',"
        "  '2026-08-09T00:00:00Z','2026-08-09T00:00:00Z')")
    raw.commit()
    raw.close()

    conn = clog.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        found = [r["source_id"] for r in clog.query(conn, search="needle")]
        assert found == ["legacy"], found
    finally:
        conn.close()


def test_search_mode_reports_what_it_can_run_not_what_is_catalogued(tmp_path):
    """Presence in ``sqlite_master`` is not usability. Probing by running a
    MATCH cannot be wrong about the thing it just did."""
    path = tmp_path / "probe.db"
    conn = clog.connect(path)
    try:
        assert clog.search_mode(conn) == "fts"
        conn.execute("DROP TABLE messages_fts")
        assert clog.search_mode(conn) == "substring"
    finally:
        conn.close()


def test_a_turn_the_hook_already_captured_is_not_filed_again(tmp_path):
    """Both sources see the identical turn -- the CLI writes it to its own
    session store and the extension appends it to the spool -- and their ids
    are unrelated, so ``UNIQUE (source, source_id)`` cannot notice. Every
    turn captured live appeared twice once the extension was installed,
    which is the one promise this store exists to keep.
    """
    store = tmp_path / "session-store.db"
    src = sqlite3.connect(str(store))
    src.executescript(
        "CREATE TABLE sessions (id TEXT, repository TEXT, cwd TEXT,"
        " branch TEXT);"
        "CREATE TABLE turns (session_id TEXT, turn_index INT,"
        " user_message TEXT, assistant_response TEXT, timestamp TEXT);")
    src.execute("INSERT INTO sessions VALUES ('s2','r','C:/x','main')")
    src.execute("INSERT INTO turns VALUES ('s2',0,'what is the plan',"
                "'the plan is this','2026-08-09T00:00:00Z')")
    src.commit()
    src.close()

    spool = tmp_path / "conversation-spool"
    spool.mkdir()
    (spool / "2026-08-09.jsonl").write_text("\n".join([
        json.dumps({"id": "h1", "direction": "inbound",
                    "body": "what is the plan", "session_id": "s2",
                    "sent_at": "2026-08-09T00:00:01Z", "cwd": "C:/x"}),
        json.dumps({"id": "h2", "direction": "outbound",
                    "body": "the plan is this", "session_id": "s2",
                    "sent_at": "2026-08-09T00:00:02Z", "cwd": "C:/x"}),
    ]) + "\n", encoding="utf-8")

    conn = clog.connect(tmp_path / "db.sqlite")
    try:
        clog.ingest_spool(conn, spool)
        report = clog.seed_session_store(conn, store)
        bodies = [r["body"] for r in conn.execute(
            "SELECT body FROM messages ORDER BY id")]
        assert bodies == ["what is the plan", "the plan is this"], bodies
        assert report.skipped.get("already captured live") == 2, report
    finally:
        conn.close()


def test_the_duplicate_guard_still_files_what_the_hook_missed(tmp_path):
    """Positive control, and the reason the match is per message rather than
    per session: an extension installed mid-session has captured some turns
    and not others, and dropping the whole session to avoid a duplicate
    would lose the earlier ones. A duplicate is visible noise; a gap is not.
    """
    store = tmp_path / "session-store.db"
    src = sqlite3.connect(str(store))
    src.executescript(
        "CREATE TABLE sessions (id TEXT, repository TEXT, cwd TEXT,"
        " branch TEXT);"
        "CREATE TABLE turns (session_id TEXT, turn_index INT,"
        " user_message TEXT, assistant_response TEXT, timestamp TEXT);")
    src.execute("INSERT INTO sessions VALUES ('s3','r','C:/x','main')")
    src.execute("INSERT INTO turns VALUES ('s3',0,'earlier question','',"
                "'2026-08-09T00:00:00Z')")
    src.execute("INSERT INTO turns VALUES ('s3',1,'later question','',"
                "'2026-08-09T00:10:00Z')")
    src.commit()
    src.close()

    spool = tmp_path / "conversation-spool"
    spool.mkdir()
    (spool / "2026-08-09.jsonl").write_text(json.dumps(
        {"id": "h9", "direction": "inbound", "body": "later question",
         "session_id": "s3", "sent_at": "2026-08-09T00:10:01Z",
         "cwd": "C:/x"}) + "\n", encoding="utf-8")

    conn = clog.connect(tmp_path / "db.sqlite")
    try:
        clog.ingest_spool(conn, spool)
        clog.seed_session_store(conn, store)
        bodies = sorted(r["body"] for r in conn.execute(
            "SELECT body FROM messages"))
        assert bodies == ["earlier question", "later question"], bodies
    finally:
        conn.close()


def test_seed_all_reads_the_spool_before_the_session_store(monkeypatch):
    """Order is the fix, not decoration. `seed_session_store` skips what the
    hook already holds, so the hook has to have been read first or the test
    has nothing to answer with -- and the very first seed on a machine with
    the extension installed files every captured turn twice.

    Observed by calling it. The first version of this test read the source
    text of `seed_all` and compared the positions of two names in it, which
    found them in the docstring and failed against correct code -- a test
    scoring the prose rather than the behaviour.
    """
    order = []

    def spy(name):
        def call(_conn, *_a, **_k):
            order.append(name)
            return clog.SeedReport(name)
        return call

    monkeypatch.setattr(clog, "ingest_spool", spy("hook"))
    monkeypatch.setattr(clog, "seed_operator_mail", spy("mail"))
    monkeypatch.setattr(clog, "seed_session_store", spy("session"))
    clog.seed_all(None)
    assert order.index("hook") < order.index("session"), order


def test_a_reply_to_a_peer_is_not_filed_as_human_conversation(tmp_path):
    """The peer's message is declined and held by mail, but the agent's
    answer to it was still being recorded as `human-agent` -- putting it in
    the one view the human asked to keep separate."""
    store = tmp_path / "session-store.db"
    src = sqlite3.connect(str(store))
    src.executescript(
        "CREATE TABLE sessions (id TEXT, repository TEXT, cwd TEXT,"
        " branch TEXT);"
        "CREATE TABLE turns (session_id TEXT, turn_index INT,"
        " user_message TEXT, assistant_response TEXT, timestamp TEXT);")
    src.execute("INSERT INTO sessions VALUES ('s4','r','C:/x','main')")
    src.execute("INSERT INTO turns VALUES ('s4',0,?,?,?)",
                (REAL_PEER, "agent reply to peer", "2026-08-09T00:00:00Z"))
    src.commit()
    src.close()

    conn = clog.connect(tmp_path / "db.sqlite")
    try:
        clog.seed_session_store(conn, store)
        rows = [dict(r) for r in conn.execute(
            "SELECT channel, direction, recipient, body FROM messages")]
        assert len(rows) == 1, rows
        assert rows[0]["channel"] == clog.AGENT_AGENT, rows[0]
        assert rows[0]["recipient"] == "scripts", rows[0]
    finally:
        conn.close()


def test_a_human_reply_is_still_filed_as_human_conversation(tmp_path):
    """Positive control for the line above: inheriting the prompt's channel
    must not reclassify ordinary turns."""
    store = tmp_path / "session-store.db"
    src = sqlite3.connect(str(store))
    src.executescript(
        "CREATE TABLE sessions (id TEXT, repository TEXT, cwd TEXT,"
        " branch TEXT);"
        "CREATE TABLE turns (session_id TEXT, turn_index INT,"
        " user_message TEXT, assistant_response TEXT, timestamp TEXT);")
    src.execute("INSERT INTO sessions VALUES ('s5','r','C:/x','main')")
    src.execute("INSERT INTO turns VALUES ('s5',0,?,?,?)",
                (REAL_HUMAN, "here is the answer", "2026-08-09T00:00:00Z"))
    src.commit()
    src.close()

    conn = clog.connect(tmp_path / "db.sqlite")
    try:
        clog.seed_session_store(conn, store)
        channels = [r["channel"] for r in conn.execute(
            "SELECT channel FROM messages")]
        assert channels == [clog.HUMAN_AGENT, clog.HUMAN_AGENT], channels
    finally:
        conn.close()


def test_a_spooled_reply_inherits_the_channel_of_what_it_answered(tmp_path):
    """Same defect on the capture path. An outbound event carries no sender,
    and defaulting every one to `human-agent` filed replies to peers in the
    human's conversation."""
    spool = tmp_path / "conversation-spool"
    spool.mkdir()
    (spool / "2026-08-09.jsonl").write_text("\n".join([
        json.dumps({"id": "p1", "direction": "inbound", "body": REAL_PEER,
                    "session_id": "s6", "sent_at": "2026-08-09T00:00:00Z"}),
        json.dumps({"id": "p2", "direction": "outbound",
                    "body": "answering the peer", "session_id": "s6",
                    "sent_at": "2026-08-09T00:00:01Z"}),
    ]) + "\n", encoding="utf-8")

    conn = clog.connect(tmp_path / "db.sqlite")
    try:
        clog.ingest_spool(conn, spool)
        rows = [dict(r) for r in conn.execute(
            "SELECT channel, recipient, body FROM messages")]
        assert len(rows) == 1, rows
        assert rows[0]["channel"] == clog.AGENT_AGENT, rows[0]
        assert rows[0]["recipient"] == "scripts", rows[0]
    finally:
        conn.close()


def test_a_spooled_reply_to_a_human_stays_human(tmp_path):
    """Positive control for the line above."""
    spool = tmp_path / "conversation-spool"
    spool.mkdir()
    (spool / "2026-08-09.jsonl").write_text("\n".join([
        json.dumps({"id": "q1", "direction": "inbound", "body": REAL_HUMAN,
                    "session_id": "s7", "sent_at": "2026-08-09T00:00:00Z"}),
        json.dumps({"id": "q2", "direction": "outbound", "body": "an answer",
                    "session_id": "s7", "sent_at": "2026-08-09T00:00:01Z"}),
    ]) + "\n", encoding="utf-8")

    conn = clog.connect(tmp_path / "db.sqlite")
    try:
        clog.ingest_spool(conn, spool)
        channels = [r["channel"] for r in conn.execute(
            "SELECT channel FROM messages ORDER BY id")]
        assert channels == [clog.HUMAN_AGENT, clog.HUMAN_AGENT], channels
    finally:
        conn.close()


def test_two_id_less_messages_to_different_agents_are_both_kept(tmp_path):
    """`to_id` decides the recipient instance, and leaving it out of the
    content key collapsed two messages that differed only in who they were
    addressed to. The same text sent to two agents is the ordinary shape of
    a broadcast, and one of the two was being dropped."""
    inbox = tmp_path / "messages" / "inbox"
    inbox.mkdir(parents=True)
    for name, to_id in (("m1", "id-A"), ("m2", "id-B")):
        (inbox / f"{name}.json").write_text(json.dumps(
            {"from": "x", "to": "y", "to_id": to_id, "text": "same text",
             "sent_at": "2026-08-09T00:00:00Z"}), encoding="utf-8")

    conn = clog.connect(tmp_path / "db.sqlite")
    try:
        report = clog.seed_operator_mail(conn, tmp_path / "messages")
        assert report.added == 2, report
        assert sorted(r["instance"] for r in conn.execute(
            "SELECT instance FROM messages")) == ["id-A", "id-B"]
    finally:
        conn.close()


def test_the_same_message_seen_twice_is_still_filed_once(tmp_path):
    """Positive control: widening the key must not retire the deduplication
    it exists for. The same message in the inbox and in the archive -- the
    normal life of every message here -- is one row."""
    root = tmp_path / "messages"
    (root / "inbox").mkdir(parents=True)
    (root / "archive").mkdir(parents=True)
    payload = json.dumps({"from": "x", "to": "y", "to_id": "id-A",
                          "text": "one message",
                          "sent_at": "2026-08-09T00:00:00Z"})
    (root / "inbox" / "live.json").write_text(payload, encoding="utf-8")
    (root / "archive" / "renamed-on-move.json").write_text(
        payload, encoding="utf-8")

    conn = clog.connect(tmp_path / "db.sqlite")
    try:
        report = clog.seed_operator_mail(conn, root)
        assert report.added == 1, report
        assert report.duplicate == 1, report
    finally:
        conn.close()


def test_the_asks_filter_is_applied_before_the_limit(conn):
    """The viewer filtered the page it got back, which applies the filter
    after LIMIT: one old question behind 200 newer statements returned
    nothing, and "no questions" is indistinguishable from "none in the last
    200 rows"."""
    _add(conn, source_id="old-q", body="what did I ask you?",
         channel="human-agent", actor="human", sent_at="2026-08-01T00:00:00Z")
    for i in range(250):
        _add(conn, source_id=f"n{i}", body="a plain statement",
             channel="human-agent", actor="human",
             sent_at=f"2026-08-09T{i // 60:02d}:{i % 60:02d}:00Z")
    hits = [r["source_id"] for r in clog.query(conn, asks=True, limit=200)]
    assert hits == ["old-q"], hits


def test_the_asks_filter_still_returns_nothing_when_there_are_none(conn):
    """Positive control."""
    _add(conn, source_id="s1", body="a plain statement",
         channel="human-agent", actor="human")
    assert clog.query(conn, asks=True) == []


# --------------------------------------------------------------------------
# The cross-language seam, executed rather than read
# --------------------------------------------------------------------------

needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed")

_EVENTS_MJS = (Path(__file__).resolve().parents[1]
               / "extensions" / "conversation-capture" / "events.mjs")

# Runs the extension's real builders and prints what they actually produce.
# The point is that no key name, no field and no default is spelled twice:
# whatever JavaScript emits is what Python is handed.
_DRIVER = """
import { inboundEvent, outboundEvent } from %s;
const lines = [
  inboundEvent(
    { prompt: "what did I ask you yesterday?", workingDirectory: "C:/x",
      sessionId: "s1" },
    { id: "fixed-in", now: "2026-08-09T10:00:00Z" }),
  outboundEvent(
    { data: { messageId: "fixed-out", content: "you asked about the plan" },
      sessionId: "s1" },
    { id: "unused", now: "2026-08-09T10:00:01Z", cwd: "C:/x" }),
];
for (const line of lines) process.stdout.write(JSON.stringify(line) + "\\n");
"""


@needs_node
def test_the_ingester_reads_what_the_extension_actually_emits(tmp_path):
    """The seam, executed.

    The previous version of this test scanned `extension.mjs` for key
    spellings and then built its own event in Python, so it proved the
    strings appeared in the source -- not that the code produced them.
    Replacing both body expressions with `body: ""` left every assertion
    passing while every captured message would have been discarded as empty.
    See the control below, which is that mutation.
    """
    driver = tmp_path / "driver.mjs"
    driver.write_text(_DRIVER % json.dumps(_EVENTS_MJS.as_uri()),
                      encoding="utf-8")
    proc = subprocess.run(["node", str(driver)], capture_output=True,
                          text=True, encoding="utf-8",
                          errors="replace")
    assert proc.returncode == 0, proc.stderr

    spool = tmp_path / "conversation-spool"
    spool.mkdir()
    (spool / "2026-08-09.jsonl").write_text(proc.stdout, encoding="utf-8")

    conn = clog.connect(tmp_path / "db.sqlite")
    try:
        report = clog.ingest_spool(conn, spool)
        assert report.added == 2, report
        rows = sorted((r["direction"], r["body"], r["session_id"], r["cwd"])
                      for r in clog.query(conn))
        assert rows == [
            ("inbound", "what did I ask you yesterday?", "s1", "C:/x"),
            ("outbound", "you asked about the plan", "s1", "C:/x"),
        ], rows
    finally:
        conn.close()


@needs_node
def test_the_seam_test_notices_an_extension_that_stops_emitting_bodies(
        tmp_path):
    """Positive control, and the exact mutation that defeated the old test.

    A builder that returns an empty body is a capture extension that records
    nothing, and the suite has to be able to say so. If this ever passes, the
    test above has stopped scoring the JavaScript.
    """
    mutated = tmp_path / "events.mjs"
    source = _EVENTS_MJS.read_text(encoding="utf-8")
    mutated.write_text(
        source.replace('body: String(input?.prompt ?? "")', 'body: ""')
              .replace('body: String(data.content ?? "")', 'body: ""'),
        encoding="utf-8")
    assert 'body: ""' in mutated.read_text(encoding="utf-8")

    driver = tmp_path / "driver.mjs"
    driver.write_text(_DRIVER % json.dumps(mutated.as_uri()),
                      encoding="utf-8")
    proc = subprocess.run(["node", str(driver)], capture_output=True,
                          text=True, encoding="utf-8",
                          errors="replace")
    assert proc.returncode == 0, proc.stderr

    spool = tmp_path / "conversation-spool"
    spool.mkdir()
    (spool / "2026-08-09.jsonl").write_text(proc.stdout, encoding="utf-8")

    conn = clog.connect(tmp_path / "db.sqlite")
    try:
        report = clog.ingest_spool(conn, spool)
        assert report.added == 0, "an empty-bodied extension looked healthy"
        assert report.skipped.get("empty body") == 2, report
    finally:
        conn.close()


@needs_node
def test_the_extension_and_the_builders_are_one_program(tmp_path):
    """`events.mjs` is only worth executing if the extension really uses it.

    Otherwise the tests above score a module nothing calls -- green, and
    entirely disconnected from what runs in a session.
    """
    source = (_EVENTS_MJS.parent / "extension.mjs").read_text(encoding="utf-8")
    assert 'from "./events.mjs"' in source
    for name in ("inboundEvent(", "outboundEvent("):
        assert name in source, f"the extension no longer calls {name}"


# --------------------------------------------------------------------------
# The CLI's own insertions are not human speech
# --------------------------------------------------------------------------

REAL_REMINDER = (
    "\n<system_reminder>\nCustom instructions from "
    ".worktrees/chore-land-peer-fixes/.github/copilot-instructions.md. "
    "Apply these to any code you write here:\n\n# Repository Agent "
    "Instructions\n</system_reminder>\n")


def test_a_turn_that_is_only_a_system_reminder_is_not_human_speech():
    """Measured on the real store: 462 of 1918 rows filed as human speech --
    24% -- were `<system_reminder>` blocks and nothing else. Not one of the
    462 contained a word the human typed. Asked "what did I say", the store
    answered with a quarter of its own instruction files."""
    actor, _channel, sender = clog.classify(REAL_REMINDER)
    assert actor == clog.SYSTEM, (actor, sender)
    assert sender == "copilot-cli"


def test_a_reminder_appended_to_real_speech_is_still_human():
    """The mirror failure, and the reason the test is "what remains" rather
    than "does it contain one". Dropping a turn because a reminder was
    appended to it would lose the sentence the human actually typed."""
    body = "please fix the failing test" + REAL_REMINDER
    actor, _channel, _sender = clog.classify(body)
    assert actor == clog.HUMAN, body[:60]


def test_a_leading_newline_does_not_hide_a_reminder():
    """None of the 462 rows *started* with the tag -- the CLI writes a
    newline first -- so a prefix check finds zero of them and reports the
    whole corpus clean, which reads exactly like success."""
    assert not REAL_REMINDER.startswith("<system_reminder>")
    assert clog.is_only_machine_text(REAL_REMINDER)


def test_ordinary_speech_is_untouched():
    """Positive control: the detector must be able to *not* fire."""
    assert not clog.is_only_machine_text(REAL_HUMAN)
    assert clog.classify(REAL_HUMAN)[0] == clog.HUMAN


def test_reseeding_reclassifies_a_row_filed_under_an_older_rule(conn,
                                                                monkeypatch):
    """Seeding is idempotent, which without this is idempotent in the
    unhelpful direction: a fixed rule leaves every previously-misfiled row
    misfiled, and the only remedy is knowing to delete the database.

    Simulated by filing a row while the detector is switched off, then
    seeding again with it on -- which is exactly the shape of shipping a
    classification fix to a machine that already has a store.
    """
    monkeypatch.setattr(clog, "is_only_machine_text", lambda _b: False)
    assert _add(conn, source_id="r1", body=REAL_REMINDER) is True
    assert conn.execute(
        "SELECT actor FROM messages WHERE source_id='r1'").fetchone()[0] \
        == clog.HUMAN

    monkeypatch.undo()
    assert _add(conn, source_id="r1", body=REAL_REMINDER) is False
    row = conn.execute(
        "SELECT actor, sender FROM messages WHERE source_id='r1'").fetchone()
    assert row["actor"] == clog.SYSTEM, dict(row)
    assert row["sender"] == "copilot-cli"


def test_reseeding_does_not_rewrite_what_was_said(conn):
    """A message's text is what was said; only the verdict about it is ours
    to revise. Re-seeding must never edit a body."""
    _add(conn, source_id="r2", body="the original words")
    _add(conn, source_id="r2", body="tampered")
    assert conn.execute(
        "SELECT body FROM messages WHERE source_id='r2'").fetchone()[0] \
        == "the original words"


REAL_SKILL_CONTEXT = (
    '<skill-context name="backlog">\nBase directory for this skill: '
    'C:\\Users\\darin\\.copilot\\skills\\backlog\n\n# Backlog\n\nOpen work '
    'belongs in the repository.\n</skill-context>')


def test_a_turn_that_is_only_a_skill_context_is_not_human_speech():
    """The second wrapper, and the reason `_MACHINE_TAGS` is a list.

    Found the same way as the first -- by reading the finished store rather
    than a fixture -- 124 rows, every one of them pure, none containing a
    word the human typed. There will be a third.
    """
    actor, _channel, sender = clog.classify(REAL_SKILL_CONTEXT)
    assert actor == clog.SYSTEM, actor
    assert sender == "copilot-cli"


def test_a_skill_context_appended_to_real_speech_is_still_human():
    body = "load the backlog skill and file this\n" + REAL_SKILL_CONTEXT
    assert clog.classify(body)[0] == clog.HUMAN


@pytest.mark.parametrize("body", [
    "use <feature-branch> as the placeholder name",
    "the diff is at <merge-sha> and <fix-sha>",
    "write it to <path> when you are done",
    "List everything <the> book has set up so far",
    "<div>this is markup I am asking about</div>",
])
def test_angle_brackets_a_person_typed_are_left_alone(body):
    """The failure this detector must not have.

    Every one of these tags occurs in a real human message in the store.
    Widening the rule to "anything angle-bracketed" would delete exactly what
    the store exists to keep, and it would do it silently -- the rows would
    simply stop being the user's.
    """
    assert not clog.is_only_machine_text(body), body
    assert clog.classify(body)[0] == clog.HUMAN


def test_every_machine_tag_is_actually_detected():
    """Control for the table itself. A tag listed in `_MACHINE_TAGS` whose
    pattern does not match is a rule that reports the corpus clean."""
    for tag in clog._MACHINE_TAGS:
        assert clog.is_only_machine_text(f"<{tag}>anything</{tag}>"), tag
        assert not clog.is_only_machine_text(
            f"human words <{tag}>anything</{tag}>"), tag
