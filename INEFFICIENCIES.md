# Categories of collaborative inefficiency

The definitive list of what this project is trying to detect. Read it at the
start of a session before building or changing a detector, and amend it when
the corpus says something new.

It exists because the first `pairs.py` run was built against a definition held in
one person's head at one moment, and measured one category out of seventeen while
appearing to measure all of them. A written taxonomy is what makes "does this
instrument test what we think it tests" an answerable question.

**A desync is an inefficiency in collaborative work between the developer and
the assistant.** Not an error, not a bug, not a disagreement — wasted effort
caused by the two parties operating on different understandings.

---

## The two axes that matter more than the categories

Decide both for any candidate detector before writing it. They determine
whether a category is worth measuring and whether it *can* be.

### 1. Did it cost anything

A category is only an inefficiency if it cost time, money or rework. This is a
filter, not a description, and it is easy to skip:

> "the assistant re-correcting itself, **but later having cost additional time
> or money**" — Daniel, 2026-08-24

A self-correction caught before it propagated is the system working, not waste.
Measured: only **2 of `pairs.py`'s 4 findings** carry `rework_evidence`. A
detector without a cost field reports healthy behaviour as failure.

### 2. Did anyone notice

This splits the whole list in two and decides what instrument can see it.

| | |
|---|---|
| **Developer noticed** | They wrote a correction. `classify.py` can find it — 122 markers, the well-covered half. |
| **Nobody noticed** | No correction exists, so there is nothing to find in the developer's messages. Invisible to every instrument built so far. |

**A1, B3, C2, D2 and E1 live in the second half.** That is where the real
inefficiency hides, and it is why a detector aimed at the first half can look
successful while telling you nothing new.

---

## A. Intake — what was asked vs what was understood

| Id | Category | Evidence in the corpus | Measured today |
|---|---|---|---|
| **A1** | **Instruction dropped** — part of the message never acted on, noticed or not | 82 of 461 substantial developer messages (**18%**) carry more than one request | **nothing** |
| A2 | **Misread** — acted, but on a different reading of the ask | `misread` × 5 | `classify.py`, only when the developer complained |
| A3 | **Guessed instead of asking** on genuine ambiguity | R09 `unverified-premise` is defined and has never fired | nothing |

A1 is the largest known blind spot. Nothing in this project has ever looked for
it, and by definition it leaves no trace in the developer's messages when it
goes unnoticed.

## B. The assistant's model of the world

| Id | Category | Evidence | Measured today |
|---|---|---|---|
| B1 | **Acted on an unverified premise** | `2ac71e1f`: a `believed` root cause, with a live refresh run against prod on top of it | `pairs.py`, partially |
| B2 | **Working from a stale fact** | `correction` × 67 — **the largest single bucket** | `classify.py`; R05 |
| B3 | **Post-compaction rework** — redone because context was lost | one observed compaction took 42,449 tokens to 9,114, preserving 5 of 46 messages | nothing |

## C. Execution

| Id | Category | Evidence | Measured today |
|---|---|---|---|
| C1 | **Costly self-correction** — corrected itself after spending something | 2 of 4 `pairs.py` findings carry rework | **`pairs.py` — this is its category** |
| C2 | **Repeated failing action** — same approach retried into the same wall | **1** run, once the signature is required to match (`tabs_context_mcp` 4x, 3 failing). An earlier figure of 8-9 runs counted *any* 3 consecutive failures, which is a different and much weaker claim — different commands failing in a row is not retrying into a wall. 226 runs repeat a signature 3+ times but mostly succeed, which is ordinary iteration | `session.py` computes it deterministically; **no detector uses it yet** |
| C3 | **Scope overrun** — did more than was wanted | `scope` × 10 | `classify.py`, only when the developer complained |
| C4 | **Work undone** — output actively reversed | `undo` × 1 | `classify.py` |

C1 must be split on cost. A free self-correction does not belong here.

## D. Reporting back

| Id | Category | Evidence | Measured today |
|---|---|---|---|
| D1 | **Illegible progress** — the developer cannot tell what is happening | `state-question` × 10; `rules_engine.RE_STATE_QUESTION` is written and wired to nothing | half-built (the R14 candidate) |
| D2 | **Asserted a state its own tool output contradicts** | *"17 tests, all green"* against a dev server it had just broken; *"still running, log ticking"* against a 3.5-minute-stale log | **nothing** — recorded as `rules.json`'s largest open gap |
| D3 | **Answer not actionable** — right content, wrong altitude or no acceptance criterion | assistant turns run **10.7x** the developer's word count (median 462 vs 19); the median reply is **23x** the words of the request, p90 80x, max 1106x. (An earlier 15.6x/678x was measured through `friction.py`, which truncated text at 4,000 chars — 1.0% of events, but the long tail) | R10; `chat.py --metrics` computes the ratios free |

## E. Turn-taking

| Id | Category | Evidence | Measured today |
|---|---|---|---|
| E1 | **Asked what it could have determined itself** | an off-menu answer reading *"I would like you to continue working without interactions with me until you are able to fulfil the brief"* | nothing |
| E2 | **Did not ask when it should have** | `a0c27fd2`: AskUserQuestion rejected → *"I'll make the calls myself and flag the assumptions"* → the same message re-sent 19 seconds later | nothing |
| E3 | **The developer had to repeat themselves** | 12 near-duplicate consecutive re-sends across 5 sessions | R07 |
| E4 | **A fix did not land and the symptom was re-reported** | `re-report` × 29 | `classify.py` |

---

## Relationships worth remembering

**A1 and C3 are one measurement in opposite directions.** Map the distinct
requests in a developer message against the work that followed:

* a request with no matching work → **A1, instruction dropped**
* work with no matching request → **C3, scope overrun**

One instrument, two readings. Neither is a contradiction task, which is why
`pairs.py` cannot reach either. `chat.py` is built for them.

**B1 and C1 are the same failure at different stages.** B1 is asserting without
checking; C1 is the correction that follows. B1 is the preventable half.

---

## Deliberately excluded, so the list has edges

**Harness friction.** 73 `permission-rule` denials and 19 `automode-blocked`
events in the corpus. Real time lost, but to configuration rather than to
collaboration — counting it reports the settings file as a symptom. Only
`user-rejected` denials (29) reflect a human decision.

**Wall-clock idle.** 69 gaps of 10+ minutes between the assistant finishing and
the developer replying, 186 hours in total. Measurable and meaningless: nothing
in the timestamps distinguishes "blocked and waiting" from "asleep".

---

## What each instrument actually covers

Keep this honest. The failure mode this document exists to prevent is an
instrument being credited with a category it does not test.

| Instrument | Covers | Does **not** cover |
|---|---|---|
| `classify.py` | B2, C3, C4, D1, E4, A2 — the *developer noticed* half | anything unnoticed |
| `session.py` | nothing — it is the reader. Format facts from `refined/`, everything tagged, nothing dropped: speech, tool calls, results, denials, questions, markers, epochs | any policy; each script filters for itself |
| `pairs.py` | **C1 and B1** — assistant-side reasoning failures, by pairing claims and asking whether one contradicts the other | the developer↔assistant exchange — see below |
| `chat.py` | **A1, A2, C3, D1, D3, E1, E2** — the exchange-shaped categories, one turn-pair at a time | B-group, C1, C2: not exchange-shaped |
| `/ds` | anything, but only what the developer noticed *and* bothered to record | the unnoticed |
| `backwalk.py` | walks back from a marker to find a preventable signal | needs a marker to start from |

**On `pairs.py` specifically** (named `ledger.py` until 2026-08-24; renamed
because "ledger" read as a general desync finder and it is not one): it is a
keeper. Across 4 sessions and 118
pairs it produced 4 findings, **all** assistant self-corrections and **zero**
developer-caught, in sessions containing 7 known markers. A contradiction
Pair-based testing asks *"was a stated fact later shown false?"*, and only C1
and B1 have
that shape, so keep it pointed there.

But the cause of that result was the **unit**, not the approach, and the
distinction matters or we will wrongly conclude that exchange-level analysis
cannot work. It paired raw assistant text blocks. Measured:

    RAW      : 3702 assistant text blocks vs 528 developer messages   7.0 : 1
    AS TURNS :  527 assistant turns       vs 528 developer turns      1.00 : 1

Collapsed into turns the transcript is a **perfectly alternating two-party
chat**. The 7:1 is the harness rendering interstitial narration as separate
blocks; a person does not send those. Pairing on blocks shredded one turn into
seven and compared the fragments, which is why 97.9% of candidate pairs were
assistant-to-assistant. `chat.py` uses the turn as its unit for exactly this
reason, and `pairs.py` is named for its method so nobody mistakes its scope
again.

---

## Amending this document

Add a category only with corpus evidence, and record the evidence inline with
the figure so a later reader can tell a measurement from an assumption. When a
figure is superseded, replace it and note the date — a stale number that reads
as current is worse than no number.

The counts here were measured against 56 sessions in `refined/` (46 with
candidate pairs), **122 desync markers across 26 sessions**, and 543 developer
messages. `classify.py` marker kinds:

```
correction 67   re-report 29   scope 10   state-question 10   misread 5   undo 1
```

**Re-baselined 2026-08-25.** The earlier figures (161 markers, 27 sessions)
counted ROWS in `desync.jsonl`, which is append-only: 662 rows cover 541
distinct messages, because a Haiku pilot judged 40 of them before the Opus
pass. 41 messages hold more than one verdict and **13 of those disagree**, so
choosing a row is a real decision. `backtest.load_markers()` was always right —
it keys by message — but this document quoted the raw row counts. Marker totals
are now taken per message from the keeper model.

Open questions, unresolved:

* Whether D2 can be detected at all without feeding tool results to a model, and
  whether that is affordable.
* Whether A1 needs a model or can be approximated deterministically from
  enumerated requests.
* Whether B3 is separable from ordinary forgetting, given the transcript cannot
  fully reconstruct a post-compaction context set.

---

## Where things stand — 2026-08-24

### The pipeline

    reduce.py     corpus/  -> refined/            lossless, 140x, verified
    session.py    refined/ -> tagged records      format facts; drops nothing
    plaintext.py  strips markdown                 lossy, OUTBOUND EDGE ONLY
    pairs.py      C1, B1                          claim pairs
    chat.py       A1 A2 C3 D1 D3 E1 E2            exchanges, with actions
    actions.py    C2, C4, B3                      action log

`session.py` is a library, not a stage: every script reads `refined/` for
itself so they can diverge. The only thing shared is *format facts* — how the
transcript is shaped — never filtering policy. `classify.py` deliberately keeps
compaction summaries that `chat.py` must drop, and a shared filter would have
taken that choice away silently.

### Two directories, different lifetimes

    <resync_home>/views/{pairs,chat,actions}/<sid>.jsonl   deterministic input
    <resync_home>/{pairs,chat}/<sid>.jsonl                 verdicts, paid for

A view regenerates for free when extraction changes; a verdict cost money and
must survive. Separate trees mean a re-extraction can never overwrite something
expensive. **Views are write-only — nothing reads them.** That is the whole
difference from the retired `friction/`, which two scripts depended on, so its
text-only shape silently blocked eight categories.

Current: 2,217 candidate pairs, 528 exchanges, 7,696 actions. All 56 sessions.

### Coverage

    covered by an instrument   A1 A2 B1 B3 C1 C2 C3 C4 D1 D3 E1 E2   (12)
    no instrument yet          A3 B2 D2 E3 E4                        ( 5)

`B2` is arguably `pairs.py` already — a claim contradicted later is its shape.

### Spent so far

$1.29 of Opus across 4 runs / 105 verdicts, all on `pairs.py`, all before the
`session.py` rewire — so `--verify` reports them as from a superseded pipeline.
They still cache-hit, because `pair_key` is content-addressed and extraction
came out byte-identical.

Unrun, awaiting a decision: `chat.py --all` at **$13.37**, `pairs.py --all` at
**$21-26** (counted, not estimated — `--count-tokens` asks the API).

### Pick up here

1. **D2** is the biggest gap and the most valuable — the assistant asserting a
   state its own tool output contradicts. Needs an `evidence.py`: a claim
   paired with the tool results from its own turn. `session.evidence_before()`
   already exists; the open question is how much result body to carry (one
   session held 72,627 chars of tool output against 26,138 of text).
2. **E3/E4** are homeless since `friction.py` went — near-duplicate developer
   turns at a short window (E3) and a long one (E4). Fully deterministic, an
   hour's work, no model.
3. **C4's 126 candidates** are all `needs_review`. They need a model pass to
   separate waste from an intended change of mind.
4. **`chat.py` has never been run.** One small session is ~$0.05 and would say
   whether the exchange view finds what `pairs.py` could not.

### Things that cost time to learn — do not relearn them

* `lst[-0:]` is the whole list in Python. A lookback of 0 silently meant
  "every preceding claim".
* Sorting epoch files by size tagged every line with the larger file's epoch
  and deduped the rest away, so every session looked single-epoch and B3 was
  undetectable by construction. A line belongs to the EARLIEST epoch it
  appears in.
* The model re-decorates its own quotes with markdown it was never shown.
  `--audit` strips both sides before comparing; a raw compare fails sound
  findings.
* Estimating tokens at chars/4 understated a real call by 1.9x. This payload
  runs at ~2.1 chars/token. Use `--count-tokens` for anything you will pay for.
* zsh does not word-split unquoted `$var`; use `${=var}` in loops.

