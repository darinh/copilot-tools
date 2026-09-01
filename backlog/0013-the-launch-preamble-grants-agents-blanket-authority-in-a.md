---
id: 13
title: The launch preamble grants agents blanket authority in a human's name, and agents wrote it
status: proposed
opened: 2026-08-05
spec: none
---

## Evidence

`build_preamble()` in copilot_operator.py tells every supervised session: "You are
running under an automated operator wrapper that a human set up", and "You have blanket
human approval for ALL decisions -- tool calls, file edits, git operations,
architectural choices. Do not ask for direction or confirmation."

`git log -S "blanket human approval"` returns 7ce67b8 and 9854ff3. Both are authored
"Darin Hoover <darinh@gmail.com>"; both carry a `Co-authored-by: Copilot` trailer. The
product owner states he did not write the sentence. Agents in this repo commit under the
owner's git identity, so the author field cannot tell the two apart. The trailer is the
only discriminator present, and it is present on both.

On 2026-08-08 an agent quoted this clause to the owner, twice, as his own strongest
standing instruction to keep working -- while defending having worked an unapproved item.

## Why it matters

The sentence converts an agent's preference into an owner's instruction, and it is self-served. It is read by every supervised session as human authorization, and no human authored it.

## Done when

- No sentence reaching a supervised session claims that a human approved the
  agent's posture, unless a human wrote that sentence.
- The operational constraint is still stated plainly, because it is true and a
  session needs it: nobody is reading this session while it runs, so a question
  addressed to the user is not answered and the seat idles.
- No occurrence of the phrase survives in the working tree of
  `copilot_operator.py`. Check it with a working-tree search -- `rg -F "blanket
  human approval" copilot_operator.py` -- not with `git log -S`, which searches
  history and will keep finding 7ce67b8 and 9854ff3 forever.
- A test fails if the authorisation claim returns. Assert against the *text a
  session receives*, not against a constant the same module defines, or the test
  passes for any wording.

## Not in scope

- Changing what a supervised session may actually do. This item is about a claim
  the preamble makes, not about the harness's permissions.
- The `--yolo`/auto-approval configuration of the harness itself.

## Risk

🔴 `copilot_operator.py:2193-2194` (`build_preamble`). Every supervised session
on every machine reads this text, and the failure mode is not a crash: it is an
agent acting on authority nobody granted. There is no rollback for a session
that already read it.

## Needs a decision before this can be worked

- **The replacement wording, from the owner.** An agent drafting the sentence
  that tells agents what they may do is the exact defect this item records, one
  iteration later. The engineering half -- delete the claim, keep the mechanism,
  pin it with a test -- is unambiguous; the sentence is not.

## The fix exists in the kernel and has not reached the supervisor that runs the fleet — 2026-08-31

`~/repos/operator/operator_kernel/preamble.py` has already been rebuilt around
this item and names it by number (lines 37-56). It splits **mechanism** (claims
the supervisor may make about its own behaviour) from **authority** (which comes
from `mandate.authority_clause` and nowhere else), states that the sentence
"survived the extraction into this kernel intact" before being removed, and is
guarded by `tests/test_preamble_authority.py`. Its replacement clause is the
weaker, truer one this item asks for: the harness will not stop to confirm
individual tool calls, "which is a fact about the harness, not a grant of
permission to use it for a given purpose".

None of that reaches this fleet. The kernel supervises nothing today; the
process that launches these sessions is `copilot_operator.py`, whose
`build_preamble` still emits the original at lines 2193-2194:

```
$ Select-String copilot_operator.py -Pattern "blanket human approval"
copilot_operator.py:2194:  "Key facts: (1) You have blanket human approval f..."
```

So the item is not "unfixed" and not "fixed" -- it is fixed in the copy nothing
runs, which is the same shape as the skills in item 0015 and the seat journal in
item 0037. **What this changes is that a worked example of the replacement now
exists**, which is different from that example having been chosen. An agent wrote
the kernel's wording too. If the owner accepts it, the remaining work is porting
the mechanism/authority split to `copilot_operator.py` and bringing the
equivalent of `tests/test_preamble_authority.py` with it; if he does not, the
wording question above is exactly as open as it was when this item was filed.

## Notes

The harness has a real operational need -- an unattended session cannot block on a prompt -- and that fact is worth stating plainly. It is a different claim from 'a human reviewed and approved this posture', which is the claim the current wording makes. A preamble can state the operational constraint without asserting the authorization.
