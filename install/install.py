#!/usr/bin/env python3
"""Installer / uninstaller / corpus manager for claude-resync.

Everything installed lives in one global home, `~/.claude-resync`:

    ~/.claude-resync/
        recorder.py          # capture hook entrypoint
        intercept.py         # pre-send interception hook entrypoint
        rules_engine.py      # shared triggers, imported by intercept.py
        rules.json           # the rule catalogue (deployed copy)
        corpus/              # raw captures (0700, gitignored)
        refined/             # lossless reduction (0700, gitignored)
        data/                # fires.jsonl, labels.jsonl, normalise.jsonl,
                             # desync.jsonl, directive.jsonl, error logs
        intercept-cache/     # per-session incremental state
        .venv/               # the anthropic SDK, for API-drafted fixes
        .env                 # ANTHROPIC_API_KEY (0600), never in the repo

The source repo is canonical; this deploys from it. Hook commands reference
$HOME/.claude-resync rather than a repo path, so moving or deleting the repo
cannot break a live session.

rules.json is the one file that flows BOTH ways: it is hand-edited and
git-tracked in the repo, `research/backtest.py --write` updates the repo copy
with measured precision, and install deploys it. `status` reports when the
deployed copy has drifted from the repo so a stale catalogue is visible rather
than silently in force.

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
import glob
import json
import os
import shutil
import subprocess
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
TRANSCRIPTS_HOME = (os.environ.get("CLAUDE_RESYNC_HOME")
                    or os.environ.get("CLAUDE_TRANSCRIPTS_HOME")
                    or os.path.join(os.path.expanduser("~"), ".claude-resync"))

# Substring that uniquely identifies a hook command as belonging to this
# recorder. Used for idempotent install and for surgical uninstall.
# Substrings identifying a hook command as ours, for idempotent install and
# surgical uninstall. Path-qualified rather than bare filenames so we never
# strip an unrelated hook that happens to mention "recorder.py". Both the
# current home and the pre-rename one are listed, so uninstall still works on
# an install that predates the rename.
SENTINELS = (
    ".claude-resync/collection/hooks/recorder.py",
    ".claude-resync/collection/hooks/intercept.py",
    ".claude-resync/recorder.py",          # pre-collection/ flat layout
    ".claude-resync/intercept.py",         # pre-collection/ flat layout
    ".claude-transcripts/recorder.py",     # legacy
    ".claude-transcripts/intercept.py",    # legacy
)
SENTINEL = SENTINELS[0]                    # retained for existing references
SENTINEL_INTERCEPT = SENTINELS[1]


def is_ours(command):
    return isinstance(command, str) and any(x in command for x in SENTINELS)


def script_of(command):
    """The .py path out of one of our hook commands, $HOME expanded."""
    for token in (command or "").split('"'):
        if token.endswith(".py"):
            return os.path.expandvars(os.path.expanduser(token))
    return ""


def registered_commands(hooks):
    """Every hook command in a settings `hooks` block that belongs to us."""
    if not isinstance(hooks, dict):
        return []
    return [c.get("command") for groups in hooks.values()
            for g in (groups or []) if isinstance(g, dict)
            for c in g.get("hooks", []) if isinstance(c, dict)
            and is_ours(c.get("command"))]

# Where the hooks live, as the shell sees it. One constant, because when
# collection/ was introduced the deploy moved and these two commands did not:
# every hook then pointed at a path that no longer existed, and `|| true`
# swallowed the error for eight days. Derive, never restate.
HOOKS_SH_DIR = "$HOME/.claude-resync/collection/hooks"


# The hook command. $HOME is expanded by the shell inside double quotes, so this
# one form is portable across machines and works for both global and
# project-scoped registration (the recorder is always deployed globally).
def command_for(event):
    return 'python3 "%s/recorder.py" %s || true' % (HOOKS_SH_DIR, event)


def intercept_command():
    return 'python3 "%s/intercept.py" || true' % HOOKS_SH_DIR

# Reserved buffer that Claude Code subtracts from the compaction window. A
# window at or below this collapses the trigger threshold to zero and
# compaction loops forever. See --window guards below.
COMPACT_RESERVED_BUFFER = 13000
WINDOW_HARD_FLOOR = 20000   # reject below this
WINDOW_SAFE = 30000         # warn between floor and this


# --------------------------------------------------------------------------- #
# path resolution
# --------------------------------------------------------------------------- #

# The home is split in three, because the parts have different owners and
# different lifetimes.
#
#     <home>/corpus/      raw capture          ROOT: shared input
#     <home>/refined/     lossless archive     ROOT: shared input
#     <home>/.env .venv/                       ROOT: shared credentials + SDK
#
#     <home>/collection/  everything that runs LIVE
#         hooks/          recorder.py, intercept.py
#         rules_engine.py rules.json
#         cache/          per-session interception state
#         data/           fires, labels, normalise, markers, error log
#
#     <home>/analysis/    everything that runs OFFLINE, on stored data
#         views/          deterministic views, free to regenerate
#         verdicts/       model judgements, paid for
#         data/           desync.jsonl, directive.jsonl
#
# corpus/ and refined/ sit at the root rather than under collection/ because
# collection WRITES them and analysis READS them: filing shared input under one
# consumer would say it belongs to that one. The previous flat layout put a
# live fires.jsonl next to a research desync.jsonl in the same data/ directory,
# which is how a marker file gets hunted for.
#
# Naming caveat, worth stating: intercept.py is an INTERVENTION — the
# preventative actions and the normalisation layer — not collection. It lives
# here because it is a hook and splitting it from recorder.py would scatter the
# live runtime, so "collection" means "everything that runs live" rather than
# "everything that captures".

def recorder_dir():
    """Where the live runtime is deployed."""
    return os.path.join(TRANSCRIPTS_HOME, "collection")


def hooks_dir():
    return os.path.join(recorder_dir(), "hooks")


def analysis_dir():
    return os.path.join(TRANSCRIPTS_HOME, "analysis")


def corpus_path():
    return os.path.join(TRANSCRIPTS_HOME, "corpus")


# Old flat location -> new home. Applied by `migrate_layout()` on any install
# or deploy, so an existing install moves itself rather than needing a manual
# shuffle and rather than silently running two layouts at once.
LAYOUT_MOVES = [
    ("intercept-cache", "collection/cache"),
    ("views", "analysis/views"),
    ("verdicts", "analysis/verdicts"),
]
# data/ splits by who writes the file, which the flat layout could not express.
DATA_MOVES = [
    ("fires.jsonl", "collection/data"),
    ("labels.jsonl", "collection/data"),
    ("normalise.jsonl", "collection/data"),
    ("markers.jsonl", "collection/data"),
    ("intercept-errors.log", "collection/data"),
    ("desync.jsonl", "analysis/data"),
    ("directive.jsonl", "analysis/data"),
    # Superseded by analysis/verdicts/, kept as history rather than deleted:
    # it is the pre-per-session verdict store and cost real money.
    ("ledger.jsonl", "analysis/data"),
]


# Directories the split owns. Anything else at the root that has a counterpart
# under analysis/verdicts/ is a pre-split leftover.
RESERVED_DIRS = ("corpus", "refined", "collection", "analysis", "data",
                 ".venv", "__pycache__")


def _is_superset(new_dir, old_dir):
    """Is every row of every file in old_dir present in new_dir?

    Compared on content with the renamed key fields excluded, because
    migration renamed pair_key/exchange_key to unit_key. Any file without a
    counterpart, or any row that does not appear, makes this False."""
    for f in glob.glob(os.path.join(old_dir, "*.jsonl")):
        counterpart = os.path.join(new_dir, os.path.basename(f))
        if not os.path.isfile(counterpart):
            return False

        def rows(path):
            out = []
            for line in open(path, errors="replace"):
                try:
                    d = json.loads(line)
                except Exception:
                    return None
                out.append(json.dumps(
                    {k: v for k, v in d.items()
                     if k not in ("kind", "pair_key", "exchange_key",
                                  "unit_key", "view")},
                    sort_keys=True, default=str))
            return out

        old_rows, new_rows = rows(f), rows(counterpart)
        if old_rows is None or new_rows is None:
            return False
        if any(r not in set(new_rows) for r in old_rows):
            return False
    return True


def migrate_layout():
    """Move an old flat install into collection/ and analysis/. Idempotent.

    Never overwrites: if the destination already exists the source is left
    alone and reported, because two copies of a corpus-derived file are a
    problem to look at rather than to resolve automatically."""
    moved, skipped = [], []
    for old, new in LAYOUT_MOVES:
        src = os.path.join(TRANSCRIPTS_HOME, old)
        dst = os.path.join(TRANSCRIPTS_HOME, new)
        if not os.path.isdir(src):
            continue
        if os.path.exists(dst):
            skipped.append((old, new))
            continue
        os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
        shutil.move(src, dst)
        moved.append((old, new))
    for name, sub in DATA_MOVES:
        src = os.path.join(TRANSCRIPTS_HOME, "data", name)
        dst = os.path.join(TRANSCRIPTS_HOME, sub, name)
        if not os.path.isfile(src):
            continue
        if os.path.exists(dst):
            skipped.append(("data/" + name, sub + "/" + name))
            continue
        os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
        shutil.move(src, dst)
        moved.append(("data/" + name, sub + "/" + name))
    # Per-view verdict directories that predate analysis/verdicts/. Removed
    # only when the migrated copy is a superset row for row: a verdict was paid
    # for, so "probably a duplicate" is not good enough to delete on.
    for old in sorted(glob.glob(os.path.join(TRANSCRIPTS_HOME, "*"))):
        name = os.path.basename(old)
        if not os.path.isdir(old) or name in RESERVED_DIRS:
            continue
        new_dir = os.path.join(TRANSCRIPTS_HOME, "analysis", "verdicts", name)
        if not os.path.isdir(new_dir):
            continue
        if _is_superset(new_dir, old):
            shutil.rmtree(old)
            moved.append((name + "/", "(removed, superseded by analysis/verdicts/%s)" % name))
        else:
            skipped.append((name + "/", "analysis/verdicts/" + name))

    # Empty directories the old layout left behind. Only ever empty ones —
    # a directory with anything in it is reported, never removed.
    for old in sorted(glob.glob(os.path.join(TRANSCRIPTS_HOME, "*"))):
        name = os.path.basename(old)
        if os.path.isdir(old) and name not in RESERVED_DIRS and not os.listdir(old):
            os.rmdir(old)
            moved.append((name + "/", "(removed, empty)"))

    # Old deployed entrypoints at the root: the new ones live in
    # collection/hooks/ and are written by deploy_runtime.
    for name in ("recorder.py", "intercept.py", "rules_engine.py", "rules.json"):
        old = os.path.join(TRANSCRIPTS_HOME, name)
        if os.path.isfile(old) and os.path.isfile(
                os.path.join(recorder_dir(), name if name.endswith(".json")
                             or name == "rules_engine.py" else "hooks/" + name)):
            os.remove(old)
            moved.append((name, "(removed, superseded)"))
    return moved, skipped


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
    """Remove every command hook that belongs to us — capture AND interception,
    current paths and pre-rename ones — pruning any matcher-groups and event
    arrays we thereby empty. Returns count removed. Leaves everything else,
    including other people's UserPromptSubmit hooks, untouched."""
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
                if isinstance(cmd, dict) and is_ours(cmd.get("command")):
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


def add_intercept_hook(settings):
    """Register the pre-send interceptor. Separate from the recorder hooks: the
    recorder is passive and always wanted, whereas interception changes what
    Claude sees and a user may reasonably run one without the other."""
    hooks = settings.setdefault("hooks", {})
    groups = hooks.get("UserPromptSubmit") or []
    kept = []
    for g in groups:
        inner = [h for h in (g.get("hooks") or [])
                 if not is_ours(h.get("command"))]
        if inner:
            kept.append(dict(g, hooks=inner))
    kept.append({"hooks": [{"type": "command", "command": intercept_command(),
                            "timeout": 15}]})
    hooks["UserPromptSubmit"] = kept


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

# What gets deployed, and where it comes from. hooks/ holds the two hook
# entrypoints; the engine and the catalogue live at the repo root because both
# the hooks and the research scripts depend on them and neither owns them.
RUNTIME_FILES = [
    ("hooks/recorder.py", "hooks/recorder.py", 0o755),
    ("hooks/intercept.py", "hooks/intercept.py", 0o755),
    ("rules_engine.py", "rules_engine.py", 0o644),
    ("rules.json", "rules.json", 0o644),
]

# Slash commands, deployed into <claude_dir>/commands/ — the same scope the
# hooks are registered in, so a --project install does not leak a command into
# every other project.
#
# These were not installed at all until 2026-08-25. `/ds` worked only where
# someone had copied it by hand, which meant a fresh install had the hooks, the
# catalogue and no way to record a retrospective marker — the one thing the
# corpus cannot reconstruct for itself.
COMMAND_FILES = [
    ("commands/ds.md", "ds.md", 0o644),
]


ENVFILE_TEMPLATE = """\
# Credentials for claude-resync's API-drafted fixes.
# Read by intercept.py because a hook does not inherit your shell's exports.
# A real exported variable always wins over this file.
#
# ANTHROPIC_API_KEY=sk-ant-...
#
# Turn every outbound call off without editing the catalogue:
# CLAUDE_RESYNC_API=0
"""


def venv_dir(dest_dir):
    return os.path.join(dest_dir, ".venv")


def venv_python(dest_dir):
    return os.path.join(venv_dir(dest_dir), "bin", "python")


def sdk_present(dest_dir):
    """Whether intercept.py will be able to import anthropic.

    Mirrors intercept.import_anthropic(): the interpreter version is part of the
    path, so a venv built against another python does not count."""
    site = os.path.join(venv_dir(dest_dir), "lib",
                        "python%d.%d" % sys.version_info[:2], "site-packages")
    return os.path.isdir(os.path.join(site, "anthropic"))


def ensure_sdk(dest_dir):
    """Create ~/.claude-resync/.venv and install the anthropic SDK.

    A venv rather than the system python because homebrew's is PEP 668
    externally-managed and refuses `pip install`. Best-effort by design: without
    the SDK every API-drafted fix falls back to its template, so a machine with
    no network still gets a working install. Returns a status string."""
    if sdk_present(dest_dir):
        return "already present"
    try:
        if not os.path.isdir(venv_dir(dest_dir)):
            subprocess.run([sys.executable, "-m", "venv", venv_dir(dest_dir)],
                           check=True, capture_output=True, timeout=120)
        subprocess.run([os.path.join(venv_dir(dest_dir), "bin", "pip"),
                        "install", "--quiet", "anthropic"],
                       check=True, capture_output=True, timeout=300)
    except FileNotFoundError:
        return "SKIPPED (no venv/pip available)"
    except subprocess.TimeoutExpired:
        return "SKIPPED (timed out)"
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return "SKIPPED (%s)" % (tail[-1][:70] if tail else "pip failed")
    return "installed" if sdk_present(dest_dir) else "SKIPPED (import path mismatch)"


def ensure_envfile(dest_dir):
    """Write the .env template if absent. Never overwrites — it holds a key."""
    p = os.path.join(dest_dir, ".env")
    if os.path.exists(p):
        return "exists (left alone)"
    try:
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(ENVFILE_TEMPLATE)
        os.chmod(p, 0o600)
    except OSError as exc:
        return "could not create: %s" % exc
    return "created (add your key)"


def repo_root():
    """This installer lives at the repo root.

    It was in `hooks/` until 2026-08-25, which was wrong: it deploys the hooks
    AND `rules_engine.py` AND `rules.json` AND the slash commands, so it is not
    a hook — it is the installer for all of them, and burying it under one of
    the things it installs made the layout read backwards."""
    return os.path.dirname(os.path.abspath(__file__))


def deploy_commands(project):
    """Copy the slash commands into the same scope the hooks were registered in.

    Scope matters: a default install registers hooks in `~/.claude` and the
    commands belong there too, while `--project` keeps both inside that
    project. Installing a command globally for a project-scoped recorder would
    put `/ds` in every unrelated project, where it would write markers into a
    corpus that is not recording."""
    root = repo_root()
    dest_dir = os.path.join(claude_dir(project), "commands")
    os.makedirs(dest_dir, exist_ok=True)
    out = []
    for rel, name, mode in COMMAND_FILES:
        src = os.path.join(root, rel)
        if not os.path.exists(src):
            raise SystemExit("ERROR: %s not found in the repo (%s)."
                             % (rel, src))
        dest = os.path.join(dest_dir, name)
        shutil.copyfile(src, dest)
        os.chmod(dest, mode)
        out.append(dest)
    return out


def deploy_runtime(dest_dir):
    """Copy the runtime from the repo into the global home.

    Every hook entrypoint plus everything it imports, so a deployed install has
    no dependency on the repo remaining in place. Returns the deployed paths."""
    root = repo_root()
    os.makedirs(dest_dir, exist_ok=True)
    out = []
    for rel, name, mode in RUNTIME_FILES:
        src = os.path.join(root, rel)
        if not os.path.exists(src):
            raise SystemExit("ERROR: %s not found in the repo (%s)." % (rel, src))
        dest = os.path.join(dest_dir, name)
        os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
        shutil.copyfile(src, dest)
        os.chmod(dest, mode)
        out.append(dest)
    # Both trees, every time: an install that creates only what it happens to
    # write leaves the other half to be created by whoever touches it first,
    # with whatever permissions they choose.
    for d in (os.path.join(dest_dir, "data"),
              os.path.join(dest_dir, "cache"),
              os.path.join(analysis_dir(), "data"),
              os.path.join(analysis_dir(), "views"),
              os.path.join(analysis_dir(), "verdicts")):
        os.makedirs(d, mode=0o700, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    return out


def deploy_recorder(dest_dir):
    """Retained name used by cmd_install; deploys the whole runtime now."""
    return deploy_runtime(dest_dir)[0]


def catalogue_drift(dest_dir):
    """True when the deployed rules.json differs from the repo's. The repo copy
    is canonical, so drift means the running catalogue is stale."""
    import hashlib
    def h(p):
        try:
            with open(p, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        except Exception:
            return None
    a = h(os.path.join(repo_root(), "rules.json"))
    b = h(os.path.join(dest_dir, "rules.json"))
    if a is None or b is None:
        return None
    return a != b


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

def cmd_deploy(args):
    """Copy the runtime into ~/.claude-resync and change nothing else.

    `install` deploys too, but it also rewrites settings.json, strips and
    re-adds every hook entry, and ensures the corpus, the .env and the SDK —
    far too much to run after editing one rule. Without a deploy-only path the
    temptation is to `cp` the four files by hand, and that is how the repo and
    the runtime drift: a hand-copy misses a file, or skips the chmod that makes
    intercept.py executable, and the hook goes on running the old code while
    the repo looks correct.

    The repo is the source of truth; this is the only sanctioned way to move it
    into place."""
    moved, skipped = migrate_layout()
    dest = recorder_dir()
    deployed = deploy_runtime(dest)
    commands = deploy_commands(args.project)
    for a, b in moved:
        print("moved  %s -> %s" % (a, b))
    for a, b in skipped:
        print("KEPT   %s (destination %s already exists — resolve by hand)" % (a, b))
    print("runtime deployed from %s" % repo_root())
    print("  to %s" % dest)
    for d in deployed:
        print("     %s" % os.path.basename(d))
    print("  commands to %s" % os.path.dirname(commands[0]))
    for d in commands:
        print("     %s" % os.path.basename(d))
    print("\nsettings untouched. Use `install` to (re-)register hooks,")
    print("and `intercept.py --status` to confirm what is live.")


def cmd_install(args):
    project = args.project          # None => global registration
    is_global = project is None
    cdir = claude_dir(project)
    spath = settings_path(project)

    if is_global:
        print("!! GLOBAL install: this records EVERY Claude Code session on this")
        print("!! machine, across all projects (including their secrets).")
        print("!! Use --project PATH to restrict recording to one project.\n")

    # 0. move an old flat install into collection/ + analysis/ first, so the
    #    deploy below writes beside the migrated data rather than beside a
    #    half-moved copy of it.
    moved, skipped = migrate_layout()

    # 1. deploy the runtime + slash commands + prepare the global corpus
    deployed = deploy_runtime(recorder_dir())
    commands = deploy_commands(project)
    dest = deployed[0]
    cpath = corpus_path()
    ensure_corpus(cpath)
    # Root, not collection/: intercept.py reads .env from HOME_DIR and the
    # analysis scripts import the same SDK. Shared, so neither half owns it.
    env_state = ensure_envfile(TRANSCRIPTS_HOME)
    sdk_state = ("SKIPPED (--no-sdk)" if args.no_sdk
                 else ensure_sdk(TRANSCRIPTS_HOME))

    # 2. merge hooks (strip-then-add makes this idempotent)
    settings = load_settings(spath)
    removed = strip_our_hooks(settings)
    add_our_hooks(settings)
    if not args.no_intercept:
        add_intercept_hook(settings)

    # 3. optional compaction window
    warnings = []
    if args.window is not None:
        warnings += validate_window(args.window)
        settings.setdefault("env", {})["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(args.window)
        warnings += check_local_shadow(cdir)

    write_settings(spath, settings)

    # 4. report
    print("claude-resync installed.")
    print("  settings : %s" % spath)
    print("  runtime  : %s" % os.path.dirname(dest))
    for d in deployed:
        print("             %s" % os.path.basename(d))
    print("  intercept: %s" % ("registered" if not args.no_intercept
                               else "NOT registered (--no-intercept)"))
    print("  sdk      : %s  (%s)" % (sdk_state, venv_dir(TRANSCRIPTS_HOME)))
    print("  .env     : %s  (%s)"
          % (env_state, os.path.join(TRANSCRIPTS_HOME, ".env")))
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
            is_ours(c.get("command")) and "recorder.py" in (c.get("command") or "")
            for g in groups if isinstance(g, dict)
            for c in g.get("hooks", []) if isinstance(c, dict)
        )
        print("  [%s] %s" % ("x" if found else " ", event))

    win = settings.get("env", {}).get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") \
        if isinstance(settings.get("env"), dict) else None
    print("Compaction window (CLAUDE_CODE_AUTO_COMPACT_WINDOW): %s"
          % (win if win else "unset (default)"))

    ihooks = hooks.get("UserPromptSubmit", []) if isinstance(hooks, dict) else []
    ifound = any(is_ours(c.get("command")) and "intercept.py" in (c.get("command") or "")
                 for g in ihooks if isinstance(g, dict)
                 for c in g.get("hooks", []) if isinstance(c, dict))
    print("Pre-send interceptor:")
    print("  [%s] UserPromptSubmit" % ("x" if ifound else " "))

    rdir = recorder_dir()
    drift = catalogue_drift(rdir)
    print("  catalogue  : %s"
          % ("DRIFTED from the repo — re-run install" if drift
             else "in sync with the repo"))
    # TRANSCRIPTS_HOME, not rdir: cmd_install writes both to the root, where
    # collection and analysis share them. Asking collection/ for them reported
    # a working install as broken.
    print("  sdk        : %s" % ("present" if sdk_present(TRANSCRIPTS_HOME)
                                 else "missing — API fixes use templates"))
    print("  .env       : %s" % ("present" if os.path.exists(
        os.path.join(TRANSCRIPTS_HOME, ".env")) else "absent"))
    print("  (rule routing, fire and verdict counts: "
          "python3 %s/intercept.py --status)" % hooks_dir())

    # A registered hook whose script is missing is the one failure this tool
    # cannot see by itself: every hook command ends in `|| true`, so a dead
    # path records nothing and reports nothing. Check the file, not the string.
    missing = sorted({c for c in registered_commands(hooks)
                      if not os.path.exists(script_of(c))})
    if missing:
        print("  !! REGISTERED BUT MISSING — these hooks are silently no-ops:")
        for c in missing:
            print("     %s" % script_of(c))
        print("     re-run `install.py install` to re-point them.")

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
    sp.add_argument("--no-intercept", action="store_true",
                    help="deploy and register capture only, leaving the "
                         "pre-send interceptor unregistered")
    sp.add_argument("--no-sdk", action="store_true",
                    help="skip creating .venv / installing the anthropic SDK. "
                         "API-drafted fixes then fall back to templates")
    sp.add_argument("--window", type=int, default=None,
                    help="set CLAUDE_CODE_AUTO_COMPACT_WINDOW (>= %d; %d+ recommended)"
                         % (WINDOW_HARD_FLOOR, WINDOW_SAFE))
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser(
        "deploy", help="copy the runtime into ~/.claude-resync, touching no "
                       "settings — use after editing rules.json or the engine")
    add_target(sp)          # commands follow the scope the hooks were put in
    sp.set_defaults(func=cmd_deploy)

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
