"""Where File Vault keeps its data on this machine, the lock that lets one
command at a time use it, and reset, which deletes all of it."""

import contextlib
import fcntl
import os
import pwd
import shutil
import stat
import time

from . import common

LOCK_WAIT = 60             # seconds to wait for another File Vault command


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
        raise common.Failure(f"{path} is not a folder of this user.")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def work_dir():
    """Private scratch space for File Vault: never inside a repository's work
    tree, so nothing left there by a killed run can be committed."""
    path = os.path.join(private_data_dir(), "tmp")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def mirror_dir(url):
    owner, repo = url[len("https://github.com/"):-len(".git")].split("/")
    return os.path.join(data_dir(), "repos", owner, repo)


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
                    raise common.Failure("Another File Vault operation is still running. Try again when it ends.")
                time.sleep(0.2)
        yield
    finally:
        os.close(fd)


def reset():
    """Deletes everything File Vault keeps on this machine: its settings
    (~/.config/file-vault) and its local copies of repositories
    (~/.local/share/file-vault), including anything copied but not pushed.
    GitHub and the plugin itself are not touched. A symlink in place of
    either folder is removed, never followed. Returns what was removed."""
    removed = []
    for path in (config_dir(), data_dir()):
        if not os.path.lexists(path):
            continue
        st = os.lstat(path)
        if stat.S_ISDIR(st.st_mode) and st.st_uid != os.geteuid():
            raise common.Failure(f"{path} doesn't belong to this user; not deleted.")

        # Read-only folders copied in (mode 0555) would stop rmtree.
        def unlock(fn, target, _):
            os.chmod(os.path.dirname(target), 0o700)
            fn(target)

        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(path, onexc=unlock)
        else:
            os.unlink(path)
        removed.append(path)
    return removed
