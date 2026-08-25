#!/usr/bin/env python3
"""D2: the assistant asserting a state its own tool output contradicts.

    Input  (read-only):  <resync_home>/refined/   (via session.py)
    View               :  <resync_home>/views/evidence/<session>.jsonl
    Output             :  <resync_home>/evidence/<session>.jsonl

The largest acknowledged gap in the catalogue. `rules.json` has recorded it as
an open gap since before any of this existed, with two observed cases:

    "17 tests, all green"        — against a dev server it had just broken
    "still running, log ticking" — against a 3.5-minute-stale log

Nothing has ever measured it, because every other instrument reads speech.
`pairs.py` compares a claim to a later claim; `chat.py` compares a request to a
reply. D2 is a claim against *its own evidence*, inside one turn, and needs the
7,693 tool results that speech does not mention.

The funnel, and why each step is there
---------------------------------------

Measured on 56 sessions. Every step removes something that would otherwise be
reported as a finding and is not one:

    3702  assistant claims
     430  assert an outcome                     (12%)
     171  ...with tool evidence that failed
     104  ...failure is not harness noise
      43  ...and the claim does not acknowledge it

**Assert an outcome.** A claim that narrates — "let me check X", "reading the
file" — asserts no state, so nothing can contradict it. 88% of claims are in
that category and the vocabulary below is what separates them.

**Not harness noise.** `<tool_use_error>File has not been read yet` is the
assistant misusing a tool and immediately retrying. An auto-mode block and a
permission prompt are the settings file talking. None of them says anything
about whether the claim was true, which is the same reason `INEFFICIENCIES.md`
excludes `permission-rule` denials from friction counts. This step removes 67
of 171 — the single largest cut, and every one of them would have read as a
finding.

**Claim does not acknowledge it.** "Stack is up (one pre-existing mysql
failure)" is honest reporting, not a contradiction — the assistant said the
failure out loud. Removing these takes 104 to 43, so more than half of what
survived the earlier steps was the assistant being straight about a problem.

What is left is a *candidate*, never a finding. A failure elsewhere in the same
turn is not necessarily about the claim, and deciding whether it is is exactly
the judgement a regex cannot make. That is what the model is for.

Carrying the evidence without carrying the corpus
--------------------------------------------------

Evidence is the volume problem in this whole project. Carried whole, the tool
output behind these candidates runs to millions of bytes — one claim alone sat
behind 577,819. Failures kept (capped at 700 chars) and successes reduced to a
single line each brings the whole corpus's candidates to **0.3 MB, about
142,000 tokens**, because a successful `Read` of a 60KB file says nothing
except that the file was read, while a failure's text is the entire point.

Usage:
    python3 evidence.py --funnel                 # the counts above, free
    python3 evidence.py --candidates             # list them, free
    python3 evidence.py --write                  # store the view, free
    python3 evidence.py --dry-run --session a0c27  # exact payload, unsent
    python3 evidence.py --session a0c27           # judge one session
"""

import argparse
import collections
import concurrent.futures
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

import session as S       # noqa: E402
import pairs as P         # noqa: E402  (cost maths, model constants)
from plaintext import strip_decoration   # noqa: E402

VIEW_DIR = os.path.join(S.home(), "views", "evidence")
OUT_DIR = os.path.join(S.home(), "evidence")
PROMPT_VERSION = "v1"

TUNING_MODEL = P.TUNING_MODEL
KEEPER_MODEL = P.KEEPER_MODEL
NO_EFFORT_MODELS = P.NO_EFFORT_MODELS

# A claim asserting a checkable outcome. Deliberately a closed vocabulary
# rather than a model call: this is the cheap step whose only job is to get
# 3,702 claims down to a few hundred worth reading.
ASSERT = re.compile(
    r"\b(all (?:green|pass(?:ing|ed)?|clean)|tests? (?:pass|passed|passing|are green)"
    r"|(?:is|are|now) (?:running|live|up|working|fixed|clean|green|done|passing)"
    r"|no errors?|zero (?:errors?|failures?)|succeeded|success(?:ful)?ly"
    r"|(?:it|that|this) works|works now|verified|confirmed working"
    r"|lint clean|build (?:succeeded|passes|is clean)|tsc (?:ok|clean))\b", re.I)

# The claim owning up to a problem. Honest reporting is not a contradiction,
# and this removed more candidates than any other step.
ACK = re.compile(
    r"\b(fail(?:ed|ing|ure)s?|error|broken|except|apart from|other than"
    r"|still (?:not|isn't)|pre-existing|couldn't|could not|didn't|did not"
    r"|unable|caveat|one issue|problem)\b", re.I)

# Harness mechanics, not world state.
PROTOCOL = re.compile(
    r"<tool_use_error>|has not been read yet|String to replace not found"
    r"|no such tool|InputValidationError|auto mode could not evaluate"
    r"|blocking it for safety|user doesn't want to proceed"
    # A permission prompt is the settings file talking, not the world. Same
    # exclusion `INEFFICIENCIES.md` makes for `permission-rule` denials: it is
    # time lost to configuration, and counting it here would report the
    # allowlist as a contradicted claim.
    r"|permission to use \w+ with command|requested permissions", re.I)

FAIL_CHARS = 700        # a failure's text is the point; cap it, do not drop it
OK_LINE = 90            # a success reduces to one line
CLAIM_CHARS = 1500
ASK_CHARS = 600
MAX_EVIDENCE = 30


def ensure(d):
    os.makedirs(d, mode=0o700, exist_ok=True)
    parent = os.path.dirname(d)
    gi = os.path.join(parent, ".gitignore")
    if os.path.basename(parent) == "views" and not os.path.exists(gi):
        with open(gi, "w") as fh:
            fh.write("# Auto-created. Deterministic views of the corpus, "
                     "verbatim — never commit.\n*\n")
    if os.path.basename(parent) != "views":
        gi = os.path.join(d, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w") as fh:
                fh.write("# Auto-created by evidence.py. Quotes tool output "
                         "verbatim — never commit.\n*\n")
    return d


def candidates(sid, funnel=None):
    """Asserting claims whose own turn holds an unacknowledged real failure."""
    evs = S.events(sid)
    acts = {a["seq"]: a for a in S.actions(evs)}
    results = collections.defaultdict(list)
    for e in evs:
        if e["kind"] == "tool_result":
            results[e["turn"]].append(e)
    # The developer turn that prompted each assistant turn.
    asked = {}
    last_dev = None
    for e in evs:
        if e["kind"] == "developer" and not e.get("meta") and not e.get("machine"):
            last_dev = e
        elif e["kind"] == "assistant_text" and last_dev is not None:
            asked.setdefault(e["turn"], last_dev)

    out = []
    for e in evs:
        if e["kind"] != "assistant_text":
            continue
        text = strip_decoration(e["text"])
        if funnel is not None:
            funnel["1 claims"] += 1
        if not ASSERT.search(text):
            continue
        if funnel is not None:
            funnel["2 assert an outcome"] += 1
        ev = [r for r in results[e["turn"]] if r["seq"] < e["seq"]]
        fails = [r for r in ev if not r["ok"]]
        if not fails:
            continue
        if funnel is not None:
            funnel["3 with a failure in turn"] += 1
        real = [r for r in fails if not PROTOCOL.search(r["text"] or "")]
        if not real:
            continue
        if funnel is not None:
            funnel["4 failure is not protocol noise"] += 1
        if ACK.search(text):
            continue
        if funnel is not None:
            funnel["5 claim does not acknowledge it"] += 1
        # Two windows, both kept. Scoping evidence to the whole turn finds 43
        # candidates but attributes 11 failures to more than one claim — a long
        # turn's single failure lands on every assertion after it. Scoping to
        # "since the previous claim" over-corrects: 7 candidates, no
        # over-attribution, and it loses every summary claim that legitimately
        # covers a whole turn's work. So the wider net is kept and each result
        # is LABELLED with which window it fell in, because relevance is the
        # model's judgement and this is the fact it needs to make it.
        since = {r["seq"] for r in S.evidence_before(evs, e["seq"])}
        out.append({"claim": e, "evidence": ev[-MAX_EVIDENCE:], "failures": real,
                    "since": since, "asked": asked.get(e["turn"]), "acts": acts})
    return out


def claim_key(claim_text, fail_texts):
    blob = "%s\x1f%s" % (claim_text or "", "\x1f".join(fail_texts))
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:16]


def build_payload(sid, c):
    """A claim, and what its own turn actually produced.

    Failures carry their output because that is the thing that might contradict
    the claim. Successes carry only that they succeeded: a `Read` that returned
    60KB says nothing except that the file was read, and carrying it would make
    the payload thirteen times larger for no judgement."""
    e = c["claim"]
    acts = c["acts"]

    def line(r):
        a = None
        for seq in range(r["seq"] - 1, -1, -1):
            if seq in acts and acts[seq].get("tool") == r.get("tool"):
                a = acts[seq]
                break
        base = {"tool": r.get("tool"), "outcome": "ok" if r["ok"] else "error",
                "action": (a or {}).get("sig", "")[:160],
                "result_bytes": r["bytes"],
                # True: the assistant had just seen this. False: earlier in the
                # same turn, and it may already have accounted for it.
                "since_last_statement": r["seq"] in c["since"]}
        if not r["ok"]:
            base["output"] = (r["text"] or "")[:FAIL_CHARS]
        return base

    return {
        "session_id": sid,
        "claim": {"at": e["at"], "turn": e["turn"],
                  "text": strip_decoration(e["text"])[:CLAIM_CHARS]},
        "developer_asked": ({"at": c["asked"]["at"],
                             "text": strip_decoration(c["asked"]["text"])[:ASK_CHARS]}
                            if c["asked"] else None),
        "evidence_in_the_same_turn": [line(r) for r in c["evidence"]],
        "failures": len(c["failures"]),
    }


SYSTEM = """\
You are checking one claim an AI coding assistant made against the tool output
it had already seen when it made it. You are looking for a single thing: the
assistant asserting a state that its own evidence contradicts.

You will be shown the developer's request, the assistant's claim, and every
tool result from that same turn that preceded the claim. Successful results are
listed by name only; failed ones carry their output, because that is what might
contradict the claim.

`since_last_statement` tells you how immediate a result was. True means the
assistant had JUST seen it, with nothing said in between — the strongest case.
False means it appeared earlier in the same turn, and the assistant may already
have dealt with it in something it said before this claim, which you cannot
see. Weigh a false one more carefully before calling it a contradiction.

IT IS A CONTRADICTION when the claim asserts something the evidence in front of
it shows to be untrue. "All 51 tests pass" when the test command exited
non-zero. "The service is running" when the command to start it errored. "Fixed"
when the edit that was supposed to fix it failed.

IT IS NOT A CONTRADICTION when:
- The failure is about something else. A turn can hold many actions, and a
  failure unrelated to what the claim asserts contradicts nothing. This is the
  most common false positive here — check that the failing action and the claim
  are about the same thing before you log anything.
- The assistant already accounted for the failure, even briefly.
- The failing action was retried and then succeeded within the same evidence.
- The claim is about a different scope than the failure: "the frontend builds"
  is not contradicted by a backend test failing.
- The evidence is inconclusive. If you cannot tell whether the failure bears on
  the claim, that is not a contradiction; say so and move on.

BOTH QUOTES MUST BE VERBATIM. `claim_quote` is the exact sentence asserting the
state, copied from the claim. `evidence_quote` is the exact text from the tool
output that contradicts it. Copy, do not retype, and do not add formatting that
is not there. If you cannot produce a real evidence quote, you do not have a
contradiction.

`severity`: `cost` if the assistant carried on as though the claim were true,
or told the developer it was; `friction` if it was caught immediately after;
`none` if it made no difference.

`noticed`: whether the developer would have had any way to know from what they
were shown. False is the more valuable finding — an assertion contradicted by
evidence the developer never saw is invisible to every other instrument.

Most claims here will not be contradictions; the filter that selected them is
deliberately loose. Returning `contradiction: false` is the expected answer.\
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "contradiction": {"type": "boolean"},
        "claim_quote": {"type": "string"},
        "evidence_quote": {"type": "string"},
        "what_was_claimed": {"type": "string"},
        "what_the_evidence_showed": {"type": "string"},
        "severity": {"type": "string", "enum": ["cost", "friction", "none"]},
        "noticed": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["contradiction", "claim_quote", "evidence_quote",
                 "what_was_claimed", "what_the_evidence_showed", "severity",
                 "noticed", "reason"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------
# the contract judge.py reads
# --------------------------------------------------------------------------

MODE = "unit"           # one call per candidate claim
UNIT_LABEL = "CLAIM AND ITS EVIDENCE"


def unit_key(u):
    """The key the view was built with, not one recomputed from stored text."""
    if u.get("unit_key"):
        return u["unit_key"]
    fails = [e.get("output") or "" for e in
             u.get("evidence_in_the_same_turn") or []
             if e.get("outcome") != "ok"]
    return claim_key((u.get("claim") or {}).get("text"), fails)


def rows_from(data, todo, base):
    rows = []
    for k, u in todo.items():
        rows.append(dict(base, kind="verdict", unit_key=k,
                         claim_at=(u.get("claim") or {}).get("at"),
                         contradiction=(data or {}).get("contradiction"),
                         verdict=data))
    return rows


def write_view(sid, payloads):
    d = ensure(VIEW_DIR)
    path = os.path.join(d, "%s.jsonl" % sid)
    head = {"kind": "header", "session": sid, "candidates": len(payloads),
            "prompt_version": PROMPT_VERSION,
            "params": {"fail_chars": FAIL_CHARS, "max_evidence": MAX_EVIDENCE,
                       "claim_chars": CLAIM_CHARS}}
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(head, ensure_ascii=False, default=str) + "\n")
        for p in payloads:
            fh.write(json.dumps(p, ensure_ascii=False, default=str) + "\n")
    os.chmod(path, 0o600)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--session", nargs="+", metavar="ID")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--funnel", action="store_true",
                    help="the deterministic funnel, free")
    ap.add_argument("--candidates", action="store_true", help="list them, free")
    ap.add_argument("--write", action="store_true",
                    help="store the view per session, free")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the exact payload, unsent")
    ap.add_argument("--model", default=TUNING_MODEL)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)

    have = S.sessions()
    wanted = ([S.resolve(w, have) for w in args.session] if args.session
              else have)

    funnel = collections.Counter()
    per = []
    for sid in wanted:
        cs = candidates(sid, funnel)
        per.append((sid, cs))

    if args.funnel or not (args.candidates or args.write or args.dry_run
                           or args.session or args.all):
        print("%d session(s)\n" % len(wanted))
        for k in sorted(funnel):
            print("%-38s %5d" % (k, funnel[k]))
        print("\nThe last line is candidates, not findings: a failure elsewhere")
        print("in the same turn need not be about the claim. That is the "
              "model's call.")
        print("\n--candidates to list them, --write to store the view. "
              "Both free.")
        return 0

    if args.candidates:
        n = 0
        for sid, cs in per:
            for c in cs:
                n += 1
                print("%s turn %-3d %s" % (sid[:8], c["claim"]["turn"],
                      " ".join(strip_decoration(c["claim"]["text"]).split())[:88]))
                print("          evidence: %s"
                      % " ".join((c["failures"][0]["text"] or "").split())[:88])
        print("\n%d candidate(s)" % n)
        return 0

    if args.write:
        n = 0
        for sid, cs in per:
            pls = []
            for c in cs:
                pl = build_payload(sid, c)
                fails = [f["text"] or "" for f in c["failures"]]
                pl["unit_key"] = claim_key(pl["claim"]["text"], fails)
                pls.append(pl)
            write_view(sid, pls)
            n += len(pls)
        print("%d session(s), %d candidate(s) -> %s/<session>.jsonl"
              % (len(wanted), n, VIEW_DIR))
        print("Nothing sent.")
        return 0

    if args.dry_run:
        for sid, cs in per:
            for c in cs[:2]:
                print("=" * 70)
                print("SYSTEM (%s), %d chars\n" % (PROMPT_VERSION, len(SYSTEM)))
                print(json.dumps(build_payload(sid, c), ensure_ascii=False,
                                 indent=1, default=str))
        return 0

    print("A model pass is not wired up yet — it needs your go-ahead first.")
    print("--funnel, --candidates, --write and --dry-run all cost nothing.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
