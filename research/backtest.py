#!/usr/bin/env python3
"""Backtest the rule catalogue against the historical corpus.

Replays every human-authored message in `refined/` through `rules_engine`,
message by message, rebuilding the pre-send state as it was at each point —
then judges each fire and reports precision per rule.

The rules themselves live in rules_engine.py and are shared verbatim with
intercept.py, so a precision figure here describes the code that actually
runs. This file contributes the two things the engine deliberately lacks:

  * the replay, which reconstructs PreSendState from a finished session
  * the labelling, which is allowed to look at the future

Labelling honesty is the point of this file. Precision is only as good as its
label, so every rule reports HOW its fires were judged:

  auto          The label is a fact in the data, independent of the trigger.
  auto-proxy    A weak stand-in for the real question, flagged as such.
  hindsight     The label comes from what the user said later. A correction
                worded without shared vocabulary is missed, so it UNDER-counts.
  tautological  The label would restate the trigger, so precision is undefined
                and reported as null. R02 fires when a task is unreported and
                would be "confirmed" by the task being unreported: the same
                fact twice, not evidence.
  manual        Fires are real, labels need a human. R11's subject is a whole
                message, so the hindsight matcher confirms almost anything —
                before this was caught it "confirmed" a 2,000-word brief
                against "vpn was not on, is now".
  none          Not labellable at all. R08 needs dev servers that no longer
                run; R09 has no implemented trigger.
  user          Your own verdicts from labels.jsonl, once intercept.py starts
                collecting them. These override every heuristic above.

Two precision figures are reported and the difference matters:

  floor     confirmed / all fires. Counts every unlabelled fire as
            unconfirmed. Conservative, and the number to trust.
  labelled  confirmed / (confirmed + refuted). Ignores unlabelled fires, so it
            flatters any rule with many unknowns — quoting it alone reports a
            hindsight rule with 31 unknowns out of 32 as "100%".

Usage:
    python3 backtest.py                 # table to stdout
    python3 backtest.py --write         # fold results into rules.json
    python3 backtest.py --rule R01      # one rule, listing its fires
    python3 backtest.py --list-fires    # list fires for every rule
"""

import argparse
import collections
import difflib
import glob
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import rules_engine as E   # noqa: E402  (needs REPO on the path first)


def resync_home():
    env = os.environ.get("CLAUDE_RESYNC_HOME") or os.environ.get(
        "CLAUDE_TRANSCRIPTS_HOME")
    if env:
        return env
    new = os.path.join(os.path.expanduser("~"), ".claude-resync")
    if os.path.isdir(new):
        return new
    legacy = os.path.join(os.path.expanduser("~"), ".claude-transcripts")
    return legacy if os.path.isdir(legacy) else new


REFINED = os.path.join(resync_home(), "refined")
# The repo copy of rules.json is canonical — it is the git-tracked record and
# what --write updates. install.py deploys it; the deployed copy is a artefact.
RULES = os.path.join(REPO, "rules.json")
# Labels are written by the live interceptor, so they live with the runtime.
LABELS = os.path.join(resync_home(), "data", "labels.jsonl")

SYS_PREFIX = re.compile(
    r"^\s*<(task-notification|bash-|local-command|system-reminder|command-name)")
INTERRUPT = re.compile(r"^\[Request interrupted")

RE_CORRECTION = re.compile(
    r"\b(?:there (?:are|is) no|no longer|should(?:n'?t| not) be|has been changed"
    r"|have been changed|is now|are now|not correct|incorrect|wrong|instead of"
    r"|replaced|deprecated|legacy|we moved away|moved to|is really"
    r"|i (?:meant|said)|actually)\b", re.I)

# How each rule's fires are judged. Kept here, never in the engine: the engine
# must not know that a future exists.
BASIS = {
    "R01": "auto", "R02": "tautological", "R03": "tautological",
    "R04": "auto-proxy", "R05": "hindsight", "R06": "hindsight",
    "R07": "auto", "R08": "none", "R09": "none", "R10": "auto",
    "R11": "manual", "R12": "auto", "R13": "none",
}


def ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return None


def msg_text(d):
    m = d.get("message") or {}
    c = m.get("content")
    if isinstance(c, str):
        return c
    return " ".join(x.get("text", "") for x in c or []
                    if isinstance(x, dict) and x.get("type") == "text")


def load_session_events(sid, paths):
    """Flatten a session's snapshots into one ordered, deduped event list."""
    seen, rows = set(), []
    for p in sorted(paths, key=os.path.getsize, reverse=True):
        with open(p, errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                key = d.get("uuid") or (d.get("timestamp"), d.get("type"),
                                        str(d.get("content"))[:60])
                if key in seen:
                    continue
                seen.add(key)
                rows.append(d)
    rows.sort(key=lambda d: d.get("timestamp") or "")
    return rows


def replay(sid, rows):
    """Walk a session forwards, yielding (PreSendState, message) at each human
    message. State accumulates only from events already seen, which is what
    makes the replay faithful to what the interceptor would have known.

    Ingestion is rules_engine.ingest_line — the same code intercept.py folds
    live lines through — so a divergence between historical and runtime state
    is not possible by construction."""
    acc = E.new_accumulator()
    idx = 0
    for d in rows:
        before = len(acc["msgs"])
        E.ingest_line(acc, d)
        if len(acc["msgs"]) == before:
            continue
        # A human message was just appended: that is a send point. Evaluate
        # against state EXCLUDING it, which is what the hook would have seen.
        newest = acc["msgs"][-1]
        idx += 1
        msg = {"id": "%s#%04d" % (sid[:8], idx), "at": ts(newest.get("at")),
               "text": newest.get("text") or "",
               "toks": E.tokens(newest.get("text") or ""),
               "queued": bool(newest.get("queued"))}
        held = acc["msgs"].pop()
        state = E.state_from_accumulator(
            acc, prompt=msg["text"], at=msg["at"], session_id=sid,
            cwd=d.get("cwd"), resolver=None,   # the world of that day is gone
            parse_ts=ts)
        acc["msgs"].append(held)
        yield state, msg


def load_fixture():
    groups = collections.defaultdict(list)
    for p in (glob.glob(REFINED + "/*.epoch*.jsonl")
              + glob.glob(REFINED + "/*.inflight.*.jsonl")):
        b = os.path.basename(p)
        sid = b.split(".epoch")[0].split(".inflight")[0]
        groups[sid].append(p)
    cat = E.load_catalogue()
    # Dedupe policy lives in rules_engine.dedupes so the interceptor and this
    # replay cannot disagree about what counts as one fire. See its docstring
    # for why the policy follows the action rather than the rule.
    dedupe = {r["id"] for r in cat["rules"] if E.dedupes(r["id"], cat)}
    fixture = {}
    for sid, paths in groups.items():
        rows = load_session_events(sid, paths)
        msgs, fires, seen_keys = [], [], set()
        for state, msg in replay(sid, rows):
            msgs.append(msg)
            for f in E.evaluate(state):
                if f.rule in dedupe:
                    if f.key in seen_keys:
                        continue
                    seen_keys.add(f.key)
                fires.append((f, msg))
        fixture[sid] = {"msgs": msgs, "fires": fires}
    return fixture


def later_correction(msgs, after, subject):
    """Did the user later correct this subject?

    Requires two shared vocabulary items with the triggering *reference*, not
    with the whole message. That guard is load-bearing: a long brief overlaps
    almost any later message, which produced four bogus confirmations before
    it existed. Conservative by design, so it under-counts."""
    for m in msgs:
        if not m["at"] or not after or m["at"] <= after:
            continue
        if not RE_CORRECTION.search(m["text"]):
            continue
        overlap = subject & m["toks"]
        if len(overlap) >= 2:
            return {"gap_seconds": round((m["at"] - after).total_seconds()),
                    "on": sorted(overlap)[:4],
                    "quote": re.sub(r"\s+", " ", m["text"])[:110]}
    return None


def load_user_labels():
    """Verdicts collected by intercept.py. These outrank every heuristic."""
    out = {}
    if not os.path.exists(LABELS):
        return out
    with open(LABELS, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            v = d.get("verdict")
            if d.get("rule") and d.get("fire_key") and v in ("applies",
                                                             "does-not-apply"):
                out[(d["rule"], d["fire_key"])] = (v == "applies")
    return out


def label(rule, fire, msg, session, user_labels):
    """Return (label, why). label is True / False / None."""
    hit = user_labels.get((rule, fire.key))
    if hit is not None:
        return hit, "your verdict from labels.jsonl"

    basis = BASIS.get(rule, "none")
    msgs = session["msgs"]

    if basis in ("tautological", "manual", "none"):
        return None, {"tautological": "label would restate the trigger",
                      "manual": "needs a human to compare both messages",
                      "none": "not labellable from this data"}[basis]

    if rule == "R01":
        # The engine already dropped retractions matching a sent message, so
        # every surviving fire is a genuine withholding. The refuted cases show
        # up as retractions the engine never turned into fires.
        return True, "no counterpart message anywhere in the session"

    if rule == "R07":
        # "a duplicate arrived" restates the trigger. Only a negation flip is
        # independent evidence of harm.
        if "negation differs" in (fire.detail or ""):
            return True, "negation flips between versions"
        return None, "duplicate delivered, but harm not independently shown"

    if rule == "R10":
        others = [m for m in msgs
                  if m is not msg and E.RE_NO_CRITERION.search(m["text"])]
        again = any(difflib.SequenceMatcher(
            None, msg["text"][:300], o["text"][:300]).ratio() >= 0.7
            for o in others)
        return (True, "instruction re-issued later") if again else \
               (False, "not re-issued; no evidence of dispute")

    if rule == "R12":
        return True, "API error at delivery — message lost"

    if rule == "R04":
        after = [m for m in msgs
                 if m["at"] and msg["at"] and m["at"] > msg["at"]]
        span = ((after[-1]["at"] - msg["at"]).total_seconds()
                if after and after[-1]["at"] else 0)
        if len(after) >= 3 or span >= 600:
            return True, ("session continued %d message(s) over %.0f min "
                          "without the answer (proxy, not topic resolution)"
                          % (len(after), span / 60))
        return False, "session ended shortly after"

    if basis == "hindsight":
        corr = later_correction(msgs, msg["at"], fire.subject)
        if corr:
            return True, ("user corrected this %.0f min later on %s: %s"
                          % (corr["gap_seconds"] / 60,
                             "/".join(corr["on"]), corr["quote"]))
        return None, "no later correction found — unlabelled"

    return None, "no labelling strategy"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", action="store_true",
                    help="fold results into rules.json")
    ap.add_argument("--rule", default=None, help="only this rule id")
    ap.add_argument("--list-fires", action="store_true",
                    help="print individual fires")
    args = ap.parse_args(argv)

    fixture = load_fixture()
    user_labels = load_user_labels()
    total_msgs = sum(len(s["msgs"]) for s in fixture.values())
    print("fixture: %d session(s), %d human-authored message(s)"
          % (len(fixture), total_msgs))
    if user_labels:
        print("your labels: %d verdict(s) from labels.jsonl" % len(user_labels))
    print()

    by_rule = collections.defaultdict(list)
    for sid, sess in fixture.items():
        for fire, msg in sess["fires"]:
            lab, why = label(fire.rule, fire, msg, sess, user_labels)
            by_rule[fire.rule].append((sid, fire, msg, lab, why))

    results = {}
    wanted = [args.rule] if args.rule else sorted(E.RULES)
    print("%-5s %-13s %6s %6s %6s %6s %8s %9s"
          % ("rule", "basis", "fires", "conf", "refut", "unkn", "floor",
             "labelled"))
    print("-" * 74)
    for rid in wanted:
        rows = by_rule.get(rid, [])
        basis = BASIS.get(rid, "none")
        if not rows:
            results[rid] = {"fires": 0, "basis": basis, "confirmed": 0,
                            "refuted": 0, "unknown": 0, "sessions": 0,
                            "precision": None, "precision_labelled": None}
            print("%-5s %-13s %6d %6s %6s %6s %8s %9s"
                  % (rid, basis, 0, "-", "-", "-", "-", "-"))
            continue
        tp = sum(1 for r in rows if r[3] is True)
        fp = sum(1 for r in rows if r[3] is False)
        un = sum(1 for r in rows if r[3] is None)
        floor = (tp / len(rows)) if rows else None
        lab = (tp / (tp + fp)) if (tp + fp) else None
        if basis in ("tautological", "manual", "none"):
            floor = lab = None
        results[rid] = {
            "fires": len(rows), "confirmed": tp, "refuted": fp, "unknown": un,
            "basis": basis, "sessions": len(set(r[0] for r in rows)),
            "precision": round(floor, 3) if floor is not None else None,
            "precision_labelled": round(lab, 3) if lab is not None else None,
        }
        print("%-5s %-13s %6d %6d %6d %6d %8s %9s"
              % (rid, basis, len(rows), tp, fp, un,
                 ("%.0f%%" % (floor * 100)) if floor is not None else "n/a",
                 ("%.0f%%" % (lab * 100)) if lab is not None else "n/a"))
        if args.list_fires or args.rule:
            for sid, fire, msg, l, why in rows[:25]:
                mark = {True: "TP", False: "FP", None: "??"}[l]
                print("        %s %-14s %s" % (mark, msg["id"], why[:86]))
                print("           %s" % (fire.detail or fire.why)[:84])
            if len(rows) > 25:
                print("        ... %d more" % (len(rows) - 25))

    print("\nfloor = confirmed/fires (trust this).  labelled = "
          "confirmed/(confirmed+refuted), flattered by unknowns.")
    print("basis: auto = independent fact | auto-proxy = weak stand-in | "
          "hindsight = user's later correction, under-counts")
    print("       tautological/manual/none = precision undefined, reported "
          "null | your labels override all of these")

    if args.write:
        with open(RULES) as fh:
            cat = json.load(fh)
        for r in cat["rules"]:
            res = results.get(r["id"])
            if not res:
                continue
            r["metrics"]["precision"] = res["precision"]
            r["metrics"]["backtest"] = {k: v for k, v in res.items()
                                        if k != "precision"}
        cat["provenance"]["backtest_generated"] = "2026-08-20"
        cat["provenance"]["backtest_engine"] = (
            "rules_engine.py — the same triggers intercept.py runs, so these "
            "figures describe the deployed code rather than a reimplementation.")
        tmp = RULES + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cat, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, RULES)
        print("\nwrote precision + backtest metrics into rules.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
