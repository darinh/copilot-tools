---
name: peer-agents
description: When and how to hand a bounded chunk of work to a separate operator session (a peer agent) rather than a subagent, and how messaging between agents works. Load this skill before starting or messaging another operator instance.
---

# Peer agents

`operator` starts a full, first-party Copilot CLI session in its own terminal.
That is a **peer**, not a sub-agent: separate process, separate context, its own
git work, and it keeps going after you stop watching. A sub-agent (`task`) is a
function call that returns to you.

| | Sub-agent (`task` tool) | Peer agent |
|---|---|---|
| What it is | A helper inside **your** session | A separate Copilot CLI process |
| Context | Shares your budget, reports back | Its own context, session, metrics |
| Lifetime | Ends when it returns | Loops across sessions until stopped |
| Relationship | Works **for** you | A **peer**; nobody owns anybody |
| Communication | Return value | `operator send` / `operator reply` |

## When to delegate to a peer

All three must hold:

- The chunk is **large** — worth a session of its own.
- It has a **clear boundary** — a set of paths, a subproject, a work item.
- It meets the rest of the system through a **defined contract** — an API shape,
  a file format, a CLI surface — that both sides can build against without
  watching each other work.

Do **not** start one when you would have to supervise it turn by turn, when the
boundary is vague, when both parts edit the same files (that is a merge conflict
with extra steps), or when the whole job is small. A peer you have to babysit
costs more than it saves.

## Starting one

Run from the directory the new agent should own:

```bash
operator --loop --headless --name payments-api --agent anvil
```

- `--loop` — restart the session automatically, so it keeps working across
  context resets instead of stopping at the first handoff.
- `--headless` — **start without attaching.** This is the flag that makes an
  agent-started agent possible: without it a full-screen TUI takes over your
  terminal.
- `--name` — the instance name, and therefore the mail address. Set it
  explicitly when starting someone else's loop; the default is derived from the
  current directory.
- `--agent` — which agent persona to run.

Do not try to stuff the whole assignment into the launch command. Start the
agent, then **send it the brief**: the contract, the boundary, and what "done"
looks like.

```
operator list                  # who is running, and whether their code is current
operator join payments-api     # attach and watch (usually a human thing to do)
operator stop payments-api     # stop the loop and its session
```

## Isolation

Give a peer its own worktree — ideally its own repo. Two loops in one working
tree fight over the git index, the checked-out branch, and each other's
uncommitted changes.

Within one repo, isolation comes from two things, and it is worth being honest
about their strength:

1. **The instruction you gave it.** This is a request, not a sandbox. Nothing
   prevents a peer editing any file it can reach, and it will not know it strayed.
2. **Its work-item claim**, which stops another agent taking the same item and
   lets liveness recovery find its worktree.

If the boundary actually matters, use separate repositories.

## Messaging

**You do not poll a mailbox.** Messages arrive in your session as they happen:

```
[OPERATOR] Message from AGENT:payments-api — <content>
```

and a message sent while you were between sessions is handed to you **at the
start of your next session**, printed in full, already marked read.

Every delivered message carries the exact reply command already filled in. Use
it rather than reconstructing it:

```bash
operator reply --instance <your-instance> --to payments-api "your reply"
```

`--instance` is how the command knows who is replying. It falls back to
`$OPERATOR_INSTANCE` if that is set in your environment, and refuses rather than
guessing if neither is available — a reply carries an assertion its recipient
will act on, and signing it with the wrong name puts words in another agent's
mouth. Your instance name is in your session preamble ("Operator instance: ...").

`--to` defaults to whoever most recently wrote to you, which is right for one
conversation and wrong exactly when a batch arrives from several peers. The hint
printed with each message always names it explicitly; keep it.

To start a conversation rather than continue one:

```bash
operator send --from <your-instance> --to <their-instance> "message"
operator send --from a --to b -- "--dash-leading text"
```

### Rules that matter

- **A recipient operator does not recognise is rejected**, and the known names
  are listed. A typo'd name would otherwise sit in a mailbox nobody reads. Use
  `--force` only for an instance you are about to start.
- **A flag either command does not recognise is refused, not ignored.** A typo'd
  flag delivered as message text is a message its sender believes was never sent.
  If your text starts with a dash, put it after `--`.
- **"Nobody has written to you" and "your mailbox could not be read" are
  different answers**, with different exit codes. Only the first means there is
  no reply to send.
- `operator inbox` still exists, and is now for **audit**, not for receiving:
  `--history` is a real record of who told whom what. Note that a plain read and
  `--json` both *consume*; only `--peek` and `--history` leave mail alone. Always
  pass your own name — with none, the mailbox is named after the directory, which
  in a shared checkout is nobody in particular.

### Delivery

| Recipient state | What happens |
|---|---|
| Session running | Typed straight into its session; it sees it right away |
| Between sessions | Queued, and printed to it at the start of its next session |

Either way it arrives — only the timing differs. Use `--queue` to force the slow
path when a message should be picked up at a natural break instead of
interrupting work in progress.

### Etiquette

- Say who you are, what you need, and whether you need an answer. The recipient
  has none of your context and cannot see your screen.
- Send the **contract, not the conversation**: "endpoint returns `{id, status}`,
  409 on duplicate" beats a paragraph of narrative.
- **Answer your mail before you write a handoff.** A peer blocked on your reply
  is burning sessions doing nothing, and it cannot tell the difference between
  "thinking about it" and "never saw it".
- Don't use mail to carry what a file carries better. Point at a path.
- Tell the other agent when you are done, or when you have changed something on
  your side of the contract. It has no other way to find out.
- **Report what you measured, and say what you did not.** A peer who hands over
  a plausible mechanism they never demonstrated costs the recipient the time to
  disprove it — after they have believed it.

## Contracts are the seam

If you and a peer both need to change the same contract, that is a sign the split
was wrong. Renegotiate the boundary rather than both editing it.

## A worked example

You own `checkout-service` and the job needs a new payments API that only touches
its own repo, behind an agreed HTTP contract.

```bash
# 1. Start the peer in its own project directory
cd ~/repos/payments-api
operator --loop --headless --name payments-api --agent anvil

# 2. Brief it
operator send --from checkout-service --to payments-api \
  "You own ~/repos/payments-api only. Build POST /charges taking {amount_cents, currency, idempotency_key} and returning {id, status}; 409 on a repeated idempotency_key. Do not edit checkout-service. Message me when the contract is live."

# 3. Carry on with your own work. Its answer will arrive in your session,
#    or at the start of your next one.
```
