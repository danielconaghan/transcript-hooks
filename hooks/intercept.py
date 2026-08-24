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
question.

Latency, measured rather than estimated: median 18ms, p95 47ms for the
deterministic path (see `--status`). A rule with an API-drafted fix adds up to
API_TIMEOUT_S on the first message citing a given set of references — measured
5.5s for claude-opus-5 — and nothing after that, because the draft is cached per
cause for the session. `CLAUDE_RESYNC_API=0` removes it entirely.

Usage:
    python3 intercept.py                 # hook mode: payload on stdin
    python3 intercept.py --install       # register the UserPromptSubmit hook
    python3 intercept.py --uninstall     # remove it
    python3 intercept.py --status        # what is registered, what has fired
    python3 intercept.py --dry-run "some prompt text"
    python3 intercept.py --verdict "no"  # what that reply would be parsed as
    python3 intercept.py --recent [N] [--rule R06] [--session S] [--todo]
    python3 intercept.py --label R06 --key K --verdict applies --note "..."

Note `--verdict` does double duty: alone it shows how a reply would parse, and
with --label it is the verdict being recorded.
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
# Every invocation of the normalisation layer, so "does it pay for itself?" is
# answerable rather than a matter of opinion: latency paid, and whether it had
# anything to say.
NORMALISE_LOG = os.path.join(DATA_DIR, "normalise.jsonl")
# Desyncs you called out yourself, via /ds. The only source of the two things
# a transcript cannot yield: a desync that produced no visible correction, and
# what the right answer actually was.
MARKERS = os.path.join(DATA_DIR, "markers.jsonl")
# Credentials for the API-drafted fixes. Outside the repo, 0600. A hook does
# not see your shell's exports, so this file is how a key reaches it.
ENVFILE = os.path.join(HOME_DIR, ".env")

# Substrings identifying a hook command as ours, for idempotent install and
# surgical uninstall. Path-qualified rather than the bare filename so
# --uninstall can never strip an unrelated hook that happens to mention
# "intercept.py". Kept in step with install.py's SENTINELS, and listing the
# pre-rename home so uninstall still works on an older install.
SENTINELS = (
    ".claude-resync/intercept.py",
    ".claude-transcripts/intercept.py",    # legacy
)


def is_ours(command):
    return isinstance(command, str) and any(x in command for x in SENTINELS)


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
def _engine_cfg(key, default):
    """One engine-wide setting from rules.json's `engine` block.

    The catalogue is the single place these live; the literal passed as
    `default` is what the hook uses when rules.json is missing or unreadable,
    because a hook that cannot load its config must still run rather than
    refuse the prompt."""
    try:
        import rules_engine as E
        return E.engine_param(key, default)
    except Exception:
        return default


MAX_PRIOR = 300

# Most interrupting fires one session may surface. The catalogue's own
# budget_finding puts the tolerable ask rate at ~1.4% of messages and predicts
# a service asking on 20% "will be disabled within a day". Dedupe already stops
# one cause repeating; this stops many distinct causes arriving at once in a
# session that happens to trip several rules. Clamped fires are logged with
# suppressed="ask-budget" rather than dropped silently, so the cap is visible
# in the data instead of looking like the rules never fired.
MAX_ASKS_PER_SESSION = _engine_cfg("max_asks_per_session", 5)

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


def _sha_text(s):
    import hashlib
    return hashlib.sha1((s or "").encode("utf-8", "replace")).hexdigest()[:16]


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
        self.normalised = {}  # prompt hash -> restatement, "" for a SKIP
        self.norm_fails = 0   # consecutive normalisation failures, for the breaker
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
            norm = d.get("normalised")
            self.normalised = norm if isinstance(norm, dict) else {}
            try:
                self.norm_fails = int(d.get("norm_fails") or 0)
            except Exception:
                self.norm_fails = 0
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
                           "asks": self.asks, "drafted": self.drafted,
                           "normalised": self.normalised,
                           "norm_fails": self.norm_fails}, fh,
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

FALSY_ENV = ("", "0", "no", "off", "false")

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
            not in FALSY_ENV)


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


# --------------------------------------------------------------------------
# normalisation layer
# --------------------------------------------------------------------------
#
# Not a rule, and deliberately outside rules.json.
#
# A rule needs a deterministic trigger. Whether a directive is weakened —
# hedged, understated, or delivered as a question — cannot be determined
# deterministically: a regex over 448 messages found understatement 0 times,
# and the model found 15. Any trigger written for it either fires on nearly
# everything (weakening is the ambient register) or misses most of it, so the
# trigger would not be detecting anything. It would be a cost gate wearing a
# rule's clothes, with a precision field it could never earn.
#
# So this always runs and the model is the detector. SKIP is the no-op. It makes
# no prediction and claims no precision — it restates a buried instruction at
# full force, which cannot do harm even when it was not needed.
#
# It is a third category alongside the two the project already names: not a
# preventative action (it predicts nothing) and not a retrospective marker (it
# reports nothing). It normalises the input.
#
# The cost is real and paid on every message: measured 1.6s on Haiku, 3.0s on
# Sonnet, 5.5s on Opus. Haiku by default for that reason. Whether it pays for
# itself is a phase 4 question, which is why every invocation is logged.
NORMALISE_MODEL = _engine_cfg("normalise.model", "claude-haiku-4-5")
NORMALISE_TIMEOUT_S = _engine_cfg("normalise.timeout_s", 4.0)
NORMALISE_MAX_TOKENS = 300

# After this many consecutive failures the layer stops trying for the rest of
# the session. Without it, an unreachable API would add the full timeout to
# every message you send — the one failure mode that would make this
# intolerable rather than merely slow.
NORMALISE_MAX_FAILS = 3

NORMALISE_SYSTEM = """\
A developer has sent the message below to an AI coding assistant. Its force may \
be reduced by how it is phrased — an epistemic hedge, an understatement, or an \
instruction delivered as a question — so the assistant might read a correction \
or an instruction as mere curiosity and fail to act on it.

Write one or two sentences addressed to the ASSISTANT, restating what the \
developer is actually asking for or asserting, at full force.

Two parties, named the same way every time. "You" is the assistant and \
nothing else — never the developer, and never replaced by "me" or "I". The \
person who sent the message is "the developer" — never "you". Never use he, \
she, him, her, his or hers: a message does not tell you the sender's gender, \
and it has no bearing on the restatement.

Rules, in order of importance:

1. Restore FORCE, never CONFIDENCE — the one rule that must not be broken.
   "Force" is whether this is an instruction. "Confidence" is whether the claim
   is true. Raise the first, never the second.
     Given "i think the Useful URLs are incorrect, now":
       WRONG  "The Useful URLs need updating because they are now incorrect."
              (asserts as fact what the developer offered as a belief)
       RIGHT  "This functions as an instruction: the developer believes the
              Useful URLs are now incorrect and wants you to check them."
   Note the shape of the RIGHT line as well as its content: the developer in
   the third person, the assistant as "you". That is the shape every
   restatement takes.
   Attribute the belief to the developer. Never state it as established.
   Putting words in the developer's mouth is worse than leaving the hedge
   alone.
2. Be respectful. Never characterise the developer or the message — not
   unclear, not indirect, not frustrated, not annoyed, not telling you off.
   Describe the message, never the person.
3. Politeness is not noise. "please", "can you", "would you mind" are the
   developer's normal register and carry full force. Never present removing
   them as a correction, and never treat their presence as weakening.
4. Never fabricate a quote. Paraphrase, or quote verbatim.
5. Address the assistant, never the developer. This is the rule most often
   broken, so it gets examples too. Summarising a request as "you want X" is
   idiomatic English and completely wrong here, because it makes the developer
   the addressee:
     Given "I wonder if the SEO is any good.. can you give it a healthcheck
     score (don't change naything)":
       WRONG  "You want a search and SEO healthcheck of the site, without
              making any changes."          ("you" is the developer)
       WRONG  "You want me to run a healthcheck."
                                            (same inversion, assistant as "me")
       WRONG  "You want the assistant to audit the site."
                                            (same inversion, third person)
       RIGHT  "The developer wants you to run a search and SEO healthcheck of
              the site and to report a score, and to change nothing while
              doing it."
   Whenever the sentence describes what the developer wants, the developer is
   the SUBJECT of it and you are the one being asked.
6. Be brief and plain. No preamble, no restating these rules.

Reply with exactly SKIP — adding nothing else — whenever there is no weakening \
to undo. That is the common case and it costs nothing, whereas a restatement \
that adds no information is noise on a message that was already clear. SKIP when:

  * the message is ALREADY a direct instruction or assertion. "please fix
    manifest.local.json still isn't gitignored" is an imperative; repeating it
    back helps nobody. Only restate when the force is genuinely reduced.
  * it is a genuine question seeking information
  * it is an answer to something the assistant asked
  * it is pasted specification, brief, or reference material
  * your restatement would say no more than the message already says\
"""


def normalise_enabled():
    """Its own switch, separate from CLAUDE_RESYNC_API, because this one is paid
    on every message rather than on a rule firing — it deserves to be turnable
    off without disabling the API-drafted fixes too."""
    if not api_enabled():
        return False
    return (os.environ.get("CLAUDE_RESYNC_NORMALISE", "1").strip().lower()
            not in FALSY_ENV)


def _norm_timing(total_t0, setup_ms=0.0, request_ms=0.0):
    return {"total_ms": round((time.monotonic() - total_t0) * 1000, 1),
            "setup_ms": round(setup_ms, 1),
            "request_ms": round(request_ms, 1)}


def normalise(prompt, cache=None):
    """Restate a weakened directive at full force.

    Returns (text, status, timing), where timing splits the elapsed time into
    `setup_ms` and `request_ms`.

    The split is not decoration. One timer starting at function entry charged
    the SDK import and the .env read to the model, which is how `--status` came
    to report a p95 of 10s against a request the SDK is given 4.0s to make —
    two figures that cannot both describe the same call. The tell was R03, a
    rule that makes no request at all, logging 9-13s in those same messages.
    Since the whole keep-or-drop argument for this layer rests on what it costs
    per message, the number has to say WHICH part is expensive: a slow import
    is fixed by importing once, a slow model by choosing another.

    text is None when there is nothing to say, which is the common case."""
    t0 = time.monotonic()
    if not normalise_enabled():
        return None, "off", _norm_timing(t0)
    if len((prompt or "").strip()) < 15:
        return None, "too-short", _norm_timing(t0)
    key = _sha_text(prompt)
    if cache is not None and key in cache.normalised:
        return (cache.normalised[key] or None), "cached", _norm_timing(t0)
    if cache is not None and cache.norm_fails >= NORMALISE_MAX_FAILS:
        return None, "circuit-open", _norm_timing(t0)

    # Setup: the venv path append plus `import anthropic`, then the .env read.
    # Paid in full on the first message of a session and largely cached by the
    # OS afterwards, which is exactly why it must not be averaged into the
    # request figure.
    t_setup = time.monotonic()
    anthropic = import_anthropic()
    if anthropic is None:
        return None, "sdk-missing", _norm_timing(t0)
    load_env_file()
    setup_ms = (time.monotonic() - t_setup) * 1000
    # t_req is a sentinel, not just a stopwatch: on a timeout the request has
    # spent the whole budget and reporting it as 0ms is exactly the kind of
    # misattribution the split exists to remove.
    t_req = None
    request_ms = 0.0
    try:
        client = anthropic.Anthropic()
        t_req = time.monotonic()
        resp = client.with_options(
            timeout=NORMALISE_TIMEOUT_S, max_retries=0).messages.create(
                model=NORMALISE_MODEL, max_tokens=NORMALISE_MAX_TOKENS,
                system=NORMALISE_SYSTEM,
                messages=[{"role": "user", "content": prompt[:4000]}])
        request_ms = (time.monotonic() - t_req) * 1000
        timing = _norm_timing(t0, setup_ms, request_ms)
        if resp.stop_reason == "refusal":
            return None, "refusal", timing
        text = " ".join(b.text for b in resp.content
                        if getattr(b, "type", None) == "text").strip()
        if cache is not None:
            cache.norm_fails = 0
        if not text or text.rstrip(".").upper() == "SKIP":
            if cache is not None:
                cache.normalised[key] = ""      # remember the silence too
            return None, "skip", timing
        if cache is not None:
            cache.normalised[key] = text
        return text, "restated", timing
    except Exception as exc:
        if cache is not None:
            cache.norm_fails += 1
        log_error("normalise failed: %r" % (exc,))
        if t_req is not None:
            request_ms = (time.monotonic() - t_req) * 1000
        return None, "error", _norm_timing(t0, setup_ms, request_ms)


def append_normalise(row):
    _append_jsonl(NORMALISE_LOG, [row], "normalise")


def do_normalise_log(limit=20, restated_only=True):
    """Before and after, side by side — what you typed against the restatement.

    The transcript holds both but merges every injection in a turn into one
    additionalContext string, so the pair is only recoverable from this log."""
    if not os.path.exists(NORMALISE_LOG):
        print("no invocations yet (%s)" % NORMALISE_LOG)
        return 0
    rows = []
    with open(NORMALISE_LOG, errors="replace") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    shown = [r for r in rows
             if not restated_only or r.get("status") == "restated"]
    import collections
    st = collections.Counter(r.get("status") for r in rows)
    print("%d invocation(s): %s\n"
          % (len(rows), ", ".join("%s=%d" % kv for kv in st.most_common())))
    for r in shown[-max(1, limit):]:
        print("-" * 74)
        print("%s  %s  %.0fms" % ((r.get("ts") or "")[11:19],
                                  r.get("status"), r.get("elapsed_ms") or 0))
        print("  you typed  : %s" % " ".join((r.get("prompt") or "").split())[:300])
        print("  restated as: %s" % " ".join((r.get("restatement") or "").split())[:300])
    if restated_only and st.get("skip"):
        print("\n(%d skipped invocation(s) hidden — pass --all to include them)"
              % st["skip"])
    return 0


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
    norm_text, norm_status, norm_timing = normalise(prompt, cache)
    if norm_text:
        injections.append(norm_text)
    if not dry_run:
        append_normalise({
            "ts": _iso(_now()), "session_id": session_id,
            "prompt_id": prompt_id, "status": norm_status,
            # elapsed_ms stays the total, so the 77 rows written before the
            # split remain comparable; setup/request are the new detail.
            "elapsed_ms": norm_timing["total_ms"],
            "setup_ms": norm_timing["setup_ms"],
            "request_ms": norm_timing["request_ms"],
            "model": NORMALISE_MODEL,
            "prompt_chars": len(prompt),
            # The original alongside the restatement, so before/after is one
            # row rather than a join across two files. The transcript has both
            # too, but every injection in a turn is merged into a single
            # additionalContext string there, so the pair cannot be recovered
            # from it. Same secrets exposure as fires.jsonl, which already
            # keeps up to 300 chars of prompt in `detail`.
            "prompt": prompt[:1000],
            "restatement": (norm_text or "")[:1000],
        })
    try:
        if transcript and os.path.exists(transcript):
            cache.advance(transcript)

        resolver = Resolver(cwd=cwd)
        state = cache.to_state(prompt, cwd, resolver)

        catalogue = E.load_catalogue()
        by_id = {r["id"]: r for r in catalogue["rules"]}
        fires = E.evaluate(state)

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
            elif action == "skip":
                suppressed = "not-in-catalogue"
            else:
                # Every non-augment action dedupes (see rules_engine.dedupes),
                # so: one record per cause whether or not it surfaces, keeping
                # live counts comparable with the backtest's. `deduped` rows are
                # kept rather than dropped — they are how a trigger that
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
    tag = "intercept: %s%s%s · %.0fms" % (
        " ".join(fired_ids) or "-",
        " (asking %s)" % " ".join(asked) if asked else "",
        " +normalised" if norm_text else "", elapsed_ms)
    return {
        "systemMessage": tag,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(injections),
        },
    }


RE_BACK = re.compile(r"^-?(\d+)$")


def live_transcript():
    """The transcript being written right now: newest mtime under
    ~/.claude/projects. There is no ambient session id for a CLI run from a
    slash command, and guessing one is worse than deriving it — the file also
    carries the message uuids the backwalk needs, which no argument could."""
    root = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    best, best_mt = None, -1.0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if not f.endswith(".jsonl"):
                continue
            fp = os.path.join(dirpath, f)
            try:
                mt = os.path.getmtime(fp)
            except OSError:
                continue
            if mt > best_mt:
                best, best_mt = fp, mt
    return best


def _human_turns(path, keep=40):
    """The last `keep` messages you actually typed, oldest first.

    Same three filters the engine uses (isMeta, SYS_PREFIX, INTERRUPT), for the
    same reason: a /ds anchored on a system-generated user line would point the
    backwalk at something you never wrote. A slash command invocation is
    already excluded by SYS_PREFIX's `command-name`, so /ds cannot anchor on
    itself."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "user" or d.get("isMeta"):
                    continue
                msg = d.get("message") or {}
                c = msg.get("content")
                if isinstance(c, str):
                    text = c
                else:
                    text = " ".join(b.get("text", "") for b in c or []
                                    if isinstance(b, dict)
                                    and b.get("type") == "text")
                text = (text or "").strip()
                if not text or SYS_PREFIX.match(text) or INTERRUPT.match(text):
                    continue
                out.append({"uuid": d.get("uuid"), "at": d.get("timestamp"),
                            "text": text[:200], "sid": d.get("sessionId"),
                            "cwd": d.get("cwd")})
                out = out[-keep:]
    except Exception as exc:
        log_error("markers: transcript read failed: %r" % (exc,))
    return out


def do_mark_desync(raw, transcript=None):
    """Record a desync you spotted, from `/ds [-N] <note>`.

    A retrospective marker, deliberately: it is ground truth and never a
    trigger. Two things a transcript cannot give up, and this can. One is a
    desync that produced no visible correction — every marker the model finds
    is one you wrote a correction for, so the silent ones leave no trace at
    all. The other is what the RIGHT answer was, which is what turns a marker
    into a candidate preventative action rather than a complaint.

    -N is how many of your messages back the divergence started, because the
    note lands at T and the backwalk needs T-n. Everything else is derived: a
    figure you would have to reconstruct is a figure that will be wrong.

    `transcript` overrides the newest-mtime guess. Needed to test this without
    writing a fake marker, and useful when the session you mean is not the one
    that last wrote to disk."""
    raw = (raw or "").strip()
    back = 0
    if raw:
        first, _, rest = raw.partition(" ")
        m = RE_BACK.match(first)
        if m:
            back, raw = int(m.group(1)), rest.strip()
    if not raw:
        print("usage: /ds [-N] <what went wrong, and what the right answer was>")
        print("  -N   how many of your messages back it started (default 0,")
        print("       meaning this turn). /ds -3 the container path assumption")
        print("a marker with no note is not evidence, so this is refused.")
        return 2

    path = transcript or live_transcript()
    if not path:
        print("no transcript found under ~/.claude/projects — nothing to anchor to")
        return 1
    turns = _human_turns(path)
    if not turns:
        print("no messages of yours found in %s" % path)
        return 1

    at = turns[-1]
    idx = max(0, len(turns) - 1 - back)
    frm = turns[idx]
    if back and idx == 0 and len(turns) - 1 < back:
        print("note: only %d message(s) of yours are in this transcript, so the "
              "anchor is the earliest one rather than %d back."
              % (len(turns), back))

    spanned = None
    try:
        spanned = round((_ts(at["at"]) - _ts(frm["at"])).total_seconds())
    except Exception:
        pass

    row = {
        "ts": _iso(_now()), "source": "slash-command",
        "session_id": at.get("sid"), "cwd": at.get("cwd"),
        # `back` is what you asked for; `spanned_msgs` is what the transcript
        # could actually give. They differ when the divergence predates the
        # messages on hand, and conflating them would overstate the span.
        "note": raw, "back": back,
        "at_uuid": at.get("uuid"), "at_ts": at.get("at"),
        "at_text": at.get("text"),
        "from_uuid": frm.get("uuid"), "from_ts": frm.get("at"),
        "from_text": frm.get("text"),
        "spanned_msgs": (len(turns) - 1) - idx, "spanned_s": spanned,
        "transcript": path,
    }
    _append_jsonl(MARKERS, [row], "markers")
    n = sum(1 for _ in open(MARKERS, errors="replace")) if os.path.exists(MARKERS) else 1
    actual = (len(turns) - 1) - idx
    span = ("%d msg(s), %s" % (actual, _human_span(spanned))) if actual else "this turn"
    print("desync recorded (%s) — %d in %s" % (span, n, MARKERS))
    return 0


def _human_span(secs):
    if secs is None:
        return "duration unknown"
    if secs < 90:
        return "%ds" % secs
    if secs < 5400:
        return "%dm" % round(secs / 60.0)
    return "%.1fh" % (secs / 3600.0)


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

def existing_labels():
    """{(rule, fire_key): verdict} from labels.jsonl, last row winning — the
    same precedence backtest.load_user_labels applies."""
    out = {}
    try:
        with open(LABELS, errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("verdict") in ("applies", "does-not-apply"):
                    out[(d.get("rule"), d.get("fire_key"))] = d["verdict"]
    except Exception:
        pass
    return out


def do_recent(limit=20, rule=None, session=None, todo_only=False):
    """List recent fires with their keys, ready to label.

    Needed because an `augment` fire is otherwise unlabellable in band: the
    verdict directive is only appended to apply/ask fires, `pending` only holds
    apply/ask, and `backtest.py --review` reads fires replayed from refined/, so
    a live augment fire is invisible to it until its session is captured and
    reduced. That left reading fires.jsonl by hand as the only route.

    Fire keys are content hashes, so a verdict recorded here binds to the same
    fire when the session is later replayed."""
    try:
        with open(FIRES, errors="replace") as fh:
            rows = []
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        print("no fires.jsonl yet")
        return 0

    labels = existing_labels()
    # A rule with no `fix` at all never says anything, so "did its concern
    # apply?" is not a question — R13 fires on every message by design and
    # would otherwise flood the todo list with unanswerable rows.
    try:
        import rules_engine as E
        unjudgeable = {r["id"] for r in E.load_catalogue()["rules"]
                       if not r.get("fix")}
    except Exception:
        unjudgeable = set()
    if rule:
        rows = [r for r in rows if r.get("rule") == rule]
    if session:
        rows = [r for r in rows if (r.get("session_id") or "").startswith(session)]
    # One row per cause: the newest observation of each (rule, fire_key). A
    # deduped repeat is the same thing to be judged, not another thing.
    by_cause = {}
    for r in rows:
        by_cause[(r.get("rule"), r.get("fire_key"))] = r
    rows = list(by_cause.values())
    def judgeable(r):
        return (r.get("rule") not in unjudgeable
                and (r.get("rule"), r.get("fire_key")) not in labels)

    if todo_only:
        rows = [r for r in rows if judgeable(r)]
    rows.sort(key=lambda r: r.get("ts") or "")
    rows = rows[-max(1, limit):]

    if not rows:
        print("nothing to show%s" % (" (nothing left to judge)"
                                     if todo_only else ""))
        return 0

    print("%-8s %-5s %-9s %-14s %-40s %s"
          % ("time", "rule", "action", "verdict", "fire_key", "detail"))
    print("-" * 108)
    todo = []
    for r in rows:
        rid, key = r.get("rule"), r.get("fire_key")
        verdict = labels.get((rid, key))
        if not verdict and rid in unjudgeable:
            verdict = "n/a"
        flags = "".join(c for c, on in (
            ("*", r.get("surfaced")), ("d", r.get("deduped")),
            ("s", bool(r.get("suppressed")))) if on)
        # fire_key is never truncated — it is the thing you copy.
        print("%-8s %-5s %-9s %-14s %-40s %s"
              % ((r.get("ts") or "")[11:19], rid,
                 (r.get("action") or "") + (" " + flags if flags else ""),
                 verdict or "-", key or "",
                 " ".join((r.get("detail") or r.get("why") or "").split())[:28]))
        if judgeable(r):
            todo.append(r)

    print("\nflags: * surfaced   d deduped   s suppressed"
          "   |   n/a = no fix template, nothing to judge")
    if todo:
        r = todo[-1]
        print("\n%d unlabelled. To record one:\n" % len(todo))
        print('  python3 "$HOME/.claude-resync/intercept.py" --label %s \\\n'
              '      --key %s \\\n'
              '      --session %s \\\n'
              '      --verdict applies|does-not-apply --note "why"'
              % (r.get("rule"), json.dumps(r.get("fire_key") or ""),
                 r.get("session_id") or "?"))
    return 0


def do_label(rule, key, verdict, note=None, session_id=None,
             synthetic=False):
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
        # A verdict on a message written to exercise the plumbing is not
        # evidence about the rule. Readers drop these rather than counting them.
        "synthetic": bool(synthetic),
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
                 if not is_ours(h.get("command"))]
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
            if is_ours(h.get("command"))]
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
    by_id = {r["id"]: r for r in cat["rules"]}
    logged = buckets.get("log", [])
    suspended = [r for r in logged if by_id.get(r, {}).get("suspended")]
    floored = [r for r in logged if r not in suspended]
    if floored:
        print("  log only : %s  (precision below the %.0f%% floor)"
              % (" ".join(floored), E.PRECISION_FLOOR * 100))
    if suspended:
        # Neutral wording on purpose: as of 2026-08-24 the four suspensions
        # have three different causes (falsified premise, a check that cannot
        # observe what it claims, an ask that returns nothing), so naming one
        # of them in the header would mislabel the others. Each rule's own
        # first sentence follows.
        print("  suspended: %s  (fires and logs, never surfaces)"
              % " ".join(suspended))
        for rid in suspended:
            why = (by_id.get(rid, {}).get("suspended_reason") or "").split(".")[0]
            if why:
                print("             %s: %s." % (rid, why))
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

    if os.path.exists(NORMALISE_LOG):
        import collections
        st, times, req_times, setup_times = collections.Counter(), [], [], []
        with open(NORMALISE_LOG, errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                st[d.get("status")] += 1
                if d.get("elapsed_ms"):
                    times.append(d["elapsed_ms"])
                if d.get("request_ms") is not None:
                    req_times.append(d["request_ms"])
                if d.get("setup_ms") is not None:
                    setup_times.append(d["setup_ms"])
        n = sum(st.values())
        print("normalise  : %d invocation(s) — %s"
              % (n, ", ".join("%s=%d" % kv for kv in st.most_common())))
        if n:
            print("             restated %.0f%% of messages"
                  % (100.0 * st.get("restated", 0) / n))
        def _band(label, xs, note=""):
            if not xs:
                return
            xs = sorted(xs)
            print("             %-8s median %.0fms, p95 %.0fms, max %.0fms%s"
                  % (label, xs[len(xs) // 2],
                     xs[int(len(xs) * 0.95)], xs[-1], note))

        # Total first for continuity with the older rows, then the split that
        # says where the time actually goes. Rows written before the split
        # have no request/setup fields, so those bands are simply shorter.
        _band("total", times, " — paid on EVERY message")
        _band("request", req_times)
        _band("setup", setup_times, " (SDK import + .env)")
        if times and not req_times:
            print("             split unavailable: every row predates it")
        print("             before/after: intercept.py --normalise-log")
    else:
        print("normalise  : no invocations yet")

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
    ap.add_argument("--synthetic", action="store_true",
                    help="with --label: this fire was on a message written to "
                         "test the plumbing, so exclude it from precision")
    ap.add_argument("--recent", nargs="?", type=int, const=20, default=None,
                    metavar="N",
                    help="list the last N fires (default 20) with their keys, "
                         "ready to label. Combine with --rule / --session / "
                         "--todo")
    ap.add_argument("--todo", action="store_true",
                    help="with --recent, show only fires with no verdict yet")
    ap.add_argument("--normalise-log", nargs="?", type=int, const=20,
                    default=None, metavar="N",
                    help="show the last N normalisations: what you typed "
                         "against how it was restated")
    ap.add_argument("--all", action="store_true",
                    help="with --normalise-log, include skipped invocations")
    ap.add_argument("--rule", default=None,
                    help="with --recent, restrict to this rule id")
    ap.add_argument("--transcript", default=None,
                    help="with --mark-desync: anchor to this transcript "
                         "instead of the most recently written one")
    ap.add_argument("--mark-desync", metavar="ARGS", default=None,
                    help="record a desync you spotted: the raw /ds arguments, "
                         "'[-N] <note>'. N is how many of your messages back "
                         "it started")
    args = ap.parse_args(argv)

    if args.mark_desync is not None:
        return do_mark_desync(args.mark_desync, args.transcript)
    if args.install:
        return do_install(remove=False)
    if args.uninstall:
        return do_install(remove=True)
    if args.status:
        return do_status()
    if args.normalise_log is not None:
        return do_normalise_log(args.normalise_log, not args.all)
    if args.recent is not None:
        return do_recent(args.recent, args.rule, args.session, args.todo)
    if args.label:
        return do_label(args.label, args.key, args.verdict, args.note,
                        args.session, args.synthetic)
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
