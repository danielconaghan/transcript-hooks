#!/usr/bin/env python3
"""The two prompts that read a session document, and the schemas that force it.

Two passes, and the split is forced by the API rather than chosen:

    findings + searched_how + a flag     4 categories max
    findings alone (what is used here)   6 categories max
    a flat list of sequence numbers     12 categories max
    boolean + searched_how              16 categories max

Measured against the API, not guessed — beyond those counts the request is
rejected outright with "the compiled grammar is too large". Sixteen categories
each carrying findings is not expressible in one call at any session size.

So the forcing device runs FIRST and ALONE over all sixteen. That device — a
required verdict per category, none optional — is what stopped A1 being
skipped, where a free findings list let it fire zero times in 33 units. It
survives at session scale: on `fecca80a` all sixteen `searched_how` came back
naming actual sequence numbers, and eleven of sixteen returned absent. It
compels a look without manufacturing a finding to justify the look.

Detail is bought afterwards, six categories at a time, only where triage said
something is there. The document is identical across both passes and is cached,
so the second pass pays a tenth for it.
"""

# Bumped by hand when a prompt changes MEANING, so a typo fix costs nothing.
#
# v3, 2026-08-26. The unit became the session. Every field the v2 prompts
# referred to — `evidence_before_it`, `developer_next`, `work_after_it`,
# `similar_to_earlier`, `its_own_evidence` — was a per-unit construct and is
# gone; the definitions now point at the document and its header index. Two
# v2 faults are also fixed: the `work_after_it` rule that made `cost`
# unreachable (zero of 27 findings rated cost, because the rule contradicted
# the shared definition and measured work AFTER the correction when the waste
# is always before it), and a dangling clause in E3 left by an earlier patch.
# v4, 2026-08-26. C4 was pointed at `rewritten_targets`, which counts repeated
# writes and structurally cannot see a deletion. On the two planted sessions
# containing a complete reversal that entry was EMPTY, and populated on the
# control that merely iterated — the signal inverted. The judge found the
# reversal in the records, checked the index, and talked itself back out of it
# in `searched_how`. C4 now points at `reversed_targets`, and the prompt says
# plainly that the index is an aid rather than an authority.
PROMPT_VERSION = "v4"

# The grammar ceiling, measured against the API rather than guessed. SIX is the
# edge, not a comfortable limit: eight is rejected, and there is zero headroom.
# Adding a FIELD to a finding will lower this, and it will fail at runtime after
# triage has already been paid for — so re-measure with the probe in the module
# docstring before changing `_FINDING`, and lower this number to match.
MAX_DETAIL_CATS = 6

CATS = {
    "A1": "instruction dropped",
    "A2": "misread",
    "B1": "acted on an unverified premise",
    "B2": "working from a stale fact",
    "B3": "post-compaction rework",
    "C1": "costly self-correction",
    "C2": "repeated failing action",
    "C3": "scope overrun",
    "C4": "work undone",
    "D1": "illegible progress",
    "D2": "asserted a state its own tool output contradicts",
    "D3": "not actionable",
    "E1": "asked what it could have determined itself",
    "E2": "did not ask when it should have",
    "E3": "the developer had to repeat themselves",
    "E4": "a fix did not land and the symptom was re-reported",
}

# --------------------------------------------------------------------------
# what the document is
# --------------------------------------------------------------------------

DOCUMENT = """\
You are reading ONE COMPLETE SESSION between a developer and an AI coding
assistant, and looking for inefficiency in their collaboration: effort wasted
because the two were working from different understandings.

The session is an ordered list of records. `who` is one of:
  developer  a message the developer typed
  assistant  something the assistant said
  action     a command it ran (`command` is the command line, not truncated
             at `&&` — a single entry often does several things, and the check
             you are looking for may be the second or third of them)
  result     what that command returned. `exit_ok` is the EXIT CODE ONLY —
             about half the commands here pipe or redirect, so a command that
             failed can still show `exit_ok: true`. READ THE TEXT, not the flag.
  question   an AskUserQuestion, with the options offered and the answer given
  denial     a tool use the developer refused
  marker     a note the developer recorded at the time

Nothing is withheld. Long results are trimmed in the MIDDLE and say so; a
record carrying "[... N characters omitted from the middle ...]" is a fragment
and you may not write confident absolutes over it.

The header carries an `index`. It ANNOTATES the records, it does not replace
them, and nothing was filtered out on its account:
  epochs             compaction boundaries. Everything after one was written
                     without what came before it.
  repeated_commands  the same command issued more than once, with the outcome
                     of each. Re-running a passing command is not a finding.
  rewritten_targets  files written more than once, with outcomes. This is
                     ITERATION — someone getting a file right. It is not the
                     same as work being undone.
  reversed_targets   writes that a later command removed or restored, with the
                     command that did it. THIS is work undone. `sweeping` marks
                     a reversal that named no path (`git reset --hard`) and so
                     undid whatever was outstanding.
  silent_gaps        stretches of five minutes or more with nothing said.
  markers            notes the developer recorded at the time.

A marker is ground truth about what the developer noticed. It is never a
trigger and never, by itself, a finding.\
"""

# --------------------------------------------------------------------------
# the sixteen
# --------------------------------------------------------------------------

DEFINITIONS = """\
A1  INSTRUCTION DROPPED
    A distinct request, question or constraint that the reply never addresses.
    This is an ABSENCE, and an absence is invisible unless you enumerate first.
    Take each developer message, break it into the separate things it asks for,
    and account for each one against the assistant's reply and the actions that
    followed. Do the enumeration before you decide.
    Go below clause level. Check the named specifics each request carries —
    file paths, command names, branch names, flags, counts — against what was
    actually done. A specific silently replaced with a different one is
    dropped: "branch client-summary" answered by branching something else is
    A1, even though a branch was created.
    A request explicitly deferred ("I'll do X after Y") is NOT dropped. A
    request the reply simply never mentions IS dropped, whether or not the
    developer noticed.

A2  MISREAD
    The assistant acted, but on a different reading of the request than the
    words support. Distinct from A1: something was done, just not the thing
    asked for. What it DID is better evidence than what it said it did.
    NOT A2: the assistant checking a factual claim in the developer's message
    and reporting that it does not hold. Flagging a discrepancy in their own
    wording is the behaviour we want, not a misreading.
    NOT A2: verbosity, or a reply the developer disagreed with.

B1  ACTED ON AN UNVERIFIED PREMISE
    It asserted as settled fact something the evidence in front of it does not
    establish, and then built on it. The question is not "was it wrong" — it is
    "did it check before asserting". A premise that turns out right but was
    never checked still counts, and `severity` is where you say it cost nothing.
    Read the `action` records before concluding nothing was checked, including
    ones from earlier turns: evidence gathered before this turn still counts.
    NOT B1: reasoning openly. If the claim's own closing sentence announces the
    verification — "let me confirm", "let me look at it" — it is thinking
    aloud, not asserting a settled premise.
    NOT B1: explaining how a language, framework or protocol works. That is
    general knowledge, not a premise about THIS system that it then built on.
    NOT B1: correcting itself. A retraction is not a new unverified premise.

B2  WORKING FROM A STALE FACT
    The world had changed and the assistant was still on the old version — a
    moved endpoint, a renamed field, a merged branch, a setting since altered.
    A developer correction in a later turn is the usual evidence.
    A developer changing their mind is not a stale fact, and neither is a
    disagreement about approach. The assistant inferring something wrong is B1,
    not B2 — **B2 needs the WORLD to have moved.**

B3  POST-COMPACTION REWORK
    Ground already covered being covered again because context was lost, not
    because anything changed. `index.epochs` gives the boundaries. If the
    session has a single epoch this is almost certainly absent.

C1  COSTLY SELF-CORRECTION
    It corrected itself, but only after spending something on the wrong thing.
    The cost is the finding, not the correction — and the cost is what happened
    BEFORE the correction, not after it. A correction caught before anything
    was spent is the system working: `severity: none` at most.

C2  REPEATED FAILING ACTION
    The same approach retried into the same wall. `index.repeated_commands`
    gives candidates with their outcomes; confirm or reject each. Check the
    failures are actually the same problem — a command that failed for three
    different reasons is not this, and retrying after changing something is
    ordinary work.

C3  SCOPE OVERRUN
    Substantial work with no request behind it, that the developer would not
    obviously want. Compare the actions against what was actually asked.
    Necessary prerequisites are not overrun; a brief aside clearly flagged as
    optional is not overrun. Installing 867 packages when asked to pull a
    branch is.

C4  WORK UNDONE
    Output written and then reversed. `index.reversed_targets` gives the
    candidates: each entry names a write and the later command that removed or
    restored it.
    Do NOT use `rewritten_targets` for this. It counts writes to the same path,
    which is iteration — a file being got right — and it cannot see a deletion
    at all. On a session containing a complete reversal it is typically EMPTY,
    because each file was written once and then removed by a shell command.
    An empty `rewritten_targets` is therefore no evidence of anything here.
    Read the records regardless of what either entry says. If you can see a
    write undone in the transcript, that is the finding, whatever the index
    lists — the index is an aid, not an authority.

D1  ILLEGIBLE PROGRESS
    After reading what the assistant said, a reasonable developer still could
    not say what state things are in, what was done, or what happens next.
    `index.silent_gaps` is strong evidence: a long stretch with nothing said,
    especially around a blocking command, is the commonest shape. A developer
    asking "is it stuck?" or "what's happening?" settles it.

D2  ASSERTED A STATE ITS OWN TOOL OUTPUT CONTRADICTS
    Two shapes, and the second is the one usually missed.
    CONTRADICTED: the reply claims something the results show to be untrue.
    "All tests pass" when the run reported failures. Note again that a command
    can exit cleanly and still report failures in what it printed.
    UNSUPPORTED: the reply reports a specific verification result and NO
    corresponding action appears anywhere. A real example from these sessions: a
    reply listing four checks — typecheck, tests, lint, build — where the build
    was never run, in that turn or anywhere in the session. Three real results
    and one invented, and nobody noticed. If a claimed result has no action
    behind it, that is D2. You have the whole session; check.
    The commonest false positive is a failure about something else — a turn
    holds many actions and one unrelated failure contradicts nothing. A failure
    the reply acknowledges is honest reporting. Ignore permission prompts,
    auto-mode blocks and tool-protocol errors entirely: those are the settings
    file talking, not the world.

D3  NOT ACTIONABLE
    The content is right but the developer cannot act on it — pitched at the
    wrong altitude, burying the answer, or ending with no clear result or next
    step. Length alone is not the test: in these sessions assistant turns run
    about 10.7x the developer's word count as a matter of course, so verbosity
    is the norm and cannot itself be the finding. If your reason reduces to
    "N words in response to a short request", you do not have D3.
    NOT D3: ending on a clarifying question before a destructive or
    irreversible action. That is correct behaviour.
    NOT D3: including a summary of what changed. That is the report owed.
    If D1 and D3 would rest on the same defect, report D1 only.

E1  ASKED WHAT IT COULD HAVE DETERMINED ITSELF
    A question put to the developer that the assistant could have answered from
    the repository, the files or a command. A round trip spent for nothing. An
    answer that overrides the question entirely — "just get on with it" — is
    strong evidence.

E2  DID NOT ASK WHEN IT SHOULD HAVE
    Committed to a consequential, genuinely ambiguous choice without asking and
    without flagging the assumption. A rejected question followed by "I'll make
    the calls myself" is the clearest case. Launching something long and
    blocking, on an assumption, is another.

E3  THE DEVELOPER HAD TO REPEAT THEMSELVES
    They asked or instructed the SAME THING AGAIN, in whatever words, because
    the first attempt did not land.
    NOT E3: a developer amending their own message seconds later, before the
    assistant has done anything. That is a correction to their own message, and
    in these sessions it is what near-verbatim repeats almost always are.
    The real cases are semantic re-asks in different words: the same question
    asked three times across half an hour, each time because the answer did not
    land. You have every turn in order — read them.

E4  A FIX DID NOT LAND AND THE SYMPTOM WAS RE-REPORTED
    The developer reports a problem that was previously claimed fixed. The test
    is whether the assistant had already said THIS SPECIFIC THING was resolved,
    so find the earlier claim before you file this.
    NOT E4: a developer reporting a NEW problem.
    NOT E4: a caveat the assistant FLAGGED IN ADVANCE which then materialised.
    It warned them, and they proceeded.\
"""

# --------------------------------------------------------------------------
# the shared system prompt — IDENTICAL across both passes
# --------------------------------------------------------------------------

# Prompt caching matches on a prefix of the whole request, system prompt
# included, so a pass-specific system prompt would miss the cache on the very
# call the cache exists for. The system prompt is therefore the same bytes in
# both passes, the document is the cached block, and the task text goes AFTER
# it in the user turn.
SYSTEM = DOCUMENT + "\n\n" + DEFINITIONS


# --------------------------------------------------------------------------
# pass one — the forcing device
# --------------------------------------------------------------------------

TRIAGE_TASK = """\
FOR EVERY ONE of the sixteen categories you were given you must return an entry.
This is not optional and you may not omit one. You are not being asked for
detail yet — only for a verdict on each, and an honest account of how you
reached it.

  searched_how       ONE SENTENCE saying what you actually looked at to decide.
                     Not what the category means — what you DID. "Read each
                     developer turn and checked the following assistant turn
                     for each named specific" is an answer. "Considered whether
                     this occurred" is not. If you did not really look, say so
                     here; that is more useful to us than a confident `false`.
  sufficient_evidence  false if the session as given cannot settle this
                     category. Say so rather than guessing — it is a real
                     answer and we would rather have it than a coin-flip.
  present            is it here at all.

Most categories are absent in most sessions, and returning absent for eleven of
sixteen is a normal result. Do not manufacture a finding to justify the look.\
"""


# --------------------------------------------------------------------------
# pass two — the detail, for what triage flagged
# --------------------------------------------------------------------------

_DETAIL_TASK = """\
You have already judged this session and found these categories to be present:
%s

Now give the findings for each. A category may carry more than one.

  at_seq         the `seq` where it is VISIBLE.
  began_seq      the `seq` where the divergence STARTED, or null if you cannot
                 locate it. **This is the most valuable field in the output.**
                 It is what a preventative measure would have to fire on, and
                 it is almost never the same place the problem became visible:
                 the developer's complaint is at the end, the decision that
                 caused it is further back. Findings in different categories
                 that share a `began_seq` are one event, not several — that is
                 a result, not a mistake, so give each its true origin rather
                 than spreading them out.
  quote          VERBATIM from the session, the span that shows it. Copy, do
                 not retype, and do not add formatting that is not there. If
                 you cannot produce a real quote, you do not have a finding.
  severity       cost      real waste followed: work redone, a wrong path
                           taken, the developer had to re-ask or correct, or a
                           round trip spent for nothing
                 friction  it made the exchange worse but nothing was redone
                 none      you noticed it but it cost nothing. Say `none`
                           rather than inflating it.
  noticed        did the developer raise it ANYWHERE in this session.
                 **False is the more valuable finding** — it means nothing
                 else can see this.
  what_happened  one sentence.

If, looking closely, a category you flagged does not survive, return an empty
findings list for it. Withdrawing is a real answer.\
"""


def detail_task(cats):
    return _DETAIL_TASK % "\n".join("  %s  %s" % (c, CATS[c])
                                    for c in sorted(cats))

# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------

_TRIAGE_CAT = {
    "type": "object",
    "properties": {"searched_how": {"type": "string"},
                   "sufficient_evidence": {"type": "boolean"},
                   "present": {"type": "boolean"}},
    "required": ["present", "searched_how", "sufficient_evidence"],
    "additionalProperties": False,
}

TRIAGE_SCHEMA = {"type": "object",
                 "properties": {c: _TRIAGE_CAT for c in CATS},
                 "required": sorted(CATS), "additionalProperties": False}

_FINDING = {
    "type": "object",
    "properties": {"at_seq": {"type": "integer"},
                   "began_seq": {"type": ["integer", "null"]},
                   "quote": {"type": "string"},
                   "severity": {"type": "string",
                                "enum": ["cost", "friction", "none"]},
                   "noticed": {"type": "boolean"},
                   "what_happened": {"type": "string"}},
    "required": ["at_seq", "began_seq", "noticed", "quote", "severity",
                 "what_happened"],
    "additionalProperties": False,
}

_DETAIL_CAT = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": _FINDING}},
    "required": ["findings"], "additionalProperties": False,
}


def detail_schema(cats):
    """Findings for up to MAX_DETAIL_CATS categories. More will not compile.

    Raised here rather than left to the API, because the API raises it after
    triage has been bought and the run is half paid for."""
    cats = list(cats)
    if not 0 < len(cats) <= MAX_DETAIL_CATS:
        raise ValueError("detail takes 1..%d categories, got %d"
                         % (MAX_DETAIL_CATS, len(cats)))
    return {"type": "object", "properties": {c: _DETAIL_CAT for c in cats},
            "required": sorted(cats), "additionalProperties": False}


def batches(cats):
    """Flagged categories, in calls of at most MAX_DETAIL_CATS."""
    cats = sorted(cats)
    return [cats[i:i + MAX_DETAIL_CATS]
            for i in range(0, len(cats), MAX_DETAIL_CATS)]


if __name__ == "__main__":
    import json
    import sys
    which = sys.argv[1] if len(sys.argv) > 1 else ""
    if which == "triage":
        print(SYSTEM + "\n\n=== task ===\n" + TRIAGE_TASK)
        print("\n--- schema ---\n" + json.dumps(TRIAGE_SCHEMA, indent=1)[:1200])
    elif which == "detail":
        print(SYSTEM + "\n\n=== task ===\n" + detail_task(["B1", "D1"]))
        print("\n--- schema (4 cats) ---\n"
              + json.dumps(detail_schema(["A1", "A2", "B1", "B2"]), indent=1)[:900])
    else:
        print("prompt version : %s" % PROMPT_VERSION)
        print("categories     : %d" % len(CATS))
        print("system (shared): %d chars, identical in both passes so the "
              "document caches" % len(SYSTEM))
        print("triage task    : %d chars, all %d categories in one call"
              % (len(TRIAGE_TASK), len(CATS)))
        print("detail task    : %d categories per call" % MAX_DETAIL_CATS)
        print("worst case     : 1 + %d calls per session"
              % len(batches(sorted(CATS))))
        print("\npython3 prompts.py triage|detail to read one in full")


# --------------------------------------------------------------------------
# split triage — one call per group instead of one call for all sixteen
# --------------------------------------------------------------------------

# Measured on the 48 planted mocks: all sixteen at once produced 152 findings
# for 32 planted faults, 81% off-target, with B1 appearing in 31 of 48 sessions
# where it was planted in 2. Eight-record fixtures do not contain that much.
# Every category is in scope for every record, so each finds something
# plausible — and B1 is easiest to reach, since almost any statement can be
# called insufficiently verified if you are looking for it.
#
# The five-view design restricted SCOPE per call, and that was doing more than
# it was credited with when collapsed into one pass. This keeps the session as
# the unit and narrows only the question asked of it.
GROUPS = {"A": ["A1", "A2"], "B": ["B1", "B2", "B3"],
          "C": ["C1", "C2", "C3", "C4"], "D": ["D1", "D2", "D3"],
          "E": ["E1", "E2", "E3", "E4"]}


def _definition_blocks():
    """DEFINITIONS, cut into one block per category."""
    out, cur, key = {}, [], None
    for line in DEFINITIONS.splitlines():
        if len(line) > 4 and line[:2] in CATS and line[2:4] == "  ":
            if key:
                out[key] = "\n".join(cur).rstrip()
            key, cur = line[:2], [line]
        elif key:
            cur.append(line)
    if key:
        out[key] = "\n".join(cur).rstrip()
    return out


BLOCKS = _definition_blocks()


def definitions_for(cats):
    return "\n\n".join(BLOCKS[c] for c in cats if c in BLOCKS)


def system_for(cats):
    """The shared document description plus ONLY these categories."""
    return DOCUMENT + "\n\n" + definitions_for(cats)


def triage_task_for(cats):
    n = {2: "two", 3: "three", 4: "four"}.get(len(cats), str(len(cats)))
    return (TRIAGE_TASK
            .replace("FOR EVERY ONE of the sixteen categories you were given",
                     "FOR EVERY ONE of the %s categories you were given" % n)
            .replace("returning absent for eleven of\nsixteen is a normal "
                     "result", "returning absent for all of\nthem is a normal "
                     "result"))


def triage_schema_for(cats):
    return {"type": "object", "properties": {c: _TRIAGE_CAT for c in cats},
            "required": sorted(cats), "additionalProperties": False}
