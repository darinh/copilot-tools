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
import sqlite3
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
    """Scored against a line built from the extension's own key names rather
    than from mine: a spool record whose fields the ingester ignores is
    silently dropped, and an empty ingest looks exactly like a quiet day.
    """
    source = _extension_source()
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
