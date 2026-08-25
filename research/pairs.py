#!/usr/bin/env python3
"""Pair-based contradiction testing: which claim was wrong, and what it cost.

Covers two categories from `INEFFICIENCIES.md`, and only two:

    C1  costly self-correction — corrected itself after spending something
    B1  acted on an unverified premise

Both have the shape this instrument tests: *was a stated fact later shown
false?* Nothing else in the taxonomy has that shape, so do not point it
elsewhere. The exchange-shaped categories belong to `chat.py`, which reads a
session as a two-party chat; this reads it as a set of claims. The name says
the method rather than the ambition, deliberately — an earlier version called
itself a ledger and got read as a general desync finder, which it is not.

`classify.py` labels the message where a desync *surfaced*. This answers the
question that comes before it — which claim was wrong, when was it made, and
how long did work continue on top of it. `backwalk.py` tried to get there
deterministically and mostly could not ("mostly nothing", on 11 markers): the
walk needs to match "case scoped" against "client scoped" three hours later
under different words, and no regex does that.

    Input  (read-only):  <resync_home>/refined/   (via session.py)
    Output            :  <resync_home>/pairs/<session>.jsonl

Two filters, and only the second one costs money
------------------------------------------------

Sending every claim would be both expensive and wrong. Measured on the corpus,
assistant text is 90.8% of the spoken characters and 70% of them are under
200 chars of pure navigation — "let me check X", "reading the file". a0c27fd2
alone holds 308 claims, which is 47,278 unordered pairs.

So the model never sees a claim list. It sees *candidate pairs* produced here,
deterministically: two events that share a specific term — an identifier, a
path, an endpoint — where the second is later than the first. The model's job
is the judgement a regex cannot make, on a shortlist small enough to batch one
call per session.

Why the shared term has to be a rare one
----------------------------------------

Pairing on any shared word pairs everything: `rules_engine.tokens()` keeps any
word of four characters or more, and in a session about one codebase every
claim shares "component", "endpoint", "session". A term is only evidence that
two claims are about the same thing if it is *specific*, so terms are extracted
as identifiers rather than words, and then thrown away again if they appear in
more than `MAX_DF` of the session's events. That second half matters more than
the first: a session about `case_ref` mentions `case_ref` everywhere, and the
term stops discriminating exactly when the session is about it.

Compounds are split as well as kept. "case-scoped" and "client-scoped" share no
whole token, which is precisely the pair Daniel's worked example is about, so a
hyphenated or snake-cased term also contributes its parts.

What can overturn a claim
-------------------------

`claim_b` is a later assistant claim or a developer message. Failed tool
results are deliberately NOT candidates — the schema records `overturned_by` as
human or assistant, and a non-zero exit code is neither. They are carried in
the context windows instead, where they can inform the judgement without
inventing a third category the output has no field for. Compaction summaries
(`machine: true` in the stream) are excluded from both, since they are the
platform restating the session rather than anyone claiming anything.

Not paying twice for the same answer
------------------------------------

A verdict costs real money and the file is the only copy, so the store is built
around never needing to buy one twice.

**Cached per pair, not per session.** The call is still one batch per session —
that is what makes it cheap per run — but the cache is indexed by `pair_key`, a
hash of the pair's own text. Raising `--max-pairs`, adding a session, or
re-running after a crash leaves every already-judged pair alone; only genuinely
new pairs are sent. The first design keyed on a hash of the whole payload plus
the prompt text, which meant a one-word prompt edit re-ran all 46 sessions at
full price.

One honest caveat: a pair judged inside a batch of 100 is not strictly the same
event as the same pair judged in a batch of 50, because the model sees
different neighbours. Per-pair caching is a good approximation, not an
equivalence, and `pipeline_sha` on each row is what lets you tell which run a
verdict came from.

**Rejections are stored too.** The prompt asks the model to omit pairs it
rejected, so absence is the verdict — and an unrecorded rejection would be
re-sent, and re-paid for, forever. `attribute()` joins the returned
contradictions back onto the pairs that were sent, and everything unclaimed is
written as `contradiction: null`.

**A version string, not a hash of the prompt.** `PROMPT_VERSION` is bumped by
hand when the wording changes meaning, so fixing a typo costs nothing. The full
text of each version is written to `pairs/prompts/<version>.txt`, and a
mismatch between that file and the current `SYSTEM` aborts the run rather than
quietly serving verdicts from the old wording.

**The raw response is kept.** Only `contradictions` is parsed out, but the
whole response is stored, so adding a field to the schema later is a re-parse
rather than a re-purchase. It is the one irreplaceable thing in the file.

Every row carries `ts`, `model`, `prompt_version`, `prompt_sha`,
`pipeline_sha`, the expanded `params`, and `usage` with a costed `cost_usd`.
`--verify` totals what has been spent and reports rows whose pipeline has since
moved on. Nothing here is ground truth: `/ds` markers are, and they arrive
through session.py already.

An LLM pass is not reproducible, and `PLAN.md` bans a model from the *trigger*
path. This is a research view, not a trigger.

`overturning_quote` is what makes a row checkable. `--audit` re-reads each
quote against the stream it came from and reports any that are not verbatim —
a model that cannot produce the real phrase should not have logged the pair.

Usage:
    python3 pairs.py --candidates                    # counts only, no API call
    python3 pairs.py --dry-run --session a0c27      # exact payload, unsent
    python3 pairs.py --session a0c27 0825c6b6       # some sessions
    python3 pairs.py --all --model claude-opus-5    # every session
    python3 pairs.py --show [--session a0c27]       # read the findings
    python3 pairs.py --audit                        # are the quotes verbatim
    python3 pairs.py --verify                       # integrity, and spend
"""

import argparse
import collections
import concurrent.futures
import glob
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import backtest as B       # noqa: E402
import session as S        # noqa: E402
from plaintext import strip_decoration   # noqa: E402

# One file per session, not one file for everything. A stored verdict is
# expensive and long-lived; per-session files mean a re-run rewrites one file,
# a corrupt write loses one session, and you can read one session's results
# without parsing every other one.
PAIRS_DIR = os.path.join(B.resync_home(), "pairs")
# The pre-per-session store, from when this was called ledger.py. Left in
# place rather than migrated: its rows
# predate pair_key and carry no timestamp, so importing them would mint cache
# entries nothing can vouch for. --verify reports it.
LEGACY_STORE = os.path.join(B.resync_home(), "data", "ledger.jsonl")

TUNING_MODEL = "claude-haiku-4-5"
KEEPER_MODEL = "claude-opus-5"
NO_EFFORT_MODELS = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-haiku-3")

# $/1M input, $/1M output. Quoted so a pass is a decision with a number
# attached, and so `usage` on a stored row converts to money years later
# without anyone having to remember what the rate was.
PRICE = {"claude-haiku-4-5": (1.00, 5.00),
         "claude-opus-5": (5.00, 25.00),
         "claude-sonnet-5": (3.00, 15.00)}
PRICE_IN = {m: p[0] for m, p in PRICE.items()}

# Bumped by hand when the PROMPT changes MEANING. Hashing SYSTEM itself was the
# obvious design and the wrong one: a reflowed line or a fixed typo would
# invalidate every cached verdict and cost a full re-run. A version string puts
# that decision in a human's hands. The full text of each version is written to
# pairs/prompts/<version>.txt, so "what was v1" stays answerable after the
# constant below has moved on.
PROMPT_VERSION = "v1"


# --------------------------------------------------------------------------
# candidate generation — deterministic, and the only thing that keeps the
# call small enough to make
# --------------------------------------------------------------------------

# A term is an identifier, not a word. Dotted and snake/kebab compounds, camel
# case, and anything with a slash in it. Bare English words are excluded by
# construction: they are what made pairing useless in the first place.
RE_TERM = re.compile(
    r"[A-Za-z_][\w]*(?:[._\-/][\w]+)+"        # case_ref, manifest.local.json, a/b
    r"|[a-z]+[A-Z][A-Za-z]+"                   # camelCase
    r"|`[^`]{2,60}`"                           # anything the assistant quoted
)

# Terms this common in a session no longer say "these two claims are about the
# same thing". An ABSOLUTE cap, not a proportion of the session: at 25% of
# events a term could appear in 93 of a0c27fd2's 375 and still count, which
# produced 4,364 pairs — a shortlist that drops 98.6% of itself to a cap is a
# lottery, not a filter. At 3 the same session produces 328.
MAX_DF_ABS = 3
MIN_DF_ABS = 2          # a term in one event cannot pair anything
MIN_TERM_CHARS = 4

# One shared term is enough, deliberately. Requiring two would cut the corpus
# from 3,048 pairs to 1,324 — and would miss the case this file exists for:
# "case-scoped" and "client-scoped" share exactly one term, `scoped`, which is
# why compounds are split at all. Rarity does the discriminating instead.
MIN_SHARED = 1

# A human message pairs with the preceding N assistant claims regardless of
# shared terms. Measured cause for needing this: pairing on a rare shared
# identifier under-samples human messages 6x — they are 13% of pairable events
# but were 2.1% of pairs — because "still not working" and "that's wrong" carry
# no paths, endpoints or snake_case names to share. The result was 4 findings
# in 3 sessions, every one of them an assistant self-correction, and 0 of
# 2ac71e1f's known markers ever becoming a candidate at all. Human-caught
# contradictions are the expensive category, so they get a route that does not
# depend on the developer happening to name an identifier.
# 2, measured. Marker coverage is flat at 98% for any value >= 1 once the cap
# is out of the way, so this is not a recall dial — 1 would do. 2 costs 4% more
# pairs and offers the model two candidate origins per marker instead of one,
# which matters because the question here is *which* claim was wrong, not
# merely whether the message was looked at. Above 2 the cap starts throwing
# away term pairs for no coverage gain: at 5 it cost 66% marker coverage.
HUMAN_LOOKBACK = 2

# Claim A has to be long enough to be asserting something. This is a volume
# control, not the narration judgement — that one is the model's, and the
# prompt makes it explicitly. Set low enough to keep real one-line assertions
# ("The endpoint is client-scoped").
MIN_CLAIM_CHARS = 60

# Context sent either side of each claim. The spec says two or three events is
# enough; two, because the windows are the payload's bulk — each pair carries
# four of them and adjacent pairs re-send overlapping events. At three windows
# of 600 chars the corpus came to 3.85M input tokens, which is a $58 Opus pass
# on 2,029 pairs. Trimming the windows rather than the claims keeps what is
# being judged intact and cuts what merely surrounds it.
CONTEXT = 2

# Per-session cap on pairs sent. Reported, never silent: a workflow that
# quietly truncates coverage reads as "we checked everything" when it did not.
# At 100 the median session (77 pairs) is covered whole and only the largest
# few are trimmed, which is the right way round — the cap should be a backstop,
# not the thing deciding what gets looked at.
MAX_PAIRS = 100

CONTEXT_CHARS = 280
CLAIM_CHARS = 900


def load_stream(sid):
    """The speech of one session, straight from `refined/`.

    Was a read of a stored `friction/` file. That file served this view and the
    exchange view and blocked eight of the seventeen categories by being
    text-only, so extraction moved into `session.py` and each script now builds
    what it needs. The whole corpus parses in about a second, so there was
    never a performance reason for the intermediate file.

    Compaction summaries are excluded: the platform restating the session is
    not a claim anybody made."""
    return S.speech(S.events(sid))


def streams():
    return S.sessions()


def resolve(sids, prefix):
    hit = [s for s in sids if s.startswith(prefix)]
    if len(hit) == 1:
        return hit[0]
    if not hit:
        raise SystemExit("no session matches %r — run reduce.py first"
                         % prefix)
    raise SystemExit("%r is ambiguous: %s" % (prefix, ", ".join(s[:8] for s in hit)))


def terms(text):
    """Specific terms in a piece of text, compounds split as well as kept.

    "case-scoped" and "client-scoped" share no whole token — they are the same
    subject under different words, which is the pair this whole file exists to
    catch — so a compound contributes its parts too."""
    out = set()
    for m in RE_TERM.findall(text or ""):
        t = m.strip("`").lower()
        if len(t) >= MIN_TERM_CHARS:
            out.add(t)
        for part in re.split(r"[._\-/]", t):
            if len(part) >= MIN_TERM_CHARS:
                out.add(part)
    return out


def ts(s):
    return B.ts(s)


def gap_str(a, b):
    """"5h47m" — the units the output schema asks for."""
    ta, tb = ts(a), ts(b)
    if not ta or not tb:
        return None
    secs = int(abs((tb - ta).total_seconds()))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return "%dd%dh" % (d, h)
    if h:
        return "%dh%02dm" % (h, m)
    if m:
        return "%dm%02ds" % (m, s)
    return "%ds" % s


def candidates(events, max_pairs=MAX_PAIRS):
    """[(i, j, shared_terms)] — pairs worth asking a model about.

    Returns (pairs, stats) so the caller can report what was dropped."""
    # Only claims and developer messages take part. Errors stay context-only:
    # `overturned_by` is human or assistant, and an exit code is neither.
    idx = [i for i, e in enumerate(events)
           if e.get("kind") in (S.SPEECH)]

    tset = {}
    for i in idx:
        tset[i] = terms(events[i].get("text"))

    df = collections.Counter()
    for i in idx:
        for t in tset[i]:
            df[t] += 1

    keep = {t for t, c in df.items() if MIN_DF_ABS <= c <= MAX_DF_ABS}
    for i in idx:
        tset[i] &= keep

    # Claim A must be an assistant claim with enough substance to be asserting
    # something. Claim B may be either side — a later claim, or the developer.
    heads = [i for i in idx
             if events[i].get("kind") == "assistant_text"
             and len(events[i].get("text") or "") >= MIN_CLAIM_CHARS
             and tset[i]]

    seen, pairs = set(), []
    for a in heads:
        for b in idx:
            if b <= a:
                continue
            shared = tset[a] & tset[b]
            if len(shared) >= MIN_SHARED:
                seen.add((a, b))
                pairs.append((a, b, sorted(shared), "shared-term"))
    n_term = len(pairs)

    # Every human message against the claims just before it, shared term or
    # not. `heads` is reused so the same substance floor applies and narration
    # is still excluded — what is dropped is only the requirement that the
    # developer used the assistant's vocabulary.
    n_look = 0
    for b in idx:
        if events[b].get("kind") != "developer":
            continue
        if HUMAN_LOOKBACK <= 0:
            continue
        # NOT `[-HUMAN_LOOKBACK:]` without this guard: `lst[-0:]` is the whole
        # list in Python, so a lookback of 0 would silently mean "every
        # preceding claim" — the opposite of off.
        for a in [h for h in heads if h < b][-HUMAN_LOOKBACK:]:
            if (a, b) in seen:
                continue
            seen.add((a, b))
            pairs.append((a, b, sorted(tset[a] & tset[b]), "human-lookback"))
            n_look += 1

    stats = {"events": len(idx), "heads": len(heads), "terms_kept": len(keep),
             "terms_seen": len(df), "pairs_found": len(pairs),
             "by_term": n_term, "by_lookback": n_look}

    # Rank before capping, so a cap keeps the pairs most worth asking about
    # rather than the earliest ones. Lookback pairs sort first: they are the
    # only route to a human-caught contradiction, which is the category that
    # costs something. Term pairs then sort by rarity, rarest shared term
    # first, then by how many terms are shared.
    def rank(p):
        if p[3] == "human-lookback":
            return (0, 0, 0)
        return (1, min(df[t] for t in p[2]) if p[2] else 99, -len(p[2]))
    pairs.sort(key=rank)
    stats["pairs_sent"] = min(len(pairs), max_pairs)
    stats["pairs_dropped"] = max(0, len(pairs) - max_pairs)
    return pairs[:max_pairs], stats


def window(events, i, before, after):
    lo = [e for e in events[max(0, i - before):i]]
    hi = [e for e in events[i + 1:i + 1 + after]]

    def slim(e):
        out = {"at": e.get("at"), "kind": e.get("kind"),
               "text": strip_decoration(e.get("text") or e.get("note") or "")
                       [:CONTEXT_CHARS]}
        if e.get("kind") == "question":
            out["text"] = "[asked] %s [outcome: %s]" % (
                "; ".join(x or "" for x in e.get("asked") or []), e.get("outcome"))
        return out
    return lo, hi


def build_payload(sid, events, pairs):
    """The per-candidate objects the contradiction prompt expects."""
    out = []
    for a, b, shared, why in pairs:
        ea, eb = events[a], events[b]
        ba, aa = window(events, a, CONTEXT, CONTEXT)
        bb, ab = window(events, b, CONTEXT, CONTEXT)
        out.append({
            "session_id": sid,
            "candidate": {
                # Stripped here and nowhere earlier: refined/ stays the
                # faithful record, and only what leaves for the model is lossy.
                "claim_a": {"at": ea.get("at"),
                            "text": strip_decoration(ea.get("text") or "")
                                    [:CLAIM_CHARS]},
                "claim_b": {"at": eb.get("at"), "kind": eb.get("kind"),
                            "text": strip_decoration(eb.get("text") or "")
                                    [:CLAIM_CHARS]},
                "shared_terms": shared[:8],
                # Additive to the brief's input format, not a change to the
                # output schema: a lookback pair can share no terms at all, and
                # offering one with an empty `shared_terms` and no reason would
                # read as a filter bug rather than a deliberate candidate.
                "paired_by": why,
                "gap": gap_str(ea.get("at"), eb.get("at")),
            },
            "context_before_a": ba, "context_after_a": aa,
            "context_before_b": bb, "context_after_b": ab,
        })
    return out


# --------------------------------------------------------------------------
# the model pass
# --------------------------------------------------------------------------

SYSTEM = """\
You are building a contradiction ledger from a Claude Code session log. You
will be given candidate pairs of claims that share a keyword, file path, or
identifier, found by a deterministic filter. Your job is to judge, for each
pair, whether a real contradiction happened, and if so, how expensive it was.

WHAT COUNTS AS A CONTRADICTION

Only log a pair if:
- One claim stated something as settled fact, a decision, or an assumption
  the assistant then acted on (wrote code against it, told the user it was
  done, or used it to justify a later step), AND
- A later message, from the human or from the assistant itself, shows that
  claim was wrong, and you can point to the specific phrase that overturns it.

Do NOT log a pair if:
- Claim A was phrased as an open question and claim B is simply the human
  answering it. A question that gets answered is not a contradiction, even
  if the human's answer differs from what the assistant guessed elsewhere.
- Claim A was narration ("let me check X", "now I'll do Y", "reading the
  file") rather than an assertion. Narration has nothing to contradict.
- The two claims are about the same topic but neither actually conflicts,
  e.g. the second claim adds detail to the first rather than overturning it.
- This is ordinary negotiation, the human changes their mind between two
  messages and the assistant simply followed each instruction in turn. That
  is normal collaboration, not a desync, unless the assistant had already
  committed to and acted on the first instruction as final before the human
  changed it.

SELF CORRECTIONS

Include cases where the assistant contradicts itself with no human message
in between, e.g. "my diagnosis is probably wrong" followed by the actual
cause a message later. Mark these with "corrected_by": "assistant" rather
than "human" in the output, so they can be filtered separately. These are
lower priority than human caught contradictions, since they cost nothing
downstream, but they're still useful signal about where the assistant was
reasoning out loud rather than checking first.

CONFIDENCE OF THE ORIGINAL CLAIM

Classify claim A's confidence as one of:
- "confirmed"  the assistant had direct evidence (read a file, ran a
  command, saw the actual output) before stating it
- "believed"   the assistant reasoned to a conclusion but flagged
  uncertainty ("I believe", "should", "assuming")
- "guessed"    the assistant stated something as fact with no evidence
  shown, or explicitly chose to guess rather than ask (e.g. after a
  rejected AskUserQuestion)

A guessed claim that turns out wrong is the expensive category. A confirmed
claim that turns out wrong usually means the ground truth changed underneath
the assistant (a branch got merged, a config changed) rather than a
reasoning failure, note this distinction in "note" if it applies.

SEVERITY

For every logged contradiction, compute the gap between claim A's timestamp
and the overturning message's timestamp, in the same units as the input
timestamps. Longer gaps generally mean more work happened downstream on a
false premise, but also check whether the assistant's own claim_b text or
context_after_b describes a rebuild, revert, or repeated work, and reflect
that in "rework_evidence" if present. A short gap with clear rework evidence
can matter more than a long gap with none.

OUTPUT

Return a JSON array, one object per confirmed contradiction, sorted by gap
descending. Do not include an entry for pairs you decided are not real
contradictions. Each object:

{
  "claim_a_at": "...",
  "claim_a_text": "...",           // verbatim, trimmed to the load bearing sentence
  "claim_a_confidence": "confirmed" | "believed" | "guessed",
  "overturned_at": "...",
  "overturned_by": "human" | "assistant",
  "overturning_quote": "...",      // the exact phrase that overturns claim A, verbatim
  "gap": "...",                    // e.g. "5h47m"
  "rework_evidence": "..." | null, // short note on what got redone, or null
  "note": "..." | null             // e.g. "ground truth changed, not a reasoning failure"
}

If none of the candidate pairs for this session are real contradictions,
return an empty array. An empty array is a valid and useful answer, do not
manufacture a weak contradiction to avoid returning one.\
"""

# The prompt says "return a JSON array". The API's structured output wants an
# object at the root, so the array is carried in a single field and unwrapped
# on the way out — the model still sees the instruction it was written with.
SCHEMA = {
    "type": "object",
    "properties": {
        "contradictions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_a_at": {"type": "string"},
                    "claim_a_text": {"type": "string"},
                    "claim_a_confidence": {
                        "type": "string",
                        "enum": ["confirmed", "believed", "guessed"]},
                    "overturned_at": {"type": "string"},
                    "overturned_by": {"type": "string",
                                      "enum": ["human", "assistant"]},
                    "overturning_quote": {"type": "string"},
                    "gap": {"type": "string"},
                    "rework_evidence": {"type": ["string", "null"]},
                    "note": {"type": ["string", "null"]},
                },
                "required": ["claim_a_at", "claim_a_text", "claim_a_confidence",
                             "overturned_at", "overturned_by",
                             "overturning_quote", "gap", "rework_evidence",
                             "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["contradictions"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------
# the contract judge.py reads (see judge.py for what each field means)
# --------------------------------------------------------------------------

MODE = "session"        # one call per session; the response is an array
UNIT_LABEL = "CANDIDATE PAIRS"


def unit_key(u):
    """The key the view was built with, not one recomputed from stored text."""
    if u.get("unit_key"):
        return u["unit_key"]
    c = u.get("candidate") or {}
    return pair_key((c.get("claim_a") or {}).get("text"),
                    (c.get("claim_b") or {}).get("text"))


def rows_from(data, todo, base):
    """One verdict row per candidate sent, contradiction or not.

    The prompt asks the model to omit pairs it rejected, so absence IS the
    verdict — and an unrecorded rejection would be re-sent, and re-paid for, on
    every future run. `attribute()` joins what came back onto what went out;
    anything unclaimed is stored as `contradiction: null`."""
    found = (data or {}).get("contradictions") or []
    sent = {k: v for k, v in todo.items()}
    hits, leftover = attribute(found, sent)
    rows = []
    for k, u in sent.items():
        hit = hits.get(k)
        c = u.get("candidate") or {}
        rows.append(dict(base, kind="verdict", unit_key=k,
                         claim_a_at=(c.get("claim_a") or {}).get("at"),
                         claim_b_at=(c.get("claim_b") or {}).get("at"),
                         shared_terms=c.get("shared_terms"),
                         contradiction=hit[0] if hit else None,
                         matched=hit[1] if hit else None))
    for f in leftover:
        # Never dropped: a finding is the expensive thing here, and losing one
        # to a bookkeeping mismatch would be the worst trade available.
        rows.append(dict(base, kind="verdict", unit_key=None,
                         contradiction=f, matched="unattributed"))
    return rows


def pipeline_params():
    """Everything that shapes the payload, as a readable dict.

    Stored expanded on every run rather than only hashed: a hash tells you two
    runs differed, this tells you how. `plaintext_sha` covers the strip rules
    by hashing the module source, since those change the text the model sees
    without any parameter here moving."""
    try:
        with open(os.path.join(HERE, "plaintext.py"), "rb") as fh:
            nsha = hashlib.sha1(fh.read()).hexdigest()[:12]
    except OSError:
        nsha = None
    return {"max_df_abs": MAX_DF_ABS, "min_df_abs": MIN_DF_ABS,
            "min_term_chars": MIN_TERM_CHARS, "min_shared": MIN_SHARED,
            "min_claim_chars": MIN_CLAIM_CHARS, "context": CONTEXT,
            "context_chars": CONTEXT_CHARS, "claim_chars": CLAIM_CHARS,
            "max_pairs": MAX_PAIRS, "plaintext_sha": nsha}


def pipeline_sha(params=None):
    p = params or pipeline_params()
    return hashlib.sha1(json.dumps(p, sort_keys=True)
                        .encode("utf-8")).hexdigest()[:12]


def prompt_sha():
    return hashlib.sha1(SYSTEM.encode("utf-8")).hexdigest()[:12]


def pair_key(claim_a_text, claim_b_text):
    """Identity of a candidate pair, from its CONTENT alone.

    Deliberately excludes model and prompt version — those index the cache
    alongside it, so the same pair judged by two models is visibly the same
    pair. Uses the stripped text, because that is what the model was shown:
    keying on the decorated original would re-send every pair the day
    plaintext.py changes a rule that alters nothing the model reads."""
    blob = "%s\x1f%s" % (claim_a_text or "", claim_b_text or "")
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:16]


def ensure_pairs_dir():
    """0700 with a self-contained `.gitignore` of `*`, matching reduce.py.

    Rows quote claim text verbatim, so the store is exactly as
    secrets-bearing as the archive it derives from, and this repo is public."""
    os.makedirs(PAIRS_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(PAIRS_DIR, 0o700)
    except OSError:
        pass
    gi = os.path.join(PAIRS_DIR, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as fh:
            fh.write("# Auto-created by pairs.py.\n"
                     "# Quotes developer and assistant text verbatim "
                     "— never commit it.\n*\n")


def save_prompt_version():
    """Write the prompt text for this version, once.

    The key stores a version string rather than a hash of the text, which is
    what stops a typo fix costing a re-run — but it means the text has to be
    recoverable from somewhere. This is that somewhere. Never overwritten: if
    the file exists and differs, the version was changed without being bumped,
    and saying so is more useful than silently replacing the record."""
    d = os.path.join(PAIRS_DIR, "prompts")
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, "%s.txt" % PROMPT_VERSION)
    if os.path.exists(path):
        with open(path, errors="replace") as fh:
            if fh.read() != SYSTEM:
                return ("MISMATCH: %s on disk differs from the current SYSTEM. "
                        "Bump PROMPT_VERSION, or the cache will serve verdicts "
                        "from the old wording." % path)
        return None
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(SYSTEM)
    os.chmod(path, 0o600)
    return None


def pairs_path(sid):
    return os.path.join(PAIRS_DIR, "%s.jsonl" % sid)


def views_dir():
    """<resync_home>/views/pairs — the deterministic view, not the verdict.

    Separate from PAIRS_DIR because the two have different lifetimes: a view is
    free to regenerate whenever extraction changes, a verdict was paid for and
    must survive. Nothing reads a view — that is what keeps it from
    constraining what any script can see, which is the mistake `friction/`
    made."""
    d = os.path.join(B.resync_home(), "views", "pairs")
    parent = os.path.join(B.resync_home(), "views")
    os.makedirs(parent, mode=0o700, exist_ok=True)
    gi = os.path.join(parent, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as fh:
            fh.write("# Auto-created. Deterministic views of the corpus, "
                     "verbatim — never commit.\n*\n")
    return d


def write_payload(sid, payload, params, psha):
    """Store the exact objects that would be sent, and send nothing.

    Deliberately a write-only artefact, not a pipeline stage: nothing reads it.
    That is the difference from the old `friction/` files, which two scripts
    depended on and which therefore constrained what either could see. This is
    here for the reason Daniel asked for it originally — so the input to a
    judgement can be read by eye rather than taken on trust."""
    d = views_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, "%s.jsonl" % sid)
    head = {"kind": "header", "session": sid, "candidates": len(payload),
            "prompt_version": PROMPT_VERSION, "prompt_sha": prompt_sha(),
            "pipeline_sha": psha, "params": params}
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(head, ensure_ascii=False, default=str) + "\n")
        for p in payload:
            fh.write(json.dumps(p, ensure_ascii=False, default=str) + "\n")
    os.chmod(path, 0o600)
    return path


def pairs_sessions():
    return sorted(os.path.basename(p)[:-6]
                  for p in glob.glob(os.path.join(PAIRS_DIR, "*.jsonl")))


def load_pair_rows(sid):
    """All rows for one session, oldest first."""
    path = pairs_path(sid)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def load_verdicts(sid, model=None, version=PROMPT_VERSION):
    """{pair_key: verdict row} — the cache, indexed by pair not by session.

    This is what stops a re-run costing what the first run cost. Raising
    --max-pairs, adding a session, or re-running after a crash all leave every
    already-judged pair alone; only genuinely new pairs are sent."""
    got = {}
    for r in load_pair_rows(sid):
        if r.get("kind") != "verdict":
            continue
        if r.get("prompt_version") != version:
            continue
        if model and r.get("model") != model:
            continue
        got[r.get("pair_key")] = r      # last write wins
    return got


def append_rows(sid, rows):
    if not rows:
        return
    ensure_pairs_dir()
    path = pairs_path(sid)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    os.chmod(path, 0o600)


def cost_of(model, usage):
    if not usage:
        return None
    rate = PRICE.get(model)
    if not rate:
        return None
    return round(usage.get("input_tokens", 0) / 1e6 * rate[0]
                 + usage.get("output_tokens", 0) / 1e6 * rate[1], 4)


def ask(client, model, sid, payload):
    """Returns (contradictions, usage, raw). The raw response is kept so a
    later schema change can be re-parsed instead of re-purchased."""
    user = ("SESSION: %s\n\nCANDIDATE PAIRS (%d):\n%s"
            % (sid, len(payload),
               json.dumps(payload, ensure_ascii=False, indent=1, default=str)))
    fmt = {"format": {"type": "json_schema", "schema": SCHEMA}}
    if not model.startswith(NO_EFFORT_MODELS):
        fmt["effort"] = "medium"
    resp = client.messages.create(
        model=model, max_tokens=16000, system=SYSTEM,
        messages=[{"role": "user", "content": user}], output_config=fmt)
    if resp.stop_reason == "refusal":
        raise RuntimeError("refused")
    blob = next(b.text for b in resp.content
                if getattr(b, "type", None) == "text")
    data = json.loads(blob)
    u = resp.usage
    usage = {"input_tokens": getattr(u, "input_tokens", 0),
             "output_tokens": getattr(u, "output_tokens", 0),
             "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0),
             "cache_creation_input_tokens":
                 getattr(u, "cache_creation_input_tokens", 0)}
    return data.get("contradictions") or [], usage, data


def attribute(found, sent):
    """Match returned contradictions back to the pairs that were sent.

    Needed because the prompt asks the model to omit pairs it rejected, so a
    pair's absence is its verdict — and an unrecorded rejection would be
    re-sent, and re-paid for, on every future run. The join is on the
    timestamps the schema already requires, which is why the output needs no
    extra field: `claim_a_at` plus `overturned_at` identifies the pair.

    A contradiction that matches nothing is kept and marked `unattributed`
    rather than dropped: a finding is the expensive thing here, and losing one
    to a bookkeeping mismatch would be the worst possible trade."""
    by_both, by_a = {}, collections.defaultdict(list)
    for pk, p in sent.items():
        c = p["candidate"]
        by_both[(c["claim_a"]["at"], c["claim_b"]["at"])] = pk
        by_a[c["claim_a"]["at"]].append(pk)

    hits, leftover = {}, []
    for f in found:
        a, b = f.get("claim_a_at"), f.get("overturned_at")
        pk = by_both.get((a, b))
        how = "exact"
        if pk is None:
            cand = [k for k in by_a.get(a, []) if k not in hits]
            pk, how = (cand[0], "claim_a") if cand else (None, None)
        if pk is None:
            leftover.append(f)
        else:
            hits[pk] = (f, how)
    return hits, leftover


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def norm(s):
    return " ".join((s or "").split()).lower()


def all_verdicts(sids=None, model=None, dedupe=True):
    """[(session, verdict row)] for every stored contradiction.

    Deduped on the CONTRADICTION's identity — (session, claim_a_at,
    overturned_at) — not on pair_key. The same wrong claim can be reached from
    more than one candidate pair: a term pair and a lookback pair may offer the
    same claim_a against different claim_b, and the model can land on the same
    overturning moment from either. Those are two routes to one finding, and
    counting them twice would inflate both the report and the audit's
    denominator. Newest wins, so a re-run supersedes rather than duplicates."""
    out = []
    for sid in (sids or pairs_sessions()):
        for r in load_pair_rows(sid):
            if r.get("kind") != "verdict" or not r.get("contradiction"):
                continue
            if model and r.get("model") != model:
                continue
            out.append((sid, r))
    if not dedupe:
        return out
    seen = {}
    for sid, r in out:                 # file order is append order
        c = r["contradiction"]
        seen[(sid, c.get("claim_a_at"), c.get("overturned_at"),
              r.get("model"))] = (sid, r)
    return list(seen.values())


def audit(sids=None):
    """Is every `overturning_quote` actually in the stream it came from?

    The quote is the whole reason a row is checkable. A model that cannot
    produce the real phrase should not have logged the pair, so a miss here is
    a finding about the prompt, not a formatting problem."""
    by_session = collections.defaultdict(list)
    for sid, r in all_verdicts(sids):
        by_session[sid].append(r)
    ok = bad = elided = redecorated = 0
    misses = []
    for sid, rows in sorted(by_session.items()):
        events = load_stream(sid)
        # Against the STRIPPED text, because that is what the model was shown.
        # Auditing a quote from a stripped payload against the decorated record
        # would report "**no crontab for daniel**" as a missing quote and blame
        # the model for the pipeline's own formatting.
        hay = norm(" ".join(strip_decoration(e.get("text") or e.get("note") or "")
                            for e in events))
        for r in rows:
            c = r["contradiction"]
            raw_q = c.get("overturning_quote")
            # Strip the quote as well as the haystack. The model is shown
            # stripped text and still hands back "The file is **tracked**"
            # where it was sent "The file is tracked" — it re-decorates its own
            # quote with markdown it never saw. Every word is right, so that is
            # neither a fabrication nor an elision, and failing it would have
            # reported a sound finding as unverifiable. This is precisely what
            # plaintext.py's idempotency buys: stripping an already-stripped
            # quote is a no-op, and stripping a re-decorated one recovers it.
            q = norm(strip_decoration(raw_q or ""))
            if not q:
                bad += 1
                misses.append(("empty", sid, r.get("model"), c))
            elif q in hay:
                ok += 1
                if norm(raw_q) not in hay:
                    redecorated += 1
            elif "..." in q and all(part.strip() in hay
                                    for part in q.split("...") if part.strip()):
                # Every fragment is real but the model stitched them with an
                # ellipsis. Checkable, so not a fabrication — but not the
                # contiguous span the schema asks for, and worth its own
                # category rather than being counted as either.
                elided += 1
                misses.append(("elided", sid, r.get("model"), c))
            else:
                bad += 1
                misses.append(("not found", sid, r.get("model"), c))
    n = ok + elided + bad
    print("quotes: %d verbatim, %d elided, %d not found" % (ok, elided, bad))
    if redecorated:
        print("  (%d of the verbatim ones were re-decorated by the model — "
              "markdown it was\n   never shown, added back around correct "
              "words. Content is exact.)" % redecorated)
    if n:
        print("verbatim rate: %.0f%%   checkable rate: %.0f%%"
              % (100.0 * ok / n, 100.0 * (ok + elided) / n))
    for kind, sid, model, c in misses[:12]:
        print("\n  [%s] %s [%s] gap %s" % (kind, sid[:8], model, c.get("gap")))
        print("    claimed quote: %s" % (c.get("overturning_quote") or "")[:110])
    if elided:
        print("\nElided quotes are real text joined with '...'. The fragments "
              "check out, so\nthe finding stands — but the schema asks for one "
              "contiguous span. Add a line\nto the prompt forbidding ellipsis "
              "if you want these to land verbatim.")
    if bad:
        print("\nA quote that is not in the stream means the row cannot be "
              "checked.\nTreat those rows as unconfirmed, and the prompt as "
              "needing work.")
    return 0


def do_verify(sids=None):
    """Every row parses, every session referenced still has a stream, and every
    verdict was produced by a pipeline whose parameters are recorded.

    In the spirit of reduce.py --verify: the archive should be able to prove
    its own integrity rather than be assumed to have it."""
    sids = sids or pairs_sessions()
    runs = verdicts = orphan = stale = bad = 0
    spend = collections.Counter()
    tokens = collections.Counter()
    current = pipeline_sha()
    for sid in sids:
        if sid not in set(S.sessions()):
            orphan += 1
            print("  no transcript in refined/ for %s — rows unauditable"
                  % sid[:8])
        for r in load_pair_rows(sid):
            k = r.get("kind")
            if k == "run":
                runs += 1
                if r.get("pipeline_sha") != current:
                    stale += 1
                m = r.get("model")
                if r.get("cost_usd"):
                    spend[m] += r["cost_usd"]
                u = r.get("usage") or {}
                tokens[m] += u.get("input_tokens", 0) + u.get("output_tokens", 0)
            elif k == "verdict":
                verdicts += 1
                c = r.get("contradiction")
                if c and not all(f in c for f in SCHEMA["properties"]
                                 ["contradictions"]["items"]["required"]):
                    bad += 1
            else:
                bad += 1
    print("%d session file(s): %d run(s), %d verdict(s)"
          % (len(sids), runs, verdicts))
    print("rows failing their schema: %d" % bad)
    print("runs from a superseded pipeline: %d (current %s)" % (stale, current))
    print("stored rows with no transcript in refined/: %d" % orphan)
    if spend:
        print("\nspent so far:")
        for m, c in spend.most_common():
            print("   %-20s $%.2f   %s tokens" % (m, c, "{:,}".format(tokens[m])))
        print("   %-20s $%.2f" % ("TOTAL", sum(spend.values())))
    if os.path.exists(LEGACY_STORE):
        n = sum(1 for _ in open(LEGACY_STORE, errors="replace"))
        print("\nlegacy %s holds %d row(s) from before per-session storage."
              % (LEGACY_STORE, n))
        print("Not imported: those rows predate pair_key and carry no "
              "timestamp, so they\ncannot seed the cache. Kept as history.")
    return 1 if bad else 0


def show(sids=None, self_corrections=False):
    rows = []
    for sid, r in all_verdicts(sids):
        c = r["contradiction"]
        if not self_corrections and c.get("overturned_by") == "assistant":
            continue
        rows.append((sid, r.get("model"), c))
    if not rows:
        print("nothing stored yet" if not pairs_sessions() else
              "no human-caught contradictions stored "
              "(--self to include self-corrections)")
        return 0

    def secs(c):
        g = c[2].get("gap") or ""
        m = re.match(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", g)
        d_, h_, m_, s_ = [int(x or 0) for x in (m.groups() if m else (0, 0, 0, 0))]
        return -(d_ * 86400 + h_ * 3600 + m_ * 60 + s_)
    rows.sort(key=secs)

    conf = collections.Counter(c.get("claim_a_confidence") for _, _, c in rows)
    print("%d contradiction(s) across %d session(s) — confidence of the "
          "original claim: %s\n"
          % (len(rows), len(set(r[0] for r in rows)), dict(conf)))
    for sid, model, c in rows:
        print("%-9s %-7s %-9s %s" % (sid[:8], c.get("gap"),
                                     c.get("claim_a_confidence"),
                                     " ".join((c.get("claim_a_text") or "")
                                              .split())[:88]))
        print("%-9s %-7s %-9s -> %s" % ("", "", c.get("overturned_by"),
                                        " ".join((c.get("overturning_quote") or "")
                                                 .split())[:88]))
        if c.get("rework_evidence"):
            print("%-28s rework: %s" % ("", c["rework_evidence"][:80]))
        if c.get("note"):
            print("%-28s note:   %s" % ("", c["note"][:80]))
        print()
    return 0


# Characters per token in a payload, MEASURED rather than assumed. The usual
# chars/4 rule of thumb understated a real 47,049-token call by 1.9x: this
# payload is JSON dense with timestamps, identifiers, paths and code, none of
# which tokenise anything like prose. Taken from the aabd1a42 run — 99,016
# characters against 47,049 counted tokens. Still an estimate; --count-tokens
# asks the API.
CHARS_PER_TOKEN = 2.1


def exact_tokens(work, model):
    """Real input size from the token-counting endpoint.

    Free, but it is a network call needing credentials, which is why
    --candidates does not do it unless asked: the point of that flag is that
    you can see what a run would cost without having any."""
    sys.path.insert(0, B.resync_home())
    try:
        import intercept as I
        anthropic = I.import_anthropic()
        if anthropic is None:
            raise ImportError("anthropic SDK not importable")
        I.load_env_file()
        client = anthropic.Anthropic()
    except Exception as exc:
        print("cannot count tokens: %s" % exc)
        return None
    total = 0
    for sid, _, td in work:
        user = ("SESSION: %s\n\nCANDIDATE PAIRS (%d):\n%s"
                % (sid, len(td), json.dumps(list(td.values()),
                                            ensure_ascii=False, indent=1,
                                            default=str)))
        r = client.messages.count_tokens(
            model=model, system=SYSTEM,
            messages=[{"role": "user", "content": user}])
        total += r.input_tokens
    return total


def select(sids, wanted):
    """Resolve a list of id prefixes. Every one must match exactly one
    session: a typo silently matching nothing would look like a clean run."""
    out = []
    for w in wanted:
        hit = resolve(sids, w)
        if hit not in out:
            out.append(hit)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--session", nargs="+", metavar="ID",
                    help="one or more sessions (id prefixes are enough)")
    ap.add_argument("--all", action="store_true",
                    help="every session in refined/. Required for a "
                         "full pass — there is no implicit one")
    ap.add_argument("--candidates", action="store_true",
                    help="report the deterministic shortlist and stop. "
                         "No API call, no cost")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the exact payload that would be sent, unsent")
    ap.add_argument("--write", action="store_true",
                    help="store the exact payload per session and stop. "
                         "Deterministic, no API call, no cost")
    ap.add_argument("--model", default=TUNING_MODEL,
                    help="default %s; use %s for the pass you keep"
                         % (TUNING_MODEL, KEEPER_MODEL))
    ap.add_argument("--max-pairs", type=int, default=MAX_PAIRS,
                    help="cap per session (default %d)" % MAX_PAIRS)
    ap.add_argument("--recheck", action="store_true",
                    help="re-ask pairs that already have a stored verdict")
    ap.add_argument("--show", action="store_true", help="print stored findings")
    ap.add_argument("--self", action="store_true", dest="self_corrections",
                    help="include assistant self-corrections in --show")
    ap.add_argument("--audit", action="store_true",
                    help="check every overturning_quote against its stream")
    ap.add_argument("--verify", action="store_true",
                    help="prove the stored rows parse, and report spend")
    ap.add_argument("--count-tokens", action="store_true",
                    help="with --candidates: get the exact input size from "
                         "the token-counting endpoint instead of estimating. "
                         "Free, but it is an API call and needs credentials")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)

    if args.show or args.audit or args.verify:
        have = pairs_sessions()
        sids = select(have, args.session) if args.session else None
        if args.verify:
            return do_verify(sids)
        return audit(sids) if args.audit else show(sids, args.self_corrections)

    sids = streams()
    if not sids:
        print("no sessions in %s — run reduce.py first" % S.REFINED)
        return 1
    if args.session:
        wanted = select(sids, args.session)
    elif args.all or args.candidates or args.write:
        wanted = sids
    else:
        print("pick a scope: --session <id> [<id> ...] for some, --all for "
              "every session.")
        print("--candidates costs nothing and shows what would be sent.")
        return 2

    params = pipeline_params()
    psha = pipeline_sha(params)

    work, stats_all = [], []
    for sid in wanted:
        events = load_stream(sid)
        pairs, st = candidates(events, args.max_pairs)
        st["session"] = sid
        payload = build_payload(sid, events, pairs) if pairs else []
        # Key every candidate by its own content, then drop the ones already
        # judged under this model and prompt version. This is what makes a
        # second run cost only what is genuinely new.
        keyed = {}
        for pl in payload:
            c = pl["candidate"]
            k = pair_key(c["claim_a"]["text"], c["claim_b"]["text"])
            pl["unit_key"] = k          # stamped, never recomputed downstream
            keyed[k] = pl
        cached = ({} if args.recheck
                  else load_verdicts(sid, args.model, PROMPT_VERSION))
        todo = {k: v for k, v in keyed.items() if k not in cached}
        st["cached"] = len(keyed) - len(todo)
        st["to_send"] = len(todo)
        stats_all.append(st)
        if todo:
            work.append((sid, events, todo))

    if args.write:
        ensure_pairs_dir()
        n = 0
        for sid in wanted:
            events = load_stream(sid)
            prs, _st = candidates(events, args.max_pairs)
            pl = build_payload(sid, events, prs) if prs else []
            write_payload(sid, pl, params, psha)
            n += len(pl)
        print("%d session(s), %d candidate pair(s) -> %s/<session>.jsonl"
              % (len(wanted), n, views_dir()))
        print("pipeline %s, prompt %s. Nothing sent." % (psha, PROMPT_VERSION))
        return 0

    if args.candidates:
        print("%-9s %6s %6s %6s %7s %7s %7s  %s"
              % ("session", "events", "heads", "terms", "pairs", "sent",
                 "cached", "dropped"))
        print("-" * 70)
        for st in sorted(stats_all, key=lambda s: -s["pairs_found"]):
            print("%-9s %6d %6d %6d %7d %7d %7d  %s"
                  % (st["session"][:8], st["events"], st["heads"],
                     st["terms_kept"], st["pairs_found"], st["to_send"],
                     st["cached"], st["pairs_dropped"] or ""))
        tot = sum(s["to_send"] for s in stats_all)
        cch = sum(s["cached"] for s in stats_all)
        drop = sum(s["pairs_dropped"] for s in stats_all)
        print("\n%d session(s), %d pair(s) would be sent, %d already stored, "
              "%d dropped by the --max-pairs %d cap"
              % (len(stats_all), tot, cch, drop, args.max_pairs))
        # The cache is per model and per prompt version, so this column means
        # nothing without saying which. Reading it against the default model
        # while intending to run another is the easy way to misjudge a bill.
        print("`cached` counts verdicts already stored for %s / %s."
              % (args.model, PROMPT_VERSION))
        if args.count_tokens:
            toks, how = exact_tokens(work, args.model), "counted by the API"
        else:
            chars = sum(len(json.dumps(list(td.values()), ensure_ascii=False,
                                       indent=1, default=str)) + len(SYSTEM)
                        for _, _, td in work)
            toks, how = int(chars / CHARS_PER_TOKEN), "estimated"
        if toks is None:
            return 1
        print("\n~%s input tokens, %s  (%s $%.2f, %s $%.2f)"
              % ("{:,}".format(toks), how, TUNING_MODEL,
                 toks / 1e6 * PRICE_IN[TUNING_MODEL],
                 KEEPER_MODEL, toks / 1e6 * PRICE_IN[KEEPER_MODEL]))
        print("input only — output is small, one JSON array per session.")
        if not args.count_tokens:
            print("--count-tokens for the real figure (free API call, needs "
                  "credentials).")
        return 0

    if not work:
        print("nothing to send: %d session(s), every pair already has a "
              "verdict for %s / %s. --recheck to re-ask."
              % (len(wanted), args.model, PROMPT_VERSION))
        return 0

    if args.dry_run:
        for sid, _, todo in work:
            payload = list(todo.values())
            print("=" * 70)
            print("SESSION %s — %d candidate(s)" % (sid, len(payload)))
            print("=" * 70)
            print("SYSTEM (%s):\n%s\n" % (PROMPT_VERSION, SYSTEM))
            print("USER:\nSESSION: %s\n\nCANDIDATE PAIRS (%d):\n%s"
                  % (sid, len(payload),
                     json.dumps(payload, ensure_ascii=False, indent=1,
                                default=str)))
        return 0

    ensure_pairs_dir()
    warn = save_prompt_version()
    if warn:
        print(warn)
        return 1

    print("%d session(s) to judge with %s, prompt %s, pipeline %s"
          % (len(work), args.model, PROMPT_VERSION, psha))

    sys.path.insert(0, B.resync_home())
    import intercept as I           # reuse its venv + .env resolution
    anthropic = I.import_anthropic()
    if anthropic is None:
        print("anthropic SDK not importable — %s/.venv" % B.resync_home())
        return 1
    I.load_env_file()
    client = anthropic.Anthropic()

    def run_one(sid, todo):
        found, usage, raw = ask(client, args.model, sid, list(todo.values()))
        hits, leftover = attribute(found, todo)
        now = datetime.now(timezone.utc).isoformat()
        rows = [{
            "kind": "run", "ts": now, "session": sid, "model": args.model,
            "prompt_version": PROMPT_VERSION, "prompt_sha": prompt_sha(),
            "pipeline_sha": psha, "params": params,
            "pairs_sent": len(todo), "found": len(found),
            "unattributed": len(leftover),
            "usage": usage, "cost_usd": cost_of(args.model, usage),
            # Kept so a later schema change can be re-parsed rather than
            # re-purchased. It is the only irreplaceable thing in the file.
            "raw": raw,
        }]
        for pk, pl in todo.items():
            hit = hits.get(pk)
            c = pl["candidate"]
            rows.append({
                "kind": "verdict", "ts": now, "session": sid, "pair_key": pk,
                "model": args.model, "prompt_version": PROMPT_VERSION,
                "pipeline_sha": psha,
                "claim_a_at": c["claim_a"]["at"], "claim_b_at": c["claim_b"]["at"],
                "shared_terms": c["shared_terms"],
                # None is a real verdict — "judged, no contradiction" — and
                # storing it is what stops the pair being re-sent forever.
                "contradiction": hit[0] if hit else None,
                "matched": hit[1] if hit else None,
            })
        for f in leftover:
            rows.append({
                "kind": "verdict", "ts": now, "session": sid,
                "pair_key": None, "model": args.model,
                "prompt_version": PROMPT_VERSION, "pipeline_sha": psha,
                "contradiction": f, "matched": "unattributed"})
        append_rows(sid, rows)
        return sid, len(found), len(leftover), cost_of(args.model, usage)

    done, errors, spend = [], collections.Counter(), 0.0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_one, sid, todo): sid for sid, _, todo in work}
        for n, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            sid = futs[fut]
            try:
                r = fut.result()
                done.append(r)
                spend += r[3] or 0.0
            except Exception as exc:
                errors["%s: %s" % (type(exc).__name__, exc)] += 1
            print("  %d/%d" % (n, len(work)))

    found = sum(d[1] for d in done)
    unattr = sum(d[2] for d in done)
    print("\n%d session(s) written -> %s/<session>.jsonl"
          % (len(done), PAIRS_DIR))
    print("%d contradiction(s), %d unattributed, $%.2f spent"
          % (found, unattr, spend))
    if errors:
        print("errors: %s" % dict(errors))
        print("Re-run the same command: stored pairs are skipped, so only the "
              "failed sessions cost anything.")
    print("\nnext: --show to read them, --audit to check the quotes, "
          "--verify for spend")
    return 0


if __name__ == "__main__":
    sys.exit(main())
