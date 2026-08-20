# claude-resync

Catches the moments where you and Claude Code drift apart, and stops the same
drift happening again.

A session goes wrong in a specific way: you believe one thing, the assistant
believes another, and neither of you notices for hours. This project records
every session, works out from the record where those divergences happened and
what they cost, turns each one into a **rule**, and then checks every message
you send against those rules *before* Claude sees it — attaching the missing
fact, or asking you the question that would have saved the afternoon.

Three parts, in the order they run:

| part | what it does | where it runs |
| --- | --- | --- |
| **capture** (`hooks/recorder.py`) | records an event record plus the full transcript state at every lifecycle event | inside your session, on every hook |
| **research** (`research/`) | reduces the corpus losslessly, then replays the rules over history to measure how often each one would fire and how often it would be right | by hand, offline |
| **interception** (`hooks/intercept.py`) | evaluates the rules against the message you just submitted and injects what the assistant is missing | inside your session, on every prompt |

The loop closes because interception is itself captured: every injection is
recorded, so the next round of research can ask whether the fix actually
prevented the correction it was meant to prevent.

## Why

Measured on 51 recorded sessions, the expensive failures were not subtle.
A decision question raised nine minutes into a session was dismissed and
resolved correctly **five hours and forty-seven minutes later**, after the wrong
thing had been built in between. Thirty-three messages were typed, queued, and
withdrawn without ever being sent — including three unreported CORS errors and
a browser choice that would have explained a whole afternoon of failures.
Seventy-four percent of background tasks finished without notifying, so the
assistant reported progress it could not see.

None of that needed cleverness to catch. It needed something looking.

## The three delta scales

Captures are minted with a monotonic per-session sequence (`count`), so diffing
any two captures is uniform: pick two captures, diff their transcript states.
The events chosen give deltas at three deliberate scales:

| Scale       | Between                     | Shows                                  |
|-------------|-----------------------------|----------------------------------------|
| Turn        | `Stop` → `Stop`             | what changed over one assistant turn   |
| Compaction  | `PreCompact` → `PostCompact`| what the compaction rewrite changed    |
| Session     | `SessionStart` → `SessionEnd`| what changed across a whole session   |

## Events captured

`SessionStart`, `PreCompact`, `PostCompact`, `SessionEnd`, `Stop`,
`PostToolUse`, and `PostToolUseFailure` (a failed tool call is a distinct kind
of churn). All seven event names and their firing/blocking behaviour were
confirmed against the installed build, **Claude Code 2.1.218**. (The hook
command uses `$HOME`, expanded by the shell, to locate the globally-deployed
recorder.)

## Where data is stored vs. which sessions are recorded

These are two independent things:

- **Storage is always global.** The recorder and its corpus live in one place,
  `~/.claude-resync/` (deployed scripts + `corpus/`), and every recorded
  session writes into that single shared corpus regardless of which project
  triggered it.
- **Recording scope** is which sessions fire the hooks, set by where the hooks
  are registered:
  - **default → global** (`~/.claude/settings.json`): records **every** Claude
    Code session on the machine, across all projects.
  - **`--project PATH`** (`PATH/.claude/settings.json`): records only that
    project's sessions.

## Install

Global (the default — records all sessions on the machine):

```bash
python3 hooks/install.py install
```

Restrict to a single project instead:

```bash
python3 hooks/install.py install --project /path/to/project
```

Optionally tune how often compaction fires while collecting data:

```bash
python3 hooks/install.py install --window 40000
```

The installer is **non-destructive** (merges into an existing
`settings.json`), **idempotent** (re-running never duplicates entries), and
reports what it registered and where the corpus lives. Confirm registration
in-session with `/hooks`.

### Uninstall

```bash
python3 hooks/install.py uninstall                       # global
python3 hooks/install.py uninstall --project /path/to/project
```

Removes **only** the recorder's hook entries; leaves the rest of your settings
and the corpus intact.

### Inspect / reset the corpus

```bash
python3 hooks/install.py status                       # hooks, catalogue drift, sdk/.env, corpus
python3 hooks/install.py clear  --yes                 # wipe the global corpus in one command
python3 hooks/install.py prune  --older-than-days 7 --yes
python3 hooks/install.py prune  --keep-sessions 10 --yes
```

`clear` and `prune` are dry-runs until you pass `--yes`, and always act on the
global corpus. `status` accepts `--project` to inspect a project's registration.

## Corpus layout

Everything lands in the global corpus at `~/.claude-resync/corpus/`
(created `0700`, with a self-contained `.gitignore` of `*` so it is never
committed). Per session:

### `<session_id>.events.jsonl` — append-only event log

One JSON line per capture, in capture order. Each line:

| Field             | Meaning                                                        |
|-------------------|----------------------------------------------------------------|
| `count`           | **Primary key** — monotonic per-session sequence minted by the recorder. Always present, unique, ordered. The definitive capture order. |
| `session_id`      | the session                                                    |
| `event`           | one of the seven event types                                   |
| `ts`              | payload timestamp if present (may be `null`)                   |
| `captured_at`     | the recorder's own reliable in-record UTC timestamp            |
| `prompt_id`       | per-turn id if present (absent on startup/resume SessionStart) |
| `tool_use_id`     | `toolu_…` on the PostToolUse family only                       |
| `hook_event_name` | as reported in the payload                                     |
| `snapshot`        | convenience filename of the transcript state (join is `count`) |
| `snapshot_bytes`  | size captured (`0` if the source did not exist yet)            |
| `payload`         | the **full raw payload** — nothing dropped                     |

### `<session_id>.<count>.transcript.jsonl` — transcript state

One file per capture, a verbatim byte copy of the transcript at that moment — a
self-contained full-state snapshot (not append-only, not a growing log). Named
**only** by `session_id` + `count`; what the capture *was* lives inside the
event record, not in the filename.

### Join & order

- **Join** an event record to its transcript file on `session_id` + `count`.
- **Order** by `count`. Never rely on parallel-hook firing order or filesystem
  timestamps for ordering.
- **Time** from the in-record `ts` / `captured_at` (they survive the corpus
  being copied/moved/synced). File mtimes are a convenience for casual
  "nearest snapshot to this time" lookups only — contents are ground truth.

## Design guarantees

- **Capture only.** No analysis, detection, scoring, flagging, categories, or
  re-injection anywhere.
- **Non-blocking by default.** Every event except `PreCompact` forks a detached
  worker and returns immediately, so capture never adds latency to the session.
  This is the most important operational property. (`Stop`, `PreCompact`, and
  others can *block* the session on a non-zero exit — hence the guarantees
  below matter.)
- **The one synchronous exception:** `PreCompact` snapshots the transcript
  *before* compaction rewrites it in place. If it returned first, the pre-state
  would be lost — so its capture is inline (blocking), kept minimal, still
  exits 0.
- **Fail-open, always exit 0.** Every failure is swallowed to
  `corpus/errors.log`. Two guards: the shell-level `|| true` in the hook command
  (covers interpreter-launch failure) and a top-level `try/except` in
  `recorder.py` (covers everything after).
- **`count` is owned by the recorder.** Payloads carry no sequence number; the
  recorder mints one under a per-session file lock (`flock`), so concurrent
  detached workers (e.g. parallel tool calls) serialize and never collide.
- **No transcript-schema dependence.** The transcript is captured by copying
  bytes and is never parsed. Everything structured comes from the hook payload,
  the stable source of truth. If the `.jsonl` schema changes, capture is
  unaffected.

## Secrets

Transcript snapshots and full tool output routinely contain file contents,
commands, and credentials. **The corpus is a secrets-bearing store:**
local-only, gitignored, `0700`, never synced. Treat anything that passes
through it as exposed. If you ever share a corpus, strip secrets first.

## Compaction window (`--window`)

Optional collection-tuning knob. Writes `CLAUDE_CODE_AUTO_COMPACT_WINDOW` into
the target project's `.claude/settings.json` `env` block (merged, not
clobbered). Guards:

- The effective trigger is `min(floor(window·pct/100), window − 13000)`. A
  window at/below ~13000 collapses it to zero and compaction loops forever, so
  values ≤ 13000 are rejected, values < 20000 are rejected as too close, and
  20000–30000 warns. 30000+ is comfortable.
- Warns if `.claude/settings.local.json` sets a window/pct override that would
  silently shadow what's written.
- Env vars bind at session start — a running session must be restarted to pick
  up a new window.

Omit `--window` to leave the project's compaction behaviour untouched.

## Reduction (`research/reduce.py`)

The raw corpus is enormous and almost entirely redundant: each snapshot is the
previous one plus a few lines. `reduce.py` collapses that **losslessly** by
exploiting the property that makes it redundant — the transcript is append-only,
so every snapshot is a byte *prefix* of every later snapshot in the same epoch.
Store one base per epoch plus a byte length per capture and any snapshot is
`head -c` away.

Measured on this corpus: **14.3 GB → 102 MB, about 140x, nothing dropped.**
`--verify --against-corpus` round-trips every capture against a sha256 taken
from the original file, which is what makes deleting the corpus a decision
rather than a gamble.

```bash
python3 research/reduce.py                              # reduce every session
python3 research/reduce.py --session <sid>              # one session
python3 research/reduce.py --verify --against-corpus    # prove it round-trips
python3 research/reduce.py --prune-corpus --session <s> # delete verified snapshots
```

**Epochs and compaction.** The file is append-only *within an epoch*, and an
epoch ends at compaction. That boundary is stored, not modelled: `PreCompact` is
its epoch's base, captures up to the matching `PostCompact` are held in full,
and the prefix property is never asserted across the gap. This matters because
the file and the context diverge there permanently — in the observed case 42,449
tokens became 9,114, with 41 of 46 lines staying in the file and leaving the
context — and because `preservedMessages.allUuids` cannot rebuild the surviving
set anyway (4 of 5 uuids resolved).

**`events.jsonl` is copied verbatim** because it is the complement to the
transcript, not a duplicate: it alone holds `background_tasks[].status`,
`agent_type` for subagents the transcript has no lines for at all,
`compact_summary`, `custom_instructions`, and raw `tool_response`.

## Rules (`rules.json`)

The catalogue, and the actual deliverable. One record per rule: what triggers
it, what to check, what to do (`augment` / `ask` / `block` / `resend` /
`annotate`), the evidence rows that justify it with session and timestamp, the
measured cost, and the precision measured by backtest.

Actions are split deliberately by what they cost you. `augment` attaches facts
silently and can run at any fire rate; `ask` and `block` spend your attention
and are gated on measured precision. Across the corpus, 21% of messages trigger
something but only ~1.4% would ask a question — a service that asks on a fifth
of your messages gets switched off in a day.

## Backtest (`research/backtest.py`)

Replays every historical message through the same engine the interceptor runs,
then judges each fire. Every rule declares *how* it was judged, because a
precision figure is only as good as its label: `auto` (a fact in the data),
`hindsight` (the user's own later correction, which under-counts),
`tautological` (the label would restate the trigger, so precision is undefined
and reported as null), `manual`, or `none`. Your verdicts from
`data/labels.jsonl` override all of them.

Two figures are reported and the difference matters — `floor` counts unlabelled
fires as unconfirmed, `labelled` ignores them and flatters any rule with many
unknowns.

```bash
python3 research/backtest.py                 # the table
python3 research/backtest.py --rule R01      # one rule, listing its fires
python3 research/backtest.py --write         # fold precision into rules.json
```

## Gap analysis (`research/gaps.py`)

`backtest.py` answers *"are the rules we have right?"*. This answers the other
half — *"what are we missing?"* — and it is where a new rule should come from,
rather than from memory.

It cross-references every historical message against families of *desync
signal* (a correction, a re-ask, a question about whether anything is
happening) and reports the messages where something clearly went wrong and no
rule responded.

Two adjustments keep the number honest. `R13` is excluded, since it fires on
every message and would report total coverage of everything. And **content-blind
fires are counted separately**: `R03` fires on a session's first message
whatever it says, and `R05`/`R06`/`R08` on any mention of an endpoint, path or
URL — a correction that happens to name a file is not a correction the catalogue
understood. On the 50-session corpus that distinction takes corrections from an
apparent 63% coverage to a real 37%.

```bash
python3 research/gaps.py                     # coverage table + samples
python3 research/gaps.py --signal state-ask  # one family, every message
```

Everything in it is deterministic — no model reads the corpus. A model is useful
for the *last* step only: reading the twenty or thirty uncovered messages it
prints and proposing a trigger. That sample fits in a context window; the corpus
does not. The irreducibly manual part is the `SIGNALS` dict itself, so a pattern
nobody thought of stays invisible; treat a new family as a hypothesis to add
there, not a rule to ship.

## Interception (`hooks/intercept.py`)

A `UserPromptSubmit` hook. Rebuilds the pre-send state, evaluates the rules,
logs every fire to `data/fires.jsonl`, and injects context for the rules that
never interrupt you. Measured latency **median 18 ms, p95 47 ms**; state is read incrementally
using a byte offset, so a long session costs no more than a short one.

Currently phase 3. Silent rules inject every turn; interrupting rules inject
once per *cause* and then record your verdict. A rule measured below 30%
precision never reaches you, and a rule whose premise turns out to be false can
be `suspended` — it keeps firing and logging, but never surfaces, so its counts
stay comparable without anyone inventing a figure to demote it with.

Fixes come from the `fix` template in `rules.json`, or are drafted by the API
when the wording needs reading the situation rather than restating it. The
template is always the fallback, so enabling the API can improve an injection
but never remove one. Credentials go in `~/.claude-resync/.env` (a hook does not
inherit your shell's exports) and the SDK lives in `~/.claude-resync/.venv`,
both created by `install.py`.

`CLAUDE_RESYNC_PHASE=2` reverts to silent-only; `CLAUDE_RESYNC_API=0` stops
every outbound call. See `PLAN.md` for the verified platform facts, phase 4, and
what is known to be wrong.

```bash
python3 hooks/intercept.py --status                  # routing, fires, verdicts, api state
python3 hooks/intercept.py --dry-run "some prompt"   # evaluate without recording
python3 hooks/intercept.py --recent --todo           # fires with no verdict yet
python3 hooks/intercept.py --label R06 --key K --verdict applies --note "..."
python3 hooks/intercept.py --verdict "no"            # how a reply would parse
```

Three paths write `data/labels.jsonl`, each tagged with its `source`:
`assistant-classified` (the assistant reads your answer and records it),
`verdict-reply` (a leading yes/no parsed by the hook — a fallback, since only
~5% of real replies parse), and `manual-review` (`backtest.py --review`, the
only route for silent rules and the only way a demoted rule earns its way
back).

## Shared engine (`rules_engine.py`)

The triggers and the transcript ingestion, imported by both the backtest and
the interceptor. It sits at the repo root because both sides depend on it and
neither owns it.

`evaluate()` accepts a `PreSendState` and nothing else, and that state contains
only what was knowable before the message was sent — so a rule *cannot* consult
the future, not by discipline but because the future is not in scope. All
hindsight lives in the backtest's labelling. That is what makes the historical
and runtime numbers comparable.

Sharing the ingestion, not just the triggers, was learned the hard way: the
first interceptor discarded every `attachment` record as a recursion guard,
which silently threw away task notifications (they arrive as
`attachment.type == "queued_command"`) and made every completed background task
look unreported — while the backtest still reported the old number.

## Layout

```
claude-resync/
  rules.json           the catalogue — hand-edited, git-tracked, canonical
  rules_engine.py      triggers + transcript ingestion, shared by both sides
  hooks/               runs inside your session: must fail open, must be fast
    install.py           installer / uninstaller / corpus manager
    recorder.py          capture hook entrypoint
    intercept.py         pre-send interception hook entrypoint
  research/            run by hand: may be slow, may crash
    reduce.py            lossless corpus reduction + verification
    backtest.py          replay the rules over history, measure precision
    gaps.py              find desync signals no rule responds to
```

The split is not cosmetic. Anything under `hooks/` runs on every prompt or tool
call and must never raise, never block, and never be slow — the same constraint
`recorder.py` was built to. Anything under `research/` is invoked deliberately
and is free to take ninety seconds or fall over.

Everything installed lives in one global home, so the runtime has no dependency
on where this repo sits:

```
~/.claude-resync/
  recorder.py  intercept.py  rules_engine.py  rules.json   # deployed copies
  corpus/              raw captures
  refined/             lossless reduction
  data/                fires.jsonl, labels.jsonl, error logs
  intercept-cache/     per-session incremental state
  .venv/               the anthropic SDK, for API-drafted fixes
  .env                 ANTHROPIC_API_KEY (0600), never in the repo
```

`rules.json` is the one file that flows both ways: canonical in the repo,
updated there by `backtest.py --write`, deployed by `install.py`. `install.py
status` reports when the deployed copy has drifted, so a stale catalogue is
visible rather than silently in force.

**Naming.** The project was `claude-transcripts` when it only captured. The old
`~/.claude-resync` path is still honoured, and `$CLAUDE_TRANSCRIPTS_HOME`
still works alongside `$CLAUDE_RESYNC_HOME`, so an install predating the rename
keeps recording into its existing corpus instead of silently starting an empty
one.

## Boundaries

The project now does detect, score, and re-inject — that is the point of it.
But the constraints are per-component, and they are what keep it trustworthy:

**`hooks/recorder.py` still only records.** No analysis, no classification, no
opinions. It copies bytes and never parses the transcript's internal schema, so
a change to that schema cannot break capture.

**`research/reduce.py` still only reduces.** Verbatim facts and plain
arithmetic over them — sizes, counts, elapsed times. A six-minute
`gap_seconds` is not labelled a stall, and a background task with no
notification is not labelled a failure. Interpreting those patterns is a
detector's job, so that changing a heuristic never means re-reducing 14 GB.

**`rules_engine.py` cannot see the future.** `evaluate()` takes only pre-send
state. Hindsight belongs to the backtest.

**No precision is ever invented.** A rule whose fires cannot be labelled from
the data reports `null`, not a guess. Four rules currently do.

**Nothing leaves the machine.** The interceptor probes only hosts in
`/etc/hosts` that map to loopback or a private range, and never resolves or
contacts a public host. API-drafted fixes are opt-in per rule and not yet
enabled.

**No dashboards, no metrics service, no multi-user.** This is one person's
instrument for one person's sessions.
