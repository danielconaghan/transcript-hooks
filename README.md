# Context Churn Recorder

A capture tool for Claude Code. Its single job is to record, at a set of
lifecycle events, both an **event record** and the **transcript state** at that
moment, so that the *changes* in context between consecutive captures can be
studied afterward — offline, by separate tooling.

It is **a recorder, nothing more**. It does not analyse, detect, score, flag,
or classify anything, and it defines no churn "categories". Any analysis is a
later, separate job and is explicitly out of scope here.

## Why

Claude Code's working context changes constantly as a session runs — things
enter it and things leave it. Some of what leaves is benign; some might matter.
We don't yet know how to tell those apart, or even what *kinds* of things leave
context. This tool gathers the raw material to characterise context churn by
type. The value is not any single transcript — it is the **delta between one
capture and the next**.

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
  `~/.claude-transcripts/` (`recorder.py` + `corpus/`), and every recorded
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
python3 install.py install
```

Restrict to a single project instead:

```bash
python3 install.py install --project /path/to/project
```

Optionally tune how often compaction fires while collecting data:

```bash
python3 install.py install --window 40000
```

The installer is **non-destructive** (merges into an existing
`settings.json`), **idempotent** (re-running never duplicates entries), and
reports what it registered and where the corpus lives. Confirm registration
in-session with `/hooks`.

### Uninstall

```bash
python3 install.py uninstall                       # global
python3 install.py uninstall --project /path/to/project
```

Removes **only** the recorder's hook entries; leaves the rest of your settings
and the corpus intact.

### Inspect / reset the corpus

```bash
python3 install.py status                       # what's registered + corpus stats
python3 install.py clear  --yes                 # wipe the global corpus in one command
python3 install.py prune  --older-than-days 7 --yes
python3 install.py prune  --keep-sessions 10 --yes
```

`clear` and `prune` are dry-runs until you pass `--yes`, and always act on the
global corpus. `status` accepts `--project` to inspect a project's registration.

## Corpus layout

Everything lands in the global corpus at `~/.claude-transcripts/corpus/`
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

## Reduction (`reduce.py`)

The raw corpus is too large and too redundant to read — each snapshot is
nearly the previous one plus a little. `reduce.py` is an **offline** batch step
that turns it into a small, surveyable **change log** per session, so a human
(or Claude, uploaded) can actually eyeball what enters and leaves context.

It is **reduction, not detection**: it records what left and what entered
context between consecutive captures as plain facts, and decides nothing about
them. No classification, scoring, flagging, or change "categories" — those are
to be discovered later by looking at this output. It only ever reads the
corpus and writes to a separate `refined/` dir; the raw corpus is never
touched.

```bash
python3 reduce.py                      # reduce every session
python3 reduce.py --session <sid>      # just one session
python3 reduce.py --no-preview         # structural only, no verbatim content (safer to share)
python3 reduce.py --skip-existing      # skip sessions already reduced
python3 reduce.py --preview-chars 400  # longer per-unit content previews
```

- **Input** (read-only): `~/.claude-transcripts/corpus`.
- **Output**: `~/.claude-transcripts/refined/<session_id>.change.jsonl` — one
  JSON line per consecutive-capture delta, created `0700` with its own
  `.gitignore` of `*` (it is as secrets-bearing as the corpus).

**The unit grain (the one design choice).** Diffing is done per transcript
*line*, keyed on the line's stable `uuid`. Claude Code already writes one line
per content block — each assistant thinking/text/tool_use block, each
tool_result, each user message is its own line with its own `uuid` — so this
*is* message/content-block grain, and diffing on ids (not text) means a
reworded block keeps its uuid and shows as neither left nor entered. Lines with
no `uuid` (mode / permission-mode / ai-title / last-prompt / file-history) fall
back to a deterministic digest of their content (volatile fields stripped).

**Each delta line** records the two counts, the event types either side (so the
turn / compaction / session *scale* is derivable — it is not labelled), the
units that `left` and that `entered`, unit totals, and the source snapshot
filenames — so any interesting delta is traceable straight back to the full raw
snapshot. Each unit carries its verbatim `type` / `role` / block-types / tool
ids so a survey can be filtered any way you like — the tool itself judges
nothing. Deterministic and re-runnable: re-running over an unchanged corpus
reproduces byte-identical output.

## Files

- `recorder.py` — the hook entrypoint and capture worker (deployed by the
  installer to `~/.claude-transcripts/recorder.py`).
- `install.py` — installer / uninstaller / corpus manager.
- `reduce.py` — the offline change-reduction ETL (corpus → per-session change
  log). Never runs in a hook; read-only on the corpus.

## Out of scope (not built, by design)

Churn detection, classification, or scoring; any definition of churn
categories; re-injection or surfacing of captured data; dashboards or judging
metrics (a plain "what's in the corpus" listing via `status` is fine);
task-event anchoring. The seven events above are the whole surface.
