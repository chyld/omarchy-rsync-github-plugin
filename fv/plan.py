"""What a copy takes: every file and symlink under the picked paths, with
what is skipped and why, then rsync copying exactly that list and anything
else removed, so the vault folder stays an exact copy. compare counts what
such a copy would change, without copying.

While planning: a .git folder inside a picked folder is skipped (git would
store only a pointer to that repository); symlinks inside a picked folder
are stored as links (a picked path that is itself a symlink is followed);
files over 100 MB (GitHub refuses them), unreadable files and sockets,
FIFOs and devices are skipped and reported. So are files that look like
secrets, unless the vault includes secrets: everything copied is pushed to
GitHub as it is, unencrypted."""

import filecmp
import os
import re
import secrets
import shutil
import stat

from . import common, places, proc

# ------------------------------------------------------------ secrets

# Private keys and credential files by name. A heuristic, not a guarantee:
# it catches the usual places, not a token pasted into any file.
SECRET_NAMES = {
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk",
    ".netrc", ".git-credentials", ".pgpass", ".env", "secring.gpg",
}
SECRET_SUFFIXES = (".key", ".p12", ".pfx", ".jks", ".keystore")
ENV_EXAMPLES = (".example", ".sample", ".template", ".dist")
# Paths ending so hold login tokens.
SECRET_TAILS = ("/.aws/credentials", "/.config/gh/hosts.yml", "/.docker/config.json", "/.kube/config")
# Folders whose every file is secret.
SECRET_FOLDERS = ("/.gnupg/private-keys-v1.d/",)
# The start of a PEM or OpenSSH private key, or a PGP private key block.
PRIVATE_KEY = re.compile(rb"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY")
HEAD = 4096                # bytes read from each file to look for a key


def secret_by_name(path):
    """Why `path` looks like a secret from its name alone, or ""."""
    name = path.rsplit("/", 1)[-1]
    if name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES):
        return "looks like a secret (credentials or a private key)"
    if name.startswith(".env.") and not name.endswith(ENV_EXAMPLES):
        return "looks like a secret (credentials or a private key)"
    if path.endswith(SECRET_TAILS) or any(f in path for f in SECRET_FOLDERS):
        return "looks like a secret (a login token or key)"
    return ""


def secret_in(head):
    """Why the first bytes of a file look like a secret, or ""."""
    return "looks like a secret (a private key)" if PRIVATE_KEY.search(head) else ""


# ------------------------------------------------------------ planning

class Plan:
    """What a sync copies. Reads only the first few KB of each file, to look
    for a private key; rsync then copies exactly this list."""

    def __init__(self, exclude, include_secrets=False):
        self.exclude = exclude     # never copied: the plugin's own data
        self.include_secrets = include_secrets
        self.entries = []          # (path, size, link target or None, followed)
        self.over = ""             # the limit a sync would stop at
        self.files = 0
        self.bytes = 0
        self.skipped = []
        self.following = False     # inside a picked path that is a symlink

    def skip(self, path, reason):
        self.skipped.append(common.plain(path, 300) + ": " + reason)

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
        if st.st_size > common.LIMIT_BYTES:
            return self.skip(path, f"over 100 MB ({st.st_size // common.MiB} MB)")
        if not self.include_secrets and secret_by_name(path):
            return self.skip(path, secret_by_name(path))
        try:
            flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | (0 if follow else os.O_NOFOLLOW)
            fd = os.open(path, flags)
        except OSError as e:
            return self.skip(path, e.strerror or "can't read")
        try:
            real = os.fstat(fd)
            head = b"" if self.include_secrets or not stat.S_ISREG(real.st_mode) else os.read(fd, HEAD)
        except OSError as e:
            return self.skip(path, e.strerror or "can't read")
        finally:
            os.close(fd)
        if not stat.S_ISREG(real.st_mode) or real.st_size > common.LIMIT_BYTES:
            return self.skip(path, "changed while copying")
        if secret_in(head):
            return self.skip(path, secret_in(head))
        self.count(real.st_size)
        self.entries.append((path, real.st_size, None, self.following))

    def count(self, size):
        self.files += 1
        self.bytes += size
        if self.files > common.MAX_FILES:
            reason = f"More than {common.MAX_FILES:,} files in this vault. Pick fewer or smaller folders."
        elif self.bytes > common.MAX_BYTES:
            reason = "More than 2 GB in this vault. Pick fewer or smaller folders."
        else:
            reason = ""
        self.over = self.over or reason


def plan(sources, include_secrets=False):
    p = Plan(places.data_dir(), include_secrets)
    for src in sources:
        p.add_source(src)
    return p


# ------------------------------------------------------------ copying

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
        raise common.Failure(p.over)
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
    listing = os.path.join(places.work_dir(), secrets.token_hex(8) + ".files")
    fd = os.open(listing, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b"\0".join(path.lstrip("/").encode("utf-8", "surrogateescape") for path in paths))
        code, _, err = proc.run([RSYNC, *RSYNC_ARGS, *extra, "--files-from=" + listing, "--", "/", dest + "/"],
                                timeout=COPY)
    finally:
        os.unlink(listing)
    # 23 and 24: some files couldn't be read or vanished after planning; the
    # rest were copied. Anything else is a failure.
    if code in (23, 24):
        for line in err.decode("utf-8", "replace").splitlines()[:common.MAX_LISTED]:
            if line.startswith("rsync:") and "some files" not in line:
                p.skipped.append(common.plain(line[len("rsync:"):].strip(), 300))
        return
    if code != 0:
        text = err.decode("utf-8", "replace").strip().splitlines()
        raise common.Failure("Copying failed: " + common.plain(text[0] if text else f"rsync exited {code}", 200))


def compare(dest, p, keep=()):
    """How many paths a mirror of plan `p` into `dest` would add, change or
    remove, touching nothing: the check for files changed since the last
    copy. Size and time first, as rsync does; a file whose time alone
    differs is compared byte for byte, so a file only touched, or written
    again as it was, doesn't count."""
    if p.over:
        raise common.Failure(p.over)
    planned = set()
    changed = 0
    for path, _, link, followed in p.entries:
        rel = path.lstrip("/")
        planned.add(rel)
        if differs(path, os.path.join(dest, rel), link, followed):
            changed += 1
    return changed + leftovers(dest, planned | set(keep))


def differs(src, dst, link, followed):
    """Whether the copy of `src` at `dst` would change. A symlink inside a
    picked folder is compared as a link, anything under a picked symlink as
    what it points at (see mirror)."""
    try:
        d = os.lstat(dst)
    except FileNotFoundError:
        return True
    if link is not None and not followed:
        try:
            return not stat.S_ISLNK(d.st_mode) or os.readlink(dst) != link
        except OSError:
            return True
    try:
        s = os.stat(src)
    except OSError:
        return False               # gone since planning: a copy skips it too
    if not stat.S_ISREG(s.st_mode):
        return False
    if not stat.S_ISREG(d.st_mode):
        return True
    # git keeps a file's execute bit, and nothing else of its mode.
    if s.st_size != d.st_size or (s.st_mode ^ d.st_mode) & stat.S_IXUSR:
        return True
    if s.st_mtime_ns == d.st_mtime_ns:
        return False
    try:
        return not filecmp.cmp(src, dst, shallow=False)
    except OSError:
        return True


def leftovers(dest, keep):
    """Files and links under `dest` that prune would remove."""
    def walk(path, rel):
        n = 0
        try:
            entries = list(os.scandir(path))
        except (FileNotFoundError, NotADirectoryError):
            return 0
        for entry in entries:
            child_rel = rel + "/" + entry.name if rel else entry.name
            if entry.is_dir(follow_symlinks=False):
                n += walk(entry.path, child_rel)
            elif child_rel not in keep:
                n += 1
        return n

    return walk(dest, "")


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
