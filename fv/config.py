"""config.json, this machine, and the vault definitions kept in the
repository.

config.json holds what only the user can say: the connected repository,
the vaults, what each one holds, and the summary of its last copy. What git
already knows (what waits to be pushed, when a vault was last pushed) is
read from the local copy each time, never stored here."""

import hashlib
import json
import os
import secrets
import socket
import stat

from . import common, places


def normalize(obj, problems=None):
    """The config with every unknown or invalid part dropped; what was
    dropped, and why, is appended to `problems`."""
    problems = [] if problems is None else problems
    out = {"version": 1, "repoUrl": "", "vaults": []}
    if not isinstance(obj, dict):
        problems.append("config.json is not a JSON object")
        return out
    out["repoUrl"] = common.repo_page(obj.get("repoUrl", ""))
    if not out["repoUrl"] and obj.get("repoUrl"):
        problems.append(f"repoUrl {common.plain(obj.get('repoUrl'), 80)!r}: not an https://github.com/<owner>/<repo> URL")
    seen = set()
    vaults = obj.get("vaults", [])
    if not isinstance(vaults, list):
        problems.append("vaults: not a list")
        vaults = []
    if len(vaults) > common.MAX_VAULTS:
        problems.append(f"vaults: only the first {common.MAX_VAULTS} are used")
    for i, v in enumerate(vaults[:common.MAX_VAULTS]):
        if not isinstance(v, dict):
            problems.append(f"vault #{i + 1}: not an object")
            continue
        name = common.vault_name(v.get("name"))
        if not name:
            problems.append(f"vault {common.plain(v.get('name'), 80)!r}: the name must be yyyy-mm-dd-<name>")
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
        for s in raw[:common.MAX_SOURCES]:
            p = common.source_path(s)
            if not p:
                problems.append(f"{name}: {common.plain(s, 80)!r} is not an absolute path")
            elif p not in sources:
                sources.append(p)
        linked = common.repo_page(v.get("repoUrl", ""))
        if not linked and v.get("repoUrl"):
            problems.append(f"{name}: repoUrl {common.plain(v.get('repoUrl'), 80)!r} is not a github.com URL; unlinked")
        out["vaults"].append({
            "name": name,
            "repoUrl": linked,
            "sources": sources,
            # Set by hand only: copy files that look like secrets too.
            "includeSecrets": v.get("includeSecrets") is True,
            "lastSummary": common.plain(v.get("lastSummary", ""), 120) if isinstance(v.get("lastSummary"), str) else "",
            # The machine the vault belongs to: "" for one made before this
            # was recorded, which is taken to be this machine's.
            "machine": common.machine_id(v.get("machine")),
            "machineName": common.plain(v.get("machineName", ""), 64) if isinstance(v.get("machineName"), str) else "",
        })
    return out


def this_machine():
    """{"id", "name"}: a hash of /etc/machine-id (the raw id never leaves the
    machine; the hash is stored in the repository) and the host name."""
    try:
        with open("/etc/machine-id", encoding="ascii") as f:
            raw = f.read(64).strip()
    except (OSError, UnicodeDecodeError):
        raw = ""
    ident = hashlib.sha256(("file-vault:" + raw).encode()).hexdigest()[:16] if raw else ""
    return {"id": ident, "name": common.plain(socket.gethostname(), 64)}


# Each vault folder holds its own definition, so a new machine connecting to
# the repository knows what the vault is made of and where it came from.
DEFINITION = ".file-vault.json"
DEFINITION_MAX = 256 * common.KiB


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
    for s in raw[:common.MAX_SOURCES] if isinstance(raw, list) else []:
        p = common.source_path(s)
        if p and p not in sources:
            sources.append(p)
    m = obj.get("machine") if isinstance(obj.get("machine"), dict) else {}
    name = m.get("name")
    return {"sources": sources, "machine": {"id": common.machine_id(m.get("id")),
                                            "name": common.plain(name, 64) if isinstance(name, str) else ""}}


def load():
    path = os.path.join(places.config_dir(), "config.json")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return {"config": normalize({}), "problems": [], "exists": False, "machine": this_machine()}
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_size > common.CONFIG_MAX:
            raise common.Failure("config.json is not a regular file of this user within 256 KB")
        os.set_blocking(fd, True)
        data = os.read(fd, common.CONFIG_MAX + 1)
    finally:
        os.close(fd)
    try:
        obj = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise common.Failure("config.json isn't UTF-8 text")
    except json.JSONDecodeError as e:
        raise common.Failure(f"config.json line {e.lineno}, column {e.colno}: {e.msg}")
    problems = []
    return {"config": normalize(obj, problems), "problems": problems[:common.MAX_LISTED], "exists": True,
            "machine": this_machine()}


def save(text):
    if len(text) > common.CONFIG_MAX:
        raise common.Failure("config too large")
    try:
        cfg = normalize(json.loads(text.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        raise common.Failure("config is not JSON")
    d = places.config_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        raise common.Failure(f"{d} is not a directory of this user")
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
