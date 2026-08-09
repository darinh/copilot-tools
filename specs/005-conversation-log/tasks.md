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
- [x] T036 — Adversarial review.
