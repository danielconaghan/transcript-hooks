#!/usr/bin/env python3
"""Shared rule engine for the pre-send interceptor.

One implementation of the rules in `rules.json`, with two callers:

    backtest.py   builds PreSendState by replaying a historical session from
                  refined/, message by message   -> measures precision
    intercept.py  builds PreSendState from the UserPromptSubmit payload plus
                  a tail of the live transcript  -> runs for real

The point of sharing is that a precision figure has to describe the code that
actually runs. If the backtest reimplemented the triggers, "R01 at 89%" would
be a statement about a script nobody uses.

The load-bearing design constraint
----------------------------------

`evaluate()` takes a PreSendState and nothing else, and PreSendState contains
only what was knowable *before* the message was sent. A rule therefore cannot
accidentally consult the future — not by discipline, but because the future is
not in scope. Everything that needs hindsight (did the user later correct
this?) is labelling, lives in backtest.py, and never runs at runtime.

That split is why the runtime and historical numbers are comparable at all.

Fire identity
-------------

Each fire carries a `key` that is stable for the thing that caused it, not for
the message that surfaced it. A retracted draft is one fire however many
messages follow it, so the interceptor can surface it once and the backtest can
count it once.

`intercept.py` persists seen keys per session in `intercept-cache/<sid>.json`;
backtest.py dedupes within its replay. Both ask `dedupes()` which causes to
collapse, so the runtime and the measurement cannot disagree about what "one
fire" means — the same drift that made the two diverge on notification
handling once already.
"""

import hashlib
import json
import os
import re
import difflib

def _rules_path():
    """rules.json sits beside this file in both layouts — repo root, and the
    deployed ~/.claude-resync — so co-location is the primary lookup. The home
    fallback covers a partial deployment rather than failing at import time."""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")
    if os.path.exists(here):
        return here
    home = (os.environ.get("CLAUDE_RESYNC_HOME")
            or os.path.join(os.path.expanduser("~"), ".claude-resync"))
    return os.path.join(home, "rules.json")


RULES_PATH = _rules_path()

# --------------------------------------------------------------------------
# extractors — the reference-resolution rules all key off these
# --------------------------------------------------------------------------

RE_URL = re.compile(r"https?://[\w.\-]+(?::\d+)?[\w./\-#?=&%]*")
RE_PATH = re.compile(r"(?:\.\./|\./|/Users/|~/)[\w./\-\[\]]+")
RE_REPO = re.compile(r"github\.com/[\w.\-]+/[\w.\-]+")
RE_ENDPOINT = re.compile(
    r"/(?:clients?|cases?|users?|oauth|authorization|instructions|answers|hash)"
    r"[\w{}/\-]*")
RE_NO_CRITERION = re.compile(
    r"as (?:clean|good|abstract|nice)\b.{0,40}as possible"
    r"|as many times as it takes|make it (?:nice|good|better)"
    r"|do it all over again", re.I)
RE_ABSOLUTE = re.compile(
    r"\b(?:no|never|only|don'?t|must not)\b.{0,40}"
    r"\b(?:more|screen|branching|notification|report|option)s?\b", re.I)
RE_NEGATION = re.compile(
    r"\b(?:shouldn'?t|should not|isn'?t|is not|don'?t|do not|no longer|never"
    r"|not)\b", re.I)
RE_STATE_QUESTION = re.compile(
    r"\b(?:still (?:running|going)|stuck|hung|frozen|finished yet"
    r"|have you (?:stopped|finished)|is it (?:done|working|running)"
    r"|taking (?:this|so) long|any progress|what'?s happening)\b", re.I)

STOP = frozenset("""the a an and or of to in for on at is are be this that it
with from you we i as by not but if then so please can could would should
we're its it's have has had do does did will just now new one two""".split())


def tokens(s):
    return set(t for t in re.findall(r"[a-zA-Z_][\w\-]{3,}", (s or "").lower())
               if t not in STOP)


def _sha(s):
    return hashlib.sha1((s or "").encode("utf-8", "replace")).hexdigest()[:12]


# --------------------------------------------------------------------------
# the state a rule is allowed to see
# --------------------------------------------------------------------------

class PreSendState(object):
    """Everything knowable at the instant a message is about to be sent.

    Deliberately a plain container with no I/O: both callers build it from
    different sources, and neither the shape nor the rules care which.

    prompt          the outgoing message text
    at              its timestamp (datetime, or None)
    session_id/cwd  from the hook payload, or the session being replayed
    prior_msgs      earlier human messages, oldest first, each
                    {"id","at","text","toks","prompt_source"}
    queue_ops       {"op","at","content","human"} — enqueue/remove/dequeue
    questions       {"id","at","outcome"} — outcome in answered/clarify/
                    user-rejected/permission-rule/...
    launches        {tool_use_id: at} background command launches
    notified        {tool_use_id: at} task notifications received
    api_errors      [at] assistant API failures
    denials         {"kind","at"} tool denials seen so far
    resolver        optional object with .exists(path) / .repo_has(repo, needle)
                    / .url_serves(url); None means those checks are skipped
                    rather than guessed
    """

    def __init__(self, prompt, at=None, session_id=None, cwd=None,
                 prior_msgs=None, queue_ops=None, questions=None,
                 launches=None, notified=None, api_errors=None, denials=None,
                 resolver=None):
        self.prompt = prompt or ""
        self.at = at
        self.session_id = session_id
        self.cwd = cwd
        self.prior_msgs = prior_msgs or []
        self.queue_ops = queue_ops or []
        self.questions = questions or []
        self.launches = launches or {}
        self.notified = notified or {}
        self.api_errors = api_errors or []
        self.denials = denials or []
        self.resolver = resolver

    @property
    def toks(self):
        if not hasattr(self, "_toks"):
            self._toks = tokens(self.prompt)
        return self._toks


class Fire(object):
    """One rule firing. `key` identifies the *cause*, so the same underlying
    problem is not re-surfaced on every subsequent message."""

    def __init__(self, rule, key, why, detail="", subject=None, fix=None):
        self.rule = rule
        self.key = key
        self.why = why
        self.detail = detail
        self.subject = subject or set()   # tokens, for hindsight labelling
        self.fix = fix                    # text to inject, when the rule has one

    def as_dict(self):
        return {"rule": self.rule, "key": self.key, "why": self.why,
                "detail": self.detail, "subject": sorted(self.subject),
                "fix": self.fix}

    def __repr__(self):
        return "<Fire %s %s>" % (self.rule, self.key)


# --------------------------------------------------------------------------
# helpers shared by several rules
# --------------------------------------------------------------------------

def _unresolved_retractions(state):
    """Human drafts removed from the queue whose content never appears as a
    sent message. Compared against messages in BOTH directions: a removal that
    matches an earlier send is the queue tidying up after itself, which is the
    check that stopped 4 of 37 historical fires being counted as withheld."""
    out = []
    for op in state.queue_ops:
        if op.get("op") != "remove" or not op.get("human"):
            continue
        content = op.get("content") or ""
        if len(content) < 20:
            continue
        best = 0.0
        for m in state.prior_msgs:
            r = difflib.SequenceMatcher(
                None, content[:400], (m.get("text") or "")[:400]).ratio()
            best = max(best, r)
        # Also compare against the message being sent right now: if this is a
        # reworded resend, there is nothing to surface.
        best = max(best, difflib.SequenceMatcher(
            None, content[:400], state.prompt[:400]).ratio())
        if best < 0.55:
            out.append(op)
    return out


def _stale_tasks(state):
    """Background launches with no notification yet. The assistant's belief
    about these is unverifiable from its own context, which is what makes it
    assert progress it cannot see."""
    stale = []
    for tid, when in (state.launches or {}).items():
        if state.at and when and when >= state.at:
            continue
        n = state.notified.get(tid)
        if n is None or (state.at and n > state.at):
            stale.append(tid)
    return stale


def _open_questions(state):
    """Questions whose outcome was not an answer. Ordered oldest first."""
    return [q for q in state.questions
            if q.get("outcome") and q["outcome"] != "answered"]


# --------------------------------------------------------------------------
# rules. each takes PreSendState, returns [Fire]
# --------------------------------------------------------------------------

def r01_withheld_draft(state):
    fires = []
    for op in _unresolved_retractions(state):
        content = op["content"]
        fires.append(Fire(
            "R01", "retraction:" + _sha(content),
            "a draft was written and withdrawn, and its content was never sent",
            detail=re.sub(r"\s+", " ", content)[:200],
            subject=tokens(content)))
    return fires


def r02_stale_task_state(state):
    stale = _stale_tasks(state)
    if not stale:
        return []
    return [Fire(
        "R02", "tasks:" + ",".join(sorted(stale)),
        "%d background task(s) launched with no completion notification"
        % len(stale),
        detail=", ".join(sorted(stale)))]


def r03_permission_blocked(state):
    """Fires once per session, on the first message. The injection is a static
    declaration of what is blocked; repeating it every turn is noise."""
    if state.prior_msgs:
        return []
    return [Fire("R03", "session:" + (state.session_id or "?"),
                 "declare blocked operation classes up front",
                 detail="permission_mode-dependent")]


def r04_unanswered_question(state):
    fires = []
    for q in _open_questions(state):
        fires.append(Fire(
            "R04", "question:" + str(q.get("id")),
            "a decision question was %s and never answered" % q.get("outcome"),
            detail=str(q.get("id"))))
    return fires


def r05_stale_api_surface(state):
    found = sorted(set(RE_ENDPOINT.findall(state.prompt)))
    if not found:
        return []
    subj = set()
    for f in found:
        subj |= tokens(f)
    return [Fire("R05", "endpoints:" + _sha("|".join(found)),
                 "message names API path(s) that may have moved",
                 detail=", ".join(found)[:200], subject=subj)]


def r06_unresolved_reference(state):
    refs = sorted(set(RE_PATH.findall(state.prompt)
                      + RE_REPO.findall(state.prompt)))
    if not refs:
        return []
    # The concern is "this reference does not resolve", so the resolver decides
    # whether to FIRE, not merely how to word it.
    #
    # It used to fire on any message containing a path and consult the resolver
    # only for the `why`. That made it a path-mention detector: a reference that
    # existed and resolved perfectly still fired, 59 times across the corpus,
    # and its measured precision described nothing. It also meant the backtest —
    # which passes resolver=None because the world of that day is gone — could
    # never evaluate the check at all.
    #
    # Consequence, accepted deliberately: with no resolver there is no fire, so
    # R06 has no historical fires and is measurable only from live use. Nothing
    # is lost, because the historical fires were not measuring the rule.
    if state.resolver is None:
        return []
    missing = []
    for r in refs:
        try:
            if not state.resolver.exists(r):
                missing.append(r)
        except Exception:
            pass
    if not missing:
        return []
    subj = set()
    for r in missing:
        subj |= tokens(r)
    return [Fire("R06", "refs:" + _sha("|".join(missing)),
                 "reference(s) do not resolve: %s" % ", ".join(missing),
                 detail=", ".join(missing)[:200], subject=subj)]


def r07_near_duplicate(state):
    if not state.prior_msgs or len(state.prompt) < 25:
        return []
    prev = state.prior_msgs[-1]
    prev_text = prev.get("text") or ""
    if len(prev_text) < 25:
        return []
    if state.at and prev.get("at"):
        if (state.at - prev["at"]).total_seconds() > 120:
            return []
    ratio = difflib.SequenceMatcher(
        None, prev_text[:600], state.prompt[:600]).ratio()
    if ratio < 0.85:
        return []
    flip = (len(RE_NEGATION.findall(prev_text))
            != len(RE_NEGATION.findall(state.prompt)))
    return [Fire(
        "R07", "dup:" + _sha(prev_text) + ":" + _sha(state.prompt),
        ("this revises the previous message and flips a negation — both "
         "versions will be in context" if flip else
         "near-duplicate of the previous message; both will be in context"),
        detail="similarity %.2f%s" % (ratio, ", negation differs" if flip
                                      else ""),
        subject=tokens(state.prompt))]


def r08_unreachable_url(state):
    urls = sorted(set(RE_URL.findall(state.prompt)))
    if not urls:
        return []
    # Same change as R06: the resolver decides whether to fire. Previously any
    # message containing a URL fired, 52 times across the corpus, and for a
    # public host the injection could only ever say "reachability unchecked" —
    # a guaranteed fire carrying no information. Only a local dev address that
    # is genuinely not listening is worth saying anything about.
    if state.resolver is None:
        return []
    dead = []
    for u in urls:
        try:
            if state.resolver.url_serves(u) is False:
                dead.append(u)
        except Exception:
            pass
    if not dead:
        return []
    return [Fire("R08", "urls:" + _sha("|".join(dead)),
                 "url(s) not serving: %s" % ", ".join(dead),
                 detail=", ".join(dead)[:200], subject=tokens(" ".join(dead)))]


def r09_unverified_premise(state):
    """Deliberately unimplemented. The catalogue records this rule at n=1 with
    a trigger that needs claim extraction rather than a regex; shipping a
    guess would put unmeasurable fires into the label stream and corrupt every
    other rule's denominator."""
    return []


def r10_no_acceptance_criterion(state):
    if not RE_NO_CRITERION.search(state.prompt):
        return []
    return [Fire("R10", "criterion:" + _sha(state.prompt),
                 "open-ended instruction with no stated definition of done",
                 detail=re.sub(r"\s+", " ", state.prompt)[:200],
                 subject=state.toks)]


def r11_absolute_scope(state):
    if not RE_ABSOLUTE.search(state.prompt):
        return []
    return [Fire("R11", "scope:" + _sha(state.prompt),
                 "absolute scope claim — fixed principle, or just not now?",
                 detail=re.sub(r"\s+", " ", state.prompt)[:200],
                 subject=state.toks)]


def r12_dequeue_into_error(state):
    """A queued message delivered while the API was failing was lost. Keyed on
    the dequeue, so it surfaces once."""
    fires = []
    for op in state.queue_ops:
        if op.get("op") != "dequeue" or not op.get("at"):
            continue
        near = [e for e in state.api_errors
                if e and abs((e - op["at"]).total_seconds()) <= 90]
        if near:
            fires.append(Fire(
                "R12", "lost:" + op["at"].isoformat(),
                "a queued message was delivered during an API failure and may "
                "never have arrived",
                detail="dequeue at %s" % op["at"].isoformat()))
    return fires


def r13_provenance(state):
    """Annotate how this message came to be. Fires on every message by design;
    it changes no behaviour and exists so the other rules can weight a typed
    instruction differently from an accepted suggestion."""
    return [Fire("R13", "prov:" + _sha(state.prompt),
                 "record message provenance",
                 detail="prompt_chars=%d" % len(state.prompt))]


RULES = {
    "R01": r01_withheld_draft,
    "R02": r02_stale_task_state,
    "R03": r03_permission_blocked,
    "R04": r04_unanswered_question,
    "R05": r05_stale_api_surface,
    "R06": r06_unresolved_reference,
    "R07": r07_near_duplicate,
    "R08": r08_unreachable_url,
    "R09": r09_unverified_premise,
    "R10": r10_no_acceptance_criterion,
    "R11": r11_absolute_scope,
    "R12": r12_dequeue_into_error,
    "R13": r13_provenance,
}


def load_catalogue(path=None):
    with open(path or RULES_PATH) as fh:
        return json.load(fh)


def evaluate(state, only=None, catalogue=None):
    """Run the rules over one pre-send state. Returns [Fire], rule order.

    `only` restricts to a set of rule ids — used by intercept.py to run the
    non-interrupting rules first while the interrupting ones are still being
    measured. A rule raising is swallowed: one broken trigger must never stop
    the others, and at runtime must never block the prompt."""
    fires = []
    for rid in sorted(RULES):
        if only is not None and rid not in only:
            continue
        try:
            fires.extend(RULES[rid](state) or [])
        except Exception:
            continue
    return fires


# Precision at or above this is trusted enough to act on without asking.
PRECISION_CEILING = 0.8

# Precision KNOWN to be below this is too wrong to spend attention on. Such a
# rule still fires and still logs — it just never reaches the user.
#
# This floor is the build-time half of the self-tuning rule in PLAN.md ("a rule
# reaching ~10 labels at under 30% precision should be demoted from ask to
# log-only"). Waiting for ten labels means ten interruptions that are ~97%
# noise for a rule the backtest has already measured at 0.038, and the
# catalogue's own budget_finding says a service that asks too often "will be
# disabled within a day". A rule measured below the floor starts demoted and
# earns its way up, rather than annoying its way down.
#
# `None` is not "below the floor" — an unmeasured rule asks, because asking is
# how it acquires the labels that measure it.
PRECISION_FLOOR = 0.30

# Which labelling bases may drive a behaviour change at all.
#
# This gate exists because precision is not one kind of number. backtest.py
# records HOW each fire was judged, and most of those ways cannot support a
# routing decision:
#
#   auto          an independent fact in the data. Trustworthy.
#   auto-proxy    "a weak stand-in for the real question, flagged as such" —
#                 R04's label is literally "session continued N messages
#                 without the answer (proxy, not topic resolution)". Almost
#                 every session continues, so 86% measures sessions being
#                 normal, not the rule being right.
#   hindsight     "conservative by design, so it under-counts" — a miss returns
#                 UNLABELLED, never refuted. R06 reads 0% on 57 fires with ZERO
#                 refutations: that is "we could not prove it right", not "it
#                 was wrong 57 times". Demoting on it inverts the metric.
#   tautological / manual / none
#                 no automatic label exists.
#
# A hand label is always trusted: it is the one judgement made by someone who
# knew what the message meant.
TRUSTED_BASIS = ("auto",)


def action_for(rule_id, catalogue, precision_threshold=PRECISION_CEILING,
               precision_floor=PRECISION_FLOOR):
    """What the interceptor should do with a fire, derived from the catalogue's
    `action` plus measured precision — not from a second hand-set field.

    Returns one of: "augment" (inject, no interruption), "ask" (inject a
    directive to raise it), "apply" (an interrupting rule proven accurate
    enough to just act), "log" (fires and is recorded, never surfaced),
    "skip".
    """
    rule = next((r for r in catalogue["rules"] if r["id"] == rule_id), None)
    if rule is None:
        return "skip"
    if rule.get("suspended"):
        # A rule whose premise has been falsified must stop reaching the user
        # without anyone inventing a precision figure to demote it with. It
        # keeps firing and keeps being recorded, so the fires stay available to
        # hand-review and the counts stay comparable to the day it is fixed.
        # See the rule's `suspended_reason`.
        return "log"
    act = rule.get("action")
    if act in ("augment", "annotate", "resend"):
        return "augment"
    metrics = rule.get("metrics") or {}
    bt = metrics.get("backtest") or {}
    prec = metrics.get("precision")
    trusted = (bt.get("basis") in TRUSTED_BASIS
               or (bt.get("hand_labelled") or 0) > 0)
    if prec is not None and trusted:
        if prec >= precision_threshold:
            return "apply"
        # Demote only on actual counter-evidence. A rule with no refutations has
        # nothing said against it; a low floor there means "unproven", and
        # unproven is what `ask` is for.
        if prec < precision_floor and (bt.get("refuted") or 0) > 0:
            return "log"
    return "ask"


def dedupes(rule_id, catalogue):
    """Whether a cause should be surfaced only once per session.

    Keyed off the action, not the rule, because the two kinds of fire mean
    different things. An interrupting rule must surface a given cause ONCE — a
    retracted draft asked about on every subsequent message is a nag. A silent
    augment re-states current facts every turn, because that is what makes them
    current. Counting them the same way would either inflate the ask rules or
    understate the augment rules' true fire rate.

    A `log` rule dedupes too: it is an interrupting rule serving a suspended
    sentence, and its fire counts have to stay comparable to the day it is
    promoted."""
    return action_for(rule_id, catalogue) != "augment"

# --------------------------------------------------------------------------
# transcript ingestion — shared, so the two callers cannot diverge
# --------------------------------------------------------------------------
#
# backtest.py and intercept.py read the same transcript line format, one from
# refined/ and one from the live file. Building state separately in each is how
# they drift: the first version of intercept.py discarded every `attachment`
# record as a recursion guard, which silently threw away task notifications
# (they arrive as attachment.type == "queued_command") and made every completed
# background task look unreported. R02 fired on messages it had no business
# firing on, and only the hook was wrong — the backtest still said 75.
#
# So ingestion lives here with the triggers. The accumulator is a plain dict of
# JSON-safe values so intercept.py can cache it verbatim between invocations.

# Our own injections. Skipping exactly these two is the recursion guard: an
# injection must never become part of the state that produces the next one.
OUR_ATTACHMENTS = frozenset({"hook_additional_context", "hook_system_message"})

RE_TOOL_USE_ID = re.compile(r"<tool-use-id>(.*?)</tool-use-id>")
_SYS_PREFIX = re.compile(
    r"^\s*<(task-notification|bash-|local-command|system-reminder|command-name)")
_INTERRUPT = re.compile(r"^\[Request interrupted")


# A typed message is essentially never this long; the pathological cases are
# machine-generated. Nothing downstream reads beyond a few hundred characters
# (R01 compares 400, R07 compares 600), so storing more is pure weight on a
# file that is re-read and re-written on every prompt.
MSG_MAX_CHARS = 8000

# queue_ops accumulates for the whole session and every entry carries its full
# content. Bounded for the same reason msgs is.
MAX_QUEUE_OPS = 400


def new_accumulator():
    return {"msgs": [], "queue_ops": [], "questions": [], "launches": {},
            "notified": {}, "api_errors": [], "denials": [], "pending_q": {}}


def _text_of(msg):
    c = (msg or {}).get("content")
    if isinstance(c, str):
        return c
    return " ".join(x.get("text", "") for x in c or []
                    if isinstance(x, dict) and x.get("type") == "text")


def _note_notification(acc, text, when):
    """A task notification marks its launch as reported. Returns True if the
    text was a notification and should not be treated as a human message."""
    if not text.strip().startswith("<task-notification>"):
        return False
    m = RE_TOOL_USE_ID.search(text)
    if m:
        acc["notified"][m.group(1)] = when
    return True


def ingest_line(acc, d):
    """Fold one transcript line into the accumulator. Order matters; callers
    must feed lines oldest-first."""
    t = d.get("type")
    when = d.get("timestamp")

    if t == "attachment":
        a = d.get("attachment") or {}
        kind = a.get("type")
        if kind in OUR_ATTACHMENTS:
            return                      # recursion guard, precisely scoped
        if kind == "queued_command":
            # Written when something is ENQUEUED, not when it is delivered.
            # Verified: the a0c27fd2 draft the user withdrew at 18:58:59 has an
            # enqueue, a remove, AND a queued_command attachment, all in the
            # same second. So counting these as sent messages makes every
            # retracted draft look delivered and silently zeroes R01 — which is
            # exactly what happened on the first attempt.
            #
            # Delivered human messages already arrive as `type: user` lines, so
            # the only thing worth taking from here is a task notification,
            # which has no other representation.
            _note_notification(acc, a.get("prompt") or "",
                               a.get("timestamp") or when)
        elif kind == "command_permissions":
            acc["allowed_tools"] = a.get("allowedTools") or []
        return

    if t == "queue-operation":
        content = d.get("content")
        content = content if isinstance(content, str) else ""
        acc["queue_ops"].append({
            "op": d.get("operation"), "at": when,
            "content": content[:MSG_MAX_CHARS],
            "human": bool(content) and not _SYS_PREFIX.match(content)})
        if len(acc["queue_ops"]) > MAX_QUEUE_OPS:
            del acc["queue_ops"][:-MAX_QUEUE_OPS]
        return

    if t == "assistant":
        if d.get("apiErrorStatus"):
            acc["api_errors"].append(when)
        for b in (d.get("message") or {}).get("content") or []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            inp = b.get("input") or {}
            if b.get("name") == "Bash" and inp.get("run_in_background"):
                acc["launches"][b["id"]] = when
            if b.get("name") == "AskUserQuestion":
                q = {"id": b["id"], "at": when, "outcome": "answered"}
                acc["questions"].append(q)
                acc["pending_q"][b["id"]] = b["id"]
        return

    if t == "user":
        # `isMeta` marks a user-role line the platform generated rather than
        # one you typed: skill bodies, tool-companion output, injected notices.
        # They arrive with role "user" and arbitrary text, so nothing in the
        # text itself reliably identifies them. Measured in session 0825c6b6: a
        # single isMeta line held 94,691 of the 96,984 characters stored as
        # "your messages" — 97.6% of the corpus the similarity rules compare
        # against, and a 95KB string re-tokenised on every prompt.
        if d.get("isMeta"):
            return
        msg = d.get("message") or {}
        c = msg.get("content")
        text = (_text_of(msg) or "").strip()
        if d.get("toolDenialKind"):
            acc["denials"].append({"kind": d["toolDenialKind"], "at": when})
            for b in c if isinstance(c, list) else []:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                tuid = b.get("tool_use_id")
                if tuid in acc["pending_q"]:
                    for q in acc["questions"]:
                        if q.get("id") == tuid:
                            q["outcome"] = ("clarify" if d.get("userFeedback")
                                            else d["toolDenialKind"])
        if _note_notification(acc, text, when):
            return
        if not text or _SYS_PREFIX.match(text) or _INTERRUPT.match(text):
            return
        acc["msgs"].append({"at": when, "text": text[:MSG_MAX_CHARS]})


def state_from_accumulator(acc, prompt, at, session_id=None, cwd=None,
                           resolver=None, parse_ts=None, max_prior=None):
    """Turn an accumulator into a PreSendState. `parse_ts` converts the stored
    timestamp representation (ISO strings from a cache, or already-parsed
    datetimes) into datetimes; identity by default."""
    pt = parse_ts or (lambda x: x)
    msgs = acc["msgs"][-max_prior:] if max_prior else acc["msgs"]
    return PreSendState(
        prompt=prompt, at=at, session_id=session_id, cwd=cwd,
        prior_msgs=[{"id": m.get("id"), "at": pt(m.get("at")),
                     "text": m.get("text") or "",
                     "toks": tokens(m.get("text") or "")} for m in msgs],
        queue_ops=[{"op": q.get("op"), "at": pt(q.get("at")),
                    "content": q.get("content"), "human": q.get("human")}
                   for q in acc["queue_ops"]],
        questions=[dict(q, at=pt(q.get("at"))) for q in acc["questions"]],
        launches={k: pt(v) for k, v in acc["launches"].items()},
        notified={k: pt(v) for k, v in acc["notified"].items()},
        api_errors=[pt(a) for a in acc["api_errors"]],
        denials=[dict(x, at=pt(x.get("at"))) for x in acc["denials"]],
        resolver=resolver)
