---
name: operator-agents
description: Use when you need another agent working in parallel rather than another tool call — delegating a bounded piece of a large task, starting a headless operator loop, or sending and receiving messages between running agents with operator send / operator inbox.
---

# Operator — Parallel Agents and Mail

`operator` runs a **full, first-party GitHub Copilot CLI** inside a terminal
multiplexer session (tmux/psmux in a terminal tab). Starting one gives you a
peer agent, not a helper.

## These are not sub-agents

| | Sub-agent (`task` tool) | Operator agent |
|---|---|---|
| What it is | A helper inside **your** session | A separate Copilot CLI process |
| Context | Shares your budget, reports back to you | Its own context, session, metrics |
| Lifetime | Ends when it returns | Loops across sessions until stopped |
| Relationship | Works **for** you; you own the result | A **peer**; nobody owns anybody |
| Communication | Return value | `operator send` / `operator inbox` |

A sub-agent is a function call. An operator agent is a colleague: its own
terminal, its own handoff file, its own git history, and it keeps working after
you stop paying attention to it. You cannot read its context and it cannot read
yours.

## Decide: should you start one?

Start a parallel agent when **all** of these hold:

- The work is big enough that one agent would burn its context on it.
- It divides along a **clear boundary** — a separate deliverable, not the next
  step of the same deliverable.
- The seam between the parts is a **defined contract** (an API shape, a file
  format, a schema, a CLI surface) that both sides can build against without
  watching each other work.
- Each part has, or can be given, **its own folder**.

Do **not** start one when:

- You would have to supervise it turn by turn — that is slower than doing it.
- The boundary is vague, or the contract is "we'll figure it out as we go".
- Both parts edit the same files. That is not parallel work, it is a merge
  conflict with extra steps.
- The whole job is small. Starting an agent costs a session; just do the work.

## Give it its own project

**An operator agent needs a dedicated directory, ideally its own repository.**
The instance name is derived from the directory name, the handoff file is keyed
to the project, and the loop commits, merges and restarts on its own schedule.
Two loops sharing one working tree will fight over the git index, the checked
out branch, and each other's uncommitted changes.

You *can* point two operator agents at one project — for example one per
worktree. Be honest about what that buys you: **there is no enforcement.** The
only thing keeping a parallel agent inside its lane is the instruction you gave
it asking nicely. It is a vibe-wish, not a sandbox. Nothing prevents it from
editing any file it can reach, and it will not know it strayed. If the boundary
actually matters, use separate repositories.

## Starting one

Run from the directory the new agent should own:

```bash
operator --loop --headless --name payments-api --agent anvil
```

- `--loop` — restart the session automatically, so it keeps working across
  context resets instead of stopping at the first handoff.
- `--headless` — **start without attaching.** This is the flag that makes an
  agent-started agent possible: without it, `operator --loop` attaches and a
  full-screen TUI takes over your terminal.
- `--name` — the instance name, and therefore the mail address. Set it
  explicitly when starting someone else's loop; the default is derived from the
  current directory.
- `--agent` — which agent persona to run.

Managing them:

```bash
operator list                  # who is running
operator join payments-api     # attach and watch (usually a human thing to do)
operator stop payments-api     # stop the loop and its session
operator                       # interactive browser: stats, join, stop
```

Do not try to stuff the whole assignment into the launch command. Start the
agent, then **send it the brief**: the contract, the boundary, and what "done"
looks like.

## Operator Mail

Parallel agents are separate OS processes with no shared memory. They talk by
mail.

```bash
operator send --from <your-instance> --to <their-instance> "message"

operator inbox                 # read yours (marks them read)
operator inbox --peek          # read without marking
operator inbox --history       # what was already delivered
operator inbox --json          # machine-readable
operator inbox payments-api    # read a specific instance's mailbox
operator send --from a --to b -- "--peek is the flag"   # dash-leading text
```

### Rules that matter

- **`--from` and `--to` are both required.** The recipient needs to know who
  wrote and where to send the answer. Your own instance name is in your session
  preamble ("Operator instance: ...").
- Every message you receive arrives with the **exact reply command already
  filled in**. Use it rather than reconstructing it.
- A recipient the operator does not recognize is **rejected**, and the known
  names are listed. That is deliberate: a typo'd name would otherwise sit in a
  mailbox nobody reads. Use `--force` only for an instance you are about to
  start.
- **A flag either command does not recognize is refused, not ignored.** Reading
  an inbox archives what it shows, so a typo'd `--peek` would otherwise eat the
  mail it was meant to leave alone, and a typo'd flag on `send` would be
  delivered as message text by a sender who believed nothing was sent. If your
  message — or an instance name — starts with a dash, put it after `--`.

### Delivery

| Recipient state | What happens |
|---|---|
| Session running | Typed straight into its session; it sees it right away |
| Between sessions | Queued, and handed to it at the start of its next session |

Either way it arrives — only the timing differs. Use `--queue` to force the
slow path when a message should be picked up at a natural break instead of
interrupting work in progress.

Messages are stored under `~/.operator/messages/<instance>/`, and read ones are
archived rather than deleted, so `--history` is a real audit trail of who told
whom what.

### Etiquette

- Say who you are, what you need, and whether you need an answer. The recipient
  has none of your context and cannot see your screen.
- Send the **contract, not the conversation**: "endpoint returns `{id, status}`,
  409 on duplicate" beats a paragraph of narrative.
- **Answer your mail.** A parallel agent blocked on your reply is burning
  sessions doing nothing.
- Check `operator inbox` when you start work, and again before you write a
  handoff — a handoff that ignores a pending question wastes the next session.
- Don't use mail to carry what a file carries better. Point at a path.
- Tell the other agent when you are done, or when you have changed something on
  your side of the contract. It has no other way to find out.

## A worked example

You own `checkout-service` and the job needs a new payments API that only
touches its own repo, behind an agreed HTTP contract.

```bash
# 1. Start the peer in its own project directory
cd ~/repos/payments-api
operator --loop --headless --name payments-api --agent anvil

# 2. Brief it
operator send --from checkout-service --to payments-api \
  "You own ~/repos/payments-api only. Build POST /charges taking {amount_cents, currency, idempotency_key} and returning {id, status}; 409 on a repeated idempotency_key. Do not edit checkout-service. Message me when the contract is live."

# 3. Carry on with your own work, and check for the answer
operator inbox
```
