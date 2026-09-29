// Unit tests for Safe.js and Commands.js, the code every untrusted value
// passes through. Run with: node --test tests/
import { test } from "node:test"
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import vm from "node:vm"

const root = fileURLToPath(new URL("..", import.meta.url))

// QML JavaScript libraries start with ".pragma" / ".import" lines, which are
// not JavaScript; drop them and evaluate the rest in a sandbox.
function load(file, globals = {}) {
  const src = readFileSync(root + file, "utf8").replace(/^\.(pragma|import) .*$/gm, "")
  const context = vm.createContext({ ...globals })
  vm.runInContext(src, context)
  return context
}

const Safe = load("Safe.js")
const Commands = load("Commands.js", { Safe })
const plainJson = (v) => JSON.parse(JSON.stringify(v))

test("repoUrl and repoPage accept https github.com repos only", () => {
  assert.equal(Safe.repoUrl("https://github.com/chyld/backups/"), "https://github.com/chyld/backups.git")
  assert.equal(Safe.repoPage("https://github.com/chyld/backups.git"), "https://github.com/chyld/backups")
  for (const bad of ["", "git@github.com:chyld/b.git", "https://gitlab.com/a/b", "https://github.com/a/..",
                     "https://github.com/a/b/tree/main", "https://github.com/a/b?x", null])
    assert.equal(Safe.repoUrl(bad), "", JSON.stringify(bad))
})

test("vaultName wants a real date and a plain name", () => {
  assert.equal(Safe.vaultName("2026-10-01-omarchy"), "2026-10-01-omarchy")
  for (const bad of ["omarchy", "2026-10-01", "2026-02-30-x", "2026-10-01-../x", "2026-10-01-a/b",
                     "2026-10-01-.x", "2026-10-01-x.", "2026-10-01-a b", null])
    assert.equal(Safe.vaultName(bad), "", JSON.stringify(bad))
})

test("cleanDate and isoDate", () => {
  assert.equal(Safe.cleanDate(" 2026-10-01 "), "2026-10-01")
  assert.equal(Safe.cleanDate("2026-2-1"), "")
  assert.equal(Safe.cleanDate("2026-02-29"), "")
  assert.equal(Safe.isoDate(new Date(2026, 0, 5)), "2026-01-05")
})

test("sourcePath wants a clean absolute path other than /", () => {
  assert.equal(Safe.sourcePath("/home/u/.bashrc"), "/home/u/.bashrc")
  assert.equal(Safe.sourcePath("/home/u/dir/"), "/home/u/dir")
  for (const bad of ["", "/", "//", "rel", "/a/../b", "/a/./b", "/a//b", "/a\nb", "/a\u202eb", 3])
    assert.equal(Safe.sourcePath(bad), "", JSON.stringify(bad))
})

test("shortPath shows the home folder as ~", () => {
  assert.equal(Safe.shortPath("/home/u/.bashrc", "/home/u"), "~/.bashrc")
  assert.equal(Safe.shortPath("/home/user2/x", "/home/u"), "/home/user2/x")
  assert.equal(Safe.shortPath("/etc/hosts", "/home/u"), "/etc/hosts")
})

test("config drops invalid parts, duplicates and junk", () => {
  const cfg = Safe.config(JSON.stringify({
    repoUrl: "https://github.com/chyld/backups.git",
    vaults: [
      { name: "2026-10-01-omarchy", sources: ["/etc/hosts", "rel", "/etc/hosts"], lastSync: 5, lastSummary: "a\nb" },
      { name: "2026-10-01-omarchy" }, { name: "bad" }, null, 7,
    ] }))
  assert.deepEqual(plainJson(cfg), { version: 1, repoUrl: "https://github.com/chyld/backups", vaults: [
    { name: "2026-10-01-omarchy", repoUrl: "", sources: ["/etc/hosts"], lastSync: 5, lastSummary: "ab",
      machine: "", machineName: "" }] })
  assert.deepEqual(plainJson(Safe.config("not json")), { version: 1, repoUrl: "", vaults: [] })
})

test("withVault changes one vault in a copy", () => {
  const cfg = Safe.config({ vaults: [{ name: "2026-10-01-a", sources: [] }, { name: "2026-10-01-b", sources: [] }] })
  const next = Safe.withVault(cfg, "2026-10-01-b", (v) => { v.sources = ["/x"] })
  assert.deepEqual(plainJson(next.vaults.map((v) => v.sources)), [[], ["/x"]])
  assert.deepEqual(plainJson(cfg.vaults[1].sources), [])
})

test("addSources takes the picker's lines, skipping bad and repeated ones", () => {
  assert.deepEqual(plainJson(Safe.addSources(["/a"], "/a\n/b\nrel\n\n/c/\n")), ["/a", "/b", "/c"])
})

test("engineEvent checks each field", () => {
  assert.deepEqual(plainJson(Safe.engineEvent('{"event":"step","text":"Copying"}')), { event: "step", text: "Copying" })
  const copied = Safe.engineEvent(JSON.stringify({ event: "copied", files: 3, bytes: 2048, changed: 1,
                                                    skipped: ["/x: not found", 5], skippedCount: 1 }))
  assert.deepEqual(plainJson(copied), { event: "copied", files: 3, bytes: 2048, changed: 1,
                                        skipped: ["/x: not found"], skippedCount: 1 })
  assert.equal(Safe.engineEvent(JSON.stringify({ event: "copied", files: -1 })).files, 0)
  assert.deepEqual(plainJson(Safe.engineEvent(JSON.stringify({ event: "pushed", vaults: ["2026-10-01-a", "../x"],
                                                               commit: "; rm" }))),
                   { event: "pushed", vaults: ["2026-10-01-a"], commit: "" })
  assert.deepEqual(plainJson(Safe.engineEvent(JSON.stringify({ event: "status", cloned: true,
    pending: { "2026-10-01-a": 3, "bad": 2, "2026-10-01-b": -1 }, unpushed: ["2026-10-01-b"] }))),
    { event: "status", cloned: true, pending: { "2026-10-01-a": 3 }, unpushed: ["2026-10-01-b"] })
  assert.equal(Safe.engineEvent("nope"), null)
  assert.equal(Safe.engineEvent('{"event":"other"}'), null)
})

test("connected events and connect argv", () => {
  assert.deepEqual(plainJson(Safe.engineEvent(JSON.stringify({ event: "connected", branch: "main", empty: false, vaults: 2 }))),
                   { event: "connected", branch: "main", empty: false })
  const v = Safe.engineEvent(JSON.stringify({ event: "vault", name: "2026-10-01-a", defined: true,
    sources: ["/etc/hosts", "rel", "/etc/hosts"], machine: { id: "0123456789abcdef", name: "lap\ntop" } }))
  assert.deepEqual(plainJson(v), { event: "vault", name: "2026-10-01-a", defined: true, sources: ["/etc/hosts"],
                                   machine: { id: "0123456789abcdef", name: "laptop" }, pushedAt: 0 })
  assert.equal(Safe.engineEvent(JSON.stringify({ event: "vault", name: "../x" })), null)
  assert.deepEqual(plainJson(Safe.engineEvent(JSON.stringify({ event: "vault", name: "2026-10-01-b", machine: { id: "bad" } }))),
                   { event: "vault", name: "2026-10-01-b", defined: false, sources: [], machine: { id: "", name: "" }, pushedAt: 0 })
  assert.equal(Safe.engineEvent(JSON.stringify({ event: "connected", branch: "a b" })).branch, "")
  assert.deepEqual(plainJson(Commands.connect("/p/engine.py", "https://github.com/a/b")),
                   ["/usr/bin/python3", "-I", "-S", "/p/engine.py", "connect", "https://github.com/a/b.git"])
  assert.equal(Commands.connect("/p/engine.py", "nope"), null)
  const linked = Safe.config({ vaults: [{ name: "2026-10-01-a", repoUrl: "https://github.com/a/b.git" }] })
  assert.equal(linked.vaults[0].repoUrl, "https://github.com/a/b")
})

test("reset", () => {
  assert.deepEqual(plainJson(Commands.reset("/p/engine.py")), ["/usr/bin/python3", "-I", "-S", "/p/engine.py", "reset"])
  assert.equal(Commands.reset("rel"), null)
  assert.deepEqual(plainJson(Safe.engineEvent('{"event":"reset","removed":["/x"]}')), { event: "reset" })
})

test("summary", () => {
  assert.equal(Safe.summary({ files: 1, bytes: 10, changed: 0, skippedCount: 0 }), "1 file \u00b7 10 B \u00b7 nothing new")
  assert.equal(Safe.summary({ files: 12, bytes: 49152, changed: 3, skippedCount: 2 }),
               "12 files \u00b7 48 KB \u00b7 3 to push \u00b7 2 skipped")
})

test("copy, push, status and diff argv are built only from valid values", () => {
  const argv = Commands.copy("/p/engine.py", "https://github.com/chyld/backups", "2026-10-01-omarchy", ["/etc/hosts"])
  assert.deepEqual(plainJson(argv), ["/usr/bin/python3", "-I", "-S", "/p/engine.py", "copy",
    "https://github.com/chyld/backups.git", "2026-10-01-omarchy", "--", "/etc/hosts"])
  assert.deepEqual(plainJson(Commands.copy("/p/engine.py", "https://github.com/a/b", "2026-10-01-a", ["/x"], true)),
    ["/usr/bin/python3", "-I", "-S", "/p/engine.py", "copy", "--adopt", "https://github.com/a/b.git", "2026-10-01-a", "--", "/x"])
  assert.equal(Commands.copy("/p/engine.py", "nope", "2026-10-01-omarchy", []), null)
  assert.equal(Commands.copy("/p/engine.py", "https://github.com/a/b", "x", []), null)
  assert.equal(Commands.copy("/p/engine.py", "https://github.com/a/b", "2026-10-01-a", ["--help"]), null)
  assert.deepEqual(plainJson(Commands.push("/p/engine.py", "https://github.com/a/b")),
                   ["/usr/bin/python3", "-I", "-S", "/p/engine.py", "push", "https://github.com/a/b.git"])
  assert.equal(Commands.status("/p/engine.py", "x"), null)
  assert.deepEqual(plainJson(Commands.diff("", "/p/engine.py", "https://github.com/a/b", "2026-10-01-a")),
    ["/usr/share/omarchy/bin/omarchy-launch-tui", "--app-id=org.omarchy.file-vault",
     "/usr/bin/python3", "-I", "-S", "/p/engine.py", "diff", "https://github.com/a/b.git", "2026-10-01-a"])
  assert.equal(Commands.diff("", "/p/engine.py", "https://github.com/a/b", "../x"), null)
})

test("pick argv", () => {
  assert.deepEqual(plainJson(Commands.pick(true, "/home/u")), ["/usr/bin/zenity", "--file-selection", "--multiple",
    "--separator=\n", "--title=File Vault: add folders", "--directory", "--filename=/home/u/"])
  assert.equal(Commands.pick(false, "").includes("--directory"), false)
})

test("environment passes only clean values", () => {
  const env = Commands.environment("/home/u", "/run/user/1000", "unix:path=/run/user/1000/bus", "rel", undefined)
  assert.deepEqual(plainJson(env), { PATH: "/usr/bin:/bin", LC_ALL: "C", HOME: "/home/u",
    XDG_RUNTIME_DIR: "/run/user/1000", DBUS_SESSION_BUS_ADDRESS: "unix:path=/run/user/1000/bus" })
})
