#!/usr/bin/env python3
"""Read a session as a two-person chat, and find the inefficiencies in it.

    Input  (read-only):  <resync_home>/refined/   (via session.py)
    Output            :  <resync_home>/chat/<session>.jsonl

Companion to `pairs.py`, not a replacement. Pair-based testing asks *"was a stated
fact later shown false?"* and answers it well for the two categories with that
shape — C1 costly self-correction, B1 acting on an unverified premise. This
asks the questions that are about an *exchange* between two people, which is
most of `INEFFICIENCIES.md`.

The unit is a turn, and that is the whole point
-----------------------------------------------

The first attempt at this ran on raw assistant text blocks, and produced
findings that were 98% the assistant talking to itself. The cause was the unit,
not the prompt. Measured on the corpus:

    RAW      : 3702 assistant text blocks vs 528 developer messages   7.0 : 1
    AS TURNS :  527 assistant turns       vs 528 developer turns      1.00 : 1

Collapse contiguous assistant blocks into one turn — which is exactly what a
chat client does — and the transcript is a perfectly alternating two-party
conversation. The 7:1 is an artefact of the harness rendering interstitial
narration ("Let me check X", "Reading the file") as separate blocks. A person
does not send those as messages. Treating them as messages shredded one turn
into seven and then compared the fragments to each other.

So: contiguous same-speaker blocks collapse into one turn, and the unit of
analysis is the **exchange** — one developer turn plus the assistant turn that
answers it. 528 of them in this corpus, which is both the right shape and six
times cheaper than the pair-based approach (867k tokens against 5.19M).

The asymmetry is itself a finding, and it is free
--------------------------------------------------

    words per turn   developer  median  19   mean  32
                     assistant  median 461   mean 501   (15.6x)

In a chat between two people a 461-word reply to a 19-word question is a
problem on its own. That needs no model, so it is computed here and reported by
`--metrics`.

What it looks for
-----------------

Every finding names a category id from `INEFFICIENCIES.md`, so an instrument
can never again be credited with a category it did not test. The
exchange-shaped ones:

    A1  instruction dropped — part of the turn never acted on
    A2  misread — acted, on a different reading
    C3  scope overrun — work with no request behind it
    D1  illegible progress — the developer cannot tell what happened
    D3  not actionable — right content, wrong altitude
    E1  asked what it could have determined itself
    E2  did not ask when it should have

`severity` carries the cost axis the taxonomy insists on: a category with no
cost is noted, not counted as waste. `noticed` carries the other axis — whether
the developer ever raised it — because the unnoticed half is the half nothing
else can see.

What the assistant did, not only what it said
----------------------------------------------

Each exchange carries the reply's **actions** — every tool call issued in that
turn, with a normalised signature, its target and its outcome. That matters
most for A1: without it the question is only "did the reply *claim* to address
the request", which a confident reply passes whether or not the work happened.

The remaining limit is volume, not availability. Successful results are
summarised by size rather than quoted (a single session held 72,627 characters
of tool output against 26,138 of text), so a claim can be checked against
whether an action ran and succeeded, but not against what it printed. D2 —
asserting a state the tool output contradicts — needs those bodies and belongs
to its own view. Failed actions DO carry their output, since that is where the
contradictions cluster and the volume is small.

Usage:
    python3 chat.py --metrics                      # free, no API call
    python3 chat.py --exchanges --session a0c27    # what would be sent
    python3 chat.py --dry-run --session a0c27      # the exact payload, unsent
    python3 chat.py --session a0c27 0825c6b6       # some sessions
    python3 chat.py --all --model claude-opus-5    # every session
    python3 chat.py --show [--category A1]         # read the findings
    python3 chat.py --audit                        # are the quotes verbatim
    python3 chat.py --verify                       # integrity, and spend
"""

import argparse
import collections
import concurrent.futures
import glob
import hashlib
import json
import os
import statistics
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import backtest as B       # noqa: E402
import pairs as P          # noqa: E402  (cost maths, session selection)
import session as S        # noqa: E402  (the reader)
from plaintext import strip_decoration   # noqa: E402

CHAT_DIR = os.path.join(B.resync_home(), "chat")

TUNING_MODEL = P.TUNING_MODEL
KEEPER_MODEL = P.KEEPER_MODEL
NO_EFFORT_MODELS = P.NO_EFFORT_MODELS
PRICE = P.PRICE
CHARS_PER_TOKEN = P.CHARS_PER_TOKEN

# Bumped by hand when the prompt changes MEANING, so a typo fix costs nothing.
# Full text of each version lands in chat/prompts/<version>.txt.
PROMPT_VERSION = "v1"

# Context around the exchange. Two later developer turns are enough to see
# whether they raised it — which is the `noticed` axis — without dragging in
# the rest of the session.
LOOKAHEAD_TURNS = 2
PREV_CHARS = 400
TURN_CHARS = 6000
LATER_CHARS = 400
ERROR_CHARS = 300
MAX_ACTIONS = 40


# --------------------------------------------------------------------------
# turns and exchanges — deterministic, free
# --------------------------------------------------------------------------

def load(sid):
    """(turns, actions) for one session, straight from `refined/`.

    Turn collapsing lives in `session.py` because it is a format fact, not a
    policy: contiguous assistant text blocks ARE one turn, and the harness
    emitting narration as separate blocks is a rendering detail. Text is
    stripped here rather than there — `session.py` returns the record as it
    is, and lossiness belongs at the edge that leaves for the model."""
    evs = S.events(sid)
    ts = S.turns(evs)
    for t in ts:
        t["text"] = strip_decoration(t["text"])
        t["words"] = len(t["text"].split())
    return ts, S.actions(evs)


def exchanges(ts):
    """[(developer turn, assistant reply or None, index)] — the unit."""
    out = []
    for i, t in enumerate(ts):
        if t["who"] != "developer":
            continue
        reply = ts[i + 1] if i + 1 < len(ts) and ts[i + 1]["who"] == "assistant" else None
        out.append((t, reply, i))
    return out


def exchange_key(dev_text, reply_text):
    """Content-addressed identity, so a re-run pays only for what changed.

    Excludes model and prompt version deliberately — those index the cache
    alongside it, keeping the same exchange visibly the same across models."""
    blob = "%s\x1f%s" % (dev_text or "", reply_text or "")
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:16]


def build_payload(sid, ts, acts, ex):
    """One object per exchange, with just enough either side to judge it.

    Carries what the assistant DID as well as what it said. Until this read
    `refined/` an assistant turn was its text alone, so A1 was really "did the
    reply claim to address the request" rather than "was the request done".
    The action list closes most of that: signature, target and outcome per tool
    call, attributed to the turn that issued it."""
    dev, reply, i = ex
    prev = None
    for j in range(i - 1, -1, -1):
        if ts[j]["who"] == "assistant":
            prev = ts[j]
            break
    later = [t for t in ts[i + 2:] if t["who"] == "developer"][:LOOKAHEAD_TURNS]
    work = [a for a in acts if reply and a["turn"] == reply["turn"]]
    return {
        "session_id": sid,
        "exchange": {
            "developer": {"at": dev["at"], "words": dev["words"],
                          "text": dev["text"][:TURN_CHARS]},
            "assistant": ({"at": reply["at"], "words": reply["words"],
                           "text": reply["text"][:TURN_CHARS]}
                          if reply else None),
        },
        # What the reply actually did. `sig` is normalised so a repeated
        # attempt is visible as a repeat; failures carry their output.
        "assistant_actions": [
            {"tool": a["tool"], "action": a["sig"][:160], "target": a["target"],
             "outcome": a["outcome"],
             "output": (a["result_head"][:ERROR_CHARS]
                        if a["outcome"] != "ok" else None)}
            for a in work[:MAX_ACTIONS]],
        "assistant_actions_total": len(work),
        "context_before": ({"at": prev["at"],
                            "text": prev["text"][:PREV_CHARS]} if prev else None),
        "developer_turns_after": [{"at": t["at"], "text": t["text"][:LATER_CHARS]}
                                  for t in later],
    }


# --------------------------------------------------------------------------
# the prompt
# --------------------------------------------------------------------------

CATEGORIES = ("A1", "A2", "C3", "D1", "D3", "E1", "E2")

SYSTEM = """\
You are analysing one exchange from a chat between a developer and an AI coding
assistant, the way you would review a conversation between two colleagues in a
Teams thread. You are looking for inefficiency in their collaboration: effort
wasted because the two were working from different understandings.

You will be shown the developer's message, the assistant's reply, any tool
failures inside that reply, a little context before, and the developer's next
one or two messages.

REPORT ONLY THESE CATEGORIES, by id:

A1  INSTRUCTION DROPPED. The developer's message contained a distinct request,
    question or constraint that the reply never addresses. This is the most
    important category and the easiest to miss, because it is an ABSENCE.
    Enumerate the requests in the message first, then check each against the
    reply. A request deferred explicitly ("I'll do X after Y") is NOT dropped.
    A request the reply simply never mentions IS dropped, whether or not the
    developer noticed.

A2  MISREAD. The reply acts, but on a different reading of the request than
    the words support. Distinct from A1: something was done, just not the
    thing asked for.

C3  SCOPE OVERRUN. The reply does substantial work the developer did not ask
    for and would not obviously want. Not: work that is a necessary part of
    what was asked. Not: a brief aside clearly flagged as optional.

D1  ILLEGIBLE PROGRESS. After reading the reply, a reasonable developer still
    could not say what state things are in, what was done, or what happens
    next. Judge the reply as written, not the work behind it.

D3  NOT ACTIONABLE. The content is right but the developer cannot act on it —
    pitched at the wrong altitude, burying the answer, or ending without a
    clear result or next step. A 500-word reply to a 15-word question is a
    candidate, though length alone is not the test.

E1  ASKED WHAT IT COULD HAVE DETERMINED. The reply puts a question to the
    developer that the assistant could have answered from the repository, the
    files, or a command. Costs a round trip for nothing.

E2  DID NOT ASK WHEN IT SHOULD HAVE. The reply commits to a consequential
    choice that was genuinely ambiguous, without asking, and without flagging
    the assumption.

DO NOT REPORT:
- The assistant being verbose, unless it crosses into D1 or D3.
- A factual claim that later turns out wrong. That is a different instrument's
  job; unless the error was avoidable within THIS exchange, leave it.
- Ordinary iteration. A developer refining a request across turns is
  collaboration working, not waste.
- Anything you would have to speculate about work you cannot see. If the reply
  says it ran the tests and you have no evidence either way, that is not a
  finding.

TWO FIELDS THAT DECIDE WHETHER A FINDING MATTERS

`severity` — did this cost anything?
  cost      real waste followed: work redone, a wrong path taken, the developer
            had to re-ask or correct, or a round trip was spent for nothing
  friction  it made the exchange worse but nothing had to be redone
  none      you noticed it but it cost nothing. Report it as none rather than
            inflating it.

`noticed` — did the developer raise it in their next messages? Look at
`developer_turns_after`. Set false when they did not. False is the more
valuable finding: it means nothing else in this project can see it.

`evidence_quote` must be copied VERBATIM from the exchange, and must be the
span that shows the problem. For A1, quote the request that went unanswered,
from the developer's message. Copy, do not retype, and do not add formatting
that is not there.

Most exchanges are fine. Returning an empty list is the expected answer and a
useful one. Do not manufacture a weak finding to avoid returning nothing.\
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "severity": {"type": "string",
                                 "enum": ["cost", "friction", "none"]},
                    "noticed": {"type": "boolean"},
                    "evidence_quote": {"type": "string"},
                    "what_was_asked": {"type": "string"},
                    "what_happened": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["category", "severity", "noticed",
                             "evidence_quote", "what_was_asked",
                             "what_happened", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["findings"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------
# storage — same discipline as pairs.py, separate directory
# --------------------------------------------------------------------------

def ensure_dir():
    os.makedirs(CHAT_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(CHAT_DIR, 0o700)
    except OSError:
        pass
    gi = os.path.join(CHAT_DIR, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as fh:
            fh.write("# Auto-created by chat.py.\n# Quotes developer and "
                     "assistant text verbatim — never commit it.\n*\n")


def save_prompt_version():
    d = os.path.join(CHAT_DIR, "prompts")
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, "%s.txt" % PROMPT_VERSION)
    if os.path.exists(path):
        with open(path, errors="replace") as fh:
            if fh.read() != SYSTEM:
                return ("MISMATCH: %s differs from the current SYSTEM. Bump "
                        "PROMPT_VERSION, or the cache serves verdicts from the "
                        "old wording." % path)
        return None
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(SYSTEM)
    os.chmod(path, 0o600)
    return None


def chat_path(sid):
    return os.path.join(CHAT_DIR, "%s.jsonl" % sid)


def views_dir():
    """<resync_home>/views/chat — the deterministic view, not the verdict.

    A view regenerates for free; a verdict was paid for. Separate directories
    so a re-extraction can never overwrite something expensive."""
    d = os.path.join(B.resync_home(), "views", "chat")
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

    Write-only: nothing reads it. That is what separates it from the old
    `friction/` files, which were a dependency and so limited what every
    consumer could see."""
    d = views_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, "%s.jsonl" % sid)
    head = {"kind": "header", "session": sid, "exchanges": len(payload),
            "prompt_version": PROMPT_VERSION, "pipeline_sha": psha,
            "params": params}
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(head, ensure_ascii=False, default=str) + "\n")
        for p in payload:
            fh.write(json.dumps(p, ensure_ascii=False, default=str) + "\n")
    os.chmod(path, 0o600)
    return path


def chat_sessions():
    return sorted(os.path.basename(p)[:-6]
                  for p in glob.glob(os.path.join(CHAT_DIR, "*.jsonl")))


def load_rows(sid):
    p = chat_path(sid)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, errors="replace") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def load_judged(sid, model=None, version=PROMPT_VERSION):
    got = {}
    for r in load_rows(sid):
        if r.get("kind") != "exchange":
            continue
        if r.get("prompt_version") != version:
            continue
        if model and r.get("model") != model:
            continue
        got[r.get("exchange_key")] = r
    return got


def append_rows(sid, rows):
    if not rows:
        return
    ensure_dir()
    p = chat_path(sid)
    with open(p, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    os.chmod(p, 0o600)


# --------------------------------------------------------------------------
# the contract judge.py reads
# --------------------------------------------------------------------------

MODE = "unit"           # one call per exchange
UNIT_LABEL = "EXCHANGE"


def unit_key(u):
    """The key the view was built with, not one recomputed from stored text."""
    if u.get("unit_key"):
        return u["unit_key"]
    ex = u.get("exchange") or {}
    return exchange_key((ex.get("developer") or {}).get("text"),
                        (ex.get("assistant") or {}).get("text"))


def rows_from(data, todo, base):
    """One row per exchange judged. An empty findings list is a real verdict —
    storing it is what stops the exchange being re-sent forever."""
    rows = []
    for k, u in todo.items():
        rows.append(dict(base, kind="verdict", unit_key=k,
                         developer_at=((u.get("exchange") or {})
                                       .get("developer") or {}).get("at"),
                         findings=(data or {}).get("findings") or []))
    return rows


def pipeline_params():
    try:
        with open(os.path.join(HERE, "plaintext.py"), "rb") as fh:
            nsha = hashlib.sha1(fh.read()).hexdigest()[:12]
    except OSError:
        nsha = None
    return {"lookahead_turns": LOOKAHEAD_TURNS, "turn_chars": TURN_CHARS,
            "prev_chars": PREV_CHARS, "later_chars": LATER_CHARS,
            "error_chars": ERROR_CHARS, "max_actions": MAX_ACTIONS,
            "plaintext_sha": nsha}


def pipeline_sha(params=None):
    return hashlib.sha1(json.dumps(params or pipeline_params(), sort_keys=True)
                        .encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# the call
# --------------------------------------------------------------------------

def ask(client, model, payload):
    user = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
    fmt = {"format": {"type": "json_schema", "schema": SCHEMA}}
    if not model.startswith(NO_EFFORT_MODELS):
        fmt["effort"] = "medium"
    resp = client.messages.create(
        model=model, max_tokens=8000, system=SYSTEM,
        messages=[{"role": "user", "content": user}], output_config=fmt)
    if resp.stop_reason == "refusal":
        raise RuntimeError("refused")
    blob = next(b.text for b in resp.content
                if getattr(b, "type", None) == "text")
    data = json.loads(blob)
    u = resp.usage
    usage = {"input_tokens": getattr(u, "input_tokens", 0),
             "output_tokens": getattr(u, "output_tokens", 0)}
    return data.get("findings") or [], usage, data


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def norm(s):
    return " ".join((s or "").split()).lower()


def all_findings(sids=None, model=None, category=None, severity=None):
    out = []
    for sid in (sids or chat_sessions()):
        seen = {}
        for r in load_rows(sid):
            if r.get("kind") != "exchange":
                continue
            if model and r.get("model") != model:
                continue
            seen[r.get("exchange_key")] = r      # newest wins
        for r in seen.values():
            for f in r.get("findings") or []:
                if category and f.get("category") != category:
                    continue
                if severity and f.get("severity") != severity:
                    continue
                out.append((sid, r, f))
    return out


def metrics(sids=None):
    """Everything computable without a model. Free, and it is not nothing."""
    dev_w, ast_w, ratios, blocks, n_ex = [], [], [], 0, 0
    unanswered = 0
    for sid in (sids or S.sessions()):
        ts, _acts = load(sid)
        blocks += sum(1 for e in S.events(sid)
                      if e.get("kind") == "assistant_text")
        for dev, reply, _ in exchanges(ts):
            n_ex += 1
            dev_w.append(dev["words"])
            if reply is None:
                unanswered += 1
                continue
            ast_w.append(reply["words"])
            if dev["words"]:
                ratios.append(reply["words"] / dev["words"])
    print("exchanges: %d across %d session(s)" % (n_ex, len(sids or S.sessions())))
    print("turns    : %d assistant, %d developer  (%.2f:1)"
          % (len(ast_w), len(dev_w), len(ast_w) / max(1, len(dev_w))))
    print("raw text blocks the harness emitted: %d  (%.1f per assistant turn)"
          % (blocks, blocks / max(1, len(ast_w))))
    print("\nwords per turn:")
    print("   developer  median %5d  mean %6.0f"
          % (statistics.median(dev_w), statistics.mean(dev_w)))
    print("   assistant  median %5d  mean %6.0f"
          % (statistics.median(ast_w), statistics.mean(ast_w)))
    print("   asymmetry  %.1fx more words per assistant turn"
          % (statistics.mean(ast_w) / statistics.mean(dev_w)))
    ratios.sort()
    print("\nreply-to-request word ratio: median %.0fx, p90 %.0fx, max %.0fx"
          % (statistics.median(ratios), ratios[int(len(ratios) * .9)], ratios[-1]))
    print("exchanges where the developer got no assistant reply at all: %d"
          % unanswered)
    print("\nNone of the above needs a model. D1/D3 candidates start here.")
    return 0


def show(sids=None, category=None, severity=None, unnoticed=False):
    rows = all_findings(sids, category=category, severity=severity)
    if unnoticed:
        rows = [r for r in rows if not r[2].get("noticed")]
    if not rows:
        print("no findings stored" + (" for that filter" if category or severity
                                      or unnoticed else ""))
        return 0
    order = {"cost": 0, "friction": 1, "none": 2}
    rows.sort(key=lambda r: (order.get(r[2].get("severity"), 3),
                             r[2].get("category")))
    by_cat = collections.Counter(f["category"] for _, _, f in rows)
    by_sev = collections.Counter(f["severity"] for _, _, f in rows)
    unn = sum(1 for _, _, f in rows if not f.get("noticed"))
    print("%d finding(s) across %d session(s)" % (rows and len(rows),
                                                  len({r[0] for r in rows})))
    print("by category: %s" % dict(by_cat))
    print("by severity: %s   unnoticed by the developer: %d\n" % (dict(by_sev), unn))
    for sid, r, f in rows:
        print("%-9s %-3s %-9s %s" % (sid[:8], f["category"], f["severity"],
                                     "" if f.get("noticed") else "[unnoticed]"))
        print("     asked : %s" % " ".join((f.get("what_was_asked") or "").split())[:92])
        print("     got   : %s" % " ".join((f.get("what_happened") or "").split())[:92])
        print("     quote : %s" % " ".join((f.get("evidence_quote") or "").split())[:92])
        print()
    return 0


def audit(sids=None):
    """Is every evidence_quote actually in the exchange it came from?

    Compared after stripping both sides: the model re-decorates its own quotes
    with markdown it was never shown (measured twice in pairs.py's runs), and
    failing a sound finding for that would be the pipeline blaming the model
    for its own formatting."""
    ok = bad = 0
    misses = []
    for sid in (sids or chat_sessions()):
        hay = norm(strip_decoration(" ".join(
            (e.get("text") or "") for e in S.events(sid))))
        for _, r, f in all_findings([sid]):
            q = norm(strip_decoration(f.get("evidence_quote") or ""))
            if q and q in hay:
                ok += 1
            else:
                bad += 1
                misses.append((sid, f))
    n = ok + bad
    print("quotes: %d verbatim, %d not found" % (ok, bad))
    if n:
        print("verbatim rate: %.0f%%" % (100.0 * ok / n))
    for sid, f in misses[:10]:
        print("\n  %s [%s] %s" % (sid[:8], f.get("category"), f.get("severity")))
        print("    %s" % (f.get("evidence_quote") or "")[:110])
    if bad:
        print("\nA quote not in the exchange means the finding cannot be "
              "checked. Treat those\nas unconfirmed, and the prompt as needing "
              "work.")
    return 0


def verify(sids=None):
    sids = sids or chat_sessions()
    runs = ex = bad = 0
    spend = collections.Counter()
    current = pipeline_sha()
    stale = 0
    for sid in sids:
        if sid not in set(S.sessions()):
            print("  no transcript in refined/ for %s" % sid[:8])
        for r in load_rows(sid):
            if r.get("kind") == "run":
                runs += 1
                if r.get("pipeline_sha") != current:
                    stale += 1
                if r.get("cost_usd"):
                    spend[r.get("model")] += r["cost_usd"]
            elif r.get("kind") == "exchange":
                ex += 1
                for f in r.get("findings") or []:
                    if f.get("category") not in CATEGORIES:
                        bad += 1
            else:
                bad += 1
    print("%d session file(s): %d run(s), %d exchange(s) judged"
          % (len(sids), runs, ex))
    print("rows failing their schema: %d" % bad)
    print("runs from a superseded pipeline: %d (current %s)" % (stale, current))
    if spend:
        print("\nspent so far:")
        for m, c in spend.most_common():
            print("   %-20s $%.2f" % (m, c))
        print("   %-20s $%.2f" % ("TOTAL", sum(spend.values())))
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--session", nargs="+", metavar="ID",
                    help="one or more sessions (id prefixes are enough)")
    ap.add_argument("--all", action="store_true", help="every session")
    ap.add_argument("--metrics", action="store_true",
                    help="turn and asymmetry figures. Free, no API call")
    ap.add_argument("--exchanges", action="store_true",
                    help="what would be sent, and what it costs. No API call")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the exact payload, unsent")
    ap.add_argument("--write", action="store_true",
                    help="store the exact payload per session and stop. "
                         "Deterministic, no API call, no cost")
    ap.add_argument("--model", default=TUNING_MODEL,
                    help="default %s; use %s for the pass you keep"
                         % (TUNING_MODEL, KEEPER_MODEL))
    ap.add_argument("--recheck", action="store_true",
                    help="re-ask exchanges that already have a verdict")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--category", choices=CATEGORIES,
                    help="with --show: only this INEFFICIENCIES.md category")
    ap.add_argument("--severity", choices=("cost", "friction", "none"),
                    help="with --show: only findings at this severity")
    ap.add_argument("--unnoticed", action="store_true",
                    help="with --show: only what the developer never raised")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)

    if args.show or args.audit or args.verify:
        have = chat_sessions()
        sids = P.select(have, args.session) if args.session else None
        if args.verify:
            return verify(sids)
        if args.audit:
            return audit(sids)
        return show(sids, args.category, args.severity, args.unnoticed)

    have = S.sessions()
    if not have:
        print("no sessions in %s — run reduce.py first" % S.REFINED)
        return 1
    if args.session:
        wanted = P.select(have, args.session)
    elif args.all or args.metrics or args.exchanges or args.write:
        wanted = have
    else:
        print("pick a scope: --session <id> [...] or --all.")
        print("--metrics and --exchanges cost nothing.")
        return 2

    if args.metrics:
        return metrics(wanted)

    params = pipeline_params()
    psha = pipeline_sha(params)
    work = []
    stats = []
    for sid in wanted:
        ts, acts = load(sid)
        keyed = {}
        for ex in exchanges(ts):
            dev, reply, _ = ex
            k = exchange_key(dev["text"], (reply or {}).get("text"))
            # Stamped into the unit rather than recomputed later. The payload
            # truncates turn text at TURN_CHARS, so a key derived from the
            # STORED unit differs from one derived from the source for any
            # exchange longer than that — 3 of 30 in the first session judged.
            # Carrying the key removes the class of bug entirely.
            keyed[k] = dict(build_payload(sid, ts, acts, ex), unit_key=k)
        judged = {} if args.recheck else load_judged(sid, args.model,
                                                     PROMPT_VERSION)
        todo = {k: v for k, v in keyed.items() if k not in judged}
        stats.append((sid, len(keyed), len(todo)))
        if todo:
            work.append((sid, todo))

    if args.write:
        ensure_dir()
        n = 0
        for sid in wanted:
            ts, acts = load(sid)
            pl = [build_payload(sid, ts, acts, ex) for ex in exchanges(ts)]
            write_payload(sid, pl, params, psha)
            n += len(pl)
        print("%d session(s), %d exchange(s) -> %s/<session>.jsonl"
              % (len(wanted), n, views_dir()))
        print("pipeline %s, prompt %s. Nothing sent." % (psha, PROMPT_VERSION))
        return 0

    if args.exchanges:
        print("%-9s %10s %8s" % ("session", "exchanges", "to send"))
        print("-" * 30)
        for sid, tot, td in sorted(stats, key=lambda s: -s[1]):
            print("%-9s %10d %8d" % (sid[:8], tot, td))
        chars = sum(len(json.dumps(p, ensure_ascii=False, indent=1,
                                   default=str)) + len(SYSTEM)
                    for _, td in work for p in td.values())
        toks = int(chars / CHARS_PER_TOKEN)
        print("\n%d exchange(s) to send, ~%s input tokens estimated"
              % (sum(s[2] for s in stats), "{:,}".format(toks)))
        print("   %-18s $%.2f\n   %-18s $%.2f"
              % (TUNING_MODEL, toks / 1e6 * PRICE[TUNING_MODEL][0],
                 KEEPER_MODEL, toks / 1e6 * PRICE[KEEPER_MODEL][0]))
        print("`to send` counts exchanges without a verdict for %s / %s."
              % (args.model, PROMPT_VERSION))
        return 0

    if not work:
        print("nothing to send: every exchange already judged for %s / %s. "
              "--recheck to re-ask." % (args.model, PROMPT_VERSION))
        return 0

    if args.dry_run:
        for sid, todo in work:
            for p in list(todo.values())[:3]:
                print("=" * 70)
                print("SYSTEM (%s) — %d chars\n" % (PROMPT_VERSION, len(SYSTEM)))
                print("USER:\n%s" % json.dumps(p, ensure_ascii=False, indent=1,
                                               default=str))
            print("\n(%d more exchange(s) in %s not printed)"
                  % (max(0, len(todo) - 3), sid[:8]))
        return 0

    ensure_dir()
    warn = save_prompt_version()
    if warn:
        print(warn)
        return 1

    total = sum(len(td) for _, td in work)
    print("%d exchange(s) across %d session(s), %s, prompt %s, pipeline %s"
          % (total, len(work), args.model, PROMPT_VERSION, psha))

    sys.path.insert(0, B.resync_home())
    import intercept as I
    anthropic = I.import_anthropic()
    if anthropic is None:
        print("anthropic SDK not importable — %s/.venv" % B.resync_home())
        return 1
    I.load_env_file()
    client = anthropic.Anthropic()

    def run_one(sid, key, payload):
        findings, usage, raw = ask(client, args.model, payload)
        now = datetime.now(timezone.utc).isoformat()
        return sid, {
            "kind": "exchange", "ts": now, "session": sid,
            "exchange_key": key, "model": args.model,
            "prompt_version": PROMPT_VERSION, "pipeline_sha": psha,
            "developer_at": payload["exchange"]["developer"]["at"],
            "findings": findings, "usage": usage,
            "cost_usd": P.cost_of(args.model, usage), "raw": raw,
        }

    jobs = [(sid, k, p) for sid, td in work for k, p in td.items()]
    got, errors, spend = collections.defaultdict(list), collections.Counter(), 0.0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_one, *j): j for j in jobs}
        for n, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            try:
                sid, row = fut.result()
                got[sid].append(row)
                spend += row["cost_usd"] or 0.0
            except Exception as exc:
                errors["%s: %s" % (type(exc).__name__, exc)] += 1
            if n % 10 == 0 or n == len(jobs):
                print("  %d/%d" % (n, len(jobs)))

    found = 0
    for sid, rows in got.items():
        run = {"kind": "run", "ts": datetime.now(timezone.utc).isoformat(),
               "session": sid, "model": args.model,
               "prompt_version": PROMPT_VERSION, "pipeline_sha": psha,
               "params": params, "exchanges": len(rows),
               "findings": sum(len(r["findings"]) for r in rows),
               "usage": {"input_tokens": sum(r["usage"]["input_tokens"] for r in rows),
                         "output_tokens": sum(r["usage"]["output_tokens"] for r in rows)},
               "cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4)}
        found += run["findings"]
        append_rows(sid, [run] + rows)

    print("\n%d session file(s) written -> %s/<session>.jsonl"
          % (len(got), CHAT_DIR))
    print("%d finding(s), $%.2f spent" % (found, spend))
    if errors:
        print("errors: %s" % dict(errors))
        print("Re-run the same command: judged exchanges are skipped.")
    print("\nnext: --show, --show --unnoticed, --audit, --verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
