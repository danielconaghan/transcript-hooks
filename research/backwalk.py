#!/usr/bin/env python3
"""From each retrospective marker, walk backwards: what was visible earlier?

The two categories this project works in:

  **preventative action** — something done before the assistant acts, to
  decrease the chance of a desync. Every rule in `rules.json` is one.
  **retrospective marker** — a point where a desync *surfaced*. The divergence
  happened earlier; the message is only where it became visible, and by then
  the cost is paid. `classify.py` finds these.

A marker can never be a trigger: intercepting the message that *reports* a
failure prevents nothing. Markers are ground truth. To get a preventative
action out of one you have to walk backwards — a desync landed at message T, so
what was in the pre-send state at T-n that predicted it? That walk is this file.

For each marker, it replays the session from `refined/` and prints, for the
messages before it, which rules fired and which pre-send state facts were
present. Anything that shows up reliably before markers and rarely elsewhere is
a candidate preventative action.

What the first run found, on 11 located markers
-----------------------------------------------

Mostly **nothing**. The clearest trace was:

    T   : please fix manifest.local.json still isn't gitignored ... still
          reports a pure reorder as drift
    T-1 : commit this              fired: -  state: -
    T-2 : yes, fix both comments   fired: -  state: -
    T-3 : yes, sync and re-run     fired: -  state: -

The developer approved two fixes, said commit, then had to report both still
broken. Nothing fired because there was nothing to fire on: the evidence that
the fixes had not landed was in the assistant's own tool output. No preventative
action on the INPUT side can catch that, which is the strongest argument in the
corpus for a PostToolUse-side check of assertion against tool result.

Where rules did fire beforehand they were mostly content-blind — R03 fires on a
session's first message whatever it says, R05 on any endpoint mention. (R06 and
R08 counted here too until the resolver was made to gate their fire.) The one
genuine signal was R04 — an unanswered decision persisting across T-1, T-2 and
T-3 before two markers in one session.

Every candidate signal then died on its base rate. Against all 448 messages:
`denial` 54.8% before a marker vs 72.3% overall (0.76x), `open-question` 28.6%
vs 32.8% (0.87x), `queue-remove` 19.0% vs 31.9% (0.60x). All BELOW 1.0 — less
common before a marker than in general. `denial` looked compelling at 23 of 42
until the base rate showed it is simply present nearly everywhere. Nothing in
the current PreSendState predicts a desync, so a new preventative action cannot
be built from the state as it stands.

Usage:
    python3 backwalk.py                  # every marker, 3 messages back
    python3 backwalk.py --lookback 5
    python3 backwalk.py --kind re-report # only markers of one kind
"""

import argparse
import collections
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import rules_engine as E   # noqa: E402
import backtest as B       # noqa: E402

MARKERS = os.path.join(B.resync_home(), "data", "desync.jsonl")

# Fires on nearly everything, so its presence before a marker is not evidence.
# Kept in step with gaps.CONTENT_BLIND.
# R06/R08 dropped: the resolver now gates their fire, so they are
# no longer content-blind. Kept in step with gaps.CONTENT_BLIND.
CONTENT_BLIND = {"R03", "R05"}
ALWAYS = {"R13"}


def load_markers(kind=None):
    if not os.path.exists(MARKERS):
        return []
    rows = {}
    with open(MARKERS, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("key"):
                rows[d["key"]] = d          # last write wins
    out = [d for d in rows.values() if d.get("desync")]
    if kind:
        out = [d for d in out if d.get("kind") == kind]
    return out


def session_paths():
    paths = collections.defaultdict(list)
    for p in (glob.glob(B.REFINED + "/*.epoch*.jsonl")
              + glob.glob(B.REFINED + "/*.inflight.*.jsonl")):
        sid = os.path.basename(p).split(".epoch")[0].split(".inflight")[0]
        paths[sid].append(p)
    return paths


def state_facts(state):
    """The pre-send facts a rule could key on, named as a rule would see them."""
    out = []
    openq = [q for q in state.questions
             if q.get("outcome") and q["outcome"] != "answered"]
    if openq:
        out.append("open-question x%d" % len(openq))
    stale = [t for t in (state.launches or {})
             if state.notified.get(t) is None]
    if stale:
        out.append("unreported-task x%d" % len(stale))
    rem = [o for o in state.queue_ops
           if o.get("op") == "remove" and o.get("human")]
    if rem:
        out.append("queue-remove x%d" % len(rem))
    if state.api_errors:
        out.append("api-error x%d" % len(state.api_errors))
    if state.denials:
        # Populated on every message and read by NO rule — see PLAN.md.
        out.append("denial x%d" % len(state.denials))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--lookback", type=int, default=3,
                    help="messages before each marker to inspect (default 3)")
    ap.add_argument("--kind", default=None,
                    help="only markers of this kind (correction, re-report, "
                         "state-question, misread, scope, undo)")
    ap.add_argument("--quiet", action="store_true",
                    help="totals only, no per-marker traces")
    args = ap.parse_args(argv)

    markers = load_markers(args.kind)
    if not markers:
        print("no markers in %s — run classify.py first" % MARKERS)
        return 1
    paths = session_paths()
    by_session = collections.defaultdict(list)
    for d in markers:
        by_session[d["session"]].append(d)

    print("%d marker(s) across %d session(s), looking back %d\n"
          % (len(markers), len(by_session), args.lookback))

    fired_before = collections.Counter()
    facts_before = collections.Counter()
    located = missing = 0
    silent = []           # markers with nothing at all beforehand

    for sid in sorted(by_session):
        if sid not in paths:
            missing += len(by_session[sid])
            continue
        seq = list(B.replay(sid, B.load_session_events(sid, paths[sid])))
        texts = [m["text"] for _, m in seq]
        for d in by_session[sid]:
            needle = (d.get("text") or "")[:120]
            idx = next((i for i, t in enumerate(texts)
                        if t[:120] == needle), None)
            if idx is None:
                missing += 1
                continue
            located += 1
            trace, anything = [], False
            for back in range(1, args.lookback + 1):
                j = idx - back
                if j < 0:
                    break
                state, msg = seq[j]
                fired = sorted({f.rule for f in E.evaluate(state)} - ALWAYS)
                facts = state_facts(state)
                real = [r for r in fired if r not in CONTENT_BLIND]
                if real or facts:
                    anything = True
                for r in fired:
                    fired_before[r] += 1
                for f in facts:
                    facts_before[f.split(" x")[0]] += 1
                trace.append((back, msg["text"], fired, facts))
            if not anything:
                silent.append(d)
            if args.quiet:
                continue
            print("=" * 74)
            print("MARKER [%s/%s] %s  %s"
                  % (d.get("kind"), d.get("confidence"), sid[:8], d["key"]))
            print("  T   : %s" % " ".join((d.get("text") or "").split())[:92])
            if d.get("contradicts"):
                print("  claim contradicted: %s"
                      % " ".join(d["contradicts"].split())[:88])
            for back, text, fired, facts in trace:
                print("  T-%d : %s" % (back, " ".join(text.split())[:86]))
                print("        fired: %-22s state: %s"
                      % (" ".join(fired) or "-", ", ".join(facts) or "-"))
            print()

    print("=" * 74)
    print("located %d marker(s); %d not in refined/ or not matched" % (located, missing))
    print("\nrules firing in the %d message(s) before a marker:"
          % (located * args.lookback))
    for r, n in fired_before.most_common():
        tag = "  (content-blind — not evidence)" if r in CONTENT_BLIND else ""
        print("   %-5s %3d%s" % (r, n, tag))
    print("\npre-send state facts present before a marker:")
    for f, n in facts_before.most_common():
        print("   %-18s %d" % (f, n))
    print("\n%d of %d marker(s) had NOTHING non-content-blind beforehand — "
          "no preventative action was available for these." % (len(silent), located))
    for d in silent[:8]:
        print("   [%s] %s" % (d.get("kind"), (d.get("text") or "")[:70]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
