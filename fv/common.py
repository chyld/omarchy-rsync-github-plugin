"""Limits, the Failure exception, and validation of every value that comes
from outside: arguments, config.json, and the repository.

The same rules are written in Safe.js for the shell. tests/vectors.json
holds the cases both must agree on; both test suites run them."""

import re
import time

KiB = 1024
MiB = KiB * KiB
GiB = KiB * MiB

# Shared with Safe.js (see tests/vectors.json, "limits").
MAX_VAULTS = 200
MAX_SOURCES = 500
MAX_LISTED = 20            # skipped paths named in the copied event
MAX_PATH = 4096            # characters in a picked path
LINE_MAX = 64 * KiB        # characters in one line the shell reads

EVENT_MAX = 60000          # bytes in one JSON line this engine prints; under LINE_MAX
CONFIG_MAX = 256 * KiB
MAX_FILES = 100000         # files in one vault
MAX_BYTES = 2 * GiB        # bytes in one vault
LIMIT_BYTES = 100 * MiB    # GitHub refuses files this large

# Control, bidirectional and other invisible characters.
CONTROL = re.compile("[\u0000-\u001f\u007f-\u009f؜‎‏  ‪-‮⁦-⁩﻿]")

# What is trimmed from a pasted URL: the ASCII whitespace JavaScript and
# Python agree on, not str.strip()'s Unicode set.
TRIM = " \t\r\n"


class Failure(Exception):
    """A readable reason the command failed."""


def repo_url(value):
    """https://github.com/<owner>/<repo>, normalized to ...<repo>.git, or ""."""
    if not isinstance(value, str):
        return ""
    m = re.fullmatch(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?",
                     value.strip(TRIM))
    if not m or m.group(2) in (".", ".."):
        return ""
    return f"https://github.com/{m.group(1)}/{m.group(2)}.git"


def repo_page(value):
    """https://github.com/<owner>/<repo>, or ""."""
    url = repo_url(value)
    return url[:-len(".git")] if url else ""


def vault_name(value):
    """yyyy-mm-dd-<name>: a real date in ASCII digits, then letters, digits,
    ., _ or -."""
    if not isinstance(value, str):
        return ""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})-[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value, re.ASCII)
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
    if not isinstance(value, str) or not 2 <= len(value) <= MAX_PATH or not value.startswith("/"):
        return ""
    if CONTROL.search(value):
        return ""
    s = value.rstrip("/")
    if len(s) < 2 or any(p in ("", ".", "..") for p in s.split("/")[1:]):
        return ""
    return s


def machine_id(value):
    """16 hex digits (a hash of /etc/machine-id), or ""."""
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16}", value) else ""


def plain(value, cap=200):
    """Text with control characters removed, cut to `cap` characters."""
    s = CONTROL.sub("", str(value))
    return s if len(s) <= cap else s[:cap - 1] + "…"
