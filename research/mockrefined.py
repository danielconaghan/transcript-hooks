#!/usr/bin/env python3
"""Synthetic sessions in REFINED form, with one inefficiency planted in each.

    Output:  <repo>/mock-refined/<sid>.epoch<N>.jsonl   the fixture
             <repo>/mock-views/<name>.jsonl             built from it
             <repo>/mock-views/ANSWERS.json             ground truth

Why refined and not view
------------------------

The first version of this generator emitted finished session documents. That
tested the judge and the prompt, and nothing else — every fixture entered the
pipeline downstream of the two modules that actually convert a transcript.

The worst defect found in this project lived precisely there. `signature()`
truncates a command at 160 characters, unmarked, and `views.py` was displaying
that index label as the command: 3,125 of 5,537 Bash commands — 56.4% — shown
ending mid-token. No mock could have caught it, because no mock ever ran
through the code that produced it.

Entering at refined puts the fixtures through the whole conversion:

    refined -> session.py -> views.py -> document -> judge
               signature()  document()
               actions()    command_of()
               epochs()     trim()
               events()     index_from_records()

Two things stop being asserted and start being derived. Epochs become real
files, so B3 is tested against the same `raw_lines()` ordering and dedup logic
the corpus uses rather than against a hand-written index entry. And the
document is built by `views.py`, so the fixtures can never drift from the
shape that ships — which they already did once, when the index learned to see
reversals and the mocks silently did not.

Faithfulness
------------

A fixture that only approximates refined tests a pipeline that does not exist.
Two properties were checked against a real two-epoch session before being
imitated here: a later epoch file REPEATS the earlier one's lines (measured:
28 shared uuids, 18 new), and a line belongs to the earliest epoch it appears
in. So epoch0 holds the prefix, epoch1 holds everything, and `raw_lines()`
does the assignment.
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for p in (REPO, HERE, os.path.join(REPO, "install")):
    sys.path.insert(0, p)

import session as S                            # noqa: E402
import views as V                              # noqa: E402

REFINED = os.path.join(REPO, "mock-refined")
VIEWS = os.path.join(REPO, "mock-views")
T0 = datetime(2026, 5, 12, 9, 0, 0)


# --------------------------------------------------------------------------
# scenario helpers — unchanged from the view-based generator on purpose, so
# the 48 scenarios carry over without an edit
# --------------------------------------------------------------------------

def d(text, gap=20):
    return {"who": "developer", "text": text, "_gap": gap}


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
# emitting refined
# --------------------------------------------------------------------------

def _tool_input(tool, command, target):
    """The raw input a tool was called with.

    The COMMAND goes in whole. `signature()` will shorten it to an index label
    and `views.command_of()` will show the input instead — which is the very
    interaction the view-based fixtures could not exercise."""
    if tool == "Bash":
        return {"command": command}
    if tool in ("Edit", "Write", "NotebookEdit", "Read"):
        return {"file_path": target or command.split(":", 1)[-1]}
    return {"command": command}


def refined_lines(recs):
    """Scenario records -> refined transcript rows."""
    rows, t, n = [], T0, 0
    pending = None                      # tool_use_id awaiting its result

    def stamp(gap):
        nonlocal t
        t = t + timedelta(seconds=gap)
        return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def row(kind, ts, **kw):
        nonlocal n
        n += 1
        r = {"type": kind, "uuid": "u%04d" % n, "timestamp": ts,
             "sessionId": "mock", "cwd": "/repo"}
        r.update(kw)
        return r

    for r in recs:
        r = dict(r)
        gap = r.pop("_gap", 5)
        who = r.pop("who")
        ts = stamp(gap)

        if who == "developer":
            rows.append(row("user", ts, message={"role": "user",
                                                 "content": r["text"]}))
        elif who == "assistant":
            rows.append(row("assistant", ts, message={
                "role": "assistant",
                "content": [{"type": "text", "text": r["text"]}]}))
        elif who == "action":
            pending = "tu%04d" % (n + 1)
            rows.append(row("assistant", ts, message={
                "role": "assistant",
                "content": [{"type": "tool_use", "id": pending,
                             "name": r["tool"],
                             "input": _tool_input(r["tool"], r["command"],
                                                  r.get("target"))}]}))
        elif who == "result":
            rows.append(row("user", ts, message={
                "role": "user",
                "content": [{"type": "tool_result",
                             "tool_use_id": pending or "tu0000",
                             "is_error": not r["exit_ok"],
                             "content": r["text"]}]}))
        elif who == "question":
            pending = "tu%04d" % (n + 1)
            rows.append(row("assistant", ts, message={
                "role": "assistant",
                "content": [{"type": "tool_use", "id": pending,
                             "name": "AskUserQuestion",
                             "input": {"questions": [
                                 {"question": r["asked"],
                                  "options": [{"label": o}
                                              for o in r["options"]]}]}}]}))
            # The answer comes back as a tool_result whose text carries
            # "question"="answer" pairs — the shape `parse_answers()` reads.
            pairs = " ".join('"%s"="%s"' % (x["question"], x["answer"])
                             for x in r["answers"]) or '"%s"="%s"' % (
                                 r["asked"], r["options"][0])
            rows.append(row("user", stamp(1), message={
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": pending,
                             "is_error": False,
                             "content": "Your questions have been answered: "
                                        + pairs}]}))
        elif who == "denial":
            rows.append(row("user", ts, toolDenialKind=r["kind"],
                            userFeedback=r.get("feedback") or "",
                            message={"role": "user", "content": [
                                {"type": "tool_result",
                                 "tool_use_id": pending or "tu0000",
                                 "is_error": True,
                                 "content": "The user doesn't want to proceed "
                                            "with this tool use."}]}))
        elif who == "marker":
            rows.append(row("user", ts, message={
                "role": "user",
                "content": "<command-name>/ds</command-name>"
                           "<command-args>%s</command-args>" % r["note"]}))
    return rows


def write_refined(sid, rows, epochs):
    os.makedirs(REFINED, exist_ok=True)
    if epochs == 1:
        parts = [(0, rows)]
    else:
        # Faithful to the real thing: the later epoch file REPEATS the earlier
        # one and adds to it. raw_lines() dedupes by uuid and assigns each line
        # to the EARLIEST epoch it appears in.
        cut = len(rows) // 2
        parts = [(0, rows[:cut]), (1, rows)]
    for ep, part in parts:
        p = os.path.join(REFINED, "%s.epoch%d.jsonl" % (sid, ep))
        with open(p, "w", encoding="utf-8") as fh:
            for r in part:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def main(argv=None):
    import scenarios
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    # Stale fixtures are worse than none: a renamed scenario leaves its old
    # file behind and the next run judges both, which happened once and cost
    # real money judging sessions with no answer key.
    for dirpath in (REFINED, VIEWS):
        os.makedirs(dirpath, exist_ok=True)
    keep_v = {"ANSWERS.json"}
    keep_r = set()
    plan = []
    for sc in scenarios.ALL:
        sid = "%s%02d-0000-4000-8000-%s" % (sc["cat"].lower(), sc["n"],
                                            V.key(sc["cat"], sc["n"],
                                                  sc["slug"])[:12])
        name = "%s-%d-%s" % (sc["cat"], sc["n"], sc["slug"])
        plan.append((sid, name, sc))
        keep_v.add(name + ".jsonl")
        for ep in range(sc.get("epochs", 1)):
            keep_r.add("%s.epoch%d.jsonl" % (sid, ep))
    for old in glob.glob(os.path.join(VIEWS, "*")):
        if os.path.basename(old) not in keep_v:
            os.remove(old)
    for old in glob.glob(os.path.join(REFINED, "*")):
        if os.path.basename(old) not in keep_r:
            os.remove(old)

    # Build refined first, then let session.py and views.py do the rest.
    for sid, name, sc in plan:
        write_refined(sid, refined_lines(sc["recs"]), sc.get("epochs", 1))

    # Point the shipping reader at the fixtures and let it do ALL the work.
    # An earlier version rebuilt the header here instead of calling
    # `views.build()`. The two happened to agree — checked, 48 of 48 identical
    # — but agreeing today is not the same as being one implementation, and
    # this generator has already drifted from views.py once. Only the file
    # NAMING stays local, because `A1-1-second-ask-never-addressed.jsonl` is
    # worth far more to a reader than `a101-0000-4000-8000-....jsonl`.
    S.REFINED = REFINED
    answers, rows = [], []
    for sid, name, sc in plan:
        head, recs = V.build(sid)
        eps = head["index"]["epochs"]
        with open(os.path.join(VIEWS, name + ".jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(head, ensure_ascii=False) + "\n")
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        answers.append({"file": name + ".jsonl", "session": sid,
                        "category": sc["cat"], "expect": sc["expect"],
                        "began_seq": sc.get("began_seq"),
                        "at_seq": sc.get("at_seq"),
                        "severity": sc.get("severity"), "why": sc["why"],
                        "distractor": sc.get("distractor")})
        rows.append((sc["cat"], sc["n"], len(recs), len(eps), name))

    with open(os.path.join(VIEWS, "ANSWERS.json"), "w", encoding="utf-8") as fh:
        json.dump({"note": "Ground truth. One issue per mock and no others; "
                           "`expect: false` sessions are clean controls whose "
                           "category must NOT fire. Built from mock-refined/ "
                           "through session.py and views.py.",
                   "answers": answers}, fh, indent=1)

    if args.list:
        print("%-5s %-3s %7s %7s  %s" % ("cat", "n", "records", "epochs", "slug"))
        print("-" * 62)
        for c, n, r, e, s in rows:
            print("%-5s %-3d %7d %7d  %s" % (c, n, r, e, s))
    print("\n%d fixture(s): %s/ -> %s/" % (len(rows), REFINED, VIEWS))
    print("%d planted, %d clean control(s)"
          % (sum(1 for x in answers if x["expect"]),
             sum(1 for x in answers if not x["expect"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
