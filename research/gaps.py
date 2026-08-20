#!/usr/bin/env python3
"""Where the catalogue is blind: desync signals no rule responds to.

`backtest.py` answers "are the rules we have right?". This answers the other
half — "what are we missing?" — and it is the input to writing a new rule.

The method, and why it is not just grep
--------------------------------------

A *signal* is language that suggests the previous turn went wrong: a
correction, a re-ask, a question about whether anything is happening. Signals
are deliberately broader than any rule's trigger, because the point is to find
what the catalogue does not catch.

For every human message the replay knows whether any rule fired. Cross that
with the signals and you get, per signal family, the messages where something
clearly went wrong and the service said nothing.

Two corrections make the number honest, and without them it flatters badly:

  * **R13 is excluded.** It fires on every message by design, so counting it as
    coverage would report 100% coverage of everything.
  * **Content-blind fires are reported separately.** R03 fires on the first
    message of a session whatever it says. R05, R06 and R08 fire on any
    message mentioning an endpoint, path or URL — a correction that happens to
    name a file is not a correction the catalogue understood. Measured on the
    50-session corpus, 27 of 42 "covered" corrections were of this kind, taking
    real coverage from 63% to roughly 24%.

What is automatic and what is not
---------------------------------

Everything here is deterministic: no model reads the corpus, and the same
corpus gives the same report. What a model is good for is the *last* step —
reading the twenty or thirty uncovered messages this prints and proposing a
trigger for them. That sample fits in a context window; the corpus does not.

The irreducibly manual part is SIGNALS itself. Discovery is bounded by the
patterns someone thought to look for, so a family that is absent from this dict
is invisible to the report. Treat a new signal family as a hypothesis to add
here, not as a rule to ship.

Usage:
    python3 gaps.py                    # coverage table + uncovered samples
    python3 gaps.py --signal state-ask # only that family, all its messages
    python3 gaps.py --samples 8        # how many examples per family
"""

import argparse
import collections
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import rules_engine as E   # noqa: E402
import backtest as B       # noqa: E402

# Rules whose firing tells you nothing about whether the message was a desync.
# Not a judgement about their worth — R03's paragraph is useful — only that they
# cannot count as evidence the catalogue understood this particular message.
CONTENT_BLIND = {
    "R03": "fires on the first message of a session, whatever it says",
    "R05": "fires on any message naming an endpoint",
    "R06": "fires on any message naming a path or repo",
    "R08": "fires on any message containing a URL",
}
ALWAYS = {"R13"}          # fires on every message by design

SIGNALS = {
    # The same pattern backtest.py labels hindsight corrections with, reused so
    # the two files agree on what a correction looks like.
    "correction": B.RE_CORRECTION,

    # The user re-reporting a symptom that was supposed to be fixed. Distinct
    # from R07, which only looks at the immediately previous message within 120
    # seconds — this is the same complaint arriving much later.
    "repeat/re-ask": re.compile(
        r"\b(?:again|already (?:said|told|asked)|as i said|like i said"
        r"|you keep|still (?:not|hasn'?t|doesn'?t|isn'?t))\b", re.I),

    # "is it stuck or working?" — the user asking for state they cannot see.
    # rules_engine defines this extractor and NO rule uses it; R02's own
    # rationale says the service should answer this question directly.
    "state-ask": E.RE_STATE_QUESTION,

    # The assistant did something other than what was asked.
    "misread": re.compile(
        r"\b(?:that'?s not what|thats not what|i didn'?t ask"
        r"|not what i (?:asked|meant|said)|why (?:did|are) you)\b", re.I),

    # The assistant did far more than was asked.
    "scope-creep": re.compile(
        r"\b(?:i only asked|you'?ve gone|such trouble|too much"
        r"|over[- ]?engineer|simpler|scale it back|out of scope)\b", re.I),

    # Work being thrown away.
    "undo": re.compile(
        r"\b(?:revert|undo|roll ?back|put it back|discard (?:that|those)"
        r"|start again|from scratch)\b", re.I),
}


def analyse():
    """Returns (total_messages, sessions, per-signal stats)."""
    fixture = B.load_fixture()
    stats = {k: {"msgs": 0, "real": 0, "blind_only": 0, "none": 0,
                 "by_rule": collections.Counter(), "examples": []}
             for k in SIGNALS}
    total = 0
    for sid, sess in fixture.items():
        fired = collections.defaultdict(set)
        for fire, msg in sess["fires"]:
            if fire.rule not in ALWAYS:
                fired[msg["id"]].add(fire.rule)
        for msg in sess["msgs"]:
            total += 1
            text = msg["text"]
            for name, rx in SIGNALS.items():
                if not rx.search(text):
                    continue
                st = stats[name]
                st["msgs"] += 1
                rules = fired.get(msg["id"], set())
                for r in rules:
                    st["by_rule"][r] += 1
                real = rules - set(CONTENT_BLIND)
                if real:
                    st["real"] += 1
                else:
                    st["blind_only" if rules else "none"] += 1
                    st["examples"].append({
                        "session": sid[:8],
                        "text": " ".join(text.split()),
                        "blind": sorted(rules),
                    })
    return total, len(fixture), stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--signal", default=None, help="only this signal family")
    ap.add_argument("--samples", type=int, default=3,
                    help="uncovered examples to print per family (default 3)")
    args = ap.parse_args(argv)

    total, nsess, stats = analyse()
    print("corpus: %d human message(s) across %d session(s)" % (total, nsess))
    print("excluded from coverage: %s (fires on every message); %s"
          % (" ".join(sorted(ALWAYS)),
             ", ".join("%s (%s)" % (k, v) for k, v in
                       sorted(CONTENT_BLIND.items()))))
    print()
    print("%-14s %5s %6s %7s %6s  %s"
          % ("signal", "msgs", "real", "blind", "none", "real coverage"))
    print("-" * 68)
    wanted = [args.signal] if args.signal else list(SIGNALS)
    for name in wanted:
        st = stats.get(name)
        if st is None:
            print("unknown signal %r; known: %s" % (name, " ".join(SIGNALS)))
            return 2
        n = st["msgs"]
        print("%-14s %5d %6d %7d %6d  %s"
              % (name, n, st["real"], st["blind_only"], st["none"],
                 ("%.0f%%" % (100.0 * st["real"] / n)) if n else "-"))
    print("\nreal    = a rule fired that was not content-blind")
    print("blind   = only content-blind rules fired — the catalogue did not "
          "understand this message")
    print("none    = nothing fired at all")

    for name in wanted:
        st = stats[name]
        if not st["examples"]:
            continue
        print("\n--- %s: %d message(s) the catalogue missed ---"
              % (name, len(st["examples"])))
        limit = len(st["examples"]) if args.signal else args.samples
        for ex in st["examples"][:limit]:
            tag = ("only %s" % " ".join(ex["blind"])) if ex["blind"] else "nothing fired"
            print("  [%s] %s" % (tag, ex["text"][:100]))
        if len(st["examples"]) > limit:
            print("  ... %d more (--signal %s to see all)"
                  % (len(st["examples"]) - limit, name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
