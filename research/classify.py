#!/usr/bin/env python3
"""Ask a model whether each message was a real desync.

`gaps.py` decides that from six hand-written regexes over the message text.
That cannot work, for a reason worth stating plainly: *"did the user have to
correct us?"* is a question about the **previous turn**, and a regex over the
message cannot see the previous turn. It can only spot vocabulary. Measured on
the 50-session corpus it also produced obvious nonsense — both `scope-creep`
hits were the phrase "out of scope" inside a spec Daniel pasted, and two
`correction` hits were the platform's own compaction summary.

Worse than the false positives is the recall: nobody knows how many desyncs the
regexes miss, because you can only inspect what they matched.

This sends each message plus the assistant turn before it to a model and asks
the actual question. 433 of 483 messages have that preceding turn available.

Reproducibility
---------------

An LLM pass is not reproducible, and `PLAN.md` bans a model from the *trigger*
path for exactly that reason. This is the labelling path, not the trigger path
— but the concern still applies, so verdicts are **cached to disk** keyed by a
content hash. The pass runs once, results persist, and a re-run only spends
money on messages it has not seen. After the first run the corpus figures are
as reproducible as the hand labels are.

Every row records the model that produced it. Nothing here is ground truth:
spot-check a sample by hand before trusting a number that rests on it, and
because the rows are tagged you can always recompute without them.

    python3 classify.py --limit 40                    # pilot, cheap model
    python3 classify.py --limit 40 --recheck          # ignore the cache
    python3 classify.py --model claude-opus-5         # the pass you keep
    python3 classify.py --compare                     # model vs the regexes
    python3 classify.py --show                        # what is cached so far
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

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import rules_engine as E   # noqa: E402
import backtest as B       # noqa: E402

VERDICTS = os.path.join(B.resync_home(), "data", "desync.jsonl")

# Not labels.jsonl. That file is keyed on (rule, fire_key) and answers "was this
# rule right about this fire". This answers "was this message a desync at all",
# which is a different question about a different object, and mixing them would
# let a message-level opinion masquerade as evidence about a rule.

TUNING_MODEL = "claude-haiku-4-5"
KEEPER_MODEL = "claude-opus-5"
NO_EFFORT_MODELS = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-haiku-3")

SYSTEM = """\
You are auditing a developer's coding session with an AI assistant, to find \
moments where the assistant had drifted out of sync with what the developer \
actually wanted.

You will be shown the assistant's previous turn, then the developer's next \
message. Decide whether that message shows the assistant was out of sync.

It IS a desync when the developer:
  - corrects a fact the assistant was working from (a moved endpoint, a renamed
    field, a wrong assumption)
  - re-reports a symptom that was supposed to be fixed already
  - asks whether work is actually progressing, because they cannot observe it:
    "is it stuck?", "still running?", "why is this taking so long?". A question
    about how something works, what an error means, or what the right approach
    is, is NOT this — that is a developer thinking, not a developer blocked.
  - points out the assistant did something other than what was asked
  - asks for work to be undone or redone

It is NOT a desync when the developer:
  - gives a new instruction, or the next step in a plan, however it is worded.
    "do it all over again" as a fresh request is not a desync; "still not
    working" is.
  - asks a question out of curiosity, or for an explanation
  - pastes a specification, brief, or documentation. Such text often contains
    words like "out of scope", "simpler" or "actually" as part of its content;
    that is the document talking, not the developer complaining.
  - reports a problem in their own environment they have just fixed themselves
  - is answering a question the assistant asked

Some inputs are not developer messages at all: a platform-generated conversation
summary (often opening "This session is being continued from a previous
conversation") is machine text. Return desync false, kind "none".

Judge only what the message shows. Do not speculate about what might have gone
wrong off-screen. When genuinely unsure, say so with confidence "low" rather
than guessing either way — an uncertain label is more useful than a confident
wrong one.

`kind` names the KIND OF DESYNC. If desync is false, kind must be "none" — do not reach for the closest-looking category. Most messages in a healthy session are not desyncs; "none" is the expected answer.

`quote` must be copied verbatim from the developer's message, or be empty.\
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "desync": {"type": "boolean"},
        "kind": {"type": "string", "enum": [
            "correction", "re-report", "state-question", "misread",
            "scope", "undo", "none"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "quote": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["desync", "kind", "confidence", "quote", "reason"],
    "additionalProperties": False,
}


def msg_key(session_id, text):
    """Stable identity for a message, so a verdict survives a re-run and a
    re-reduction. Content-hashed for the same reason fire keys are."""
    return hashlib.sha1(
        ("%s|%s" % (session_id, text)).encode("utf-8", "replace")).hexdigest()[:16]


def collect(limit=None):
    """(key, session, prior_assistant_turn, message_text) oldest first.

    Compaction summaries are deliberately NOT filtered out: they are currently
    ingested as human messages (a recorded open gap), and whether the model
    rejects them is a useful check on the prompt."""
    groups = collections.defaultdict(list)
    for p in (glob.glob(B.REFINED + "/*.epoch*.jsonl")
              + glob.glob(B.REFINED + "/*.inflight.*.jsonl")):
        sid = os.path.basename(p).split(".epoch")[0].split(".inflight")[0]
        groups[sid].append(p)

    out = []
    for sid in sorted(groups):
        prior = ""
        for d in B.load_session_events(sid, groups[sid]):
            t = d.get("type")
            if t == "assistant":
                blocks = (d.get("message") or {}).get("content") or []
                txt = " ".join(b.get("text", "") for b in blocks
                               if isinstance(b, dict) and b.get("type") == "text")
                if txt.strip():
                    prior = txt
            elif t == "user" and not d.get("isMeta"):
                text = (B.msg_text(d) or "").strip()
                if not text or B.SYS_PREFIX.match(text) or B.INTERRUPT.match(text):
                    continue
                out.append((msg_key(sid, text), sid, prior, text))
    if limit:
        # Spread the sample across sessions rather than taking one session's
        # worth: a pilot that only sees the tail of the corpus tells you nothing
        # about the rest of it.
        step = max(1, len(out) // limit)
        out = out[::step][:limit]
    return out


def load_cache():
    got = {}
    if not os.path.exists(VERDICTS):
        return got
    with open(VERDICTS, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("key"):
                got[d["key"]] = d       # last write wins
    return got


def append(rows):
    if not rows:
        return
    os.makedirs(os.path.dirname(VERDICTS), mode=0o700, exist_ok=True)
    with open(VERDICTS, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    os.chmod(VERDICTS, 0o600)


def classify_one(client, model, item):
    key, sid, prior, text = item
    user = ("ASSISTANT'S PREVIOUS TURN:\n%s\n\n"
            "DEVELOPER'S MESSAGE:\n%s"
            % ((prior[:3000] or "(nothing — this is the first message)"),
               text[:3000]))
    kwargs = {}
    if not model.startswith(NO_EFFORT_MODELS):
        kwargs["output_config"] = {"effort": "low",
                                   "format": {"type": "json_schema",
                                              "schema": SCHEMA}}
    else:
        kwargs["output_config"] = {"format": {"type": "json_schema",
                                              "schema": SCHEMA}}
    resp = client.messages.create(
        model=model, max_tokens=1000, system=SYSTEM,
        messages=[{"role": "user", "content": user}], **kwargs)
    if resp.stop_reason == "refusal":
        raise RuntimeError("refused")
    blob = next(b.text for b in resp.content if getattr(b, "type", None) == "text")
    data = json.loads(blob)
    data.update({"key": key, "session": sid, "model": model,
                 "text": " ".join(text.split())[:300]})
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=None,
                    help="classify only N messages, spread across sessions")
    ap.add_argument("--model", default=TUNING_MODEL,
                    help="default %s; use %s for the pass you keep"
                         % (TUNING_MODEL, KEEPER_MODEL))
    ap.add_argument("--recheck", action="store_true",
                    help="re-classify even if a verdict is cached")
    ap.add_argument("--compare", action="store_true",
                    help="compare cached verdicts against gaps.py's regexes")
    ap.add_argument("--show", action="store_true", help="print cached verdicts")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)

    cache = load_cache()

    if args.show or args.compare:
        if not cache:
            print("nothing cached yet — run without --show first")
            return 1
        if args.show:
            for d in cache.values():
                print("%-6s %-14s %-8s %s" % (
                    "DESYNC" if d.get("desync") else "-", d.get("kind"),
                    d.get("confidence"), d.get("text", "")[:78]))
        if args.compare:
            import gaps as G
            agree = collections.Counter()
            disagree = []
            for d in cache.values():
                rx_hit = any(rx.search(d.get("text") or "")
                             for rx in G.SIGNALS.values())
                m_hit = bool(d.get("desync"))
                agree[(m_hit, rx_hit)] += 1
                if m_hit != rx_hit:
                    disagree.append((m_hit, d))
            n = sum(agree.values())
            same = agree[(True, True)] + agree[(False, False)]
            print("\nmodel vs regex over %d cached message(s): agree on %d (%.0f%%)"
                  % (n, same, 100.0 * same / n if n else 0))
            print("  both say desync      : %d" % agree[(True, True)])
            print("  both say no          : %d" % agree[(False, False)])
            print("  model yes, regex no  : %d   <- regex MISSED these"
                  % agree[(True, False)])
            print("  regex yes, model no  : %d   <- regex FALSE POSITIVES"
                  % agree[(False, True)])
            for m_hit, d in disagree[:12]:
                print("\n  %s  [%s/%s] %s" % (
                    "regex missed" if m_hit else "regex false positive",
                    d.get("kind"), d.get("confidence"), d.get("text", "")[:74]))
                print("      %s" % (d.get("reason") or "")[:96])
        return 0

    items = collect(args.limit)
    todo = [i for i in items if args.recheck or i[0] not in cache]
    print("%d message(s) selected, %d already cached, %d to classify with %s"
          % (len(items), len(items) - len(todo), len(todo), args.model))
    if not todo:
        return 0

    sys.path.insert(0, os.path.join(B.resync_home()))
    import intercept as I           # reuse its venv + .env resolution
    anthropic = I.import_anthropic()
    if anthropic is None:
        print("anthropic SDK not importable — %s/.venv" % B.resync_home())
        return 1
    I.load_env_file()
    client = anthropic.Anthropic()

    rows, errors = [], collections.Counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(classify_one, client, args.model, it): it
                for it in todo}
        for n, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            try:
                rows.append(fut.result())
            except Exception as exc:
                errors[type(exc).__name__] += 1
            if n % 10 == 0 or n == len(todo):
                print("  %d/%d" % (n, len(todo)))
    append(rows)
    print("\nwrote %d verdict(s) -> %s" % (len(rows), VERDICTS))
    if errors:
        print("errors: %s" % dict(errors))
    d = collections.Counter((r["desync"], r["kind"]) for r in rows)
    print("\n%-8s %-14s %s" % ("desync", "kind", "count"))
    for (yes, kind), c in d.most_common():
        print("%-8s %-14s %d" % ("yes" if yes else "no", kind, c))
    print("\nnext: --compare to see where this disagrees with the regexes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
