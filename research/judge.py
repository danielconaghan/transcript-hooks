#!/usr/bin/env python3
"""Put a model's verdict on a stored session. The only script here that spends money.

    Input  (read-only):  <home>/analysis/views/session/<session>.jsonl
    Output            :  <home>/analysis/verdicts/session/<session>.jsonl

    corpus  -> refined     reduce.py    lossless, expensive to redo
    refined -> document    views.py     deterministic, free to redo
    document -> verdict    THIS         costs money, stored so it is bought once

It reads the document **from disk**, never by regenerating it. The bytes judged
are the bytes on the file, so `--audit` checks a quote against exactly what was
sent. A runner that rebuilds its own payload can drift from the file it wrote,
and then the stored document is decoration rather than evidence.

`views.py` cannot spend money — it imports no API client. `prompts.py` holds
the questions and no code that could ask them. This holds the client and no
opinion about what to ask.

Two passes, because sixteen will not compile
--------------------------------------------

Pass one forces a verdict on all sixteen categories at once. Pass two buys
findings for the ones it flagged, four at a time — the measured ceiling above
which the API rejects the schema outright. See `prompts.py`.

The document is the bulk of both calls and is sent identically, so it goes as a
cached block and pass two pays a tenth for it. The system prompt is the same
bytes in both passes for the same reason: caching matches on a prefix of the
whole request, so a pass-specific system prompt would miss on the very call the
cache exists for.

Not paying twice for the same answer
------------------------------------

Cached on `(unit_key, stage, model, prompt_version)`, where `unit_key` is
stamped into the document at build time and never recomputed. Triage and each
detail batch are stored as they land, so a crash partway through pass two does
not throw away the pass-one verdict that was already bought.

The **model is part of the index**. Without it, running a second model silently
returns the first one's verdicts — which is exactly the comparison you would be
running it to make.

`prompt_version` is a string bumped by hand, not a hash of the text, so fixing
a typo costs nothing. The text of each version is stored beside the verdicts,
and a changed prompt under an unchanged version aborts the run rather than
quietly serving the old wording from cache.

Estimating cost
---------------

`chars/4` is a prose heuristic and understated a real 47,049-token call by
1.9x: these payloads are JSON dense with timestamps, identifiers and code, and
run at about **2.1 chars per token**. `--count-tokens` asks the API instead —
free, but it is a call and needs credentials. Output is extra and is not small
here: pass one writes sixteen `searched_how` sentences whatever it finds.

Usage:
    python3 judge.py --list                          # sessions, cost, progress
    python3 judge.py --session fecca80a --dry-run    # exact payload, unsent
    python3 judge.py --session fecca80a              # judge one session
    python3 judge.py --all --model claude-opus-5     # the pass you keep
    python3 judge.py --show [--category A1] [--unnoticed] [--severity cost]
    python3 judge.py --origins                       # findings sharing a cause
    python3 judge.py --audit                         # quotes verbatim?
    python3 judge.py --verify                        # integrity and spend
"""

import argparse
import collections
import concurrent.futures
import glob
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "install"))

import session as S                          # noqa: E402
import views as V                            # noqa: E402
import prompts as P                          # noqa: E402
from plaintext import strip_decoration       # noqa: E402

VERDICTS = os.path.join(S.home(), "analysis", "verdicts")
VIEW = V.VIEW

TUNING_MODEL = "claude-haiku-4-5"
KEEPER_MODEL = "claude-opus-5"
# `effort` is rejected on these; everything else takes it.
NO_EFFORT_MODELS = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-haiku-3")

# $/1M for input, output, cache write, cache read. A cache write costs 1.25x
# input and a read 0.1x. Stored rather than remembered, so a `usage` row
# converts to money years from now without anyone looking up a rate.
PRICE = {"claude-haiku-4-5": (1.00, 5.00, 1.25, 0.10),
         "claude-opus-5": (5.00, 25.00, 6.25, 0.50),
         "claude-sonnet-5": (3.00, 15.00, 3.75, 0.30)}

CHARS_PER_TOKEN = 2.1


# --------------------------------------------------------------------------
# storing the verdict
# --------------------------------------------------------------------------

def ensure():
    d = os.path.join(VERDICTS, VIEW)
    os.makedirs(d, mode=0o700, exist_ok=True)
    gi = os.path.join(VERDICTS, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as fh:
            fh.write("# Auto-created by judge.py. Quotes developer and "
                     "assistant text verbatim — never commit.\n*\n")
    return d


def verdict_path(sid):
    return os.path.join(VERDICTS, VIEW, "%s.jsonl" % sid)


def load_rows(sid, model=None, version=None):
    """{(unit_key, stage): row} — the cache. Newest wins."""
    p = verdict_path(sid)
    if not os.path.exists(p):
        return {}
    got = {}
    with open(p, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("kind") != "verdict":
                continue
            if version and d.get("prompt_version") != version:
                continue
            if model and d.get("model") != model:
                continue
            got[(d.get("unit_key"), d.get("stage"))] = d
    return got


def append(sid, rows):
    if not rows:
        return
    ensure()
    p = verdict_path(sid)
    with open(p, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    os.chmod(p, 0o600)


def save_prompt():
    """Store each prompt version once; abort if it changed without a bump."""
    d = os.path.join(VERDICTS, VIEW, "prompts")
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, "%s.txt" % P.PROMPT_VERSION)
    text = (P.SYSTEM + "\n\n=== triage ===\n" + P.TRIAGE_TASK
            if not P.PROMPT_VERSION.endswith("-split") else
            "\n\n".join("=== %s ===\n%s\n%s" % (g, P.system_for(c),
                                                P.triage_task_for(c))
                        for g, c in sorted(P.GROUPS.items())))
    if os.path.exists(path):
        with open(path, errors="replace") as fh:
            if fh.read() != text:
                return ("MISMATCH: %s differs from the current prompt. Bump "
                        "PROMPT_VERSION in prompts.py, or the cache will serve "
                        "verdicts produced by the old wording." % path)
        return None
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)
    return None


def usage_of(u):
    return {"input_tokens": getattr(u, "input_tokens", 0) or 0,
            "output_tokens": getattr(u, "output_tokens", 0) or 0,
            "cache_write_tokens":
                getattr(u, "cache_creation_input_tokens", 0) or 0,
            "cache_read_tokens":
                getattr(u, "cache_read_input_tokens", 0) or 0}


def cost_of(model, usage):
    rate = PRICE.get(model)
    if not usage or not rate:
        return None
    return round(usage.get("input_tokens", 0) / 1e6 * rate[0]
                 + usage.get("output_tokens", 0) / 1e6 * rate[1]
                 + usage.get("cache_write_tokens", 0) / 1e6 * rate[2]
                 + usage.get("cache_read_tokens", 0) / 1e6 * rate[3], 4)


# --------------------------------------------------------------------------
# the call
# --------------------------------------------------------------------------

def client_or_die():
    sys.path.insert(0, os.path.join(S.home(), "collection", "hooks"))
    try:
        import intercept as I               # reuse its venv + .env resolution
    except Exception as exc:
        print("cannot load intercept.py for SDK/env resolution: %s" % exc)
        return None
    anthropic = I.import_anthropic()
    if anthropic is None:
        print("anthropic SDK not importable — %s/.venv" % S.home())
        return None
    I.load_env_file()
    return anthropic.Anthropic()


def payload(head, recs):
    """The document, exactly as stored, as one cacheable block.

    Compact rather than indented: indent=1 cost 6% of every call — about
    389,000 tokens across all sessions — and bought nothing, since the text a judge
    quotes from lives inside the string values either way."""
    return json.dumps({"session": head.get("session"),
                       "index": head.get("index"),
                       "records": recs},
                      ensure_ascii=False, separators=(",", ":"), default=str)


def ask(client, model, doc, task, schema, system=None):
    fmt = {"format": {"type": "json_schema", "schema": schema}}
    if not model.startswith(NO_EFFORT_MODELS):
        fmt["effort"] = "medium"
    resp = client.messages.create(
        model=model, max_tokens=16000, system=system or P.SYSTEM,
        messages=[{"role": "user", "content": [
            # Cached: identical across both passes and the bulk of each call.
            {"type": "text", "text": doc,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": task}]}],
        output_config=fmt)
    if resp.stop_reason == "refusal":
        raise RuntimeError("refused")
    blob = next(b.text for b in resp.content
                if getattr(b, "type", None) == "text")
    return json.loads(blob), usage_of(resp.usage)


def judge_session(client, model, sid, head, recs, psha, want_stages,
                  split=False):
    """Triage, then detail for whatever it flagged. Returns rows as they land.

    Rows are returned per stage rather than as one verdict so that a failure
    in pass two does not discard the pass-one verdict already paid for."""
    doc = payload(head, recs)
    k = head.get("unit_key")
    now = lambda: datetime.now(timezone.utc).isoformat()
    base = {"kind": "verdict", "session": sid, "view": VIEW, "unit_key": k,
            "model": model, "prompt_version": P.PROMPT_VERSION,
            "prompt_sha": psha}
    rows = []

    # Detail stages cannot be named until triage has said what is flagged, so
    # `stages_for` returns only "triage" for a session judged from scratch.
    # Filtering detail against that set skipped pass two entirely on every
    # first run — the stages it would have matched did not exist yet. When
    # triage runs HERE, everything it flags is new by definition and runs.
    fresh = "triage" in want_stages
    triage = None
    if fresh:
        if split:
            # One call per group, each carrying ONLY that group's definitions.
            # The unit is still the whole session; only the question narrows.
            triage = {}
            for g, cats in sorted(P.GROUPS.items()):
                data, u = ask(client, model, doc, P.triage_task_for(cats),
                              P.triage_schema_for(cats), P.system_for(cats))
                triage.update(data)
                rows.append(dict(base, stage="triage:" + g, ts=now(),
                                 categories=data,
                                 present=sorted(c for c in cats
                                                if (data.get(c) or {})
                                                .get("present")),
                                 insufficient=sorted(
                                     c for c in cats
                                     if not (data.get(c) or {})
                                     .get("sufficient_evidence", True)),
                                 usage=u, cost_usd=cost_of(model, u)))
        else:
            data, u = ask(client, model, doc, P.TRIAGE_TASK, P.TRIAGE_SCHEMA)
            triage = data
            rows.append(dict(base, stage="triage", ts=now(), categories=data,
                             present=sorted(c for c in P.CATS
                                            if (data.get(c) or {})
                                            .get("present")),
                             insufficient=sorted(
                                 c for c in P.CATS
                                 if not (data.get(c) or {})
                                 .get("sufficient_evidence", True)),
                             usage=u, cost_usd=cost_of(model, u)))
    else:
        prior = load_rows(sid, model, P.PROMPT_VERSION).get((k, "triage"))
        triage = (prior or {}).get("categories") or {}

    flagged = sorted(c for c in P.CATS if (triage.get(c) or {}).get("present"))
    for batch in P.batches(flagged):
        stage = "detail:" + "+".join(batch)
        if not fresh and stage not in want_stages:
            continue
        data, u = ask(client, model, doc, P.detail_task(batch),
                      P.detail_schema(batch))
        rows.append(dict(base, stage=stage, ts=now(), categories=data,
                         findings=sum(len((data.get(c) or {}).get("findings")
                                          or []) for c in batch),
                         usage=u, cost_usd=cost_of(model, u)))
    return rows


def stages_for(sid, model, head, done, split=False):
    """Which calls this session still owes, given what is already stored."""
    k = head.get("unit_key")
    want = []
    tri = [d for (kk, st), d in done.items()
           if kk == k and (st == "triage" or str(st).startswith("triage:"))]
    need = len(P.GROUPS) if split else 1
    if len(tri) < need:
        want.append("triage")
        return want, True          # detail batches are unknown until triage runs
    triage = {}
    for d in tri:
        triage.update(d.get("categories") or {})
    flagged = sorted(c for c in P.CATS if (triage.get(c) or {}).get("present"))
    for batch in P.batches(flagged):
        stage = "detail:" + "+".join(batch)
        if (k, stage) not in done:
            want.append(stage)
    return want, False


# --------------------------------------------------------------------------
# reading verdicts back
# --------------------------------------------------------------------------

def norm(s):
    return " ".join((s or "").split()).lower()


def stored(sids=None, model=None):
    """[(session, triage, {cat: [findings]})] — newest row per stage wins."""
    out = []
    for sid in (sids or sorted(os.path.basename(p)[:-6] for p in
                               glob.glob(verdict_path("*")))):
        rows = load_rows(sid, model)
        triage, detail = {}, collections.defaultdict(list)
        for (_, stage), d in sorted(rows.items(), key=lambda kv: str(kv[0][1])):
            if stage == "triage" or str(stage).startswith("triage:"):
                triage.update(d.get("categories") or {})
            elif str(stage).startswith("detail:"):
                for cat, v in (d.get("categories") or {}).items():
                    detail[cat] = list(v.get("findings") or [])
        if triage or detail:
            out.append((sid, triage, dict(detail)))
    return out


def findings(sids=None, model=None, category=None, severity=None,
             unnoticed=False):
    hits = []
    for sid, _, detail in stored(sids, model):
        for cat, fs in detail.items():
            if category and cat != category:
                continue
            for f in fs:
                if severity and f.get("severity") != severity:
                    continue
                if unnoticed and f.get("noticed"):
                    continue
                hits.append((sid, cat, f))
    return hits


def do_show(sids, category, severity, unnoticed):
    hits = findings(sids, None, category, severity, unnoticed)
    if not hits:
        print("no findings stored%s" % (" with that filter"
                                        if (category or severity or unnoticed)
                                        else ""))
        return 0
    order = {"cost": 0, "friction": 1, "none": 2}
    hits.sort(key=lambda h: (order.get(h[2].get("severity"), 3), h[1]))
    by_cat = collections.Counter(c for _, c, _ in hits)
    by_sev = collections.Counter(f.get("severity") for _, _, f in hits)
    unn = sum(1 for _, _, f in hits if not f.get("noticed"))
    print("%d finding(s) across %d session(s)"
          % (len(hits), len({s for s, _, _ in hits})))
    print("by category: %s" % dict(by_cat.most_common()))
    print("by severity: %s   unnoticed: %d\n" % (dict(by_sev), unn))
    for sid, cat, f in hits:
        print("%-9s %-3s %-9s seq %-5s began %-6s %s"
              % (sid[:8], cat, f.get("severity"), f.get("at_seq"),
                 f.get("began_seq"), "" if f.get("noticed") else "[unnoticed]"))
        print("     what : %s" % norm(f.get("what_happened"))[:96])
        print("     quote: %s" % norm(f.get("quote"))[:96])
        print()
    return 0


def do_origins(sids):
    """Findings in different categories that trace to the same `began_seq`.

    The reason the unit is the session. Five findings across four categories
    sharing an origin are one event, and the origin is the only thing a
    preventative action can fire on — under a per-turn unit they land in
    separate files and nothing can see they are the same thing."""
    by_origin = collections.defaultdict(list)
    for sid, cat, f in findings(sids):
        if f.get("began_seq") is None:
            continue
        by_origin[(sid, f["began_seq"])].append((cat, f))
    clusters = sorted(((k, v) for k, v in by_origin.items()
                       if len({c for c, _ in v}) > 1),
                      key=lambda kv: -len(kv[1]))
    if not clusters:
        print("no origin shared by more than one category yet.")
        return 0
    print("%d shared origin(s) — one event, several categories\n"
          % len(clusters))
    for (sid, seq), items in clusters:
        cats = sorted({c for c, _ in items})
        worst = min((f.get("severity") for _, f in items),
                    key=lambda s: {"cost": 0, "friction": 1}.get(s, 2))
        print("%s  began at seq %-5s  %-8s  %s"
              % (sid[:8], seq, worst, " ".join(cats)))
        # Sort on the category and the sequence, never on the tuple: two
        # findings in the SAME category at one origin made Python fall through
        # to comparing the finding dicts, which raises.
        for cat, f in sorted(items, key=lambda x: (x[0], x[1].get("at_seq") or 0)):
            print("     %-3s visible at seq %-5s %s"
                  % (cat, f.get("at_seq"), norm(f.get("what_happened"))[:78]))
        print()
    return 0


def do_coverage(sids):
    """What did triage actually look at, and where did it decline to answer?"""
    rows = stored(sids)
    if not rows:
        print("nothing judged yet.")
        return 0
    looked = collections.Counter()
    present = collections.Counter()
    insufficient = collections.Counter()
    for _, triage, _ in rows:
        for cat, v in triage.items():
            looked[cat] += 1
            if v.get("present"):
                present[cat] += 1
            if not v.get("sufficient_evidence", True):
                insufficient[cat] += 1
    nf = collections.Counter(c for _, c, _ in findings(sids))
    print("%d session(s) judged\n" % len(rows))
    print("%-4s %-8s %-9s %-13s %-9s  %s"
          % ("cat", "judged", "present", "insufficient", "findings", "what"))
    print("-" * 92)
    for cat in sorted(P.CATS):
        print("%-4s %-8d %-9d %-13d %-9d  %s"
              % (cat, looked[cat], present[cat], insufficient[cat], nf[cat],
                 P.CATS[cat][:34]))
    dead = [c for c in sorted(P.CATS) if looked[c] and not present[c]]
    if dead:
        print("\nnever found in any session judged so far: %s" % " ".join(dead))
        print("That is either a clean set of sessions or a blind instrument, "
              "and this\ntable cannot tell you which. Read the "
              "`searched_how` lines before concluding.")
    return 0


def do_audit(sids):
    """Is every quote actually in the session it came from?

    Both sides are stripped before comparing. The model re-decorates its own
    quotes with markdown it was never shown — measured twice — and a raw
    compare fails sound findings for the pipeline's own formatting. An elided
    quote (real fragments joined with "...") is checkable but not contiguous,
    so it gets its own category rather than counting as a fabrication."""
    ok = elided = bad = redecorated = 0
    misses = []
    by_session = collections.defaultdict(list)
    for sid, cat, f in findings(sids):
        by_session[sid].append((cat, f))
    for sid, items in sorted(by_session.items()):
        # Built from the STORED DOCUMENT, not from a re-derived event stream.
        # The old haystack took `text` and `note` only, so a finding quoting a
        # command it was shown scored as a fabrication: `git push 2>&1 |
        # tail -10` was marked "not found" while sitting at the exact seq the
        # finding cited. Commands are two-thirds of what a judge can quote in
        # an execution-heavy session, and the audit could not see any of them.
        # Auditing what was sent, rather than what can be regenerated, is the
        # rule this file already states for everything else.
        head, recs = V.read(sid)
        # TWO haystacks, because a quote can be verbatim against either.
        # `sent` is the exact payload string the model read — JSON, escapes and
        # all. A model copying faithfully from it returns `\"proxy-roles\"`
        # with the backslashes, which is verbatim but does not appear in any
        # parsed value; 20 sound findings were scored as fabrications this way.
        # `plain` is the parsed text with markdown stripped, which catches the
        # opposite case: the model re-decorating a quote it read unescaped.
        sent = norm(payload(head, recs))
        plain = norm(strip_decoration(" ".join(
            " ".join(str(r.get(f) or "") for f in
                     ("text", "note", "command", "of_command", "asked",
                      "feedback"))
            for r in recs)))
        for cat, f in items:
            raw = f.get("quote") or ""
            q = norm(strip_decoration(raw))
            found = (q in plain) or (norm(raw) in sent) or (q in sent)
            if not q:
                bad += 1
                misses.append(("empty", sid, cat, raw))
            elif found:
                ok += 1
                if norm(raw) not in plain and norm(raw) not in sent:
                    redecorated += 1
            elif "..." in q and all(p.strip() in plain or p.strip() in sent
                                    for p in q.split("...") if p.strip()):
                elided += 1
                misses.append(("elided", sid, cat, raw))
            else:
                bad += 1
                misses.append(("not found", sid, cat, raw))
    n = ok + elided + bad
    print("quotes: %d verbatim, %d elided, %d unmatched" % (ok, elided, bad))
    if redecorated:
        print("  (%d of the verbatim ones were re-decorated by the model — "
              "markdown it was\n   never shown, added around correct words)"
              % redecorated)
    if n:
        print("verbatim rate: %.0f%%   checkable rate: %.0f%%"
              % (100.0 * ok / n, 100.0 * (ok + elided) / n))
    for kind, sid, cat, raw in misses[:12]:
        print("\n  [%s] %s %s\n    %s" % (kind, sid[:8], cat, raw[:110]))
    if bad:
        print("\nUnmatched does not mean invented — two of these were checked "
              "by hand and are\nreally in the session, one exactly and one "
              "with a span silently dropped\nmid-quote. Treat them as "
              "unconfirmed and read them before relying on them.")
    return 0


def do_list(model):
    sids = V.built()
    if not sids:
        print("no documents built — run: python3 views.py --all")
        return 1
    print("%-10s %8s %10s %9s %10s  %s"
          % ("session", "records", "~tokens", "calls", "est $", "stage"))
    print("-" * 72)
    tot_tok = tot_done = 0
    est = 0.0
    rate = PRICE[KEEPER_MODEL]
    for sid in sids:
        head, recs = V.read(sid)
        if head is None:
            continue
        # Sized from the payload actually sent, not from the file on disk.
        # They differ — the file is one JSON object per line, the payload is
        # one object — and an estimate that measures the wrong thing is how a
        # 47,049-token call was under-called by 1.9x once already.
        toks = int(len(payload(head, recs)) / CHARS_PER_TOKEN)
        done = load_rows(sid, model, P.PROMPT_VERSION)
        want, unknown = stages_for(sid, model, head, done)
        calls = "1+?" if unknown else str(len(want))
        # One cache write, then a read for each later call.
        c = (toks * rate[2] + max(0, len(want) - 1) * toks * rate[3]) / 1e6
        est += c
        tot_tok += toks
        tot_done += len(done)
        print("%-10s %8d %10s %9s %10s  %s"
              % (sid[:8], len(recs), "{:,}".format(toks), calls, "$%.2f" % c,
                 "done" if not want else " ".join(want)[:28]))
    print("\n%d session(s), ~%s tokens, %d row(s) stored for %s / %s"
          % (len(sids), "{:,}".format(tot_tok), tot_done, model,
             P.PROMPT_VERSION))
    print("outstanding input cost at %s: ~$%.2f" % (KEEPER_MODEL, est))
    print("Input only, at %.1f chars/token. Output is extra and is not small "
          "here —\npass one writes sixteen searched_how sentences whatever it "
          "finds." % CHARS_PER_TOKEN)
    return 0


def do_verify():
    bad = 0
    spend = collections.Counter()
    stages = collections.Counter()
    files = glob.glob(verdict_path("*"))
    n = 0
    for f in files:
        for line in open(f, errors="replace"):
            try:
                d = json.loads(line)
            except Exception:
                bad += 1
                continue
            if d.get("kind") != "verdict":
                continue
            n += 1
            stage = d.get("stage") or "?"
            stages[str(stage).split(":")[0]] += 1
            cats = d.get("categories") or {}
            if stage == "triage" and set(P.CATS) - set(cats):
                bad += 1          # a category was not answered at all
            spend[d.get("model")] += d.get("cost_usd") or 0
    print("%d session file(s), %d verdict row(s)" % (len(files), n))
    for s, c in stages.most_common():
        print("   %-10s %d" % (s, c))
    print("\nrows failing their schema: %d" % bad)
    if spend:
        print("spent by model:")
        for m, c in spend.most_common():
            print("   %-20s $%.2f" % (m, c))
        print("   %-20s $%.2f" % ("TOTAL", sum(spend.values())))
    return 1 if bad else 0


# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--session", nargs="+", metavar="ID")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--origins", action="store_true")
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--category", choices=sorted(P.CATS))
    ap.add_argument("--severity", choices=("cost", "friction", "none"))
    ap.add_argument("--unnoticed", action="store_true")
    ap.add_argument("--model", default=TUNING_MODEL,
                    help="default %s; %s for the pass you keep"
                         % (TUNING_MODEL, KEEPER_MODEL))
    ap.add_argument("--recheck", action="store_true")
    ap.add_argument("--count-tokens", action="store_true",
                    help="with --dry-run: exact input size from the API")
    ap.add_argument("--limit", type=int, help="judge at most N sessions")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--split", action="store_true",
                    help="triage in five calls, one per group (A/B/C/D/E), "
                         "each seeing only its own definitions — the unit is "
                         "still the session, only the question narrows")
    ap.add_argument("--mock", action="store_true",
                    help="read <repo>/mock-views/ and write mock-verdicts/ — "
                         "planted sessions with a known answer key, kept apart "
                         "from real verdicts so a fixture can never be mistaken "
                         "for evidence")
    args = ap.parse_args(argv)

    if args.split:
        # A different prompt SHAPE, so it gets its own version and never shares
        # a cache line with the all-sixteen run it is being compared against.
        P.PROMPT_VERSION = P.PROMPT_VERSION + "-split"
    if args.mock:
        global VIEW, VERDICTS
        V.OUT, V.VIEW = REPO, "mock-views"
        VIEW = "mock-views"
        VERDICTS = os.path.join(REPO, "mock-verdicts")

    if args.list:
        return do_list(args.model)
    if args.verify:
        return do_verify()

    have = V.built()
    if not have:
        print("no documents built — run: python3 views.py --all")
        return 1
    if args.session:
        wanted = []
        for w in args.session:
            hit = [s for s in have if s.startswith(w)]
            if len(hit) != 1:
                raise SystemExit("%r matches %d sessions" % (w, len(hit)))
            wanted.append(hit[0])
    elif args.all or args.show or args.audit or args.origins or args.coverage:
        wanted = have
    else:
        print("pick a scope: --session <id> [...] or --all")
        return 2

    scope = wanted if args.session else None
    if args.show:
        return do_show(scope, args.category, args.severity, args.unnoticed)
    if args.origins:
        return do_origins(scope)
    if args.coverage:
        return do_coverage(scope)
    if args.audit:
        return do_audit(scope)

    psha = hashlib.sha1(P.SYSTEM.encode("utf-8")).hexdigest()[:12]
    todo = []
    for sid in wanted:
        head, recs = V.read(sid)
        if head is None:
            continue
        done = ({} if args.recheck
                else load_rows(sid, args.model, P.PROMPT_VERSION))
        want, _ = stages_for(sid, args.model, head, done, args.split)
        if want:
            todo.append((sid, head, recs, want))
    if args.limit:
        todo = todo[:args.limit]

    if not todo:
        print("nothing to judge: every session has a verdict for %s / %s. "
              "--recheck to re-ask." % (args.model, P.PROMPT_VERSION))
        return 0

    if args.dry_run:
        sid, head, recs, want = todo[0]
        doc = payload(head, recs)
        print("SYSTEM (%s), %d chars — identical in both passes\n"
              % (P.PROMPT_VERSION, len(P.SYSTEM)))
        print(P.SYSTEM)
        print("\n--- USER, block 1: the document (cached), %d chars ---"
              % len(doc))
        print(doc[:5000])
        print("\n--- USER, block 2: the task ---")
        print(P.TRIAGE_TASK)
        toks = sum(int(len(payload(h, r)) / CHARS_PER_TOKEN)
                   for _, h, r, _ in todo)
        if args.count_tokens:
            client = client_or_die()
            if client is None:
                return 1
            toks = 0
            for sid, h, r, _ in todo:
                res = client.messages.count_tokens(
                    model=args.model, system=P.SYSTEM,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": payload(h, r)},
                        {"type": "text", "text": P.TRIAGE_TASK}]}])
                toks += res.input_tokens
        rate = PRICE[args.model]
        print("\n(%d session(s) would be sent, %s stage(s) outstanding, "
              "~%s input tokens %s"
              % (len(todo), sum(len(w) for _, _, _, w in todo),
                 "{:,}".format(toks),
                 "counted" if args.count_tokens else "estimated"))
        print(" first pass at %s: ~$%.2f as a cache write, later passes a "
              "tenth of that)" % (args.model, toks / 1e6 * rate[2]))
        return 0

    warn = save_prompt()
    if warn:
        print(warn)
        return 1

    print("%d session(s), %s, prompt %s. Pass one forces a verdict on all %d "
          "categories;\npass two buys findings %d at a time for what it flags."
          % (len(todo), args.model, P.PROMPT_VERSION, len(P.CATS),
             P.MAX_DETAIL_CATS))

    client = client_or_die()
    if client is None:
        return 1

    spend, nfind, errors = 0.0, 0, collections.Counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(judge_session, client, args.model, sid, head, recs,
                          psha, set(want), args.split): sid
                for sid, head, recs, want in todo}
        for n, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            sid = futs[fut]
            try:
                rows = fut.result()
            except Exception as exc:
                errors["%s: %s" % (type(exc).__name__, exc)] += 1
                print("  %d/%d  %s FAILED" % (n, len(todo), sid[:8]))
                continue
            # Written as each session lands, not batched at the end: a crash
            # after this point must not discard what was already paid for.
            run = {"kind": "run", "ts": datetime.now(timezone.utc).isoformat(),
                   "session": sid, "view": VIEW, "model": args.model,
                   "prompt_version": P.PROMPT_VERSION, "prompt_sha": psha,
                   "stages": [r["stage"] for r in rows],
                   "cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4)}
            append(sid, [run] + rows)
            spend += run["cost_usd"]
            nfind += sum(r.get("findings", 0) for r in rows)
            flagged = sorted({c for r in rows
                              if str(r["stage"]).startswith("triage")
                              for c in r.get("present", [])})
            print("  %d/%d  %s  %d flagged, %d finding(s), $%.2f"
                  % (n, len(todo), sid[:8], len(flagged),
                     sum(r.get("findings", 0) for r in rows), run["cost_usd"]))

    print("\n%d finding(s) across %d session(s), $%.2f -> %s/%s/"
          % (nfind, len(todo), spend, VERDICTS, VIEW))
    if errors:
        print("errors: %s" % dict(errors))
        print("Re-run the same command: finished stages are skipped, so only "
              "the failures cost anything.")
    print("\nnext: --coverage, --origins, --show --unnoticed, --audit, --verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
