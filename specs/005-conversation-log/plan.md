# Implementation Plan: Conversation Log

**Feature Branch**: `feat/conversation-log`

## Components as built

| File | Role |
| --- | --- |
| `conversation_log.py` | Schema, classification, the three seeders, the query API |
| `conversation_viewer.py` | Loopback `http.server` UI and JSON API |
| `extensions/conversation-capture/extension.mjs` | Live capture into a JSONL spool |
| `copilot_operator.py` | `operator conversations <seed\|serve\|stats>` |
| `tests/test_conversation_log.py` | 55 tests: classification, seeding, ingest, the cross-language seam |
| `tests/test_conversation_viewer.py` | 21 tests over a real socket |
| `tests/test_conversations_command.py` | 14 tests over the CLI surface |

## Decisions, and what each one is defending against

### A JSONL spool, not SQLite written from Node

The capture hook writes lines; all classification stays in Python.

A JavaScript copy of `classify()` would agree with the Python one right up
until either was edited, and *nothing at runtime would report the
disagreement* — the store would quietly start filing 39% of its rows under the
wrong speaker. Appending also means no Node version floor, no lock contention
with a viewer the user may have open, and no way for a capture failure to
break a turn.

`tests/test_conversation_log.py::test_the_spool_directory_is_spelled_the_same_in_both_languages`
pins the one string that must survive the language boundary, with a positive
control. Nothing else compares them: a rename on either side yields a spool
nobody reads and an ingest that finds nothing, and *both halves report
success*, because "no new events" is indistinguishable from "no events
happened".

### Deployment is `setup_tools.install_extensions`, not a verb of our own

An earlier draft had `operator conversations install` copying the extension
into `~/.copilot/extensions`. It was removed: `setup_tools._extension_sources`
already deploys *every* directory under `extensions/`, with manifest tracking,
digest-based upgrade classification, and a Windows junction fallback for
machines without Developer Mode. A second installer is a second place for the
deployment rules to drift.

### `UNIQUE (source, source_id)` as the natural key

The user seeds manually on each machine, so a second run is the expected case,
not the exceptional one. Idempotency is a property of the schema rather than
of the seeder's care.

### Mail is authoritative for peer messages

Decided by counting, not by preference: `operator_mail.record_delivered`
persisted 284 of 286 messages as `live`, with sender, recipient, delivery
state and read time. The session store's peer-prefixed copies have none of
that. So mail wins in *both* the seeder and the capture hook, via one
`peer_sender()`.

### Timestamps normalised on the way in

The session store mixes SQLite `'YYYY-MM-DD HH:MM:SS'` with ISO `...Z`. A
space sorts before `'T'`, so ordering by the raw column interleaves the two
formats wrongly. `_utc()` normalises at write time.

### WAL

The writer is a live agent session while a human may have the viewer open.

## Verification performed

- **90 targeted tests**, all passing.
- **Mutation testing: 23 mutants, 22 killed.** The single survivor is
  genuinely equivalent (re-adding a redundant regex anchor that was removed
  precisely so the remaining one is load-bearing).
- **Two of the survivors were defects in the tests, not the code**, and only
  mutation exposed them:
  - `test_seeding_never_writes_to_the_session_store` opened its *own*
    read-only connection and asserted the write failed — a fact about SQLite,
    true whatever the code did. **Unfalsifiable by its own input.** Rewritten
    to spy on `conversation_log.sqlite3.connect`, take the URI *from the code
    under test*, and prove that URI refuses writes.
  - `test_the_limit_is_bounded` stored 5 rows and asserted `limit=10**9`
    returned 5 — true clamped or unclamped. Rewritten with 1,001 rows plus
    `limit=0` and `limit=-5`, making both ends falsifiable.
- **An unfaithful mutant survived for the wrong reason**: the "viewer shares
  one connection" mutant never actually shared. Rewritten as
  `type("Shared", (), {})()`; it then died.
- **A dead detector was found and deleted.** A regex for "queued message(s)
  from" matched **zero** rows: that string only ever appears in `operator.log`,
  never in a prompt, because `operator_mail.render_for_agent` *appends* mail to
  the preamble. A detector that matches nothing reports the corpus clean.
- **The viewer was fuzzed** with 12 FTS5-metacharacter inputs (`--force`,
  `a"b`, `x:y`, `(`, `*`, a Windows path): no 500s.
- **All 9 HTTP routes** exercised over a real socket, including 400 on a bad
  filter and 404 on an unknown route.

## Defects found by hand during integration, each now pinned by a test

1. **The seeder wrote to the developer's real database.** The command resolved
   the DB from the configured home but let each seeder resolve its own root, so
   a test home read mail from the temporary directory and wrote rows into
   `~/.operator/conversations.db`. Every root is now passed explicitly.
2. **An absent source exited 1**, which would fail `seed` on any machine that
   had never run a peer agent. Became `SeedReport.absent` vs `.failed`.
3. **An invented environment variable.** The module used `OPERATOR_HOME`; the
   toolkit uses `COPILOT_OPERATOR_HOME` (`project_paths.py:146`). The
   duplicate implementation was replaced by an import of
   `project_paths.operator_home`.
4. **A bad timestamp could break a turn.** `new Date(junk).toISOString()`
   throws `RangeError`, and it ran in the hook body *outside* the writer's
   `try`. `isoFrom()` now falls back to the current time.
5. **The extension was undocumented**, which `tests/test_extensions.py` caught
   — `setup` installs every directory under `extensions/`, so an undocumented
   one is deployed to every session on the machine with nothing written down.

## Defects found by adversarial review, after the branch looked finished

The suite was green and mutation-clean when these were found. Each came with
a reproduction; each reproduction was re-run against the fix.

6. **A punctuation-only search returned every row.** `(`, `*` and `-->`
   tokenise to nothing under FTS5, leaving an empty MATCH expression — and the
   code then *dropped the search predicate entirely*. A result set meaning "no
   filter was applied" is indistinguishable from one meaning "everything
   matched", so the user reads an unfiltered list as their answer. Punctuation
   searches now take the substring path, which both finds the rows that
   genuinely contain the characters and keeps FTS and non-FTS machines
   returning the same rows for the same query.
7. **`%` and `_` were live LIKE wildcards** on the substring path — found
   while fixing the above, not reported. Searching `100%` matched every body
   containing `100` followed by anything. `_like_term()` escapes them.
8. **The viewer was readable by DNS rebinding.** Binding `127.0.0.1` stops a
   remote *socket*; it does nothing about a page the user is merely visiting
   resolving its own hostname to loopback and then reading the API
   same-origin, from the user's own browser. Reproduced with `Host:
   attacker.example` returning 200 and the JSON. The `Host` header is now
   checked against a loopback allow-list before any route is dispatched.
9. **An id-less mail message was keyed by its filename.** Mail moves inbox →
   archive as its normal life, and the move may rename; the same message then
   filed twice. Reproduced. Keyed by a SHA-256 of `from/to/sent_at/text`
   instead — the content cannot move.
10. **`--port abc` raised `ValueError`** out of the CLI instead of failing
    cleanly. Now a `die()` with the value quoted, plus a 1..65535 range check.

**The review's most useful finding was about the tests, not the code.** The
existing fuzz tests asserted only that no exception escaped and that HTTP was
200 — which defect 6 satisfies perfectly. They were the "unfalsifiable by its
input" shape in a third form: falsifiable in principle, but scored against an
assertion too weak to see the bug. Both now assert the returned rows.

