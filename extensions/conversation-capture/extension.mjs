// Capture what is said to this agent, and what it says back.
//
// Deliberately the dumbest component in this feature. It appends one JSON
// object per line to ~/.operator/conversation-spool/<date>.jsonl and does
// nothing else -- no classification, no database, no schema.
//
// That split is the point. Deciding whether a "user message" is a human, the
// operator's launch preamble or a peer message injected by `operator send` is
// the only interesting logic here, and it lives in conversation_log.py where
// it is tested against real captured text. A second copy of those rules in
// JavaScript would agree with the first until one of them was edited, and
// nothing would report the disagreement -- the store would just quietly start
// filing 39% of its rows under the wrong speaker.
//
// Appending also means no SQLite writer in Node (no version floor, no lock
// contention with the viewer) and no way for a capture failure to interrupt a
// session: every write is wrapped, and a spool file that cannot be written is
// a lost log line, never a broken turn.

import { appendFileSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { joinSession } from "@github/copilot-sdk/extension";
import { inboundEvent, outboundEvent } from "./events.mjs";

const HOME =
  process.env.COPILOT_OPERATOR_HOME || join(homedir(), ".operator");
const SPOOL = join(HOME, "conversation-spool");

//: An off switch, because this one records what a human typed. The other
//: extensions with a knob use it to skip work; here it is about consent, so
//: it is checked before any hook is registered rather than inside the writer.
//: A capture that hooks but discards is indistinguishable from one that does
//: not hook, right up until someone changes the writer.
const DISABLED = process.env.COPILOT_CONVERSATION_CAPTURE_DISABLE === "1";

/** Best-effort append. A logger must never be able to fail a session.
 *
 * Takes a *builder*, not an event. The first version took the event, so the
 * arguments were evaluated at the call site -- outside this try -- and
 * `randomUUID()`, `isoFrom()` and especially `process.cwd()` could all throw
 * from there. `process.cwd()` throws on POSIX once the working directory has
 * been removed, which is the ordinary end of every `git worktree remove`, and
 * the throw would reject the hook's promise mid-turn. Moving construction
 * inside means there is no expression left on the outside to fail.
 */
function spool(build) {
  try {
    const event = typeof build === "function" ? build() : build;
    mkdirSync(SPOOL, { recursive: true });
    const day = new Date().toISOString().slice(0, 10);
    appendFileSync(
      join(SPOOL, `${day}.jsonl`),
      JSON.stringify(event) + "\n",
      "utf8",
    );
  } catch {
    // Intentionally silent: stderr from an extension lands in the user's
    // terminal mid-turn, and a disk hiccup is not worth interrupting them for.
    // Loss is detectable -- `operator conversations stats` shows the day.
  }
}

/** `process.cwd()`, or "" if the directory it named is gone. */
function cwdOrEmpty() {
  try {
    return process.cwd();
  } catch {
    return "";
  }
}

const iso = () => new Date().toISOString();

/** `value` as an ISO string, falling back to now.
 *
 * `new Date(junk).toISOString()` throws RangeError rather than returning
 * anything, and this ran in the hook body -- outside `spool`'s try -- so one
 * unparsable timestamp from the CLI would reject the hook's promise mid-turn.
 * A logger taking the turn down with it is the one failure mode this file is
 * written to make impossible.
 */
function isoFrom(value) {
  if (value === undefined || value === null || value === "") return iso();
  const when = new Date(value);
  return Number.isNaN(when.getTime()) ? iso() : when.toISOString();
}

const session = DISABLED
  ? null
  : await joinSession({
      hooks: {
        // Inbound. Returning nothing leaves the prompt exactly as typed: this
        // extension observes, and must never alter what the agent is asked.
        onUserPromptSubmitted: async (input) => {
          spool(() => inboundEvent(input, {
            id: randomUUID(),
            now: isoFrom(input?.timestamp),
          }));
        },
      },
    });

// Outbound. `assistant.message` carries the finished reply; the reasoning and
// token deltas arrive as separate `assistant.reasoning_delta` /
// `assistant.message_delta` events and are deliberately not subscribed to.
// The ask was for what was said, not for how it was arrived at, and the
// deltas are where all the volume is.
session?.on("assistant.message", (event) => {
  spool(() => outboundEvent(event, {
    id: randomUUID(),
    now: iso(),
    cwd: cwdOrEmpty(),
  }));
});
