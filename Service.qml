import QtQuick
import Quickshell
import Quickshell.Io
import "Safe.js" as Safe
import "Commands.js" as Commands

// File Vault: picked files and folders, copied under their full paths into
// dated vault folders of a GitHub repository's local copy (sync button 1),
// then pushed to GitHub (sync button 2).
//
// This service holds the state and drives the helpers; the popup
// (Widget.qml) only shows it and calls the functions below.
//
//   Service.qml    state, the config file, the file picker, connect, copy
//                  and push,
//                  and opening the editor, a terminal and the file manager
//   Widget.qml     the bar icon and its popup
//   Logo.qml       the bar mark, changing with the state
//   Runner.qml     runs one command at a time: deadline, byte budget,
//                  minimal environment
//   engine.py      the config file and every file and git operation
//   Commands.js    every command the shell runs
//   Safe.js        validation of everything that is not a literal
//
// Nothing touches the network except Connect and Push.
Item {
  id: svc

  property var shell: null
  property var manifest: null

  readonly property string omarchyRoot: String(Quickshell.env("OMARCHY_PATH") || "")
  readonly property string home: Safe.sourcePath(String(Quickshell.env("HOME") || ""))

  // The engine, next to this file.
  readonly property string engineScript: {
    var url = String(Qt.resolvedUrl("engine.py"))
    return url.indexOf("file://") === 0 ? Safe.sourcePath(decodeURIComponent(url.slice(7))) : ""
  }

  // ------------------------------------------------------------ config

  // ~/.config/file-vault/config.json: the repository, the vaults and what
  // each one holds. This service writes it, and reads it again whenever it
  // changes, so it can also be edited by hand (Edit in the popup). Every
  // popup (one per monitor's bar) shows the same thing. Changes show at once
  // and are written in the background, one command at a time.
  //
  // A file that isn't valid JSON is never saved over: nothing can change
  // until it is fixed. Entries left out as invalid are listed in `problems`.
  readonly property string configPath: svc.home ? svc.home + "/.config/file-vault/config.json" : ""
  property var config: Safe.config(null)
  property bool loaded: false
  property string configError: ""
  property var problems: []
  property bool saveAgain: false
  property bool loadAgain: false
  // Counts changes made here, so a read that started before one is dropped.
  property int changes: 0

  Runner { id: io }

  Component.onCompleted: svc.load()

  FileView {
    id: configWatch
    path: svc.configPath
    preload: false
    blockAllReads: true
    watchChanges: true
    printErrors: false
    // An editor may replace the file, which ends the watch: watch again.
    onFileChanged: { svc.load(); Qt.callLater(svc.rewatch) }
  }

  // Watch again after a write: a watch set up before the file existed, or
  // on a file replaced by rename, may not fire.
  function rewatch() {
    configWatch.path = ""
    configWatch.path = svc.configPath
  }

  function load() {
    if (!svc.engineScript) { svc.configError = "Can't find engine.py."; return }
    if (io.running) { svc.loadAgain = true; return }
    svc.loadAgain = false
    var changes = svc.changes
    io.run(Commands.load(svc.engineScript), 10000, function(code, out, err) {
      svc.next()
      // A change made here since the read started is newer; its save follows.
      if (changes !== svc.changes || svc.saveAgain) return
      var result = null
      try { result = code === 0 ? JSON.parse(out) : null } catch (e) { result = null }
      if (result && result.config) {
        if (result.machine && typeof result.machine === "object") {
          svc.machineId = Safe.machineId(result.machine.id)
          svc.machineName = Safe.plain(result.machine.name || "", 64)
        }
        svc.config = Safe.config(result.config)
        svc.problems = Array.isArray(result.problems) ? result.problems.slice(0, 20).map(function(p) { return Safe.plain(p, 200) }) : []
        svc.loaded = true
        svc.configError = ""
        // Create the file on first start, so there is a file to watch and
        // to open with Edit.
        if (result.exists === false) svc.save()
        svc.refreshStatus()
      } else {
        svc.loaded = false
        svc.problems = []
        svc.configError = Safe.plain(String(err || "Couldn't read config.json").trim().replace(/\.?$/, "."), 200)
          + " Fix it with Edit."
      }
    })
  }

  // Whatever is waiting runs next: a save before a read.
  function next() {
    if (svc.saveAgain) Qt.callLater(svc.save)
    else if (svc.loadAgain) Qt.callLater(svc.load)
  }

  function setConfig(next) {
    if (!svc.loaded) return
    svc.changes++
    svc.config = Safe.config(next)
    svc.problems = []
    svc.save()
  }

  function save(then) {
    if (!svc.loaded) return
    if (io.running) { svc.saveAgain = true; return }
    svc.saveAgain = false
    var text = JSON.stringify(svc.config)
    var started = io.run(Commands.save(svc.engineScript), 10000, function(code, out, err) {
      svc.configError = code === 0 ? "" : "Couldn't save config.json: " + Safe.plain(err || "error " + code, 160)
      svc.rewatch()
      if (typeof then === "function") then(code === 0)
      svc.next()
    }, text)
    if (!started) svc.saveAgain = true
  }

  // Opens config.json in the default editor, saving it first so it exists.
  // A file that can't be read is opened as it is, to be fixed.
  function editConfig() {
    var argv = Commands.edit(svc.omarchyRoot, svc.configPath)
    if (!argv) return
    if (svc.loaded && !io.running) svc.save(function() { Quickshell.execDetached(argv) })
    else Quickshell.execDetached(argv)
  }

  // This machine: a hash of /etc/machine-id, and its host name. Each vault
  // records the machine it belongs to.
  property string machineId: ""
  property string machineName: ""

  // Whether this machine may copy into the vault: it was made here (or
  // before machines were recorded), or adopted here. A vault from another
  // machine is read-only until adopted, because copying mirrors this
  // machine's files and would remove everything that machine had and this
  // one doesn't.
  function owns(v) {
    return v !== null && v !== undefined && (!v.machine || !svc.machineId || v.machine === svc.machineId)
  }

  // Lets this machine copy into a vault made on another one.
  function adopt(name) {
    svc.setConfig(Safe.withVault(svc.config, name, function(v) {
      v.machine = svc.machineId
      v.machineName = svc.machineName
    }))
  }

  readonly property string repoUrl: svc.config.repoUrl
  readonly property var vaults: svc.config.vaults

  function vault(name) { return Safe.findVault(svc.config, name) }

  // The connected repository's vaults, and the ones not linked to any
  // repository yet (linked to it by the next Connect). Vaults linked to
  // another repository are kept for when it is connected again.
  readonly property var repoVaults: svc.vaults.filter(function(v) { return v.repoUrl === svc.repoUrl || v.repoUrl === "" })

  // ------------------------------------------------------------ connect

  readonly property bool connecting: svc.job === "connect"
  // Recorded for a vault found in the repository without a definition: it
  // came from some other machine, so it is read-only until adopted.
  readonly property string unknownMachine: "0000000000000000"
  property string connectError: ""
  // The last successful check this session: { url, branch, empty, vaults,
  // imported, at }.
  property var connection: null

  // Checks the repository can be used (reachable, logged in, exists, push
  // allowed) and clones or updates its local copy. Only then is it saved as
  // the connected repository, vaults not linked to any repository yet are
  // linked to it, and vaults in the repository this machine doesn't know
  // are added, with the picked paths their definitions record. Those belong
  // to the machine that made them; one without a definition (made before
  // definitions were written) is marked as from another machine too.
  function connect(value) {
    var page = Safe.repoPage(value)
    svc.connectError = ""
    var found = []   // the vaults already in the repository, one event each
    svc.runJob("connect", "", Commands.connect(svc.engineScript, page), function(result, failure) {
      if (!result) { svc.connectError = failure; return }
      var next = Safe.config(svc.config)
      next.repoUrl = page
      for (var i = 0; i < next.vaults.length; i++) if (!next.vaults[i].repoUrl) next.vaults[i].repoUrl = page
      var imported = 0
      for (var j = 0; j < found.length; j++) {
        var r = found[j]
        var known = Safe.findVault(next, r.name)
        // When it last changed on GitHub, for a vault that doesn't know
        // (added here, or its settings deleted and connected again).
        if (known) {
          if (!known.lastSync && known.repoUrl === page) known.lastSync = r.pushedAt * 1000
          continue
        }
        if (next.vaults.length >= Safe.MAX_VAULTS) continue
        var unknown = !r.defined || !r.machine.id
        next.vaults.push({ name: r.name, repoUrl: page, sources: r.sources, lastSync: r.pushedAt * 1000, lastSummary: "",
                           machine: unknown ? svc.unknownMachine : r.machine.id,
                           machineName: unknown ? "" : r.machine.name })
        imported++
      }
      svc.connection = { url: page, branch: result.branch, empty: result.empty, vaults: found,
                         imported: imported, at: Date.now() }
      svc.setConfig(next)
    }, function(e) { if (found.length < Safe.MAX_VAULTS) found.push(e) })
  }

  // Adds the vault <date>-<name>; returns "" or why it can't.
  function createVault(date, name) {
    if (!svc.loaded) return "Still starting."
    if (!svc.repoUrl) return "Connect a repository first."
    if (!Safe.cleanDate(date)) return "Use a date like 2026-10-01."
    var full = Safe.vaultName(Safe.cleanDate(date) + "-" + String(name || "").trim())
    if (!full) return "Name: letters, digits, dots, dashes or underscores."
    if (svc.vault(full)) return full + " already exists."
    if (svc.vaults.length >= Safe.MAX_VAULTS) return "Too many vaults."
    var next = Safe.config(svc.config)
    next.vaults.push({ name: full, repoUrl: svc.repoUrl, sources: [], lastSync: 0, lastSummary: "",
                       machine: svc.machineId, machineName: svc.machineName })
    svc.setConfig(next)
    return ""
  }

  // Every file a sync of the vault would copy and skip, in a terminal.
  function showFiles(name) {
    var v = svc.vault(name)
    var argv = v ? Commands.listFiles(svc.omarchyRoot, svc.engineScript, name, v.sources) : null
    if (argv) Quickshell.execDetached(argv)
  }

  // ------------------------------------------------------------ local copy

  Runner { id: aux }
  property string notice: ""

  // Opens the local copy of the repository in the file manager. It exists
  // from the first connect on.
  function openFolder() {
    var argv = Commands.folder(svc.engineScript, svc.repoUrl)
    if (!argv || aux.running) return
    svc.notice = ""
    aux.run(argv, 10000, function(code, out) {
      var open = code === 0 ? Commands.openFolder(out.trim()) : null
      if (open) Quickshell.execDetached(open)
      else svc.notice = code === 4 ? "There is no local copy yet: press Connect." : "Couldn't find the local copy."
    })
  }

  // ------------------------------------------------------------ picker

  property bool picking: false
  property string pickVault: ""
  property int pickGeneration: 0
  property string pickError: ""

  // Opens the file picker for files, or for folders; what is picked is added
  // to the vault.
  function pick(name, folders) {
    if (svc.picking || !svc.vault(name)) return
    svc.picking = true
    svc.pickVault = name
    svc.pickError = ""
    svc.pickGeneration++
    svc.pickCode = null
    svc.pickText = null
    picker.command = Commands.pick(folders, svc.home)
    picker.running = true
  }

  function picked(generation, code, text) {
    if (generation !== svc.pickGeneration || !svc.picking) return
    svc.picking = false
    if (code === -1) { svc.pickError = "Couldn't open the file picker (zenity)."; return }
    if (code !== 0) return   // cancelled
    var name = svc.pickVault
    svc.setConfig(Safe.withVault(svc.config, name, function(v) { v.sources = Safe.addSources(v.sources, text) }))
  }

  // The picker's exit code and its output arrive separately; it is done
  // when both have.
  property var pickCode: null
  property var pickText: null

  function pickMaybeDone() {
    if (svc.pickCode === null || svc.pickText === null) return
    var code = svc.pickCode, text = svc.pickText
    svc.pickCode = null
    svc.pickText = null
    svc.picked(svc.pickGeneration, code, text)
  }

  Process {
    id: picker
    property bool started: false
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: { svc.pickText = text.slice(0, 1048576); svc.pickMaybeDone() }
    }
    onStarted: picker.started = true
    onExited: function(code) { svc.pickCode = code; svc.pickMaybeDone() }
    // A picker that failed to start never exits.
    onRunningChanged: {
      if (running) return
      var generation = svc.pickGeneration
      var started = picker.started
      picker.started = false
      if (!started) Qt.callLater(function() { svc.picked(generation, -1, "") })
    }
  }

  // ------------------------------------------------------------ copy and push

  Runner { id: runner }

  // What the runner is doing: "connect", "copy" or "push", one at a time,
  // and for which vault (copy).
  property string job: ""
  property string jobVault: ""
  readonly property bool busy: svc.job !== ""
  property string stage: ""
  // Per vault: the last copy's error, and the paths it skipped.
  property var errors: ({})
  property var skipped: ({})
  property string pushError: ""

  function setIn(map, key, value) {
    var next = {}
    for (var k in map) next[k] = map[k]
    if (value === undefined) delete next[key]
    else next[key] = value
    return next
  }

  // Runs one engine command that reports JSON lines. `done(result, failure)`
  // gets its last event, or null and why it failed; `each(event)`, when
  // given, gets every other event as it arrives.
  function runJob(kind, vault, argv, done, each) {
    // A reset also works with a config.json that can't be read: it is the
    // way out.
    if (!argv || (!svc.loaded && kind !== "reset") || svc.busy || runner.running) return
    svc.job = kind
    svc.jobVault = vault
    svc.stage = "Starting"
    var result = null, error = ""
    var started = runner.run(argv, 1800000, function(code, out, err) {
      svc.job = ""
      svc.jobVault = ""
      svc.stage = ""
      var failure = result ? "" : error
        || (code === 124 || /timed out/.test(err) ? "It took too long and was stopped." : "")
        || Safe.plain(err, 200) || "It stopped unexpectedly."
      done(result, failure)
      svc.refreshStatus()
    }, null, function(line) {
      var e = Safe.engineEvent(line)
      if (!e) return
      if (e.event === "step") svc.stage = e.text
      else if (e.event === "error") error = e.message
      else if (each && e.event === "vault") each(e)
      else result = e
    })
    if (!started) {
      svc.job = ""
      svc.jobVault = ""
      done(null, "Couldn't start it.")
    }
  }

  // Sync button 1: the vault's folder in the local copy becomes an exact
  // copy of what it holds (rsync copies only what changed), staged, not
  // committed. Nothing leaves the machine.
  function copyFiles(name) {
    var v = svc.vault(name)
    if (!v || !v.repoUrl || !svc.owns(v)) return
    svc.errors = svc.setIn(svc.errors, name, undefined)
    // Adopted (or made) here and recorded as this machine's: the engine may
    // copy over a definition from another machine.
    var adopted = svc.machineId !== "" && v.machine === svc.machineId
    var argv = Commands.copy(svc.engineScript, v.repoUrl, name, v.sources, adopted)
    svc.runJob("copy", name, argv, function(result, failure) {
      if (!result) { svc.errors = svc.setIn(svc.errors, name, failure); return }
      svc.skipped = svc.setIn(svc.skipped, name, { list: result.skipped, count: result.skippedCount })
      var summary = Safe.summary(result)
      svc.setConfig(Safe.withVault(svc.config, name, function(x) {
        x.lastSummary = summary
        // A vault made before machines were recorded is this machine's.
        if (!x.machine) { x.machine = svc.machineId; x.machineName = svc.machineName }
      }))
    })
  }

  // Sync button 2: everything copied into the local copy, for every vault,
  // committed and pushed to the connected repository.
  function push() {
    svc.pushError = ""
    svc.runJob("push", "", Commands.push(svc.engineScript, svc.repoUrl), function(result, failure) {
      if (!result) { svc.pushError = failure; return }
      var now = Date.now()
      var next = Safe.config(svc.config)
      for (var i = 0; i < next.vaults.length; i++)
        if (result.vaults.indexOf(next.vaults[i].name) !== -1) next.vaults[i].lastSync = now
      svc.setConfig(next)
    })
  }

  // A vault's copied changes (or its last pushed one) as a diff, in a
  // terminal.
  function showDiff(name) {
    var v = svc.vault(name)
    var argv = v && !svc.busy ? Commands.diff(svc.omarchyRoot, svc.engineScript, v.repoUrl, name) : null
    if (argv) Quickshell.execDetached(argv)
  }

  // Deletes everything File Vault keeps on this machine (settings and local
  // copies of repositories), as a fresh install has it. GitHub is not
  // touched. The empty config.json is made again, as on a first start.
  function resetAll() {
    if (svc.busy || io.running) return
    svc.saveAgain = false
    svc.runJob("reset", "", Commands.reset(svc.engineScript), function(result, failure) {
      if (!result) { svc.notice = "Couldn't delete everything: " + failure; return }
      svc.changes++
      svc.config = Safe.config(null)
      svc.problems = []
      svc.configError = ""
      svc.connection = null
      svc.connectError = ""
      svc.pushError = ""
      svc.errors = {}
      svc.skipped = {}
      svc.pending = {}
      svc.unpushed = []
      svc.cloned = false
      svc.notice = "Deleted all local data. Paste a repository URL to start again."
      svc.load()
    })
  }

  // ------------------------------------------------------------ status

  // Read from the local copy of the connected repository: per vault, the
  // changes copied but not pushed, and the vaults with commits not pushed.
  property var pending: ({})
  property var unpushed: []
  property bool cloned: false
  property bool statusAgain: false

  Runner { id: probe }

  // Asked for often (a load, a save, the end of every job): the requests
  // within a quarter second become one read.
  function refreshStatus() { statusSoon.restart() }

  Timer {
    id: statusSoon
    interval: 250
    onTriggered: svc.readStatus()
  }

  // Not while a job runs (the engine would wait for it anyway): asked again
  // when the job ends.
  function readStatus() {
    var argv = Commands.status(svc.engineScript, svc.repoUrl)
    if (!argv) { svc.pending = {}; svc.unpushed = []; svc.cloned = false; return }
    if (svc.busy || probe.running) { svc.statusAgain = true; return }
    svc.statusAgain = false
    var url = svc.repoUrl
    probe.run(argv, 60000, function(code, out) {
      var lines = out.split("\n")
      for (var i = lines.length - 1; i >= 0; i--) {
        var e = Safe.engineEvent(lines[i])
        if (!e || e.event !== "status") continue
        if (url === svc.repoUrl) { svc.pending = e.pending; svc.unpushed = e.unpushed; svc.cloned = e.cloned }
        break
      }
      if (svc.statusAgain) svc.refreshStatus()
    })
  }

  onRepoUrlChanged: svc.refreshStatus()

  // Vaults of the connected repository waiting to be pushed.
  readonly property var waiting: {
    var list = []
    for (var k in svc.pending) list.push(k)
    for (var i = 0; i < svc.unpushed.length; i++) if (list.indexOf(svc.unpushed[i]) === -1) list.push(svc.unpushed[i])
    return list
  }

  function openRepo() {
    var argv = Commands.openUrl(svc.omarchyRoot, svc.repoUrl)
    if (argv) Quickshell.execDetached(argv)
  }
}
