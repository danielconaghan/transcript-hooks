#!/usr/bin/env python3
"""Strip markdown decoration from claim text, on the way into a model payload.

The first deliberately **lossy** step in the pipeline:

    reduce.py     lossless archive of the whole corpus
    session.py    reads it; format facts only, drops nothing
    plaintext.py  lossy — removes formatting characters, keeps every word
    pairs.py / chat.py   build a payload and make the call

It is a separate module for the same reason `session.py` reads rather than
rewrites: the faithful record has other readers, and a stripped archive could
not be un-stripped. Anything building a model payload calls `strip_decoration`
on the way out; anything wanting the real thing reads `refined/` and does not.
Lossiness belongs at the edge that leaves for the model, never upstream.

What lossy means here, precisely
--------------------------------

This removes formatting characters and normalises six decorative glyphs. It
never rewords, summarises, reorders or truncates, and it never touches a code
fence or a backtick span — the delimiters stay too, see `_protect`. Where a case is ambiguous it does
nothing — the function is conservative to a fault, because the text it runs on
is evidence and the ledger quotes from it verbatim.

Idempotency, written in rather than tested for
----------------------------------------------

`strip_decoration(strip_decoration(x)) == strip_decoration(x)` for any input,
which is what makes it safe to apply anywhere in a pipeline without tracking
where it has already run. Three constructs need care to get that for free
rather than by luck, and each is handled by matching the *repeated* form in one
pass instead of peeling one layer per call:

  * `***bold italic***` — stripping `**` first would leave `*text*` for a
    second pass to strip again. The emphasis pattern takes 1-3 markers at once.
  * `- - nested` — a marker regex of `^\\s*[-*+]\\s+` leaves `- nested`, which
    matches again. The list pattern consumes every leading marker in one go.
  * `> > quoted` and `# # heading` — same shape, same fix.

Protecting code by removing it first
------------------------------------

Fenced blocks and inline spans are lifted out whole — backticks and all — and
replaced with placeholders before any other rule runs, then put back at the
end. Nothing in between can see them, so "unchanged byte for byte" is a
property of the structure rather than a promise each rule has to keep. It also
means a `---` inside a code block is not mistaken for a horizontal rule, and
indentation inside a fence survives the whitespace collapsing that would
otherwise flatten it.

Keeping the delimiters is the one place this departs from the brief, and
`_protect` documents why: idempotency is unobtainable without a marker that
survives into the output, and the brief's own suggested marker is heavier than
the backtick it would replace.

Usage:
    python3 plaintext.py --self-test            # run the unit tests
    python3 plaintext.py --measure              # what it saves on the corpus
    python3 plaintext.py --show <session>       # before/after on real claims
"""

import argparse
import re
import sys

# --------------------------------------------------------------------------
# protection
# --------------------------------------------------------------------------

# A sentinel no rule below can match: NUL-delimited, no markdown characters,
# no spaces, so whitespace collapsing and trailing-strip leave it alone.
_SENTINEL = "\x00%d\x00"
_SENTINEL_RE = re.compile(r"\x00(\d+)\x00")

# A fenced block, delimiters included. `\Z` closes an unterminated fence at
# end of text rather than letting the rest of the claim leak out of protection.
RE_FENCE = re.compile(r"^[ \t]*```[^\n]*\n.*?(?:^[ \t]*```[^\n]*$|\Z)",
                      re.S | re.M)
# An inline span, delimiters included. One or more backticks, closed by a run
# of the same length.
RE_INLINE = re.compile(r"(?<!`)(`+)(?!`)(?:.+?)(?<!`)\1(?!`)", re.S)


def _protect(text):
    """Lift code spans out whole — DELIMITERS INCLUDED — leaving placeholders.

    Keeping the backticks is a deviation from the brief, and it is what buys
    idempotency. Strip them and a second pass cannot tell restored code from
    prose: a diff line `- old value` loses its `-` to the list rule, a shell
    `# comment` loses its hash to the heading rule, and JSON indentation is
    flattened by the space rule. Protection has to be re-derivable from the
    text itself, so something has to survive to mark it.

    The brief's own escape hatch was "a lightweight marker ... e.g. wrap
    survivors as CODE(...)". A backtick is the lighter one: two characters
    against six, no ambiguity when the content contains a bracket, and it
    already round-trips through this function unchanged. Measured cost of
    keeping every backtick in the corpus: 32,930 characters, 1.73% of claim
    text, ~8,232 tokens — about $0.04 of Opus input across all 56 sessions.
    Idempotency is worth more than four cents."""
    kept = []

    def take(m):
        kept.append(m.group(0))
        return _SENTINEL % (len(kept) - 1)

    text = RE_FENCE.sub(take, text)
    return RE_INLINE.sub(take, text), kept


def _restore(text, kept):
    return _SENTINEL_RE.sub(lambda m: kept[int(m.group(1))], text)


# --------------------------------------------------------------------------
# the rules
# --------------------------------------------------------------------------

# A line that is only ---, ***, ___ (3+, spaces allowed between).
RE_HRULE = re.compile(r"^[ \t]*(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}"
                      r"|(?:_[ \t]*){3,})$")
# A markdown table separator: |---|---| or | :-- | --: |
RE_TABLE_SEP = re.compile(r"^\|[\s\-:|]+\|$")

# Every leading marker at once, so nesting collapses in a single pass.
RE_HEADING = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+)+")
RE_QUOTE = re.compile(r"^[ \t]*(?:>[ \t]?)+")
RE_LIST = re.compile(r"^[ \t]*(?:(?:[-*+]|\d+\.)[ \t]+)+")

# [text](url) -> text: url. Both halves are content; only the punctuation goes.
RE_LINK = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)\)")

# 1-3 emphasis markers taken together: ***x***, **x**, *x*, __x__, _x_.
# The inner text may not start or end with whitespace, which is what stops
# "2 * 3 * 4" in prose being read as emphasis.
RE_STAR = re.compile(r"(?<!\*)(\*{1,3})(?!\s)(.+?)(?<!\s)(?<!\*)\1(?!\*)", re.S)
RE_UNDER = re.compile(r"(?<![\w_])(_{1,3})(?!\s)(.+?)(?<!\s)(?<!_)\1(?![\w_])",
                      re.S)

# Decorative glyphs standing in for plain meaning. Fixed mapping, always.
# U+FE0F is the variation selector some of these arrive with and some do not.
GLYPHS = [
    ("✅", "OK"),        # OK
    ("❌", "FAIL"),      # FAIL
    ("⚠️", "WARNING"),
    ("⚠", "WARNING"),
    ("→", "->"),
    ("←", "<-"),
    ("…", "..."),
]

RE_SPACES = re.compile(r"[ \t]{2,}")
RE_BLANKS = re.compile(r"\n{4,}")


def _one_pass(text):
    """One application of every rule. Inline rules run BEFORE line rules.

    That order matters and was found the hard way. `**1. \\`admin-api\\` was on
    the wrong branch.**` begins with `**`, so the list rule cannot see the
    `1. ` behind it; stripping the emphasis first *reveals* a list marker that
    only the next pass would remove. Running inline rules first means the
    marker is revealed and removed within the same pass."""
    text, kept = _protect(text)

    text = RE_LINK.sub(lambda m: "%s: %s" % (m.group(1), m.group(2)), text)
    text = RE_STAR.sub(lambda m: m.group(2), text)
    text = RE_UNDER.sub(lambda m: m.group(2), text)

    out = []
    for line in text.split("\n"):
        if RE_HRULE.match(line) or RE_TABLE_SEP.match(line):
            # Deleting a rule that sat between two blank lines would leave both
            # behind — a gap this function created, not one the author wrote.
            # Drop one of them, so removing the line is indistinguishable from
            # it never having been there.
            if out and not out[-1].strip():
                out.pop()
            continue
        line = RE_QUOTE.sub("", line)
        line = RE_HEADING.sub("", line)
        # After a quote or heading marker a list marker can still lead.
        line = RE_LIST.sub("", line)
        out.append(line)
    text = "\n".join(out)

    for glyph, plain in GLYPHS:
        text = text.replace(glyph, plain)

    text = RE_SPACES.sub(" ", text)
    text = "\n".join(l.rstrip() for l in text.split("\n"))
    text = RE_BLANKS.sub("\n\n", text)

    return _restore(text, kept).strip()


# Ordering makes one pass enough in every case measured; the loop is what makes
# idempotency structural rather than a property to re-prove per rule. Reaching a
# fixed point IS idempotency — if another pass changes nothing, then by
# definition strip_decoration(strip_decoration(x)) == strip_decoration(x).
MAX_PASSES = 4


def strip_decoration(text):
    """Remove markdown formatting, keep every piece of content unchanged.

    Idempotent by construction: rules are applied to a fixed point."""
    if not text:
        return text or ""

    # The sentinel is NUL-delimited, so a NUL already in the input would be
    # indistinguishable from one this function wrote. Transcript text has none;
    # dropping them is the safe reading rather than risking a bad restore.
    cur = text.replace("\x00", "")
    for _ in range(MAX_PASSES):
        nxt = _one_pass(cur)
        if nxt == cur:
            break
        cur = nxt
    return cur


# --------------------------------------------------------------------------
# tests — written against real shapes from this corpus
#
# Shapes are real; the identifiers in them are not. Hostnames, app names and
# env vars are anonymised to reserved example domains, because this repo is
# public and a fixture only needs the markdown structure to be faithful. Do not
# paste verbatim corpus text in here.
# --------------------------------------------------------------------------

CASES = [
    ("plain prose is a no-op",
     "The endpoint is client-scoped, so I'll key the journey on client id. "
     "It returns 200 for a valid case ref (see the 2026-07-10 run).",
     "The endpoint is client-scoped, so I'll key the journey on client id. "
     "It returns 200 for a valid case ref (see the 2026-07-10 run)."),

    ("gate check report with glyphs and a fence",
     "## Gate check\n\n"
     "- ✅ `tsc` clean\n"
     "- ❌ e2e **failed** on 2 specs\n"
     "- ⚠️ coverage 65.2% → gate is 50%\n\n"
     "```bash\nnpm run test -- --coverage\n  indented line kept\n```\n",
     "Gate check\n\n"
     "OK `tsc` clean\n"
     "FAIL e2e failed on 2 specs\n"
     "WARNING coverage 65.2% -> gate is 50%\n\n"
     "```bash\nnpm run test -- --coverage\n  indented line kept\n```"),

    ("markdown table keeps rows, drops the separator",
     "| Cartridge | Registered |\n|---|---|\n| checkout-ui | yes |\n"
     "| search-api | no |",
     "| Cartridge | Registered |\n| checkout-ui | yes |\n| search-api | no |"),

    ("table separator with alignment colons",
     "| a | b |\n| :-- | --: |\n| 1 | 2 |",
     "| a | b |\n| 1 | 2 |"),

    ("headed section with nested bullets",
     "### Root cause\n"
     "- The cron was never migrated\n"
     "  - `crontab -l` returns nothing\n"
     "    - and no systemd timer\n",
     "Root cause\n"
     "The cron was never migrated\n"
     "`crontab -l` returns nothing\n"
     "and no systemd timer"),

    ("inline code holds paths and env vars unchanged",
     "Set `API_BASE_URL=https://api.example.test` in "
     "`apps/checkout-ui/.env.local` before `npm run dev`.",
     "Set `API_BASE_URL=https://api.example.test` in "
     "`apps/checkout-ui/.env.local` before `npm run dev`."),

    ("horizontal rules go, content either side stays",
     "Before\n\n---\n\nAfter\n\n***\n\nEnd",
     "Before\n\nAfter\n\nEnd"),

    ("markdown link becomes text: url",
     "See [the IDP page](https://confluence.example.com/x/AB1) for detail.",
     "See the IDP page: https://confluence.example.com/x/AB1 for detail."),

    ("bare url untouched",
     "Live at https://checkout-ui.local.test:3400 now.",
     "Live at https://checkout-ui.local.test:3400 now."),

    ("triple emphasis strips in one pass",
     "This is ***very*** important and __also__ this.",
     "This is very important and also this."),

    ("nested list and quote markers collapse in one pass",
     "> > quoted twice\n- - double marker",
     "quoted twice\ndouble marker"),

    ("a fence containing markdown is untouched inside",
     "Here:\n```md\n# Not a heading\n- not a list\n---\n```\ndone",
     "Here:\n```md\n# Not a heading\n- not a list\n---\n```\ndone"),

    ("arithmetic in prose is not emphasis",
     "The budget is 2 * 3 * 4 units and snake_case_name is a name.",
     "The budget is 2 * 3 * 4 units and snake_case_name is a name."),

    ("blockquote with bold inside",
     "> **Note:** the slug is `onboarding-flow`",
     "Note: the slug is `onboarding-flow`"),

    ("four blank lines collapse to one gap",
     "one\n\n\n\n\ntwo",
     "one\n\ntwo"),

    ("repeated spaces collapse outside code",
     "word    spaced   out `keep    these` end",
     "word spaced out `keep    these` end"),

    ("numbered list markers go, line breaks stay",
     "1. First step\n2. Second step\n3. Third step",
     "First step\nSecond step\nThird step"),

    ("unterminated fence still protects its content",
     "Output:\n```\nExit code 1\n  traceback line\n",
     "Output:\n```\nExit code 1\n  traceback line"),

    ("empty and whitespace input",
     "", ""),
]


def self_test():
    failed = 0
    for name, src, want in CASES:
        got = strip_decoration(src)
        if got != want:
            failed += 1
            print("FAIL  %s" % name)
            print("  expected: %r" % want)
            print("  got     : %r" % got)
        # Idempotency is a property of every case, not a case of its own.
        again = strip_decoration(got)
        if again != got:
            failed += 1
            print("FAIL  %s  [not idempotent]" % name)
            print("  once : %r" % got)
            print("  twice: %r" % again)
    print("%d case(s), %d failure(s)" % (len(CASES), failed))
    return 1 if failed else 0


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------

def measure():
    """What it saves, and proof it is idempotent on real text rather than
    on cases someone chose."""
    import session as S
    raw = norm = 0
    events = idem_fail = 0
    for sid in S.sessions():
        evs = S.speech(S.events(sid))
        for e in evs:
            t = e.get("text")
            if not t:
                continue
            s = strip_decoration(t)
            if strip_decoration(s) != s:
                idem_fail += 1
            raw += len(t)
            norm += len(s)
            events += 1
    print("%d event(s) across %d session(s)" % (events, len(S.sessions())))
    print("chars %s -> %s  (%.1f%% smaller)"
          % ("{:,}".format(raw), "{:,}".format(norm),
             100.0 * (raw - norm) / raw if raw else 0))
    print("~%s -> ~%s tokens" % ("{:,}".format(raw // 4),
                                 "{:,}".format(norm // 4)))
    print("idempotency failures on real text: %d" % idem_fail)
    return 1 if idem_fail else 0


def show(prefix, limit=3):
    import session as S
    sid = S.resolve(prefix)
    evs = S.speech(S.events(sid))
    n = 0
    for e in evs:
        t = e.get("text") or ""
        s = strip_decoration(t)
        if s == t or len(t) < 200:
            continue
        print("=" * 70)
        print("BEFORE (%d chars)\n%s" % (len(t), t[:700]))
        print("-" * 70)
        print("AFTER  (%d chars)\n%s" % (len(s), s[:700]))
        n += 1
        if n >= limit:
            break
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true", help="run unit tests")
    ap.add_argument("--measure", action="store_true",
                    help="size saved across the corpus")
    ap.add_argument("--show", metavar="SESSION",
                    help="before/after on real claims from one session")
    args = ap.parse_args(argv)
    if args.show:
        return show(args.show)
    if args.measure:
        return measure()
    return self_test()


if __name__ == "__main__":
    sys.exit(main())
