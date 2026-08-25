# Build record — claude-resync, as of 2026-08-25

A standing record of what this is, what we know, and what we tried that did not
work. Written so the whole thing could be rebuilt from scratch by someone who
was not here, because getting to this point took a lot of measurement and most
of the cost was in the dead ends.

Every figure below was measured, not estimated. Where a figure was later found
wrong, both the wrong one and the correction are kept, because the correction
is usually the more useful fact.

---

## 1. What we are building

Two things that share a corpus.

**The product** is a pre-send interception service. Before a developer's message
reaches the assistant, it evaluates the message plus the state of the session
and may inject a short directive — "you have an unanswered decision", "this
path does not resolve". It exists to reduce wasted effort in collaborative work
between a developer and an AI coding assistant.

**The research** is everything that decides what the product should do. It
works only on stored sessions, never on a live one, and its job is to answer:
what kinds of waste actually happen, how often, and which of them can be
prevented by something said before the assistant acts.

Both rest on a captured corpus of real sessions. Nothing here is theoretical;
if a claim in this document has no number attached, treat it as unverified.

---

## 2. Terminology

Four terms. Use them by name; conflating them has cost real time.

### Preventative action

Something the service does **before** the assistant acts, to decrease the
chance of waste. Fires on a message that might *cause* trouble. Every rule in
`rules.json` is one — 13 of them, R01–R13.

A preventative action **requires a deterministic trigger**. If the judgement
cannot be made deterministically, it is not a rule. Writing a regex as a proxy
for a model judgement is the specific failure this rule exists to prevent:
measured, regexes reached ~25% recall on desync markers, and a regex for
understatement found it 0 times in 448 messages.

### Normalisation layer

Neither predicts nor reports: it **rewrites the input**. Always runs, no
trigger, lives outside `rules.json` so it cannot pollute fire counts or the
precision apparatus.

One instance exists: restating a weakened directive at full force
(`normalise()` in `hooks/intercept.py`). Reached for when a judgement cannot be
made deterministically — a trigger there would be a cost gate wearing a rule's
clothes, with a precision field it could never earn.


### Retrospective marker

The point where an inefficiency becomes visible **to the research layer**.

The divergence happened earlier. The evidence may have entered the record
earlier still. By the time it is visible, the cost is paid.

A marker can **never be a trigger** — intercepting the point where something
became visible prevents nothing. To get a preventative action out of a marker
you must walk *backwards*: waste surfaced at message T, so what was in the
pre-send state at T-n that predicted it? That walk is `backwalk.py`.

Markers vary in how much they can be trusted, and that must be recorded per
marker rather than assumed from the word:

    /ds              the developer asserted it at the time — strongest
    classify.py      a model read a message where the developer complained
    the new views    a model read the record; nobody ever complained

The third tier is the most valuable and the least confirmed. An audit of one
run caught a **fabricated quote in 1 of 19 findings**. Do not let model-asserted
markers into a precision calculation without a provenance field.

Note this definition was sharpened twice. It began as "where a *desync*
surfaced", became "where an *inefficiency* becomes visible" (tying it to the
category taxonomy), and finally named the observer. The observer matters: of 43
cases where an assistant's claim was contradicted by its own tool output, the
evidence was in the record 43 times and the developer said something 14 times.
"Visible" without an observer names two different moments.

### Category of inefficiency

A *kind* of waste. Seventeen of them, in `INEFFICIENCIES.md`, grouped by where
in the collaboration loop the divergence lives.

The relationship between the four terms:

    a CATEGORY is a kind of waste that can happen
    a MARKER is one instance of it, visible to us
    a PREVENTATIVE ACTION is an attempt to stop that category
    a NORMALISATION LAYER is an attempt that cannot be deterministically gated

---

---

# PART ONE — THE RESEARCH LAYER

How we learn what is worth acting on. Three stages: **collection** turns live
sessions into a lossless archive, **conversion** turns that archive into views
shaped for a question, **analysis** puts a judgement on a view.

The stages are separated because they have different costs and different
failure modes, and each arrow is owned by exactly one kind of script:

    corpus  -> refined     reduce.py                    lossless, expensive
                                                        to redo, must never
                                                        lose anything
    refined -> view        pairs/chat/actions/evidence  deterministic, free to
                                                        redo, must never be a
                                                        shared dependency
    view    -> verdict     judge.py                     costs money, stored so
                                                        it is never bought twice

A view script cannot spend money — it imports no API client. `judge.py` is the
only script that can, and it reads a view **from disk** rather than
regenerating it, so the bytes that were judged are the bytes on the file and an
audit can check a quote against exactly what was sent. A script that rebuilds
its own payload at judgement time can drift from the file it wrote, and then
the stored view is decoration rather than evidence.

## 3. The data path

    ~/.claude-resync/
      corpus/     raw capture              14.268 GB
      refined/    lossless archive         87.1 MB epochs + 32.4 MB events
      views/      deterministic views      23.8 MB
      pairs/ chat/ ...                     verdicts, paid for

### 3.1 COLLECTION — capture (`hooks/recorder.py`)

Hooks fire on seven events and snapshot the transcript file plus a verbatim
payload. Deliberately over-captures: "so nothing is lost to a field we did not
think to extract".

### 3.2 COLLECTION — lossless reduction (`research/reduce.py`)

14.268 GB across 51 sessions reduces to ~69 MB of epoch bases (0.482%), with
events logs kept verbatim. About **140x, nothing dropped**, and `--verify`
proves it by round-tripping every capture against a sha256 of the original.

The trick: the transcript file is append-only, so a snapshot is a **byte
prefix** of every later snapshot *in the same epoch*. Store the largest
snapshot per epoch; record every other capture as a byte length into it.

**Epochs exist because compaction breaks that property.** At a compaction the
transcript and the context diverge permanently: in the one observed case 42,449
tokens became 9,114, with 5 of 46 messages preserved. Worse, the divergence is
not fully derivable — `compactMetadata.preservedMessages.allUuids` listed 5
uuids and only 4 resolved to a line in the file. So the boundary is stored,
not modelled. Any capture that fails to byte-prefix its base is stored in full.

Corpus today: **56 sessions**, 2 of which have more than one epoch.

### 3.3 CONVERSION — the reader (`research/session.py`)

A **library, not a pipeline stage**. Each analysis script reads `refined/` for
itself so they can diverge. The whole corpus parses in about **one second**, so
there is no performance case for an intermediate file.

What it shares is only **format facts** — how the transcript is shaped — never
filtering policy. Each of these was a bug before it was a fact:

- `isMeta` marks a platform-generated user-role line. One such line in session
  0825c6b6 held 94,691 of the 96,984 characters stored as "your messages" —
  **97.6%** — and was being re-tokenised on every prompt.
- An AskUserQuestion result is **prose**, not structure: `"Q"="A", "Q2"="A2"`.
  `events.jsonl` carries a parsed dict but only for 18 of the corpus's 31 uses;
  the transcript has all 31.
- A slash command arrives as a user-role line opening `<command-name>`, with
  its argument in `<command-args>`. `/ds` must be recovered *before* the
  system-prefix filter drops it, and its `-N` stripped exactly as
  `intercept.py` strips it or one marker counts twice.
- A denial's reason is `toolDenialKind`. Of **132 denials, only 29 are
  `user-rejected`** — 73 are `permission-rule` (a static allowlist) and 30 are
  `automode-*`. Counting the rest reports your settings file as a symptom.
- A compaction summary is a genuine user-role line that `isMeta` does not
  catch; it must be recognised by its opening words.

**Policy is the caller's.** `events()` emits everything, tagged, and each
script decides. `classify.py` deliberately *keeps* compaction summaries because
whether a model rejects them tests the prompt; `chat.py` must drop them. A
shared filter would have taken that choice away silently.

**Coordinate system.** Every record carries `seq`, `turn`, `epoch`, `uuid`. The
uuid is the point: a derived view is a *lens*, not a copy, so you can truncate
aggressively when the original is one lookup away. The previous generation of
files carried none of these, and "we can always go back to refined" was a hope
rather than a mechanism.

What the corpus contains, via this reader:

    developer         562        assistant_text   3702
    tool_use         7696        tool_result      7693
    question           31        denial            132

### 3.4 CONVERSION — deterministic views (`views/`)

Each view reshapes `refined/` for a set of categories. They are the input to a
judgement, made cheap enough to read by eye and to send to a model.

    views/pairs      56 files   2,217 candidate pairs   15.2 MB
    views/chat       56 files     528 exchanges          3.5 MB
    views/actions    56 files   7,823 rows               4.9 MB
    views/evidence   56 files      43 candidates         0.2 MB

**Views are write-only. Nothing reads them.** That is the entire difference
from the retired `friction/`, which two scripts depended on — and so its
text-only shape silently blocked eight of the seventeen categories.

**Views live apart from verdicts** because the two have different lifetimes. A
view regenerates for free whenever extraction changes; a verdict cost money and
must survive. Separate trees mean a re-extraction can never overwrite something
expensive.

### 3.5 ANALYSIS — a judgement on a view (`research/judge.py`)

A model reads a view and returns structured findings. Every stored row carries
`ts`, `model`, `prompt_version`, `prompt_sha`, `pipeline_sha`, the expanded
parameters, token `usage`, a costed `cost_usd`, and the **raw response** — so a
later schema change is a re-parse rather than a re-purchase.

---

### 3.6 One engine, four views

`pairs.py` and `chat.py` each grew their own API client, cache, prompt
versioning, cost arithmetic, quote audit and integrity check — the same six
things, twice, diverging. `evidence.py` had a prompt and no engine at all;
`actions.py` needed one for C4 and had nothing. Every fix to the caching or the
audit had to be made twice, and once was missed.

A view module now exposes six things and nothing else:

    PROMPT_VERSION   bumped by hand when the wording changes MEANING, so a
                     typo fix costs nothing; each version's text is stored
    SYSTEM, SCHEMA   the prompt and the response shape
    MODE             "session" (one call for the whole session, response is an
                     array) or "unit" (one call per unit)
    unit_key(u)      identity of one unit
    rows_from(...)   turn a response into verdict rows

**The view stamps its own `unit_key` at build time**, and `unit_key()` reads it
rather than recomputing. This was found the hard way: `chat.py`'s key hashed
untruncated turn text while the view stored it truncated at 6,000 chars, so any
exchange longer than that hashed differently when read back — 3 of 30 in the
first judged session. Recomputing a key from stored text is a whole class of
bug; carrying the key removes it.

Storage mirrors the split:

    views/<view>/<session>.jsonl      free to regenerate
    verdicts/<view>/<session>.jsonl   paid for, must survive

Cache is per **unit**, not per session, so adding a session or widening a
view's cap re-judges only what is genuinely new. It is indexed by
`(unit_key, model, prompt_version)` — the model is part of the index because
without it, running a second model silently returns the first one's verdicts.

## 4. The seventeen categories

Full definitions and evidence in `INEFFICIENCIES.md`. Summary:

    A  Intake            A1 instruction dropped · A2 misread · A3 guessed instead of asking
    B  World-model       B1 unverified premise · B2 stale fact · B3 post-compaction rework
    C  Execution         C1 costly self-correction · C2 repeated failing action
                         C3 scope overrun · C4 work undone
    D  Reporting back    D1 illegible progress · D2 asserted vs own tool output
                         D3 not actionable
    E  Turn-taking       E1 asked what it could determine · E2 did not ask when it should
                         E3 developer repeated themselves · E4 fix did not land, re-reported

**Two axes matter more than the categories.**

*Did it cost anything.* A self-correction caught before it propagated is the
system working. Only 2 of `pairs.py`'s 4 findings carried rework evidence. A
detector without a cost field reports healthy behaviour as failure.

*Did anyone notice.* This splits the list in two and decides what can see it.
The developer-noticed half leaves a correction and `classify.py` finds it — 122
markers. The unnoticed half leaves nothing in the developer's messages and is
invisible to every instrument built before 2026-08-24. **A1, B3, C2, D2 and E1
live there.**

**Deliberately excluded, so the list has edges:** harness friction (73
`permission-rule` denials — time lost to configuration, not collaboration) and
wall-clock idle (69 gaps of 10+ minutes, 186 hours, but nothing in the
timestamps separates "blocked" from "asleep").

---

## 5. The instruments

    hooks/recorder.py     capture
    hooks/intercept.py    runs preventative actions; hosts the normalisation
                          layer; records /ds markers
    rules_engine.py       evaluates preventative actions (shared with replay)
    rules.json            the catalogue of preventative actions

    research/reduce.py    corpus -> refined, lossless
    research/session.py   refined -> tagged records (library)
    research/plaintext.py  markdown stripper for model payloads

    research/backtest.py  precision of each preventative action
    research/gaps.py      signals no preventative action responds to
    research/backwalk.py  marker -> candidate preventative action
    research/classify.py  detects retrospective markers
    research/weakened.py  one tested hypothesis (negative result)

    research/pairs.py     C1 B1        claim vs later claim
    research/chat.py      A1 A2 C3 D1 D3 E1 E2   exchange: request vs reply
    research/actions.py   C2 C4 B3     the tool-call log
    research/evidence.py  D2           claim vs its own tool output
    research/judge.py     -            the only script that spends money

Coverage: **14 of 17 categories** have an instrument. A3, E3, E4 do not.

Model pass status:

    pairs.py      wired, run       $1.29, 105 verdicts
    chat.py       wired, run       $1.15, 19 findings on one session
    actions.py    not wired        C2/B3 need no model; C4's 126 candidates do
    evidence.py   not wired        43 candidates ready

Total spent to date: **$2.44**.

---

---

# PART TWO — FROM FINDINGS TO INTERVENTIONS

Everything in Part One produces *findings*. This part is what turns a finding
into something the product does. It is the point of the project and the least
finished part of it.

## 11. The walk from a marker to a preventative action

A marker can never be a trigger. Intercepting the message that reports a
failure prevents nothing — by then the cost is paid. So the only route from a
finding to a rule is **backwards**: waste surfaced at message T, so what was in
the pre-send state at T-n that predicted it?

`backwalk.py` does that walk. Its results so far are the most important
negative result in the project.

### What the walk found

**Mostly nothing.** On 11 located markers, the clearest trace was:

    T   : please fix manifest.local.json still isn't gitignored ... still
          reports a pure reorder as drift
    T-1 : commit this              fired: -  state: -
    T-2 : yes, fix both comments   fired: -  state: -
    T-3 : yes, sync and re-run     fired: -  state: -

The developer approved two fixes, said commit, then had to report both still
broken. **Nothing fired because there was nothing to fire on** — the evidence
that the fixes had not landed was in the assistant's own tool output, not in
anything the developer said. No preventative action on the INPUT side can catch
that. This is the single strongest argument in the corpus for checking an
assertion against its own tool result, which is category D2.

### Why the candidates died

Every candidate signal died on its **base rate**, measured against all 448
messages:

    denial          54.8% before a marker vs 72.3% overall   0.76x
    open-question   28.6% vs 32.8%                           0.87x
    queue-remove    19.0% vs 31.9%                           0.60x

All **below 1.0** — less common before a marker than in general. `denial`
looked compelling at 23 of 42 until the base rate showed it is present nearly
everywhere.

The lesson generalises: a signal that appears before most markers is worthless
if it also appears before everything else. **Always divide by the base rate.**
A raw count before markers is not evidence, and it is the easiest mistake to
make because the number looks large.

### What this means today

Nothing in the current pre-send state predicts waste. A new preventative action
cannot be built from the state as it stands — which is why the research moved
to finding *categories* first. You cannot write a trigger for something you
have not characterised.

## 12. Choosing between a preventative action and a normalisation

Once a category is characterised, there is one test:

> **Can the judgement be made deterministically from the pre-send state?**
> Yes → a preventative action, with a trigger, in `rules.json`, carrying a
> precision figure. No → a normalisation layer, always-on, outside
> `rules.json`, carrying no precision figure because it could never earn one.

Do not write a regex trigger as a proxy for a model judgement. Measured, that
route reaches ~25% recall on markers, and a regex for understatement found it
**0 times in 448 messages**. A rule with a trigger it cannot honour is worse
than no rule: it accrues fire counts and a precision figure that mean nothing.

The one existing normalisation — restating a weakened directive at full force —
exists precisely because "is this directive weakened?" could not be decided
deterministically. Note that the *hypothesis behind it* was later tested and
not supported (see 15.7), which is a live question about whether it should
remain.

## 13. Measuring what ships

`backtest.py` replays every developer message through `rules_engine` and reports
per-rule precision. Two figures, and the difference matters:

    floor      confirmed / all fires. Counts every unlabelled fire as
               unconfirmed. Conservative, and the number to trust.
    labelled   confirmed / (confirmed + refuted). Ignores unknowns, so it
               flatters any rule with many — it reported a rule with 31
               unknowns out of 32 as "100%".

**Every rule reports how its fires were judged**, because precision is only as
good as its label:

    auto          a fact in the data, independent of the trigger
    auto-proxy    a weak stand-in, flagged as such
    hindsight     from what the developer said later — UNDER-counts, because a
                  correction worded without shared vocabulary is missed
    tautological  the label would restate the trigger, so precision is
                  undefined and reported as null
    manual        fires are real, labels need a human
    none          not labellable at all
    user          the developer's own verdicts, which override every heuristic

This vocabulary is the model for how marker provenance should work too. A
marker from `/ds` and a marker asserted by a model reading the record are not
the same kind of evidence, and the word "marker" alone does not say which.

### The honest state of the catalogue

Only three rules have trustworthy (`auto`) evidence — R07, R10, R12 — with a
**combined n of 12**. Six are unmeasurable by construction. R01 is suspended.
R09 has never fired. The catalogue is 13 rules and about one rule's worth of
dependable measurement.

## 14. What the analysis layer has not yet produced

No preventative action has yet been written from the new category work. That is
the gap this whole structure exists to close, and it is worth being explicit
that it is still open.

The most promising route is D2, because it is the one case where the backwalk
identified a concrete mechanism: the assistant asserts a state its own tool
output contradicts. It cannot be an input-side preventative action — the
evidence is on the assistant's side — so it would be either a post-tool-use
check or a normalisation. That decision has not been made.

---

# PART THREE — WHAT WE LEARNED THE HARD WAY

## 15. What we tried that did not work

This is the expensive part of the record.

### 15.1 Naming an instrument for its ambition

The first analysis tool was called `ledger.py` — "a contradiction ledger". It
got read, including by its author, as a general desync finder. It is not: a
contradiction ledger asks *"was a stated fact later shown false?"*, and only
two of seventeen categories have that shape.

Renamed `pairs.py`, for its method. **Name an instrument for what it does, not
what you hope it finds.**

### 15.2 Analysing the transcript as claims rather than as a conversation

The single most expensive mistake. `pairs.py` paired raw assistant text blocks
against each other and produced findings that were **98% the assistant talking
to itself**. Four findings across three sessions, every one a self-correction,
zero developer-caught, in sessions holding seven known markers.

The cause was the **unit**, not the prompt:

    RAW      : 3702 assistant text blocks vs 528 developer messages   7.0 : 1
    AS TURNS :  527 assistant turns       vs 528 developer turns      1.00 : 1

Collapsed into turns, the transcript is a **perfectly alternating two-party
chat**. The 7:1 is the harness rendering interstitial narration ("Let me check
X") as separate blocks; a person does not send those. Pairing on blocks shredded
one turn into seven and compared the fragments.

Turn-level is also six times cheaper: 867k tokens against 5.19M.

The asymmetry is itself a finding, and free: assistant turns run **10.7x** the
developer's word count (median 462 vs 19); the median reply is 23x the words of
the request, p90 80x, **max 1106x**.

### 15.3 A shared extraction file

`friction.py` extracted "communicative events" to `friction/<session>.jsonl`,
and both analysis scripts read it. Three problems:

- It was **text-only**, and as a shared *dependency* every consumer inherited
  that limit. Eight of seventeen categories were unreachable while it was the
  input — it dropped all 7,696 tool calls and all successful results.
- There was **no performance case** for it. One second to parse the corpus.
- Its scoring half — `clean`/`minor`/`sideways` and six signals — was **read by
  nothing**. `pairs.py` assigned the header to a variable and never used it.

Replaced by `session.py` as a library. The lesson: share the *facts*, not a
*file*; a file everything depends on constrains everything.

### 15.4 Filters tuned by intuition rather than measurement

Four cases, all the same shape — a plausible threshold that turned out to be
measuring the wrong thing:

**Pair rarity.** A document-frequency cap of 25% of events let a term appear in
93 of a session's 375 events and still count. Result: 4,364 candidate pairs,
98.6% of them discarded by a cap. A shortlist that drops 98.6% of itself is a
lottery. An absolute cap of 3 gives 328.

**Requiring two shared terms** looked right on volume (3,048 pairs down to
1,324) and would have missed the motivating case: "case-scoped" and
"client-scoped" share exactly one term, `scoped`. Rarity discriminates; count
does not.

**C2, repeated failing action**, measured three ways:

    consecutive failures, any action     9 runs   <- a much weaker claim
    same signature repeated 3+ times   226 runs   <- ordinary iteration
    ...of which 2+ failed                1 run    <- the real thing

Nine sounds like a finding until you notice the failures are different
commands. 226 sounds alarming until you notice most succeed.

**D2 evidence scoping.** Whole-turn scoping finds 43 candidates but attributes
11 failures to more than one claim. Scoping to "since the previous statement"
gives 7 with no over-attribution but loses every summary claim. Resolution: keep
the wider net and **label** each result with which window it fell in.

### 15.5 Trusting a token heuristic

`chars/4` is a prose rule of thumb. This payload is JSON dense with timestamps,
identifiers and code, and runs at about **2.1 chars per token**. The heuristic
understated a real 47,049-token call by **1.9x**, and a corpus estimate by 2x
($12.06 quoted, $21.07 actual). Use the token-counting endpoint for anything
you will pay for.

Also: output tokens are not in an input estimate. A findings-heavy `chat.py`
session cost $1.15 against a $0.79 input estimate — **~50% more**.

### 15.6 Assuming the cheap model would do

`pairs.py` on session 2ac71e1f, identical 62-candidate input:

    claude-haiku-4-5   0 contradictions
    claude-opus-5      3 contradictions, 100% verbatim quotes

The session visibly contained a self-correction. On this task the cheap model
is a false-negative machine; a "tune on Haiku, keep Opus" workflow would have
concluded the prompt was broken.

### 15.7 A hypothesis that was properly tested and failed

`weakened.py`: do hedged or interrogative directives get under-weighted and
cause desyncs? Result **NOT SUPPORTED** — weakened directives are followed by a
desync slightly *less* often than direct ones, consistently across three
lookahead windows (RR 0.83x, 0.94x, 0.86x). The phenomenon is real and common
(82 of 377 directives) and simply does not predict.

Kept because a negative result stops the same idea being re-proposed. Note that
a conclusion on this project has reversed once on sample size, so it is worth
re-running as n grows.

### 15.8 A hypothesis I proposed and then disproved within the hour

When `chat.py` missed 5 of 8 markers on a session, I proposed building E3/E4
(developer repeats themselves) to close the gap. Then measured it: repeat
detection finds 2 repeats in that session and **neither is one of the misses**.
The misses are fresh symptom reports and corrections — B2 territory. E3/E4
would have caught none of them.

### 15.9 Counting rows in an append-only file

`desync.jsonl` holds **662 rows over 541 distinct messages**, because a Haiku
pilot judged 40 of them before the Opus pass. I counted rows and reported
**161 markers across 27 sessions**. The true figure is **122 across 26**, and
every per-kind count was inflated with it.

The code was always right — `backtest.load_markers()` keys by message. The
document quoting it was not.

Worse: 41 messages hold more than one verdict and **13 of those disagree**.
Last-write-wins resolved to Opus for all 541, but only because the Opus pass
ran last; a future pilot re-run would have silently taken over the labels.
`load_markers()` now names the keeper model rather than trusting run order.

### 15.10 An installer that did not install everything

`install.py` lived in `hooks/` and deployed four files: two hook entrypoints,
`rules_engine.py` and `rules.json`. It never deployed `commands/ds.md`.

So `/ds` worked only where someone had copied it by hand. A fresh install got
the hooks and the catalogue and **no way to record a retrospective marker** —
the one kind of evidence the corpus cannot reconstruct for itself, and the only
one that captures what the right answer was.

Two structural mistakes behind it. The installer was filed under `hooks/`,
which made it look like a hook rather than the thing that installs the hooks,
so nobody read it as owning the commands. And slash commands were tracked in
the repo, referenced in the documentation, and connected to nothing.

Moved to the repo root, `COMMAND_FILES` added beside `RUNTIME_FILES`, and
deployed into the same scope the hooks were registered in — a `--project`
install must not put `/ds` into every unrelated project, where it would write
markers into a corpus that is not recording.

The general lesson: **a file tracked in the repo and referenced in the docs is
not thereby installed.** Check the deploy list against the docs, not against
what is on your own machine.

### 15.11 Regexes as a proxy for judgement

`gaps.py`'s six hand-written regexes for desync signals: both `scope-creep`
hits were the phrase "out of scope" inside a pasted spec, and two `correction`
hits were the platform's own compaction summary. Measured recall ~25%.

A regex over a message cannot answer *"did the user have to correct us?"*,
because that is a question about the **previous turn**. It can only spot
vocabulary.

---

## 16. Traps that cost an hour each

- **`lst[-0:]` is the whole list in Python.** A lookback parameter of 0 silently
  meant "every preceding claim" — the opposite of off.
- **Sorting epoch files by size** tagged every shared line with the larger
  file's epoch and deduped the rest away. Every session looked single-epoch and
  B3 was undetectable by construction. A line belongs to the **earliest** epoch
  it appears in.
- **The model re-decorates its own quotes.** Sent `The file is tracked`, it
  returns `The file is **tracked**` — markdown it was never shown. An audit
  that does not strip both sides fails sound findings. Observed twice.
- **The model elides with `...`.** A quote stitched from two real fragments is
  checkable but not contiguous. Worth its own audit category, not a failure.
- **A stripped payload must be audited against stripped text**, or the
  pipeline blames the model for its own formatting.
- **Cache keys must include the model**, or comparing two models silently
  returns the first one's verdicts from cache.
- **Idempotency needs a fixed point, not a careful pass.** `strip_decoration`
  applies rules until stable; a single ordered pass failed on real text 55
  times because stripping emphasis *reveals* new line-level structure
  (`**1. \`admin-api\` was...**`).
- **Stripping code delimiters makes idempotency impossible.** Once backticks
  are gone, a second pass cannot tell code from prose: a diff line `- old`
  loses its `-`, `# comment` loses its hash, JSON indentation collapses.
  Backticks are 1.73% of text (~$0.04 across the corpus). Idempotency is worth
  more than four cents.
- **zsh does not word-split unquoted `$var`.** Use `${=var}` in loops, or every
  smoke test reports FAIL.

---

## 17. Measurements worth keeping

    corpus reduction              140x, verified by round-trip
    parse whole corpus            ~1 second
    speech vs actions             3,702 text blocks vs 7,696 tool calls
    tool output vs text           3x the volume (one session: 72,627 vs 26,138)
    turns                         527 assistant : 528 developer  (1.00:1)
    word asymmetry                10.7x per turn; median reply 23x the request
    claims that assert an outcome 430 of 3,702  (12%)
    markers                       122 across 26 sessions
    AskUserQuestion               31 uses, 8 non-answered, 2 answered off-menu
    denials                       132 total, 29 user-rejected
    developer messages with >1 request   82 of 461  (18%)
    chars per token, this payload ~2.1

---

## 18. Open questions

1. **D2's model pass is unwired** — 43 candidates, ~94k tokens, $0.47 at Opus.
2. **C4's 126 candidates are all `needs_review`** — deterministic detection
   cannot separate waste from an intended change of mind.
3. **A3, E3, E4 have no view.** E3/E4 are deterministic and cheap; A3 overlaps
   B1 heavily and may not need its own.
4. **`chat.py` recall is 38%** against `classify.py` markers on the one session
   run. It is aimed at seven categories `classify.py` does not cover, and 11 of
   its 19 findings were never raised by the developer — so low recall against a
   different instrument may be correct rather than a fault. Undecided.
5. **Marker identity is message-shaped** (`msg_key` = hash of a developer
   message). A C2 marker is a run of retries, a D2 marker is a claim, a C3
   marker is an exchange. If markers can be any event, the schema and
   `backtest.later_correction`'s join both change.
6. **One marker file or two?** One lets `backtest.py` label rule fires against
   everything, which is the point of the apparatus. Two stops model-asserted
   findings contaminating the precision figures the shipping catalogue rests
   on. A fabricated quote has already slipped an audit once.

---

## 19. Working principles, earned rather than assumed

- Measure before tuning. Every threshold in this project that was set by
  intuition was wrong, and the measurement usually took ten minutes.
- Report the funnel, not the final number. "43 candidates" means nothing; "430
  → 171 → 104 → 43, and here is what each step removed" is reviewable.
- Name what an instrument tests, and let it say what it does not test.
- A negative result is a deliverable. Write it down or it gets re-proposed.
- Store the input to a judgement, not just the judgement.
- Separate extraction from judgement, so a prompt change costs a re-judge and
  not a re-derivation.
- Quote provenance with every figure, and date it. A stale number that reads as
  current is worse than no number.
