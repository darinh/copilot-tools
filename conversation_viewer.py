"""A local web viewer for the conversation store.

Standard library only -- ``http.server`` and ``sqlite3``. That is not
minimalism for its own sake: this repository fails the build on any
third-party import that is not declared in ``pyproject.toml``, and a log viewer
is not worth adding a runtime dependency to a tool whose whole job is to run
unattended on other people's machines.

Bound to loopback. The store holds every prompt typed on this machine across
every project, which is the most sensitive artifact the toolkit produces, and
an ambient listener on ``0.0.0.0`` would publish it to the local network by
default. ``--host`` can override that; nothing else can.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import conversation_log as clog

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Conversations</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#dde3ee;--dim:#8b94a7;
--human:#63b3ff;--agent:#7ee0a8;--system:#c0a0ff;--mail:#ffc46b;--warn:#ff9d9d}
*{box-sizing:border-box}
body{margin:0;font:14px/1.55 ui-sans-serif,system-ui,"Segoe UI",sans-serif;
background:var(--bg);color:var(--fg);display:flex;height:100vh;overflow:hidden}
aside{width:270px;flex:none;background:var(--panel);border-right:1px solid var(--line);
overflow-y:auto;padding:14px}
main{flex:1;display:flex;flex-direction:column;min-width:0}
h1{font-size:15px;margin:0 0 4px}
h2{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim);
margin:18px 0 6px}
.sub{color:var(--dim);font-size:12px;margin-bottom:8px}
.tabs{display:flex;gap:6px;margin-bottom:12px}
.tabs button{flex:1;padding:7px 4px;font-size:12px;border-radius:6px;cursor:pointer;
background:#20242e;color:var(--fg);border:1px solid var(--line)}
.tabs button.on{background:#2f6ee0;border-color:#2f6ee0;color:#fff}
.item{padding:5px 8px;border-radius:5px;cursor:pointer;display:flex;
justify-content:space-between;gap:8px;color:var(--fg)}
.item:hover{background:#20242e}
.item.on{background:#2f6ee0;color:#fff}
.item .n{color:var(--dim);font-size:11px;flex:none}
.item.on .n{color:#dbe6ff}
.item span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
header{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;
gap:8px;flex-wrap:wrap;align-items:center}
input,select{background:#20242e;color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:7px 9px;font:inherit}
input[type=search]{flex:1;min-width:180px}
#list{overflow-y:auto;padding:12px 16px;flex:1}
.msg{border:1px solid var(--line);border-left-width:3px;border-radius:7px;
padding:9px 12px;margin-bottom:9px;background:var(--panel)}
.msg.human{border-left-color:var(--human)}
.msg.agent{border-left-color:var(--agent)}
.msg.system{border-left-color:var(--system)}
.msg.mail{border-left-color:var(--mail)}
.meta{font-size:11px;color:var(--dim);display:flex;gap:9px;flex-wrap:wrap;
margin-bottom:5px;align-items:center}
.who{font-weight:600;color:var(--fg)}
.tag{background:#20242e;border:1px solid var(--line);border-radius:4px;
padding:0 5px;font-size:10px;text-transform:uppercase;letter-spacing:.05em}
.q{color:#151821;background:var(--mail);border-radius:4px;padding:0 5px;
font-size:10px;font-weight:700}
.body{white-space:pre-wrap;word-break:break-word;font-size:13px;
max-height:19em;overflow:hidden;position:relative}
.body.open{max-height:none}
.more{color:var(--human);cursor:pointer;font-size:12px;
display:inline-block;margin-top:4px}
.empty,.note{color:var(--dim);padding:14px 0}
.note{color:var(--warn);font-size:12px}
mark{background:#5a4a00;color:#ffe9a8;border-radius:2px}
</style></head><body>
<aside>
  <h1>Conversations</h1>
  <div class="sub" id="stats">loading…</div>
  <div class="tabs">
    <button id="t-all" class="on">All</button>
    <button id="t-ha">Human</button>
    <button id="t-aa">Agent mail</button>
  </div>
  <h2>Projects</h2><div id="projects"></div>
  <h2>Days</h2><div id="days"></div>
</aside>
<main>
  <header>
    <input type="search" id="q" placeholder="Search message text…">
    <select id="actor">
      <option value="">anyone</option>
      <option value="human">human</option>
      <option value="agent">agent</option>
      <option value="system">operator preamble</option>
    </select>
    <select id="direction">
      <option value="">both ways</option>
      <option value="inbound">to the agent</option>
      <option value="outbound">from the agent</option>
    </select>
    <label style="font-size:12px;color:var(--dim)">
      <input type="checkbox" id="asks" style="vertical-align:-2px"> questions only
    </label>
    <button id="clear" style="background:#20242e;color:var(--fg);
      border:1px solid var(--line);border-radius:6px;padding:7px 11px;
      cursor:pointer">Reset</button>
  </header>
  <div id="list"><div class="empty">loading…</div></div>
</main>
<script>
const S = {channel:"", project:"", day:"", q:"", actor:"", direction:"",
           asks:false};
const $ = s => document.querySelector(s);
const esc = s => s.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));

function highlight(text, term){
  const safe = esc(text);
  if(!term.trim()) return safe;
  const parts = term.split(/[^\\w]+/).filter(Boolean).map(
    t => t.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&"));
  if(!parts.length) return safe;
  return safe.replace(new RegExp("(" + parts.join("|") + ")", "gi"),
                      "<mark>$1</mark>");
}

async function get(path, params){
  const u = new URL(path, location.origin);
  for(const [k,v] of Object.entries(params||{})) if(v) u.searchParams.set(k,v);
  const r = await fetch(u);
  if(!r.ok) throw new Error(await r.text());
  return r.json();
}

function pickList(el, rows, key, label, current, onPick){
  el.innerHTML = "";
  const all = document.createElement("div");
  all.className = "item" + (current ? "" : " on");
  all.innerHTML = "<span>everything</span>";
  all.onclick = () => onPick("");
  el.appendChild(all);
  for(const row of rows){
    const d = document.createElement("div");
    d.className = "item" + (row[key] === current ? " on" : "");
    d.innerHTML = "<span>" + esc(label(row)) + "</span><span class='n'>"
                + row.messages + "</span>";
    d.onclick = () => onPick(row[key]);
    el.appendChild(d);
  }
}

async function refreshSidebar(){
  const s = await get("/api/summary");
  $("#stats").textContent =
    s.messages + " messages · " + s.projects + " projects · "
    + (s.first_day || "?") + " → " + (s.last_day || "?")
    + (s.search_mode === "fts" ? "" : " · substring search");
  pickList($("#projects"), await get("/api/projects"), "project",
           r => r.project, S.project, v => { S.project = v; S.day = "";
                                             refreshSidebar(); load(); });
  pickList($("#days"), await get("/api/days", {project:S.project}), "day",
           r => r.day, S.day, v => { S.day = v; refreshSidebar(); load(); });
}

async function load(){
  const list = $("#list");
  let rows;
  try{
    rows = await get("/api/messages", {
      channel:S.channel, project:S.project, date_from:S.day, date_to:S.day,
      search:S.q, actor:S.actor, direction:S.direction,
      asks:S.asks ? "1" : ""});
  }catch(err){
    list.innerHTML = "<div class='note'>" + esc(String(err)) + "</div>";
    return;
  }
  if(!rows.length){ list.innerHTML = "<div class='empty'>Nothing matches.</div>";
                    return; }
  list.innerHTML = "";
  for(const m of rows){
    const kind = m.channel === "agent-agent" ? "mail" : m.actor;
    const who = m.actor === "human" ? "you"
              : m.actor === "system" ? "operator preamble"
              : (m.sender || (m.direction === "outbound" ? "agent" : "agent"));
    const div = document.createElement("div");
    div.className = "msg " + kind;
    const bits = ["<span class='who'>" + esc(who) + "</span>"];
    if(m.recipient) bits.push("→ " + esc(m.recipient));
    bits.push(esc(m.sent_at.replace("T"," ").replace("Z","")));
    if(m.project) bits.push("<span class='tag'>" + esc(m.project) + "</span>");
    if(m.branch) bits.push(esc(m.branch));
    if(m.asks) bits.push("<span class='q'>?</span>");
    bits.push("<span class='tag'>" + esc(m.source) + "</span>");
    const long = m.body.length > 1400;
    div.innerHTML = "<div class='meta'>" + bits.join("") .replace(/><s/g,"> <s")
      + "</div><div class='body'>" + highlight(m.body, S.q) + "</div>"
      + (long ? "<span class='more'>show all</span>" : "");
    if(long){
      const b = div.querySelector(".body"), t = div.querySelector(".more");
      t.onclick = () => { b.classList.toggle("open");
                          t.textContent = b.classList.contains("open")
                                        ? "show less" : "show all"; };
    }
    list.appendChild(div);
  }
}

function tab(id, channel){
  $(id).onclick = () => {
    S.channel = channel;
    for(const b of document.querySelectorAll(".tabs button"))
      b.classList.toggle("on", b.id === id.slice(1));
    load();
  };
}
tab("#t-all", ""); tab("#t-ha", "human-agent"); tab("#t-aa", "agent-agent");

let timer;
$("#q").oninput = e => { clearTimeout(timer); S.q = e.target.value;
                         timer = setTimeout(load, 220); };
$("#actor").onchange = e => { S.actor = e.target.value; load(); };
$("#direction").onchange = e => { S.direction = e.target.value; load(); };
$("#asks").onchange = e => { S.asks = e.target.checked; load(); };
$("#clear").onclick = () => {
  Object.assign(S, {project:"", day:"", q:"", actor:"", direction:"",
                    asks:false});
  $("#q").value = ""; $("#actor").value = ""; $("#direction").value = "";
  $("#asks").checked = false;
  refreshSidebar(); load();
};

refreshSidebar(); load();
</script></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "operator-conversations"
    db_file: Path = None  # type: ignore[assignment]

    # One connection per thread. sqlite3 objects are not shareable across
    # threads by default, and ThreadingHTTPServer will absolutely try.
    _local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = clog.connect(self.db_file)
            self._local.conn = conn
        return conn

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        pass  # A request log on stdout would bury the "serving on ..." line.

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The viewer is loopback-only and same-origin; say so rather than
        # leaving the defaults to imply otherwise.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: object, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    #: Hostnames a browser may legitimately have used to reach this server.
    #: A literal loopback address, or the one name that always means it.
    #: Anything else is a name that resolved here without being ours.
    LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", ""})

    def _host_is_local(self) -> bool:
        """Whether the Host header names loopback.

        The port is stripped, but an IPv6 literal is bracketed and full of
        colons, so splitting on the last colon only works after checking for
        the bracket -- ``[::1]:8765`` and ``[::1]`` must both survive.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            host = host.split("]", 1)[0] + "]"
        elif host.count(":") == 1:
            host = host.split(":", 1)[0]
        return host in self.LOCAL_HOSTS

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_is_local():
            # Binding to 127.0.0.1 stops a *remote* socket connecting. It does
            # nothing about DNS rebinding, where a page the user is merely
            # visiting resolves its own hostname to 127.0.0.1 and then reads
            # this API from the user's own browser -- a same-origin read of
            # every word they have ever typed to an agent, from a tab they did
            # not know was hostile. The Host header is the part of that attack
            # that cannot be forged away: it names the hostname the browser
            # believed it was talking to, and that is never ours.
            self._send(403, b"forbidden: unrecognised Host header",
                       "text/plain; charset=utf-8")
            return
        parsed = urlparse(self.path)
        route = parsed.path
        args = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if route in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"),
                           "text/html; charset=utf-8")
            elif route == "/api/summary":
                self._json(clog.summary(self._conn()))
            elif route == "/api/projects":
                self._json(clog.projects(self._conn()))
            elif route == "/api/days":
                self._json(clog.days(self._conn(), args.get("project", "")))
            elif route == "/api/messages":
                self._json(self._messages(args))
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")
        except clog.ConversationError as exc:
            self._json({"error": str(exc)}, 400)
        except (sqlite3.Error, ValueError) as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _messages(self, args: dict) -> list:
        rows = clog.query(
            self._conn(),
            search=args.get("search", ""), project=args.get("project", ""),
            channel=args.get("channel", ""), actor=args.get("actor", ""),
            direction=args.get("direction", ""),
            instance=args.get("instance", ""),
            session_id=args.get("session_id", ""),
            date_from=args.get("date_from", ""),
            date_to=args.get("date_to", ""),
            limit=int(args.get("limit", 200)))
        if args.get("asks"):
            rows = [r for r in rows if r["asks"]]
        return rows


def serve(db: "Path | None" = None, host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True) -> int:
    path = Path(db) if db is not None else clog.db_path()
    conn = clog.connect(path)
    total = clog.summary(conn)["messages"]
    conn.close()

    handler = type("Handler", (_Handler,), {"db_file": path})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{httpd.server_port}/"
    print(f"Conversations: {total} message(s) from {path}")
    print(f"Serving on {url}  (Ctrl-C to stop)")
    if total == 0:
        print("Nothing stored yet — run `operator conversations seed` first.")
    if open_browser:
        try:
            webbrowser.open(url)
        except OSError:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
    return 0
