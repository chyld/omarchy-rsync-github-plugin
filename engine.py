#!/usr/bin/python3 -I
"""File Vault's engine: the config file, and every file and git operation.

Run by the shell as an argv array, never through a shell:

    /usr/bin/python3 -I -S engine.py load
    /usr/bin/python3 -I -S engine.py save                          config JSON on stdin
    /usr/bin/python3 -I -S engine.py connect <repo-url>
    /usr/bin/python3 -I -S engine.py copy [--adopt] <repo-url> <vault> -- <path>...
    /usr/bin/python3 -I -S engine.py push <repo-url>
    /usr/bin/python3 -I -S engine.py status <repo-url>
    /usr/bin/python3 -I -S engine.py folder <repo-url>
    /usr/bin/python3 -I -S engine.py reset
    /usr/bin/python3 -I -S engine.py list <vault> -- <path>...      in a terminal
    /usr/bin/python3 -I -S engine.py diff <repo-url> <vault>          in a terminal

load prints {"config": {...}, "problems": [...], "exists": bool, "machine":
{"id", "name"}}: the config with anything invalid left out, what was left
out and why, whether config.json exists, and this machine. A config.json that isn't JSON is an error (exit 3), so a
typo made in an editor is reported, never saved over. save checks the JSON
on stdin and writes it to ~/.config/file-vault/config.json (0600,
atomically). folder prints the local copy of the repository's path, or
exits 4 before the first connect. list shows every file a copy would take,
and every path it would skip, in a pager; diff shows a vault's copied
files changed but not pushed yet, or those of its last pushed change.

The local copy of the repository lives in
~/.local/share/file-vault/repos/<owner>/<repo> and belongs to this plugin.

connect checks that the repository can be used (GitHub reachable, logged
in, the repository exists, and this account may push to it, checked with a
dry-run push that sends nothing), then clones or updates the local copy,
and lists the vaults already in it with their definitions
(<vault>/.file-vault.json: the picked paths and the machine they're from).

copy (the first sync button) makes <vault>/ in the local copy an exact copy
of the picked paths, each under its full path (/home/u/.bashrc is saved as
<vault>/home/u/.bashrc), writes its definition, and stages it. It commits
nothing and, unless the local copy has to be cloned first, sends nothing. A vault made on another
machine is refused without --adopt: copying mirrors this machine's files,
so everything that machine had and this one doesn't would be removed.

push (the second) commits what was copied, one commit per vault, brings in
what others pushed (replaying the local commits on top; where both changed
the same vault file, this machine's copy wins) and pushes. It never
force-pushes.

status reports, per vault, changes copied but not pushed. Local only.

reset deletes everything File Vault keeps on this machine: its settings and
its local copies of repositories. GitHub is not touched.

connect, copy, push and status print one JSON object per line, and nothing
else on stdout:

    {"event": "step",      "text": "Copying changed files"}
    {"event": "connected", "branch": "main", "empty": false,
     "vaults": n}, after one {"event": "vault", "name": ..., "defined": bool,
     "sources": [...], "machine": {"id", "name"}, "pushedAt": seconds} per vault
    {"event": "copied",    "files": n, "bytes": n, "changed": n,
                           "skipped": ["/path: reason", ...], "skippedCount": n}
    {"event": "pushed",    "vaults": [...], "commit": "abc1234"}
    {"event": "status",    "cloned": bool, "pending": {vault: n}, "unpushed": [vault, ...]}
    {"event": "reset",     "removed": [path, ...]}
    {"event": "error",     "message": ...}

Copying is planned here and done by rsync, which copies only files whose
size or time changed since the last copy; whatever is no longer picked is
then removed, so <vault>/ stays an exact copy. While planning: a .git folder
inside a picked folder is skipped (git would store only a pointer to that
repository); symlinks inside a picked folder are stored as links (a picked
path that is itself a symlink is followed); files over 100 MB (GitHub
refuses them), unreadable files and sockets, FIFOs and devices are skipped
and reported.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import re
import secrets
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time

GIT = "/usr/bin/git"
KiB = 1024
MiB = KiB * KiB
GiB = KiB * MiB

CONFIG_MAX = 256 * KiB
MAX_VAULTS = 200
MAX_SOURCES = 500
MAX_FILES = 100000         # files in one vault
MAX_BYTES = 2 * GiB        # bytes in one vault
LIMIT_BYTES = 100 * MiB    # GitHub refuses files this large
MAX_LISTED = 20            # skipped paths named in the done event

OUT_CAP = 64 * MiB         # stdout of one git command: a 100,000-file vault's paths fit
ERR_CAP = 64 * KiB         # stderr of one git command
LOCAL = 120                # seconds for a local git command
NETWORK = 600              # seconds for clone, fetch and push
LINE_MAX = 60000           # bytes in one JSON line; the shell reads at most 64 KiB
LOCK_WAIT = 60             # seconds to wait for another File Vault command
STALE = 60                 # seconds after which a git index.lock is taken as left by a killed git

# Predictable git whatever the user's config says: no hooks, no fsmonitor, no
# signing prompt from a background process.
GIT_CONFIG = [
    "-c", "core.quotePath=false", "-c", "color.ui=false", "-c", "core.pager=cat",
    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "gc.auto=0",
    "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", "-c", "advice.detachedHead=false",
]

CONTROL = re.compile("[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]")


class Failure(Exception):
    """A readable reason the sync failed."""


# ------------------------------------------------------------ validation

def repo_url(value):
    """https://github.com/<owner>/<repo>, normalized to ...<repo>.git, or ""."""
    if not isinstance(value, str):
        return ""
    m = re.fullmatch(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?",
                     value.strip())
    if not m or m.group(2) in (".", ".."):
        return ""
    return f"https://github.com/{m.group(1)}/{m.group(2)}.git"


def vault_name(value):
    """yyyy-mm-dd-<name>: a real date, then letters, digits, ., _ or -."""
    if not isinstance(value, str):
        return ""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})-[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value)
    if not m or value.endswith((".", ".lock")):
        return ""
    try:
        time.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return ""
    return value


def source_path(value):
    """An absolute path, not /, with no control characters and no ., .. or
    empty parts; a trailing / is dropped."""
    if not isinstance(value, str) or not 2 <= len(value) <= 4096 or not value.startswith("/"):
        return ""
    if CONTROL.search(value):
        return ""
    s = value.rstrip("/")
    if len(s) < 2 or any(p in ("", ".", "..") for p in s.split("/")[1:]):
        return ""
    return s


def plain(value, cap=200):
    s = CONTROL.sub("", str(value))
    return s if len(s) <= cap else s[:cap - 1] + "\u2026"


# ------------------------------------------------------------ places

def home():
    # $HOME can be set by whoever started us; the passwd entry is the anchor.
    return pwd.getpwuid(os.geteuid()).pw_dir


def config_dir():
    return os.path.join(home(), ".config", "file-vault")


def data_dir():
    return os.path.join(home(), ".local", "share", "file-vault")


def private_data_dir():
    """~/.local/share/file-vault, made private (0700): the local copies hold
    copies of the user's files, some of them secret, so other users must not
    read them even where the home folder is open."""
    path = data_dir()
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        raise Failure(f"{path} is not a folder of this user.")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def work_dir():
    """Private scratch space for File Vault: never inside a repository's work
    tree, so nothing left there by a killed run can be committed."""
    path = os.path.join(private_data_dir(), "tmp")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


@contextlib.contextmanager
def exclusive():
    """One File Vault command at a time on this machine's local copies:
    status, copy, push, connect, diff and reset all use a repository's index,
    and two gits on one index fail. Waits LOCK_WAIT seconds for the other."""
    fd = os.open(os.path.join(private_data_dir(), "lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise Failure("Another File Vault operation is still running. Try again when it ends.")
                time.sleep(0.2)
        yield
    finally:
        os.close(fd)


def mirror_dir(url):
    owner, repo = url[len("https://github.com/"):-len(".git")].split("/")
    return os.path.join(data_dir(), "repos", owner, repo)


# ------------------------------------------------------------ config

def normalize(obj, problems=None):
    """The config with every unknown or invalid part dropped; what was
    dropped, and why, is appended to `problems`."""
    problems = [] if problems is None else problems
    out = {"version": 1, "repoUrl": "", "vaults": []}
    if not isinstance(obj, dict):
        problems.append("config.json is not a JSON object")
        return out
    url = repo_url(obj.get("repoUrl", ""))
    out["repoUrl"] = url[:-len(".git")] if url else ""
    if not url and obj.get("repoUrl"):
        problems.append(f"repoUrl {plain(obj.get('repoUrl'), 80)!r}: not an https://github.com/<owner>/<repo> URL")
    seen = set()
    vaults = obj.get("vaults", [])
    if not isinstance(vaults, list):
        problems.append("vaults: not a list")
        vaults = []
    if len(vaults) > MAX_VAULTS:
        problems.append(f"vaults: only the first {MAX_VAULTS} are used")
    for i, v in enumerate(vaults[:MAX_VAULTS]):
        if not isinstance(v, dict):
            problems.append(f"vault #{i + 1}: not an object")
            continue
        name = vault_name(v.get("name"))
        if not name:
            problems.append(f"vault {plain(v.get('name'), 80)!r}: the name must be yyyy-mm-dd-<name>")
            continue
        if name in seen:
            problems.append(f"vault {name}: listed twice")
            continue
        seen.add(name)
        sources = []
        raw = v.get("sources", [])
        if not isinstance(raw, list):
            problems.append(f"{name}: sources is not a list")
            raw = []
        for s in raw[:MAX_SOURCES]:
            p = source_path(s)
            if not p:
                problems.append(f"{name}: {plain(s, 80)!r} is not an absolute path")
            elif p not in sources:
                sources.append(p)
        last = v.get("lastSync")
        linked = repo_url(v.get("repoUrl", ""))
        if not linked and v.get("repoUrl"):
            problems.append(f"{name}: repoUrl {plain(v.get('repoUrl'), 80)!r} is not a github.com URL; unlinked")
        out["vaults"].append({
            "name": name,
            "repoUrl": linked[:-len(".git")] if linked else "",
            "sources": sources,
            "lastSync": int(last) if isinstance(last, (int, float)) and 0 < last < 2 ** 42 else 0,
            "lastSummary": plain(v.get("lastSummary", ""), 120) if isinstance(v.get("lastSummary"), str) else "",
            # The machine the vault belongs to: "" for one made before this
            # was recorded, which is taken to be this machine's.
            "machine": machine_id(v.get("machine")),
            "machineName": plain(v.get("machineName", ""), 64) if isinstance(v.get("machineName"), str) else "",
        })
    return out


# ------------------------------------------------------------ machines

def machine_id(value):
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16}", value) else ""


def this_machine():
    """{"id", "name"}: a hash of /etc/machine-id (the raw id never leaves the
    machine; the hash is stored in the repository) and the host name."""
    try:
        with open("/etc/machine-id", encoding="ascii") as f:
            raw = f.read(64).strip()
    except (OSError, UnicodeDecodeError):
        raw = ""
    ident = hashlib.sha256(("file-vault:" + raw).encode()).hexdigest()[:16] if raw else ""
    return {"id": ident, "name": plain(socket.gethostname(), 64)}


# Each vault folder holds its own definition, so a new machine connecting to
# the repository knows what the vault is made of and where it came from.
DEFINITION = ".file-vault.json"
DEFINITION_MAX = 256 * KiB


def definition(vault, sources):
    return {"version": 1, "name": vault, "sources": sources, "machine": this_machine()}


def read_definition(data):
    """A vault definition from the repository, checked, or None."""
    if not data or len(data) > DEFINITION_MAX:
        return None
    try:
        obj = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    raw = obj.get("sources")
    sources = []
    for s in raw[:MAX_SOURCES] if isinstance(raw, list) else []:
        p = source_path(s)
        if p and p not in sources:
            sources.append(p)
    m = obj.get("machine") if isinstance(obj.get("machine"), dict) else {}
    name = m.get("name")
    return {"sources": sources, "machine": {"id": machine_id(m.get("id")),
                                            "name": plain(name, 64) if isinstance(name, str) else ""}}


def load():
    path = os.path.join(config_dir(), "config.json")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return {"config": normalize({}), "problems": [], "exists": False, "machine": this_machine()}
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_size > CONFIG_MAX:
            raise Failure("config.json is not a regular file of this user within 256 KB")
        os.set_blocking(fd, True)
        data = os.read(fd, CONFIG_MAX + 1)
    finally:
        os.close(fd)
    try:
        obj = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise Failure("config.json isn't UTF-8 text")
    except json.JSONDecodeError as e:
        raise Failure(f"config.json line {e.lineno}, column {e.colno}: {e.msg}")
    problems = []
    return {"config": normalize(obj, problems), "problems": problems[:MAX_LISTED], "exists": True,
            "machine": this_machine()}


def save(text):
    if len(text) > CONFIG_MAX:
        raise Failure("config too large")
    try:
        cfg = normalize(json.loads(text.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        raise Failure("config is not JSON")
    d = config_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        raise Failure(f"{d} is not a directory of this user")
    tmp = os.path.join(d, ".config." + secrets.token_hex(8) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(d, "config.json"))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return cfg


# ------------------------------------------------------------ running git

def git_error(stderr):
    """A short, readable reason from a failed git command's stderr."""
    s = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else str(stderr)
    checks = [
        (r"could not read Username|Authentication failed|terminal prompts disabled|Invalid username or (password|token)",
         "GitHub login needed. Run gh auth login, then gh auth setup-git, in a terminal."),
        (r"Repository not found|repository '.*' not found", "Repository not found, or your GitHub account can't access it."),
        (r"Could not resolve host|Failed to connect|Connection timed out|Network is unreachable",
         "Can't reach GitHub. Check your connection."),
        (r"Please tell me who you are|empty ident",
         "Git needs your name and email. Run git config --global user.name and user.email."),
        (r"exceeds GitHub's file size limit|GH001", "GitHub refused a large file."),
        (r"Permission to .* denied|The requested URL returned error: 403|write access to repository not granted",
         "Your GitHub account can't push to this repository."),
        (r"rejected|fetch first|non-fast-forward", "GitHub changed during the sync. Sync again."),
        (r"timed out", "A git command took too long and was stopped."),
    ]
    for pattern, message in checks:
        if re.search(pattern, s, re.I):
            return message
    for line in s.splitlines():
        line = re.sub(r"^(fatal|error|remote): ", "", line).strip()
        if line:
            return plain(line, 160)
    return "Git failed."


def git_env():
    env = dict(os.environ)
    env.update({"LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true",
                "GIT_ASKPASS": "/usr/bin/true", "SSH_ASKPASS": "/usr/bin/true"})
    return env


def run(argv, timeout=LOCAL):
    """(code, stdout, stderr) of argv in its own session. The deadline or an
    output cap stops the whole process group."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=git_env(), start_new_session=True)
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "out")
    sel.register(proc.stderr, selectors.EVENT_READ, "err")
    out, err = bytearray(), bytearray()
    deadline = time.monotonic() + timeout
    reason = ""
    try:
        while sel.get_map():
            left = deadline - time.monotonic()
            if left <= 0:
                reason = "timed out"
                break
            for key, _ in sel.select(min(left, 1.0)):
                chunk = os.read(key.fileobj.fileno(), 64 * KiB)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                buf, cap = (out, OUT_CAP) if key.data == "out" else (err, ERR_CAP)
                buf += chunk
                if len(buf) > cap:
                    reason = "output too large"
                    break
            if reason:
                break
    finally:
        sel.close()
        if reason:
            stop(proc)
        try:
            proc.wait(timeout=max(1, deadline - time.monotonic()) if not reason else 5)
        except subprocess.TimeoutExpired:
            stop(proc)
            reason = reason or "timed out"
        proc.stdout.close()
        proc.stderr.close()
    if reason:
        return 124, bytes(out), reason.encode()
    return proc.returncode, bytes(out), bytes(err)


def stop(proc):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue


class Git:
    def __init__(self, repo):
        self.repo = repo

    def run(self, *args, timeout=LOCAL):
        return run([GIT, "-C", self.repo, *GIT_CONFIG, *args], timeout=timeout)

    def ok(self, *args, timeout=LOCAL):
        code, out, err = self.run(*args, timeout=timeout)
        if code != 0:
            raise Failure(git_error(err))
        return out.decode("utf-8", "replace")

    def maybe(self, *args):
        code, out, _ = self.run(*args)
        return out.decode("utf-8", "replace").strip() if code == 0 else ""


# ------------------------------------------------------------ copying

class Plan:
    """What a sync copies: every file and symlink under the picked paths,
    with what is skipped and why. Reads no file contents; each file is opened
    once to check it can be read. rsync then copies exactly this list."""

    def __init__(self, exclude):
        self.exclude = exclude     # never copied: the plugin's own data
        self.entries = []          # (path, size, link target or None, followed)
        self.over = ""             # the limit a sync would stop at
        self.files = 0
        self.bytes = 0
        self.skipped = []
        self.following = False     # inside a picked path that is a symlink

    def skip(self, path, reason):
        self.skipped.append(plain(path, 300) + ": " + reason)

    def add_source(self, path):
        # The plugin's own data (the local copy itself) is never copied; a
        # folder holding it is copied without it, in add_tree.
        if path == self.exclude or path.startswith(self.exclude + "/"):
            return self.skip(path, "File Vault's own data")
        try:
            st = os.stat(path)     # a picked symlink is followed
            self.following = os.path.islink(path)
        except FileNotFoundError:
            return self.skip(path, "not found")
        except OSError as e:
            return self.skip(path, e.strerror or "can't read")
        try:
            if stat.S_ISDIR(st.st_mode):
                return self.add_tree(path)
            return self.add_entry(path, st, follow=True)
        finally:
            self.following = False

    def add_tree(self, path):
        if path == self.exclude:
            return self.skip(path, "File Vault's own data")
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name)
        except OSError as e:
            return self.skip(path, e.strerror or "can't read")
        for entry in entries:
            child = path + "/" + entry.name
            if entry.name == ".git":
                self.skip(child, "a git repository's own folder")
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as e:
                self.skip(child, e.strerror or "can't read")
                continue
            if stat.S_ISDIR(st.st_mode):
                self.add_tree(child)
            else:
                self.add_entry(child, st, follow=False)

    def add_entry(self, path, st, follow):
        if stat.S_ISLNK(st.st_mode):
            try:
                link = os.readlink(path)
            except OSError as e:
                return self.skip(path, e.strerror or "can't read")
            self.count(0)
            self.entries.append((path, 0, link, self.following))
            return
        if not stat.S_ISREG(st.st_mode):
            return self.skip(path, "not a regular file")
        if st.st_size > LIMIT_BYTES:
            return self.skip(path, f"over 100 MB ({st.st_size // MiB} MB)")
        try:
            flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | (0 if follow else os.O_NOFOLLOW)
            fd = os.open(path, flags)
        except OSError as e:
            return self.skip(path, e.strerror or "can't read")
        try:
            real = os.fstat(fd)
        finally:
            os.close(fd)
        if not stat.S_ISREG(real.st_mode) or real.st_size > LIMIT_BYTES:
            return self.skip(path, "changed while copying")
        self.count(real.st_size)
        self.entries.append((path, real.st_size, None, self.following))

    def count(self, size):
        self.files += 1
        self.bytes += size
        if self.files > MAX_FILES:
            reason = f"More than {MAX_FILES:,} files in this vault. Pick fewer or smaller folders."
        elif self.bytes > MAX_BYTES:
            reason = "More than 2 GB in this vault. Pick fewer or smaller folders."
        else:
            reason = ""
        self.over = self.over or reason


def plan(sources):
    p = Plan(data_dir())
    for src in sources:
        p.add_source(src)
    return p


# rsync copies the planned list and nothing else (--files-from, relative to
# /, so every file lands under its full path). -l keeps symlinks as links,
# -p and -t keep permissions and times, so the next sync's quick check
# (size and time) skips files that didn't change. Times are compared to the
# nanosecond (--modify-window=-1): by default rsync compares whole seconds,
# and would miss a same-size edit made within the second of the last sync.
# The owner always keeps write access to what is copied, so git and the
# next sync can replace it.
RSYNC = "/usr/bin/rsync"
RSYNC_ARGS = ["-lpt", "--modify-window=-1", "--from0", "--force", "--chmod=Du+rwx,Fu+rw", "--no-motd", "-q"]
COPY = 1800                # seconds for one rsync run


def mirror(dest, p, keep=()):
    """Makes `dest` an exact copy of plan `p`: rsync copies what changed,
    then anything not in the plan (or in `keep`, paths relative to `dest`)
    is removed. Returns the plan."""
    if p.over:
        raise Failure(p.over)
    os.makedirs(dest, exist_ok=True)
    # Paths under a picked symlink are copied through it (-L): the link was
    # picked for what it points at.
    for group, extra in ((False, []), (True, ["-L"])):
        paths = [e[0] for e in p.entries if e[3] == group]
        if paths:
            rsync(paths, dest, extra, p)
    prune(dest, {e[0].lstrip("/") for e in p.entries} | set(keep))
    return p


def rsync(paths, dest, extra, p):
    listing = os.path.join(work_dir(), secrets.token_hex(8) + ".files")
    fd = os.open(listing, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b"\0".join(path.lstrip("/").encode("utf-8", "surrogateescape") for path in paths))
        code, _, err = run([RSYNC, *RSYNC_ARGS, *extra, "--files-from=" + listing, "--", "/", dest + "/"],
                           timeout=COPY)
    finally:
        os.unlink(listing)
    # 23 and 24: some files couldn't be read or vanished after planning; the
    # rest were copied. Anything else is a failure.
    if code in (23, 24):
        for line in err.decode("utf-8", "replace").splitlines()[:MAX_LISTED]:
            if line.startswith("rsync:") and "some files" not in line:
                p.skipped.append(plain(line[len("rsync:"):].strip(), 300))
        return
    if code != 0:
        text = err.decode("utf-8", "replace").strip().splitlines()
        raise Failure("Copying failed: " + plain(text[0] if text else f"rsync exited {code}", 200))


def prune(dest, keep):
    """Removes everything under `dest` that isn't a planned path, or a folder
    holding one. Never follows a symlink."""
    folders = set()
    for rel in keep:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            folders.add("/".join(parts[:i]))

    def walk(path, rel):
        for entry in os.scandir(path):
            child_rel = rel + "/" + entry.name if rel else entry.name
            child = os.path.join(path, entry.name)
            if entry.is_dir(follow_symlinks=False):
                if child_rel in folders:
                    walk(child, child_rel)
                else:
                    shutil.rmtree(child)
            elif child_rel not in keep:
                os.unlink(child)

    walk(dest, "")


# ------------------------------------------------------------ sync

def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def step(text):
    emit({"event": "step", "text": text})


def prepare(url):
    """The local copy of the repository, cloned when missing, and its branch."""
    repo = mirror_dir(url)
    if not os.path.isdir(os.path.join(repo, ".git")):
        parent = os.path.dirname(repo)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        if os.path.lexists(repo):
            raise Failure(f"{repo} exists but isn't a git repository. Move it away and sync again.")
        # A clone killed part way leaves its temporary folder: remove those.
        prefix = "." + os.path.basename(repo) + "."
        for name in os.listdir(parent):
            if name.startswith(prefix) and name.endswith(".clone"):
                shutil.rmtree(os.path.join(parent, name), ignore_errors=True)
        step("Cloning from GitHub")
        tmp = os.path.join(parent, prefix + secrets.token_hex(4) + ".clone")
        code, _, err = run([GIT, *GIT_CONFIG, "clone", "-q", "--no-tags", url, tmp], timeout=NETWORK)
        if code != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise Failure(git_error(err))
        os.rename(tmp, repo)
    git = Git(repo)
    origin = git.maybe("config", "--get", "remote.origin.url")
    if repo_url(origin) != url:
        raise Failure(f"{repo} points at another repository. Move it away and sync again.")
    return git


def fetch(git):
    """Fetches from GitHub. A repository cloned while empty has no record of
    GitHub's default branch; once GitHub has one, it is recorded, so this
    machine pushes to it rather than starting a branch of its own. (git 2.48
    and later record it on fetch by themselves; this covers older ones.)"""
    git.ok("fetch", "-q", "--no-tags", "--prune", "origin", timeout=NETWORK)
    if not git.maybe("symbolic-ref", "-q", "refs/remotes/origin/HEAD"):
        git.run("remote", "set-head", "origin", "--auto", timeout=NETWORK)


def branch_of(git):
    head = git.maybe("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head.startswith("origin/"):
        return head[len("origin/"):]
    return git.maybe("symbolic-ref", "--short", "HEAD") or "main"


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def connect(url):
    """Checks access to the repository, then clones or updates the local copy."""
    step("Contacting GitHub")
    code, _, err = run([GIT, *GIT_CONFIG, "ls-remote", "-q", "--heads", url], timeout=NETWORK)
    if code != 0:
        raise Failure(git_error(err))
    git = prepare(url)
    settle(git)
    step("Fetching")
    fetch(git)
    step("Checking push access")
    # A commit of the empty tree, pushed with --dry-run to a branch name no one
    # uses: GitHub checks the login and write access, and nothing is sent. The
    # commit stays a loose object in the plugin's own copy.
    git.ok("hash-object", "-t", "tree", "-w", "/dev/null")
    probe = git.ok("commit-tree", EMPTY_TREE, "-m", "File Vault access check").strip()
    git.ok("push", "--dry-run", "-q", "origin", f"{probe}:refs/heads/file-vault-access-check", timeout=NETWORK)
    branch = branch_of(git)
    remote = f"refs/remotes/origin/{branch}"
    has_remote = bool(git.maybe("rev-parse", "--verify", "-q", remote))
    count = 0
    if has_remote:
        step("Reading the vaults in the repository")
        listed = git.ok("ls-tree", "-z", "-d", "--name-only", remote).split("\0")
        # One event per vault: a vault's picked paths can run to hundreds of
        # kilobytes, more than the shell reads as one line.
        for name in sorted(n for n in listed if vault_name(n))[:MAX_VAULTS]:
            code, out, _ = git.run("show", f"{remote}:{name}/{DEFINITION}")
            found = read_definition(out) if code == 0 else None
            pushed = git.maybe("log", "-1", "--format=%ct", remote, "--", f":(literal){name}")
            vault = {"event": "vault", "name": name, "defined": found is not None,
                     "sources": found["sources"] if found else [],
                     "machine": found["machine"] if found else {"id": "", "name": ""},
                     "pushedAt": int(pushed) if pushed.isdigit() else 0}
            while len(json.dumps(vault)) > LINE_MAX and vault["sources"]:
                vault["sources"] = vault["sources"][:len(vault["sources"]) // 2]   # never in practice
            emit(vault)
            count += 1
    emit({"event": "connected", "branch": branch, "empty": not has_remote, "vaults": count})


def settle(git):
    """Clears anything an interrupted run left half done: a git killed while
    holding the index lock (under `exclusive`, no other File Vault git can
    hold it; an old one is left over), and a rebase stopped part way."""
    gitdir = os.path.join(git.repo, ".git")
    lock = os.path.join(gitdir, "index.lock")
    try:
        if time.time() - os.lstat(lock).st_mtime > STALE:
            os.unlink(lock)
    except FileNotFoundError:
        pass
    if os.path.isdir(os.path.join(gitdir, "rebase-merge")) or os.path.isdir(os.path.join(gitdir, "rebase-apply")):
        git.maybe("rebase", "--abort")


def has_head(git):
    return bool(git.maybe("rev-parse", "--verify", "-q", "HEAD"))


def changed_paths(git, pathspec=None):
    """Paths whose staged state differs from HEAD (or everything staged, on
    a branch with no commits yet)."""
    args = ["diff", "--cached", "--name-only", "-z"]
    if not has_head(git):
        args.append(EMPTY_TREE)
    if pathspec:
        args += ["--", pathspec]
    return [p for p in git.ok(*args).split("\0") if p]


def copy(url, vault, sources, adopt=False):
    """Button 1: the vault's folder in the local copy becomes an exact copy
    of its picked paths (rsync copies only what changed), with its
    definition beside them, and the result is staged. Nothing is committed
    and nothing leaves the machine, unless the local copy has to be cloned
    first.

    A vault made on another machine is refused unless `adopt`: this
    machine's copy would remove every file that machine had and this one
    doesn't, and the next push would take them off GitHub."""
    git = prepare(url)
    settle(git)
    dest = os.path.join(git.repo, vault)
    me = this_machine()
    try:
        with open(os.path.join(dest, DEFINITION), "rb") as f:
            existing = read_definition(f.read(DEFINITION_MAX + 1))
    except OSError:
        existing = None
    owner = existing["machine"] if existing else {"id": "", "name": ""}
    if owner["id"] and me["id"] and owner["id"] != me["id"] and not adopt:
        raise Failure(f"{vault} was made on {owner['name'] or 'another machine'}. Adopt it on this machine "
                      "first: copying would remove every file of it this machine doesn't have.")
    step("Checking files")
    planned = plan(sources)
    step("Copying changed files")
    if os.path.islink(dest) or (os.path.lexists(dest) and not os.path.isdir(dest)):
        os.unlink(dest)
    mirror(dest, planned, keep=[DEFINITION])
    write_definition(dest, definition(vault, sources))
    git.ok("add", "-A", "--", f":(literal){vault}")
    changed = changed_paths(git, f":(literal){vault}")
    emit({"event": "copied", "files": planned.files, "bytes": planned.bytes, "changed": len(changed),
          "skipped": planned.skipped[:MAX_LISTED], "skippedCount": len(planned.skipped)})


def write_definition(dest, obj):
    path = os.path.join(dest, DEFINITION)
    tmp = os.path.join(work_dir(), secrets.token_hex(8) + ".json")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
    # A rename replaces a file or symlink in place, never following it; a
    # folder in the way (from the repository) goes first.
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    os.replace(tmp, path)


def commit_pending(git):
    """One commit per vault with copied changes, then one for anything else
    staged outside the vault folders. Returns the vault names committed."""
    git.ok("add", "-A")
    groups = {}
    for path in changed_paths(git):
        top = path.split("/", 1)[0]
        groups.setdefault(top if vault_name(top) else "", []).append(path)

    def message(what, n):
        return f"{what} \u2014 {n} file{'' if n == 1 else 's'} changed"

    vaults = sorted(t for t in groups if t)
    for top in vaults:
        git.ok("commit", "-q", "--no-verify", "-m", message(f"Sync {top}", len(groups[top])), "--", f":(literal){top}")
    if "" in groups:
        # Whatever is still staged: no pathspec, so however many paths.
        git.ok("commit", "-q", "--no-verify", "-m", message("Sync", len(groups[""])))
    return vaults


def unpushed_vaults(git, remote):
    """Vault folders changed by commits GitHub doesn't have yet."""
    if not has_head(git):
        return []
    rng = f"{remote}..HEAD" if git.maybe("rev-parse", "--verify", "-q", remote) else "HEAD"
    out = git.ok("log", "--format=", "--name-only", "-z", rng)
    return sorted({p.split("/", 1)[0].strip("\n") for p in out.split("\0") if vault_name(p.split("/", 1)[0].strip("\n"))})


def push(url):
    """Button 2: commits what button 1 copied, brings in what others pushed
    (replaying the local commits on top; where both changed the same vault
    file, this machine's copy wins), and pushes. Never force-pushes."""
    git = prepare(url)
    settle(git)
    step("Committing")
    committed = commit_pending(git)
    for attempt in (1, 2):
        step("Fetching from GitHub")
        fetch(git)
        branch = branch_of(git)
        remote = f"refs/remotes/origin/{branch}"
        has_remote = bool(git.maybe("rev-parse", "--verify", "-q", remote))
        if not has_head(git):
            if has_remote:
                git.ok("checkout", "-q", "-B", branch, remote)
            emit({"event": "pushed", "vaults": [], "commit": ""})
            return
        vaults = unpushed_vaults(git, remote)
        if has_remote and git.run("merge-base", "--is-ancestor", remote, "HEAD")[0] != 0:
            step("Bringing in changes from GitHub")
            code, _, err = git.run("rebase", "-q", "-X", "theirs", remote)
            if code != 0:
                git.maybe("rebase", "--abort")
                raise Failure("GitHub has changes that can't be combined with this machine's: " + git_error(err))
        if has_remote and git.maybe("rev-list", "--count", f"{remote}..HEAD") in ("", "0"):
            emit({"event": "pushed", "vaults": [], "commit": ""})
            return
        step("Pushing to GitHub")
        code, _, err = git.run("push", "-q", "origin", f"HEAD:refs/heads/{branch}", timeout=NETWORK)
        if code == 0:
            # GitHub now has HEAD: record that here instead of fetching again.
            git.maybe("update-ref", remote, "HEAD")
            emit({"event": "pushed", "vaults": sorted(set(vaults) | set(committed)),
                  "commit": git.maybe("rev-parse", "--short", "HEAD")})
            return
        if attempt == 2 or not re.search(rb"rejected|fetch first|non-fast-forward", err):
            raise Failure(git_error(err))
        step("GitHub changed; trying again")


def status(url):
    """Per vault: changes copied but not committed, and whether commits wait
    to be pushed. Local only."""
    repo = mirror_dir(url)
    if not os.path.isdir(os.path.join(repo, ".git")):
        emit({"event": "status", "cloned": False, "pending": {}, "unpushed": []})
        return
    git = Git(repo)
    settle(git)
    git.maybe("add", "-A")   # untracked and deleted files count as changes
    pending = {}
    for path in changed_paths(git):
        top = path.split("/", 1)[0]
        if vault_name(top):
            pending[top] = pending.get(top, 0) + 1
    branch = branch_of(git)
    emit({"event": "status", "cloned": True, "pending": pending,
          "unpushed": unpushed_vaults(git, f"refs/remotes/origin/{branch}")})


STATUS_WORDS = {"A": ("added", "32"), "M": ("modified", "33"), "D": ("deleted", "31"), "T": ("modified", "33")}


def file_list(vault, out):
    """git's --name-status -z output as one line per file: what happened to
    it, and its full path as picked (the vault folder dropped, / put back)."""
    tokens = out.split("\0")
    rows = []
    for i in range(0, len(tokens) - 1, 2):
        code, path = tokens[i].strip(), tokens[i + 1]
        word, color = STATUS_WORDS.get(code[:1], ("changed", "0"))
        shown = path[len(vault):] if path.startswith(vault + "/") else "/" + path
        if path == f"{vault}/{DEFINITION}":
            shown = "(the vault's definition: its picked paths)"
        rows.append((word, color, plain(shown, 4096)))
    counts = {}
    for word, _, _ in rows:
        counts[word] = counts.get(word, 0) + 1
    summary = ", ".join(f"{n} {word}" for word, n in sorted(counts.items()))
    lines = [f"  \x1b[{color}m{word:<9}\x1b[0m {path}" for word, color, path in sorted(rows, key=lambda r: r[2])]
    return f"{len(rows)} file{'' if len(rows) == 1 else 's'}: {summary}\n\n" + "\n".join(lines) + "\n"


def diff(url, vault):
    """In a terminal: the files of the vault copied but not pushed yet, or,
    with none, the files of its last pushed change. Names only. The lock is
    held while reading, not while the pager is open."""
    try:
        with exclusive():
            text = diff_text(url, vault)
    except Failure as e:
        text = str(e) + "\n"
    page(text)


def diff_text(url, vault):
    repo = mirror_dir(url)
    if not os.path.isdir(os.path.join(repo, ".git")):
        return "No local copy yet: connect or copy first.\n"
    git = Git(repo)
    settle(git)
    spec = f":(literal){vault}"
    git.maybe("add", "-A", "--", spec)
    names = ["--name-status", "--no-renames", "-z"]
    if changed_paths(git, spec):
        title = f"{vault}: copied, not pushed yet\n"
        out = git.ok("diff", "--cached", *names, *([] if has_head(git) else [EMPTY_TREE]), "--", spec)
    elif has_head(git) and git.maybe("log", "-1", "--format=%h", "--", spec):
        when = git.maybe("log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M", "--", spec)
        title = f"{vault}: nothing waiting. The last push that changed it ({when}):\n"
        out = git.ok("log", "-1", "--format=", *names, "--", spec)
    else:
        return f"{vault}: nothing copied yet.\n"
    return title + file_list(vault, out.lstrip("\n"))


# ------------------------------------------------------------ reset

def reset():
    """Deletes everything File Vault keeps on this machine: its settings
    (~/.config/file-vault) and its local copies of repositories
    (~/.local/share/file-vault), including anything copied but not pushed.
    GitHub and the plugin itself are not touched. A symlink in place of
    either folder is removed, never followed."""
    removed = []
    for path in (config_dir(), data_dir()):
        if not os.path.lexists(path):
            continue
        st = os.lstat(path)
        if stat.S_ISDIR(st.st_mode) and st.st_uid != os.geteuid():
            raise Failure(f"{path} doesn't belong to this user; not deleted.")
        # Read-only folders copied in (mode 0555) would stop rmtree.
        def unlock(fn, target, _):
            os.chmod(os.path.dirname(target), 0o700)
            fn(target)
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(path, onexc=unlock)
        else:
            os.unlink(path)
        removed.append(path)
    emit({"event": "reset", "removed": removed})


# ------------------------------------------------------------ list

def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def listing(vault, sources):
    """What a sync of `vault` would copy and skip, as text."""
    c = plan(sources)
    shown = [plain(p, 4096) for p in sources]
    lines = [f"File Vault \u00b7 {vault}", "",
             f"{len(sources)} picked \u00b7 {c.files:,} file{'' if c.files == 1 else 's'} \u00b7 "
             f"{human(c.bytes)} \u00b7 {len(c.skipped)} skipped"]
    if c.over:
        lines.append("")
        lines.append("A sync would stop here: " + c.over)
    lines += ["", "PICKED", ""]
    lines += ["  " + p + ("/" if os.path.isdir(p) else "") for p in shown]
    lines += ["", "FILES (saved under " + vault + "/<full path>)", ""]
    width = min(100, max([len(plain(p, 4096)) for p, _, _, _ in c.entries] + [20]))
    for path, size, link, _ in c.entries[:MAX_FILES + 1]:
        p = plain(path, 4096)
        lines.append("  " + (f"{p} -> {plain(link, 4096)}" if link is not None else f"{p.ljust(width)}  {human(size):>9}"))
    if c.skipped:
        lines += ["", "SKIPPED", ""]
        lines += ["  " + p for p in c.skipped]
    return "\n".join(lines) + "\n"


def page(text):
    """`text` in less when on a terminal; otherwise printed."""
    if sys.stdout.isatty() and os.path.exists("/usr/bin/less"):
        proc = subprocess.Popen(["/usr/bin/less", "-S", "-R", "--", "-"], stdin=subprocess.PIPE)
        try:
            proc.communicate(text.encode("utf-8", "replace"))
        except BrokenPipeError:
            pass
        return
    sys.stdout.write(text)
    if sys.stdin.isatty():
        input("Press Enter to close.")


# ------------------------------------------------------------ main

def on_term(signum, frame):
    raise SystemExit(143)


def reporting(fn, *args):
    """Runs a JSON-lines command under the lock; a failure becomes an error
    event, an unexpected one included, rather than a traceback."""
    try:
        with exclusive():
            fn(*args)
    except Failure as e:
        emit({"event": "error", "message": plain(str(e), 300)})
        return 1
    except OSError as e:
        emit({"event": "error", "message": plain(f"{e.filename or ''}: {e.strerror or e}", 300)})
        return 1
    except Exception as e:   # a bug: say what, briefly
        emit({"event": "error", "message": plain(f"Unexpected {type(e).__name__}: {e}", 300)})
        return 1
    return 0


def main(argv):
    signal.signal(signal.SIGTERM, on_term)
    if argv[:1] == ["load"] and len(argv) == 1:
        try:
            emit(load())
        except Failure as e:
            print(e, file=sys.stderr)
            return 3
        return 0
    if argv[:1] == ["save"] and len(argv) == 1:
        try:
            save(sys.stdin.buffer.read(CONFIG_MAX + 1))
        except (Failure, OSError) as e:
            print(e, file=sys.stderr)
            return 3
        return 0
    if argv == ["reset"]:
        return reporting(reset)
    if argv[:1] == ["folder"] and len(argv) == 2:
        url = repo_url(argv[1])
        path = mirror_dir(url) if url else ""
        if not path or not os.path.isdir(os.path.join(path, ".git")):
            return 4
        print(path)
        return 0
    if len(argv) >= 2 and argv[0] == "list" and argv[2:3] == ["--"]:
        vault, sources = vault_name(argv[1]), argv[3:]
        clean = [source_path(s) for s in sources]
        if not vault or len(sources) > MAX_SOURCES or "" in clean:
            print("Invalid vault or path.", file=sys.stderr)
            return 2
        page(listing(vault, clean))
        return 0
    if argv[:1] == ["connect"] and len(argv) == 2:
        url = repo_url(argv[1])
        if not url:
            emit({"event": "error", "message": "Use a URL like https://github.com/you/backups."})
            return 2
        return reporting(connect, url)
    adopt = argv[:1] == ["copy"] and "--adopt" in argv[:4]
    if adopt:
        argv = [a for i, a in enumerate(argv) if not (a == "--adopt" and i < 4)]
    if len(argv) >= 4 and argv[0] == "copy" and argv[3] == "--":
        url, vault, sources = repo_url(argv[1]), vault_name(argv[2]), argv[4:]
        clean = [source_path(s) for s in sources]
        if not url or not vault or len(sources) > MAX_SOURCES or "" in clean:
            emit({"event": "error", "message": "Invalid repository, vault or path."})
            return 2
        return reporting(copy, url, vault, clean, adopt)
    if len(argv) == 2 and argv[0] in ("push", "status"):
        url = repo_url(argv[1])
        if not url:
            emit({"event": "error", "message": "Invalid repository."})
            return 2
        return reporting(push if argv[0] == "push" else status, url)
    if len(argv) == 3 and argv[0] == "diff":
        url, vault = repo_url(argv[1]), vault_name(argv[2])
        if not url or not vault:
            print("Invalid repository or vault.", file=sys.stderr)
            return 2
        diff(url, vault)
        return 0
    print(__doc__.strip().splitlines()[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
