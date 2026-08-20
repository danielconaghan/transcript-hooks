#!/usr/bin/env python3
"""UserPromptSubmit interceptor — phase 2: silent rules and fire logging.

Runs the triggers in rules_engine.py against the message you just submitted,
injects facts for the rules that never interrupt you, and records every fire —
including the ones that will eventually interrupt you but currently don't.

The point of this phase is to gather real fire rates without changing how it
feels to use Claude Code. Nothing asks you anything yet.

    press enter
      -> UserPromptSubmit fires with {prompt, session_id, transcript_path, cwd}
      -> pre-send state rebuilt from the transcript (incrementally, see below)
      -> rules_engine.evaluate(state)
      -> fires appended to fires.jsonl, ALL of them
      -> augment-action fires injected via additionalContext
      -> ask/block-action fires logged only, never surfaced (phase 3)

Why the state read is cheap
---------------------------

Some rules need the whole session, not a tail: a retracted draft (R01) or an
unanswered question (R04) may be hours back. Re-reading an 8 MB transcript on
every keystroke-to-enter would be the slowest thing in the loop.

The transcript is append-only — verified across 51 sessions, and the property
reduce.py is built on — so this keeps a byte offset per session and parses only
what is new since last time. Steady-state cost is a few new lines. If the
offset ever exceeds the file size, or the cache will not parse, it silently
rebuilds from scratch; correctness never depends on the cache being valid.

Failure policy
--------------

Fail open, always, exactly like recorder.py. Any exception, timeout, or
malformed payload results in no output and exit 0, so the prompt goes through
untouched. A hook that can break a session is worse than no hook. There is no
code path here that blocks a prompt.

Network policy
--------------

R08 checks whether a URL serves, which means a socket connect. It probes only
hosts listed in /etc/hosts that map to loopback or a private range — where
local dev servers live, and the case the rule exists for. A public host is
never probed and never resolved: DNS for one public hostname measured 73ms on
the critical path, to conclude the host is public and skip it. Reading
/etc/hosts instead is one cached file read and answers the only useful
question. Total hook latency with URLs, paths and endpoints present: 1-2ms.

Usage:
    python3 intercept.py                 # hook mode: payload on stdin
    python3 intercept.py --install       # register the UserPromptSubmit hook
    python3 intercept.py --uninstall     # remove it
    python3 intercept.py --status        # what is registered, what has fired
    python3 intercept.py --dry-run "some prompt text"
"""

import argparse
import ipaddress
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
# rules_engine sits beside this file when deployed to ~/.claude-resync, and one level
# up when running from the repo (hooks/ -> repo root). Both are on the path so
# the same file works in either layout.
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)


def resync_home():
    """See recorder.resync_home — same resolution, duplicated rather than
    imported so this hook has no import-time dependency on the recorder."""
    env = os.environ.get("CLAUDE_RESYNC_HOME") or os.environ.get(
        "CLAUDE_TRANSCRIPTS_HOME")
    if env:
        return env
    new = os.path.join(os.path.expanduser("~"), ".claude-resync")
    if os.path.isdir(new):
        return new
    legacy = os.path.join(os.path.expanduser("~"), ".claude-transcripts")
    return legacy if os.path.isdir(legacy) else new


HOME_DIR = resync_home()
CACHE_DIR = os.path.join(HOME_DIR, "intercept-cache")
DATA_DIR = os.path.join(HOME_DIR, "data")
FIRES = os.path.join(DATA_DIR, "fires.jsonl")
ERRLOG = os.path.join(DATA_DIR, "intercept-errors.log")

# Substring identifying our hook command, for idempotent install/uninstall.
SENTINEL = "intercept.py"

# Rules that interrupt are logged but not surfaced in this phase.
PHASE = 2

# Cap on retained prior messages. R07 needs only the previous one; R01 compares
# retractions against sent messages, and a few hundred is ample — an unresolved
# retraction from 300 messages ago is not a live concern.
MAX_PRIOR = 300

SYS_PREFIX = re.compile(
    r"^\s*<(task-notification|bash-|local-command|system-reminder|command-name)")
INTERRUPT = re.compile(r"^\[Request interrupted")


def _now():
    return datetime.now(timezone.utc)


def _ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return None


def _iso(dt):
    return dt.isoformat() if dt else None


def log_error(msg):
    """Best-effort error trail. Never raises — this is the last line of defence
    before fail-open, so it must not itself become the failure."""
    try:
        os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
        with open(ERRLOG, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (_now().isoformat(), msg))
    except Exception:
        pass


# --------------------------------------------------------------------------
# incremental session state
# --------------------------------------------------------------------------

class SessionCache(object):
    """Pre-send state for one session, advanced incrementally.

    Holds a byte offset plus the accumulator produced by
    rules_engine.ingest_line — the same accumulator backtest.py builds, cached
    verbatim between invocations because it is plain JSON. Only bytes after the
    offset are parsed, which is what keeps this off the critical path: measured
    30ms to ingest a 2.9 MB transcript cold, 9ms warm."""

    def __init__(self, session_id):
        import rules_engine as E
        self.session_id = session_id
        self.path = os.path.join(CACHE_DIR, "%s.json" % session_id)
        self.offset = 0
        self.acc = E.new_accumulator()

    def load(self):
        try:
            with open(self.path) as fh:
                d = json.load(fh)
            self.offset = int(d.get("offset") or 0)
            acc = d.get("acc")
            if isinstance(acc, dict) and "msgs" in acc:
                self.acc = acc
            else:
                raise ValueError("cache shape")
        except Exception:
            self.__init__(self.session_id)   # absent, corrupt, or stale shape

    def save(self):
        try:
            os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
            acc = dict(self.acc)
            acc["msgs"] = acc.get("msgs", [])[-MAX_PRIOR:]
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"offset": self.offset, "acc": acc}, fh,
                          ensure_ascii=False, default=str)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except Exception as exc:
            log_error("cache save failed: %r" % (exc,))

    def advance(self, transcript_path):
        """Parse everything appended since the last call."""
        import rules_engine as E
        try:
            size = os.path.getsize(transcript_path)
        except Exception:
            return
        if self.offset > size:
            # Shorter than we consumed: rotated, replaced, or a resumed session
            # writing elsewhere. The append-only guarantee is epoch-local, so
            # do not assume — start again.
            self.__init__(self.session_id)
        try:
            with open(transcript_path, "rb") as fh:
                fh.seek(self.offset)
                blob = fh.read()
        except Exception as exc:
            log_error("transcript read failed: %r" % (exc,))
            return
        if not blob:
            return
        # Consume only up to the last complete line; a partially written final
        # line is left for next time rather than parsed as garbage.
        cut = blob.rfind(b"\n")
        if cut < 0:
            return
        consumed, blob = cut + 1, blob[:cut + 1]
        for raw in blob.decode("utf-8", "replace").split("\n"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                E.ingest_line(self.acc, json.loads(raw))
            except Exception:
                continue
        self.offset += consumed

    def to_state(self, prompt, cwd, resolver):
        import rules_engine as E
        return E.state_from_accumulator(
            self.acc, prompt=prompt, at=_now(), session_id=self.session_id,
            cwd=cwd, resolver=resolver, parse_ts=_ts, max_prior=MAX_PRIOR)


# --------------------------------------------------------------------------
# resolver
# --------------------------------------------------------------------------

class Resolver(object):
    """Filesystem and local-network checks, on a hard time budget.

    `url_serves` probes only hosts listed in /etc/hosts that map to loopback or
    a private range — which is precisely where local dev servers live, and the
    case the rule exists for. Everything else returns None (unknown) rather
    than a guess.

    /etc/hosts rather than DNS is deliberate. Resolving a public hostname costs
    a network round trip (measured: 73ms for one host) on the critical path of
    every message, only to conclude the host is public and skip it. Reading
    /etc/hosts is a single cached file read and answers the only question worth
    asking: is this a local dev address that should be listening?"""

    _hosts = None

    def __init__(self, cwd=None, budget_s=0.20):
        self.cwd = cwd or os.getcwd()
        self.deadline = time.monotonic() + budget_s

    @classmethod
    def hosts_map(cls):
        if cls._hosts is not None:
            return cls._hosts
        table = {}
        try:
            with open("/etc/hosts", errors="replace") as fh:
                for line in fh:
                    line = line.split("#", 1)[0].strip()
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    for name in parts[1:]:
                        table[name.lower()] = parts[0]
        except Exception:
            pass
        cls._hosts = table
        return table

    def _out_of_time(self):
        return time.monotonic() > self.deadline

    def exists(self, ref):
        if ref.startswith("github.com/"):
            return None          # not a filesystem question
        p = os.path.expanduser(ref)
        if not os.path.isabs(p):
            p = os.path.join(self.cwd, p)
        try:
            return os.path.exists(p)
        except Exception:
            return None

    def url_serves(self, url):
        if self._out_of_time():
            return None
        try:
            u = urlparse(url)
            host = (u.hostname or "").lower()
            if not host:
                return None
            addr = self.hosts_map().get(host)
            if addr is None:
                if host in ("localhost", "127.0.0.1", "::1"):
                    addr = "127.0.0.1"
                else:
                    return None      # not a known local host: do not probe
            try:
                ip = ipaddress.ip_address(addr)
            except Exception:
                return None
            if not (ip.is_loopback or ip.is_private):
                return None
            port = u.port or (443 if u.scheme == "https" else 80)
            s = socket.socket()
            s.settimeout(max(0.02, min(0.10, self.deadline - time.monotonic())))
            try:
                s.connect((str(ip), port))
                return True
            except Exception:
                return False
            finally:
                s.close()
        except Exception:
            return None


# --------------------------------------------------------------------------
# fix rendering
# --------------------------------------------------------------------------

def render_fix(fire, rule_cfg, state):
    """Text to inject for one fire.

    Templates come from the rule's `fix` field in rules.json, so the catalogue
    stays the single record of what each rule does. A rule with no template
    injects nothing — it is logged and counted, but silence beats inventing
    advice the catalogue never sanctioned."""
    tmpl = (rule_cfg or {}).get("fix")
    if not tmpl:
        return None
    if isinstance(tmpl, dict):
        return None              # {"via": "api"} — phase 3
    try:
        return tmpl.format(detail=fire.detail or "", why=fire.why or "",
                           cwd=state.cwd or "")
    except Exception:
        return tmpl


# --------------------------------------------------------------------------
# hook mode
# --------------------------------------------------------------------------

def run_hook(payload, dry_run=False):
    import rules_engine as E
    t0 = time.monotonic()

    prompt = payload.get("prompt") or ""
    if not prompt.strip():
        return None
    session_id = payload.get("session_id") or "unknown"
    cwd = payload.get("cwd")
    transcript = payload.get("transcript_path")

    cache = SessionCache(session_id)
    cache.load()
    if transcript and os.path.exists(transcript):
        cache.advance(transcript)
        if not dry_run:
            cache.save()   # persist the new offset, or the next call re-reads
                           # the whole transcript and the incremental read is
                           # pointless

    resolver = Resolver(cwd=cwd)
    state = cache.to_state(prompt, cwd, resolver)

    catalogue = E.load_catalogue()
    by_id = {r["id"]: r for r in catalogue["rules"]}
    fires = E.evaluate(state)

    injections, records = [], []
    for f in fires:
        action = E.action_for(f.rule, catalogue)
        surfaced = False
        text = None
        if action == "augment":
            text = render_fix(f, by_id.get(f.rule), state)
            if text:
                injections.append(text)
                surfaced = True
        records.append({
            "ts": _iso(_now()), "session_id": session_id,
            "prompt_id": payload.get("prompt_id"),
            "rule": f.rule, "fire_key": f.key, "action": action,
            "surfaced": surfaced, "phase": PHASE,
            "why": f.why, "detail": (f.detail or "")[:300],
        })

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
    for r in records:
        r["elapsed_ms"] = elapsed_ms
    if not dry_run:
        append_fires(records)

    if not injections:
        return None
    fired_ids = sorted(set(r["rule"] for r in records if r["surfaced"]))
    return {
        "systemMessage": "intercept: %s · %.0fms" % (" ".join(fired_ids),
                                                      elapsed_ms),
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(injections),
        },
    }


def append_fires(records):
    if not records:
        return
    try:
        os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
        with open(FIRES, "a", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        os.chmod(FIRES, 0o600)
    except Exception as exc:
        log_error("fires append failed: %r" % (exc,))


# --------------------------------------------------------------------------
# install / status
# --------------------------------------------------------------------------

def settings_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def hook_command():
    """Points at the DEPLOYED copy, not this file. A hook command holding an
    absolute repo path breaks the moment the repo moves; $HOME/.claude-resync is
    stable and is where install.py puts the runtime."""
    return 'python3 "$HOME/.claude-resync/intercept.py" || true'


def do_install(remove=False):
    path = settings_path()
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception as exc:
        print("cannot read %s: %s" % (path, exc))
        return 1
    backup = path + ".bak-intercept"
    if not os.path.exists(backup):
        with open(backup, "w") as fh:
            json.dump(data, fh, indent=2)
        print("backup: %s" % backup)

    hooks = data.setdefault("hooks", {})
    groups = hooks.get("UserPromptSubmit") or []
    # Strip any of ours, leaving anyone else's alone.
    kept = []
    for g in groups:
        inner = [h for h in (g.get("hooks") or [])
                 if SENTINEL not in (h.get("command") or "")]
        if inner:
            kept.append(dict(g, hooks=inner))
    if remove:
        if kept:
            hooks["UserPromptSubmit"] = kept
        else:
            hooks.pop("UserPromptSubmit", None)
        if not hooks:
            data.pop("hooks", None)
    else:
        kept.append({"hooks": [{"type": "command", "command": hook_command(),
                                "timeout": 15}]})
        hooks["UserPromptSubmit"] = kept

    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    print("%s UserPromptSubmit -> %s"
          % ("removed" if remove else "installed", hook_command()))
    if not remove:
        print("note: Claude Code reads hook config at startup — a new session "
              "or /hooks may be needed before it fires.")
    return 0


def do_status():
    import rules_engine as E
    path = settings_path()
    try:
        with open(path) as fh:
            hooks = (json.load(fh).get("hooks") or {}).get("UserPromptSubmit")
    except Exception:
        hooks = None
    ours = [h for g in (hooks or []) for h in (g.get("hooks") or [])
            if SENTINEL in (h.get("command") or "")]
    print("registered : %s" % ("yes" if ours else "no"))
    for h in ours:
        print("             %s (timeout %ss)" % (h.get("command"),
                                                 h.get("timeout")))
    cat = E.load_catalogue()
    aug = [r["id"] for r in cat["rules"]
           if E.action_for(r["id"], cat) == "augment"]
    other = [r["id"] for r in cat["rules"]
             if E.action_for(r["id"], cat) != "augment"]
    print("phase      : %d — injecting %s; logging only %s"
          % (PHASE, " ".join(aug), " ".join(other)))
    if os.path.exists(FIRES):
        import collections
        c, surfaced, times = collections.Counter(), 0, []
        with open(FIRES, errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                c[d.get("rule")] += 1
                surfaced += 1 if d.get("surfaced") else 0
                if d.get("elapsed_ms") is not None:
                    times.append(d["elapsed_ms"])
        print("fires.jsonl: %d record(s), %d surfaced" % (sum(c.values()),
                                                          surfaced))
        if times:
            times.sort()
            print("latency    : median %.1fms, p95 %.1fms, max %.1fms"
                  % (times[len(times) // 2],
                     times[int(len(times) * 0.95)], times[-1]))
        for rid, n in c.most_common():
            print("             %-5s %d" % (rid, n))
    else:
        print("fires.jsonl: none yet")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", metavar="PROMPT", default=None,
                    help="evaluate a prompt without writing fires.jsonl")
    args = ap.parse_args(argv)

    if args.install:
        return do_install(remove=False)
    if args.uninstall:
        return do_install(remove=True)
    if args.status:
        return do_status()
    if args.dry_run is not None:
        out = run_hook({"prompt": args.dry_run, "session_id": "dry-run",
                        "cwd": os.getcwd()}, dry_run=True)
        print(json.dumps(out, indent=2) if out else "(no injection)")
        return 0

    # hook mode
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        log_error("payload parse failed: %r" % (exc,))
        return 0
    try:
        out = run_hook(payload)
        if out:
            sys.stdout.write(json.dumps(out))
    except Exception as exc:
        import traceback
        log_error("run_hook failed: %r\n%s" % (exc, traceback.format_exc()))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)   # fail open: never block a prompt
