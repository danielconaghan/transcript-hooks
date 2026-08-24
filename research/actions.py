#!/usr/bin/env python3
"""The action log: what the assistant DID, and where doing it went in circles.

    Input  (read-only):  <resync_home>/refined/   (via session.py)
    Output            :  <resync_home>/views/actions/<session>.jsonl

Covers three categories from `INEFFICIENCIES.md`, and they are the three that
need almost no model at all:

    C2  repeated failing action — the same approach retried into the same wall
    C4  work undone — output written and then reversed
    B3  post-compaction rework — work redone because context was lost

This is the shape `pairs.py` and `chat.py` cannot reach. Both read speech; this
reads the 7,696 tool calls and 7,693 results that speech does not mention. The
whole point of `session.py` carrying `sig`, `target`, `turn` and `epoch` on
every action was to make these three answerable by comparison rather than by
judgement.

Why the signature is the whole design
-------------------------------------

C2 is "the same action, tried again". Without a normalised signature that is a
judgement call; with one it is string equality. `session.signature()` collapses
a Bash command to its first few words and a file tool to its path, so the same
attempt with a different heredoc still matches.

Getting that definition right mattered more than the detector. Measured three
ways on this corpus:

    consecutive failures, any action     9 runs   <- a much weaker claim
    same signature repeated 3+ times   226 runs   <- mostly ordinary iteration
    ...of which 2+ failed                1 run    <- the real C2

Nine runs of "three failures in a row" sounds like a finding until you notice
the failures are different commands, which is not retrying into a wall. And 226
repeats sounds alarming until you notice most succeed, which is just work. Only
the intersection means anything, and it is rare. An earlier version of
`INEFFICIENCIES.md` cited the 9; that has been corrected.

What is deterministic here and what is not
-------------------------------------------

C2 and B3 are decided here outright — a repeat is a repeat, and an epoch
boundary is a fact `reduce.py` recorded. Neither needs a model.

C4 is only *proposed* here. "Written then reversed" has a deterministic
skeleton — a target written, then a revert-shaped command naming it, or written
and later deleted — but whether that was waste or an intended change of mind is
a judgement, and the payload exists so a model can make it. Candidates are
labelled `needs_review` rather than reported as findings.

Payloads are write-only. Nothing reads them; they are there so the input to a
judgement can be read by eye. That is the lesson from `friction/`: an
intermediate file that scripts DEPEND on constrains what they can see, and one
they merely write does not.

Usage:
    python3 actions.py --stats                  # what the corpus contains
    python3 actions.py --c2 [--session a0c27]   # repeated failing actions
    python3 actions.py --c4                     # work-undone candidates
    python3 actions.py --b3                     # post-compaction rework
    python3 actions.py --all --write            # store the view for every one
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

import session as S       # noqa: E402

# The deterministic view. `actions.py` has no paid output yet, so unlike
# pairs and chat it writes only here — but it uses the same parent so every
# view of the corpus sits together and the instrument roots stay for verdicts.
OUT_DIR = os.path.join(S.home(), "views", "actions")

# A run this long, with this many failures in it, is retrying rather than
# iterating. Both floors are needed: see the docstring's three measurements.
C2_MIN_RUN = 3
C2_MIN_FAILURES = 2

# Commands whose shape is "put it back". Deliberately a small, literal list —
# a broad regex here would turn every `git status` into evidence of a revert.
REVERT_SHAPES = ("git checkout", "git restore", "git reset", "git revert",
                 "git stash", "rm ", "rm -", "mv ")

# A target rewritten this many times in one session is worth a look. Not a
# finding on its own: iterating on a file is normal work.
C4_MIN_WRITES = 3

WRITE_TOOLS = ("Write", "Edit", "NotebookEdit")


def ensure_out():
    d = OUT_DIR
    os.makedirs(d, mode=0o700, exist_ok=True)
    parent = os.path.dirname(OUT_DIR)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass
    gi = os.path.join(parent, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as fh:
            fh.write("# Auto-created. Deterministic views of the corpus, "
                     "verbatim — never commit.\n*\n")
    return d


# --------------------------------------------------------------------------
# C2 — repeated failing action
# --------------------------------------------------------------------------

def find_c2(acts):
    """Runs of one signature, repeated, mostly failing.

    Consecutive by signature rather than adjacent in time: an assistant that
    retries the same command with one unrelated call in between is still
    retrying, so the run is broken only by a *different* signature."""
    out, run = [], []
    for a in acts + [None]:
        if run and a is not None and a["sig"] == run[-1]["sig"]:
            run.append(a)
            continue
        if len(run) >= C2_MIN_RUN:
            fails = [x for x in run if x["outcome"] != "ok"]
            if len(fails) >= C2_MIN_FAILURES:
                out.append({
                    "category": "C2", "sig": run[0]["sig"],
                    "tool": run[0]["tool"], "attempts": len(run),
                    "failures": len(fails),
                    "turn": run[0]["turn"], "at": run[0]["at"],
                    "seq_from": run[0]["seq"], "seq_to": run[-1]["seq"],
                    "outcomes": [x["outcome"] for x in run],
                    "errors": [x["result_head"][:300] for x in fails[:3]],
                    "verdict": "confirmed",
                })
        run = [] if a is None else [a]
    return out


# --------------------------------------------------------------------------
# C4 — work undone
# --------------------------------------------------------------------------

def find_c4(acts):
    """Candidates for output that was written and then reversed.

    Proposed, never concluded. Three deterministic shapes:
      * a target written, then a revert-shaped command naming it
      * a target written, then deleted
      * a target written many times over
    Only the first two are much more than noise, and even they can be an
    intended change of mind. Everything here is `needs_review`."""
    writes = collections.defaultdict(list)
    for a in acts:
        if a["tool"] in WRITE_TOOLS and a["target"]:
            writes[a["target"]].append(a)

    out = []
    for tgt, ws in writes.items():
        base = os.path.basename(tgt)
        reverts = [a for a in acts
                   if a["seq"] > ws[0]["seq"]
                   and a["tool"] == "Bash"
                   and any(sh in a["sig"] for sh in REVERT_SHAPES)
                   and (base in a["sig"] or tgt in a["sig"])]
        if reverts:
            out.append({
                "category": "C4", "target": tgt, "shape": "reverted",
                "writes": len(ws), "at": ws[0]["at"], "turn": ws[0]["turn"],
                "revert_sig": reverts[0]["sig"][:160],
                "revert_at": reverts[0]["at"],
                "seq_from": ws[0]["seq"], "seq_to": reverts[0]["seq"],
                "verdict": "needs_review",
            })
        elif len(ws) >= C4_MIN_WRITES:
            out.append({
                "category": "C4", "target": tgt, "shape": "rewritten",
                "writes": len(ws), "at": ws[0]["at"], "turn": ws[0]["turn"],
                "turns": sorted({a["turn"] for a in ws}),
                "seq_from": ws[0]["seq"], "seq_to": ws[-1]["seq"],
                "verdict": "needs_review",
            })
    return out


# --------------------------------------------------------------------------
# B3 — post-compaction rework
# --------------------------------------------------------------------------

def find_b3(acts, evs):
    """The same action performed again on the far side of a compaction.

    An epoch boundary is where the transcript and the context permanently
    diverge — 42,449 tokens became 9,114 in the one observed case, keeping 5 of
    46 messages. Work repeated across that boundary is the visible cost of what
    was forgotten. Repeating an action *within* an epoch is ordinary; repeating
    it across one is the signal, which is why `epoch` rides on every record.

    Reads only, deliberately: re-reading a file after a compaction is the
    clearest case of paying to recover what was already known, and unlike a
    re-run build it cannot be explained by the world having changed."""
    eps = S.epochs(evs)
    if len(eps) < 2:
        return []
    first = {}
    out = []
    for a in acts:
        ep = a["epoch"]
        if ep is None or a["tool"] not in ("Read", "Grep", "Glob"):
            continue
        prev = first.get(a["sig"])
        if prev is None:
            first[a["sig"]] = a
        elif prev["epoch"] is not None and ep > prev["epoch"]:
            out.append({
                "category": "B3", "sig": a["sig"], "tool": a["tool"],
                "target": a["target"],
                "first_epoch": prev["epoch"], "first_at": prev["at"],
                "again_epoch": ep, "again_at": a["at"],
                "seq_from": prev["seq"], "seq_to": a["seq"],
                "verdict": "confirmed",
            })
            first[a["sig"]] = a
    return out


# --------------------------------------------------------------------------

def analyse(sid):
    evs = S.events(sid)
    acts = S.actions(evs)
    return acts, evs, find_c2(acts) + find_c4(acts) + find_b3(acts, evs)


def write_payload(sid, acts, findings, out_dir):
    """Store the action log and the candidates it produced.

    The whole log goes in, not only the findings: a category nobody has written
    a detector for yet is still answerable from the file, and the point of
    storing it is that you can look."""
    path = os.path.join(out_dir, "%s.jsonl" % sid)
    counts = collections.Counter(f["category"] for f in findings)
    head = {"kind": "header", "session": sid, "actions": len(acts),
            "findings": dict(counts),
            "params": {"c2_min_run": C2_MIN_RUN,
                       "c2_min_failures": C2_MIN_FAILURES,
                       "c4_min_writes": C4_MIN_WRITES,
                       "revert_shapes": list(REVERT_SHAPES)}}
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(head, ensure_ascii=False, default=str) + "\n")
        for f in findings:
            fh.write(json.dumps(dict(f, kind="finding", session=sid),
                                ensure_ascii=False, default=str) + "\n")
        for a in acts:
            fh.write(json.dumps(dict(a, kind="action"),
                                ensure_ascii=False, default=str) + "\n")
    os.chmod(path, 0o600)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--session", nargs="+", metavar="ID")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--c2", action="store_true", help="repeated failing action")
    ap.add_argument("--c4", action="store_true", help="work-undone candidates")
    ap.add_argument("--b3", action="store_true", help="post-compaction rework")
    ap.add_argument("--write", action="store_true",
                    help="store the action log and candidates per session")
    args = ap.parse_args(argv)

    have = S.sessions()
    if args.session:
        wanted = [S.resolve(w, have) for w in args.session]
    else:
        wanted = have

    want_cat = {c for c, on in (("C2", args.c2), ("C4", args.c4),
                                ("B3", args.b3)) if on}
    out_dir = ensure_out() if args.write else None

    tot_actions = 0
    all_found = []
    epoch_sessions = 0
    for sid in wanted:
        acts, evs, found = analyse(sid)
        tot_actions += len(acts)
        if len(S.epochs(evs)) > 1:
            epoch_sessions += 1
        all_found += [dict(f, session=sid) for f in found]
        if args.write:
            write_payload(sid, acts, found, out_dir)

    if args.write:
        c = collections.Counter(f["category"] for f in all_found)
        print("%d session(s), %s action(s) -> %s/<session>.jsonl"
              % (len(wanted), "{:,}".format(tot_actions), out_dir))
        print("candidates written: %s" % (dict(c) or "none"))
        print("Nothing sent.")
        return 0

    if args.stats or not want_cat:
        by = collections.Counter(f["category"] for f in all_found)
        print("%d session(s), %s action(s), %d with more than one epoch"
              % (len(wanted), "{:,}".format(tot_actions), epoch_sessions))
        print("\ncategory  finding(s)  status")
        print("-" * 46)
        for cat, label in (("C2", "confirmed, no model needed"),
                           ("C4", "needs_review — model judges"),
                           ("B3", "confirmed, no model needed")):
            print("%-9s %10d  %s" % (cat, by.get(cat, 0), label))
        if not want_cat:
            print("\n--c2 / --c4 / --b3 to list them, --write to store payloads")
        return 0

    shown = [f for f in all_found if f["category"] in want_cat]
    if not shown:
        print("no candidates for %s" % ", ".join(sorted(want_cat)))
        return 0
    for f in shown:
        s = f["session"][:8]
        if f["category"] == "C2":
            print("%s C2  %dx (%d failed)  %s"
                  % (s, f["attempts"], f["failures"], f["sig"][:64]))
            for e in f["errors"][:1]:
                print("        %s" % " ".join(e.split())[:96])
        elif f["category"] == "C4":
            print("%s C4  %-10s writes=%d  %s"
                  % (s, f["shape"], f["writes"], (f["target"] or "")[-64:]))
            if f.get("revert_sig"):
                print("        reverted by: %s" % f["revert_sig"][:80])
        else:
            print("%s B3  epoch %s -> %s  %s"
                  % (s, f["first_epoch"], f["again_epoch"], f["sig"][:60]))
    print("\n%d candidate(s)" % len(shown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
