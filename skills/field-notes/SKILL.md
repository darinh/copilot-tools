---
name: field-notes
description: Writing entries in the cross-project agent-field-notes journal about working with AI agents. Load this skill when something in a session surfaces a transferable insight about agent behaviour, prompting, orchestration, or failure modes.
---

# Field notes

A cross-project journal about *working with AI* — not about any one project's
code. It lives in its own repo, typically `~/repos/agent-field-notes`:

```
journal/             One conversation per file: YYYY-MM-DD-slug.md
essays/              Synthesised principles across entries
essays/_pending.md   Topics awaiting a second data point
```

## When to write one

Proactively, without being asked, when something surfaces a transferable insight:

- Why an agent went sideways, and the principle behind it
- A division-of-labour finding — cheap vs. strong models, when to launch a peer
- A prompt framing that changed behaviour usefully
- A failure mode worth remembering, especially a wrong-but-plausible one
- A verification gap a reviewer caught that your own checks missed
- A remark from the human that reframed how you should operate

**Conversation-driven, not task-driven.** Write because something was said or
noticed that would not be obvious to a future reader who was not in the room.

## When not to

- Routine task summaries → session history
- Project code conventions → that project's `AGENTS.md`
- A single data point with no story → add the theme to `essays/_pending.md` and
  wait for the second instance, then write the entry that ties them together

## Format

```markdown
# YYYY-MM-DD — {short imperative or question}

**Context**: what conversation or task surfaced this. Be specific.

## What I said (the gist)
## What he replied / what we noticed
## What I learned          (numbered, transferable)
## What we changed (or are about to)
## Quote worth keeping     (optional)
```

## Rules

- **Write in the conversation, not after.** Memory rewrites things.
- **Quote actual exchanges.** Do not smooth them.
- **A principle without a story is a slogan.** Always include the story.
- **Do not edit old entries to be right** — write a follow-up. Wrongness is data.
- Commit them. It is a real repo.
