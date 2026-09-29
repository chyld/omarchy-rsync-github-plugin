"""list and diff: text for a person, shown in a pager in a terminal."""

import os
import subprocess
import sys

from . import common, config, places, plan, proc, sync

STATUS_WORDS = {"A": ("added", "32"), "M": ("modified", "33"), "D": ("deleted", "31"), "T": ("modified", "33")}


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def listing(vault, sources, include_secrets=False):
    """What a sync of `vault` would copy and skip, as text."""
    c = plan.plan(sources, include_secrets)
    shown = [common.plain(p, 4096) for p in sources]
    lines = [f"File Vault · {vault}", "",
             f"{len(sources)} picked · {c.files:,} file{'' if c.files == 1 else 's'} · "
             f"{human(c.bytes)} · {len(c.skipped)} skipped"]
    if include_secrets:
        lines += ["", "Secrets are included: files that look like keys or credentials are pushed, unencrypted."]
    if c.over:
        lines += ["", "A sync would stop here: " + c.over]
    lines += ["", "PICKED", ""]
    lines += ["  " + p + ("/" if os.path.isdir(p) else "") for p in shown]
    lines += ["", "FILES (saved under " + vault + "/<full path>)", ""]
    width = min(100, max([len(common.plain(p, 4096)) for p, _, _, _ in c.entries] + [20]))
    for path, size, link, _ in c.entries[:common.MAX_FILES + 1]:
        p = common.plain(path, 4096)
        lines.append("  " + (f"{p} -> {common.plain(link, 4096)}" if link is not None else f"{p.ljust(width)}  {human(size):>9}"))
    if c.skipped:
        lines += ["", "SKIPPED", ""]
        lines += ["  " + p for p in c.skipped]
    return "\n".join(lines) + "\n"


def file_list(vault, out):
    """git's --name-status -z output as one line per file: what happened to
    it, and its full path as picked (the vault folder dropped, / put back)."""
    tokens = out.split("\0")
    rows = []
    for i in range(0, len(tokens) - 1, 2):
        code, path = tokens[i].strip(), tokens[i + 1]
        word, color = STATUS_WORDS.get(code[:1], ("changed", "0"))
        shown = path[len(vault):] if path.startswith(vault + "/") else "/" + path
        if path == f"{vault}/{config.DEFINITION}":
            shown = "(the vault's definition: its picked paths)"
        rows.append((word, color, common.plain(shown, 4096)))
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
        with places.exclusive():
            text = diff_text(url, vault)
    except common.Failure as e:
        text = str(e) + "\n"
    page(text)


def diff_text(url, vault):
    """Reads the index and history only, like status: what copy staged."""
    repo = places.mirror_dir(url)
    if not os.path.isdir(os.path.join(repo, ".git")):
        return "No local copy yet: connect or copy first.\n"
    git = proc.Git(repo)
    sync.settle(git)
    spec = f":(literal){vault}"
    names = ["--name-status", "--no-renames", "-z"]
    if sync.changed_paths(git, spec):
        title = f"{vault}: copied, not pushed yet\n"
        out = git.ok("diff", "--cached", *names, *([] if sync.has_head(git) else [sync.EMPTY_TREE]), "--", spec)
    elif sync.has_head(git) and git.maybe("log", "-1", "--format=%h", "--", spec):
        when = git.maybe("log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M", "--", spec)
        title = f"{vault}: nothing waiting. The last push that changed it ({when}):\n"
        out = git.ok("log", "-1", "--format=", *names, "--", spec)
    else:
        return f"{vault}: nothing copied yet.\n"
    return title + file_list(vault, out.lstrip("\n"))


def page(text):
    """`text` in less when on a terminal; otherwise printed."""
    if sys.stdout.isatty() and os.path.exists("/usr/bin/less"):
        pager = subprocess.Popen(["/usr/bin/less", "-S", "-R", "--", "-"], stdin=subprocess.PIPE)
        try:
            pager.communicate(text.encode("utf-8", "replace"))
        except BrokenPipeError:
            pass
        return
    sys.stdout.write(text)
    if sys.stdin.isatty():
        input("Press Enter to close.")
