"""Tests for engine.py and the fv package. Run with: python3 -m unittest discover -s tests

Nothing here touches GitHub or the real home directory: home() points at a
temporary folder, and sync runs against a local bare repository."""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import engine  # noqa: E402
from fv import common, config, places, plan, proc, sync, views  # noqa: E402

URL = "https://github.com/chyld/backups.git"


class Validation(unittest.TestCase):
    def test_repo_url(self):
        for ok in ["https://github.com/chyld/backups", "https://github.com/chyld/backups.git",
                   "https://github.com/chyld/backups/", " https://github.com/chyld/backups "]:
            self.assertEqual(common.repo_url(ok), URL, ok)
        for bad in ["", "http://github.com/chyld/backups", "git@github.com:chyld/backups.git",
                    "https://gitlab.com/chyld/backups", "https://github.com/chyld", "https://github.com/chyld/..",
                    "https://github.com/chyld/backups/tree/main", "https://user@github.com/chyld/b", None, 7]:
            self.assertEqual(common.repo_url(bad), "", bad)

    def test_vault_name(self):
        for ok in ["2026-10-01-omarchy", "2026-02-28-a", "2026-10-01-My_Laptop.v2"]:
            self.assertEqual(common.vault_name(ok), ok)
        for bad in ["", "omarchy", "2026-10-01", "2026-10-01-", "2026-13-01-x", "2026-02-30-x",
                    "2026-10-01-../x", "2026-10-01-a/b", "2026-10-01-.x", "2026-10-01-x.", "2026-10-01-x.lock",
                    "2026-10-01-a b", "26-10-01-x", None]:
            self.assertEqual(common.vault_name(bad), "", bad)

    def test_source_path(self):
        self.assertEqual(common.source_path("/home/u/.bashrc"), "/home/u/.bashrc")
        self.assertEqual(common.source_path("/home/u/My Docs/"), "/home/u/My Docs")
        for bad in ["", "/", "//", "rel/path", "/home/../etc", "/home/./u", "/a//b", "/a\nb", "/a\u202eb", None]:
            self.assertEqual(common.source_path(bad), "", bad)

    def test_normalize_drops_invalid_parts(self):
        cfg = config.normalize({
            "repoUrl": "https://github.com/chyld/backups.git",
            "extra": 1,
            "vaults": [
                {"name": "2026-10-01-omarchy", "sources": ["/home/u/.bashrc", "rel", "/home/u/.bashrc", 5],
                 "lastSync": 1790000000, "lastSummary": "3 files\n"},
                {"name": "2026-10-01-omarchy", "sources": []},   # duplicate
                {"name": "bad"}, None,
            ]})
        # lastSync is no longer kept: git knows when a vault was pushed.
        self.assertEqual(cfg, {"version": 1, "repoUrl": "https://github.com/chyld/backups", "vaults": [
            {"name": "2026-10-01-omarchy", "repoUrl": "", "sources": ["/home/u/.bashrc"], "includeSecrets": False,
             "lastSummary": "3 files", "machine": "", "machineName": ""}]})

    def test_vault_links_are_normalized(self):
        problems = []
        cfg = config.normalize({"vaults": [
            {"name": "2026-10-01-a", "repoUrl": "https://github.com/chyld/backups.git"},
            {"name": "2026-10-01-b", "repoUrl": "https://gitlab.com/x/y"}]}, problems)
        self.assertEqual([v["repoUrl"] for v in cfg["vaults"]], ["https://github.com/chyld/backups", ""])
        self.assertIn("2026-10-01-b: repoUrl 'https://gitlab.com/x/y' is not a github.com URL; unlinked", problems)

    def test_push_permission_errors_read_clearly(self):
        for err in ["remote: Permission to chyld/backups.git denied to someone.",
                    "fatal: unable to access 'https://github.com/a/b/': The requested URL returned error: 403"]:
            self.assertEqual(proc.git_error(err), "Your GitHub account can't push to this repository.")
        self.assertEqual(config.normalize("nope"), {"version": 1, "repoUrl": "", "vaults": []})


VECTORS = os.path.join(os.path.dirname(__file__), "vectors.json")


def expand(value):
    """A vectors.json input: {"prefix", "repeat", "times"} spelled out."""
    if isinstance(value, dict) and "repeat" in value:
        return value["prefix"] + value["repeat"] * value["times"]
    return value


class SharedVectors(unittest.TestCase):
    """The cases Safe.js must agree on too (tests/safe.test.mjs runs them)."""

    @classmethod
    def setUpClass(cls):
        with open(VECTORS, encoding="utf-8") as f:
            cls.v = json.load(f)

    def check(self, key, fn):
        for raw, expected in self.v[key]:
            value = expand(raw)
            want = value if expected == "=" else expected
            self.assertEqual(fn(value), want, f"{key}({value!r:.80})")

    def test_limits(self):
        for name, value in self.v["limits"].items():
            self.assertEqual(getattr(common, name), value, name)
        self.assertLess(common.EVENT_MAX, common.LINE_MAX)

    def test_repo_url(self):
        self.check("repoUrl", common.repo_url)

    def test_vault_name(self):
        self.check("vaultName", common.vault_name)

    def test_source_path(self):
        self.check("sourcePath", common.source_path)

    def test_machine_id(self):
        self.check("machineId", common.machine_id)

    def test_plain(self):
        for (value, cap), expected in self.v["plain"]:
            self.assertEqual(common.plain(value, cap), expected)

    def test_config(self):
        self.check("config", config.normalize)


class Home(unittest.TestCase):
    """A temporary home directory for every test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)
        p = mock.patch.object(places, "home", return_value=self.home)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def path(self, *parts):
        return os.path.join(self.home, *parts)

    def write(self, rel, text="x", mode=0o644):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        os.chmod(p, mode)
        return p


class Config(Home):
    def test_missing_config_loads_empty(self):
        self.assertEqual(config.load(), {"config": {"version": 1, "repoUrl": "", "vaults": []}, "problems": [],
                                         "exists": False, "machine": config.this_machine()})

    def test_broken_json_is_an_error_not_an_empty_config(self):
        self.write(".config/file-vault/config.json", '{"repoUrl": "https://github.com/a/b",\n  "vaults": [}')
        with self.assertRaisesRegex(common.Failure, "line 2, column"):
            config.load()

    def test_invalid_entries_are_reported(self):
        self.write(".config/file-vault/config.json", json.dumps({
            "repoUrl": "https://gitlab.com/a/b",
            "vaults": [{"name": "omarchy", "sources": []},
                       {"name": "2026-10-01-ok", "sources": ["/etc/hosts", "relative"]},
                       {"name": "2026-10-01-ok", "sources": []}]}))
        out = config.load()
        self.assertEqual([v["name"] for v in out["config"]["vaults"]], ["2026-10-01-ok"])
        self.assertEqual(out["config"]["vaults"][0]["sources"], ["/etc/hosts"])
        text = "\n".join(out["problems"])
        for expected in ["repoUrl 'https://gitlab.com/a/b'", "vault 'omarchy'", "'relative' is not an absolute path",
                         "vault 2026-10-01-ok: listed twice"]:
            self.assertIn(expected, text)

    def test_save_then_load(self):
        config.save(json.dumps({"repoUrl": "https://github.com/chyld/backups",
                                "vaults": [{"name": "2026-10-01-omarchy", "sources": ["/etc/hosts"]}]}).encode())
        path = self.path(".config", "file-vault", "config.json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(config.load()["config"]["vaults"][0]["sources"], ["/etc/hosts"])
        self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if n.endswith(".tmp")], [])

    def test_save_rejects_garbage(self):
        with self.assertRaises(common.Failure):
            config.save(b"not json")

    def test_symlinked_config_is_refused(self):
        os.makedirs(self.path(".config", "file-vault"))
        self.write("elsewhere.json", "{}")
        os.symlink(self.path("elsewhere.json"), self.path(".config", "file-vault", "config.json"))
        with self.assertRaises(OSError):
            config.load()


class Copying(Home):
    def copy(self, *sources):
        dest = self.path("out")
        c = plan.mirror(dest, plan.plan(list(sources)))
        return c, dest

    def test_unchanged_files_are_not_copied_again(self):
        rc = self.write(".bashrc", "one")
        conf = self.write(".config/app/app.conf", "a")
        _, dest = self.copy(rc, self.path(".config"))
        copied = dest + rc
        before = os.stat(copied).st_ino
        os.utime(dest + conf, (1, 1))          # would show a re-copy
        os.utime(conf, (1, 1))
        self.copy(rc, self.path(".config"))
        self.assertEqual(os.stat(copied).st_ino, before)
        # A changed file is copied, with its new contents.
        self.write(".bashrc", "two, longer")
        self.copy(rc, self.path(".config"))
        with open(copied) as f:
            self.assertEqual(f.read(), "two, longer")

    def test_unpicked_and_deleted_paths_are_removed(self):
        rc = self.write(".bashrc", "rc")
        self.write("proj/a.txt", "a")
        self.write("proj/sub/b.txt", "b")
        _, dest = self.copy(rc, self.path("proj"))
        os.unlink(self.path("proj", "sub", "b.txt"))
        self.write("proj/big.bin")
        with open(self.path("proj", "big.bin"), "wb") as f:
            f.truncate(common.LIMIT_BYTES + 1)   # now skipped, so removed too
        self.copy(self.path("proj"))
        self.assertFalse(os.path.exists(dest + rc))
        self.assertTrue(os.path.exists(dest + self.path("proj", "a.txt")))
        self.assertFalse(os.path.exists(dest + self.path("proj", "sub")))
        self.assertFalse(os.path.exists(dest + self.path("proj", "big.bin")))

    def test_a_symlink_left_in_the_destination_is_never_written_through(self):
        outside = self.path("outside")
        os.makedirs(outside)
        self.write("proj/a.txt", "a")
        dest = self.path("out")
        # An earlier state of the vault had proj as a symlink out of it.
        os.makedirs(os.path.dirname(dest + self.path("proj")))
        os.symlink(outside, dest + self.path("proj"))
        self.copy(self.path("proj"))
        self.assertEqual(os.listdir(outside), [])
        self.assertFalse(os.path.islink(dest + self.path("proj")))
        self.assertTrue(os.path.exists(dest + self.path("proj", "a.txt")))

    def test_read_only_folders_stay_writable_in_the_copy(self):
        self.write("ro/f.txt", "x")
        os.chmod(self.path("ro"), 0o555)
        self.addCleanup(os.chmod, self.path("ro"), 0o755)
        _, dest = self.copy(self.path("ro"))
        self.assertTrue(os.stat(dest + self.path("ro")).st_mode & 0o200)

    def test_full_paths_modes_links_and_skips(self):
        rc = self.write(".bashrc", "alias ll='ls -l'")
        self.write("proj/run.sh", "#!/bin/sh", 0o755)
        self.write("proj/.git/config", "[core]")
        os.symlink("run.sh", self.path("proj", "link"))
        os.mkfifo(self.path("proj", "pipe"))
        with open(self.path("proj", "big.bin"), "wb") as f:
            f.truncate(common.LIMIT_BYTES + 1)
        c, dest = self.copy(rc, self.path("proj"), self.path("missing"))

        with open(dest + rc) as f:
            self.assertEqual(f.read(), "alias ll='ls -l'")
        self.assertEqual(os.stat(dest + self.path("proj", "run.sh")).st_mode & 0o777, 0o755)
        self.assertEqual(os.readlink(dest + self.path("proj", "link")), "run.sh")
        self.assertFalse(os.path.exists(dest + self.path("proj", ".git")))
        self.assertFalse(os.path.lexists(dest + self.path("proj", "pipe")))
        self.assertFalse(os.path.exists(dest + self.path("proj", "big.bin")))
        reasons = "\n".join(c.skipped)
        for expected in [".git: a git repository's own folder", "pipe: not a regular file",
                         "big.bin: over 100 MB", "missing: not found"]:
            self.assertIn(expected, reasons)
        self.assertEqual(c.files, 3)   # .bashrc, run.sh, link

    def test_picked_symlink_is_followed(self):
        real = self.write("dotfiles/bashrc", "real")
        os.symlink(real, self.path(".bashrc"))
        _, dest = self.copy(self.path(".bashrc"))
        self.assertFalse(os.path.islink(dest + self.path(".bashrc")))
        with open(dest + self.path(".bashrc")) as f:
            self.assertEqual(f.read(), "real")

    def test_own_data_is_never_copied(self):
        self.write(".local/share/file-vault/repos/x/y/file", "mirror")
        self.write(".local/share/other/file", "keep")
        c, dest = self.copy(self.path(".local"))
        self.assertTrue(os.path.exists(dest + self.path(".local", "share", "other", "file")))
        self.assertFalse(os.path.exists(dest + self.path(".local", "share", "file-vault")))

    def test_unreadable_file_is_skipped(self):
        if os.geteuid() == 0:
            self.skipTest("root reads everything")
        p = self.write("secret", "s", 0o000)
        c, dest = self.copy(p)
        self.assertFalse(os.path.exists(dest + p))
        self.assertIn("secret: Permission denied", "\n".join(c.skipped))

    def test_dry_run_lists_and_writes_nothing(self):
        rc = self.write(".bashrc", "rc")
        self.write("proj/.git/config", "x")
        os.symlink(".bashrc", self.path("link"))
        c = plan.plan([rc, self.path("proj"), self.path("link")])
        self.assertEqual([(p, size, followed) for p, size, _, followed in c.entries],
                         [(rc, 2, False), (self.path("link"), 2, True)])
        self.assertEqual(sorted(os.listdir(self.home)), [".bashrc", "link", "proj"])
        text = views.listing("2026-10-01-omarchy", [rc, self.path("proj")])
        self.assertIn(rc, text)
        self.assertIn(".git: a git repository's own folder", text)

    def test_dry_run_reports_limits_instead_of_failing(self):
        for i in range(3):
            self.write(f"many/{i}")
        with mock.patch.object(common, "MAX_FILES", 2):
            c = plan.plan([self.path("many")])
        self.assertIn("More than 2 files", c.over)

    def test_secrets_are_skipped_unless_included(self):
        key = self.write(".ssh/id_ed25519", "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
        pub = self.write(".ssh/id_ed25519.pub", "ssh-ed25519 AAAA me")
        hidden_key = self.write("certs/server.pem", "-----BEGIN RSA PRIVATE KEY-----\nabc\n")
        cert = self.write("certs/ca.pem", "-----BEGIN CERTIFICATE-----\nabc\n")
        env = self.write("app/.env", "TOKEN=x")
        env_local = self.write("app/.env.local", "TOKEN=x")
        example = self.write("app/.env.example", "TOKEN=")
        gh = self.write(".config/gh/hosts.yml", "oauth_token: x")
        c = plan.plan([self.path(".ssh"), self.path("certs"), self.path("app"), gh])
        copied = sorted(e[0] for e in c.entries)
        self.assertEqual(copied, sorted([pub, cert, example]))
        reasons = "\n".join(c.skipped)
        for path in [key, hidden_key, env, env_local, gh]:
            self.assertIn(path + ": looks like a secret", reasons)
        everything = plan.plan([self.path(".ssh"), self.path("certs"), self.path("app"), gh], include_secrets=True)
        self.assertEqual(len(everything.entries), 8)
        self.assertEqual(everything.skipped, [])

    def test_a_picked_secret_is_skipped_too(self):
        key = self.write(".ssh/id_rsa", "-----BEGIN RSA PRIVATE KEY-----")
        c = plan.plan([key])
        self.assertEqual((c.entries, c.files), ([], 0))
        self.assertIn("looks like a secret", c.skipped[0])

    def test_file_limit(self):
        for i in range(3):
            self.write(f"many/{i}")
        with mock.patch.object(common, "MAX_FILES", 2):
            with self.assertRaises(common.Failure):
                self.copy(self.path("many"))


class Sync(Home):
    """sync() against a local bare repository standing in for GitHub."""

    def setUp(self):
        super().setUp()
        env = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "T",
               "GIT_COMMITTER_EMAIL": "t@example.com", "HOME": self.home, "GIT_CONFIG_NOSYSTEM": "1"}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.remote = self.path("remote.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.remote], check=True)
        # The local bare repository plays github.com/chyld/backups.
        real = common.repo_url
        p = mock.patch.object(common, "repo_url", side_effect=lambda v: URL if v in (URL, self.remote) else real(v))
        p.start()
        self.addCleanup(p.stop)
        real_run = proc.run
        p = mock.patch.object(proc, "run", side_effect=lambda argv, timeout=proc.LOCAL:
                              real_run([self.remote if a == URL else a for a in argv], timeout))
        p.start()
        self.addCleanup(p.stop)

    def events(self, fn, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            fn(*args)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def copy(self, vault, *sources):
        events = self.events(sync.copy, URL, vault, list(sources))
        self.assertEqual(events[-1]["event"], "copied", events)
        return events[-1]

    def push(self):
        events = self.events(sync.push, URL)
        self.assertEqual(events[-1]["event"], "pushed", events)
        return events[-1]

    def status(self):
        events = self.events(sync.status, URL)
        self.assertEqual(events[-1]["event"], "status", events)
        return events[-1]

    def sync(self, vault, *sources):
        """Both buttons, one after the other."""
        copied = self.copy(vault, *sources)
        return {**copied, **self.push()}

    def other_machine(self):
        other = self.path("other")
        if not os.path.isdir(other):
            subprocess.run(["git", "clone", "-q", self.remote, other], check=True)
        # Nothing to pull from a repository with no commits yet.
        subprocess.run(["git", "-C", other, "pull", "-q"], capture_output=True)
        return other

    def commit_from(self, other, rel, text, message="other"):
        path = os.path.join(other, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        subprocess.run(["git", "-C", other, "add", "-A"], check=True)
        subprocess.run(["git", "-C", other, "commit", "-qm", message], check=True)
        subprocess.run(["git", "-C", other, "push", "-q"], check=True)

    def remote_log(self):
        return subprocess.run(["git", "-C", self.remote, "log", "--format=%s", "main"],
                              capture_output=True, text=True).stdout.splitlines()

    def test_copy_stays_local_until_push(self):
        rel = self.home.lstrip("/")
        rc = self.write(".bashrc", "one")
        copied = self.copy("2026-10-01-omarchy", rc)
        self.assertEqual((copied["files"], copied["changed"]), (1, 2))   # the file and the definition
        self.assertEqual(self.remote_log(), [])            # nothing sent
        self.assertEqual(self.status()["pending"], {"2026-10-01-omarchy": 2})
        pushed = self.push()
        self.assertEqual(pushed["vaults"], ["2026-10-01-omarchy"])
        self.assertEqual(self.remote_files(), [f"2026-10-01-omarchy/{rel}/.bashrc"])
        when = int(subprocess.run(["git", "-C", self.remote, "log", "-1", "--format=%ct", "main"],
                                  capture_output=True, text=True).stdout)
        self.assertEqual(self.status(), {"event": "status", "cloned": True, "pending": {}, "unpushed": [],
                                         "pushedAt": {"2026-10-01-omarchy": when}})

    def test_push_commits_each_vault_separately(self):
        self.copy("2026-10-01-a", self.write("a.txt", "a"))
        self.copy("2026-10-01-b", self.write("b.txt", "b"))
        self.push()
        self.assertEqual(sorted(self.remote_log()), ["Sync 2026-10-01-a \u2014 2 files changed",
                                                     "Sync 2026-10-01-b \u2014 2 files changed"])

    def test_push_with_nothing_copied_sends_nothing(self):
        self.sync("2026-10-01-a", self.write("a.txt", "a"))
        pushed = self.push()
        self.assertEqual((pushed["vaults"], pushed["commit"]), ([], ""))
        self.assertEqual(len(self.remote_log()), 1)

    def test_this_machines_copy_wins_over_github(self):
        rel = self.home.lstrip("/")
        rc = self.write(".bashrc", "one")
        self.sync("2026-10-01-omarchy", rc)
        other = self.other_machine()
        self.commit_from(other, f"2026-10-01-omarchy/{rel}/.bashrc", "from the other machine")
        self.write(".bashrc", "mine, newer")
        self.sync("2026-10-01-omarchy", rc)
        out = subprocess.run(["git", "-C", self.remote, "show", f"main:2026-10-01-omarchy/{rel}/.bashrc"],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, "mine, newer")

    def test_status_before_the_first_clone(self):
        self.assertEqual(self.status(), {"event": "status", "cloned": False, "pending": {}, "unpushed": [],
                                         "pushedAt": {}})

    def index(self):
        repo = places.mirror_dir(URL)
        return subprocess.run(["git", "-C", repo, "diff", "--cached", "--name-only"],
                              capture_output=True, text=True, check=True).stdout

    def test_status_never_stages_anything(self):
        self.copy("2026-10-01-a", self.write("a.txt", "a"))
        before = self.index()
        # Something changed in the local copy by hand, outside a copy.
        with open(os.path.join(places.mirror_dir(URL), "2026-10-01-a", "stray"), "w") as f:
            f.write("x")
        self.assertEqual(self.status()["pending"], {"2026-10-01-a": 2})
        self.assertEqual(self.index(), before)
        self.diff_text("2026-10-01-a")
        self.assertEqual(self.index(), before)

    def test_push_times_come_from_git_and_are_cached(self):
        self.sync("2026-10-01-a", self.write("a.txt", "a"))
        self.sync("2026-10-01-b", self.write("b.txt", "b"))
        first = self.status()["pushedAt"]
        self.assertEqual(sorted(first), ["2026-10-01-a", "2026-10-01-b"])
        cache = os.path.join(places.mirror_dir(URL), ".git", sync.PUSHED_CACHE)
        self.assertTrue(os.path.exists(cache))
        # With GitHub's branch where it was, no git log runs again.
        seen = []
        real = proc.run

        def run(argv, timeout=proc.LOCAL):
            seen.append(argv)
            return real(argv, timeout)

        with mock.patch.object(proc, "run", side_effect=run):
            self.assertEqual(self.status()["pushedAt"], first)
        self.assertEqual([a for a in seen if "log" in a and "--format=%ct" in a], [])
        self.assertTrue(seen)

    def diff_text(self, vault):
        out = io.StringIO()
        with redirect_stdout(out):
            views.diff(URL, vault)
        return re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue())   # without colors

    def test_diff_lists_files_only_then_the_last_push(self):
        rc = self.write(".bashrc", "alias x=1\n")
        gone = self.write(".config/old.conf", "old")
        self.sync("2026-10-01-omarchy", rc, self.path(".config"))
        self.write(".bashrc", "alias x=2\n")
        os.unlink(gone)
        new = self.write(".config/new.conf", "new")
        self.copy("2026-10-01-omarchy", rc, self.path(".config"))
        text = self.diff_text("2026-10-01-omarchy")
        self.assertIn("copied, not pushed yet", text)
        self.assertIn("3 files: 1 added, 1 deleted, 1 modified", text)
        for line in [f"added     {new}", f"deleted   {gone}", f"modified  {rc}"]:
            self.assertIn(line, text)
        self.assertNotIn("alias", text)                       # names only
        self.push()
        text = self.diff_text("2026-10-01-omarchy")
        self.assertIn("nothing waiting", text)
        self.assertIn(f"modified  {rc}", text)
        self.assertNotIn("alias", text)

    def remote_files(self, definitions=False):
        """The files on GitHub's main branch; vault definitions left out
        unless asked for."""
        out = subprocess.run(["git", "-C", self.remote, "ls-tree", "-r", "--name-only", "main"],
                             capture_output=True, text=True, check=True).stdout
        return sorted(p for p in out.split() if definitions or not p.endswith("/" + config.DEFINITION))

    def test_first_sync_into_empty_repo_then_resync(self):
        rc = self.write(".bashrc", "one")
        conf = self.write(".config/app/app.conf", "a")
        done = self.sync("2026-10-01-omarchy", rc, self.path(".config", "app"))
        self.assertEqual(done["files"], 2)
        rel = self.home.lstrip("/")
        self.assertEqual(self.remote_files(), [f"2026-10-01-omarchy/{rel}/.bashrc",
                                               f"2026-10-01-omarchy/{rel}/.config/app/app.conf"])

        # Sync again: a changed file is sent, a deleted one removed.
        self.write(".bashrc", "two")
        os.unlink(conf)
        done = self.sync("2026-10-01-omarchy", rc, self.path(".config", "app"))
        self.assertEqual(done["changed"], 2)
        self.assertEqual(self.remote_files(), [f"2026-10-01-omarchy/{rel}/.bashrc"])

        # Nothing changed: nothing to push.
        done = self.sync("2026-10-01-omarchy", rc, self.path(".config", "app"))
        self.assertEqual((done["changed"], done["commit"]), (0, ""))

    def test_other_vaults_and_other_machines_are_kept(self):
        rc = self.write(".bashrc", "one")
        self.sync("2026-10-01-omarchy", rc)
        # Another machine pushes its own vault.
        other = self.path("other")
        subprocess.run(["git", "clone", "-q", self.remote, other], check=True)
        os.makedirs(os.path.join(other, "2026-10-02-laptop", "etc"))
        with open(os.path.join(other, "2026-10-02-laptop", "etc", "hosts"), "w") as f:
            f.write("127.0.0.1")
        subprocess.run(["git", "-C", other, "add", "-A"], check=True)
        subprocess.run(["git", "-C", other, "commit", "-qm", "laptop"], check=True)
        subprocess.run(["git", "-C", other, "push", "-q"], check=True)

        self.write(".bashrc", "two")
        self.sync("2026-10-01-omarchy", rc)
        files = self.remote_files()
        self.assertIn("2026-10-02-laptop/etc/hosts", files)
        self.assertIn(f"2026-10-01-omarchy/{self.home.lstrip('/')}/.bashrc", files)

    def test_push_race_is_retried_once(self):
        rc = self.write(".bashrc", "one")
        self.sync("2026-10-01-omarchy", rc)
        other = self.path("other")
        subprocess.run(["git", "clone", "-q", self.remote, other], check=True)
        real_step = sync.step
        raced = []

        def step(text):
            # Another machine pushes just before our first push.
            if text == "Pushing to GitHub" and not raced:
                raced.append(True)
                with open(os.path.join(other, "NOTE"), "w") as f:
                    f.write("x")
                subprocess.run(["git", "-C", other, "add", "-A"], check=True)
                subprocess.run(["git", "-C", other, "commit", "-qm", "race"], check=True)
                subprocess.run(["git", "-C", other, "push", "-q"], check=True)
            real_step(text)

        self.write(".bashrc", "two")
        with mock.patch.object(sync, "step", side_effect=step):
            self.sync("2026-10-01-omarchy", rc)
        self.assertIn("NOTE", self.remote_files())
        out = subprocess.run(["git", "-C", self.remote, "show", f"main:2026-10-01-omarchy/{self.home.lstrip('/')}/.bashrc"],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, "two")

    def connect(self):
        out = io.StringIO()
        with redirect_stdout(out):
            sync.connect(URL)
        events = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(events[-1]["event"], "connected", events)
        vaults = [{k: v for k, v in e.items() if k != "event"} for e in events if e["event"] == "vault"]
        self.assertEqual(events[-1]["vaults"], len(vaults))
        return {**events[-1], "vaults": vaults}

    def test_connect_to_an_empty_repository_clones_it(self):
        done = self.connect()
        self.assertEqual((done["branch"], done["empty"], done["vaults"]), ("main", True, []))
        self.assertTrue(os.path.isdir(os.path.join(places.mirror_dir(URL), ".git")))
        # The dry-run push sent nothing.
        refs = subprocess.run(["git", "-C", self.remote, "for-each-ref"], capture_output=True, text=True).stdout
        self.assertEqual(refs, "")

    def test_connect_lists_the_vaults_already_in_the_repository(self):
        self.sync("2026-10-01-omarchy", self.write(".bashrc", "x"))
        self.write("NOTES.md")
        done = self.connect()
        rc = self.path(".bashrc")
        pushed = int(subprocess.run(["git", "-C", self.remote, "log", "-1", "--format=%ct", "main"],
                                    capture_output=True, text=True).stdout)
        self.assertEqual((done["empty"], done["vaults"]), (False, [
            {"name": "2026-10-01-omarchy", "defined": True, "sources": [rc], "machine": config.this_machine(),
             "pushedAt": pushed}]))
        branches = subprocess.run(["git", "-C", self.remote, "branch", "--format=%(refname:short)"],
                                  capture_output=True, text=True).stdout.split()
        self.assertEqual(branches, ["main"])

    def test_copy_writes_the_vaults_definition(self):
        rc = self.write(".bashrc", "x")
        self.sync("2026-10-01-omarchy", rc)
        self.assertIn(f"2026-10-01-omarchy/{config.DEFINITION}", self.remote_files(definitions=True))
        out = subprocess.run(["git", "-C", self.remote, "show", f"main:2026-10-01-omarchy/{config.DEFINITION}"],
                             capture_output=True, check=True).stdout
        self.assertEqual(config.read_definition(out), {"sources": [rc], "machine": config.this_machine()})

    def test_a_folder_without_a_definition_is_listed_as_undefined(self):
        other = self.other_machine()
        self.commit_from(other, "2026-09-01-old/etc/hosts", "x")
        done = self.connect()
        self.assertEqual([{k: v for k, v in x.items() if k != "pushedAt"} for x in done["vaults"]],
                         [{"name": "2026-09-01-old", "defined": False, "sources": [], "machine": {"id": "", "name": ""}}])
        self.assertGreater(done["vaults"][0]["pushedAt"], 0)

    def as_other_machine(self):
        return mock.patch.object(config, "this_machine", return_value={"id": "0123456789abcdef", "name": "laptop"})

    def test_a_vault_from_another_machine_is_not_copied_over_without_adopting(self):
        rel = self.home.lstrip("/")
        rc = self.write(".bashrc", "from the laptop")
        with self.as_other_machine():
            self.sync("2026-10-01-laptop", rc)
        os.unlink(rc)                                  # this machine doesn't have it
        events = self.events(engine.main, ["copy", URL, "2026-10-01-laptop", "--", rc])
        self.assertEqual(events[-1]["event"], "error")
        self.assertIn("was made on laptop", events[-1]["message"])
        self.assertIn(f"2026-10-01-laptop/{rel}/.bashrc", self.remote_files())
        # Adopted, it copies this machine's files, and the definition says so.
        self.write(".bashrc", "this machine's")
        events = self.events(engine.main, ["copy", "--adopt", URL, "2026-10-01-laptop", "--", rc])
        self.assertEqual(events[-1]["event"], "copied", events)
        self.push()
        out = subprocess.run(["git", "-C", self.remote, "show", f"main:2026-10-01-laptop/{config.DEFINITION}"],
                             capture_output=True, check=True).stdout
        self.assertEqual(config.read_definition(out)["machine"], config.this_machine())

    def test_a_vault_without_a_definition_can_be_copied(self):
        other = self.other_machine()
        self.commit_from(other, "2026-09-01-old/etc/hosts", "x")
        self.copy("2026-09-01-old", self.write(".bashrc", "x"))

    def test_nothing_is_left_in_the_work_tree_by_copying(self):
        self.copy("2026-10-01-a", self.write("a.txt", "a"))
        repo = places.mirror_dir(URL)
        self.assertEqual(sorted(os.listdir(repo)), [".git", "2026-10-01-a"])
        self.assertEqual(os.listdir(places.work_dir()), [])

    def test_a_stale_index_lock_is_cleared(self):
        self.copy("2026-10-01-a", self.write("a.txt", "a"))
        lock = os.path.join(places.mirror_dir(URL), ".git", "index.lock")
        open(lock, "w").close()
        os.utime(lock, (1, 1))                          # left by a git killed long ago
        self.copy("2026-10-01-a", self.write("a.txt", "b"))
        self.assertFalse(os.path.exists(lock))

    def test_a_fresh_index_lock_is_left_alone(self):
        self.copy("2026-10-01-a", self.write("a.txt", "a"))
        lock = os.path.join(places.mirror_dir(URL), ".git", "index.lock")
        open(lock, "w").close()                         # a git may be using it right now
        events = self.events(engine.main, ["copy", URL, "2026-10-01-a", "--", self.path("a.txt")])
        self.assertEqual(events[-1]["event"], "error")
        self.assertTrue(os.path.exists(lock))

    def test_the_data_folder_is_made_private(self):
        os.makedirs(places.data_dir(), mode=0o755)
        os.chmod(places.data_dir(), 0o755)
        self.copy("2026-10-01-a", self.write("a.txt", "a"))
        self.assertEqual(os.stat(places.data_dir()).st_mode & 0o777, 0o700)

    def test_commands_wait_for_each_other(self):
        with places.exclusive(), mock.patch.object(places, "LOCK_WAIT", 0.3):
            events = self.events(engine.main, ["status", URL])
        self.assertEqual(events[-1]["event"], "error")
        self.assertIn("still running", events[-1]["message"])

    def test_a_clone_left_by_a_killed_run_is_removed(self):
        parent = os.path.dirname(places.mirror_dir(URL))
        os.makedirs(os.path.join(parent, ".backups.dead.clone", "x"))
        self.connect()
        self.assertEqual(sorted(os.listdir(parent)), ["backups"])

    def test_the_default_branch_is_learned_after_cloning_an_empty_repository(self):
        self.connect()                                  # empty: no default branch yet
        other = self.other_machine()
        subprocess.run(["git", "-C", other, "checkout", "-q", "-b", "trunk"], check=True)
        with open(os.path.join(other, "x"), "w") as f:
            f.write("x")
        subprocess.run(["git", "-C", other, "add", "-A"], check=True)
        subprocess.run(["git", "-C", other, "commit", "-qm", "x"], check=True)
        subprocess.run(["git", "-C", other, "push", "-q", "origin", "trunk"], check=True)
        subprocess.run(["git", "-C", self.remote, "symbolic-ref", "HEAD", "refs/heads/trunk"], check=True)
        self.copy("2026-10-01-a", self.write("a.txt", "a"))
        self.push()
        branches = subprocess.run(["git", "-C", self.remote, "branch", "--format=%(refname:short)"],
                                  capture_output=True, text=True).stdout.split()
        self.assertEqual(branches, ["trunk"])

    def test_an_unexpected_error_is_reported_not_a_traceback(self):
        with mock.patch.object(plan, "plan", side_effect=RecursionError("too deep")):
            events = self.events(engine.main, ["copy", URL, "2026-10-01-a", "--", self.path("x")])
        self.assertEqual(events[-1], {"event": "error", "message": "Unexpected RecursionError: too deep"})

    def test_connect_reports_a_missing_repository(self):
        subprocess.run(["rm", "-rf", self.remote], check=True)
        with self.assertRaises(common.Failure), redirect_stdout(io.StringIO()):
            sync.connect(URL)

    def test_mirror_for_another_repository_is_refused(self):
        repo = places.mirror_dir(URL)
        os.makedirs(os.path.dirname(repo))
        subprocess.run(["git", "clone", "-q", self.remote, repo], check=True)
        subprocess.run(["git", "-C", repo, "remote", "set-url", "origin", "https://github.com/x/y"], check=True)
        with self.assertRaises(common.Failure):
            sync.copy(URL, "2026-10-01-omarchy", [])


class Machines(Home):
    def test_the_machine_id_is_a_hash_not_the_raw_id(self):
        me = config.this_machine()
        with open("/etc/machine-id") as f:
            raw = f.read().strip()
        self.assertRegex(me["id"], r"^[0-9a-f]{16}$")
        self.assertNotIn(me["id"], raw)

    def test_definitions_are_checked(self):
        d = config.read_definition(json.dumps({"sources": ["/etc/hosts", "rel", 3],
                                               "machine": {"id": "not-hex", "name": "a\nb"}}))
        self.assertEqual(d, {"sources": ["/etc/hosts"], "machine": {"id": "", "name": "ab"}})
        for bad in [b"", b"nope", b"[]", b"x" * (config.DEFINITION_MAX + 1)]:
            self.assertIsNone(config.read_definition(bad))


class Reset(Home):
    def test_reset_deletes_settings_and_local_repositories_only(self):
        self.write(".config/file-vault/config.json", "{}")
        self.write(".local/share/file-vault/repos/chyld/a1/2026-10-01-x/f", "x")
        ro = self.path(".local", "share", "file-vault", "repos", "chyld", "a1", "2026-10-01-x")
        os.chmod(ro, 0o555)                            # a read-only folder copied in
        self.write(".config/other/keep", "keep")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(engine.main(["reset"]), 0)
        event = json.loads(out.getvalue())
        self.assertEqual(event["removed"], [self.path(".config", "file-vault"), self.path(".local", "share", "file-vault")])
        self.assertFalse(os.path.exists(self.path(".config", "file-vault")))
        self.assertFalse(os.path.exists(self.path(".local", "share", "file-vault")))
        self.assertTrue(os.path.exists(self.path(".config", "other", "keep")))

    def test_a_symlink_in_place_is_removed_not_followed(self):
        target = self.write("precious/keep", "keep")
        os.makedirs(self.path(".config"))
        os.symlink(self.path("precious"), self.path(".config", "file-vault"))
        with redirect_stdout(io.StringIO()):
            engine.main(["reset"])
        self.assertFalse(os.path.lexists(self.path(".config", "file-vault")))
        self.assertTrue(os.path.exists(target))

    def test_reset_with_nothing_there(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(engine.main(["reset"]), 0)
        self.assertFalse(os.path.exists(self.path(".config", "file-vault")))
        self.assertFalse(os.path.exists(self.path(".local", "share", "file-vault")))


class Main(Home):
    def test_folder_before_and_after_the_first_clone(self):
        self.assertEqual(engine.main(["folder", URL]), 4)
        os.makedirs(os.path.join(places.mirror_dir(URL), ".git"))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(engine.main(["folder", URL]), 0)
        self.assertEqual(out.getvalue().strip(), places.mirror_dir(URL))
        self.assertEqual(engine.main(["folder", "https://gitlab.com/a/b"]), 4)

    def test_list_rejects_bad_arguments(self):
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(engine.main(["list", "bad", "--", "/etc"]), 2)
            self.assertEqual(engine.main(["list", "2026-10-01-a", "--", "rel"]), 2)

    def test_flags_come_before_the_repository_in_any_order(self):
        seen = []
        with mock.patch.object(sync, "copy", side_effect=lambda *a: seen.append(a)), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(engine.main(["copy", "--secrets", "--adopt", URL, "2026-10-01-a", "--", "/etc/hosts"]), 0)
            self.assertEqual(engine.main(["copy", URL, "2026-10-01-a", "--", "/etc/hosts"]), 0)
            self.assertEqual(engine.main(["copy", URL, "--adopt", "2026-10-01-a", "--", "/x"]), 2)
        self.assertEqual(seen, [(URL, "2026-10-01-a", ["/etc/hosts"], True, True),
                                (URL, "2026-10-01-a", ["/etc/hosts"], False, False)])

    def test_list_takes_the_secrets_flag(self):
        key = self.write(".ssh/id_rsa", "-----BEGIN RSA PRIVATE KEY-----")
        for flags, shown in (([], False), (["--secrets"], True)):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(engine.main(["list", *flags, "2026-10-01-a", "--", key]), 0)
            self.assertEqual(f"  {key}  " in out.getvalue(), shown, flags)

    def test_invalid_arguments_report_an_error(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = engine.main(["copy", URL, "../etc", "--", "/etc/hosts"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())["event"], "error")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(engine.main(["copy", URL, "2026-10-01-x", "--", "relative"]), 2)


if __name__ == "__main__":
    unittest.main()
