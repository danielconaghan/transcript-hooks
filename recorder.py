#!/usr/bin/env python3
"""Context Churn Recorder — hook entrypoint and capture worker.

This is a *recorder*, nothing more. At each of a set of Claude Code lifecycle
events it records two things into a local corpus:

  1. an event record   -> appended to  <session_id>.events.jsonl
  2. the transcript state -> written to <session_id>.<count>.transcript.jsonl

so that the *delta* between any two consecutive captures can be reconstructed
offline later by joining on (session_id, count). It does no analysis, scoring,
detection, or classification of any kind — that is deliberately out of scope.

Invocation (from the hook command configured in .claude/settings.json):

    python3 recorder.py <EventName>        # payload arrives as JSON on stdin

<EventName> is one of:
    SessionStart PreCompact PostCompact SessionEnd Stop
    PostToolUse PostToolUseFailure

Design properties (see README.md for the full rationale):

  * count is minted by *this* recorder — a monotonic, per-session sequence that
    is always present, always unique, always ordered. It is the primary key and
    the definitive capture order. We never rely on hook firing order (parallel
    tool-call hooks fire in completion order) or on filesystem timestamps.

  * Non-blocking by default. Every event except PreCompact detaches a worker
    process and returns immediately, so capture never adds latency to the
    session. PreCompact is the one synchronous exception: it must snapshot the
    transcript *before* compaction rewrites it, so it captures inline. Both
    paths still always exit 0.

  * Fail-open, always. Any failure (disk, permissions, malformed payload) is
    swallowed to an error log; the hook never breaks or interrupts the session.
    A shell-level `|| true` in the hook command is the outer guard; the
    try/except in this file is the inner guard.

  * No dependence on the transcript .jsonl internal schema. The transcript is
    captured by copying bytes — it is never parsed here. Everything the event
    record needs (session_id, prompt_id, tool_use_id, timestamps) comes from the
    hook payload, which is the stable source of truth.
"""

import datetime
import fcntl
import json
import os
import shutil
import sys
import traceback

# Events that MUST capture synchronously. PreCompact fires before compaction
# rewrites the transcript file in place; if we returned before snapshotting, the
# pre-compaction state would be lost or half-written. This is the single
# justified blocking capture. Everything else detaches.
SYNC_EVENTS = frozenset({"PreCompact"})

KNOWN_EVENTS = frozenset({
    "SessionStart", "PreCompact", "PostCompact", "SessionEnd",
    "Stop", "PostToolUse", "PostToolUseFailure",
})


def transcripts_home():
    """The recorder's global home directory, `~/.claude-transcripts` by default.

    A single global store is used deliberately: the corpus is shared across
    every project/session on the machine, independent of where the hooks are
    registered. Override with $CLAUDE_TRANSCRIPTS_HOME (used by tests)."""
    return (os.environ.get("CLAUDE_TRANSCRIPTS_HOME")
            or os.path.join(os.path.expanduser("~"), ".claude-transcripts"))


def corpus_dir():
    """Resolve the corpus directory: `<transcripts_home>/corpus`.

    Always a fixed global location, never relative to cwd or to this script,
    so every session records into the same corpus regardless of which project
    triggered the hook."""
    return os.path.join(transcripts_home(), "corpus")


def ensure_corpus(path):
    """Create the corpus dir (0700) with a self-contained .gitignore.

    The `.gitignore` contains `*`, so the entire corpus — a secrets-bearing
    store — is ignored by git regardless of the surrounding project's ignore
    rules, and is never accidentally committed.
    """
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    gi = os.path.join(path, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as fh:
            fh.write("# Auto-created by the context churn recorder.\n"
                     "# The corpus is local-only and may contain secrets — never commit it.\n"
                     "*\n")


def log_error(path, event, message):
    """Fail-open error logging. Best-effort; never raises."""
    try:
        line = "%s\t%s\t%s\n" % (_now_iso(), event, message.replace("\n", " \\n "))
        with open(os.path.join(path, "errors.log"), "a") as fh:
            fh.write(line)
    except Exception:
        pass  # last resort: even logging is best-effort.


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class _SessionLock:
    """Advisory exclusive file lock, per session, for the brief critical
    sections (minting count, appending the event line). flock is honoured
    across processes on macOS/Linux, so concurrent detached workers for the
    same session serialize here. Different sessions use different lock files
    and never contend. The slow transcript copy happens OUTSIDE the lock."""

    def __init__(self, corpus, session_id):
        self._path = os.path.join(corpus, "%s.lock" % session_id)
        self._fd = None

    def __enter__(self):
        self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
        return False


def mint_count(corpus, session_id):
    """Atomically increment and return the per-session counter.

    Must be called while holding the session lock. The counter lives in
    <session_id>.count and is fsynced so a crash cannot rewind it and produce a
    duplicate count (which would collide two snapshots and corrupt ordering)."""
    cpath = os.path.join(corpus, "%s.count" % session_id)
    n = 0
    if os.path.exists(cpath):
        try:
            with open(cpath) as fh:
                n = int(fh.read().strip() or "0")
        except (ValueError, OSError):
            n = 0
    n += 1
    fd = os.open(cpath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, str(n).encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return n


def snapshot_transcript(transcript_path, dest):
    """Copy the transcript file verbatim to `dest`. Returns bytes written.

    A byte copy — the transcript is never parsed, so a change to its internal
    .jsonl schema cannot break capture. If the source does not exist yet (e.g. a
    fresh SessionStart before any transcript is written), we still create an
    empty snapshot so the (session_id, count) join and the delta-from-nothing
    remain representable."""
    if transcript_path and os.path.exists(transcript_path):
        shutil.copyfile(transcript_path, dest)
    else:
        # touch an empty snapshot
        open(dest, "w").close()
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return os.path.getsize(dest)


def capture(event, raw):
    """Perform one capture. Raises on failure (caller logs & swallows)."""
    corpus = corpus_dir()
    ensure_corpus(corpus)

    # The payload is the source of truth. If it will not parse as JSON there is
    # nothing sensible to key on (no session_id, no transcript_path), so we log
    # the raw bytes and give up on this capture — fail-open.
    try:
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
    except Exception as exc:
        log_error(corpus, event,
                  "unparseable payload (%s): %r" % (exc, raw[:500]))
        return

    session_id = payload.get("session_id")
    if not session_id:
        log_error(corpus, event, "payload missing session_id; skipping")
        return

    transcript_path = payload.get("transcript_path")

    # --- critical section 1: mint the count (fast) ---------------------------
    with _SessionLock(corpus, session_id):
        count = mint_count(corpus, session_id)

    # --- slow part: snapshot the transcript (OUTSIDE the lock) ---------------
    snap_name = "%s.%d.transcript.jsonl" % (session_id, count)
    snap_path = os.path.join(corpus, snap_name)
    snap_bytes = None
    snap_error = None
    try:
        snap_bytes = snapshot_transcript(transcript_path, snap_path)
    except Exception as exc:
        # Record the failure in the event line rather than aborting: the event
        # itself is still worth logging even if the snapshot could not be taken.
        snap_error = "%s: %s" % (type(exc).__name__, exc)
        log_error(corpus, event,
                  "snapshot failed for count=%d src=%r: %s"
                  % (count, transcript_path, snap_error))

    # Build the event record. Fields are pulled from the payload (stable source
    # of truth); `captured_at` is our own reliable in-record timestamp, and the
    # full raw payload is embedded so nothing is lost to a field we did not
    # think to extract.
    record = {
        "count": count,                       # primary key + definitive order
        "session_id": session_id,
        "event": event,
        "ts": payload.get("ts"),              # payload time if present (may be null)
        "captured_at": _now_iso(),            # recorder's own reliable time
        "prompt_id": payload.get("prompt_id"),        # absent on startup/resume
        "tool_use_id": payload.get("tool_use_id"),    # PostToolUse family only
        "hook_event_name": payload.get("hook_event_name"),
        "snapshot": snap_name,                # convenience; join is really count
        "snapshot_bytes": snap_bytes,
        "snapshot_error": snap_error,
        "payload": payload,                   # full raw payload, nothing dropped
    }
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"

    # --- critical section 2: append the event line (fast, atomic) ------------
    # Held under the lock so concurrent workers cannot interleave bytes within a
    # line. The append order need not match count order — every line carries its
    # count, and readers sort by it.
    events_path = os.path.join(corpus, "%s.events.jsonl" % session_id)
    with _SessionLock(corpus, session_id):
        fd = os.open(events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)


def _detach():
    """Fork into a detached background process so the hook returns immediately.

    Returns True in the (surviving) child that should do the work, False in the
    parent that should exit now. If fork is unavailable or fails, returns True
    to fall back to a synchronous (still fast, still fail-open) capture."""
    try:
        pid = os.fork()
    except (OSError, AttributeError):
        return True  # no fork (e.g. Windows) -> capture synchronously

    if pid > 0:
        # Parent: return immediately, unblocking the hook. Use _exit to skip
        # atexit/buffer flushing — the parent has written nothing.
        os._exit(0)

    # Child: fully detach from the session's process group and controlling
    # terminal, and redirect stdio to /dev/null so no inherited pipe keeps
    # Claude Code's reader open (which would defeat the non-blocking goal).
    try:
        os.setsid()
    except OSError:
        pass
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        if devnull > 2:
            os.close(devnull)
    except OSError:
        pass
    return True


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else "Unknown"

    # Read the whole payload NOW, before any detach: once the hook returns,
    # Claude Code closes stdin. The bytes are then carried into the forked
    # child via inherited memory (no temp file needed).
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        raw = b""

    if event in SYNC_EVENTS:
        # Synchronous path: snapshot before the transcript is rewritten.
        capture(event, raw)
        return

    # Async path: detach, then capture in the background.
    if _detach():
        capture(event, raw)
        # In the forked child, terminate hard so we never run interpreter
        # shutdown in the detached process.
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(0)


if __name__ == "__main__":
    # Outer catch-all: this recorder must NEVER return non-zero to the hook
    # (some events — PreCompact, Stop — can block the session on a non-zero
    # exit). Two guards protect the session: this try/except and the shell-level
    # `|| true` in the hook command.
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        try:
            log_error(corpus_dir(), sys.argv[1] if len(sys.argv) > 1 else "?",
                      "fatal: " + traceback.format_exc())
        except Exception:
            pass
    sys.exit(0)
