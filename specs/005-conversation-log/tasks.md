# Tasks: Conversation Log

All tasks complete. Checkboxes reflect delivered state, not intent.

## Store

- [x] T001 — Schema in `conversation_log.py`: `messages` keyed
      `UNIQUE (source, source_id)`, WAL enabled, FTS5 mirror created
      separately with triggers.
- [x] T002 — `search_mode()` reports whether search is FTS-backed or has
      fallen back to substring matching.
- [x] T003 — `_utc()` normalises the session store's two timestamp formats.
- [x] T004 — `classify()` separates human speech from the operator preamble
      and from peer messages; `peer_sender()` extracts the instance name.
- [x] T005 — `asks_question()` flags replies that ask something, so the user
      can filter for the ones that wanted an answer.
- [x] T006 — `project_of()` derives a project name from a working directory.
- [x] T007 — `record()` holds the idempotency key.
- [x] T008 — `query()` validates enum filters against `_ENUMS` and clamps
      `limit` to 1..1000; `_fts_query()` quotes and ANDs every token.
- [x] T009 — `projects()`, `days()`, `instances()`, `summary()` for the
      sidebar and the `stats` verb.

## Seeders

- [x] T010 — `seed_session_store()`: prompts and any non-NULL replies, peer
      copies skipped.
- [x] T011 — `seed_operator_mail()`: the authoritative agent-to-agent record.
- [x] T012 — `ingest_spool()`: folds captured events in; an unreadable line is
      skipped with a reason, never fatal.
- [x] T013 — `SeedReport` distinguishes `absent` from `failed`.
- [x] T014 — `seed_all()` runs all three and aggregates.

## Capture

- [x] T015 — `extensions/conversation-capture/extension.mjs`:
      `onUserPromptSubmitted` inbound, `assistant.message` outbound, deltas
      deliberately unsubscribed.
- [x] T016 — Every write wrapped; a spool failure is a lost line, never a
      broken turn.
- [x] T017 — `isoFrom()` so an unparsable CLI timestamp cannot reject the
      hook's promise mid-turn.
- [x] T018 — `COPILOT_CONVERSATION_CAPTURE_DISABLE=1` checked before any hook
      is registered.
- [x] T019 — Deployment left to `setup_tools.install_extensions`; the
      duplicate `operator conversations install` verb was removed.

## Viewer

- [x] T020 — `conversation_viewer.PAGE`: single page, All / Human / Agent-mail
      tabs, project and day sidebar, search, actor/direction/question filters.
- [x] T021 — JSON API behind it; 400 on an invalid filter, 404 on an unknown
      route.
- [x] T022 — `threading.local()` connection per thread.
- [x] T023 — `serve(host="127.0.0.1")`.

## CLI

- [x] T024 — `CONVERSATION_VERBS`, dispatch, help line.
- [x] T025 — `_flag_value()` refuses to read the next flag as a value.
- [x] T026 — Every root passed explicitly, so a configured home cannot read
      one store and write another.

## Documentation

- [x] T027 — `README.md`: component table, directory tree, usage example.
- [x] T028 — `extensions/README.md`: extension row and the disable knob.
- [x] T029 — `specs/005-conversation-log/`: `spec.md`, `plan.md`, `tasks.md`.

## Verification

- [x] T030 — 90 targeted tests.
- [x] T031 — Mutation testing: 22 of 23 killed; the survivor is equivalent.
- [x] T032 — Two unfalsifiable tests found by mutation and rewritten.
- [x] T033 — FTS sanitiser fuzzed with FTS5 metacharacters.
- [x] T034 — Cross-language seam test for the spool directory name, with a
      positive control.
- [x] T035 — Full suite green.
- [x] T036 — Adversarial review (gpt-5.3-codex): five findings, all real, all
      reproduced and fixed, each pinned by a test with a positive control.
- [x] T037 — Punctuation-only searches filter instead of returning every row.
- [x] T038 — `_like_term()` escapes LIKE's `%` and `_` wildcards.
- [x] T039 — `Host` header checked against a loopback allow-list, IPv6
      literals included.
- [x] T040 — Id-less mail keyed by content hash, not by filename.
- [x] T041 — `--port` validated with a range check rather than a bare `int()`.
- [x] T042 — The fuzz tests, which asserted only "no exception" and "HTTP
      200", now assert the returned rows. They passed against the bug.
- [x] T043 — Second adversarial review, run against the repairs rather than
      the original code: five findings, four of them reachable through the
      fixes themselves.
- [x] T044 — A NUL byte in `search` no longer truncates the bound LIKE
      pattern to `%` and matches every row.
- [x] T045 — The `Host` port is parsed rather than trimmed, so
      `[::1]evil.com` is refused.
- [x] T046 — The deliberately-bound `--host` joins the allow-list, so the
      flag works; a non-loopback bind warns and names what the store holds.
- [x] T047 — Mail content-hash fields are length-prefixed, so a field
      containing the separator cannot collide two messages onto one key.
- [x] T048 — `--port` rejects `80_80`, `" 8765"` and unicode digits by
      checking `isascii()`/`isdigit()` before converting.
- [x] T049 — The LIKE-escaping regression test forces substring mode. It had
      been taking the FTS branch, and would have passed with the fix deleted.
- [x] T050 — A meta-control asserts `query()` still consults `search_mode`,
      so T049's monkeypatch cannot silently become decorative.
- [x] T051 — Third adversarial review, `gpt-5.6-sol` at maximum effort — a
      different model family from the author, which is what changed between
      this round and the two before it. Ten findings, all real.
- [x] T052 — An FTS index created after the rows exist is rebuilt, so a store
      opened by a newer Python does not answer every search with nothing.
- [x] T053 — `search_mode` runs a MATCH instead of reading `sqlite_master`,
      so a catalogued-but-unloadable index reports `substring`.
- [x] T054 — `_fts_query` splits on FTS5's separator set, so a search for `_`
      falls back instead of matching nothing.
- [x] T055 — `asks` moved into `query()`'s SQL, so it filters before `LIMIT`.
- [x] T056 — Turns already captured by the hook are not filed again from the
      session store; matched per message, so a partly-captured session keeps
      its earlier turns.
- [x] T057 — `seed_all` reads the spool before the session store, asserted by
      calling it rather than by reading its source.
- [x] T058 — A reply inherits the channel of what it answered, on both the
      seeder and the capture path, so answers to peers stay out of the human
      conversation.
- [x] T059 — `to_id` joins the mail content key, so two id-less messages to
      different agents are both kept.
- [x] T060 — `spool()` takes a builder, so `process.cwd()` and every other
      field expression is inside the writer's `try`.
- [x] T061 — `--allow-host` makes a deliberate non-loopback bind usable; a
      wildcard bind without it says why requests will be refused.
- [x] T062 — The forced-substring test replaces `_fts_query` with something
      that raises, because forcing the mode alone did not distinguish the
      paths for that fixture.
- [x] T063 — Event builders extracted to `events.mjs`; the seam test executes
      them under node and ingests the real output, with the `body: ""`
      mutation as its control and a third test asserting the extension still
      calls them.
- [x] T064 — Guard verified by mutating the real `events.mjs` and observing
      the suite go red, then restoring it.

## Found by running the finished feature against the real store

- [x] T065 — `<system_reminder>` blocks recognised as the CLI's own text.
      462 of 1918 rows filed as human speech (24%) were nothing else, and not
      one of them contained a word the human typed.
- [x] T066 — Detected by what remains after removal, not by prefix: none of
      the 462 started with the tag, so a prefix check finds zero and reports
      the corpus clean.
- [x] T067 — A reminder appended to real speech stays human, with a test, so
      the fix cannot trade this failure for its mirror.
- [x] T068 — `record()` re-applies the classification to rows already stored,
      so a corrected rule reaches them; the body is never rewritten.
- [x] T069 — Verified against the real store: 462 rows moved out of `human`,
      7 agent replies moved into `agent-agent`.
- [x] T070 — Viewer exercised end to end on the real 4,438-message store:
      page, summary, agent-agent filter, search, and an `asks` query reaching
      messages older than the page limit.
