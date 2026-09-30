#!/usr/bin/python3 -I
"""File Vault's engine: the config file, and every file and git operation.

Run by the shell as an argv array, never through a shell:

    /usr/bin/python3 -I -S engine.py load
    /usr/bin/python3 -I -S engine.py save                          config JSON on stdin
    /usr/bin/python3 -I -S engine.py connect <repo-url>
    /usr/bin/python3 -I -S engine.py copy [--adopt] [--secrets] <repo-url> <vault> -- <path>...
    /usr/bin/python3 -I -S engine.py push <repo-url>
    /usr/bin/python3 -I -S engine.py status <repo-url>
    /usr/bin/python3 -I -S engine.py check <repo-url>
    /usr/bin/python3 -I -S engine.py folder <repo-url>
    /usr/bin/python3 -I -S engine.py reset
    /usr/bin/python3 -I -S engine.py list [--secrets] <vault> -- <path>...   in a terminal
    /usr/bin/python3 -I -S engine.py diff <repo-url> <vault>                 in a terminal

load prints {"config": {...}, "problems": [...], "exists": bool, "machine":
{"id", "name"}}: the config with anything invalid left out, what was left
out and why, whether config.json exists, and this machine. A config.json
that isn't JSON is an error (exit 3), so a typo made in an editor is
reported, never saved over. save checks the JSON on stdin and writes it to
~/.config/file-vault/config.json (0600, atomically). folder prints the
local copy of the repository's path, or exits 4 before the first connect.

connect checks the repository can be used and clones or updates the local
copy; copy (the first sync button) mirrors a vault's picked paths into it
and stages them; push (the second) commits and pushes; status reports what
waits to be pushed; check reports, per vault of this machine, the files
changed since its last copy (read-only, local). --adopt lets this machine copy into a vault made on
another one; --secrets copies files that look like secrets too. See fv/ for
each; fv/__init__.py lists the modules.

connect, copy, push, status, check and reset print one JSON object per line, and
nothing else on stdout:

    {"event": "step",      "text": "Copying changed files"}
    {"event": "vault",     "name": ..., "defined": bool, "sources": [...],
                           "machine": {"id", "name"}, "pushedAt": seconds}   one per vault, then
    {"event": "connected", "branch": "main", "empty": false, "vaults": n}
    {"event": "copied",    "files": n, "bytes": n, "changed": n,
                           "skipped": ["/path: reason", ...], "skippedCount": n}
    {"event": "pushed",    "vaults": [...], "commit": "abc1234"}
    {"event": "status",    "cloned": bool, "pending": {vault: n}, "unpushed": [vault, ...],
                           "outgoing": {vault: n}, "pushedAt": {vault: seconds}}
    {"event": "checked",   "stale": {vault: n}, "failed": {vault: reason}}
    {"event": "reset",     "removed": [path, ...]}
    {"event": "error",     "message": ...}
"""

import os
import signal
import sys

# Isolated mode (-I) leaves the script's folder off sys.path; the fv
# package is this plugin's own code, beside this file.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from fv import common, config, places, sync, views  # noqa: E402


def on_term(signum, frame):
    raise SystemExit(143)


def reporting(fn, *args):
    """Runs a JSON-lines command under the lock; a failure becomes an error
    event, an unexpected one included, rather than a traceback."""
    try:
        with places.exclusive():
            fn(*args)
    except common.Failure as e:
        sync.emit({"event": "error", "message": common.plain(str(e), 300)})
        return 1
    except OSError as e:
        sync.emit({"event": "error", "message": common.plain(f"{e.filename or ''}: {e.strerror or e}", 300)})
        return 1
    except Exception as e:   # a bug: say what, briefly
        sync.emit({"event": "error", "message": common.plain(f"Unexpected {type(e).__name__}: {e}", 300)})
        return 1
    return 0


def reset():
    sync.emit({"event": "reset", "removed": places.reset()})


def options(args, allowed):
    """The flags from `allowed` at the start of `args`, and the rest."""
    flags = set()
    while args and args[0] in allowed:
        flags.add(args[0])
        args = args[1:]
    return flags, args


def main(argv):
    signal.signal(signal.SIGTERM, on_term)
    command, args = (argv[0], argv[1:]) if argv else ("", [])
    if command == "load" and not args:
        try:
            sync.emit(config.load())
        except common.Failure as e:
            print(e, file=sys.stderr)
            return 3
        return 0
    if command == "save" and not args:
        try:
            config.save(sys.stdin.buffer.read(common.CONFIG_MAX + 1))
        except (common.Failure, OSError) as e:
            print(e, file=sys.stderr)
            return 3
        return 0
    if command == "reset" and not args:
        return reporting(reset)
    if command == "folder" and len(args) == 1:
        url = common.repo_url(args[0])
        path = places.mirror_dir(url) if url else ""
        if not path or not os.path.isdir(os.path.join(path, ".git")):
            return 4
        print(path)
        return 0
    if command == "list":
        flags, args = options(args, {"--secrets"})
        if len(args) >= 2 and args[1] == "--":
            vault, sources = common.vault_name(args[0]), args[2:]
            clean = [common.source_path(s) for s in sources]
            if vault and len(sources) <= common.MAX_SOURCES and "" not in clean:
                views.page(views.listing(vault, clean, "--secrets" in flags))
                return 0
        print("Invalid vault or path.", file=sys.stderr)
        return 2
    if command == "connect" and len(args) == 1:
        url = common.repo_url(args[0])
        if not url:
            sync.emit({"event": "error", "message": "Use a URL like https://github.com/you/backups."})
            return 2
        return reporting(sync.connect, url)
    if command == "copy":
        flags, args = options(args, {"--adopt", "--secrets"})
        if len(args) >= 3 and args[2] == "--":
            url, vault, sources = common.repo_url(args[0]), common.vault_name(args[1]), args[3:]
            clean = [common.source_path(s) for s in sources]
            if url and vault and len(sources) <= common.MAX_SOURCES and "" not in clean:
                return reporting(sync.copy, url, vault, clean, "--adopt" in flags, "--secrets" in flags)
        sync.emit({"event": "error", "message": "Invalid repository, vault or path."})
        return 2
    if command in ("push", "status", "check") and len(args) == 1:
        url = common.repo_url(args[0])
        if not url:
            sync.emit({"event": "error", "message": "Invalid repository."})
            return 2
        return reporting({"push": sync.push, "status": sync.status, "check": sync.check}[command], url)
    if command == "diff" and len(args) == 2:
        url, vault = common.repo_url(args[0]), common.vault_name(args[1])
        if not url or not vault:
            print("Invalid repository or vault.", file=sys.stderr)
            return 2
        views.diff(url, vault)
        return 0
    print(__doc__.strip().splitlines()[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
