"""connect, copy, push and status: everything done to the local copy of the
repository (~/.local/share/file-vault/repos/<owner>/<repo>), which belongs
to this plugin. Each prints JSON lines on stdout and nothing else."""

import json
import os
import re
import secrets
import shutil
import sys
import time

from . import common, config, places, plan, proc

STALE = 60                 # seconds after which a git index.lock is taken as left by a killed git
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
# In the local copy's .git folder, never its work tree: when each vault was
# last pushed, as of one state of GitHub's branch. A cache of git history,
# rebuilt whenever the branch moves.
PUSHED_CACHE = "file-vault-pushed.json"


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def step(text):
    emit({"event": "step", "text": text})


# ------------------------------------------------------------ the local copy

def prepare(url):
    """The local copy of the repository, cloned when missing."""
    repo = places.mirror_dir(url)
    if not os.path.isdir(os.path.join(repo, ".git")):
        parent = os.path.dirname(repo)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        if os.path.lexists(repo):
            raise common.Failure(f"{repo} exists but isn't a git repository. Move it away and sync again.")
        # A clone killed part way leaves its temporary folder: remove those.
        prefix = "." + os.path.basename(repo) + "."
        for name in os.listdir(parent):
            if name.startswith(prefix) and name.endswith(".clone"):
                shutil.rmtree(os.path.join(parent, name), ignore_errors=True)
        step("Cloning from GitHub")
        tmp = os.path.join(parent, prefix + secrets.token_hex(4) + ".clone")
        code, _, err = proc.run([proc.GIT, *proc.GIT_CONFIG, "clone", "-q", "--no-tags", url, tmp], timeout=proc.NETWORK)
        if code != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise common.Failure(proc.git_error(err))
        os.rename(tmp, repo)
    git = proc.Git(repo)
    origin = git.maybe("config", "--get", "remote.origin.url")
    if common.repo_url(origin) != url:
        raise common.Failure(f"{repo} points at another repository. Move it away and sync again.")
    return git


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


def fetch(git):
    """Fetches from GitHub. A repository cloned while empty has no record of
    GitHub's default branch; once GitHub has one, it is recorded, so this
    machine pushes to it rather than starting a branch of its own. (git 2.48
    and later record it on fetch by themselves; this covers older ones.)"""
    git.ok("fetch", "-q", "--no-tags", "--prune", "origin", timeout=proc.NETWORK)
    if not git.maybe("symbolic-ref", "-q", "refs/remotes/origin/HEAD"):
        git.run("remote", "set-head", "origin", "--auto", timeout=proc.NETWORK)


def branch_of(git):
    head = git.maybe("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head.startswith("origin/"):
        return head[len("origin/"):]
    return git.maybe("symbolic-ref", "--short", "HEAD") or "main"


def has_head(git):
    return bool(git.maybe("rev-parse", "--verify", "-q", "HEAD"))


def changed_paths(git, pathspec=None):
    """Paths whose staged state differs from HEAD (or everything staged, on
    a branch with no commits yet). Reads the index only, not the work tree."""
    args = ["diff", "--cached", "--name-only", "-z"]
    if not has_head(git):
        args.append(EMPTY_TREE)
    if pathspec:
        args += ["--", pathspec]
    return [p for p in git.ok(*args).split("\0") if p]


def vaults_on(git, remote):
    """The vault folders on GitHub's branch, as last fetched."""
    listed = git.ok("ls-tree", "-z", "-d", "--name-only", remote).split("\0")
    return sorted(n for n in listed if common.vault_name(n))[:common.MAX_VAULTS]


def pushed_times(git, remote):
    """{vault: seconds}: when each vault on GitHub's branch last changed,
    from git history. Kept in the .git folder for as long as the branch
    stays where it is, so a status read costs one rev-parse."""
    sha = git.maybe("rev-parse", "--verify", "-q", remote)
    if not sha:
        return {}
    cache = os.path.join(git.repo, ".git", PUSHED_CACHE)
    try:
        with open(cache, encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("ref") == sha and isinstance(saved.get("times"), dict):
            return {k: v for k, v in saved["times"].items() if common.vault_name(k) and isinstance(v, int)}
    except (OSError, ValueError, AttributeError):
        pass
    times = {}
    for name in vaults_on(git, remote):
        t = git.maybe("log", "-1", "--format=%ct", remote, "--", f":(literal){name}")
        if t.isdigit():
            times[name] = int(t)
    tmp = os.path.join(git.repo, ".git", PUSHED_CACHE + "." + secrets.token_hex(4) + ".tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as f:
            json.dump({"ref": sha, "times": times}, f)
        os.replace(tmp, cache)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return times


# ------------------------------------------------------------ connect

def connect(url):
    """Checks access to the repository, then clones or updates the local copy."""
    step("Contacting GitHub")
    code, _, err = proc.run([proc.GIT, *proc.GIT_CONFIG, "ls-remote", "-q", "--heads", url], timeout=proc.NETWORK)
    if code != 0:
        raise common.Failure(proc.git_error(err))
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
    git.ok("push", "--dry-run", "-q", "origin", f"{probe}:refs/heads/file-vault-access-check", timeout=proc.NETWORK)
    branch = branch_of(git)
    remote = f"refs/remotes/origin/{branch}"
    has_remote = bool(git.maybe("rev-parse", "--verify", "-q", remote))
    count = 0
    if has_remote:
        step("Reading the vaults in the repository")
        times = pushed_times(git, remote)
        # One event per vault: a vault's picked paths can run to hundreds of
        # kilobytes, more than the shell reads as one line.
        for name in vaults_on(git, remote):
            code, out, _ = git.run("show", f"{remote}:{name}/{config.DEFINITION}")
            found = config.read_definition(out) if code == 0 else None
            vault = {"event": "vault", "name": name, "defined": found is not None,
                     "sources": found["sources"] if found else [],
                     "machine": found["machine"] if found else {"id": "", "name": ""},
                     "pushedAt": times.get(name, 0)}
            while len(json.dumps(vault).encode()) > common.EVENT_MAX and vault["sources"]:
                vault["sources"] = vault["sources"][:len(vault["sources"]) // 2]   # never in practice
            emit(vault)
            count += 1
    emit({"event": "connected", "branch": branch, "empty": not has_remote, "vaults": count})


# ------------------------------------------------------------ copy

def copy(url, vault, sources, adopt=False, include_secrets=False):
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
    me = config.this_machine()
    try:
        with open(os.path.join(dest, config.DEFINITION), "rb") as f:
            existing = config.read_definition(f.read(config.DEFINITION_MAX + 1))
    except OSError:
        existing = None
    owner = existing["machine"] if existing else {"id": "", "name": ""}
    if owner["id"] and me["id"] and owner["id"] != me["id"] and not adopt:
        raise common.Failure(f"{vault} was made on {owner['name'] or 'another machine'}. Adopt it on this machine "
                             "first: copying would remove every file of it this machine doesn't have.")
    step("Checking files")
    planned = plan.plan(sources, include_secrets)
    step("Copying changed files")
    if os.path.islink(dest) or (os.path.lexists(dest) and not os.path.isdir(dest)):
        os.unlink(dest)
    plan.mirror(dest, planned, keep=[config.DEFINITION])
    write_definition(dest, config.definition(vault, sources))
    git.ok("add", "-A", "--", f":(literal){vault}")
    changed = changed_paths(git, f":(literal){vault}")
    emit({"event": "copied", "files": planned.files, "bytes": planned.bytes, "changed": len(changed),
          "skipped": planned.skipped[:common.MAX_LISTED], "skippedCount": len(planned.skipped)})


def write_definition(dest, obj):
    path = os.path.join(dest, config.DEFINITION)
    tmp = os.path.join(places.work_dir(), secrets.token_hex(8) + ".json")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
    # A rename replaces a file or symlink in place, never following it; a
    # folder in the way (from the repository) goes first.
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    os.replace(tmp, path)


# ------------------------------------------------------------ push

def commit_pending(git):
    """One commit per vault with copied changes, then one for anything else
    staged outside the vault folders. Returns the vault names committed."""
    git.ok("add", "-A")
    groups = {}
    for path in changed_paths(git):
        top = path.split("/", 1)[0]
        groups.setdefault(top if common.vault_name(top) else "", []).append(path)

    def message(what, n):
        return f"{what} — {n} file{'' if n == 1 else 's'} changed"

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
    tops = {p.split("/", 1)[0].strip("\n") for p in out.split("\0")}
    return sorted(t for t in tops if common.vault_name(t))


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
                raise common.Failure("GitHub has changes that can't be combined with this machine's: " + proc.git_error(err))
        if has_remote and git.maybe("rev-list", "--count", f"{remote}..HEAD") in ("", "0"):
            emit({"event": "pushed", "vaults": [], "commit": ""})
            return
        step("Pushing to GitHub")
        code, _, err = git.run("push", "-q", "origin", f"HEAD:refs/heads/{branch}", timeout=proc.NETWORK)
        if code == 0:
            # GitHub now has HEAD: record that here instead of fetching again.
            git.maybe("update-ref", remote, "HEAD")
            emit({"event": "pushed", "vaults": sorted(set(vaults) | set(committed)),
                  "commit": git.maybe("rev-parse", "--short", "HEAD")})
            return
        if attempt == 2 or not re.search(rb"rejected|fetch first|non-fast-forward", err):
            raise common.Failure(proc.git_error(err))
        step("GitHub changed; trying again")


# ------------------------------------------------------------ status

def status(url):
    """Per vault: changes copied but not committed, whether commits wait to
    be pushed, and when it was last pushed. Local and read-only: it reads
    the index and history, and never stages anything. Only copy changes
    the work tree, and it stages what it changes."""
    repo = places.mirror_dir(url)
    if not os.path.isdir(os.path.join(repo, ".git")):
        emit({"event": "status", "cloned": False, "pending": {}, "unpushed": [], "pushedAt": {}})
        return
    git = proc.Git(repo)
    settle(git)
    pending = {}
    for path in changed_paths(git):
        top = path.split("/", 1)[0]
        if common.vault_name(top):
            pending[top] = pending.get(top, 0) + 1
    remote = f"refs/remotes/origin/{branch_of(git)}"
    emit({"event": "status", "cloned": True, "pending": pending, "unpushed": unpushed_vaults(git, remote),
          "pushedAt": pushed_times(git, remote)})
