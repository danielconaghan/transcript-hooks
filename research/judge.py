#!/usr/bin/env python3
"""Put a model's judgement on a stored view. The only script that spends money.

    Input  (read-only):  <resync_home>/views/<view>/<session>.jsonl
    Output            :  <resync_home>/verdicts/<view>/<session>.jsonl

The chain is deliberate and each arrow is a different kind of thing:

    corpus  ->  refined     lossless, expensive to redo, must never lose
    refined ->  view        deterministic, free to redo, must never be shared
    view    ->  verdict     costs money, must never be bought twice

This script owns the last arrow and nothing else. It reads a view **from
disk** — not by regenerating it — so the bytes that were judged are the bytes
on the file, and `--audit` can check a quote against exactly what was sent. A
view script that regenerates its own payload at judgement time can drift from
the file it wrote, and then the stored view is decoration rather than evidence.

Why one engine rather than one per view
---------------------------------------

`pairs.py` and `chat.py` each grew their own API client, cache, prompt
versioning, cost arithmetic, quote audit and integrity check — the same six
things, twice, diverging. `evidence.py` had a prompt and no engine at all;
`actions.py` needed one for C4 and had nothing. Every fix to the caching or the
audit had to be made twice and once was missed.

So the view modules keep what is genuinely theirs — the prompt, the schema, the
shape of a unit — and this owns what is common to all of them.

What a view module must expose
------------------------------

    PROMPT_VERSION  bumped by hand when the wording changes MEANING, so a typo
                    fix costs nothing. The text of each version is stored.
    SYSTEM          the prompt
    SCHEMA          JSON schema for the response
    MODE            "session" — one call for the whole session's units, the
                    response an array; or "unit" — one call per unit
    unit_key(p)     a content hash of one payload unit. Excludes model and
                    prompt version, which index the cache alongside it, so the
                    same unit is visibly the same across models.
    rows_from(...)  turn a response into verdict rows, one per unit judged

Nothing else. A view module imports no API client and cannot spend money.

Usage:
    python3 judge.py --list                          # views, units, cost
    python3 judge.py pairs --session a0c27 --dry-run # exact payload, unsent
    python3 judge.py chat  --session a0c27           # judge one session
    python3 judge.py evidence --all --model claude-opus-5
    python3 judge.py chat --show [--unnoticed]
    python3 judge.py pairs --audit                   # quotes verbatim?
    python3 judge.py --verify                        # integrity and spend
"""

import argparse
import collections
import concurrent.futures
import glob
import hashlib
import importlib
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import session as S       # noqa: E402

VIEWS_DIR = os.path.join(S.home(), "views")
VERDICT_DIR = os.path.join(S.home(), "verdicts")

# Views this engine knows how to judge. A view without a prompt is listed here
# with `judgeable: False` so `--list` reports it honestly rather than hiding
# it: `actions` produces C2 and B3 deterministically and needs no model at all,
# and saying so is more useful than an absence.
VIEWS = {
    "pairs":    {"module": "pairs",    "judgeable": True},
    "chat":     {"module": "chat",     "judgeable": True},
    "evidence": {"module": "evidence", "judgeable": True},
    "actions":  {"module": "actions",  "judgeable": False,
                 "note": "C2 and B3 are deterministic; only C4 would need a model"},
}

TUNING_MODEL = "claude-haiku-4-5"
KEEPER_MODEL = "claude-opus-5"
NO_EFFORT_MODELS = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-haiku-3")

# $/1M input, $/1M output. Quoted so a stored `usage` converts to money years
# later without anyone having to remember the rate.
PRICE = {"claude-haiku-4-5": (1.00, 5.00),
         "claude-opus-5": (5.00, 25.00),
         "claude-sonnet-5": (3.00, 15.00)}

# Characters per token in these payloads, MEASURED. The usual chars/4 rule of
# thumb understated a real 47,049-token call by 1.9x: this is JSON dense with
# timestamps, identifiers, paths and code, none of which tokenise like prose.
CHARS_PER_TOKEN = 2.1


def mod(view):
    return importlib.import_module(VIEWS[view]["module"])


# --------------------------------------------------------------------------
# reading the view
# --------------------------------------------------------------------------

def view_path(view, sid):
    return os.path.join(VIEWS_DIR, view, "%s.jsonl" % sid)


def view_sessions(view):
    return sorted(os.path.basename(p)[:-6]
                  for p in glob.glob(os.path.join(VIEWS_DIR, view, "*.jsonl")))


def read_view(view, sid):
    """(header, [units]) exactly as written. No regeneration."""
    p = view_path(view, sid)
    if not os.path.exists(p):
        return None, []
    head, units = None, []
    with open(p, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("kind") == "header":
                head = d
            else:
                units.append(d)
    return head, units


# --------------------------------------------------------------------------
# storing the verdict
# --------------------------------------------------------------------------

def ensure_verdicts(view):
    d = os.path.join(VERDICT_DIR, view)
    os.makedirs(d, mode=0o700, exist_ok=True)
    gi = os.path.join(VERDICT_DIR, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as fh:
            fh.write("# Auto-created by judge.py. Quotes developer and "
                     "assistant text verbatim — never commit.\n*\n")
    return d


def verdict_path(view, sid):
    return os.path.join(VERDICT_DIR, view, "%s.jsonl" % sid)


def load_verdicts(view, sid, model=None, version=None):
    """{unit_key: row} — the cache, indexed by UNIT not by session.

    This is what stops a re-run costing what the first run cost. Adding a
    session, widening a view's cap, or re-running after a crash leaves every
    already-judged unit alone. The first design keyed on a hash of the whole
    payload plus the prompt text, so a one-word prompt edit re-ran everything
    at full price."""
    p = verdict_path(view, sid)
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
            got[d.get("unit_key")] = d
    return got


def append_verdicts(view, sid, rows):
    if not rows:
        return
    ensure_verdicts(view)
    p = verdict_path(view, sid)
    with open(p, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    os.chmod(p, 0o600)


def save_prompt(view, m):
    """Store each prompt version once, so `what was v1` stays answerable.

    The cache keys on a version STRING rather than a hash of the text, which is
    what stops a typo fix costing a full re-run — but then the text has to be
    recoverable from somewhere, and a changed prompt under an unchanged version
    must be caught rather than silently served from cache."""
    d = os.path.join(VERDICT_DIR, view, "prompts")
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, "%s.txt" % m.PROMPT_VERSION)
    if os.path.exists(path):
        with open(path, errors="replace") as fh:
            if fh.read() != m.SYSTEM:
                return ("MISMATCH: %s differs from the module's current SYSTEM. "
                        "Bump PROMPT_VERSION, or the cache serves verdicts from "
                        "the old wording." % path)
        return None
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(m.SYSTEM)
    os.chmod(path, 0o600)
    return None


def cost_of(model, usage):
    rate = PRICE.get(model)
    if not usage or not rate:
        return None
    return round(usage.get("input_tokens", 0) / 1e6 * rate[0]
                 + usage.get("output_tokens", 0) / 1e6 * rate[1], 4)


# --------------------------------------------------------------------------
# the call
# --------------------------------------------------------------------------

def client_or_die():
    sys.path.insert(0, S.home())
    import intercept as I           # reuse its venv + .env resolution
    anthropic = I.import_anthropic()
    if anthropic is None:
        print("anthropic SDK not importable — %s/.venv" % S.home())
        return None
    I.load_env_file()
    return anthropic.Anthropic()


def ask(client, model, m, user):
    fmt = {"format": {"type": "json_schema", "schema": m.SCHEMA}}
    if not model.startswith(NO_EFFORT_MODELS):
        fmt["effort"] = "medium"
    resp = client.messages.create(
        model=model, max_tokens=16000, system=m.SYSTEM,
        messages=[{"role": "user", "content": user}], output_config=fmt)
    if resp.stop_reason == "refusal":
        raise RuntimeError("refused")
    blob = next(b.text for b in resp.content
                if getattr(b, "type", None) == "text")
    u = resp.usage
    return json.loads(blob), {
        "input_tokens": getattr(u, "input_tokens", 0),
        "output_tokens": getattr(u, "output_tokens", 0)}


def render(m, sid, units):
    return ("SESSION: %s\n\n%s (%d):\n%s"
            % (sid, getattr(m, "UNIT_LABEL", "UNITS"), len(units),
               json.dumps(units, ensure_ascii=False, indent=1, default=str)))


def judge_session(client, model, view, m, sid, todo, psha):
    """One session's outstanding units. Returns (rows, usage, findings)."""
    now = datetime.now(timezone.utc).isoformat()
    units = list(todo.values())
    base = {"session": sid, "model": model, "view": view,
            "prompt_version": m.PROMPT_VERSION, "prompt_sha": psha, "ts": now}

    if getattr(m, "MODE", "session") == "session":
        data, usage = ask(client, model, m, render(m, sid, units))
        rows = m.rows_from(data, todo, base)
        return rows, usage, sum(1 for r in rows if r.get("findings")
                                or r.get("contradiction")), data

    rows, usage, found, raws = [], {"input_tokens": 0, "output_tokens": 0}, 0, []
    for k, u in todo.items():
        data, us = ask(client, model, m, json.dumps(u, ensure_ascii=False,
                                                    indent=1, default=str))
        usage["input_tokens"] += us["input_tokens"]
        usage["output_tokens"] += us["output_tokens"]
        raws.append(data)
        r = m.rows_from(data, {k: u}, base)
        rows += r
        found += sum(1 for x in r if x.get("findings") or x.get("contradiction"))
    return rows, usage, found, raws


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def norm(s):
    return " ".join((s or "").split()).lower()


def all_verdicts(view, sids=None, model=None):
    out = []
    for sid in (sids or sorted(os.path.basename(p)[:-6] for p in
                               glob.glob(verdict_path(view, "*")))):
        seen = {}
        for line in open(verdict_path(view, sid), errors="replace"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("kind") != "verdict":
                continue
            if model and d.get("model") != model:
                continue
            seen[d.get("unit_key")] = d      # newest wins
        out += list(seen.values())
    return out


def do_list():
    print("%-10s %7s %8s %10s %9s  %s"
          % ("view", "files", "units", "~tokens", "opus $", "judged"))
    print("-" * 62)
    for v in sorted(VIEWS):
        files = glob.glob(os.path.join(VIEWS_DIR, v, "*.jsonl"))
        units = chars = 0
        for f in files:
            lines = open(f, errors="replace").readlines()
            units += max(0, len(lines) - 1)
            chars += sum(len(l) for l in lines[1:])
        toks = int(chars / CHARS_PER_TOKEN)
        done = len(glob.glob(verdict_path(v, "*")))
        tag = "" if VIEWS[v]["judgeable"] else "  (no model needed)"
        print("%-10s %7d %8d %10s %9s  %d session(s)%s"
              % (v, len(files), units, "{:,}".format(toks),
                 "$%.2f" % (toks / 1e6 * PRICE[KEEPER_MODEL][0]), done, tag))
    print("\nEstimates use %.1f chars/token, measured on these payloads."
          % CHARS_PER_TOKEN)
    print("Output tokens are extra: a findings-heavy run cost about 50 percent")
    print("above its input estimate.")
    return 0


def do_verify():
    total = collections.Counter()
    bad = 0
    print("%-10s %8s %9s %9s" % ("view", "sessions", "verdicts", "spent"))
    print("-" * 40)
    for v in sorted(VIEWS):
        files = glob.glob(verdict_path(v, "*"))
        n = 0
        spend = 0.0
        for f in files:
            for line in open(f, errors="replace"):
                try:
                    d = json.loads(line)
                except Exception:
                    bad += 1
                    continue
                if d.get("kind") == "verdict":
                    n += 1
                elif d.get("kind") == "run":
                    spend += d.get("cost_usd") or 0
                    total[d.get("model")] += d.get("cost_usd") or 0
        if files:
            print("%-10s %8d %9d %9s" % (v, len(files), n, "$%.2f" % spend))
    print("\nunparseable rows: %d" % bad)
    if total:
        print("spent by model:")
        for m, c in total.most_common():
            print("   %-20s $%.2f" % (m, c))
        print("   %-20s $%.2f" % ("TOTAL", sum(total.values())))
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("view", nargs="?", choices=sorted(VIEWS))
    ap.add_argument("--session", nargs="+", metavar="ID")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--model", default=TUNING_MODEL,
                    help="default %s; %s for the pass you keep"
                         % (TUNING_MODEL, KEEPER_MODEL))
    ap.add_argument("--recheck", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args, rest = ap.parse_known_args(argv)

    if args.list:
        return do_list()
    if args.verify:
        return do_verify()
    if not args.view:
        ap.print_help()
        return 2

    spec = VIEWS[args.view]
    if not spec["judgeable"]:
        print("%s needs no model: %s" % (args.view, spec.get("note", "")))
        return 0
    m = mod(args.view)

    have = view_sessions(args.view)
    if not have:
        print("no view stored — run %s --write first" % spec["module"])
        return 1
    if args.session:
        wanted = []
        for w in args.session:
            hit = [s for s in have if s.startswith(w)]
            if len(hit) != 1:
                raise SystemExit("%r matches %d sessions" % (w, len(hit)))
            wanted.append(hit[0])
    elif args.all:
        wanted = have
    else:
        print("pick a scope: --session <id> [...] or --all")
        return 2

    if args.show or args.audit:
        rows = all_verdicts(args.view, wanted if args.session else None)
        fn = getattr(m, "audit" if args.audit else "show", None)
        if fn is None:
            print("%s has no %s renderer" % (args.view, "audit" if args.audit
                                             else "show"))
            return 2
        return fn(rows, rest)

    psha = hashlib.sha1(m.SYSTEM.encode("utf-8")).hexdigest()[:12]
    work = []
    for sid in wanted:
        head, units = read_view(args.view, sid)
        if not units:
            continue
        keyed = {m.unit_key(u): u for u in units}
        done = ({} if args.recheck
                else load_verdicts(args.view, sid, args.model, m.PROMPT_VERSION))
        todo = {k: v for k, v in keyed.items() if k not in done}
        if todo:
            work.append((sid, todo, head))

    if not work:
        print("nothing to judge: every unit already has a verdict for %s / %s. "
              "--recheck to re-ask." % (args.model, m.PROMPT_VERSION))
        return 0

    if args.dry_run:
        for sid, todo, _ in work[:1]:
            print("SYSTEM (%s), %d chars\n" % (m.PROMPT_VERSION, len(m.SYSTEM)))
            if getattr(m, "MODE", "session") == "session":
                print(render(m, sid, list(todo.values())))
            else:
                print(json.dumps(list(todo.values())[0], ensure_ascii=False,
                                 indent=1, default=str))
        print("\n(%d session(s), %d unit(s) would be sent)"
              % (len(work), sum(len(t) for _, t, _ in work)))
        return 0

    warn = save_prompt(args.view, m)
    if warn:
        print(warn)
        return 1

    total_units = sum(len(t) for _, t, _ in work)
    print("%s: %d unit(s) across %d session(s), %s, prompt %s"
          % (args.view, total_units, len(work), args.model, m.PROMPT_VERSION))

    client = client_or_die()
    if client is None:
        return 1

    spend, found, errors = 0.0, 0, collections.Counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(judge_session, client, args.model, args.view, m,
                          sid, todo, psha): sid for sid, todo, _ in work}
        for n, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            sid = futs[fut]
            try:
                rows, usage, nf, raw = fut.result()
                cost = cost_of(args.model, usage)
                run = {"kind": "run", "ts": datetime.now(timezone.utc).isoformat(),
                       "session": sid, "view": args.view, "model": args.model,
                       "prompt_version": m.PROMPT_VERSION, "prompt_sha": psha,
                       "units": len(rows), "findings": nf,
                       "usage": usage, "cost_usd": cost, "raw": raw}
                append_verdicts(args.view, sid, [run] + rows)
                spend += cost or 0
                found += nf
            except Exception as exc:
                errors["%s: %s" % (type(exc).__name__, exc)] += 1
            print("  %d/%d" % (n, len(work)))

    print("\n%d finding(s), $%.2f spent -> %s/%s/<session>.jsonl"
          % (found, spend, VERDICT_DIR, args.view))
    if errors:
        print("errors: %s" % dict(errors))
        print("Re-run the same command: judged units are skipped, so only the "
              "failures cost anything.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
