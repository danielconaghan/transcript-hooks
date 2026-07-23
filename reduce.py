#!/usr/bin/env python3
"""Change-reduction ETL for the context-churn corpus.

Turns the large, highly-redundant raw capture corpus into a small, surveyable
*change log* per session, by diffing consecutive transcript snapshots at a
meaningful unit grain.

This is **reduction, NOT detection**. Its only job is to make the data small
enough to actually look at, while preserving the change signal and deciding
*nothing* about it. It records what left context and what entered context
between consecutive captures, as plain facts, and stops there. It does not
classify, score, flag, or interpret any change, and it defines no change
"categories" — those are for a human to discover later by eyeballing this
output. It never modifies, moves, or deletes anything in the raw corpus.

    Input  (read-only):  <transcripts_home>/corpus
    Output (derived)  :  <transcripts_home>/refined

Per session, in the recorder's `count` order:

  1. Join each event record (<sid>.events.jsonl) to its transcript snapshot
     (<sid>.<count>.transcript.jsonl) on `count`.
  2. Segment each snapshot into units — one transcript line = one unit.
  3. Diff consecutive snapshots on unit *id*: units present in N and absent in
     N+1 (left), and absent in N but present in N+1 (entered).
  4. Emit one JSON line per consecutive pair: the two counts, the event types
     either side (so scale — turn / compaction / session — is derivable),
     what left, what entered, and references back to the source snapshots.

Segmentation grain (the one real design choice):

  Claude Code's transcript already writes one line per content block — each
  assistant thinking/text/tool_use block, each tool_result, each user message
  is its own line carrying a stable `uuid`. So "one line = one unit, keyed by
  `uuid`" *is* message/content-block grain, and diffing on `uuid` cleanly
  distinguishes "this genuinely left" from "this got reworded" (a reworded
  block keeps its uuid and so is neither left nor entered). Lines without a
  `uuid` (mode / permission-mode / ai-title / last-prompt / file-history) fall
  back to a deterministic digest of their content (volatile fields stripped),
  which is stable across snapshots and needs no fuzzy text matching.

  Choosing the grain is NOT classification. Every unit carries its verbatim
  transcript `type` (and role / block-types / tool ids where present) so a
  human can filter the survey however they like — the tool itself judges
  nothing.

Properties: offline batch tool (never a hook); read-only on the corpus;
deterministic / idempotent (re-running reproduces identical output); fully
traceable (every entry references the counts and snapshot files it came from).

Secrets: left/entered content is verbatim, so the refined output is as
secrets-bearing as the raw corpus. It is written 0700 with a self-contained
`.gitignore` of `*`. Use --no-preview for a structural-only log (ids, types,
sizes — no verbatim content) when a log needs to be shared.

Usage:
    python3 reduce.py                      # reduce every session
    python3 reduce.py --session <sid>      # one session
    python3 reduce.py --no-preview         # structural only, no verbatim content
    python3 reduce.py --preview-chars 400  # longer content previews
    python3 reduce.py --skip-existing       # skip sessions already reduced
"""

import argparse
import hashlib
import json
import os
import re
import sys

SNAP_RE = re.compile(r"^(.*)\.(\d+)\.transcript\.jsonl$")

# Fields stripped before digesting/previewing a line that has no `uuid`. These
# vary snapshot-to-snapshot or are pure plumbing, so removing them makes an
# id-less unit's identity depend only on its actual content — stable when the
# content is unchanged, changed only when the content genuinely changes.
VOLATILE_KEYS = frozenset({
    "timestamp", "cwd", "gitBranch", "version", "requestId", "userType",
    "isSidechain", "entrypoint", "uuid", "parentUuid", "logicalParentUuid",
    "sessionId", "session_id", "effort", "durationMs", "captured_at", "ts",
})


def transcripts_home():
    """Mirror the recorder's home resolution so both halves agree by default."""
    return (os.environ.get("CLAUDE_TRANSCRIPTS_HOME")
            or os.path.join(os.path.expanduser("~"), ".claude-transcripts"))


def ensure_out(path):
    """Create the refined dir (0700) with a self-contained `.gitignore` of `*`.

    The refined output is as secrets-bearing as the raw corpus (left/entered
    content is verbatim), so it is ignored by git regardless of surrounding
    project rules and never accidentally committed."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    gi = os.path.join(path, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as fh:
            fh.write("# Auto-created by the change-reduction ETL (reduce.py).\n"
                     "# Derived from a secrets-bearing corpus — never commit it.\n"
                     "*\n")


def _sha(s):
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:12]


def _block_summary(b):
    """Return (block_type, text) for one content block. Verbatim extraction —
    no interpretation of what the block means."""
    if not isinstance(b, dict):
        return ("_", "" if b is None else str(b))
    bt = b.get("type")
    if bt == "text":
        return (bt, b.get("text", "") or "")
    if bt == "thinking":
        return (bt, b.get("thinking", "") or "")
    if bt == "tool_use":
        inp = json.dumps(b.get("input", {}), ensure_ascii=False, default=str,
                         sort_keys=True)
        return (bt, "%s %s" % (b.get("name", ""), inp))
    if bt == "tool_result":
        c = b.get("content")
        if isinstance(c, list):
            parts = []
            for x in c:
                if isinstance(x, dict):
                    parts.append(x.get("text") or ("<%s>" % x.get("type", "?")))
                else:
                    parts.append(str(x))
            c = " ".join(parts)
        return (bt, "" if c is None else str(c))
    # image / document / anything else: dump verbatim, still no judgement.
    return (bt or "_", json.dumps(b, ensure_ascii=False, default=str,
                                  sort_keys=True))


def build_unit(raw_line, preview_chars, include_preview):
    """Turn one transcript line into a unit record: a stable id, its verbatim
    structural metadata, a size, and (optionally) a truncated content preview.

    Truncation is reduction, not interpretation. The `type` / `role` /
    `blocks` / `tool_name` fields are verbatim structural facts already present
    in the transcript — surfacing them is not classification."""
    try:
        obj = json.loads(raw_line)
        if not isinstance(obj, dict):
            raise ValueError
    except Exception:
        # Unparseable line (e.g. a snapshot copied mid-write). Keep it as a unit
        # keyed by its own bytes so it still participates in the diff faithfully.
        unit = {"id": "_raw:" + _sha(raw_line), "type": "_unparseable",
                "chars": len(raw_line)}
        if include_preview:
            unit["preview"] = raw_line[:preview_chars]
        return unit

    uuid = obj.get("uuid")
    msg = obj.get("message")
    unit = {"type": obj.get("type")}
    tool_use_id = None
    tool_name = None

    if isinstance(msg, dict):
        role = msg.get("role")
        if role is not None:
            unit["role"] = role
        content = msg.get("content")
        if isinstance(content, str):
            unit["blocks"] = ["str"]
            content_str = content
        elif isinstance(content, list):
            block_types, summaries = [], []
            for b in content:
                bt, txt = _block_summary(b)
                block_types.append(bt)
                summaries.append("[%s] %s" % (bt, txt))
                if isinstance(b, dict):
                    if b.get("type") == "tool_use":
                        tool_use_id = b.get("id") or tool_use_id
                        tool_name = b.get("name") or tool_name
                    elif b.get("type") == "tool_result":
                        tool_use_id = b.get("tool_use_id") or tool_use_id
            unit["blocks"] = block_types
            content_str = " | ".join(summaries)
        else:
            content_str = json.dumps(content, ensure_ascii=False, default=str,
                                     sort_keys=True)
    else:
        # Non-message line (mode / ai-title / file-history-snapshot / system …).
        # Its content-of-record is everything minus the volatile plumbing.
        norm = {k: v for k, v in obj.items() if k not in VOLATILE_KEYS}
        content_str = json.dumps(norm, ensure_ascii=False, default=str,
                                 sort_keys=True)

    if uuid:
        unit["id"] = uuid
    else:
        unit["id"] = "%s:%s" % (unit.get("type") or "_",
                                _sha("%s|%s" % (unit.get("type"), content_str)))

    if tool_use_id:
        unit["tool_use_id"] = tool_use_id
    if tool_name:
        unit["tool_name"] = tool_name
    unit["chars"] = len(content_str)
    if include_preview:
        unit["preview"] = content_str[:preview_chars]
    return unit


def load_units(path, preview_chars, include_preview):
    """Load a snapshot into (id -> unit) plus id order-of-appearance.

    First occurrence of an id wins (id-less lines with identical content
    dedupe, which is the intended set semantics). Order is transcript order so
    left/entered lists read top-to-bottom."""
    units, order = {}, []
    if not path or not os.path.exists(path):
        return units, order
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            unit = build_unit(line, preview_chars, include_preview)
            uid = unit["id"]
            if uid not in units:
                units[uid] = unit
                order.append(uid)
    return units, order


def discover_sessions(corpus):
    """Session ids present in the corpus, from events files and, as a fallback,
    from stray snapshot filenames."""
    sids = set()
    try:
        names = os.listdir(corpus)
    except OSError:
        return []
    for name in names:
        if name.endswith(".events.jsonl"):
            sids.add(name[:-len(".events.jsonl")])
        else:
            m = SNAP_RE.match(name)
            if m:
                sids.add(m.group(1))
    return sorted(sids)


def load_captures(corpus, sid):
    """Ordered-by-count capture list for a session.

    Prefer the events log (it carries the event type, hence the delta scale);
    fall back to snapshot filenames for any counts the events log doesn't
    mention, so a session with a missing/partial events file still reduces."""
    captures = {}  # count -> capture dict

    events_path = os.path.join(corpus, "%s.events.jsonl" % sid)
    if os.path.exists(events_path):
        with open(events_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                count = rec.get("count")
                if not isinstance(count, int) or count in captures:
                    continue
                snap = rec.get("snapshot") or "%s.%d.transcript.jsonl" % (sid, count)
                captures[count] = {
                    "count": count,
                    "event": rec.get("event"),
                    "snapshot": snap,
                    "tool_use_id": rec.get("tool_use_id"),
                    "prompt_id": rec.get("prompt_id"),
                    "snapshot_error": rec.get("snapshot_error"),
                }

    # Fill in any snapshot files not represented in the events log.
    try:
        names = os.listdir(corpus)
    except OSError:
        names = []
    for name in names:
        m = SNAP_RE.match(name)
        if not m or m.group(1) != sid:
            continue
        count = int(m.group(2))
        if count not in captures:
            captures[count] = {
                "count": count, "event": None, "snapshot": name,
                "tool_use_id": None, "prompt_id": None, "snapshot_error": None,
            }

    return [captures[c] for c in sorted(captures)]


def reduce_session(corpus, out_dir, sid, preview_chars, include_preview,
                   skip_existing, verbose):
    """Write <sid>.change.jsonl. Returns (pairs, left_total, entered_total)."""
    out_path = os.path.join(out_dir, "%s.change.jsonl" % sid)
    if skip_existing and os.path.exists(out_path):
        if verbose:
            print("  skip (exists): %s" % os.path.basename(out_path))
        return (0, 0, 0)

    captures = load_captures(corpus, sid)
    if len(captures) < 2:
        if verbose:
            print("  skip (%d capture(s), nothing to diff): %s"
                  % (len(captures), sid))
        return (0, 0, 0)

    lines = []
    left_total = entered_total = 0

    prev = captures[0]
    prev_path = os.path.join(corpus, prev["snapshot"])
    prev_units, prev_order = load_units(prev_path, preview_chars, include_preview)

    for cur in captures[1:]:
        cur_path = os.path.join(corpus, cur["snapshot"])
        cur_units, cur_order = load_units(cur_path, preview_chars, include_preview)

        left = [prev_units[i] for i in prev_order if i not in cur_units]
        entered = [cur_units[i] for i in cur_order if i not in prev_units]
        left_total += len(left)
        entered_total += len(entered)

        entry = {
            "session_id": sid,
            "from_count": prev["count"],
            "to_count": cur["count"],
            "from_event": prev["event"],
            "to_event": cur["event"],
            "from_snapshot": prev["snapshot"],
            "to_snapshot": cur["snapshot"],
            "from_units": len(prev_order),
            "to_units": len(cur_order),
            "left": left,
            "entered": entered,
        }
        # Traceability extras from the "to" capture — the event that produced
        # this snapshot. Included only when present; recorded, never judged.
        if cur.get("tool_use_id"):
            entry["to_tool_use_id"] = cur["tool_use_id"]
        if cur.get("prompt_id"):
            entry["to_prompt_id"] = cur["prompt_id"]
        missing = [k for k, cap in (("from_snapshot_missing", prev),
                                    ("to_snapshot_missing", cur))
                   if not os.path.exists(os.path.join(corpus, cap["snapshot"]))]
        for k in missing:
            entry[k] = True

        lines.append(json.dumps(entry, ensure_ascii=False, default=str))

        prev, prev_units, prev_order = cur, cur_units, cur_order

    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + ("\n" if lines else ""))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, out_path)  # atomic; re-run overwrites deterministically.

    if verbose:
        print("  %s -> %d delta(s), %d left / %d entered"
              % (os.path.basename(out_path), len(lines), left_total, entered_total))
    return (len(lines), left_total, entered_total)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Reduce the raw capture corpus to a per-session change log "
                    "by diffing consecutive transcript snapshots. Reduction "
                    "only — no classification, scoring, or judgement.")
    ap.add_argument("--corpus", default=None,
                    help="raw corpus dir (default: <transcripts_home>/corpus, "
                         "read-only)")
    ap.add_argument("--out", default=None,
                    help="refined output dir (default: <transcripts_home>/refined)")
    ap.add_argument("--session", default=None,
                    help="reduce only this session id (default: all)")
    ap.add_argument("--preview-chars", type=int, default=200,
                    help="max chars of verbatim content per unit preview "
                         "(default: 200)")
    ap.add_argument("--no-preview", action="store_true",
                    help="omit verbatim content previews entirely (structural "
                         "log only — safer to share)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="skip sessions whose change log already exists")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="only print the final summary")
    args = ap.parse_args(argv)

    corpus = args.corpus or os.path.join(transcripts_home(), "corpus")
    out_dir = args.out or os.path.join(transcripts_home(), "refined")
    verbose = not args.quiet
    include_preview = not args.no_preview

    if not os.path.isdir(corpus):
        print("error: corpus not found: %s" % corpus, file=sys.stderr)
        return 1
    if os.path.abspath(corpus) == os.path.abspath(out_dir):
        print("error: --out must differ from --corpus (never write into the "
              "raw corpus)", file=sys.stderr)
        return 1

    ensure_out(out_dir)

    sessions = ([args.session] if args.session
                else discover_sessions(corpus))
    if not sessions:
        print("no sessions found in %s" % corpus)
        return 0

    if verbose:
        print("corpus: %s\nrefined: %s\nsessions: %d\n"
              % (corpus, out_dir, len(sessions)))

    n_sessions = n_pairs = n_left = n_entered = 0
    for sid in sessions:
        if verbose:
            print("session %s" % sid)
        pairs, left, entered = reduce_session(
            corpus, out_dir, sid, args.preview_chars, include_preview,
            args.skip_existing, verbose)
        if pairs:
            n_sessions += 1
            n_pairs += pairs
            n_left += left
            n_entered += entered

    print("\ndone: %d session(s) reduced, %d delta(s), "
          "%d units left / %d entered  ->  %s"
          % (n_sessions, n_pairs, n_left, n_entered, out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
