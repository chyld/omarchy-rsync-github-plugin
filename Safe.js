.pragma library

// Validation of everything File Vault's QML handles that is not a literal:
// the repository URL, vault names, picked paths, the config file and the
// engine's output. Mirrors the checks in engine.py, which checks again.

var MAX_PATH = 4096
var MAX_VAULTS = 200
var MAX_SOURCES = 500
var MAX_LISTED = 20

// Control, bidirectional and other invisible characters.
var CONTROL = /[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]/

// https://github.com/<owner>/<repo>, normalized to ...<repo>.git, or "".
function repoUrl(value) {
  if (typeof value !== "string") return ""
  var m = /^https:\/\/github\.com\/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))\/([A-Za-z0-9._-]{1,100}?)(?:\.git)?\/?$/.exec(value.trim())
  if (!m || m[2] === "." || m[2] === "..") return ""
  return "https://github.com/" + m[1] + "/" + m[2] + ".git"
}

function repoSlug(value) {
  var url = repoUrl(value)
  return url ? url.slice("https://github.com/".length, -".git".length) : ""
}

// The repository's page, https://github.com/<owner>/<repo>, or "".
function repoPage(value) {
  var slug = repoSlug(value)
  return slug ? "https://github.com/" + slug : ""
}

function pad(n) { return n < 10 ? "0" + n : String(n) }

// yyyy-mm-dd for a Date, in local time.
function isoDate(date) {
  return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate())
}

// A real calendar date written yyyy-mm-dd, or "".
function cleanDate(value) {
  var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(typeof value === "string" ? value.trim() : "")
  if (!m) return ""
  var d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
  return isoDate(d) === m[0] ? m[0] : ""
}

// yyyy-mm-dd-<name>: a real date, then letters, digits, ., _ or -, or "".
function vaultName(value) {
  if (typeof value !== "string") return ""
  var m = /^(\d{4}-\d{2}-\d{2})-([A-Za-z0-9][A-Za-z0-9._-]{0,63})$/.exec(value)
  if (!m || !cleanDate(m[1]) || /(\.|\.lock)$/.test(value)) return ""
  return value
}

// An absolute path, not /, with no control characters and no ., .. or empty
// parts; a trailing / is dropped. "" otherwise.
function sourcePath(value) {
  if (typeof value !== "string" || value.length < 2 || value.length > MAX_PATH) return ""
  if (value.charAt(0) !== "/" || CONTROL.test(value)) return ""
  var s = value.replace(/\/+$/, "")
  if (s.length < 2) return ""
  var parts = s.split("/")
  for (var i = 1; i < parts.length; i++) {
    if (parts[i] === "" || parts[i] === "." || parts[i] === "..") return ""
  }
  return s
}

// A path for display: the home folder as ~.
function shortPath(path, home) {
  if (home && home.length > 1 && (path === home || path.indexOf(home + "/") === 0)) return "~" + path.slice(home.length)
  return path
}

// Text with control characters removed, cut to `max`.
function plain(value, max) {
  var s = String(value === undefined || value === null ? "" : value)
  s = s.replace(new RegExp(CONTROL.source, "g"), "")
  var cap = max || 120
  return s.length > cap ? s.slice(0, cap - 1) + "\u2026" : s
}

// Plain text safe for the host's tooltips, which may render markup.
function hostText(value, max) {
  return plain(String(value === undefined || value === null ? "" : value).replace(/[<>&]/g, ""), max)
}

function shortTime(date, now) {
  var hm = pad(date.getHours()) + ":" + pad(date.getMinutes())
  if (date.toDateString() === now.toDateString()) return hm
  var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
  return months[date.getMonth()] + " " + date.getDate() + " " + hm
}

// ------------------------------------------------------------ config

// The config with every unknown or invalid part dropped:
// { version: 1, repoUrl, vaults: [{ name, repoUrl, sources, lastSync, lastSummary, machine, machineName }] }
// The top-level repoUrl is the connected repository; each vault's is the one
// it is linked to ("" before its first connect).
function config(value) {
  var obj = value
  if (typeof value === "string") {
    try { obj = JSON.parse(value) } catch (e) { obj = null }
  }
  var out = { version: 1, repoUrl: "", vaults: [] }
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return out
  out.repoUrl = repoPage(obj.repoUrl)
  var seen = Object.create(null)
  var list = Array.isArray(obj.vaults) ? obj.vaults.slice(0, MAX_VAULTS) : []
  for (var i = 0; i < list.length; i++) {
    var v = list[i]
    if (!v || typeof v !== "object") continue
    var name = vaultName(v.name)
    if (!name || seen[name]) continue
    seen[name] = true
    var sources = []
    var raw = Array.isArray(v.sources) ? v.sources.slice(0, MAX_SOURCES) : []
    for (var j = 0; j < raw.length; j++) {
      var p = sourcePath(raw[j])
      if (p && sources.indexOf(p) === -1) sources.push(p)
    }
    var last = typeof v.lastSync === "number" && v.lastSync > 0 && v.lastSync < 4398046511104 ? Math.floor(v.lastSync) : 0
    out.vaults.push({ name: name, repoUrl: repoPage(v.repoUrl), sources: sources, lastSync: last,
                      machine: machineId(v.machine),
                      machineName: typeof v.machineName === "string" ? plain(v.machineName, 64) : "",
                      lastSummary: typeof v.lastSummary === "string" ? plain(v.lastSummary, 120) : "" })
  }
  return out
}

// A machine id: 16 hex digits (a hash of /etc/machine-id), or "".
function machineId(value) {
  return typeof value === "string" && /^[0-9a-f]{16}$/.test(value) ? value : ""
}

// The vault named `name` in `cfg`, or null.
function findVault(cfg, name) {
  for (var i = 0; i < cfg.vaults.length; i++) if (cfg.vaults[i].name === name) return cfg.vaults[i]
  return null
}

// A copy of `cfg` with the vault `name` changed by `fn(vault)`.
function withVault(cfg, name, fn) {
  var next = config(cfg)
  for (var i = 0; i < next.vaults.length; i++) if (next.vaults[i].name === name) fn(next.vaults[i])
  return config(next)
}

// Picked paths from the file picker's output, one per line, added to
// `existing` without duplicates. Invalid lines are dropped.
function addSources(existing, text) {
  var out = existing.slice()
  var lines = String(text || "").split("\n")
  for (var i = 0; i < lines.length && out.length < MAX_SOURCES; i++) {
    var p = sourcePath(lines[i])
    if (p && out.indexOf(p) === -1) out.push(p)
  }
  return out
}

// ------------------------------------------------------------ engine

// One line of `engine.py` connect, copy, push or status output, checked, or null.
function engineEvent(line) {
  var e
  try { e = JSON.parse(line) } catch (err) { return null }
  if (!e || typeof e !== "object") return null
  if (e.event === "step") return { event: "step", text: plain(e.text, 80) }
  if (e.event === "error") return { event: "error", message: plain(e.message, 300) }
  if (e.event === "vault") {
    // One vault already in the repository, with its definition.
    if (!vaultName(e.name)) return null
    var src = []
    var rawSrc = Array.isArray(e.sources) ? e.sources.slice(0, MAX_SOURCES) : []
    for (var q = 0; q < rawSrc.length; q++) if (sourcePath(rawSrc[q]) && src.indexOf(rawSrc[q]) === -1) src.push(rawSrc[q])
    var m = e.machine && typeof e.machine === "object" ? e.machine : {}
    return { event: "vault", name: e.name, defined: e.defined === true, sources: src,
             machine: { id: machineId(m.id), name: typeof m.name === "string" ? plain(m.name, 64) : "" },
             pushedAt: typeof e.pushedAt === "number" && e.pushedAt > 0 && e.pushedAt < 4398046511 ? Math.floor(e.pushedAt) : 0 }
  }
  if (e.event === "connected")
    return { event: "connected", branch: /^[A-Za-z0-9._\/-]{1,100}$/.test(e.branch || "") ? e.branch : "",
             empty: e.empty === true }
  var n = function(v) { return typeof v === "number" && v >= 0 && v < 1e15 ? Math.floor(v) : 0 }
  var vaultList = function(list, max) {
    var out = []
    var raw = Array.isArray(list) ? list.slice(0, max) : []
    for (var i = 0; i < raw.length; i++) if (vaultName(raw[i])) out.push(raw[i])
    return out
  }
  if (e.event === "reset") return { event: "reset" }
  if (e.event === "pushed")
    return { event: "pushed", vaults: vaultList(e.vaults, MAX_VAULTS),
             commit: /^[0-9a-f]{4,40}$/.test(e.commit || "") ? e.commit : "" }
  if (e.event === "status") {
    var pending = {}
    var raw = e.pending && typeof e.pending === "object" ? e.pending : {}
    var keys = Object.keys(raw).slice(0, MAX_VAULTS)
    for (var k = 0; k < keys.length; k++) if (vaultName(keys[k]) && n(raw[keys[k]]) > 0) pending[keys[k]] = n(raw[keys[k]])
    return { event: "status", cloned: e.cloned === true, pending: pending, unpushed: vaultList(e.unpushed, MAX_VAULTS) }
  }
  if (e.event !== "copied") return null
  var skipped = []
  var rawSkipped = Array.isArray(e.skipped) ? e.skipped.slice(0, MAX_LISTED) : []
  for (var i = 0; i < rawSkipped.length; i++) if (typeof rawSkipped[i] === "string") skipped.push(plain(rawSkipped[i], 300))
  return { event: "copied", files: n(e.files), bytes: n(e.bytes), changed: n(e.changed),
           skipped: skipped, skippedCount: n(e.skippedCount) }
}

function size(bytes) {
  if (bytes < 1024) return bytes + " B"
  if (bytes < 1048576) return Math.round(bytes / 1024) + " KB"
  if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + " MB"
  return (bytes / 1073741824).toFixed(2) + " GB"
}

// "12 files · 48 KB · 3 to push"
function summary(e) {
  var parts = [e.files + (e.files === 1 ? " file" : " files"), size(e.bytes)]
  parts.push(e.changed === 0 ? "nothing new" : e.changed + " to push")
  if (e.skippedCount > 0) parts.push(e.skippedCount + " skipped")
  return parts.join(" \u00b7 ")
}
