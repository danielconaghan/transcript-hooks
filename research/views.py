#!/usr/bin/env python3
"""Build the session document: one file per session, the whole thing, in order.

    Input  (read-only):  <resync_home>/refined/   (via session.py)
    Output            :  <resync_home>/analysis/views/session/<session>.jsonl

One view, one unit: the session. This replaced five per-unit views, and the
reason is worth keeping because it was expensive to learn.

Why the unit is the session
---------------------------

The five views cut the session into units chosen per category — an exchange,
a claim and its evidence, an action log. Every unit was a *selection*, and
every defect five independent checkers found was in the selection logic, not
in the judging: results paired to the wrong command (5.6%), commands cut at
`&&` so 17.2% of Bash collapsed to a bare `cd`, developer messages truncated
head-first with the ask at the end, successes carrying no output at all.

The deeper fault was not fixable by fixing those. Markers said the recorded
sessions' inefficiency was 55% B2 and 24% E4 — both cross-turn. Findings came back 39%
B1 and 20% C1 — both per-turn. **79% of what the developer actually complained
about produced one finding**, because no unit smaller than a session can see a
fact go stale or a fix fail to land.

Measured on `fecca80a`: five findings across four categories (B1, D1 twice, E2,
E3) all trace to one moment — seq 95-97, a blocking test suite launched against
an unverified environment and piped to `tail`, killed by the developer ten
minutes later. Under the five views those land in four separate files, each
looking like a modest standalone finding, and nothing in that architecture can
see they share an origin. The origin is the only thing a preventative action
can fire on.

What is NOT selected, and what is
----------------------------------

Records are not selected. Every developer message, assistant turn, action,
result, question, denial and marker is here, in sequence. The only thing
bounded is the SIZE of a tool result, and that is marked where it bites.

The header carries a mechanical index — repeated signatures, rewritten
targets, epoch boundaries, silent gaps. These ANNOTATE, they do not filter:
each is a fact about the records below, tedious to compute by eye over a
thousand of them, and nothing is withheld because of one. That distinction is
the whole lesson: annotation is safe, selection was not.

Volume
------

Tool output is the volume problem: 7,693 results against 3,702 text blocks.
Trimming is head AND tail — a pure head is the worst possible cut for this
data, because test runners, build tools and verify scripts all put the verdict
last, so head-only makes a passing run look like a failure.

Usage:
    python3 views.py --all                # build every session
    python3 views.py --session fecca80a   # one
    python3 views.py --list               # what exists, and size
    python3 views.py --show fecca80a      # first records, to eyeball
"""

import argparse
import collections
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "install"))

import session as S                          # noqa: E402
from plaintext import strip_decoration       # noqa: E402

VIEW = "session"
OUT = os.path.join(S.home(), "analysis", "views")

# --------------------------------------------------------------------------
# limits
# --------------------------------------------------------------------------

# The only cap that survives. Head AND tail, 3,000 each way: it touches about
# 4% of results and roughly halves total volume. The predecessor kept a head
# only, which is how a developer message of 57,811 characters lost the request
# sitting at the end of it.
HEAD_TAIL = 3000
SIG_CHARS = 300         # how much of a command's INDEX label to show
CMD_CHARS = 4000        # how much of the command ITSELF to show
GAP_SECONDS = 300       # a stretch this long with nothing said is worth naming


def trim(text, n=HEAD_TAIL):
    """Cut the MIDDLE, and say so. Never silently, and never the tail."""
    t = text or ""
    if len(t) <= 2 * n:
        return t
    return "%s\n\n[... %d characters omitted from the middle ...]\n\n%s" % (
        t[:n], len(t) - 2 * n, t[-n:])


def command_of(e):
    """The command as WRITTEN, not its index label.

    `session.signature()` caps at 160 characters because it is an identity for
    "the same action, tried again" and needs to be comparable, not complete.
    This file used to show that label as the command, which meant 3,125 of
    5,537 Bash commands — 56.4% — were displayed ending mid-token, unmarked:

        ... docker exec "$n" sh -c 'cd /*/

    A model reading that reasonably concludes the command is malformed, and one
    did. The raw input was on the event the whole time; the index was being
    used as the record. Long commands are trimmed here too, but they SAY so."""
    inp = e.get("input") or {}
    if e.get("tool") == "Bash":
        cmd = str(inp.get("command") or "")
        if cmd:
            return trim(cmd, CMD_CHARS // 2)
    return e.get("sig", "")[:SIG_CHARS]


def key(*parts):
    import hashlib
    blob = "\x1f".join(str(p or "") for p in parts)
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:16]


# --------------------------------------------------------------------------
# the mechanical index — annotation, never a filter
# --------------------------------------------------------------------------

# How work actually gets undone. Not by a second Edit — by a shell command.
# `rewritten_targets` counts writes to the same path and so cannot see any of
# this: a file written once and then removed appears nowhere in it. Measured on
# the planted fixtures, that entry was EMPTY on both sessions containing a
# complete reversal and POPULATED on the control that merely iterated — the
# signal inverted. Worse, the prompt pointed C4 at it, so a judge that had
# already spotted the reversal in the records talked itself back out of it.
RE_REVERSAL = re.compile(
    r"(?:^|[;&|]\s*)(?P<verb>rm\s+(?:-[rfv]+\s+)*"
    r"|git\s+checkout\s+--\s+"
    r"|git\s+restore\s+(?:--staged\s+)?"
    r"|git\s+rm\s+(?:--cached\s+)?"
    r"|unlink\s+)(?P<rest>[^;&|]+)", re.I)

# Reversals that name no path and undo everything uncommitted.
RE_SWEEPING = re.compile(
    r"git\s+reset\s+--hard|git\s+checkout\s+--\s+\.|git\s+stash(?!\s+pop)"
    r"|git\s+clean\s+-[a-z]*[fd]", re.I)

# A path-ish token. This originally required a dot or a slash, which silently
# missed every bare name — `rm -rf dist`, `rm -rf node_modules`, `rm output`.
# Bare words are accepted because this only ever runs on the tail of a command
# already identified as a reversal, and flags are filtered separately. A token
# that matches nothing written in the session is dropped anyway, so the cost of
# being generous is a candidate nobody looks at; the cost of being strict was a
# whole class of deletion going unseen.
RE_PATHISH = re.compile(r"[\w./~@*-]+")


def _reversed_paths(cmd):
    """Paths a command removes or restores, and whether it sweeps everything."""
    out = []
    for m in RE_REVERSAL.finditer(cmd or ""):
        for tok in RE_PATHISH.findall(m.group("rest")):
            tok = tok.strip().rstrip(";")
            if tok and not tok.startswith("-"):
                out.append(tok)
    return out, bool(RE_SWEEPING.search(cmd or ""))


def _matches(target, token):
    """Does a write to `target` correspond to `token` in a reversal command?

    Writes carry absolute paths; a reversal is usually written relative to the
    working directory, so equality is no use. Suffix match on a path boundary
    is the reliable test. Bare basenames are accepted too, because `rm flags.py`
    happens — the cost of a wrong match is one extra candidate for a reader to
    reject, and the cost of a miss is the category going unfound."""
    if not target or not token:
        return False
    t = target.rstrip("/")
    tok = token.lstrip("./").rstrip("/")
    if not tok:
        return False
    return t.endswith("/" + tok) or t == tok or os.path.basename(t) == tok


def _seconds(a, b):
    from datetime import datetime
    try:
        fmt = lambda s: datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return (fmt(b) - fmt(a)).total_seconds()
    except Exception:
        return 0.0


def index_from_records(recs, epochs):
    """Facts about the records, computed FROM the records.

    Deliberately takes the document rather than the raw event stream, so the
    fixtures in mock-views/ and the real corpus go through the same code. The
    first version of this took events, the mock generator grew its own copy,
    and within a day the two disagreed — the mocks were still reporting an
    index that could not see reversals after this one had been taught to.

    Every entry here ANNOTATES the records; none of them removes anything, and
    none is a finding on its own.
    """
    acts = [r for r in recs if r["who"] == "action"]
    # Pair each action with its result: the next result naming that command.
    outcome = {}
    for i, r in enumerate(recs):
        if r["who"] != "action":
            continue
        for nxt in recs[i + 1:]:
            if nxt["who"] == "result" and nxt.get("of_command") == r.get("command"):
                outcome[r["seq"]] = "ok" if nxt.get("exit_ok") else "error"
                break

    WRITES = ("Edit", "Write", "NotebookEdit")
    by_sig, by_target = collections.defaultdict(list), collections.defaultdict(list)
    for a in acts:
        o = outcome.get(a["seq"], "no-result")
        if a.get("tool") in WRITES:
            if a.get("target"):
                by_target[a["target"]].append((a["seq"], o))
        else:
            by_sig[a.get("command", "")].append((a["seq"], o))

    written = [(a["seq"], a["target"]) for a in acts
               if a.get("tool") in WRITES and a.get("target")]
    reversals = []
    for a in acts:
        if a.get("tool") != "Bash":
            continue
        cmd = a.get("command") or ""
        toks, sweeping = _reversed_paths(cmd)
        undone = sorted({(w, t) for w, t in written if w < a["seq"]
                         and any(_matches(t, tok) for tok in toks)})
        if sweeping and not undone:
            undone = sorted({(w, t) for w, t in written if w < a["seq"]})
        if undone:
            reversals.append({"at_seq": a["seq"], "command": cmd[:SIG_CHARS],
                              "sweeping": sweeping or None,
                              "undoes": [{"seq": w, "target": t}
                                         for w, t in undone]})

    gaps, prev = [], None
    for r in recs:
        if prev is not None and r.get("at"):
            secs = _seconds(prev.get("at"), r.get("at"))
            if secs >= GAP_SECONDS:
                gaps.append({"from_seq": prev["seq"], "to_seq": r["seq"],
                             "seconds": int(secs),
                             "from": prev.get("at"), "to": r.get("at")})
        if r.get("at"):
            prev = r

    return {
        "records": len(recs), "actions": len(acts),
        "turns": len({r["turn"] for r in recs}),
        "epochs": epochs,
        # C2 is the same approach retried into the same WALL, so the outcomes
        # are the point: re-running a passing command is not a finding.
        "repeated_commands": sorted(
            ({"command": sg[:SIG_CHARS], "seqs": [x for x, _ in v],
              "outcomes": [o for _, o in v], "times": len(v),
              "failures": sum(1 for _, o in v if o == "error")}
             for sg, v in by_sig.items() if len(v) > 1),
            key=lambda x: (-x["failures"], -x["times"]))[:40],
        # Iterated on. NOT the same as undone — see reversed_targets.
        "rewritten_targets": sorted(
            ({"target": t, "seqs": [x for x, _ in v],
              "outcomes": [o for _, o in v], "times": len(v)}
             for t, v in by_target.items() if len(v) > 1),
            key=lambda x: -x["times"])[:40],
        "reversed_targets": reversals[:40],
        "silent_gaps": gaps[:40],
        "markers": [{"seq": r["seq"], "note": r.get("note")}
                    for r in recs if r["who"] == "marker"],
    }


# --------------------------------------------------------------------------
# the document
# --------------------------------------------------------------------------

def document(sid, evs, acts_by_id):
    """The session, in order, readable, with output bounded. No selection."""
    evs_by_id = {e.get("tool_use_id"): e for e in evs
                 if e["kind"] == "tool_use"}
    out = []
    for e in evs:
        k = e["kind"]
        if k == "developer":
            # A meta record is the harness talking (a command envelope, a
            # queued-message notice), not the developer. Kept out of the
            # document because a judge reads it as something they said.
            if e.get("meta"):
                continue
            out.append({"seq": e["seq"], "turn": e["turn"], "at": e["at"],
                        "who": "developer",
                        "machine_generated": bool(e.get("machine")) or None,
                        "text": strip_decoration(e["text"])})
        elif k == "assistant_text":
            out.append({"seq": e["seq"], "turn": e["turn"], "at": e["at"],
                        "who": "assistant", "text": strip_decoration(e["text"])})
        elif k == "tool_use":
            # `at` on BOTH an action and its result, because the pair is the
            # only place a duration exists and D1 is entirely about duration.
            # Without it a judge has no time for a command and infers one from
            # the nearest thing that has one: a real finding about a 10-minute
            # blocking run was reported as starting at 13:21 when the command
            # ran at 13:33. The finding was right and its evidence was wrong.
            out.append({"seq": e["seq"], "turn": e["turn"], "at": e["at"],
                        "who": "action",
                        "tool": e["tool"], "command": command_of(e),
                        "target": e.get("target")})
        elif k == "tool_result":
            a = acts_by_id.get(e.get("tool_use_id")) or {}
            src = evs_by_id.get(e.get("tool_use_id")) or {}
            out.append({"seq": e["seq"], "turn": e["turn"], "at": e["at"],
                        "who": "result",
                        # Repeats the command rather than pointing at its seq.
                        # 5.7% of results do not immediately follow their own
                        # action — parallel calls interleave — so adjacency is
                        # not a safe way to read the pairing back.
                        "of_command": (command_of(src) if src
                                       else a.get("sig", "")[:SIG_CHARS]),
                        # This is the EXIT CODE. 49% of commands here pipe or
                        # redirect, so a failing command often exits 0 — the
                        # flag is a hint and the text is the evidence.
                        "exit_ok": e["ok"], "bytes": e["bytes"],
                        "text": trim(e["text"])})
        elif k == "question":
            out.append({"seq": e["seq"], "turn": e["turn"], "who": "question",
                        "asked": e.get("asked"), "options": e.get("options"),
                        "outcome": e.get("outcome"), "answers": e.get("answers")})
        elif k == "denial":
            out.append({"seq": e["seq"], "turn": e["turn"], "who": "denial",
                        "kind": e["denial_kind"], "by_developer": e["by_user"],
                        "feedback": e.get("feedback")})
        elif k == "marker":
            out.append({"seq": e["seq"], "turn": e["turn"], "who": "marker",
                        "note": e.get("note")})
    return out


def build(sid):
    evs = S.events(sid)
    acts = S.actions(evs)
    recs = document(sid, evs, {a.get("tool_use_id"): a for a in acts})
    eps = [{"epoch": ep, "first_seq": lo, "last_seq": hi}
           for ep, (lo, hi) in sorted(S.epochs(evs).items())]
    head = {"kind": "header", "view": VIEW, "session": sid,
            "unit": "the whole session",
            "records": len(recs),
            # Stamped at build time and never recomputed, so a verdict stays
            # attached to the exact document it was formed on.
            "unit_key": key(sid, VIEW, len(recs),
                            recs[0].get("at") if recs else "",
                            recs[-1].get("at") if recs else ""),
            "params": {"head_tail": HEAD_TAIL, "sig_chars": SIG_CHARS,
                       "gap_seconds": GAP_SECONDS},
            "index": index_from_records(recs, eps)}
    return head, recs


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def ensure():
    d = os.path.join(OUT, VIEW)
    os.makedirs(d, mode=0o700, exist_ok=True)
    gi = os.path.join(OUT, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as fh:
            fh.write("# Auto-created by views.py. Deterministic views of the "
                     "recorded sessions, verbatim — never commit.\n*\n")
    return d


def write(sid, head, recs):
    path = os.path.join(ensure(), "%s.jsonl" % sid)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(head, ensure_ascii=False, default=str) + "\n")
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    os.chmod(path, 0o600)
    return path


def read(sid):
    """(header, [records]) exactly as written. No regeneration."""
    p = os.path.join(OUT, VIEW, "%s.jsonl" % sid)
    if not os.path.exists(p):
        return None, []
    head, recs = None, []
    with open(p, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("kind") == "header":
                head = d
            else:
                recs.append(d)
    return head, recs


def built():
    return sorted(os.path.basename(p)[:-6]
                  for p in glob.glob(os.path.join(OUT, VIEW, "*.jsonl")))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--session", nargs="+", metavar="ID")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--show", metavar="ID")
    args = ap.parse_args(argv)

    have = S.sessions()

    if args.show:
        sid = S.resolve(args.show, have)
        head, recs = build(sid)
        print(json.dumps(head, indent=1, ensure_ascii=False, default=str))
        print("\n--- first 6 records of %d ---" % len(recs))
        for r in recs[:6]:
            print(json.dumps(r, ensure_ascii=False, default=str)[:600])
        return 0

    if args.list:
        rows = []
        for sid in built():
            p = os.path.join(OUT, VIEW, "%s.jsonl" % sid)
            lines = open(p, errors="replace").readlines()
            rows.append((int(sum(len(x) for x in lines) / 2.1),
                         len(lines) - 1, sid))
        rows.sort()
        print("%-10s %9s %10s" % ("session", "records", "~tokens"))
        print("-" * 32)
        for tok, n, sid in rows:
            print("%-10s %9d %10s" % (sid[:8], n, "{:,}".format(tok)))
        if rows:
            tot = sum(r[0] for r in rows)
            print("\n%d session(s), ~%s tokens total, median ~%s"
                  % (len(rows), "{:,}".format(tot),
                     "{:,}".format(rows[len(rows) // 2][0])))
        return 0

    if not (args.all or args.session):
        ap.print_help()
        return 2

    sids = ([S.resolve(w, have) for w in args.session] if args.session else have)
    n = 0
    for sid in sids:
        head, recs = build(sid)
        write(sid, head, recs)
        n += len(recs)
    print("%d session(s), %d records -> %s/%s/<session>.jsonl"
          % (len(sids), n, OUT, VIEW))
    print("\nNothing sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
