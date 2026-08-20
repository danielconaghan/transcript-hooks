#!/usr/bin/env python3
"""UserPromptSubmit interceptor — phase 3: asking, dedupe and verdict capture.

Runs the triggers in rules_engine.py against the message you just submitted,
injects facts for the rules that never interrupt you, injects a directive for
the rules that should raise something with you, and records every fire.

    press enter
      -> UserPromptSubmit fires with {prompt, session_id, transcript_path, cwd}
      -> pending fires from the PREVIOUS turn are resolved against this message
         and written to labels.jsonl (see "the verdict loop")
      -> pre-send state rebuilt from the transcript (incrementally, see below)
      -> rules_engine.evaluate(state)
      -> fires appended to fires.jsonl, ALL of them
      -> augment fires injected every turn via additionalContext
      -> apply/ask fires injected ONCE PER CAUSE, then marked pending_verdict
      -> log fires recorded and never surfaced

Set CLAUDE_RESYNC_PHASE=2 to fall back to the silent behaviour — augments only,
everything else logged. The kill switch exists because phase 3 is the first
phase that can annoy you, and reaching for the uninstaller instead would also
stop the measurement.

Dedupe follows the action
------------------------

An interrupting rule surfaces a given *cause* once per session: a draft you
withdrew, asked about on every subsequent message, is a nag. A silent augment
re-states current facts every turn, because that is what makes them current.
`rules_engine.dedupes()` owns that split and the backtest asks the same
function, so the runtime and the measurement agree on what one fire is.

Seen keys live in the session cache next to the byte offset. Losing the cache
re-surfaces a cause at most once more; nothing depends on it being durable.

The verdict loop
----------------

A hook cannot ask a question and wait — no controlling terminal, and a 30s
timeout. So the ask happens in band across two turns. Turn N injects a
directive addressed to the assistant and records the fire as pending. Turn N+1
reads your reply and parses a verdict *deterministically* — a leading
yes/no/ignore/skip and nothing cleverer. Anything else stays `unlabelled`,
because a guessed label is worse than no label: it silently corrupts the
denominator every other rule is measured against.

Two things deliberately refuse to label:

  * a reply longer than VERDICT_MAX_CHARS — you moved on to the next
    instruction rather than answering, and a long message that happens to open
    with "no" is not a verdict
  * more than one fire pending at once — a bare "yes" cannot be attributed to
    one of three questions

Verdicts land in labels.jsonl, which backtest.py already reads and lets
override every heuristic figure.

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
    python3 intercept.py --verdict "no"  # what that reply would be parsed as
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
LABELS = os.path.join(DATA_DIR, "labels.jsonl")
ERRLOG = os.path.join(DATA_DIR, "intercept-errors.log")
# Credentials for the API-drafted fixes. Outside the repo, 0600. A hook does
# not see your shell's exports, so this file is how a key reaches it.
ENVFILE = os.path.join(HOME_DIR, ".env")

# Substring identifying our hook command, for idempotent install/uninstall.
SENTINEL = "intercept.py"


def phase():
    """3 normally; 2 falls back to augments-only. Anything unparseable reads as
    3 rather than failing, because a typo in an env var should not silently
    switch the service off."""
    try:
        return 2 if int(os.environ.get("CLAUDE_RESYNC_PHASE", "3")) <= 2 else 3
    except Exception:
        return 3


# Cap on retained prior messages. R07 needs only the previous one; R01 compares
# retractions against sent messages, and a few hundred is ample — an unresolved
# retraction from 300 messages ago is not a live concern.
MAX_PRIOR = 300

# Most interrupting fires one session may surface. The catalogue's own
# budget_finding puts the tolerable ask rate at ~1.4% of messages and predicts
# a service asking on 20% "will be disabled within a day". Dedupe already stops
# one cause repeating; this stops many distinct causes arriving at once in a
# session that happens to trip several rules. Clamped fires are logged with
# suppressed="ask-budget" rather than dropped silently, so the cap is visible
# in the data instead of looking like the rules never fired.
MAX_ASKS_PER_SESSION = 5

# A reply longer than this is treated as moving on, not answering. Verdicts are
# short by nature; a 400-character instruction that opens with "no" is a new
# instruction whose first word is coincidence.
VERDICT_MAX_CHARS = 200

SYS_PREFIX = re.compile(
    r"^\s*<(task-notification|bash-|local-command|system-reminder|command-name)")
INTERRUPT = re.compile(r"^\[Request interrupted")

# Leading tokens that settle a pending fire. Matched at the start of the reply
# only, on a word boundary, so "nope" is not read as "no" and a "yes" buried in
# the third sentence is not read at all. Kept small on purpose: every phrase
# added here is a chance to mislabel, and `unlabelled` costs nothing.
VERDICT_AFFIRM = ("yes", "yep", "yeah", "yup", "correct", "right", "true",
                  "confirmed", "agreed", "do it", "go ahead", "please do",
                  "good catch", "true enough")
VERDICT_NEGATE = ("no", "nope", "nah", "ignore", "skip", "wrong", "incorrect",
                  "false", "irrelevant", "disregard", "not relevant",
                  "never mind", "nevermind", "don't", "dont", "do not",
                  "false alarm", "not now")

# Strip conversational scaffolding before looking for the leading token, so
# "ok, no" and "> yes" parse the same as the bare word.
RE_VERDICT_LEAD = re.compile(
    r"^[\s>*_`\-\"'(\[]*(?:ok(?:ay)?|well|hmm+|so|and|but|actually|"
    r"right then)?[\s,.:;!]*", re.I)


def parse_verdict(text):
    """Map a reply to "applies" / "does-not-apply" / "unlabelled".

    Deterministic by design — see the module docstring. No model, no fuzzy
    match, no scoring. If it is not obviously one of the two, it is neither."""
    t = (text or "").strip()
    if not t or len(t) > VERDICT_MAX_CHARS:
        return "unlabelled"
    t = RE_VERDICT_LEAD.sub("", t.lower(), count=1)
    # Longest phrase first, so "do not" is not shadowed by a bare "do it" and
    # "not relevant" wins over "not".
    for phrase, verdict in sorted(
            [(p, "does-not-apply") for p in VERDICT_NEGATE]
            + [(p, "applies") for p in VERDICT_AFFIRM],
            key=lambda kv: -len(kv[0])):
        if re.match(re.escape(phrase) + r"\b", t):
            return verdict
    return "unlabelled"


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
        self.session_id = session_id
        self.path = os.path.join(CACHE_DIR, "%s.json" % session_id)
        # Dedupe and verdict state. Deliberately NOT cleared by
        # reset_transcript_state: a cause is identified by a content hash, so
        # its identity survives the file being rotated or replaced underneath
        # us, and a verdict in flight should not be lost to an epoch boundary.
        self.seen = {}        # fire_key -> iso ts first surfaced
        self.pending = []     # fires awaiting a verdict from the next message
        self.asks = 0         # interrupting fires surfaced this session
        self.drafted = {}     # "rule|key" -> API-drafted text, to avoid re-paying
        self.reset_transcript_state()

    def reset_transcript_state(self):
        """Forget where we were in the transcript, keeping what we learned from
        it. Used when the file turns out to be shorter than we have consumed."""
        import rules_engine as E
        self.offset = 0
        self.acc = E.new_accumulator()

    def load(self):
        try:
            with open(self.path) as fh:
                d = json.load(fh)
            acc = d.get("acc")
            if not (isinstance(acc, dict) and "msgs" in acc):
                raise ValueError("cache shape")
            self.offset = int(d.get("offset") or 0)
            self.acc = acc
            # Absent in caches written by phase 2. Defaulting rather than
            # rejecting means the upgrade costs nothing: an in-flight session
            # keeps its byte offset and simply starts deduping from now.
            seen = d.get("seen")
            self.seen = seen if isinstance(seen, dict) else {}
            pending = d.get("pending")
            self.pending = pending if isinstance(pending, list) else []
            try:
                self.asks = int(d.get("asks") or 0)
            except Exception:
                self.asks = 0
            drafted = d.get("drafted")
            self.drafted = drafted if isinstance(drafted, dict) else {}
        except Exception:
            self.__init__(self.session_id)   # absent, corrupt, or stale shape

    def save(self):
        try:
            os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
            acc = dict(self.acc)
            acc["msgs"] = acc.get("msgs", [])[-MAX_PRIOR:]
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"offset": self.offset, "acc": acc,
                           "seen": self.seen, "pending": self.pending,
                           "asks": self.asks, "drafted": self.drafted}, fh,
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
            # do not assume — re-read the file. Seen keys and pending verdicts
            # survive; they are keyed by content, not by position.
            self.reset_transcript_state()
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

TERMINAL_ROLES = ("", "0", "no", "off", "false")

# The one place anything in this project leaves the machine. Sends the outgoing
# message, the rule's concern and the reference list — never the transcript.
# claude-opus-5 with effort "low": low effort is the latency lever, not
# disabling thinking, which on Opus 5 can put a tool call into visible text.
API_MODEL = "claude-opus-5"

# `output_config.effort` is rejected by Haiku 4.5 and Sonnet 4.5 — sending it
# there is a 400, which fails open to the template and looks like the model
# being useless rather than the request being wrong. Measured: opus-5 5.5s,
# sonnet-5 3.0s, haiku-4-5 1.6s for the same draft.
NO_EFFORT_MODELS = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-haiku-3")
API_TIMEOUT_S = 5.0          # hook ceiling is 30s, but this is added latency
                             # on a message you are waiting to send
API_MAX_TOKENS = 400


def api_enabled():
    """Set CLAUDE_RESYNC_API=0 to stop every outbound call without touching
    the catalogue."""
    return (os.environ.get("CLAUDE_RESYNC_API", "1").strip().lower()
            not in TERMINAL_ROLES)


def import_anthropic():
    """Return the anthropic module, or None.

    Homebrew's python is PEP 668 externally-managed, so `pip install anthropic`
    into it is refused outright. The SDK therefore lives in a venv beside the
    runtime (`~/.claude-resync/.venv`) and this reaches into it, because the
    hook itself runs whatever `python3` is on PATH.

    The interpreter version is part of the path on purpose: a venv built
    against 3.14 is not importable from 3.15, and silently importing a
    mismatched build would be worse than reporting the SDK as missing. Upgrade
    python and `--status` will say `sdk: MISSING` until the venv is rebuilt."""
    site = os.path.join(HOME_DIR, ".venv", "lib",
                        "python%d.%d" % sys.version_info[:2], "site-packages")
    if os.path.isdir(site) and site not in sys.path:
        sys.path.append(site)
    try:
        import anthropic
        return anthropic
    except Exception:
        return None


def load_env_file(path=None):
    """Read ~/.claude-resync/.env into the environment.

    A hook does not inherit your interactive shell, so an `export` in .zshrc is
    not visible here — a file beside the runtime is. Existing environment
    variables always win, so a real export still overrides the file.

    Returns the number of variables set. Never raises: a missing or malformed
    .env must degrade to "no credentials", not to a broken hook."""
    path = path or ENVFILE
    n = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[7:].strip()
                val = val.strip().strip("'").strip('"')
                if key and val and key not in os.environ:
                    os.environ[key] = val
                    n += 1
    except Exception:
        pass
    return n


def draft_via_api(spec, fire, state):
    """Ask a model to draft one injection. Returns (text, status).

    Never raises and never retries: `max_retries=0` because the SDK retries
    timeouts, so wall-clock would be timeout x (retries+1) against a 30s hook
    ceiling. Every failure path returns None so the caller falls back to the
    rule's static template — a rule that says something slightly generic beats
    a rule that says nothing because a socket hung."""
    if not api_enabled():
        return None, "off"
    instruction = (spec or {}).get("instruction")
    if not instruction:
        return None, "no-instruction"
    anthropic = import_anthropic()
    if anthropic is None:
        return None, "sdk-missing"
    load_env_file()
    situation = (
        "Outgoing message:\n%s\n\nRule concern: %s\nReferences: %s\n"
        "Working directory: %s"
        % (state.prompt[:2000], fire.why or "", fire.detail or "",
           state.cwd or "?"))
    try:
        kwargs = {}
        if not API_MODEL.startswith(NO_EFFORT_MODELS):
            # Low effort is the latency lever. Disabling thinking is not: on
            # Opus 5 that can put a tool call into visible text.
            kwargs["output_config"] = {"effort": "low"}
        client = anthropic.Anthropic()
        resp = client.with_options(
            timeout=API_TIMEOUT_S, max_retries=0).messages.create(
                model=API_MODEL,
                max_tokens=API_MAX_TOKENS,
                system=instruction,
                messages=[{"role": "user", "content": situation}],
                **kwargs)
        if resp.stop_reason == "refusal":
            return None, "refusal"
        text = " ".join(b.text for b in resp.content
                        if getattr(b, "type", None) == "text").strip()
        if not text:
            return None, "empty"
        if text.strip().rstrip(".").upper() == "SKIP":
            # The instruction offers SKIP for "nothing useful to say". Honour it
            # as silence, not as a reason to fall back to the generic template —
            # falling back would reintroduce exactly the noise SKIP avoids.
            return None, "skip"
        return text, "drafted"
    except anthropic.APITimeoutError:
        return None, "timeout"
    except anthropic.AuthenticationError:
        return None, "no-credentials"
    except anthropic.RateLimitError:
        return None, "rate-limited"
    except anthropic.APIStatusError as exc:
        log_error("api status %s" % getattr(exc, "status_code", "?"))
        return None, "api-error"
    except anthropic.APIConnectionError:
        return None, "offline"
    except Exception as exc:
        log_error("api call failed: %r" % (exc,))
        return None, "error"


def render_fix(fire, rule_cfg, state, cache=None):
    """Text to inject for one fire. Returns (text, api_status).

    Templates come from the rule's `fix` field in rules.json, so the catalogue
    stays the single record of what each rule does. A rule with no template
    injects nothing — it is logged and counted, but silence beats inventing
    advice the catalogue never sanctioned.

    A dict `fix` means the wording needs reading the situation rather than
    restating it: `{"via": "api", "instruction": ..., "fallback": ...}`. The
    fallback is a plain template and is used whenever the call is off,
    unavailable, slow or refused — so enabling the API can improve an
    injection but can never remove one."""
    spec = (rule_cfg or {}).get("fix")
    if not spec:
        return None, None
    status = None
    if isinstance(spec, dict):
        key = "%s|%s" % (fire.rule, fire.key)
        if cache is not None and key in cache.drafted:
            return cache.drafted[key], "cached"
        text, status = draft_via_api(spec, fire, state)
        if text:
            if cache is not None:
                # Same references in the same session draft to the same advice.
                # Re-paying the latency and the call every turn would be silly.
                cache.drafted[key] = text
            return text, status
        if status == "skip":
            if cache is not None:
                cache.drafted[key] = ""   # remember the silence too
            return None, status
        spec = spec.get("fallback")
        if not spec:
            return None, status
    try:
        return spec.format(detail=fire.detail or "", why=fire.why or "",
                           cwd=state.cwd or ""), status
    except Exception:
        return spec, status


def record_verdict_directive(fire, session_id):
    """Appended to an interrupting rule's injection so the verdict can be
    recorded from natural language rather than guessed from a regex.

    Measured over 441 historical messages, a leading-yes/no parse labels 5.2%
    of replies and inverts some of those ("nope you are correct..."). Reading
    the answer is the one part of this loop a model does better than a regex,
    so the model is asked to record it — as a shell command, which lands in the
    transcript as a structured tool_use rather than as text to be grepped."""
    return (
        "When Daniel answers, record the verdict so this rule can be scored: "
        "python3 \"$HOME/.claude-resync/intercept.py\" --label %s "
        "--key %s --session %s --verdict applies|does-not-apply "
        "--note \"<his answer, verbatim>\". Record does-not-apply if the "
        "concern turned out not to hold. Do not record a verdict he did not "
        "give." % (fire.rule, json.dumps(fire.key), session_id))


# --------------------------------------------------------------------------
# hook mode
# --------------------------------------------------------------------------

def resolve_pending(cache, prompt, session_id):
    """Settle fires left pending by the previous turn against this message.

    Returns the label rows to append. Always clears `pending`: a fire gets one
    adjacent reply to be judged on, and an unanswered one is `unlabelled`
    rather than carried forward hunting for a yes somewhere later."""
    if not cache.pending:
        return []
    pending, cache.pending = cache.pending, []
    # A bare "yes" cannot be attributed when three questions are outstanding.
    ambiguous = len(pending) > 1
    verdict = "unlabelled" if ambiguous else parse_verdict(prompt)
    note = ("%d fires pending; reply cannot be attributed" % len(pending)
            if ambiguous else re.sub(r"\s+", " ", prompt)[:200])
    rows = []
    for p in pending:
        rows.append({
            "ts": _iso(_now()), "session_id": session_id,
            "rule": p.get("rule"), "fire_key": p.get("fire_key"),
            "verdict": verdict, "note": note,
            "asked_at": p.get("at"), "asked_prompt_id": p.get("prompt_id"),
            "action": p.get("action"), "phase": phase(),
            # Distinguishes a verdict inferred from your next reply from one
            # you gave deliberately in `backtest.py --review`. Same weight
            # downstream, but only one of them is something you chose to say.
            "source": "verdict-reply",
        })
    return rows


def run_hook(payload, dry_run=False):
    import rules_engine as E
    t0 = time.monotonic()

    prompt = payload.get("prompt") or ""
    if not prompt.strip():
        return None
    session_id = payload.get("session_id") or "unknown"
    prompt_id = payload.get("prompt_id")
    cwd = payload.get("cwd")
    transcript = payload.get("transcript_path")
    ph = phase()

    cache = SessionCache(session_id)
    cache.load()

    # The verdict is about the PREVIOUS turn's fires, judged on the message
    # arriving now, so this has to happen before anything new is evaluated.
    labels = resolve_pending(cache, prompt, session_id)

    injections, records = [], []
    try:
        if transcript and os.path.exists(transcript):
            cache.advance(transcript)

        resolver = Resolver(cwd=cwd)
        state = cache.to_state(prompt, cwd, resolver)

        catalogue = E.load_catalogue()
        by_id = {r["id"]: r for r in catalogue["rules"]}
        fires = E.evaluate(state)

        injections, records = [], []
        for f in fires:
            action = E.action_for(f.rule, catalogue)
            surfaced, deduped, suppressed, api = False, False, None, None

            if action == "augment":
                # Silent, and re-stated every turn: that is what keeps the
                # facts current. No dedupe, by design.
                text, api = render_fix(f, by_id.get(f.rule), state, cache)
                if text:
                    injections.append(text)
                    surfaced = True
            elif E.dedupes(f.rule, catalogue):
                # One record per cause, whether or not it surfaces, so live
                # counts stay comparable with the backtest's. `deduped` rows
                # are kept rather than dropped — they are how a trigger that
                # re-fires on a stale cause becomes visible at all.
                if f.key in cache.seen:
                    deduped = True
                elif action == "log":
                    suppressed = "precision-floor"
                    cache.seen[f.key] = _iso(_now())
                elif ph < 3:
                    suppressed = "phase-2"
                    cache.seen[f.key] = _iso(_now())
                elif cache.asks >= MAX_ASKS_PER_SESSION:
                    suppressed = "ask-budget"
                    cache.seen[f.key] = _iso(_now())
                else:
                    text, api = render_fix(f, by_id.get(f.rule), state, cache)
                    if not text:
                        # No template and no draft. Nothing to say, so say
                        # nothing — but do not mark it asked.
                        suppressed = "no-template"
                        cache.seen[f.key] = _iso(_now())
                    else:
                        injections.append(
                            text + "\n" + record_verdict_directive(f, session_id))
                        surfaced = True
                        cache.seen[f.key] = _iso(_now())
                        cache.asks += 1
                        cache.pending.append({
                            "rule": f.rule, "fire_key": f.key,
                            "action": action, "at": _iso(_now()),
                            "prompt_id": prompt_id})

            rec = {
                "ts": _iso(_now()), "session_id": session_id,
                "prompt_id": prompt_id,
                "rule": f.rule, "fire_key": f.key, "action": action,
                "surfaced": surfaced, "phase": ph,
                "why": f.why, "detail": (f.detail or "")[:300],
            }
            if deduped:
                rec["deduped"] = True
            if suppressed:
                rec["suppressed"] = suppressed
            if api:
                rec["api"] = api
            if surfaced and action != "augment":
                rec["pending_verdict"] = True
            records.append(rec)
    finally:
        # Persist even if evaluation blew up: otherwise the byte offset is lost
        # and every later call re-reads the whole transcript, and a verdict
        # already taken off `pending` would vanish with it.
        if not dry_run:
            cache.save()
            append_labels(labels)

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
    for r in records:
        r["elapsed_ms"] = elapsed_ms
    if not dry_run:
        append_fires(records)

    if not injections:
        return None
    fired_ids = sorted(set(r["rule"] for r in records if r["surfaced"]))
    asked = sorted(set(r["rule"] for r in records if r.get("pending_verdict")))
    tag = "intercept: %s%s · %.0fms" % (
        " ".join(fired_ids), " (asking %s)" % " ".join(asked) if asked else "",
        elapsed_ms)
    return {
        "systemMessage": tag,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(injections),
        },
    }


def _append_jsonl(path, records, what):
    if not records:
        return
    try:
        os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        os.chmod(path, 0o600)
    except Exception as exc:
        log_error("%s append failed: %r" % (what, exc))


def append_fires(records):
    _append_jsonl(FIRES, records, "fires")


def append_labels(records):
    """Verdicts, in the shape backtest.load_user_labels() reads: it keys on
    (rule, fire_key) and takes only `applies` / `does-not-apply`. `unlabelled`
    rows are written anyway — they are the honest denominator, and the reader
    ignores them."""
    _append_jsonl(LABELS, records, "labels")


# --------------------------------------------------------------------------
# install / status
# --------------------------------------------------------------------------

def do_label(rule, key, verdict, note=None, session_id=None):
    """Record a verdict given in conversation. Called by the assistant, not by
    the hook — see record_verdict_directive.

    Also clears the key from the session's pending list, so the hook's
    leading-yes/no fallback does not write a second, dumber label for the same
    fire on the next message."""
    import rules_engine as E
    if verdict not in ("applies", "does-not-apply"):
        print("verdict must be 'applies' or 'does-not-apply'")
        return 2
    if rule not in E.RULES:
        print("unknown rule %r" % rule)
        return 2
    if not key:
        print("--key is required (the fire_key from the injected directive)")
        return 2
    append_labels([{
        "ts": _iso(_now()), "session_id": session_id, "rule": rule,
        "fire_key": key, "verdict": verdict,
        "note": re.sub(r"\s+", " ", note or "")[:400],
        "source": "assistant-classified", "phase": phase(),
    }])
    cleared = False
    if session_id:
        cache = SessionCache(session_id)
        cache.load()
        before = len(cache.pending)
        cache.pending = [p for p in cache.pending if p.get("fire_key") != key]
        if len(cache.pending) != before:
            cache.save()
            cleared = True
    print("recorded %s %s -> %s%s" % (rule, key, verdict,
                                      " (cleared pending)" if cleared else ""))
    return 0


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
    ph = phase()
    buckets = {}
    for r in cat["rules"]:
        buckets.setdefault(E.action_for(r["id"], cat), []).append(r["id"])
    print("phase      : %d%s" % (ph, "  (CLAUDE_RESYNC_PHASE=2 — asking off)"
                                 if ph < 3 else ""))
    for name, gloss in (("augment", "silent, every turn"),
                        ("apply", "injects its fix, no question"),
                        ("ask", "injects a directive, verdict recorded")):
        print("  %-8s : %-24s (%s)"
              % (name, " ".join(buckets.get(name, [])) or "-", gloss))
    floored = buckets.get("log", [])
    if floored:
        print("  log only : %s  (precision below the %.0f%% floor)"
              % (" ".join(floored), E.PRECISION_FLOOR * 100))
    print("  budget   : max %d interrupting fire(s) per session, once per cause"
          % MAX_ASKS_PER_SESSION)

    api_rules = [r["id"] for r in cat["rules"]
                 if isinstance(r.get("fix"), dict)
                 and r["fix"].get("via") == "api"]
    load_env_file()
    have_key = bool(os.environ.get("ANTHROPIC_API_KEY")
                    or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    mod = import_anthropic()
    sdk = ("installed %s" % getattr(mod, "__version__", "?") if mod
           else "MISSING — %s/.venv/bin/pip install anthropic" % HOME_DIR)
    print("api      : %s%s"
          % ("on" if api_enabled() else "off (CLAUDE_RESYNC_API=0)",
             ", model %s, %.0fs timeout" % (API_MODEL, API_TIMEOUT_S)
             if api_enabled() else ""))
    print("  rules    : %s" % (" ".join(api_rules) or "none"))
    print("  sdk      : %s" % sdk)
    print("  key      : %s  (%s)"
          % ("found" if have_key else "NOT FOUND — falls back to templates",
             ENVFILE))

    if os.path.exists(FIRES):
        import collections
        c, surfaced, deduped, times = collections.Counter(), 0, 0, []
        supp = collections.Counter()
        with open(FIRES, errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                c[d.get("rule")] += 1
                surfaced += 1 if d.get("surfaced") else 0
                deduped += 1 if d.get("deduped") else 0
                if d.get("suppressed"):
                    supp[d["suppressed"]] += 1
                if d.get("elapsed_ms") is not None:
                    times.append(d["elapsed_ms"])
        print("fires.jsonl: %d record(s), %d surfaced, %d deduped"
              % (sum(c.values()), surfaced, deduped))
        if supp:
            print("  suppressed: %s"
                  % ", ".join("%s=%d" % kv for kv in supp.most_common()))
        if times:
            times.sort()
            print("latency    : median %.1fms, p95 %.1fms, max %.1fms"
                  % (times[len(times) // 2],
                     times[int(len(times) * 0.95)], times[-1]))
        for rid, n in c.most_common():
            print("             %-5s %d" % (rid, n))
    else:
        print("fires.jsonl: none yet")

    if os.path.exists(LABELS):
        import collections
        v = collections.Counter()
        with open(LABELS, errors="replace") as fh:
            for line in fh:
                try:
                    v[json.loads(line).get("verdict")] += 1
                except Exception:
                    continue
        print("labels.jsonl: %d verdict(s) — %s"
              % (sum(v.values()),
                 ", ".join("%s=%d" % kv for kv in v.most_common()) or "none"))
    else:
        print("labels.jsonl: none yet")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", metavar="PROMPT", default=None,
                    help="evaluate a prompt without writing fires.jsonl")
    ap.add_argument("--verdict", metavar="REPLY", default=None,
                    help="show how a reply would be parsed as a verdict; with "
                         "--label, the verdict to record")
    ap.add_argument("--label", metavar="RULE", default=None,
                    help="record a verdict for RULE (needs --key and "
                         "--verdict applies|does-not-apply)")
    ap.add_argument("--key", default=None, help="fire_key, with --label")
    ap.add_argument("--session", default=None, help="session id, with --label")
    ap.add_argument("--note", default=None,
                    help="the answer verbatim, with --label")
    args = ap.parse_args(argv)

    if args.install:
        return do_install(remove=False)
    if args.uninstall:
        return do_install(remove=True)
    if args.status:
        return do_status()
    if args.label:
        return do_label(args.label, args.key, args.verdict, args.note,
                        args.session)
    if args.verdict is not None:
        print(parse_verdict(args.verdict))
        return 0
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
