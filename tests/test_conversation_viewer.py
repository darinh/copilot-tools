"""Guards for the conversation viewer's HTTP surface.

Exercised over a real socket rather than by calling the handler directly. The
things most likely to break here -- a filter arriving as a string when the
query layer expects an enum, a 500 leaking a stack trace, an unbounded result
set -- all live in the translation between a URL and a function call, and a
direct call skips exactly that.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import conversation_log as clog
import conversation_viewer as viewer


@pytest.fixture()
def server(tmp_path):
    db = tmp_path / "conversations.db"
    conn = clog.connect(db)
    clog.record(conn, source=clog.SOURCE_HOOK, source_id="h1",
                body="does the seeder deduplicate rows?",
                direction=clog.INBOUND, sent_at="2026-08-09T10:00:00Z",
                cwd=r"C:\repos\prism")
    clog.record(conn, source=clog.SOURCE_HOOK, source_id="h2",
                body="Yes, on (source, source_id).",
                direction=clog.OUTBOUND, sent_at="2026-08-09T10:00:05Z",
                cwd=r"C:\repos\prism")
    clog.record(conn, source=clog.SOURCE_OPERATOR_MAIL, source_id="m1",
                body="Ack, nothing outstanding.", direction=clog.INBOUND,
                sent_at="2026-08-08T10:00:00Z", channel=clog.AGENT_AGENT,
                actor=clog.AGENT, sender="scripts", recipient="copilot-tools")
    conn.commit()
    conn.close()

    handler = type("Handler", (viewer._Handler,), {"db_file": db})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as response:
        return response.status, response.read().decode("utf-8")


def get_json(base, path):
    return json.loads(get(base, path)[1])


def test_the_page_is_served(server):
    status, body = get(server, "/")
    assert status == 200
    assert "<!doctype html>" in body


def test_an_unknown_route_is_a_404(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        get(server, "/etc/passwd")
    assert caught.value.code == 404


def test_messages_come_back_newest_first(server):
    rows = get_json(server, "/api/messages")
    assert [r["source_id"] for r in rows] == ["h2", "h1", "m1"]


def test_the_agent_mail_view_is_separable(server):
    """The user asked to read agent-to-agent traffic on its own.

    The control is the unfiltered request above, which returns all three.
    """
    rows = get_json(server, "/api/messages?channel=agent-agent")
    assert [r["source_id"] for r in rows] == ["m1"]
    human = get_json(server, "/api/messages?channel=human-agent")
    assert [r["source_id"] for r in human] == ["h2", "h1"]


def test_search_matches_message_text(server):
    rows = get_json(server, "/api/messages?search=deduplicate")
    assert [r["source_id"] for r in rows] == ["h1"]


@pytest.mark.parametrize("term", ["--force", "a%22b", "x%3Ay", "(", "*",
                                  "NOT", "C%3A%5Cpath.py", "OR+OR"])
def test_search_syntax_characters_do_not_produce_a_server_error(server, term):
    """FTS5 reads ``-``, ``*``, ``:`` and ``(`` as query syntax.

    Searching for a flag or a Windows path is the normal case for this store,
    and an unescaped term raises inside MATCH. A 500 on ``--force`` would be
    absurd; these must all answer.
    """
    status, _ = get(server, f"/api/messages?search={term}")
    assert status == 200


def test_an_unknown_filter_value_is_refused_with_a_400(server):
    """Not a 500, and not silently unfiltered.

    Ignoring an unrecognised filter answers a narrow question with wide data,
    and the extra rows look exactly like results.
    """
    with pytest.raises(urllib.error.HTTPError) as caught:
        get(server, "/api/messages?actor=nonsense")
    assert caught.value.code == 400
    assert "unknown actor" in caught.value.read().decode("utf-8")


def test_a_filter_value_cannot_reach_the_sql(server):
    with pytest.raises(urllib.error.HTTPError):
        get(server, "/api/messages?channel=x%27%3B+DROP+TABLE+messages%3B+--")
    assert len(get_json(server, "/api/messages")) == 3


def test_the_questions_filter_narrows(server):
    rows = get_json(server, "/api/messages?asks=1")
    assert [r["source_id"] for r in rows] == ["h1"]


def test_projects_and_days_drive_the_sidebar(server):
    assert [p["project"] for p in get_json(server, "/api/projects")] == ["prism"]
    days = {d["day"]: d["messages"] for d in get_json(server, "/api/days")}
    assert days == {"2026-08-09": 2, "2026-08-08": 1}


def test_days_narrow_to_a_project(server):
    days = get_json(server, "/api/days?project=prism")
    assert [d["day"] for d in days] == ["2026-08-09"]


def test_the_summary_names_the_search_mode(server):
    summary = get_json(server, "/api/summary")
    assert summary["messages"] == 3
    assert summary["search_mode"] in ("fts", "substring")


def test_the_viewer_binds_to_loopback_by_default():
    """The store holds every prompt typed on this machine, across projects.

    Read off the signature rather than asserted as a constant elsewhere: the
    default is the whole protection, and a test that restated it in a second
    place would keep passing after the first one changed.
    """
    import inspect
    default = inspect.signature(viewer.serve).parameters["host"].default
    assert default == "127.0.0.1"


def test_a_concurrent_request_does_not_reuse_one_connection(server):
    """ThreadingHTTPServer will absolutely serve two requests at once.

    sqlite3 connections are not shareable across threads by default, so a
    single shared connection raises ProgrammingError under load -- which shows
    up as an intermittent 500 rather than as a test failure.
    """
    results: list[int] = []
    errors: list[BaseException] = []

    def hit():
        try:
            results.append(get(server, "/api/messages")[0])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not errors
    assert results == [200] * 8


# --- Findings from adversarial review --------------------------------------

def _with_host(base, path, host):
    """A request that reaches loopback while *claiming* another hostname.

    That is exactly the shape of a DNS-rebinding read: the socket is local,
    the Host header is not.
    """
    request = urllib.request.Request(base + path)
    request.add_header("Host", host)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_a_foreign_host_header_is_refused(server):
    """Binding 127.0.0.1 stops a remote *socket*. It does nothing about a page
    the user is merely visiting resolving its own hostname to loopback and
    then reading this API, same-origin, from the user's own browser -- every
    word they have typed to an agent, from a tab they did not know was
    hostile."""
    status, body = _with_host(server, "/api/messages", "attacker.example")
    assert status == 403, body
    assert "source" not in body, "the refusal leaked rows"


def test_the_host_guard_lets_the_real_client_through(server):
    """Positive control. A guard that refuses everything would pass the test
    above while making the viewer useless, and nothing else would say so."""
    host = server.split("//", 1)[1]
    assert _with_host(server, "/api/messages", host)[0] == 200
    assert _with_host(server, "/api/messages", "localhost:1")[0] == 200
    assert _with_host(server, "/", "127.0.0.1")[0] == 200


def test_an_ipv6_loopback_host_survives_the_port_being_stripped(server):
    """`[::1]:8765` is full of colons, so stripping the port by splitting on
    one would mangle it into something the allow-list rejects -- locking a
    legitimate user out rather than letting an attacker in."""
    assert _with_host(server, "/api/summary", "[::1]:8765")[0] == 200
    assert _with_host(server, "/api/summary", "[::1]")[0] == 200


def test_a_punctuation_search_filters_rather_than_returning_everything(server):
    """The viewer's own fuzz test asserted only HTTP 200, which the bug --
    every row returned for a search of `(` -- satisfies perfectly."""
    rows = get_json(server, "/api/messages?search=%28")
    bodies = [r["body"] for r in rows]
    assert bodies == ["Yes, on (source, source_id)."], bodies


def test_the_viewer_punctuation_guard_can_fail(server):
    """Positive control: punctuation present in no body returns no rows."""
    assert get_json(server, "/api/messages?search=%7B") == []


def test_host_junk_after_an_ipv6_literal_is_refused(server):
    """The first Host guard cut at `]` and kept what came before, so
    `[::1]evil.com` read as `[::1]` and went straight through. Anything after
    the address is junk, and junk here is the whole attack."""
    for host in ("[::1]evil.com", "[::1]:80x", "localhost.evil.com",
                 "127.0.0.1.evil.com", "localhost evil.com",
                 "user@evil.com", "127.0.0.1:80:80"):
        status, body = _with_host(server, "/api/messages", host)
        assert status == 403, f"{host} was accepted: {body[:80]}"


def test_the_ipv6_junk_guard_still_admits_the_real_thing(server):
    """Positive control: tightening the parse must not lock out the forms a
    browser genuinely sends."""
    for host in ("[::1]", "[::1]:8765", "127.0.0.1", "127.0.0.1:8765",
                 "localhost", "localhost:8765", "localhost."):
        assert _with_host(server, "/api/summary", host)[0] == 200, host


def test_a_deliberately_bound_host_is_accepted(tmp_path):
    """`serve(host=...)` exists, so a guard that refuses the address the user
    asked to bind is a broken flag, not a security control -- it protects
    nothing the bind did not already expose."""
    db = tmp_path / "c.db"
    conn = clog.connect(db)
    clog.record(conn, source=clog.SOURCE_HOOK, source_id="x", body="hi",
                direction=clog.INBOUND, sent_at="2026-08-09T10:00:00Z")
    conn.commit()
    conn.close()
    handler = type("Handler", (viewer._Handler,),
                   {"db_file": db, "bound_host": "0.0.0.0"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        assert _with_host(base, "/api/summary", "0.0.0.0:1234")[0] == 200
        assert _with_host(base, "/api/summary", "attacker.example")[0] == 403
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------------------
# Round three
# --------------------------------------------------------------------------

def test_an_old_question_is_found_behind_a_page_of_newer_statements(tmp_path):
    """The `asks` filter used to be applied to the page the query returned,
    which is after LIMIT. One old question behind 200 newer statements
    returned nothing at all -- and "you were never asked anything" is
    indistinguishable from "not in the most recent 200 rows"."""
    db = tmp_path / "conversations.db"
    conn = clog.connect(db)
    clog.record(conn, source=clog.SOURCE_HOOK, source_id="old-q",
                body="what did I ask you?", direction=clog.INBOUND,
                sent_at="2026-08-01T00:00:00Z")
    for i in range(250):
        clog.record(conn, source=clog.SOURCE_HOOK, source_id=f"n{i}",
                    body="a plain statement", direction=clog.INBOUND,
                    sent_at=f"2026-08-09T{i // 60:02d}:{i % 60:02d}:00Z")
    conn.commit()
    conn.close()

    handler = type("Handler", (viewer._Handler,), {"db_file": db})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        rows = get_json(base, "/api/messages?asks=1")
        assert [r["source_id"] for r in rows] == ["old-q"], rows
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_the_asks_filter_can_still_return_nothing(server):
    """Positive control: pushing the filter into SQL must not make it
    vacuous."""
    rows = get_json(server, "/api/messages?asks=1&session_id=nothing-here")
    assert rows == []


def test_an_explicitly_allowed_host_is_accepted(tmp_path):
    """`--host` bound the server somewhere the Host guard then refused,
    which made the flag a broken feature rather than a security control. The
    remedy is an explicit list, not a wider guard: which names are
    legitimate is a fact about the user's network."""
    db = tmp_path / "conversations.db"
    clog.connect(db).close()
    handler = type("Handler", (viewer._Handler,),
                   {"db_file": db, "bound_host": "0.0.0.0",
                    "allowed_hosts": frozenset({"desktop.local"})})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        assert _with_host(base, "/api/summary", "desktop.local")[0] == 200
        assert _with_host(base, "/api/summary", "DESKTOP.LOCAL:8765")[0] == 200
        # The control: everything not named is still refused.
        assert _with_host(base, "/api/summary", "attacker.example")[0] == 403
        assert _with_host(base, "/api/summary", "desktop.local.evil")[0] == 403
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_a_wildcard_bind_without_allowed_hosts_says_so(tmp_path, capsys,
                                                       monkeypatch):
    """A wildcard bind cannot name itself: browsers send the machine's own
    address, never `0.0.0.0`, so every request is refused. Printing that is
    the alternative to quietly trusting any Host once the bind is wide --
    which would retire the guard exactly where exposure is greatest."""
    db = tmp_path / "conversations.db"
    clog.connect(db).close()

    class _Stub:
        server_port = 8765

        def __init__(self, *_a, **_k):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(viewer, "ThreadingHTTPServer", _Stub)
    viewer.serve(db, host="0.0.0.0", port=8765, open_browser=False)
    out = capsys.readouterr().out
    assert "--allow-host" in out, out
    assert "refused" in out.lower(), out


def test_a_wildcard_bind_with_allowed_hosts_does_not_warn(tmp_path, capsys,
                                                          monkeypatch):
    """Positive control: the notice must be about the broken case, not
    printed at every non-loopback bind regardless."""
    db = tmp_path / "conversations.db"
    clog.connect(db).close()

    class _Stub:
        server_port = 8765

        def __init__(self, *_a, **_k):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(viewer, "ThreadingHTTPServer", _Stub)
    viewer.serve(db, host="0.0.0.0", port=8765, open_browser=False,
                 allow_hosts=["desktop.local"])
    out = capsys.readouterr().out
    assert "--allow-host" not in out, out
