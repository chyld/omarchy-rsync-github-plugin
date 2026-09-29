.pragma library
.import "Safe.js" as Safe

// Every command the shell runs for File Vault, built in one place. Each
// returns an argv array for Process.command: no shell is ever involved.
// All file and git work happens in engine.py, run under the system Python
// in isolated mode (-I -S), from this plugin's own directory.

var PYTHON = "/usr/bin/python3"
var TIMEOUT = "/usr/bin/timeout"
var ZENITY = "/usr/bin/zenity"
var DEFAULT_OMARCHY = "/usr/share/omarchy"

// The whole environment of every engine command; nothing else is inherited.
// A fixed PATH, the home and runtime directories, and the session bus (git
// asks the user's credential helper, which may use the keyring). Only
// absolute directory values are passed on.
function environment(home, runtimeDir, bus, configHome, ghConfigDir) {
  var env = { PATH: "/usr/bin:/bin", LC_ALL: "C" }
  var dirs = { HOME: home, XDG_RUNTIME_DIR: runtimeDir, XDG_CONFIG_HOME: configHome, GH_CONFIG_DIR: ghConfigDir }
  for (var k in dirs) if (Safe.sourcePath(String(dirs[k] || ""))) env[k] = Safe.sourcePath(String(dirs[k]))
  var b = String(bus || "")
  if (/^unix:[A-Za-z0-9=,\/._-]{1,200}$/.test(b)) env.DBUS_SESSION_BUS_ADDRESS = b
  return env
}

// ------------------------------------------------------------ engine.py

// ~/.config/file-vault/config.json, as one JSON object.
function load(script) { return [PYTHON, "-I", "-S", script, "load"] }

// config.json, written 0600 from stdin.
function save(script) { return [PYTHON, "-I", "-S", script, "save"] }

// Checks the repository can be used and clones or updates its local copy,
// as JSON lines. Null without a valid URL.
function connect(script, url) {
  var repo = Safe.repoUrl(url)
  return repo ? [PYTHON, "-I", "-S", script, "connect", repo] : null
}

// Sync button 1: the vault's folder in the local copy updated from
// `sources` (rsync) and staged, as JSON lines. Null when anything is invalid.
// `adopt` lets this machine copy over a vault made on another one.
function copy(script, url, vault, sources, adopt) {
  var repo = Safe.repoUrl(url)
  var name = Safe.vaultName(vault)
  if (!repo || !name || !Array.isArray(sources) || sources.length > Safe.MAX_SOURCES) return null
  for (var i = 0; i < sources.length; i++) if (!Safe.sourcePath(sources[i])) return null
  return [PYTHON, "-I", "-S", script, "copy"].concat(adopt === true ? ["--adopt"] : [], [repo, name, "--"], sources)
}

// Sync button 2: what was copied committed and pushed, as JSON lines.
function push(script, url) {
  var repo = Safe.repoUrl(url)
  return repo ? [PYTHON, "-I", "-S", script, "push", repo] : null
}

// Per vault, changes copied but not pushed; local only.
function status(script, url) {
  var repo = Safe.repoUrl(url)
  return repo ? [PYTHON, "-I", "-S", script, "status", repo] : null
}

// The local copy of the repository's path on stdout, or exit 4 before the
// first sync.
function folder(script, url) {
  var repo = Safe.repoUrl(url)
  return repo ? [PYTHON, "-I", "-S", script, "folder", repo] : null
}

// Deletes everything File Vault keeps on this machine: settings and local
// copies of repositories. GitHub is not touched.
function reset(script) {
  return Safe.sourcePath(script) ? [PYTHON, "-I", "-S", script, "reset"] : null
}

// ------------------------------------------------------------ picker

// The GTK file picker: several files, or several folders, one path per
// line on stdout. Starts in the home folder.
function pick(folders, home) {
  var argv = [ZENITY, "--file-selection", "--multiple", "--separator=\n",
              "--title=" + (folders ? "File Vault: add folders" : "File Vault: add files")]
  if (folders) argv.push("--directory")
  var start = Safe.sourcePath(String(home || ""))
  if (start) argv.push("--filename=" + start + "/")
  return argv
}

// ------------------------------------------------------------ Omarchy

// The bin folder of the Omarchy install in use, from $OMARCHY_PATH when it
// is a plain absolute path, else the packaged location.
function omarchyBin(root) {
  var s = typeof root === "string" ? root : ""
  if (!/^\/[A-Za-z0-9._\/-]{1,200}$/.test(s) || s.indexOf("..") !== -1) s = DEFAULT_OMARCHY
  return s + "/bin"
}

// Every file a sync of `vault` would copy and skip, in less, in a terminal.
function listFiles(root, script, vault, sources) {
  var name = Safe.vaultName(vault)
  if (!name || !Safe.sourcePath(script) || !Array.isArray(sources) || sources.length > Safe.MAX_SOURCES) return null
  for (var i = 0; i < sources.length; i++) if (!Safe.sourcePath(sources[i])) return null
  return [omarchyBin(root) + "/omarchy-launch-tui", "--app-id=org.omarchy.file-vault",
          PYTHON, "-I", "-S", script, "list", name, "--"].concat(sources)
}

// A vault's copied changes (or its last pushed one) as a diff, in less, in
// a terminal.
function diff(root, script, url, vault) {
  var repo = Safe.repoUrl(url)
  var name = Safe.vaultName(vault)
  if (!repo || !name || !Safe.sourcePath(script)) return null
  return [omarchyBin(root) + "/omarchy-launch-tui", "--app-id=org.omarchy.file-vault",
          PYTHON, "-I", "-S", script, "diff", repo, name]
}

// A file in the user's default editor, as Omarchy launches it.
function edit(root, path) {
  var p = Safe.sourcePath(path)
  return p ? [omarchyBin(root) + "/omarchy-launch-editor", p] : null
}

// A folder in the default file manager.
function openFolder(path) {
  var p = Safe.sourcePath(path)
  return p ? ["/usr/bin/xdg-open", p] : null
}

// Open on GitHub: the repository's page, a validated https github.com URL.
function openUrl(root, url) {
  var page = Safe.repoPage(url)
  return page ? [omarchyBin(root) + "/omarchy-launch-browser", page] : null
}
