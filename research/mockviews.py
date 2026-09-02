#!/usr/bin/env python3
"""Synthetic session documents with inefficiencies planted at known sequences.

    Output:  <repo>/mock-views/<CAT>-<n>-<slug>.jsonl
             <repo>/mock-views/ANSWERS.json

Why this exists
---------------

Every quality number produced so far is a model agreeing with a model. The
judge is a model, the checker of the judge was a model, the PreToolUse guard is
the same again — three levels of marking our own homework, reported as
evidence. The one external anchor, the developer's own `/ds` markers, currently
reads as zero across all 56 sessions.

Planted data is the cheap way out. Here the answer is known before the model
sees anything, so recall and precision are measured rather than asserted, and
it costs nothing to produce or to regenerate.

What is planted
---------------

Two sessions per category, thirty-two in all, each built to fire exactly one
category. They are deliberately small — tens of records against a real median
of 133 — so a scoring run is affordable and a human can read the whole thing
and disagree with the answer key.

Three properties are held on purpose, because they are what the real corpus
showed and a fixture that loses them would flatter the instrument:

  `began_seq` is never `at_seq`. In the real findings the cause and the
  symptom sat a median of 10 records apart, p90 106. A fixture where the
  problem is visible exactly where it starts tests nothing.

  Clean controls are included. On real data the judge correctly returned zero
  findings on trivial sessions, and that behaviour has to survive: a model that
  fires on everything scores perfect recall and is worthless.

  Distractors are present. Most sessions carry a failed command, a rewritten
  file or a retry that is NOT the planted fault, so a hit has to be the right
  finding and not just the right session.

The schema is `views.py`'s, reusing its own `key()` and header shape, so these
files are drop-in readable by `judge.py` with no special case.
"""

import argparse
import collections
import glob
import json
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for p in (REPO, HERE, os.path.join(REPO, "install")):
    sys.path.insert(0, p)

import views as V                              # noqa: E402

OUT = os.path.join(REPO, "mock-views")
T0 = datetime(2026, 5, 12, 9, 0, 0)


# --------------------------------------------------------------------------
# record helpers — the payload only; seq, turn and time are assigned by build()
# --------------------------------------------------------------------------

def d(text, gap=20):
    return {"who": "developer", "machine_generated": None, "text": text,
            "_gap": gap}


def a(text, gap=6):
    return {"who": "assistant", "text": text, "_gap": gap}


def act(tool, command, target=None, gap=3):
    return {"who": "action", "tool": tool, "command": command,
            "target": target, "_gap": gap}


def res(text, ok=True, gap=2):
    return {"who": "result", "exit_ok": ok, "text": text, "_gap": gap}


def q(asked, options, outcome="answered", answers=None, gap=15):
    return {"who": "question", "asked": asked, "options": options,
            "outcome": outcome, "answers": answers or [], "_gap": gap}


def den(kind="tool_use", by_developer=True, feedback=None, gap=5):
    return {"who": "denial", "kind": kind, "by_developer": by_developer,
            "feedback": feedback, "_gap": gap}


def mk(note, gap=5):
    return {"who": "marker", "note": note, "_gap": gap}


# --------------------------------------------------------------------------
# building one document
# --------------------------------------------------------------------------

def build(cat, n, slug, recs, epochs=1):
    """Assign sequences, turns and clock; compute the index from the records.

    The index is derived from the records rather than authored beside them.
    Authoring both invites them to disagree, and an internally inconsistent
    fixture measures the model's tolerance for our mistakes, not its judgement.
    """
    sid = "%s%02d-0000-4000-8000-%s" % (cat.lower(), n,
                                        V.key(cat, n, slug)[:12])
    out, t, turn, last = [], T0, 0, None
    for i, r in enumerate(recs):
        r = dict(r)
        t = t + timedelta(seconds=r.pop("_gap", 5))
        if r["who"] == "developer" and last != "developer":
            turn += 1
        last = r["who"]
        row = {"seq": i, "turn": turn}
        if r["who"] in ("developer", "assistant", "action", "result"):
            row["at"] = t.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        row["who"] = r.pop("who")
        if row["who"] == "result":
            # A result names the command it belongs to, exactly as views.py
            # writes it: the pairing is by tool_use_id there, so the label can
            # never drift from the action above it.
            prior = next((x for x in reversed(out) if x["who"] == "action"), {})
            row["of_command"] = prior.get("command", "")
            row["exit_ok"] = r.pop("exit_ok")
            row["bytes"] = len(r.get("text") or "")
        row.update({k: v for k, v in r.items()})
        out.append(row)

    # The index comes from views.py, not from a copy living here. An earlier
    # version computed it locally; views.py then learned to see reversals and
    # the fixtures silently did not, so the mocks were testing a different
    # instrument from the one that ships.
    last_seq = out[-1]["seq"]
    if epochs == 1:
        eps = [{"epoch": 0, "first_seq": 0, "last_seq": last_seq}]
    else:
        cut = last_seq // 2
        eps = [{"epoch": 0, "first_seq": 0, "last_seq": cut},
               {"epoch": 1, "first_seq": cut + 1, "last_seq": last_seq}]
    index = V.index_from_records(out, eps)

    head = {"kind": "header", "view": V.VIEW, "session": sid,
            "unit": "the whole session", "records": len(out),
            "unit_key": V.key(sid, V.VIEW, len(out),
                              out[0].get("at", ""), out[-1].get("at", "")),
            "params": {"head_tail": V.HEAD_TAIL, "sig_chars": V.SIG_CHARS,
                       "gap_seconds": V.GAP_SECONDS},
            "index": index}
    return sid, head, out


def write(cat, n, slug, head, recs):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "%s-%d-%s.jsonl" % (cat, n, slug))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(head, ensure_ascii=False) + "\n")
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def main(argv=None):
    import scenarios
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    # Stale files are worse than no files: a renamed scenario leaves its old
    # version behind, and the next run judges both. That happened once and
    # cost real money judging fixtures that no longer had an answer key.
    os.makedirs(OUT, exist_ok=True)
    keep = {"%s-%d-%s.jsonl" % (sc["cat"], sc["n"], sc["slug"])
            for sc in scenarios.ALL} | {"ANSWERS.json"}
    for old in glob.glob(os.path.join(OUT, "*")):
        if os.path.basename(old) not in keep:
            os.remove(old)

    answers = []
    rows = []
    for sc in scenarios.ALL:
        sid, head, recs = build(sc["cat"], sc["n"], sc["slug"], sc["recs"],
                                sc.get("epochs", 1))
        path = write(sc["cat"], sc["n"], sc["slug"], head, recs)
        answers.append({"file": os.path.basename(path), "session": sid,
                        "category": sc["cat"], "expect": sc["expect"],
                        "began_seq": sc.get("began_seq"),
                        "at_seq": sc.get("at_seq"),
                        "severity": sc.get("severity"),
                        "why": sc["why"],
                        "distractor": sc.get("distractor")})
        rows.append((sc["cat"], sc["n"], len(recs),
                     int(len(json.dumps(recs)) / 2.1), sc["slug"]))

    with open(os.path.join(OUT, "ANSWERS.json"), "w", encoding="utf-8") as fh:
        json.dump({"note": "Ground truth. `expect: false` sessions are clean "
                           "controls and must produce NO finding for the "
                           "category they are filed under.",
                   "answers": answers}, fh, indent=1)

    if args.list:
        print("%-5s %-3s %7s %8s  %s" % ("cat", "n", "records", "~tokens", "slug"))
        print("-" * 62)
        for c, n, r, t, s in rows:
            print("%-5s %-3d %7d %8s  %s" % (c, n, r, "{:,}".format(t), s))
    tot = sum(r[3] for r in rows)
    print("\n%d mock session(s) -> %s" % (len(rows), OUT))
    print("%d planted, %d clean control(s)"
          % (sum(1 for a in answers if a["expect"]),
             sum(1 for a in answers if not a["expect"])))
    print("~%s tokens total — a full scoring run is ~$%.2f of input at opus"
          % ("{:,}".format(tot), tot / 1e6 * 5))
    return 0


if __name__ == "__main__":
    sys.exit(main())
