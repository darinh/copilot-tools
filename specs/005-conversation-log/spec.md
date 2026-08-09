# Feature Specification: Conversation Log

**Feature Branch**: `feat/conversation-log`

**Created**: 2026-08-09

**Status**: **Delivered**

## Summary

One searchable record, per machine, of everything said *to* an agent and
everything an agent said *back* — across every project, from every source that
already holds some of it.

The request that produced it: *"I want to quickly go back and see the messages
I've sent to agents and the replies."*

## The problem this solves

The answer to "what did I ask that agent, and what did it say?" was, before
this, spread across three stores that never met:

| Store | Holds | Missing |
| --- | --- | --- |
| `~/.copilot/session-store.db` | Every prompt an agent received | 76% of replies (`assistant_response` is NULL) |
| `~/.operator/messages/` | Agent-to-agent mail, with sender and recipient | Anything a human said |
| Nothing | What an agent actually replied, going forward | — |

None of them is queryable across projects, and none distinguishes a human's
words from the ~1,400 words of operator preamble prepended to every session.

## Measured facts about the corpus

These are counts taken from this machine on 2026-08-09, not estimates. They
are what the design is shaped around.

| Fact | Count | Consequence |
| --- | --- | --- |
| Turns in the session store | 3,527 | The seedable universe |
| Turns that are the operator launch preamble | 1,378 (39%) | Must not be filed as human speech |
| Turns that are peer messages injected by `operator send` | 238 (7%) | Belong to the mail store, not here |
| Turns with a NULL `assistant_response` | 2,673 (76%) | Seeding cannot recover most replies |
| Mail files on disk | 286 (284 `live`, 2 `queued`) | Mail is the authoritative copy |

Filed naively, **46% of the record would be machine text presented as
something the human said.**

## Requirements

### FR-1 — One store, three sources

`conversation_log.py` owns a SQLite database at
`~/.operator/conversations.db` (`db_path`). Every row names the `source` it
came from: `session` (the CLI's own store), `mail` (`operator send`), or
`hook` (the live capture spool).

### FR-2 — Seeding is idempotent

The user seeds manually, per machine, so it *will* be run twice.
`UNIQUE (source, source_id)` plus `INSERT OR IGNORE` in `record()` makes a
re-seed a no-op. Measured: a second `operator conversations seed` over 4,429
rows adds 0.

### FR-3 — Speech is classified, not assumed

`classify()` separates `human` from `preamble` from `peer`. `peer_sender()`
extracts the sending instance from the `[operator message from "name"]`
prefix that `operator send` puts on delivered mail.

### FR-4 — Mail owns agent-to-agent messages

The session store contains peer-prefixed *copies* of messages the mail store
already holds with better fields (real sender, real recipient, delivery state,
read time). `seed_operator_mail` is therefore the only source of `direction =
'peer'` rows; the session seeder skips them. One rule, one place.

### FR-5 — An absent source is not a failure

`SeedReport` distinguishes `absent` from `failed`. A machine that has never
run a peer agent has no `~/.operator/messages/`; that is a clean install, and
`operator conversations seed` exits 0 on it. A source that is present and
unreadable exits non-zero.

### FR-6 — Future replies are captured

`extensions/conversation-capture/extension.mjs` appends one JSON object per
line to `~/.operator/conversation-spool/<date>.jsonl` — inbound prompts via
`onUserPromptSubmitted`, finished replies via the `assistant.message` event.
`ingest_spool` folds them in. Reasoning and token deltas are deliberately not
subscribed to: the ask was for what was said, not how it was arrived at.

Capture is off when `COPILOT_CONVERSATION_CAPTURE_DISABLE=1`, checked before
any hook is registered.

### FR-7 — Search survives the input, and filters

`_fts_query` quotes and ANDs every token, because searching for `--force`, for
`a"b`, or for `C:\path\to.py` is the normal case and each is an FTS5 syntax
error unquoted.

A search that is *all* punctuation — `(`, `*`, `-->` — tokenises to nothing
under FTS5. Such a search takes the substring path rather than dropping the
predicate: a search that silently returns every row is worse than one that
returns none, because an unfiltered list reads as an answer. `_like_term`
escapes `%` and `_`, which are LIKE's own wildcards.

Where FTS5 is unavailable the store falls back to substring matching and
`search_mode()` *reports which* — a silently degraded search returns fewer
rows and looks exactly like a quiet week.

### FR-8 — The viewer is local only

`conversation_viewer.serve()` binds `127.0.0.1`, and every request's `Host`
header must name loopback. The bind stops a remote socket; the header check
stops DNS rebinding, where a page the user is merely visiting resolves its own
hostname to `127.0.0.1` and reads the API same-origin from the user's own
browser. The store holds every word a human has typed to an agent on this
machine, so both halves are needed.

## Non-goals

- **Not a system of record.** Every row names its source; losing the database
  costs a re-seed, nothing more.
- **Not thinking.** Explicitly excluded by the request.
- **No new dependencies.** Stdlib only — `sqlite3`, `http.server`, `json` —
  so `pyproject.toml` is unchanged.

## What the user gets, and the honest limitation

Seeding recovers **what the user said** well. It recovers only **24% of what
agents replied**, because the CLI's own store did not keep the rest. The
capture extension is what fixes that going forward, and it only applies to
sessions started after `setup.sh` / `setup.ps1` has deployed it.

That limitation is a property of the source data, verified by direct query
against `session-store.db`, not a defect in the seeder.

## What counts as the human speaking

Everything reaching an agent arrives as a "user message", which is a fact
about the transport and not about who spoke. Three kinds of machine text
arrive that way and are filed under their real speaker:

| Insertion | Detected by | Filed as |
|---|---|---|
| The operator launch preamble | `PREAMBLE_MARKER` in the first 400 chars | `system`, sender `operator` |
| A peer message from `operator send` | `PEER_PREFIX_RE` | declined — mail owns it |
| The CLI's `<system_reminder>` blocks | `is_only_machine_text()` | `system`, sender `copilot-cli` |
| The CLI's `<skill-context>` blocks | `is_only_machine_text()` | `system`, sender `copilot-cli` |

The last two were found by running the finished feature against the real store:
**586 of 1918 rows filed as human speech — 31% — were injected blocks and
nothing else** (462 `<system_reminder>`, 124 `<skill-context>`). Not one of the
586 contained a word the human typed. Asked "what did I say", the store
answered with a third of its own instruction files.

`_MACHINE_TAGS` is a list because there will be a third. Both of these were
found by reading the finished store rather than a fixture, and adding another
is one word. What must *not* go in it is any tag a person might type: the
other tags occurring in human bodies are `<feature-branch>`, `<merge-sha>`,
`<path>`, `<the>` and `<div>` — placeholders somebody typed, or content inside
a block already matched. Widening the rule to "anything angle-bracketed" would
silently delete what the store exists to keep, so each of those has a test.

Two details are load-bearing. The test is *what remains after removing the
blocks*, not *does the body start with one*: none of the 462 started with the
tag, because the CLI writes a newline first, so a prefix check finds zero of
them and reports the corpus clean. And a body that is *partly* a reminder
stays human, because the reminder is appended to something a person wrote —
the mirror failure would lose the sentence.

`record()` re-applies the classification when a row already exists, so a
corrected rule reaches rows already filed under the old one. Seeding is
otherwise idempotent in the unhelpful direction: the fix would ship and every
previously-misfiled row would stay misfiled, with "delete the database" as the
only remedy — which is exactly the sort of thing nobody knows to do. The body
is never rewritten; a message's text is what was said, and only the verdict
about it is ours to revise. Re-seeding the real store moved 586 rows out of
`human` and 7 agent replies into `agent-agent`.

## The viewer reads as a conversation

Bodies are rendered as Markdown, and the two sides sit on opposite edges:
**inbound on the right** — what was said *to* the agent, by the human or by a
peer — and **outbound on the left**, the agent answering. That is the
arrangement every chat client uses for "me" and "them", and it is what makes a
long session scannable without reading it.

Supported: fenced and inline code, headings, bold, italic, ordered and
unordered lists, blockquotes, horizontal rules, and links. Tables are *not*
rendered and appear as their source text — noted because agent replies in this
corpus contain them.

### Rendering is the only XSS surface this toolkit has

The text being turned into HTML is every word ever typed to an agent on this
machine, plus whatever a peer sent and whatever a web page an agent was
reading. One invariant carries the whole design: **escape first, then add
markup.** `esc()` runs over the entire body before any transform, so the only
angle brackets in the output are ones `md()` wrote; every later transform
operates on already-escaped text and may only add markup, never decode any.

Two consequences worth stating because they are easy to undo:

- **URLs are allow-listed to `http`/`https`.** `javascript:`, `data:` and
  `vbscript:` all execute from an `href`, and no message needs them. Entity
  tricks cannot get round it — `&` is already `&amp;` by the time a URL is
  tested, so `java&#115;cript:` arrives spelled out and fails on its literal
  characters.
- **Search highlighting works on the DOM, not the HTML string.** The previous
  regex was correct while a body was escaped text and nothing else; against
  markup it would insert `<mark>` inside an `href`. Walking text nodes cannot
  reach an attribute, and every inserted node is built with `createElement`
  and `textContent`.

`MARKDOWN_JS` is its own constant so the suite can *execute* it under node
rather than grep the page for reassuring substrings — the failure this feature
has already shipped twice. Twenty-three payloads (raw tags, event handlers,
`javascript:` links in five spellings, quote-breaking URLs, payloads inside
code fences, headings, lists and bold) are rendered and the result is **parsed
with `html.parser`**, then checked against an allow-list of elements, for any
`on*` attribute, and for any non-http scheme in an `href`.

The parser matters. The first version of that test used substring matching and
reported holes that were not there: `&lt;img src=x onerror=alert(1)&gt;` is
*correct* output — the payload rendered as visible text — yet it contains the
characters ` onerror=`. A crude scan cannot tell an attribute from a quoted
one, in either direction.

A control renders the same 23 payloads through a copy of `md()` with the
escaping removed and requires script, img and iframe elements, an event
handler, and a `javascript:` href to all appear — because 23 payloads passing
proves nothing unless the oracle is known to reject something.

Verified beyond the fixtures: all **4,440 messages in the real store** were
rendered and audited, producing zero disallowed elements, zero event handler
attributes and zero non-http URLs.

## Known gap: an agent-to-agent thread has no projectThe `messages` table has one `project` column, and `seed_operator_mail` leaves
it empty for every message it files. That is honest — mail carries no project
today — but it is a real limitation of the "agent conversations, separately"
view: those rows can be told apart by `channel`, and cannot be grouped by
which project they belong to.

The 0025 review council named the underlying reason, and it is sharper than
"the field is empty": **a message does not have one project. It has an origin
and a delivery context, and they can differ.** A single column cannot say that
an agent working in `copilot-tools` wrote to an agent working in `scripts`,
which is exactly the shape 5 of the 10 ordered instance pairs on this machine
have.

Backlog item **0025** (approved) adds a nullable origin and destination to
each message at send time, with an explicit status when either is unknown.
When that lands, `seed_operator_mail` should carry **both endpoints** into the
store and the viewer should show the tri-state — same-project, cross-project,
or project unknown — rather than folding an unknown affiliation into a known
one. The 286 messages predating that change are unknowable and must render as
unknown, never as same-project.

Recorded here rather than fixed here: the field does not exist yet, and
inventing one on this side would mean guessing a project from an instance
name, which is the guess the council explicitly ruled out.
