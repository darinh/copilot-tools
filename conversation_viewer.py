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

#: Markdown rendering, and the search highlighter.
#:
#: Its own constant, and a raw string, for two reasons. The suite executes it
#: under node against adversarial input -- a renderer that turns text into HTML
#: is the only XSS surface this page has, and the text it renders is every word
#: ever typed to an agent on this machine, including whatever a peer sent and
#: whatever a web page an agent read said. Scoring that boundary by grepping the
#: page source would be the same defect this feature has already shipped twice:
#: a test that reads code instead of running it.
#:
#: The raw string is so the regexes here mean what they say. The surrounding
#: page is an ordinary triple-quoted string where every backslash is doubled,
#: and a security-critical character class is the last place to spend attention
#: on that.
#:
#: The invariant everything below rests on: **escape first, then add markup.**
#: `esc` runs over the whole body before any transform, so `<` and `>` from the
#: message can never become tags -- the only angle brackets in the output are
#: ones this code wrote. Every transform after that operates on already-escaped
#: text and may only add markup, never decode any.
MARKDOWN_JS = r"""
const esc = s => String(s).replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

// Some of what is stored arrives already HTML-escaped: the CLI escapes the
// instruction files it injects, so 400 of 4,440 bodies here hold the literal
// characters `&lt;` where the file said `<`. Escaping those again renders
// `&lt;` on screen -- the transport showing through the message.
//
// Decoding before escaping is safe *because* of the order: `esc` runs over
// the whole decoded string immediately after, so nothing decoded here can
// survive as markup. `&amp;lt;` decodes once, to `&lt;`, and is then escaped
// back to `&amp;lt;` -- so text that really is about an entity still reads as
// one. One pass, so a decoded `&` cannot start a second round.
const ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
                  "&apos;": "'", "&#39;": "'", "&#x27;": "'", "&nbsp;": " "};

function decodeEntities(s){
  return String(s).replace(/&(?:amp|lt|gt|quot|apos|nbsp|#39|#x27);/gi,
                           m => ENTITIES[m.toLowerCase()] || m);
}

// The one place a URL from the message reaches an attribute. Anything not
// http(s) is left as literal text: `javascript:`, `data:` and `vbscript:` all
// execute from an href, and no message in a conversation log needs them.
// Entity tricks cannot get round this, because `esc` has already turned `&`
// into `&amp;`, so a `java&#115;cript:` in the source arrives here spelled
// `java&amp;#115;cript:` and fails the test on its literal characters.
const SAFE_URL = /^https?:\/\//i;

function anchor(url, text){
  return '<a href="' + url + '" target="_blank" rel="noreferrer noopener">'
       + text + "</a>";
}

function mdInline(s){
  s = s.replace(/`([^`\n]+)`/g, (m, code) => "<code>" + code + "</code>");
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^\w*])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^\w_])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>");
  // Markdown links and bare URLs in ONE pass. Two passes would find the URL
  // inside the href the first pass just wrote and wrap it again.
  s = s.replace(
    /\[([^\]\n]*)\]\(([^)\s]+)\)|(^|[\s(])(https?:\/\/[^\s<>()]+)/gi,
    (m, text, url, pre, bare) => {
      if(url !== undefined)
        return SAFE_URL.test(url) ? anchor(url, text || url) : m;
      return pre + anchor(bare, bare);
    });
  return s;
}

function md(text){
  // Fenced code comes out first and goes back last, so nothing inside a code
  // block is ever read as markdown. A NUL sentinel is used because no message
  // needs one -- and any NUL the body does contain is removed here, so a body
  // cannot spell a sentinel and claim another message's code block.
  const fences = [];
  let s = decodeEntities(String(text == null ? "" : text)
                         .replace(/\u0000/g, "")).replace(
    // The info string (```python) is optional and may not contain a backtick.
    // Without both of those, a single-line ```code``` had its content eaten:
    // `[^\n]*` is happy to swallow `code``` as the language name, and the
    // block came back empty.
    /```(?:[^\n`]*\n)?([\s\S]*?)```/g, (m, code) => {
      fences.push(code);
      return "\u0000F" + (fences.length - 1) + "\u0000";
    });
  s = esc(s);

  const out = [];
  let para = [], list = null;
  const flushPara = () => {
    if(para.length){
      out.push("<p>" + mdInline(para.join("\n")).replace(/\n/g, "<br>")
               + "</p>");
      para = [];
    }
  };
  const flushList = () => { if(list){ out.push("</" + list + ">"); list = null; } };

  for(const line of s.split("\n")){
    let m = line.match(/^\u0000F(\d+)\u0000$/);
    if(m){
      flushPara(); flushList();
      out.push("<pre><code>" + esc(fences[Number(m[1])]) + "</code></pre>");
      continue;
    }
    if(!line.trim()){ flushPara(); flushList(); continue; }
    if((m = line.match(/^(#{1,6})\s+(.*)$/))){
      flushPara(); flushList();
      // Shifted down two levels: a message is not the page, and an <h1> from
      // somebody's pasted README should not outrank the viewer's own heading.
      const n = Math.min(m[1].length + 2, 6);
      out.push("<h" + n + ">" + mdInline(m[2]) + "</h" + n + ">");
      continue;
    }
    if(/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)){
      flushPara(); flushList(); out.push("<hr>"); continue;
    }
    // `&gt;` rather than `>`: this runs on escaped text.
    if((m = line.match(/^\s*&gt;\s?(.*)$/))){
      flushPara(); flushList();
      out.push("<blockquote>" + mdInline(m[1]) + "</blockquote>");
      continue;
    }
    if((m = line.match(/^\s*[-*+]\s+(.*)$/))){
      flushPara();
      if(list !== "ul"){ flushList(); out.push("<ul>"); list = "ul"; }
      out.push("<li>" + mdInline(m[1]) + "</li>");
      continue;
    }
    if((m = line.match(/^\s*\d+[.)]\s+(.*)$/))){
      flushPara();
      if(list !== "ol"){ flushList(); out.push("<ol>"); list = "ol"; }
      out.push("<li>" + mdInline(m[1]) + "</li>");
      continue;
    }
    flushList();
    para.push(line);
  }
  flushPara(); flushList();

  // A fence that opened mid-line leaves its sentinel inside a paragraph.
  // Restored here so it renders as code rather than as a NUL byte.
  return out.join("").replace(/\u0000F(\d+)\u0000/g,
    (m, i) => "<pre><code>" + esc(fences[Number(i)]) + "</code></pre>");
}
"""

#: Search highlighting, applied to the DOM rather than to the HTML string.
#:
#: The old version ran a regex over escaped text, which was correct while the
#: body was escaped text and nothing else. Against rendered markup it would
#: insert `<mark>` in the middle of `<a href="...">` and corrupt the attribute
#: it landed in. Walking text nodes cannot reach a tag or an attribute at all,
#: and every inserted node is built with `createElement`/`textContent`, so the
#: highlighter has no path to producing HTML from the search term either.
HIGHLIGHT_JS = r"""
function markMatches(root, term){
  const parts = String(term || "").split(/[^\w]+/).filter(Boolean).map(
    t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if(!parts.length) return;
  const re = new RegExp("(" + parts.join("|") + ")", "gi");
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const targets = [];
  while(walker.nextNode()) targets.push(walker.currentNode);
  for(const node of targets){
    const text = node.nodeValue;
    re.lastIndex = 0;
    if(!re.test(text)) continue;
    re.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0, m;
    while((m = re.exec(text)) !== null){
      if(m[0].length === 0){ re.lastIndex++; continue; }
      if(m.index > last)
        frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      const mk = document.createElement("mark");
      mk.textContent = m[0];
      frag.appendChild(mk);
      last = m.index + m[0].length;
    }
    if(last < text.length)
      frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
  }
}
"""

#: How one stored message becomes one bubble.
#:
#: Split out of `renderRows` so the suite can run it. Left inline, the only
#: thing a test could do is assert that a ternary appears in the page source --
#: which is the shape of test this feature has already shipped twice and had
#: to replace both times. Nothing here touches the DOM, so node can call it
#: directly with a row and be told what the browser would build.
ROW_JS = r"""
// Three positions, not two. Inbound is what was said *to* the agent, on the
// right -- the side a chat client gives the person typing. Outbound is the
// agent answering, on the left. Machine text is neither: the launch preamble,
// the CLI's instruction files and its skill definitions were not said by
// anybody in the conversation, and putting them on the human's side implies
// they were typed. They go down the middle, where they read as scenery.
function rowClass(m){
  if(m.actor === "system") return "row mid";
  return "row " + (m.direction === "inbound" ? "in" : "out");
}

// `system` covers more than one speaker, and calling all of them "operator
// preamble" was wrong for 586 of the 1,964 rows on this machine -- the CLI's
// own reminders and skill definitions are not the operator's launch prompt.
// The store already knows the difference; it is the sender.
const SYSTEM_LABELS = {"operator": "operator preamble",
                       "copilot-cli": "copilot cli",
                       "pipeline": "automated prompt"};

function whoLabel(m){
  if(m.actor === "human") return "you";
  if(m.actor === "system")
    return SYSTEM_LABELS[m.sender] || (m.sender ? m.sender : "system");
  return m.sender || "agent";
}

function messageHtml(m){
  const bits = ["<span class='who'>" + esc(whoLabel(m)) + "</span>"];
  if(m.recipient) bits.push("&rarr; " + esc(m.recipient));
  bits.push(esc(String(m.sent_at || "").replace("T", " ").replace("Z", "")));
  if(m.project) bits.push("<span class='tag'>" + esc(m.project) + "</span>");
  if(m.branch) bits.push(esc(m.branch));
  if(m.asks) bits.push("<span class='q'>?</span>");
  bits.push("<span class='tag'>" + esc(m.source) + "</span>");
  return "<div class='meta'>" + bits.join("").replace(/><s/g, "> <s")
       + "</div><div class='body'>" + md(m.body) + "</div>";
}
"""

_PAGE_TEMPLATE = """<!doctype html>
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
#list{overflow-y:auto;padding:14px 16px;flex:1;display:flex;
flex-direction:column;gap:10px}
.row{display:flex;width:100%}
.row.out{justify-content:flex-start}
.row.in{justify-content:flex-end}
/* Machine text down the middle, narrower and dimmer: it is scenery, not a
   turn anybody took. */
.row.mid{justify-content:center}
.row.mid .msg{max-width:min(72ch,68%);border-left-width:1px;
border-right-width:1px;border-top:2px solid var(--system);
background:#15171f;color:var(--dim);border-radius:8px}
.row.mid .meta{justify-content:center}
.row.mid .body{font-size:12px}
.msg{border:1px solid var(--line);border-left-width:3px;border-radius:10px;
padding:9px 12px;background:var(--panel);max-width:min(80ch,78%);min-width:0}
/* Inbound sits on the right, so its coloured edge belongs on the right too --
   a left border on a right-aligned bubble points at nothing. */
.row.in .msg{border-left-width:1px;border-right-width:3px;
background:#1b2231;border-radius:10px 10px 2px 10px}
.row.out .msg{border-radius:10px 10px 10px 2px}
.msg.human{border-left-color:var(--human);border-right-color:var(--human)}
.msg.agent{border-left-color:var(--agent);border-right-color:var(--agent)}
.msg.system{border-left-color:var(--system);border-right-color:var(--system)}
.msg.mail{border-left-color:var(--mail);border-right-color:var(--mail)}
.meta{font-size:11px;color:var(--dim);display:flex;gap:9px;flex-wrap:wrap;
margin-bottom:5px;align-items:center}
.row.in .meta{justify-content:flex-end}
.who{font-weight:600;color:var(--fg)}
.tag{background:#20242e;border:1px solid var(--line);border-radius:4px;
padding:0 5px;font-size:10px;text-transform:uppercase;letter-spacing:.05em}
.q{color:#151821;background:var(--mail);border-radius:4px;padding:0 5px;
font-size:10px;font-weight:700}
.body{word-break:break-word;overflow-wrap:anywhere;font-size:13px;
max-height:19em;overflow:hidden;position:relative}
.body.open{max-height:none}
/* Rendered markdown. Margins are collapsed at the edges so a bubble whose
   body is a single paragraph is not padded twice. */
.body>*:first-child{margin-top:0}
.body>*:last-child{margin-bottom:0}
.body p{margin:0 0 .55em}
.body h3,.body h4,.body h5,.body h6{margin:.7em 0 .35em;font-size:13px;
line-height:1.3}
.body h3{font-size:15px}
.body h4{font-size:14px}
.body ul,.body ol{margin:.3em 0 .6em;padding-left:1.4em}
.body li{margin:.15em 0}
.body blockquote{margin:.4em 0;padding:.1em 0 .1em .7em;
border-left:2px solid var(--line);color:var(--dim)}
.body hr{border:0;border-top:1px solid var(--line);margin:.7em 0}
.body code{background:#0b0d12;border:1px solid var(--line);border-radius:4px;
padding:.05em .35em;font:12px/1.4 ui-monospace,"Cascadia Mono",Consolas,
monospace}
.body pre{background:#0b0d12;border:1px solid var(--line);border-radius:6px;
padding:8px 10px;margin:.5em 0;overflow-x:auto}
.body pre code{background:none;border:0;padding:0;white-space:pre;
display:block}
.body a{color:var(--human)}
.body strong{color:#fff}
.more{color:var(--human);cursor:pointer;font-size:12px;
display:inline-block;margin-top:4px}
.row.in .more{float:right}
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
/*MARKDOWN_JS*//*ROW_JS*//*HIGHLIGHT_JS*/

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
    const row = document.createElement("div");
    row.className = rowClass(m);
    const div = document.createElement("div");
    div.className = "msg " + kind;
    const long = m.body.length > 1400;
    div.innerHTML = messageHtml(m)
      + (long ? "<span class='more'>show all</span>" : "");
    const b = div.querySelector(".body");
    // After the markup exists, and against the DOM: a regex over rendered
    // HTML would happily put a <mark> inside an href.
    if(S.q.trim()) markMatches(b, S.q);
    if(long){
      const t = div.querySelector(".more");
      t.onclick = () => { b.classList.toggle("open");
                          t.textContent = b.classList.contains("open")
                                        ? "show less" : "show all"; };
    }
    row.appendChild(div);
    list.appendChild(row);
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


def _assemble(template: str = "", markdown: str = "",
              highlight: str = "", row: str = "") -> str:
    """Put the JavaScript into the page, and refuse to ship it if it missed.

    Every placeholder is checked rather than assumed. A silent miss is not a
    broken page -- it is a page whose bodies render as literal markdown and
    whose search stops highlighting, which looks like a styling regression and
    reads as one for as long as nobody tries a `<script>` in a message. The
    substitution failing must be louder than the feature degrading.
    """
    page = template or _PAGE_TEMPLATE
    for token, block in (("/*MARKDOWN_JS*/", markdown or MARKDOWN_JS),
                         ("/*ROW_JS*/", row or ROW_JS),
                         ("/*HIGHLIGHT_JS*/", highlight or HIGHLIGHT_JS)):
        if token not in page:
            raise ConversationViewerError(
                f"the page template no longer contains {token}")
        page = page.replace(token, block)
    return page


class ConversationViewerError(RuntimeError):
    """The page could not be assembled."""


PAGE = _assemble()


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
    #:
    #: ``localhost.`` is the same name with an explicit root label, which
    #: browsers do send; ``127.1`` is the short form some clients accept.
    LOCAL_HOSTS = frozenset({"127.0.0.1", "127.1", "localhost", "localhost.",
                             "::1", "[::1]", "[::ffff:127.0.0.1]", ""})

    #: The address this server was actually bound to, if it is not loopback.
    #: `serve(host=...)` lets a user deliberately bind elsewhere, and a guard
    #: that then refuses their own requests is a broken flag rather than a
    #: security control -- it protects nothing the bind did not already
    #: expose, and it costs the user the feature.
    bound_host = ""

    #: Extra authorities named by `--allow-host`. Needed because a wildcard
    #: bind cannot name itself: a server on `0.0.0.0` is reached by clients
    #: sending the machine's own IP or hostname, never the literal `0.0.0.0`,
    #: so `bound_host` alone refuses every real request to it. The choice is
    #: the user's and has to be written down, because the alternative -- trust
    #: any Host once the bind is wide -- silently retires the guard exactly
    #: where the exposure is greatest.
    allowed_hosts = frozenset()

    def _host_is_local(self) -> bool:
        """Whether the Host header names this server rather than some name
        that merely resolved to it.

        Strict, and structurally rather than by prefix: the first attempt
        stripped an IPv6 bracket by cutting at ``]`` and keeping what came
        before, which read ``[::1]evil.com`` as ``[::1]`` and let it straight
        through. Anything after the address is junk, and junk here is the
        whole attack -- so the port is *parsed*, not trimmed, and must be
        digits.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            address, _, rest = host.partition("]")
            address += "]"
        elif host.count(":") == 1:
            address, _, rest = host.partition(":")
            rest = ":" + rest
        else:
            address, rest = host, ""
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return False
        return (address in self.LOCAL_HOSTS or address == self.bound_host
                or address in self.allowed_hosts)

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
            asks=bool(args.get("asks")),
            limit=int(args.get("limit", 200)))
        return rows


def serve(db: "Path | None" = None, host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True, allow_hosts: "tuple | list" = ()) -> int:
    path = Path(db) if db is not None else clog.db_path()
    conn = clog.connect(path)
    total = clog.summary(conn)["messages"]
    conn.close()

    allowed = frozenset(h.strip().lower() for h in allow_hosts if h.strip())
    handler = type("Handler", (_Handler,),
                   {"db_file": path, "bound_host": host.strip().lower(),
                    "allowed_hosts": allowed})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{httpd.server_port}/"
    print(f"Conversations: {total} message(s) from {path}")
    print(f"Serving on {url}  (Ctrl-C to stop)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        # Said out loud because the default is the safe one, so reaching this
        # line means somebody typed `--host` and may not have thought about
        # what the store contains.
        print(f"WARNING: bound to {host}, not loopback. This store holds every "
              "word typed to an agent on this machine, and anything that can "
              "reach that address can read all of it.")
        if host in ("0.0.0.0", "::", "*") and not allowed:
            # A wildcard bind cannot name itself. Clients reach it by the
            # machine's own address or hostname and never by the literal
            # `0.0.0.0`, so the Host guard refuses every one of them and the
            # flag the user typed does nothing but print this warning. Naming
            # the remedy rather than quietly widening the guard: which names
            # are legitimate is a fact about their network, not ours.
            print("         Requests will be refused: a wildcard bind matches "
                  "no Host header. Add --allow-host <name-or-ip> for each "
                  "address browsers will use.")
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
