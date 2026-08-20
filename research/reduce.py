#!/usr/bin/env python3
"""Lossless reduction ETL for the context-churn corpus.

Turns the large, highly-redundant raw capture corpus into a small, *complete*
per-session archive, by exploiting the one structural fact that makes the
corpus redundant: the transcript file is append-only, so a snapshot is a byte
prefix of every later snapshot in the same epoch.

    Input  (read-only):  <resync_home>/corpus
    Output (derived)  :  <resync_home>/refined

This is **lossless reduction**. It selects no fields, truncates no content,
and parses the transcript only to annotate the index (never to reconstruct).
Every captured byte is recoverable, and `--verify` proves it by round-tripping
each capture against a sha256 taken from the original file. That is the whole
point: any field-selecting projection has to guess which fields a future
question needs, and guessing wrong is unrecoverable once the corpus is gone.
Views that *do* select fields (a change log, a survey) are generated on demand
from this output, where guessing wrong costs a re-run instead of a re-capture.

Measured on the corpus this was written against: 14.268 GB of snapshots across
51 sessions reduce to ~69 MB of epoch bases (0.482%), with the 32.4 MB of
events logs kept verbatim — about 140x, nothing dropped.

How the reduction works
-----------------------

  1. Captures are ordered by `count`, the recorder-minted primary key.
  2. Captures are grouped into *epochs* (below). Within an epoch, one snapshot
     — the "base", the largest — is stored verbatim; every other capture in
     that epoch is recorded as a byte length into it.
  3. Reconstruct capture c as the first `bytes` bytes of its epoch base.
     Byte offsets, not line counts: the measured property is a *byte* prefix,
     so truncating by bytes is exact and needs no parsing, no line-ending
     rules, and no special case for a torn or half-written snapshot.
  4. Everything the byte-prefix rule cannot cover is stored in full.

Epochs, and why compaction needs them
-------------------------------------

The transcript file is append-only, but the *context* is not. At a compaction
the two diverge permanently: in the observed case 42,449 tokens became 9,114,
with 5 messages preserved out of 46 — so 41 lines stayed in the file and left
the context. From that point on, "the first N bytes of the file" faithfully
reproduces the file and misrepresents what the model could see.

Worse, the divergence is not fully derivable from the transcript:
`compactMetadata.preservedMessages.allUuids` listed 5 uuids and only 4 of them
resolve to a line in the file. So the post-compaction context set cannot be
rebuilt from the transcript however carefully it is read.

An epoch is therefore a run of captures uninterrupted by a compaction, and the
boundary is stored rather than modelled:

  * epoch 0 begins at the first capture.
  * A `PreCompact` capture ends its epoch and is that epoch's base — the
    recorder captures PreCompact synchronously precisely so this last
    pre-compaction state exists.
  * Captures from there through the matching `PostCompact` are *in-flight*:
    they belong to no epoch and are stored in full.
  * The next epoch begins after `PostCompact`.
  * An unmatched `PreCompact` leaves the region open, and every later capture
    is stored in full until a `PostCompact` arrives or the session ends. This
    is not hypothetical: one session in the corpus fired two PreCompacts and
    one PostCompact.

The byte-prefix property is asserted *within* an epoch and never across one.
Both compactions in this corpus were `trigger: "manual"`, and recorder.py's
own docstring states that compaction "rewrites the transcript file in place" —
which would break the property outright. Auto-compaction has never been
observed here, so rather than trust either reading, any capture that fails to
byte-prefix its base is stored in full (`source: "full"`). The fallback is
per-capture, so an anomaly costs one file, not a session.

events.jsonl is copied verbatim
-------------------------------

It is not redundant with the transcript — it is the complement, and it holds
things the transcript never records: `background_tasks` (with live status),
`agent_type`/`agent_id` for subagents the transcript has no lines for at all,
`compact_summary`, `custom_instructions`, per-call `duration_ms`,
`is_interrupt`, `model`, `permission_mode`, and the full verbatim
`tool_response`. The recorder already embeds each raw payload "so nothing is
lost to a field we did not think to extract"; this keeps that promise.

Properties: offline batch tool (never a hook); read-only on the corpus;
deterministic / idempotent; every capture traceable to the file it came from
and provable against a hash of that file.

Secrets: the output contains the corpus verbatim, so it is exactly as
secrets-bearing as the corpus. It is written 0700 with a self-contained
`.gitignore` of `*`. There is no redacted mode — a lossless archive cannot
also be a shareable one. Generate a view for anything that needs sharing.

Usage:
    python3 reduce.py                          # reduce every session
    python3 reduce.py --session <sid>          # one session
    python3 reduce.py --skip-existing          # skip sessions already reduced
    python3 reduce.py --verify                 # round-trip every capture
    python3 reduce.py --verify --against-corpus  # also re-hash the originals
    python3 reduce.py --prune-corpus --session <sid>   # delete verified snapshots
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys

SNAP_RE = re.compile(r"^(.*)\.(\d+)\.transcript\.jsonl$")

READ_CHUNK = 1 << 20

# Events that bracket a compaction. PreCompact closes an epoch; PostCompact
# opens the next one. Names match recorder.py's KNOWN_EVENTS.
PRE_COMPACT = "PreCompact"
POST_COMPACT = "PostCompact"


def resync_home():
    """The single global home for everything installed: scripts, rules, corpus.

    Resolution order is deliberate. CLAUDE_RESYNC_HOME wins so a test run can be
    pointed elsewhere. CLAUDE_TRANSCRIPTS_HOME is honoured next because it was
    the variable before the project was renamed and may still be set. Then the
    new default. Finally the pre-rename directory, but only if it actually
    exists — an installation that predates the rename keeps working instead of
    silently starting a second, empty corpus somewhere else."""
    env = os.environ.get("CLAUDE_RESYNC_HOME") or os.environ.get(
        "CLAUDE_TRANSCRIPTS_HOME")
    if env:
        return env
    new = os.path.join(os.path.expanduser("~"), ".claude-resync")
    if os.path.isdir(new):
        return new
    legacy = os.path.join(os.path.expanduser("~"), ".claude-transcripts")
    if os.path.isdir(legacy):
        return legacy
    return new


def transcripts_home():
    """Mirror the recorder's home resolution so both halves agree by default."""
    return resync_home()


def ensure_out(path):
    """Create the refined dir (0700) with a self-contained `.gitignore` of `*`.

    The refined output holds the corpus verbatim, so it is ignored by git
    regardless of surrounding project rules and never accidentally committed."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    gi = os.path.join(path, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as fh:
            fh.write("# Auto-created by the lossless reduction ETL (reduce.py).\n"
                     "# Holds the secrets-bearing corpus verbatim — never commit it.\n"
                     "*\n")


# --------------------------------------------------------------------------
# hashing
# --------------------------------------------------------------------------

def sha256_file(path, length=None):
    """sha256 of a whole file, or of its first `length` bytes.

    Returns None if the file is missing, or if `length` exceeds its size — a
    short file cannot contain the prefix being asked for, and saying so is
    more useful than hashing whatever is there."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if length is None:
        length = size
    elif length > size:
        return None
    h = hashlib.sha256()
    remaining = length
    with open(path, "rb") as fh:
        while remaining > 0:
            block = fh.read(min(READ_CHUNK, remaining))
            if not block:
                return None
            remaining -= len(block)
            h.update(block)
    return h.hexdigest()


def prefix_digests(path, offsets):
    """One-pass sha256 (and newline count) for many prefix lengths of one file.

    sha256 is streaming, so a running hash cloned at each requested offset
    gives every prefix digest for the cost of reading the base once. An epoch
    base is read once no matter how many hundreds of captures point into it.

    Returns {offset: (hexdigest, newline_count)}; offsets past EOF are omitted."""
    wanted = sorted(set(o for o in offsets if o >= 0))
    out = {}
    if not wanted:
        return out
    h = hashlib.sha256()
    pos = 0
    nl = 0
    idx = 0
    try:
        fh = open(path, "rb")
    except OSError:
        return out
    with fh:
        while idx < len(wanted):
            target = wanted[idx]
            if pos == target:
                out[target] = (h.copy().hexdigest(), nl)
                idx += 1
                continue
            block = fh.read(min(READ_CHUNK, target - pos))
            if not block:
                break  # every remaining offset is past EOF
            h.update(block)
            nl += block.count(b"\n")
            pos += len(block)
    return out


def count_lines(path):
    """Non-empty line count, for the index's convenience `lines` field."""
    n = 0
    try:
        with open(path, "rb") as fh:
            tail_nonempty = False
            while True:
                block = fh.read(READ_CHUNK)
                if not block:
                    break
                n += block.count(b"\n")
                tail_nonempty = not block.endswith(b"\n")
    except OSError:
        return None
    return n + (1 if tail_nonempty else 0)


# --------------------------------------------------------------------------
# corpus discovery
# --------------------------------------------------------------------------

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

    Prefer the events log (it carries the event type and the raw payload);
    fall back to snapshot filenames for any counts the events log doesn't
    mention, so a session with a missing/partial events file still reduces.

    Unlike the change-log era, `payload` is carried through: the compaction
    fields the index annotates (`trigger`, `compact_summary`,
    `custom_instructions`) live only there."""
    captures = {}

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
                snap = (rec.get("snapshot")
                        or "%s.%d.transcript.jsonl" % (sid, count))
                captures[count] = {
                    "count": count,
                    "event": rec.get("event"),
                    "hook_event_name": rec.get("hook_event_name"),
                    "captured_at": rec.get("captured_at"),
                    "ts": rec.get("ts"),
                    "prompt_id": rec.get("prompt_id"),
                    "tool_use_id": rec.get("tool_use_id"),
                    "snapshot": snap,
                    "snapshot_bytes": rec.get("snapshot_bytes"),
                    "snapshot_error": rec.get("snapshot_error"),
                    "payload": rec.get("payload") or {},
                }

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
                "count": count, "event": None, "hook_event_name": None,
                "captured_at": None, "ts": None, "prompt_id": None,
                "tool_use_id": None, "snapshot": name,
                "snapshot_bytes": None, "snapshot_error": None, "payload": {},
            }

    # True on-disk size wins over the recorded one: snapshot_bytes is what the
    # recorder saw at capture time, and the file is the thing being hashed.
    for cap in captures.values():
        path = os.path.join(corpus, cap["snapshot"])
        try:
            cap["bytes"] = os.path.getsize(path)
            cap["exists"] = True
        except OSError:
            cap["bytes"] = 0
            cap["exists"] = False

    return [captures[c] for c in sorted(captures)]


# --------------------------------------------------------------------------
# epoch planning
# --------------------------------------------------------------------------

def plan_epochs(captures):
    """Assign `epoch` / `in_flight` to each capture, and pick each epoch's base.

    Returns (captures, bases) where bases maps epoch -> capture dict.

    The walk is deliberately conservative around compaction: once a PreCompact
    is seen, every capture is in-flight (stored in full) until a PostCompact
    closes the region. A second PreCompact arriving while one is already open
    changes nothing — it is already being stored in full, so there is nothing
    to get wrong."""
    epoch = 0
    pending = False
    for cap in captures:
        event = cap.get("event")
        if event == PRE_COMPACT and not pending:
            cap["epoch"] = epoch
            cap["in_flight"] = False
            cap["is_pre_compact_base"] = True
            pending = True
        elif pending:
            cap["epoch"] = None
            cap["in_flight"] = True
            if event == POST_COMPACT:
                pending = False
                epoch += 1
        else:
            cap["epoch"] = epoch
            cap["in_flight"] = False

    # Base = the largest readable snapshot in the epoch. Under append-only that
    # is the last one, but choosing by size is robust to a failed or truncated
    # capture landing last, and a base must by definition contain every prefix
    # pointing into it.
    bases = {}
    for cap in captures:
        e = cap.get("epoch")
        if e is None or not cap["exists"] or cap["bytes"] <= 0:
            continue
        cur = bases.get(e)
        if cur is None or cap["bytes"] > cur["bytes"]:
            bases[e] = cap
    return captures, bases


def scan_compaction(path):
    """Annotate compaction records found in a transcript. Best-effort.

    This is the only place the transcript is parsed, and it is *annotation*:
    nothing here participates in reconstruction, so a parse failure costs a
    few index fields and never the archive. Records the boundary's own token
    accounting plus how many preserved uuids actually resolve to a line —
    4 of 5 did in the observed case, which is why the epoch boundary is
    stored rather than recomputed from this metadata."""
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return out
    present = set()
    parsed = []
    for i, raw in enumerate(lines):
        raw = raw.strip()
        if not raw:
            parsed.append(None)
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            parsed.append(None)
            continue
        parsed.append(obj)
        if isinstance(obj, dict) and obj.get("uuid"):
            present.add(obj["uuid"])
    for i, obj in enumerate(parsed):
        if not isinstance(obj, dict):
            continue
        cm = obj.get("compactMetadata")
        if not isinstance(cm, dict) or not cm:
            continue
        pm = cm.get("preservedMessages") or {}
        uuids = pm.get("allUuids") if isinstance(pm, dict) else None
        uuids = uuids if isinstance(uuids, list) else []
        rec = {
            "line_index": i,
            "type": obj.get("type"),
            "subtype": obj.get("subtype"),
            "pre_tokens": cm.get("preTokens"),
            "post_tokens": cm.get("postTokens"),
            "dropped_tokens": cm.get("cumulativeDroppedTokens"),
            "duration_ms": cm.get("durationMs"),
            "preserved_uuids": uuids,
            "preserved_resolved": sum(1 for u in uuids if u in present),
            "preserved_unresolved": sum(1 for u in uuids if u not in present),
        }
        out.append(rec)
    return out


# --------------------------------------------------------------------------
# reduction
# --------------------------------------------------------------------------

def _copy_verbatim(src, dst):
    tmp = dst + ".tmp"
    shutil.copyfile(src, tmp)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, dst)


def _write_jsonl(path, records):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def epoch_path(out_dir, sid, epoch):
    return os.path.join(out_dir, "%s.epoch%d.jsonl" % (sid, epoch))


def full_path(out_dir, sid, count, kind):
    return os.path.join(out_dir, "%s.%s.%d.jsonl" % (sid, kind, count))


def index_path(out_dir, sid):
    return os.path.join(out_dir, "%s.index.jsonl" % sid)


def events_path(out_dir, sid):
    return os.path.join(out_dir, "%s.events.jsonl" % sid)


def reduce_session(corpus, out_dir, sid, skip_existing, verbose):
    """Reduce one session. Returns a stats dict."""
    ipath = index_path(out_dir, sid)
    if skip_existing and os.path.exists(ipath):
        if verbose:
            print("  skip (exists): %s" % os.path.basename(ipath))
        return {"skipped": True}

    captures = load_captures(corpus, sid)
    if not captures:
        if verbose:
            print("  skip (no captures): %s" % sid)
        return {"skipped": True}

    captures, bases = plan_epochs(captures)

    # Stage 1: copy each epoch base verbatim, and gather the prefix digests
    # every capture in that epoch needs — one read of the base per epoch.
    base_digests = {}
    for epoch, base in sorted(bases.items()):
        src = os.path.join(corpus, base["snapshot"])
        _copy_verbatim(src, epoch_path(out_dir, sid, epoch))
        offsets = [c["bytes"] for c in captures
                   if c.get("epoch") == epoch and c["exists"]]
        base_digests[epoch] = prefix_digests(src, offsets)

    # Stage 2: one index record per capture, plus a full copy for anything the
    # byte-prefix rule cannot cover.
    records = []
    stats = {"captures": 0, "epoch": 0, "inflight": 0, "full": 0,
             "empty": 0, "missing": 0, "epochs": len(bases),
             "base_bytes": 0, "full_bytes": 0, "orig_bytes": 0}

    for cap in captures:
        stats["captures"] += 1
        stats["orig_bytes"] += cap["bytes"]
        src = os.path.join(corpus, cap["snapshot"])
        epoch = cap.get("epoch")

        rec = {
            "session_id": sid,
            "count": cap["count"],
            "event": cap["event"],
            "captured_at": cap["captured_at"],
            "bytes": cap["bytes"],
            "epoch": epoch,
            "in_flight": bool(cap.get("in_flight")),
        }
        for key in ("hook_event_name", "ts", "prompt_id", "tool_use_id"):
            if cap.get(key):
                rec[key] = cap[key]
        rec["snapshot"] = cap["snapshot"]
        if cap.get("snapshot_error"):
            rec["snapshot_error"] = cap["snapshot_error"]
        if cap.get("is_pre_compact_base"):
            rec["is_pre_compact_base"] = True

        # Compaction payload facts, verbatim from the events record. Recorded
        # here because the transcript does not carry them: at the moment
        # PostCompact fires the summary exists only in the payload, and the
        # transcript's own compact_boundary line does not appear for another
        # two captures.
        payload = cap.get("payload") or {}
        if cap["event"] in (PRE_COMPACT, POST_COMPACT):
            if payload.get("trigger"):
                rec["compact_trigger"] = payload["trigger"]
            ci = payload.get("custom_instructions")
            rec["has_custom_instructions"] = bool(ci)
            cs = payload.get("compact_summary")
            if cs:
                rec["compact_summary_chars"] = len(cs)

        if not cap["exists"]:
            rec["source"] = "missing"
            stats["missing"] += 1
            records.append(rec)
            continue
        if cap["bytes"] == 0:
            rec["source"] = "empty"
            rec["sha256"] = hashlib.sha256(b"").hexdigest()
            rec["lines"] = 0
            stats["empty"] += 1
            records.append(rec)
            continue

        # sha256 of the ORIGINAL file. This is the round-trip target: --verify
        # recomputes from the refined output and compares, so a match proves
        # the reduction reproduces the corpus byte for byte.
        rec["sha256"] = sha256_file(src)

        if cap.get("in_flight"):
            dst = full_path(out_dir, sid, cap["count"], "inflight")
            _copy_verbatim(src, dst)
            rec["source"] = "inflight"
            rec["file"] = os.path.basename(dst)
            rec["lines"] = count_lines(src)
            stats["inflight"] += 1
            stats["full_bytes"] += cap["bytes"]
            records.append(rec)
            continue

        got = base_digests.get(epoch, {}).get(cap["bytes"])
        if got and got[0] == rec["sha256"]:
            rec["source"] = "epoch"
            # Newline count within the prefix. Equals the line count for a file
            # ending in a newline, which every recorder snapshot does; it is a
            # convenience field either way, never used to reconstruct.
            rec["lines"] = got[1]
            stats["epoch"] += 1
        else:
            # The byte-prefix property did not hold for this capture. Keep the
            # snapshot whole rather than lose it, and record why.
            dst = full_path(out_dir, sid, cap["count"], "full")
            _copy_verbatim(src, dst)
            rec["source"] = "full"
            rec["file"] = os.path.basename(dst)
            rec["lines"] = count_lines(src)
            rec["prefix_mismatch"] = True
            stats["full"] += 1
            stats["full_bytes"] += cap["bytes"]
        records.append(rec)

    # Stage 3: compaction annotations, as their own trailing record.
    # They describe the session rather than any one capture, so they are not
    # smuggled onto an unrelated capture's record. Readers that only want
    # captures filter on the absence of `record`.
    if bases:
        last_epoch = max(bases)
        found = scan_compaction(epoch_path(out_dir, sid, last_epoch))
        if found:
            records.append({
                "record": "compaction_annotations",
                "session_id": sid,
                "from_epoch_base": last_epoch,
                "boundaries": found,
            })

    for epoch in bases:
        try:
            stats["base_bytes"] += os.path.getsize(
                epoch_path(out_dir, sid, epoch))
        except OSError:
            pass

    _write_jsonl(ipath, records)

    # Stage 4: events.jsonl verbatim — the complement to the transcript, and
    # the only home of background_tasks, agent_type, compact_summary and the
    # raw tool_response.
    src_events = os.path.join(corpus, "%s.events.jsonl" % sid)
    if os.path.exists(src_events):
        _copy_verbatim(src_events, events_path(out_dir, sid))
        stats["events_bytes"] = os.path.getsize(src_events)

    if verbose:
        kept = stats["base_bytes"] + stats["full_bytes"]
        ratio = (stats["orig_bytes"] / kept) if kept else 0
        print("  %s: %d capture(s), %d epoch(s) — %d epoch / %d inflight / "
              "%d full / %d empty / %d missing"
              % (sid[:8], stats["captures"], stats["epochs"], stats["epoch"],
                 stats["inflight"], stats["full"], stats["empty"],
                 stats["missing"]))
        print("            %.1f MB -> %.1f MB  (%.0fx)"
              % (stats["orig_bytes"] / 1e6, kept / 1e6, ratio))
    return stats


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def verify_session(corpus, out_dir, sid, against_corpus, verbose):
    """Round-trip every capture in a session.

    Returns (ok, checked, failures, stale).

    Reconstruction reads only the refined output, so a pass also proves the
    archive is self-sufficient. `against_corpus` additionally re-hashes the
    original files while they still exist, which is what makes a subsequent
    --prune-corpus safe rather than merely plausible.

    `stale` is kept separate from `failures` because the two mean opposite
    things. A live session grows while it is being reduced: new captures land
    and events.jsonl gains lines, so the archive is *behind* the corpus rather
    than wrong about it. That must not read as corruption — but it must still
    block pruning, since the un-reduced tail would be deleted with the rest."""
    ipath = index_path(out_dir, sid)
    if not os.path.exists(ipath):
        return (False, 0, ["no index: %s" % os.path.basename(ipath)], [])

    stale = []
    records = []
    with open(ipath, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception as exc:
                    return (False, 0, ["unparseable index line: %s" % exc], [])

    # Group prefix checks by epoch so each base is read once.
    by_epoch = {}
    failures = []
    checked = 0

    for rec in records:
        if rec.get("record"):
            continue  # annotation record, not a capture
        src = rec.get("source")
        if src == "epoch":
            by_epoch.setdefault(rec["epoch"], []).append(rec)
        elif src in ("inflight", "full"):
            path = os.path.join(out_dir, rec["file"])
            got = sha256_file(path)
            checked += 1
            if got != rec.get("sha256"):
                failures.append("count=%s %s hash mismatch" % (rec["count"], src))
        elif src == "empty":
            checked += 1
            if rec.get("sha256") != hashlib.sha256(b"").hexdigest():
                failures.append("count=%s empty hash mismatch" % rec["count"])
        elif src == "missing":
            pass  # nothing was captured; nothing to verify
        else:
            failures.append("count=%s unknown source %r" % (rec["count"], src))

    for epoch, recs in sorted(by_epoch.items()):
        base = epoch_path(out_dir, sid, epoch)
        digests = prefix_digests(base, [r["bytes"] for r in recs])
        for rec in recs:
            checked += 1
            got = digests.get(rec["bytes"])
            if got is None:
                failures.append("count=%s offset %d past end of epoch%d"
                                % (rec["count"], rec["bytes"], epoch))
            elif got[0] != rec.get("sha256"):
                failures.append("count=%s epoch%d prefix hash mismatch"
                                % (rec["count"], epoch))

    # events copy. events.jsonl is itself append-only, so a copy that is a
    # byte prefix of the corpus file means the session has advanced since the
    # reduction — stale, not damaged. Only a copy that diverges mid-file is a
    # real failure.
    src_events = os.path.join(corpus, "%s.events.jsonl" % sid)
    dst_events = events_path(out_dir, sid)
    if os.path.exists(dst_events):
        checked += 1
        if against_corpus and os.path.exists(src_events):
            copied = sha256_file(dst_events)
            if copied != sha256_file(src_events):
                size = os.path.getsize(dst_events)
                if sha256_file(src_events, size) == copied:
                    stale.append("events.jsonl extended since reduction "
                                 "(+%d bytes)"
                                 % (os.path.getsize(src_events) - size))
                else:
                    failures.append("events.jsonl diverges from corpus")
    elif os.path.exists(src_events):
        failures.append("events.jsonl not copied")

    # Captures the corpus has and the index does not — the same liveness
    # signal seen from the snapshot side.
    indexed = set(r["count"] for r in records if not r.get("record"))
    on_disk = set()
    try:
        for name in os.listdir(corpus):
            m = SNAP_RE.match(name)
            if m and m.group(1) == sid:
                on_disk.add(int(m.group(2)))
    except OSError:
        pass
    extra = sorted(on_disk - indexed)
    if extra:
        stale.append("%d capture(s) not in the index (counts %s%s)"
                     % (len(extra), ", ".join(str(c) for c in extra[:5]),
                        ", …" if len(extra) > 5 else ""))

    if against_corpus:
        for rec in records:
            if rec.get("record") or rec.get("source") == "missing":
                continue
            orig = os.path.join(corpus, rec["snapshot"])
            if not os.path.exists(orig):
                continue
            if sha256_file(orig) != rec.get("sha256"):
                failures.append("count=%s recorded hash differs from corpus file"
                                % rec["count"])

    ok = not failures
    if verbose:
        mark = "FAIL" if failures else ("live" if stale else "ok  ")
        print("  %s %s: %d check(s)%s"
              % (mark, sid[:8], checked,
                 ", %d failure(s)" % len(failures) if failures else ""))
        for f in failures[:5]:
            print("        %s" % f)
        if len(failures) > 5:
            print("        ... %d more" % (len(failures) - 5))
        for s in stale:
            print("        stale: %s" % s)
    return (ok, checked, failures, stale)


def prune_session(corpus, out_dir, sid, verbose):
    """Delete a session's corpus snapshots — only after a full verify passes.

    Verification is re-run here, against the corpus, immediately before
    deleting. Never touches events.jsonl, .count or .lock: the count file is
    the recorder's monotonic sequence and deleting it could rewind a live
    session's numbering."""
    ok, checked, failures, stale = verify_session(
        corpus, out_dir, sid, against_corpus=True, verbose=False)
    if not ok:
        print("  refuse %s: verification failed (%d failure(s)); nothing deleted"
              % (sid[:8], len(failures)))
        for f in failures[:5]:
            print("        %s" % f)
        return (0, 0)
    if stale:
        # The archive is correct but behind. Pruning now would delete captures
        # that were never reduced, so refuse and say what to do about it.
        print("  refuse %s: archive is behind the corpus; re-reduce first"
              % sid[:8])
        for s in stale:
            print("        %s" % s)
        return (0, 0)

    freed = 0
    removed = 0
    try:
        names = os.listdir(corpus)
    except OSError:
        return (0, 0)
    for name in names:
        m = SNAP_RE.match(name)
        if not m or m.group(1) != sid:
            continue
        path = os.path.join(corpus, name)
        try:
            size = os.path.getsize(path)
            os.remove(path)
            freed += size
            removed += 1
        except OSError as exc:
            print("  warn %s: could not remove %s: %s" % (sid[:8], name, exc))
    if verbose:
        print("  pruned %s: %d snapshot(s), %.1f MB freed (%d check(s) passed)"
              % (sid[:8], removed, freed / 1e6, checked))
    return (removed, freed)


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Losslessly reduce the raw capture corpus by collapsing "
                    "the append-only redundancy between snapshots. Selects no "
                    "fields and truncates nothing; every captured byte is "
                    "recoverable and --verify proves it.")
    ap.add_argument("--corpus", default=None,
                    help="raw corpus dir (default: <resync_home>/corpus, "
                         "read-only)")
    ap.add_argument("--out", default=None,
                    help="refined output dir (default: <resync_home>/refined)")
    ap.add_argument("--session", default=None,
                    help="operate on this session id only (default: all)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="skip sessions that already have an index")
    ap.add_argument("--verify", action="store_true",
                    help="round-trip every capture against its recorded hash "
                         "instead of reducing")
    ap.add_argument("--against-corpus", action="store_true",
                    help="with --verify, also re-hash the original corpus files")
    ap.add_argument("--prune-corpus", action="store_true",
                    help="delete corpus snapshots for sessions that verify "
                         "against the corpus (never events/count/lock)")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="only print the final summary")
    args = ap.parse_args(argv)

    corpus = args.corpus or os.path.join(transcripts_home(), "corpus")
    out_dir = args.out or os.path.join(transcripts_home(), "refined")
    verbose = not args.quiet

    if not os.path.isdir(corpus):
        print("error: corpus not found: %s" % corpus, file=sys.stderr)
        return 1
    if os.path.abspath(corpus) == os.path.abspath(out_dir):
        print("error: --out must differ from --corpus (never write into the "
              "raw corpus)", file=sys.stderr)
        return 1
    if args.prune_corpus and not args.session:
        print("error: --prune-corpus requires --session; pruning every session "
              "in one command is not offered", file=sys.stderr)
        return 1

    ensure_out(out_dir)

    sessions = [args.session] if args.session else discover_sessions(corpus)
    if not sessions:
        print("no sessions found in %s" % corpus)
        return 0

    if verbose:
        mode = ("verify" if args.verify
                else "prune" if args.prune_corpus else "reduce")
        print("corpus: %s\nrefined: %s\nsessions: %d\nmode: %s\n"
              % (corpus, out_dir, len(sessions), mode))

    if args.verify:
        n_ok = n_fail = n_checks = n_stale = 0
        for sid in sessions:
            ok, checked, _, stale = verify_session(
                corpus, out_dir, sid, args.against_corpus, verbose)
            n_checks += checked
            n_fail += 0 if ok else 1
            if ok and stale:
                n_stale += 1
            elif ok:
                n_ok += 1
        print("\nverify: %d session(s) ok, %d live/behind, %d failed, "
              "%d check(s)" % (n_ok, n_stale, n_fail, n_checks))
        if n_stale:
            print("  live sessions verified correct but are behind the corpus; "
                  "re-reduce them when the session ends")
        return 1 if n_fail else 0

    if args.prune_corpus:
        removed = freed = 0
        for sid in sessions:
            r, f = prune_session(corpus, out_dir, sid, verbose)
            removed += r
            freed += f
        print("\npruned: %d snapshot(s), %.1f MB freed" % (removed, freed / 1e6))
        return 0

    agg = {"sessions": 0, "captures": 0, "epochs": 0, "epoch": 0,
           "inflight": 0, "full": 0, "empty": 0, "missing": 0,
           "orig_bytes": 0, "base_bytes": 0, "full_bytes": 0,
           "events_bytes": 0}
    for sid in sessions:
        if verbose:
            print("session %s" % sid)
        st = reduce_session(corpus, out_dir, sid, args.skip_existing, verbose)
        if st.get("skipped"):
            continue
        agg["sessions"] += 1
        for k in ("captures", "epochs", "epoch", "inflight", "full", "empty",
                  "missing", "orig_bytes", "base_bytes", "full_bytes",
                  "events_bytes"):
            agg[k] += st.get(k, 0)

    kept = agg["base_bytes"] + agg["full_bytes"] + agg["events_bytes"]
    ratio = (agg["orig_bytes"] / kept) if kept else 0
    print("\ndone: %d session(s), %d capture(s), %d epoch(s)"
          % (agg["sessions"], agg["captures"], agg["epochs"]))
    print("  sources: %d epoch / %d inflight / %d full / %d empty / %d missing"
          % (agg["epoch"], agg["inflight"], agg["full"], agg["empty"],
             agg["missing"]))
    print("  %.3f GB of snapshots -> %.1f MB bases + %.1f MB full + "
          "%.1f MB events = %.1f MB  (%.0fx, lossless)"
          % (agg["orig_bytes"] / 1e9, agg["base_bytes"] / 1e6,
             agg["full_bytes"] / 1e6, agg["events_bytes"] / 1e6,
             kept / 1e6, ratio))
    print("  next: python3 reduce.py --verify --against-corpus")
    return 0


if __name__ == "__main__":
    sys.exit(main())
