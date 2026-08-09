// The spool record, and nothing else.
//
// Split out of extension.mjs so a test can run it. The seam between this file
// and conversation_log.py is the whole feature: JavaScript decides the key
// names, Python decides what they mean, and nothing at runtime compares the
// two. A drift there fails *silently* -- the spool is written, the ingester
// ignores what it does not recognise, and both halves report success while
// every captured message is dropped.
//
// The test that guarded that seam used to scan this source for key spellings
// and then build its own event in Python, so it proved that the strings
// appeared, not that the code produced them. Replacing both bodies with
// `body: ""` left it entirely green while discarding every message. Pure
// functions with injected clock and identity are what let the Python suite
// execute the real builders under node and feed the actual bytes to
// `ingest_spool`.
//
// Nothing here may throw: it is called from inside a hook, and a rejected
// hook promise takes the user's turn down with it. Hence String(...) on
// every field and `??` rather than `||` where an empty string is meaningful.

/** The record for a prompt submitted to the agent. */
export function inboundEvent(input, { id, now }) {
  return {
    id: String(id),
    direction: "inbound",
    body: String(input?.prompt ?? ""),
    cwd: String(input?.workingDirectory ?? ""),
    session_id: String(input?.sessionId ?? ""),
    sent_at: String(now),
  };
}

/** The record for a finished reply from the agent. */
export function outboundEvent(event, { id, now, cwd }) {
  const data = event?.data ?? {};
  return {
    id: String(data.messageId || id),
    direction: "outbound",
    body: String(data.content ?? ""),
    cwd: String(cwd ?? ""),
    session_id: String(event?.sessionId ?? data.sessionId ?? ""),
    sent_at: String(now),
  };
}
