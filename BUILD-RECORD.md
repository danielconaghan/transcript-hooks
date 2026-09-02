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

### 3.4 CONVERSION — the session document (`research/views.py`)

One view, one unit: the session. `refined/` becomes one readable document per
session — every developer message, assistant turn, action, result, question,
denial and marker, in order.

    views/session    56 files   19,799 records   ~6.46M tokens
                                                 median session ~58,000

**Records are not selected. Only the SIZE of a tool result is bounded**, and
that is marked where it bites. This is the opposite of every predecessor, and
deliberately so — see 3.6.

The header carries a mechanical index: compaction boundaries, repeated commands
with their outcomes, rewritten targets, silent gaps of five minutes or more,
and the developer's own markers. These **annotate** the records; they do not
filter them. That distinction is the whole lesson of the rewrite. Annotation is
safe. Selection was not.

The index is not decoration. On `fecca80a` the `silent_gaps` entry
(96 → 97, 587 seconds; 97 → 98, 557 seconds) independently pins the same moment
the model found by reading — for free, in the header, before a call is made.

**Views are write-only. Nothing reads them but `judge.py`.** That is the entire difference
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

### 3.6 Why the unit is the session

Three shapes were tried. The first cut the transcript into claim pairs and
exchanges; the second cut it per category into five views. Both were
*selections*, and this is what selection cost:

**Every defect five independent checkers found was in the selection logic, not
in the judging.** Results paired to the wrong command by walking back for the
nearest call of the same tool — 430 of 7,693, 5.6% mislabelled. Commands cut at
the first `&&`, so 950 of 5,537 Bash actions collapsed to a bare `cd <path>`,
one label covering 82 unrelated commands. Developer messages truncated
head-first with the ask sitting at the end. Successes carrying no output at
all, which reduced "did it claim something its evidence contradicts" to "did it
lie about a failure".

Each was fixable. The fault underneath was not. Markers said the recorded
sessions' inefficiency was **55% B2 and 24% E4** — both cross-turn. Findings came back
**39% B1 and 20% C1** — both per-turn. **79% of what the developer actually
complained about produced one finding**, because no unit smaller than a session
can watch a fact go stale or a fix fail to land.

The measurement that settled it, on `fecca80a`: five findings across **four
different categories** — B1, D1 twice, E2, E3 — all carrying a `began_seq` of
95–97. They are not five problems. They are one: a blocking test suite launched
against an unverified environment and piped to `tail`, killed by the developer
ten minutes later, then asked about twice. Under five views those land in four
separate files, each looking like a modest standalone finding, and nothing in
that architecture can see they share an origin.

**The origin is the only thing a preventative action can fire on.** That is why
`began_seq` is the field the prompt calls the most valuable in the output, and
why `judge.py --origins` exists: it groups findings that trace to one cause.

The price is about 2x — ~$41 of input across all 56 sessions, against ~$45
all-in for the five views.

**The view stamps its own `unit_key` at build time**, and `unit_key()` reads it
rather than recomputing. This was found the hard way: `chat.py`'s key hashed
untruncated turn text while the view stored it truncated at 6,000 chars, so any
exchange longer than that hashed differently when read back — 3 of 30 in the
first judged session. Recomputing a key from stored text is a whole class of
bug; carrying the key removes it.

Storage mirrors the split:

    views/session/<session>.jsonl     free to regenerate
    verdicts/session/<session>.jsonl  paid for, must survive

Cache is indexed by `(unit_key, stage, model, prompt_version)`. The **model is
part of the index** because without it, running a second model silently returns
the first one's verdicts — exactly the comparison you would be running it to
make. **`stage` is part of it** so that a failure in pass two does not discard
the pass-one verdict already bought.

### 3.7 Two passes, because sixteen will not compile

The forcing device — a required verdict on every category, none optional — is
what stopped A1 being skipped, where a free findings list let it fire zero times
in 33 units. Asking for all sixteen at session scale ran into a wall that has
nothing to do with the session:

    findings + searched_how + a flag     4 categories max
    findings alone (what is used here)   6 categories max
    a flat list of sequence numbers     12 categories max
    boolean + searched_how              16 categories max

Measured against the API, not guessed. Beyond those counts the request is
rejected outright — *"the compiled grammar is too large"*. **Sixteen categories
each carrying findings is not expressible in one call at any session size.**
That the five-view split sat at 3–4 categories each was luck, not design.

So the forcing device runs first and alone over all sixteen, and detail is
bought afterwards, six at a time, only where triage said something is there.
Worst case is four calls per session; a clean session costs one.

It survives the move. On `fecca80a` all sixteen `searched_how` came back naming
actual sequence numbers, and **eleven of sixteen returned absent** — it compels
a look without manufacturing a finding to justify the look. One caveat stands:
`sufficient_evidence` was offered sixteen times and never once returned false,
so whether that honesty valve works is still unknown.

The document is the bulk of every call and is sent identically, so it goes as a
**cached block** and later passes pay a tenth for it. The system prompt is the
same bytes in both passes for the same reason — caching matches on a prefix of
the whole request, so a pass-specific system prompt would miss the cache on the
very call the cache exists for.

## 4. The sixteen categories

Grouped by where in the collaboration loop the divergence lives. Evidence and
amendment history in `INEFFICIENCIES.md`; these are the definitions.

**A — Intake: what was asked vs what was understood**

    A1  Instruction dropped     A distinct request, question or constraint in
                                the developer's message that the reply never
                                addresses. Noticed or not. An ABSENCE, which is
                                what makes it hard to detect.
    A2  Misread                 Acted, but on a different reading of the ask.
                                Something was done; not the thing wanted.

**B — The assistant's model of the world**

    B1  Acted on an unverified  Asserted something as settled fact with no
        premise                 evidence gathered, then built on it.
    B2  Working from a stale    The world changed — a moved endpoint, a renamed
        fact                    field, a merged branch — and the assistant is
                                still operating on the old one.
    B3  Post-compaction rework  Work redone because context was lost at a
                                compaction, not because anything changed.

**C — Execution**

    C2  Repeated failing        The same approach retried into the same wall.
        action                  Requires a normalised signature to detect;
                                otherwise it is a judgement call.
    C3  Scope overrun           Did substantial work nobody asked for and
                                nobody would obviously want.
    C4  Work undone             Output written and then reversed.

**C1 was filed with the B group rather than with C**, back when the unit was
smaller than a session: a costly self-correction needs the claim, the
correction, and the work between them all in one unit, and the execution view
carried claims only as 220-character summaries — enough to see that a
correction happened, not enough to judge whether it cost anything, which is the
entire test. The session document made the question moot. It is worth keeping
because the *shape* of the problem recurs: whenever a category needs two things
that a unit boundary separates, the boundary is the bug.

**D — Reporting back**

    D1  Illegible progress      After reading the reply, a reasonable developer
                                still cannot say what state things are in.
    D2  Asserted a state its    "17 tests, all green" against a dev server it
        own tool output         had just broken. The evidence was on screen and
        contradicts             nobody was reading it.
    D3  Not actionable          The content is right but pitched at the wrong
                                altitude, burying the answer, or ending with no
                                clear result or next step.

**E — Turn-taking**

    E1  Asked what it could     Put a question to the developer that it could
        have determined         have answered from the repo, the files or a
                                command. A round trip for nothing.
    E2  Did not ask when it     The converse. Guessed on something that
        should have             warranted a question.
    E3  The developer had to    The same message sent twice because the first
        repeat themselves       one did not land.
    E4  A fix did not land and  The symptom comes back. Evidence that a change
        the symptom was         reported as done was not done, or not enough.
        re-reported

**Two axes matter more than the categories.**

*Did it cost anything.* A self-correction caught before it propagated is the
system working. Only 2 of `pairs.py`'s 4 findings carried rework evidence. A
detector without a cost field reports healthy behaviour as failure.

*Did anyone notice.* This splits the list in two and decides what can see it.
The developer-noticed half leaves a correction and `classify.py` finds it — 122
markers. The unnoticed half leaves nothing in the developer's messages and is
invisible to every instrument built before 2026-08-24. **A1, B3, C2, D2 and E1
live there.**

### Two changes made on 2026-08-25, and why

**A3 was retired into E2.** They were the same category written twice:

    A3  Guessed instead of asking   Committed to a consequential choice that
                                    was genuinely ambiguous, without asking and
                                    without flagging the assumption.
    E2  Did not ask when it         The converse. Guessed on something that
        should have                 warranted a question.

Nothing caught it while the list was only a list. It surfaced the moment two
different views were given prompts for both — A3 in `intake-understood` and E2
in `turn-taking` would have fired on the same event, in two views, and produced
one problem reported as two findings that appeared to corroborate each other.

E2 kept it because its view carries the evidence: the AskUserQuestion outcomes,
and whether the developer picked an offered option or typed past them.

**C1 moved from `execution` to `assistants-view`**, for the reason given above:
a category belongs where its evidence is best, not where its group letter says.
That is the same mistake in miniature as the one that produced the previous set
of views — arranging by convenient shape and letting the categories fall where
they land.

`assistants-view` units gained a `work_after_it` field to make the move real:
the actions taken between a claim and the next thing said. Without it the view
could see the correction but not the cost, and the cost IS the finding.

### Do the views match the categories?

No, and the mismatch is concentrated where the evidence is heaviest. Coverage
at three levels of strictness, because "covered" quietly meant "claimed" for
most of the build:

All sixteen are now judged on the same unit — the whole session — and each is a
REQUIRED field in the triage schema, so none can be skipped. `judge.py
--coverage` prints the live table; there is no hand-maintained copy here,
because the last one recorded intentions and read as results.

The reasoning below describes the PREVIOUS sets of views and is kept because it
is why the unit is now the session.

**9 demonstrated, 4 with a view that has never produced a finding, 4 with no
view at all.** `in schema` matters: where it says `asserted`, the view has no
category field, so its coverage is a claim in a docstring rather than something
the model was made to attribute.

Two of the never-fired have excuses — `D2` has not been run, and `B3` is
unmeasurable on this corpus (2 sessions have a compaction, holding 5 actions
between them). Two do not. **`A1` is in `chat.py`'s schema and did not fire once
in 30 exchanges**, despite being the category the taxonomy calls the largest
blind spot. `B1` was asserted for `pairs.py` and never demonstrated; all its
findings were self-corrections.

Now map the corpus onto the same list. The 122 markers, by category:

    B2   67  (55%)  NO VIEW
    E4   29  (24%)  NO VIEW
    C3   10  ( 8%)
    D1   10  ( 8%)
    A2    5  ( 4%)
    C4    1  ( 1%)

**96 of 122 markers — 79% — are in categories with no view.**

The cause is an ordering mistake worth naming. The views were built around
shapes that were convenient — a claim pair, an exchange, an action log — and
then categories were mapped onto whichever shape looked closest. That is
backwards. Take a category, ask what evidence would settle it, build the view
that carries that evidence. Seven categories covered because seven happened to
fit one shape is not coverage, it is convenience.

B2 and E4 are both cross-turn and fit neither the exchange nor the claim-pair
shape, which is exactly why they were skipped, and exactly why they dominate.

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


## 5A. Ground truth: the planted fixtures (`mock-refined/`, `mock-views/`)

Everything the analysis layer reports is a model judging a model, checked by a
model. The one external anchor — the developer's own `/ds` markers, 122 of them
by the build record's own count — currently reads as **zero across all 56
sessions**, and no `markers.jsonl` survives. Until that is settled there is no
independent signal in the system at all.

So the ground truth is manufactured instead, and it costs nothing to make.

    research/scenarios.py    48 sessions, written by hand
    research/mockrefined.py  emits them as refined, then builds views
    mock-refined/            the fixtures, in transcript form
    mock-views/              the documents, built by the SHIPPING code
    mock-views/ANSWERS.json  what each one contains, written before judging

**One issue per mock, and no others.** Sixteen categories, two that must fire
and one control that must not. Isolation is the whole design: with a single
fault per fixture, any other category reported is a false positive *by
construction*, and nobody has to adjudicate — in particular I do not get to
decide whether an extra finding was "really" a second fault after seeing the
judge's answer.

The first version did not hold that line. 43 of 48 carried extra faults, mostly
my own writing habits — B1 leaked into ~35 because I kept writing "The cache is
unbounded. Fixing it.", E2 into ~25 because assistants acted without flagging.
Scoring counted every one against the judge, and "81% off-target" turned out to
be measuring the fixture. The discipline that fixed it belongs at authoring
time, not at scoring time:

> **The assistant behaves correctly in every respect except the planted one.**

**They enter at refined, not at view.** The first generator emitted finished
documents, which tested the judge and nothing else. The worst defect in this
project lived upstream of that: `signature()` truncating commands at 160
characters, unmarked, corrupting 56.4% of Bash in the real corpus. No fixture
could have caught it, because none ran through the code that produced it.
Entering at refined puts them through `session.py` and `views.py` — so epochs
are derived from two real files rather than asserted, commands survive whole,
and the documents cannot drift from the shape that ships. Verified: 48 of 48
byte-identical to `views.build()`.

Three properties held on purpose, because the real corpus has them:
`began_seq` is never `at_seq` (real median gap: 10 records, p90 106); controls
wear the surface shape of their category and are still correct behaviour; and
each fixture records a `distractor` — something that looks like a finding and
is not.

### What the fixtures measured

**Caveat, found by auditing this section rather than by trusting it.** These
numbers were taken before the fixtures were rebuilt through `mock-refined/`.
45 of the 48 documents are byte-identical afterwards, but **three are not** —
all three E1 mocks, because an AskUserQuestion now round-trips through
`session.py`'s question/answer parsing instead of being written directly, which
changes the record count and therefore the `unit_key`. So the figures below
hold for 45 of 48 and the E1 column specifically should be re-measured. Cost to
settle: $0.89.

Haiku, v3 prompt, all sixteen categories in one triage call:

    plants found            27 / 32
    controls stayed quiet   14 / 16
    quotes verbatim         97%
    false-positive share    81%

Two hypotheses about that 81% were tested and **both were wrong**. Splitting
triage into five calls, one per group, made it *worse* — 27 plants, 86%
off-target, and E1 rose from 5 sessions to 22 once it had no competition. And
cleaning the fixtures moved it not at all: 81% before, 81% after.

Reading the verdicts settled in a minute what the aggregates could not. B1
appears in 29 of 48 sessions where it is planted in 2, and none of the extras
are hallucinations — they are the definition applied far wider than written:
"the assistant said something it did not separately re-prove" rather than "the
assistant asserted an unchecked premise". Real examples scored as B1: a general
statement that deleting 4.8M rows would lock a table (general knowledge, which
the prompt **explicitly excludes**); an assistant not re-reading a file it had
just written; an assistant flagging its own assumption and inviting correction.
The `NOT B1` clauses exist and are not binding.


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


## 11A. Where the causes actually are — and what can reach them

This is the measurement the whole project turns on, taken on 2026-08-26 across
all 56 sessions and 449 findings.

`began_seq` — the record where a divergence STARTED, as opposed to where it
became visible — is populated on **449 of 449 findings**. Cause and symptom sit
a median of **10 records apart**, p90 **106**, max **1,337**. Only 9% share a
record. The corpus holds **52 shared origins**, where one event drove findings
in several categories at once.

That is the case for the session as the unit, and it is not arguable: under any
per-turn unit, 91% of these have their cause outside the unit that would catch
them.

### What sits at the origin

    action (a tool call)   44.1%  (198)   PreToolUse          NOT WIRED
    assistant text         26.1%  (117)   see below
    developer message      17.4%  ( 78)   UserPromptSubmit    the only hook wired
    tool result            12.5%  ( 56)   PostToolUse         recorder only

`rules.json` describes itself as *"a pre-send service that augments, questions,
or blocks a user message before it reaches the assistant"*. **That reaches
17.4% of what was measured.** `PreToolUse` appears nowhere in `install.py`,
`intercept.py` or `rules_engine.py`, and 176 of the 198 action origins are
Bash, Write and Edit — exactly what it fires on.

### Deterministic triggers are not available

Of the 198 action origins, the share where a deterministic index signal
(repeated command, rewritten target) was already true at the firing moment:

    **14%.**

By this project's own rule — *a judgement a regex cannot make is not a rule* —
86% of them need a model at fire time. That is a cost and latency decision, not
a detection one.

### The backtest: would a model have caught it?

`research/backtest.py` replays each origin with the future removed — the
session up to that point plus the pending tool call, nothing after. Half the
sample is control actions that are not origins, because a guard answering
"risky" to everything scores perfect recall and is worthless.

    Opus    27% recall, 0% false positives, 100% precision
    Haiku   comparable recall at a sixth of the cost

Production cost, firing on all 6,662 Bash/Write/Edit calls in the corpus at
~23k tokens of context each: **$2.97/session on Haiku, $14.87 on Opus.**

**The misses were not tuning failures.** Read them: on the ten-minute blocking
`npm run e2e`, the guard said *"running the e2e suite after updating the spec
is normal verification"* — and it is right. What cost ten minutes was the hang
and the silence afterwards, which does not exist yet when the command is about
to run. The same for a premise misread from a command's output. `began_seq`
conflates two things, and the backtest separated them:

    the action that was wrong          -> preventable at PreToolUse
    the action whose RESULT was then   -> not preventable there, at any
    mishandled                            prompt quality

### The reframing that matters (2026-08-27)

The reporter reading — build something that reports yesterday's friction — was
put to the developer and **rejected outright**, in terms worth keeping:

> "A reporter is not useful, the user is aware when there is a desync, they
> normally feel something is off and then see the assistant head in the wrong
> direction. The interesting bit is the prevention."

That sentence relocates the target. The guard tested above asked "is this
action about to cause wasted effort" — vague, and aimed at the action. What is
described is narrower and more visible: **the assistant states a direction, and
the direction is wrong.** All four of the guard's catches are exactly that
shape, caught before execution:

    "the edit reverses the developer's own change, seen in the git diff at seq 3"
    "the developer asked to turn hooks off; at seq 26 it announced a new
     master off-switch feature instead"
    "the blanket sed replaces every occurrence including manifest.json — the
     README (seq 7) says the ref is immutable"

And the direction is nearly always **announced first**: *"The port is 3401 —
808 looks like a truncated typo."* *"I'll take the text after the last ' by '."*
*"The analyser is splitting on hyphens."* Then it acts. `PreToolUse` fires in
that gap, with the stated premise already in context.

So the earlier claim that assistant-text origins have no hook is **wrong**.
Reachable share is not 44% but closer to **70%** — action origins plus the
assistant-text origins that precede an action.

The experiment this implies, not yet run: rewrite the guard to ask one
question — *what has the assistant committed to here, and does the evidence in
this session support it?* — and score it only on `cost`-severity origins, since
prevention that fires on cheap friction is not worth paying for at every tool
call.


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


### 15.12 An index entry that pointed the wrong way

`rewritten_targets` counted files written more than once and was what the
prompt told the judge to use for C4, work undone. Work is not usually undone by
a second Edit — it is undone by a shell command. A file written once and then
removed appears nowhere in that entry.

Measured on the planted fixtures, the signal was **inverted**: EMPTY on both
sessions containing a complete reversal, POPULATED on the control that merely
iterated toward a correct result. Worse, the judge's own `searched_how` shows
it finding the reversal in the records and then talking itself back out of it:

> "Checked index.rewritten_targets for files written more than once. The index
> shows an empty list. The files created in turn 1 are deleted in turn 2..."

It found the evidence and overruled itself on a signal that structurally cannot
see deletions. C4 returned 23 findings in the 449 despite this, so that number
is an undercount.

Fixed by adding `reversed_targets` — writes that a later command removed or
restored, with the command that did it, and a `sweeping` flag for reversals
naming no path (`git reset --hard`). Kept SEPARATE from `rewritten_targets`,
because iterating on a file and deleting it are different things and merging
them would just invert the problem the other way. The prompt now also says
plainly: **the index is an aid, not an authority — read the records regardless
of what it lists.**

Two follow-ons worth keeping. The path pattern originally required a dot or a
slash, so every bare name was invisible — `rm -rf dist`, `rm -rf node_modules`.
And the entry only sees reversals of files written by the write TOOLS: if the
assistant creates a file through a redirect or a generator and later deletes
it, that is genuinely work undone and this cannot see it. A known limit, not a
bug to discover later.

### 15.13 Spending before validating, three times over

The order was: build the instrument, spend $103 judging all 56 sessions, then
discover what the instrument got wrong. Five defects surfaced *after* the money
was spent — pass two silently skipped on every from-scratch session, the audit
blind to command text, the audit blind to JSON escaping, commands truncated at
160 characters, the C4 index inverted. Each time the reflex was to propose a
larger run as the remedy.

The developer's assessment, and it is the correct one:

> "This feels like a wild goose chase I feel we are not be scientific about
> this."

Three compounding faults, none of them technical. No validation before scale.
No ground truth — model judging model, checked by model, reported as agreement.
No falsifiable claim: "can a model produce plausible findings" was being
measured, and the product claim is "intervening reduces wasted effort", which
nothing tested.

### 15.14 Theorising over aggregates instead of reading the output

The 81% off-target rate got three explanations in succession — prompt size,
fixture impurity, small sessions causing hallucinations — and all three were
wrong. Reading six of the actual verdicts settled it in about a minute: the B1
definition was being applied far wider than written, and the exclusions already
in the prompt were not binding.

The pattern is worth naming because it recurred all day: a number invites a
theory, and the theory is cheaper to produce than the reading. **Read the
output before explaining the summary of it.**

### 15.15 Principles in context that did not govern behaviour

Three in one session, each within minutes of the principle being stated:

The build record contains *"a truncated field that doesn't say it's truncated
reads as complete"*. Commands were then shipped truncated at 160 characters,
unmarked, and it took a model flagging one as malformed to notice.

Having just fixed the mock generator for keeping its own copy of the index —
and having written down why duplication was the hazard — the next generator
inlined the header construction, the same fault one layer up. The developer
caught it, not me.

These are not knowledge gaps; the information was in context. Recency
outweighed it. In a long session, what surfaces unprompted degrades even when
retrieval does not — which is [[B3]] happening to the project that studies it,
and the reason durable artifacts beat a long conversation.


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

## 17A. Measurements from 2026-08-26/27

    449 findings across 56 sessions, $103.25, 0 rows failing schema
    quotes verbatim                       97% (437/449)
    severity                              258 cost / 130 friction / 61 none
    unnoticed by the developer            268 (60%), incl. 99 cost-severity
    began_seq populated                   449/449 (100%)
    cause-to-symptom gap                  median 10 records, p90 106, max 1337
    shared origins                        52 clusters
    finding density by size               9.5 per 100 records under 50 records,
                                          falling to 2.0 at 600+
    B1 + C1 share of all findings         42%  (suspect — see 5A)
    B3 findings                           0; only 2 of 56 sessions have >1 epoch
    reversals visible to the fixed index  4 sessions, 109 writes undone

    judging cost model, fitted and then confirmed to 3%
      fixed floor            ~$0.08/session (16 searched_how regardless of size)
      marginal               ~$1.60 per 100k document tokens
      detail calls           driven by how many categories triage flags; a
                             session flagging 7+ crosses into a second batch,
                             which is what made the original $87 estimate $105

    grammar ceiling, measured against the API not guessed
      findings + searched_how + a flag      4 categories max
      findings alone (what ships)           6 categories max
      a flat list of sequence numbers      12 categories max
      boolean + searched_how               16 categories max

## 18. Open questions

1. **D2's model pass is unwired** — 43 candidates, ~94k tokens, $0.47 at Opus.
2. **C4's 126 candidates are all `needs_review`** — deterministic detection
   cannot separate waste from an intended change of mind.
3. **A3, E3, E4 have no view.** E3/E4 are deterministic and cheap; A3 overlaps
   B1 heavily and may not need its own.
4. **`chat.py` cannot be measured for recall, and the attempt was malformed.**
   A "38% recall against `classify.py`" figure was computed and is withdrawn:
   all 8 markers in that session were B2 or E4, neither of which `chat.py`
   covers, so the maximum honest score was zero and the 3 apparent hits were
   incidental — flagged for a different reason in the same exchange.
   `classify.py`'s markers are ground truth for *"the developer complained"*,
   not for *"every inefficiency here"*, so measuring one against the other
   gives overlap, not recall. Recall cannot be measured at all without a
   ground-truth set nobody has built. Precision can: `--audit` gives 17 of 19
   quotes verbatim with one genuine fabrication.
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
