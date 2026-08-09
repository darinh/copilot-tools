"""Guards for the conversation viewer's HTTP surface.

Exercised over a real socket rather than by calling the handler directly. The
things most likely to break here -- a filter arriving as a string when the
query layer expects an enum, a 500 leaking a stack trace, an unbounded result
set -- all live in the translation between a URL and a function call, and a
direct call skips exactly that.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from pathlib import Path

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


# --------------------------------------------------------------------------
# Markdown rendering: the XSS boundary, executed
# --------------------------------------------------------------------------
#
# This page turns stored text into HTML, and the stored text is every word
# ever typed to an agent on this machine -- including whatever a peer agent
# sent and whatever a web page an agent was reading said. That makes `md()`
# the only place in the toolkit where hostile input meets an HTML sink.
#
# So it is run, not read. Grepping `conversation_viewer.py` for reassuring
# substrings is the exact failure this feature has already shipped twice: a
# test that scores the source instead of the behaviour.

needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed")


def _render(payloads, source=None):
    """Run the page's real `md()` over each payload under node."""
    js = (source if source is not None else viewer.MARKDOWN_JS)
    with tempfile.TemporaryDirectory() as tmp:
        mod = Path(tmp) / "md.mjs"
        mod.write_text(js + "\nexport { md, esc };\n", encoding="utf-8")
        driver = Path(tmp) / "run.mjs"
        driver.write_text(
            "import { md } from %s;\n"
            "const inputs = JSON.parse(process.argv[2]);\n"
            "process.stdout.write(JSON.stringify(inputs.map(md)));\n"
            % json.dumps(mod.as_uri()), encoding="utf-8")
        proc = subprocess.run(
            ["node", str(driver), json.dumps(list(payloads))],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace")
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)


#: Every one of these is a real way to get script into a page that renders
#: user text. They are the point of the test file.
XSS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<svg/onload=alert(1)>",
    "<iframe src=javascript:alert(1)></iframe>",
    "[click](javascript:alert(1))",
    "[click](JaVaScRiPt:alert(1))",
    "[click](java\tscript:alert(1))",
    "[click](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
    "[click](vbscript:msgbox(1))",
    "[click](java&#115;cript:alert(1))",
    '[x](https://ok" onmouseover="alert(1))',
    "[x](https://ok') onmouseover=alert(1) foo='",
    "`<script>alert(1)</script>`",
    "```\n<script>alert(1)</script>\n```",
    "> <script>alert(1)</script>",
    "- <img src=x onerror=alert(1)>",
    "# <script>alert(1)</script>",
    "**<script>alert(1)</script>**",
    "<a href=\"javascript:alert(1)\">x</a>",
    "<<script>script>alert(1)<</script>/script>",
    "<body onload=alert(1)>",
    "<style>@import'evil'</style>",
    "<meta http-equiv=refresh content=0;url=javascript:alert(1)>",
]


#: Tags that can execute or fetch. `md` is only ever allowed to emit
#: formatting, so the safe set is an allow-list and everything else is a
#: finding -- naming the dangerous ones instead would pass the first tag
#: nobody thought of.
ALLOWED_TAGS = frozenset({
    "p", "br", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "hr",
    "code", "pre", "strong", "em", "a", "mark"})

SAFE_SCHEMES = ("http://", "https://")


class _Audit(HTMLParser):
    """What a browser would actually build from the rendered string.

    A substring scan is the wrong oracle here and the first draft of this
    file used one. `&lt;img src=x onerror=alert(1)&gt;` is *correct* output --
    the payload rendered as visible text -- but it contains the characters
    ` onerror=`, so a regex looking for an event handler reports a hole that
    is not there. Worse, the same crudeness fails in the other direction: it
    cannot tell an `href` from the word "javascript:" in a sentence, so a
    real finding and a quoted one look identical.

    Parsing answers the question actually being asked: which elements exist,
    and what are their attributes.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.attrs = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend((tag, k, v or "") for k, v in attrs)

    handle_startendtag = handle_starttag


def _audit(html):
    parser = _Audit()
    parser.feed(html)
    parser.close()
    return parser


@needs_node
@pytest.mark.parametrize("payload", XSS)
def test_markdown_never_emits_an_executable_construct(payload):
    """The whole security case, one payload at a time.

    `esc` runs over the entire body before any transform, so the only angle
    brackets in the output are ones this code wrote. Everything here must
    come back as visible text.
    """
    html = _render([payload])[0]
    found = _audit(html)
    assert not set(found.tags) - ALLOWED_TAGS, (found.tags, html)
    for tag, name, value in found.attrs:
        assert not name.lower().startswith("on"), (tag, name, html)
        if name.lower() in ("href", "src", "action", "formaction", "data"):
            assert value.lower().startswith(SAFE_SCHEMES), (name, value, html)


@needs_node
def test_the_xss_assertions_can_fail():
    """Positive control, and the one this file cannot do without.

    Twenty-three payloads all passing proves nothing unless the assertions
    are known to reject something. This runs the same corpus through a
    renderer with the escaping removed and requires every check above to
    fire at least once.
    """
    broken = viewer.MARKDOWN_JS.replace(
        'const esc = s => String(s).replace(/[&<>"\']/g, c => ({',
        "const esc = s => String(s); const _unused = (c => ({")
    assert broken != viewer.MARKDOWN_JS, "the mutation did not apply"

    tags, handlers, schemes = set(), [], []
    for html in _render(XSS, source=broken):
        found = _audit(html)
        tags |= set(found.tags)
        for tag, name, value in found.attrs:
            if name.lower().startswith("on"):
                handlers.append((tag, name))
            if name.lower() in ("href", "src") and not value.lower().startswith(
                    SAFE_SCHEMES):
                schemes.append(value)
    assert "script" in tags, tags
    assert "img" in tags, tags
    assert "iframe" in tags, tags
    assert handlers, "no event-handler attribute was produced"
    assert any("javascript:" in v.lower() for v in schemes), schemes


@needs_node
def test_a_safe_link_is_still_a_link():
    """Positive control for the URL allow-list: refusing everything would
    pass every assertion above and make the feature pointless."""
    html = _render(["see [the docs](https://example.com/a?b=1&c=2)"])[0]
    assert '<a href="https://example.com/a?b=1&amp;c=2"' in html, html
    assert ">the docs</a>" in html, html
    assert 'rel="noreferrer noopener"' in html, html


@needs_node
def test_a_bare_url_is_linked_once_and_only_once():
    """The two link forms run in one pass precisely so the bare-URL rule
    cannot find the URL inside the href the markdown rule just wrote."""
    html = _render(["ref https://example.com/x and [y](https://example.com/y)"])[0]
    assert html.count("<a href=") == 2, html
    assert "href=\"<a" not in html, html


@needs_node
@pytest.mark.parametrize("body,expected", [
    ("plain words", "<p>plain words</p>"),
    ("# Title", "<h3>Title</h3>"),
    ("## Sub", "<h4>Sub</h4>"),
    ("- one\n- two", "<ul><li>one</li><li>two</li></ul>"),
    ("1. one\n2. two", "<ol><li>one</li><li>two</li></ol>"),
    ("> quoted", "<blockquote>quoted</blockquote>"),
    ("---", "<hr>"),
    ("**bold**", "<p><strong>bold</strong></p>"),
    ("*italic*", "<p><em>italic</em></p>"),
    ("`code`", "<p><code>code</code></p>"),
    ("a\nb", "<p>a<br>b</p>"),
])
def test_markdown_renders_what_it_should(body, expected):
    """The feature itself. Without these the security tests above are
    satisfied by a renderer that outputs nothing at all."""
    assert _render([body])[0] == expected


@needs_node
def test_a_fenced_block_keeps_its_contents_verbatim():
    html = _render(["```python\nif a < b:\n    x = '**not bold**'\n```"])[0]
    assert html.startswith("<pre><code>"), html
    assert "if a &lt; b:" in html, html
    assert "<strong>" not in html, "markdown was applied inside a code fence"


@needs_node
def test_the_renderer_survives_the_shapes_the_corpus_actually_contains():
    """Nothing here may throw: a body that crashes the renderer empties the
    whole list, and the corpus is 4,000 messages of unconstrained text."""
    odd = ["", "```", "```\nunclosed", "[", "](", "*", "#", ">", "- ",
           "\u0000F0\u0000", "a" * 20000, "\n\n\n", "![img](x)",
           "|a|b|\n|-|-|\n|1|2|"]
    rendered = _render(odd)
    assert len(rendered) == len(odd)
    for html in rendered:
        assert "<script" not in html.lower()


@needs_node
def test_a_sentinel_in_the_body_cannot_forge_a_code_block():
    """The NUL sentinel is safe only because no stored body can contain one.
    That is a property of the store, so it is asserted against the store."""
    assert "\x00" not in viewer.PAGE
    # Stripped rather than assumed absent. SQLite stores a NUL in TEXT
    # without complaint, so "no body can contain one" was a guess -- and a
    # body that spelled the sentinel would have been handed another
    # message's code block, or `undefined`.
    rendered = _render(["\u0000F0\u0000 and ```real```"])[0]
    assert "\u0000" not in rendered, rendered
    assert "undefined" not in rendered, rendered
    assert "<pre><code>real</code></pre>" in rendered, rendered


def test_the_page_refuses_to_ship_with_its_javascript_missing():
    """A silent miss renders every body as literal markdown and stops the
    highlighter -- which reads as a styling regression until somebody puts a
    <script> in a message. Failing to substitute must be louder than that."""
    with pytest.raises(viewer.ConversationViewerError):
        viewer._assemble(template="<html>no placeholders</html>")


def test_the_assembled_page_carries_both_blocks():
    """Positive control for the guard above."""
    assert "function md(" in viewer.PAGE
    assert "function markMatches(" in viewer.PAGE
    assert "/*MARKDOWN_JS*/" not in viewer.PAGE


# --------------------------------------------------------------------------
# Chat layout
# --------------------------------------------------------------------------

def _rows(messages):
    """Run the page's real `rowClass` and `messageHtml` over each message.

    The first version of this asserted that a ternary appeared in the page
    source. That is the shape this feature has already shipped twice and
    replaced both times: it passes for a renderer that was never called, and
    it would keep passing if `renderRows` stopped using the expression it
    pins. Running the functions asks the question directly.
    """
    with tempfile.TemporaryDirectory() as tmp:
        mod = Path(tmp) / "row.mjs"
        mod.write_text(viewer.MARKDOWN_JS + viewer.ROW_JS
                       + "\nexport { rowClass, messageHtml };\n",
                       encoding="utf-8")
        driver = Path(tmp) / "run.mjs"
        driver.write_text(
            "import { rowClass, messageHtml } from %s;\n"
            "const xs = JSON.parse(process.argv[2]);\n"
            "process.stdout.write(JSON.stringify(xs.map(\n"
            "  m => ({cls: rowClass(m), html: messageHtml(m)}))));\n"
            % json.dumps(mod.as_uri()), encoding="utf-8")
        proc = subprocess.run(
            ["node", str(driver), json.dumps(list(messages))],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace")
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)


def _message(**over):
    base = {"direction": "inbound", "actor": "human", "body": "hello",
            "sent_at": "2026-08-09T10:00:00Z", "source": "hook",
            "channel": "human-agent", "sender": "", "recipient": "",
            "project": "", "branch": "", "asks": 0}
    base.update(over)
    return base


@needs_node
def test_inbound_is_placed_on_the_right_and_outbound_on_the_left():
    """What was said *to* the agent sits where a chat client puts the person
    typing; the agent's answer sits opposite."""
    out = _rows([_message(direction="inbound"),
                 _message(direction="outbound", actor="agent")])
    assert out[0]["cls"] == "row in", out
    assert out[1]["cls"] == "row out", out


def test_the_stylesheet_positions_the_two_sides():
    """The class names above only mean something if the CSS acts on them."""
    assert ".row.in{justify-content:flex-end}" in viewer.PAGE
    assert ".row.out{justify-content:flex-start}" in viewer.PAGE


@needs_node
def test_a_body_reaches_the_markdown_renderer_not_the_escaper():
    """The wiring, which no markdown test can see.

    Every `md()` test would still pass if `messageHtml` called `esc()`
    instead -- the renderer would be correct and unused, and every body would
    show its markdown as literal characters.
    """
    html = _rows([_message(body="# Heading\n\n- a\n- b")])[0]["html"]
    assert "<h3>Heading</h3>" in html, html
    assert "<ul><li>a</li><li>b</li></ul>" in html, html
    assert "# Heading" not in html, html


@needs_node
def test_the_meta_line_still_escapes_its_own_fields():
    """`md()` renders the body; the metadata around it is not markdown and
    must stay escaped. A project name or a peer's name is attacker-influenced
    too -- both arrive from another program."""
    html = _rows([_message(project="<script>alert(1)</script>",
                           sender="<img src=x onerror=alert(1)>",
                           actor="agent", recipient="a<b")])[0]["html"]
    found = _audit(html)
    # `div` and `span` are the meta line's own wrappers, which `md()` never
    # emits and so are not in its allow-list.
    allowed = ALLOWED_TAGS | {"div", "span"}
    assert not set(found.tags) - allowed, (found.tags, html)
    for _tag, name, _value in found.attrs:
        assert not name.lower().startswith("on"), html
    # And the payloads are present as text, not dropped -- a renderer that
    # silently discarded the fields would satisfy every line above.
    assert "&lt;script&gt;" in html, html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html, html


@needs_node
def test_the_speaker_label_names_who_actually_spoke():
    labels = [r["html"] for r in _rows([
        _message(actor="human"),
        _message(actor="system"),
        _message(actor="agent", sender="scripts"),
        _message(actor="agent", sender=""),
    ])]
    assert ">you<" in labels[0]
    assert ">operator preamble<" in labels[1]
    assert ">scripts<" in labels[2]
    assert ">agent<" in labels[3]
