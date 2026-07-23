#!/usr/bin/env python3
"""Installer / uninstaller / corpus manager for the Context Churn Recorder.

The recorder and its corpus live in a single global home, `~/.claude-transcripts`:

    ~/.claude-transcripts/
        recorder.py          # the deployed hook entrypoint
        corpus/              # the shared corpus (0700, gitignored)

All captured data lands in that one global corpus regardless of which project
triggered the hook. What varies is *which sessions get recorded*, controlled by
where the hooks are registered:

    * default            -> hooks in ~/.claude/settings.json  (records EVERY
                            session on the machine, across all projects)
    * --project PATH     -> hooks in PATH/.claude/settings.json (records only
                            that project's sessions)

Subcommands:

    install    Deploy the recorder, prepare the global corpus, and merge the
               hooks into the chosen settings.json. Optionally tune the
               compaction window.
    uninstall  Remove ONLY the recorder's hook entries (leaves everything else,
               and leaves the corpus intact).
    status     Report which hooks are registered and what the corpus holds.
    clear      Wipe the corpus in one command (reset between collection runs).
    prune      Remove part of the corpus (old files or old sessions).

Examples:

    python3 install.py install                      # global (all sessions)
    python3 install.py install --window 40000
    python3 install.py install --project /path/to/proj
    python3 install.py uninstall
    python3 install.py status
    python3 install.py clear --yes
    python3 install.py prune --older-than-days 7 --yes

Design notes:
  * Global by default. A global install captures EVERY Claude Code session on
    the machine — including unrelated projects and their secrets. Use --project
    to restrict recording to a single project.
  * Non-destructive & idempotent: existing settings are merged, not overwritten;
    re-running never duplicates entries (our hooks are stripped then re-added).
  * The recorder command is guarded with a shell-level `|| true` so that even a
    failure to launch the interpreter cannot return non-zero to a blocking hook.
"""

import argparse
import json
import os
import shutil
import sys

# The six capture points, plus the tool-failure event (a distinct kind of churn
# — a tool call that failed). Events that take a tool-name matcher are marked;
# the rest fire unconditionally (PreCompact with no matcher fires on both auto
# and manual compaction).
EVENTS = [
    ("SessionStart", False),
    ("PreCompact", False),
    ("PostCompact", False),
    ("SessionEnd", False),
    ("Stop", False),
    ("PostToolUse", True),
    ("PostToolUseFailure", True),
]

# The recorder's global home. Kept in sync with recorder.transcripts_home().
TRANSCRIPTS_HOME = os.path.join(os.path.expanduser("~"), ".claude-transcripts")

# Substring that uniquely identifies a hook command as belonging to this
# recorder. Used for idempotent install and for surgical uninstall.
SENTINEL = ".claude-transcripts/recorder.py"

# The hook command. $HOME is expanded by the shell inside double quotes, so this
# one form is portable across machines and works for both global and
# project-scoped registration (the recorder is always deployed globally).
def command_for(event):
    return 'python3 "$HOME/.claude-transcripts/recorder.py" %s || true' % event

# Reserved buffer that Claude Code subtracts from the compaction window. A
# window at or below this collapses the trigger threshold to zero and
# compaction loops forever. See --window guards below.
COMPACT_RESERVED_BUFFER = 13000
WINDOW_HARD_FLOOR = 20000   # reject below this
WINDOW_SAFE = 30000         # warn between floor and this


# --------------------------------------------------------------------------- #
# path resolution
# --------------------------------------------------------------------------- #

def recorder_dir():
    return TRANSCRIPTS_HOME


def corpus_path():
    return os.path.join(TRANSCRIPTS_HOME, "corpus")


def claude_dir(project):
    """The .claude dir whose settings.json receives the hooks. Global (~/.claude)
    unless a project was given."""
    if project is None:
        return os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(os.path.abspath(project), ".claude")


def settings_path(project):
    return os.path.join(claude_dir(project), "settings.json")


# --------------------------------------------------------------------------- #
# settings.json read / write / merge
# --------------------------------------------------------------------------- #

def load_settings(path):
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        text = fh.read().strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            "ERROR: %s is not valid JSON (%s).\n"
            "Refusing to overwrite it — fix or remove it, then re-run." % (path, exc))
    if not isinstance(data, dict):
        raise SystemExit("ERROR: %s does not contain a JSON object." % path)
    return data


def write_settings(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def strip_our_hooks(settings):
    """Remove every command hook that belongs to this recorder, pruning any
    matcher-groups and event arrays we thereby empty. Returns count removed.
    Leaves all non-recorder settings untouched."""
    removed = 0
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    for event in list(hooks.keys()):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            cmds = group.get("hooks")
            if not isinstance(cmds, list):
                new_groups.append(group)
                continue
            kept = []
            for cmd in cmds:
                if (isinstance(cmd, dict)
                        and isinstance(cmd.get("command"), str)
                        and SENTINEL in cmd["command"]):
                    removed += 1
                else:
                    kept.append(cmd)
            if kept:
                group["hooks"] = kept
                new_groups.append(group)
            # else: group is now empty -> drop it
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return removed


def add_our_hooks(settings):
    hooks = settings.setdefault("hooks", {})
    for event, takes_matcher in EVENTS:
        group = {"hooks": [{"type": "command", "command": command_for(event)}]}
        if takes_matcher:
            group = {"matcher": "*", **group}
        hooks.setdefault(event, []).append(group)


# --------------------------------------------------------------------------- #
# compaction window
# --------------------------------------------------------------------------- #

def validate_window(n):
    """Return a list of warning strings, or raise SystemExit on a bad value."""
    if n <= COMPACT_RESERVED_BUFFER:
        raise SystemExit(
            "ERROR: --window %d collapses the compaction threshold to zero "
            "(min(window*pct/100, window-%d) <= 0) and compaction would loop "
            "continuously. Use a value well above %d (>= %d)."
            % (n, COMPACT_RESERVED_BUFFER, COMPACT_RESERVED_BUFFER, WINDOW_HARD_FLOOR))
    if n < WINDOW_HARD_FLOOR:
        raise SystemExit(
            "ERROR: --window %d is too close to the %d reserved buffer to be "
            "safe. Use >= %d (%d+ recommended)."
            % (n, COMPACT_RESERVED_BUFFER, WINDOW_HARD_FLOOR, WINDOW_SAFE))
    warnings = []
    if n < WINDOW_SAFE:
        warnings.append(
            "--window %d is above the floor but modest; %d+ is a comfortable "
            "margin over the %d reserved buffer." % (n, WINDOW_SAFE, COMPACT_RESERVED_BUFFER))
    return warnings


def check_local_shadow(cdir):
    """Warn if .claude/settings.local.json sets a window/pct override that would
    silently shadow what we write into settings.json."""
    local = os.path.join(cdir, "settings.local.json")
    if not os.path.exists(local):
        return []
    try:
        with open(local) as fh:
            data = json.load(fh)
    except Exception:
        return []
    env = data.get("env", {}) if isinstance(data, dict) else {}
    shadow_keys = [k for k in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                               "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") if k in env]
    if shadow_keys:
        return ["WARNING: %s sets %s in its `env` block, which overrides "
                "settings.json. Your --window may have no effect until that is "
                "removed." % (local, ", ".join(shadow_keys))]
    return []


# --------------------------------------------------------------------------- #
# corpus helpers
# --------------------------------------------------------------------------- #

def deploy_recorder(dest_dir):
    """Copy recorder.py from beside this installer into the target dir."""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recorder.py")
    if not os.path.exists(src):
        raise SystemExit("ERROR: recorder.py not found next to install.py (%s)." % src)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "recorder.py")
    shutil.copyfile(src, dest)
    os.chmod(dest, 0o755)
    return dest


def ensure_corpus(path):
    """Mirror recorder.ensure_corpus so `status`/install can report a ready,
    gitignored, 0700 corpus without importing the recorder."""
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


def _corpus_stats(cpath):
    sessions, snaps, total = set(), 0, 0
    if os.path.isdir(cpath):
        for name in os.listdir(cpath):
            full = os.path.join(cpath, name)
            if not os.path.isfile(full):
                continue
            total += os.path.getsize(full)
            if name.endswith(".events.jsonl"):
                sessions.add(name.split(".")[0])
            elif name.endswith(".transcript.jsonl"):
                snaps += 1
    return sessions, snaps, total


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def _iter_corpus_files(cpath):
    if not os.path.isdir(cpath):
        return
    for name in os.listdir(cpath):
        full = os.path.join(cpath, name)
        if os.path.isfile(full) and name != ".gitignore":
            yield name, full


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #

def cmd_install(args):
    project = args.project          # None => global registration
    is_global = project is None
    cdir = claude_dir(project)
    spath = settings_path(project)

    if is_global:
        print("!! GLOBAL install: this records EVERY Claude Code session on this")
        print("!! machine, across all projects (including their secrets).")
        print("!! Use --project PATH to restrict recording to one project.\n")

    # 1. deploy the recorder + prepare the global corpus
    dest = deploy_recorder(recorder_dir())
    cpath = corpus_path()
    ensure_corpus(cpath)

    # 2. merge hooks (strip-then-add makes this idempotent)
    settings = load_settings(spath)
    removed = strip_our_hooks(settings)
    add_our_hooks(settings)

    # 3. optional compaction window
    warnings = []
    if args.window is not None:
        warnings += validate_window(args.window)
        settings.setdefault("env", {})["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(args.window)
        warnings += check_local_shadow(cdir)

    write_settings(spath, settings)

    # 4. report
    print("Context Churn Recorder installed.")
    print("  settings : %s" % spath)
    print("  recorder : %s" % dest)
    print("  corpus   : %s  (global, 0700, gitignored)" % cpath)
    print("  records  : %s" % ("ALL sessions on this machine"
                               if is_global else "sessions in %s" % os.path.abspath(project)))
    if removed:
        print("  (replaced %d pre-existing recorder hook entrie(s))" % removed)
    print("  hooks    : %s" % ", ".join(e for e, _ in EVENTS))
    if args.window is not None:
        print("  window   : CLAUDE_CODE_AUTO_COMPACT_WINDOW=%d" % args.window)
        print("             NOTE: env vars bind at session start — restart any")
        print("             running session for the new window to take effect.")
    for w in warnings:
        print("  " + w)
    print("\nConfirm registration in-session with /hooks.")


def cmd_uninstall(args):
    spath = settings_path(args.project)
    if not os.path.exists(spath):
        print("Nothing to do: %s does not exist." % spath)
        return
    settings = load_settings(spath)
    removed = strip_our_hooks(settings)
    write_settings(spath, settings)
    print("Removed %d recorder hook entrie(s) from %s" % (removed, spath))
    print("The global corpus at %s was left intact (use `clear` to wipe it)."
          % corpus_path())


def cmd_status(args):
    spath = settings_path(args.project)
    settings = load_settings(spath) if os.path.exists(spath) else {}
    hooks = settings.get("hooks", {}) if isinstance(settings, dict) else {}

    print("Settings: %s%s" % (spath, "" if args.project else "  (global)"))
    print("Registered recorder hooks:")
    for event, _ in EVENTS:
        groups = hooks.get(event, []) if isinstance(hooks, dict) else []
        found = any(
            SENTINEL in (c.get("command") or "")
            for g in groups if isinstance(g, dict)
            for c in g.get("hooks", []) if isinstance(c, dict)
        )
        print("  [%s] %s" % ("x" if found else " ", event))

    win = settings.get("env", {}).get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") \
        if isinstance(settings.get("env"), dict) else None
    print("Compaction window (CLAUDE_CODE_AUTO_COMPACT_WINDOW): %s"
          % (win if win else "unset (default)"))

    cpath = corpus_path()
    sessions, snaps, total = _corpus_stats(cpath)
    print("Corpus (global): %s" % cpath)
    print("  sessions   : %d" % len(sessions))
    print("  snapshots  : %d" % snaps)
    print("  total size : %s" % _human(total))


def cmd_clear(args):
    cpath = corpus_path()
    files = list(_iter_corpus_files(cpath))
    if not files:
        print("Corpus is already empty: %s" % cpath)
        return
    total = sum(os.path.getsize(f) for _, f in files)
    if not args.yes:
        print("Would delete %d file(s) (%s) from %s" % (len(files), _human(total), cpath))
        print("Re-run with --yes to actually clear the corpus.")
        return
    for _, full in files:
        try:
            os.remove(full)
        except OSError as exc:
            print("  could not remove %s: %s" % (full, exc))
    print("Cleared %d file(s) (%s) from %s" % (len(files), _human(total), cpath))


def cmd_prune(args):
    cpath = corpus_path()
    if args.older_than_days is None and args.keep_sessions is None:
        raise SystemExit("prune needs --older-than-days N and/or --keep-sessions N")

    files = list(_iter_corpus_files(cpath))
    victims = set()

    # By age: mtime is a convenience signal only (never used for ordering/joins).
    if args.older_than_days is not None:
        import time
        cutoff = time.time() - args.older_than_days * 86400
        for name, full in files:
            if os.path.getmtime(full) < cutoff:
                victims.add(full)

    # By session recency: keep the N most-recently-active sessions, drop the
    # rest wholesale (all files sharing that session_id prefix).
    if args.keep_sessions is not None:
        by_session = {}
        for name, full in files:
            sid = name.split(".")[0]
            by_session.setdefault(sid, []).append(full)
        recency = sorted(
            by_session.items(),
            key=lambda kv: max(os.path.getmtime(f) for f in kv[1]),
            reverse=True)
        for _sid, sfiles in recency[args.keep_sessions:]:
            victims.update(sfiles)

    if not victims:
        print("Nothing to prune in %s" % cpath)
        return
    total = sum(os.path.getsize(f) for f in victims if os.path.exists(f))
    if not args.yes:
        print("Would prune %d file(s) (%s) from %s" % (len(victims), _human(total), cpath))
        print("Re-run with --yes to actually prune.")
        return
    for full in victims:
        try:
            os.remove(full)
        except OSError as exc:
            print("  could not remove %s: %s" % (full, exc))
    print("Pruned %d file(s) (%s) from %s" % (len(victims), _human(total), cpath))


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(description="Install/manage the Context Churn Recorder.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_target(sp):
        sp.add_argument("--project", default=None,
                        help="restrict recording to this project's sessions "
                             "(hooks go in PATH/.claude/settings.json). "
                             "Default: global (~/.claude), recording ALL sessions.")

    sp = sub.add_parser("install", help="install/refresh the recorder hooks")
    add_target(sp)
    sp.add_argument("--window", type=int, default=None,
                    help="set CLAUDE_CODE_AUTO_COMPACT_WINDOW (>= %d; %d+ recommended)"
                         % (WINDOW_HARD_FLOOR, WINDOW_SAFE))
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("uninstall", help="remove only the recorder hooks")
    add_target(sp)
    sp.set_defaults(func=cmd_uninstall)

    sp = sub.add_parser("status", help="show registered hooks and corpus stats")
    add_target(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("clear", help="wipe the entire (global) corpus")
    sp.add_argument("--yes", action="store_true", help="actually delete (otherwise dry-run)")
    sp.set_defaults(func=cmd_clear)

    sp = sub.add_parser("prune", help="remove old files or old sessions from the corpus")
    sp.add_argument("--older-than-days", type=int, default=None,
                    help="remove files older than N days (by mtime)")
    sp.add_argument("--keep-sessions", type=int, default=None,
                    help="keep only the N most-recently-active sessions")
    sp.add_argument("--yes", action="store_true", help="actually delete (otherwise dry-run)")
    sp.set_defaults(func=cmd_prune)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
