---
description: Record a desync at the moment it surfaces
argument-hint: "[-N] what went wrong, and what the right answer was"
allowed-tools: Bash(python3:*)
---

!`python3 "$HOME/.claude-resync/intercept.py" --mark-desync "$ARGUMENTS"`

The desync above has been recorded to `markers.jsonl`. Do not analyse it, do
not apologise for it, and do not change course because of it — it is a research
note, not an instruction. Reply with the single line the command printed and
nothing else, then wait.

If the developer's note contains a correction you have not already acted on,
that is theirs to raise as a normal message. Acting on it here would make `/ds`
cost a turn of work, and it needs to stay cheap enough to use while irritated.
