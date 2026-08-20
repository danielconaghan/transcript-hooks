# Plan

Handoff document. Where the project stands, what is next, and the decisions and
platform facts a future session should not have to rediscover.

Read `README.md` first for what the project *is*. This file is what to *do*.

---

## Status

| phase | state | what it delivered |
| --- | --- | --- |
| 1. shared engine | **done** | `rules_engine.py` — triggers + transcript ingestion, imported by both the backtest and the interceptor. `research/backtest.py` refactored onto it. |
| 2. silent rules + fire logging | **done, live** | `hooks/intercept.py` registered as a `UserPromptSubmit` hook. Six non-interrupting rules inject; all thirteen log to `data/fires.jsonl`. |
| 3. asking + labels + API fixes | **done, live** | dedupe by cause, verdict capture (assistant-recorded, regex fallback, offline `--review`), a precision floor, an ask budget, rule suspension, and API-drafted fixes. |
| 4. fix-efficacy measurement | **not started** | see below |

**The phase-2 soak was skipped, knowingly.** The original gate here was "let
phase 2 run a few days, then compare live fire rates against `rules.json`".
Phase 3 was switched on with 9 real prompts of live data instead. Two things
substitute for the soak, and both need watching rather than trusting:

- a **precision floor** stops a rule the backtest has *already* measured as bad
  from ever reaching you, and **suspension** stops a rule whose premise turns out
  to be false (R01) without inventing a figure to demote it with
- an **ask budget** of 5 interrupting fires per session caps the damage if a
  trigger turns out broader live than in replay

What the soak would have caught and these do not: a trigger whose *live* fire
rate diverges from its historical one while its precision looks fine. Compare
`fires.jsonl` against `rules.json` once there is a few days of data, as
originally planned — the gate was skipped, not made unnecessary.

---

## Phase 3 — asking, and the feedback loop

**Built and live.** The goal was to make the `ask` and `block` rules usable and
to replace heuristic precision with your actual verdicts. What follows is the
spec as implemented; where the build departed from the original plan, it says so.

### What is running

`intercept.py` at phase 3:

- **Dedupe by cause.** `rules_engine.dedupes()` owns the policy and both the
  interceptor and the backtest call it, so they cannot drift on what one fire
  means. Seen keys live in the session cache beside the byte offset. Repeat
  observations are still written to `fires.jsonl` tagged `deduped: true` rather
  than dropped — that tag is what made the original bug visible, and dropping
  them would hide the next one.
- **Two-turn verdicts.** Turn N injects and marks the fire pending; turn N+1
  parses a leading yes/no and appends to `data/labels.jsonl`. It refuses to
  label a reply over 200 characters (you moved on, and a long instruction
  opening with "no" is coincidence) or any turn with more than one fire pending
  (a bare "yes" cannot be attributed). Both refusals write `unlabelled`, which
  `backtest.load_user_labels` ignores by design.
- **A precision floor** (`PRECISION_FLOOR = 0.30`) — see the decision below.
- **An ask budget** (`MAX_ASKS_PER_SESSION = 5`), logged as
  `suppressed: "ask-budget"` when it clamps, never silently.
- **A kill switch**: `CLAUDE_RESYNC_PHASE=2` reverts to augments-only without
  uninstalling, because reaching for the uninstaller would also stop the
  measurement.

`python3 ~/.claude-resync/intercept.py --status` reports the current routing,
the suppression counts and the verdict tally. `--verdict "some reply"` shows how
a reply would parse.

### The forward loop is not enough on its own

`python3 research/backtest.py --review [--rule R06] [--limit N]` hand-labels
fires at the terminal and writes to the same `labels.jsonl`, tagged
`source: "manual-review"` against the interceptor's `source: "verdict-reply"`.

It exists because the hook can only judge a fire it actually *surfaced*, which
leaves two blind spots the forward loop cannot reach by construction:

- **`augment` rules never ask anything, so they are never judged.** R08 has 59
  fires and a `null` precision, permanently. R05 sits at 3% and injects into
  every message that names an endpoint, silently, unmeasured.
- **The precision floor is a one-way door without it.** A demoted rule never
  surfaces, so it never earns a verdict, so it stays demoted on the very figure
  that demoted it. Hand review of the historical fires is the only way R06 gets
  out of jail.

Reviewing replayed fires is cheap in a way the live ask is not: the fires
already exist, so the judging happens offline and in bulk, nowhere near the
critical path of a message. Verdicts are flushed every 5 rows, so an
interrupted review keeps what you already gave it.

### How a verdict is recorded

Two paths write `labels.jsonl`, and every row carries a `source` so any figure
can be recomputed without whichever path you distrust:

| source | written by | when |
| --- | --- | --- |
| `assistant-classified` | `intercept.py --label` run by the assistant | the primary path — the assistant reads your natural answer and records it |
| `verdict-reply` | the hook's leading-yes/no parse | fallback, only if the assistant did not record one |
| `manual-review` | `backtest.py --review` | offline, in bulk |

The assistant-classified path exists because the regex path barely works.
Measured over all 441 historical messages, `parse_verdict` labels **5.2%** of
them and inverts some of those — *"nope you are correct its dev-adviser"* parses
as `does-not-apply` while semantically confirming. Reading an answer is the one
part of this loop a model does better than a regex, so the injected directive
asks the assistant to record it via a shell command. That lands in the
transcript as a structured `tool_use`, which phase 4 can detect by shape rather
than by grepping text — the trap that caught the probe.

`--label` clears the fire from the session's `pending` list, so the regex
fallback cannot write a second, dumber label for the same fire.

This does put a model in the labelling path. `PLAN.md` forbids that in the
*trigger* path, for reproducibility; the label path is a different question, and
survivable only because the provenance stays separable and the note keeps your
words verbatim for audit.

### API-drafted fixes

Built. A rule whose `fix` is `{"via": "api", "instruction": ..., "fallback":
...}` has its injection drafted at fire time; `fallback` is a plain template
used whenever the call is off, uncredentialled, slow, rate-limited or refused.
**Enabling the API can improve an injection but can never remove one.**

- `claude-opus-5`, `effort: "low"`, `max_tokens` 400, **5s** timeout,
  `max_retries=0` — the SDK retries timeouts, so retries would multiply
  wall-clock against the hook ceiling. Low effort is the latency lever;
  disabling thinking on Opus 5 risks a tool call landing in visible text.
- Sends the outgoing message, the rule's concern, the reference list and cwd.
  **Not the transcript.**
- Drafts are cached per `(rule, fire_key)` in the session cache, so the same
  references do not re-pay the latency every turn. A `SKIP` reply is cached as
  silence rather than falling back to the generic template.
- `CLAUDE_RESYNC_API=0` stops every outbound call without touching the
  catalogue. Credentials come from `~/.claude-resync/.env` (0600) because a hook
  does not inherit your shell's exports; a real exported variable still wins.
- Requires the SDK in `~/.claude-resync/.venv` — homebrew python is PEP 668
  externally-managed, so a plain `pip install anthropic` is refused.
  `import_anthropic()` reaches into that venv, keyed on interpreter version.
  Without it the status line says so and every R06 fire quietly uses the
  template.

**R06 was moved from `ask` to `augment` to make this reachable at all.** At 3.8%
precision the floor held it at log-only, so an API-drafted fix would have been
drafted for nobody. As an `augment` the floor does not apply, and low precision
costs context rather than attention — which is the right trade for a rule whose
fires are real references and whose *check* is the weak part. The consequence to
watch: R06 fires on ~12% of messages with no dedupe, so expect up to 5s of added
latency the first time a message cites a new set of paths. Build this when a rule
that actually fires needs it.

### The shape, decided

A hook **cannot** ask a question and wait — no controlling terminal, 30s
timeout (see platform facts). So the ask happens **in band, across two turns**,
not in a dialog. An earlier design used an `osascript` popup; it was dropped
because it steals focus, is macOS-only, and has to resolve inside 20 seconds.

**Turn N — you submit a message**

1. `intercept.py` evaluates as it does now
2. Fire records are written unconditionally (already implemented) — this is the
   denominator, and it costs nothing
3. For an `ask`/`block` rule, inject a directive addressed to the assistant:
   *"R06 fired: you cite `../summary` for the questionnaire pattern and it isn't
   there. Ask Daniel whether this applies before acting."* The `fix` templates
   for all thirteen rules are already written in `rules.json`.
4. Mark the fire `pending_verdict` in `fires.jsonl`

**Turn N+1 — you answer**

5. The hook sees a pending fire for this session and that this message is the
   adjacent reply. Parse the verdict **deterministically** — leading
   yes/no/correct/ignore/skip. Ambiguous stays `unlabelled`; never guess.
6. Append to `~/.claude-resync/data/labels.jsonl`:
   `{ts, session_id, rule, fire_key, verdict, note}`
7. If confirmed and the rule's `fix` is `{"via": "api"}`, call the API to draft
   the augmentation and inject it. Latency is tolerable here — it is off the
   critical path of every other message.

`research/backtest.py` already reads `labels.jsonl` and lets your verdicts
override every heuristic. That wiring exists; nothing fills it yet.

### Rules that change behaviour in phase 3

`action_for()` derives behaviour from the catalogue `action` plus measured
precision, between a floor of 0.30 and a ceiling of 0.80. As currently
measured — check with `--status`, not from this table:

| runtime action | rules | behaviour |
| --- | --- | --- |
| `augment` | R02 R03 R05 **R06** R08 R12 R13 | silent, every turn, no dedupe |
| `apply` | R01 R04 | injects its fix, asks nothing; the label comes from whether you object |
| `ask` | R07 R09 R10 R11 | injects a directive, verdict recorded |
| `log` | *(none currently)* | fires and is recorded, never surfaced |

R09 still returns no fires by design: its trigger needs claim extraction, not a
regex, and a guessed regex would put unmeasurable fires into the label stream
and corrupt every other rule's denominator.

Note this file previously recorded R01 at 89% and the catalogue now says 100% —
a later backtest run moved it. Both clear the ceiling so the routing is
unchanged, but **the numbers in this document are stale by construction**; the
catalogue and `--status` are the record. The current backtest also reports 49
sessions / 441 messages against the 51 / 423 in `rules.json`'s provenance, so a
`backtest.py --write` is due.

### Self-tuning, once labels accumulate

A rule reaching ~10 labels at under 30% precision should be demoted from `ask`
to log-only automatically, written back into `rules.json`. The annoying
fortnight then ends by itself rather than needing a decision.

**Half of this is now enforced at build time** by `PRECISION_FLOOR`: a rule
whose *measured* precision is already under 30% starts demoted instead of
earning its demotion through ten bad interruptions. What is still missing is the
write-back — demotion currently follows from the catalogue's precision figure
being refreshed by `backtest.py --write`, not from a live label count. `None`
precision is deliberately not floored: an unmeasured rule has to ask in order
to acquire the labels that measure it.

### API fixes

Default to templates; they cost nothing and cover most rules. Reserve the API
for R05, R06 and R09 where the injection needs reading the situation. Keep it
opt-in per rule via `{"via": "api"}` in the rule's `fix` field, and remember it
sends the prompt plus transcript context to a second endpoint — the only place
in this project where anything leaves the machine.

---

## Phase 4 — did the fix actually work?

Trigger precision answers *"was the rule right?"*. This answers *"did the
injection prevent the correction it was meant to prevent?"* — which is the
question that decides whether a template or an API call was worth it.

Measurable with **no extra instrumentation**, because injections are recorded
in the transcript and therefore in the corpus. For each fire, look forward in
the session: did the user still issue a correction on that subject? If yes, the
injection did not land.

**The gotcha that will bite:** filter on `type == "attachment"` and
`attachment.type == "hook_additional_context"`. Never grep raw text. During the
probe, the sentinel appeared on six transcript lines and only two were the
actual injection — the rest were us *discussing* it. Talking about a rule looks
identical to firing it if you match on text.

This also resolves the counterfactual problem that historical backtesting
cannot: replay tells you a rule *would* have fired, never what would have
happened next. Once the hook is live, every fire has a recorded consequence.

---

## R01 is suspended — its premise was false

The phase-3 loop's first live fires were both R01, both false positives, and
chasing them falsified the rule outright. It now fires and logs but never
surfaces (`suspended: true` in the catalogue, honoured by `action_for`), and no
precision figure was invented to demote it with.

### What `remove` actually means

R01 assumed a `queue-operation` `remove` carrying human content meant a draft
was withheld. It means the message **left the queue** — and delivery is how a
queued message leaves the queue.

| operation | total | human-authored |
| --- | --- | --- |
| `enqueue` | 202 | 54 |
| `remove` | 116 | **41** |
| `dequeue` | 61 | **0** |
| `popAll` | 1 | 1 |

`dequeue` is never used for human content. Both confirmed live deliveries in
session 0825c6b6 emitted `remove`. So `remove` is the normal delivery record for
a queued human message, and the docs give it a second, indistinguishable cause:

> **Take back what you queued** — Press `Up` from the first line of the input
> box to take back the queued messages and commands. Claude Code removes them
> from the queue and puts them in the input box... Edit the text and press
> `Enter` to queue it again, or clear the input box to drop it.

Delivery and take-back produce the same record, with no field separating them.

### Why the check could never have worked

Daniel described the mechanism: *"you are working on something and I have a
message ready to go, or halfway through typing, when you ask a question or
require permission."* The docs confirm the timing — *"if you queue a message
while Claude is running tool calls, Claude Code passes it to Claude as soon as
those tool calls finish, within the same turn."*

A same-turn delivery writes **no `type: user` line**. R01's check was "no similar
user message exists before or after", so it could never observe delivery. "No
counterpart" never implied "not delivered".

### Where the 33/33 came from

`backtest.label()` returned `True` for every R01 fire, reasoning that "the engine
already dropped retractions matching a sent message, so every surviving fire is a
genuine withholding". That is the trigger restated. `BASIS["R01"]` is now
`tautological` and the rule reports **null**, with 33 unclassified fires.

This also dissolves the old `known_limit`: the a0c27fd2 draft that "arrived 11
minutes later reworded" was most likely delivered at the time, and the later
message was Daniel saying it again.

### Discarded discriminators — do not re-derive

- **`queued_command` fields**: byte-identical between a confirmed delivery and
  two claimed withholdings (`commandMode: 'prompt'`, `origin: {'kind':'human'}`).
- **enqueue→remove gap**: predicted bimodal, measured continuous across 39
  removes (3 at ≤1s, 2 at 1–2s, 10 at 2–5s, 20 at 5–30s, 4 beyond).
- **a re-`enqueue` after the `remove`** (the docs' take-back-and-edit path):
  **zero** removes are followed by a similar human enqueue, so take-back-and-edit
  is not what these are.

### To revive R01

Find a positive signal for delivery rather than an absence. The remaining
candidate: a delivery's `remove` should coincide with the turn yielding — next to
an `AskUserQuestion`, a permission prompt, or a tool-call boundary — where a
genuine take-back should not. Until then the rule stays suspended.

## Verified platform facts

Established empirically with a throwaway probe (kept at
`~/.claude-resync/data/probe.py.done`, payload sample beside it). Do not
re-derive these.

**`UserPromptSubmit` payload contains:**
`session_id`, `transcript_path`, `cwd`, `prompt_id`, `permission_mode`,
`hook_event_name`, `prompt`.

**It does NOT contain** `promptSource`, queue state, or `background_tasks`. So
provenance (typed / queued / suggestion_accepted) must be recovered from the
transcript, and task state from the recorder's `events.jsonl`. This is why
`R13` injects nothing at runtime — it has no provenance to report.

**Hook output channels**, both recorded in the transcript:

| channel | lands as | shape |
| --- | --- | --- |
| `hookSpecificOutput.additionalContext` | `attachment.type == "hook_additional_context"` | `content` is a **list**, so several rules can inject in one turn |
| `systemMessage` | `attachment.type == "hook_system_message"` | `content` is a string |

Injected context reaches the model prefixed *"UserPromptSubmit hook additional
context:"* — so it is visibly distinguishable from the user's own words, and an
injection can address the assistant directly without reading as an instruction
from the user.

**Exit 2 blocks the prompt AND ERASES IT.** That is why R04's gate is an
injected directive rather than a hard block: destroying what you typed costs
more than the gate is worth. Escalate to exit 2 only if the soft gate
demonstrably fails.

**Timeout is 30s** for `UserPromptSubmit` (lower than the 600s default
elsewhere). Any API call needs its own cap well inside that, and a fail-open
path.

**Hooks cannot prompt interactively** — they run without a controlling terminal
and cannot open `/dev/tty` or emit escape sequences.

**Task notifications arrive as `attachment.type == "queued_command"`**, with the
`<task-notification>` XML in its `prompt` field. Not as user text.

**`queued_command` is written at ENQUEUE time, not delivery.** Verified: the
draft withdrawn in session `a0c27fd2` at 18:58:59 has an enqueue, a remove, AND
a `queued_command` attachment, all in the same second. Treating these as sent
messages makes every retracted draft look delivered and silently zeroes R01 —
which happened on the first attempt. Delivered human messages already appear as
`type: user` lines, so `queued_command` is only useful for notifications.

**The recursion guard must be precisely scoped** to
`hook_additional_context` and `hook_system_message`. A blanket
"skip all attachments" guard throws away task notifications and makes every
completed background task look unreported.

**`isMeta: True` marks a user-role line the platform generated, not one you
typed.** Skill bodies, tool-companion output and injected notices all arrive as
`type: user` with `role: user` and arbitrary text — nothing in the text itself
identifies them, and `_SYS_PREFIX` only catches the ones that open with an XML
tag. They also carry `sourceToolUseID` and `turnCompanion`.

Ignoring this cost more than it looks. In session 0825c6b6 a single `isMeta`
line — the `claude-api` skill body — held **94,691 of the 96,984 characters**
stored as "your messages": 97.6% of what the similarity rules compare against,
re-tokenised on every prompt, in a cache file re-read and re-written every turn
(103,887 bytes, rebuilt at 5,500 after the fix). Across the corpus 13 of 441
"human-authored messages" were machine-generated, and they were generating
fires: R06 lost 8, R08 lost 7, R05 lost 4, R11 lost 2 — and both of R06's
"confirmed" hindsight labels turned out to be fires on injected documentation,
taking it from 3% to 0%.

`ingest_line` now drops them. Message text is also capped at
`MSG_MAX_CHARS` (8000) and `queue_ops` at `MAX_QUEUE_OPS` (400), because
nothing downstream reads past a few hundred characters (R01 compares 400, R07
compares 600) and both lists otherwise grow for the life of a session.

**Other `attachment.type` values seen:** `deferred_tools_delta`,
`agent_listing_delta`, `mcp_instructions_delta`, `skill_listing`, `auto_mode`,
`total_tokens_reminder`, `date_change`, `command_permissions` (carries
`allowedTools` — potentially useful for R03), `edited_text_file`.

---

## Decisions not to revisit

**`evaluate()` sees only pre-send state.** Not a convention — the future is not
in scope, so a rule cannot consult it. All hindsight lives in the backtest's
labelling. This is what makes historical and runtime numbers comparable.

**Ingestion is shared, not just triggers.** Sharing only the triggers was not
enough: the interceptor and the backtest diverged on notification handling and
only the interceptor was wrong. Both now fold transcript lines through
`rules_engine.ingest_line`.

**Triggers stay deterministic — no LLM in the trigger path.** An LLM deciding
whether a rule fires makes the backtest irreproducible and destroys the whole
precision apparatus. The API is for drafting fixes, never for firing.

**Precision is never invented.** A rule whose fires cannot be labelled reports
`null`. Two figures are published because `confirmed/(confirmed+refuted)`
flatters any rule with many unknowns — it reported a hindsight rule with 31
unknowns out of 32 as "100%". `floor` is the number to trust.

**Dedupe follows the action.** Interrupting rules fire once per *cause* (asking
about the same retracted draft every message is a nag); silent augments fire
every message (re-stating current task state is what makes it current).
Implemented in `rules_engine.dedupes()` and called by both the interceptor and
the backtest. This sat as a written decision with no implementation for a while,
and the symptom was R01 re-firing one withdrawn draft on five consecutive
prompts in session `9d3b23cd` — a decision recorded here is not a decision
running in the code.

**A rule measured as bad never reaches the user.** `PRECISION_FLOOR` in
`rules_engine.py`. The catalogue's own `budget_finding` predicts a service that
asks too often "will be disabled within a day", and R06 at 3.8% precision was
about to become a question on every message naming a path. Unmeasured (`None`)
is not floored — that rule needs to ask to become measured.

**A hand verdict is not a heuristic.** `basis` records which *automatic*
labelling strategy applies to a rule, so `tautological`/`manual`/`none` mean no
heuristic can judge it. The report used to null those rules' precision
unconditionally, which silently discarded hand labels for the six rules that
can only ever be labelled by hand — the verdict was applied per fire and then
thrown away in the aggregate. Basis now nulls precision only when there are no
hand labels. Rules with `basis: manual` were always meant to be judged this
way; `rules.json`'s own `caveat_precision` says "no figure here comes from
hand-labelling yet", and that "yet" needed a tool.

**`rules.json` is canonical in the repo**, deployed by `install.py`, updated by
`backtest.py --write`. `install.py status` reports drift so a stale catalogue is
visible rather than silently in force.

**Everything fails open.** Any error in a hook means the prompt goes through
untouched. A hook that can break a session is worse than no hook.

---

## Loose ends

- **`view.py` was specced and never written.** Named narrow projections over
  `refined/`. Deferred deliberately: the four views that serve the goal are
  `claims`, `undelivered`, `unanswered`, `repair`, and the earlier nine-view
  design was instrumentation rather than findings. Not needed until phase 4.
- **The corpus is unpruned** (14 GB) and staying that way by choice.
  `reduce.py --prune-corpus --session <sid>` works and refuses unless verify
  passes, if that ever changes.
- **`~/.claude-transcripts` is a compat symlink** to `~/.claude-resync`.
  Removable with `rm ~/.claude-transcripts` once you are satisfied.
- **R03 injects a paragraph on the first message of every session.** If that
  reads as noise, gate it on `command_permissions.allowedTools` being
  non-empty — one line in `r03_permission_blocked`.
- **R06 is the rule to fix next.** Its trigger was broadened (added `./` to the
  path pattern), taking it from 40 to 53 historical fires — and its precision is
  3.8%, so the floor now holds it at log-only. It is the highest-volume
  interrupting rule in the catalogue and it currently reaches nobody. Narrowing
  the trigger is worth more than any new rule: the fires are real references,
  the check ("does it resolve") is just too weak to be worth a question.
  Start with `backtest.py --review --rule R06` — 53 fires is an afternoon, and
  it is the only thing that can either raise the figure or prove it deserved.
  (Checked: only 2 of the 53 fire on a compaction summary rather than a typed
  message, so contaminated input is not the explanation. The check is.)
- **No rule addresses the assistant asserting a state its own tool output
  contradicts** — observed twice: *"still running, log ticking"* against a
  3.5-minute-stale log, and *"17 tests, all green"* against a dev server it had
  just broken. That is assistant-side, so an input interceptor cannot catch it.
  Recorded in `rules.json` under `open_gaps`.
