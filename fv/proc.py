"""Running git and rsync: each command in its own session under a deadline
and an output cap, with a git whose behaviour the user's config can't
change, and git's errors turned into readable reasons."""

import os
import re
import selectors
import signal
import subprocess
import time

from . import common

GIT = "/usr/bin/git"
LOCAL = 120                # seconds for a local git command
NETWORK = 600              # seconds for clone, fetch and push
OUT_CAP = 64 * common.MiB  # stdout of one command: a 100,000-file vault's paths fit
ERR_CAP = 64 * common.KiB  # stderr of one command

# Predictable git whatever the user's config says: no hooks, no fsmonitor, no
# signing prompt from a background process.
GIT_CONFIG = [
    "-c", "core.quotePath=false", "-c", "color.ui=false", "-c", "core.pager=cat",
    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "gc.auto=0",
    "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", "-c", "advice.detachedHead=false",
]


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
            return common.plain(line, 160)
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
                chunk = os.read(key.fileobj.fileno(), 64 * common.KiB)
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
            raise common.Failure(git_error(err))
        return out.decode("utf-8", "replace")

    def maybe(self, *args):
        code, out, _ = self.run(*args)
        return out.decode("utf-8", "replace").strip() if code == 0 else ""
