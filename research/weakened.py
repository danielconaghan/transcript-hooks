#!/usr/bin/env python3
"""Does a WEAKENED directive get under-weighted, and so cause a desync?

Daniel's hypothesis: meiosis (understatement) and hedged assertions delivered
through interrogative form both weaken a directive, so the assistant
under-weights it — reads a correction as curiosity, an instruction as an open
question — and fails to act.

If it held it would be a genuine **preventative action**: the weakening sits in
the outgoing message, visible pre-send, and the injection writes itself — "this
reads as a question but functions as an instruction; confirm which."

Two tests, both needed
----------------------

The **marginal** test ("is this message hedged?") cannot work, and its failure
is informative rather than disappointing: hedging is the ambient register in
this corpus. "please", "can you", "would you mind" are ordinary and carry full
force, so hedged-vs-not does not separate anything. Measured at 1.03x
within-session.

The **conditional** test is the real one: *given a directive*, does weakened
delivery precede a desync more often than direct delivery? Both judgements come
from a model rather than a regex — `directive.jsonl` for delivery strength,
`desync.jsonl` for whether a marker landed. A regex attempt at the same question
found meiosis 0 times in 448 messages, which is a broken detector, not an
absence.

Result: NOT SUPPORTED
---------------------

    lookahead 1 msg: direct  73/292 = 25.0%   weakened 17/82 = 20.7%   RR 0.83x
    lookahead 2 msg: direct 106/292 = 36.3%   weakened 28/82 = 34.1%   RR 0.94x
    lookahead 3 msg: direct 136/292 = 46.6%   weakened 33/82 = 40.2%   RR 0.86x

Weakened directives are followed by a desync slightly LESS often than direct
ones, consistently across the window, so it is not an artefact of the window
choice. The phenomenon is real and common — 82 of 377 directives are weakened,
51 of them interrogative — it simply does not predict a desync.

Worth re-running as n grows. On this project a conclusion has already reversed
once on sample size: at 14 markers no pre-send state fact had a lift above 1.0,
and at 105 markers open-question reached 1.37x.

The one thread left, and it is not evidence
-------------------------------------------

`weakening: "multiple"` — a message weakened in more than one way at once — was
followed by a marker in 5 of 6 cases (83% against a 46.6% base rate). n=6, so
p is around 0.08 on a binomial. That is a hint about maximally-hedged messages,
nothing more. It needs an order of magnitude more data before it means anything.

Usage:
    python3 weakened.py                # the table above
    python3 weakened.py --lookahead 1
    python3 weakened.py --examples     # messages behind each cell
"""

import argparse
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import backtest as B       # noqa: E402
import backwalk as W       # noqa: E402

DIRECTIVES = os.path.join(B.resync_home(), "data", "directive.jsonl")
DESYNC = os.path.join(B.resync_home(), "data", "desync.jsonl")


def load(path):
    rows = {}
    if not os.path.exists(path):
        return rows
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("key"):
                rows[d["key"]] = d      # last write wins
    return rows


def measure(lookahead, want_examples=False):
    dirs, desy = load(DIRECTIVES), load(DESYNC)
    if not dirs or not desy:
        return None
    paths = W.session_paths()
    tot = collections.Counter()
    fol = collections.Counter()
    form_tot = collections.Counter()
    form_fol = collections.Counter()
    examples = collections.defaultdict(list)

    for sid in sorted(paths):
        seq = list(B.replay(sid, B.load_session_events(sid, paths[sid])))
        keys = [B.msg_key(sid, m["text"]) for _, m in seq]
        marker = [bool(desy.get(k, {}).get("desync")) for k in keys]
        for i, k in enumerate(keys):
            d = dirs.get(k)
            if not d or not d.get("is_directive"):
                continue
            st = d.get("strength")
            if st not in ("direct", "weakened"):
                continue
            hit = any(marker[i + 1: i + 1 + lookahead])
            tot[st] += 1
            if hit:
                fol[st] += 1
            if st == "weakened":
                # Three independent flags, so a message counts under each form
                # it carries. `weakening` was a single enum until hedge was
                # found to be absorbing every understated message.
                forms = [f for f in ("hedged", "understated", "interrogative")
                         if d.get(f)]
                for wf in (forms or ["(unflagged)"]):
                    form_tot[wf] += 1
                    if hit:
                        form_fol[wf] += 1
                        if want_examples and len(examples[wf]) < 4:
                            examples[wf].append(
                                " ".join((d.get("text") or "").split())[:84])
    return tot, fol, form_tot, form_fol, examples


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--lookahead", type=int, default=None,
                    help="messages after the directive to look in "
                         "(default: report 1, 2 and 3)")
    ap.add_argument("--examples", action="store_true",
                    help="print messages behind each form of weakening")
    args = ap.parse_args(argv)

    windows = [args.lookahead] if args.lookahead else [1, 2, 3]
    last = None
    for look in windows:
        got = measure(look, args.examples)
        if got is None:
            print("need both directive.jsonl and desync.jsonl — run "
                  "classify.py --judge directive and --judge desync first")
            return 1
        tot, fol, form_tot, form_fol, examples = got
        last = got
        rd = fol["direct"] / tot["direct"] if tot["direct"] else 0
        rw = fol["weakened"] / tot["weakened"] if tot["weakened"] else 0
        print("lookahead %d msg: direct %3d/%3d = %4.1f%%   "
              "weakened %2d/%2d = %4.1f%%   RR %s"
              % (look, fol["direct"], tot["direct"], 100 * rd,
                 fol["weakened"], tot["weakened"], 100 * rw,
                 ("%.2fx" % (rw / rd)) if rd else "-"))

    tot, fol, form_tot, form_fol, examples = last
    base = fol["direct"] / tot["direct"] if tot["direct"] else 0
    print("\nby form of weakening (lookahead %d, base rate %.1f%%):"
          % (windows[-1], 100 * base))
    for wf, n in form_tot.most_common():
        f = form_fol[wf]
        r = f / n if n else 0
        print("   %-16s %3d directive(s), %3d followed  %5.0f%%  %s"
              % (wf, n, f, 100 * r,
                 ("%.2fx" % (r / base)) if base else "-"))
    print("\nRR below 1.0 means weakened delivery is followed by a desync LESS "
          "often than direct — the hypothesis is not supported.")
    if args.examples:
        for wf, ex in examples.items():
            print("\n   %s — followed by a desync:" % wf)
            for e in ex:
                print("      %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
