#!/usr/bin/env python3
"""Would a model at PreToolUse have caught it, seeing only what came before?

The judge read the whole session, consequence included. A preventative action
fires BEFORE the action and cannot see the future — it gets the session so far
and the pending tool call, nothing else. That asymmetry is the entire question,
and no amount of prompt work removes it: if the problem is not predictable from
what precedes it, there is nothing to prevent.

So this replays each origin with the future removed.

The negative control is not optional
------------------------------------

A model that answers "yes, risky" to everything scores 100% recall and is
worthless: PreToolUse fires on every Bash, Write and Edit, so a hook with poor
precision is a tax on every action the assistant takes. Half the sample is
therefore actions that are NOT origins and carry no finding anywhere near them.
The number that matters is the gap between the two rates, not the hit rate.

Nothing here writes to verdicts/ — it is an experiment, not a judgement.
"""
import argparse
import collections
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for p in (REPO, HERE, os.path.join(REPO, "install")):
    sys.path.insert(0, p)

import views as V                              # noqa: E402
import judge as J                              # noqa: E402

# What a hook can realistically hold. The whole session is not available to a
# latency-sensitive PreToolUse call, and pretending otherwise would measure a
# hook nobody can ship.
WINDOW_RECORDS = 120
WINDOW_CHARS = 60000
NEAR = 25          # a control action must have no finding within this many seqs

SYSTEM = """\
You are a guard that runs immediately BEFORE an AI coding assistant executes a
tool call, inside a developer's session. You see the session so far and the
single pending action. You do NOT see what happens next — that is the point.

Your job is to decide whether this specific action is about to cause wasted
effort, and to say so ONLY when you have a concrete reason visible in what
precedes it.

You will be run before EVERY Bash, Write and Edit in every session. If you flag
ordinary work, you are a tax on everything the assistant does and you will be
turned off. Most actions are fine. The expected answer is `intervene: false`.

Flag only these, and only with evidence from the records above:
  the action rests on something asserted but never checked in this session
  the action repeats something that already failed here
  it rewrites or reverses a file this session already wrote
  it is long and blocking, with no way for the developer to see progress
  it goes beyond what the developer actually asked for
  it acts on a reading of an ambiguous instruction without that being flagged

`concern` must name what you saw and where — a command, a claim, a file, a
sequence number. "This could fail" is not a concern. "seq 84 asserted the build
passes but no build command appears above" is.\
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "intervene": {"type": "boolean"},
        "concern": {"type": "string"},
        "category": {"type": "string",
                     "enum": ["unverified_premise", "repeat_of_failure",
                              "reversal", "blocking_no_progress",
                              "beyond_the_ask", "ambiguous_reading", "none"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["category", "concern", "confidence", "intervene"],
    "additionalProperties": False,
}


def context(recs, seq):
    """Everything strictly before `seq`, bounded the way a hook would be."""
    before = [r for r in recs if r["seq"] < seq][-WINDOW_RECORDS:]
    blob = json.dumps(before, ensure_ascii=False, separators=(",", ":"),
                      default=str)
    while len(blob) > WINDOW_CHARS and len(before) > 8:
        before = before[len(before) // 8:]
        blob = json.dumps(before, ensure_ascii=False, separators=(",", ":"),
                          default=str)
    return before


def sample(n, seed=11):
    """n origins that are tool calls, and n control tool calls that are not."""
    rnd = random.Random(seed)
    origins, controls = [], []
    by_session = collections.defaultdict(list)
    for sid, cat, f in J.findings():
        by_session[sid[:8]].append((cat, f))
    for pre, items in sorted(by_session.items()):
        full = [s for s in V.built() if s.startswith(pre)][0]
        _, recs = V.read(full)
        at = {r["seq"]: r for r in recs}
        marked = {f["began_seq"] for _, f in items}
        marked |= {f["at_seq"] for _, f in items}
        for cat, f in items:
            r = at.get(f["began_seq"])
            if r and r["who"] == "action" and f["severity"] in ("cost", "friction"):
                origins.append((full, r, cat, f))
        for r in recs:
            if r["who"] != "action" or r.get("tool") not in ("Bash", "Write", "Edit"):
                continue
            if any(abs(r["seq"] - m) <= NEAR for m in marked):
                continue
            controls.append((full, r, None, None))
    rnd.shuffle(origins)
    rnd.shuffle(controls)
    return origins[:n], controls[:n]


def ask(client, model, recs, seq, action):
    before = context(recs, seq)
    user = ("SESSION SO FAR (%d records, ending at seq %d):\n%s\n\n"
            "PENDING ACTION — about to run, has NOT run yet:\n%s"
            % (len(before), before[-1]["seq"] if before else -1,
               json.dumps(before, ensure_ascii=False, indent=1, default=str),
               json.dumps(action, ensure_ascii=False, indent=1, default=str)))
    fmt = {"format": {"type": "json_schema", "schema": SCHEMA}}
    if not model.startswith(J.NO_EFFORT_MODELS):
        fmt["effort"] = "low"          # a hook cannot afford to think for long
    r = client.messages.create(model=model, max_tokens=1000, system=SYSTEM,
                               messages=[{"role": "user", "content": user}],
                               output_config=fmt)
    u = J.usage_of(r.usage)
    return (json.loads(next(b.text for b in r.content
                            if getattr(b, "type", None) == "text")),
            u, J.cost_of(model, u))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-n", type=int, default=15, help="cases per arm")
    ap.add_argument("--model", default=J.KEEPER_MODEL)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    origins, controls = sample(args.n)
    print("%d origin(s) and %d control(s), model %s"
          % (len(origins), len(controls), args.model))
    if args.dry_run:
        full, r, cat, f = origins[0]
        _, recs = V.read(full)
        before = context(recs, r["seq"])
        print("\nexample: %s seq %s (%s)" % (full[:8], r["seq"], cat))
        print("  context %d records, ~%d tokens"
              % (len(before),
                 len(json.dumps(before, default=str)) / J.CHARS_PER_TOKEN))
        print("  pending: %s" % json.dumps(r, ensure_ascii=False)[:200])
        print("  judge found: %s" % f["what_happened"][:150])
        tot = 0
        for full, r, _, _ in origins + controls:
            _, recs = V.read(full)
            tot += len(json.dumps(context(recs, r["seq"]), default=str))
        print("\n%d call(s), ~%s input tokens, ~$%.2f"
              % (len(origins) + len(controls),
                 "{:,}".format(int(tot / J.CHARS_PER_TOKEN)),
                 tot / J.CHARS_PER_TOKEN / 1e6 * J.PRICE[args.model][0]))
        return 0

    client = J.client_or_die()
    if client is None:
        return 1
    spend = 0.0
    out = {"origin": [], "control": []}
    for arm, cases in (("origin", origins), ("control", controls)):
        for full, r, cat, f in cases:
            _, recs = V.read(full)
            try:
                d, u, c = ask(client, args.model, recs, r["seq"], r)
            except Exception as exc:
                print("  %s seq %s FAILED %s" % (full[:8], r["seq"], exc))
                continue
            spend += c or 0
            out[arm].append({"session": full[:8], "seq": r["seq"],
                             "judge_cat": cat,
                             "judge_said": (f or {}).get("what_happened"),
                             "guard": d, "cost": c})
            print("  %-7s %s seq %-5s %-6s %-22s %s"
                  % (arm, full[:8], r["seq"],
                     "FLAG" if d["intervene"] else "pass",
                     d["category"], (cat or "-")))
    o = out["origin"]; c = out["control"]
    tp = sum(1 for x in o if x["guard"]["intervene"])
    fp = sum(1 for x in c if x["guard"]["intervene"])
    print("\n%-22s %s" % ("recall on origins:", "%d/%d = %.0f%%"
                          % (tp, len(o), 100.0 * tp / max(1, len(o)))))
    print("%-22s %s" % ("false-positive rate:", "%d/%d = %.0f%%"
                        % (fp, len(c), 100.0 * fp / max(1, len(c)))))
    if tp + fp:
        print("%-22s %.0f%%" % ("precision:", 100.0 * tp / (tp + fp)))
    print("%-22s $%.2f" % ("spend:", spend))
    p = os.path.join(HERE, "..", "backtest-%s.json" % args.model)
    with open(os.path.abspath(p), "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print("\n-> %s" % os.path.abspath(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
