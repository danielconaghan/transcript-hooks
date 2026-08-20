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
| 3. asking + labels | **done, live** | dedupe by cause, two-turn verdict capture into `data/labels.jsonl`, a precision floor, and an ask budget. API-drafted fixes deliberately not built — see below. |
| 4. fix-efficacy measurement | **not started** | see below |

**The phase-2 soak was skipped, knowingly.** The original gate here was "let
phase 2 run a few days, then compare live fire rates against `rules.json`".
Phase 3 was switched on with 9 real prompts of live data instead. Two things
substitute for the soak, and both need watching rather than trusting:

- a **precision floor** stops a rule the backtest has *already* measured as bad
  from ever reaching you (this is what R06 at 3.8% would otherwise have done)
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

### Not built: API-drafted fixes

Only R09 carries `{"via": "api"}`, and R09 returns no fires by design, so the
API path would have been unreachable code guarding the one place anything
leaves this machine. A fire whose template renders nothing is recorded as
`suppressed: "no-template"` and never marked as asked. Build this when a rule
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
| `augment` | R02 R03 R05 R08 R12 R13 | silent, every turn, no dedupe |
| `apply` | R01 R04 | injects its fix, asks nothing; the label comes from whether you object |
| `ask` | R07 R09 R10 R11 | injects a directive, verdict recorded |
| `log` | R06 | fires and is recorded, never surfaced |

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
- **No rule addresses the assistant asserting a state its own tool output
  contradicts** — observed twice: *"still running, log ticking"* against a
  3.5-minute-stale log, and *"17 tests, all green"* against a dev server it had
  just broken. That is assistant-side, so an input interceptor cannot catch it.
  Recorded in `rules.json` under `open_gaps`.
