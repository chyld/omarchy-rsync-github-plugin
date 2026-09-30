import QtQuick
import qs.Commons
import qs.Ui
import "Safe.js" as Safe

// Bar icon for File Vault. Clicking it opens a popup with the repository,
// the vaults in it, the chosen vault's actions, and Sync. A second view in
// the same popup creates a vault. Long lists go elsewhere: what a vault
// holds opens in a terminal, and the config in the default editor.
//
// Everything shown comes from the service, and every change goes through
// it; only the chosen vault and the view are this popup's own.
Panel {
  id: root
  moduleName: "chyld.file-vault"
  ipcTarget: "chyld.file-vault"

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // The service (Service.qml); null until the host has loaded it, so it is
  // looked up again until found.
  property var service: null

  function findService() {
    var shell = root.bar ? root.bar.shell : null
    var found = shell && typeof shell.serviceFor === "function" ? shell.serviceFor(root.moduleName) : null
    if (found !== root.service) root.service = found || null
  }

  Timer {
    interval: 1000
    repeat: true
    running: root.service === null
    triggeredOnStart: true
    onTriggered: root.findService()
  }

  onBarChanged: root.findService()

  onOpenedChanged: {
    if (!opened || !root.service) return
    // Picks up a hand edit even if the file watch missed it, and looks for
    // files changed since each vault's last copy.
    root.service.load()
    root.service.check()
    root.creating = false
    root.resetting = false
    urlField.text = root.service.repoUrl
    root.service.notice = ""
    root.keepChosen()
  }

  // The chosen vault stays chosen while it belongs to the connected
  // repository; otherwise the newest one is.
  function keepChosen() {
    for (var i = 0; i < root.sortedVaults.length; i++) if (root.sortedVaults[i].name === root.chosen) return
    root.chosen = root.sortedVaults.length > 0 ? root.sortedVaults[0].name : ""
  }

  // A reload (the popup opening, or config.json edited by hand) updates the
  // URL field unless it is being typed in, and the chosen vault if it went.
  Connections {
    target: root.service
    function onRepoUrlChanged() { if (!urlField.activeFocus) urlField.text = root.service.repoUrl }
    function onRepoVaultsChanged() { root.keepChosen() }
  }

  // ------------------------------------------------------------ state

  readonly property bool ready: root.service !== null && root.service.loaded
  readonly property bool busy: root.service !== null && root.service.busy
  readonly property bool connected: root.service !== null && root.service.repoUrl !== ""
  // The URL field holds a valid URL other than the connected repository's.
  readonly property string typedRepo: Safe.repoPage(urlField.text)
  readonly property bool urlChanged: root.typedRepo !== "" && root.service !== null && root.typedRepo !== root.service.repoUrl

  // The new-vault view, or the delete-local-data view, is showing instead
  // of the main one.
  property bool creating: false
  property bool resetting: false

  // Newest first: vault names start with their date.
  readonly property var sortedVaults: {
    var list = root.service ? root.service.repoVaults.slice() : []
    list.sort(function(a, b) { return a.name < b.name ? 1 : a.name > b.name ? -1 : 0 })
    return list
  }

  // The vault the popup acts on.
  property string chosen: ""
  readonly property var chosenVault: root.service && root.chosen ? root.service.vault(root.chosen) : null
  // The chosen vault can be copied into from this machine; see Service.owns.
  readonly property bool chosenOwned: root.service !== null && root.service.owns(root.chosenVault)
  readonly property bool chosenCopying: root.busy && root.service.job === "copy" && root.service.jobVault === root.chosen
  readonly property bool pushing: root.busy && root.service.job === "push"
  readonly property int waitingCount: root.service ? root.service.waiting.length : 0
  // Vaults with files changed since their last copy, and the chosen one's count.
  readonly property int outdatedCount: root.service ? root.service.outdated.length : 0
  readonly property int chosenStale: root.service && root.chosen ? (root.service.stale[root.chosen] || 0) : 0
  property string createError: ""

  function vaultStatus(v) {
    var s = root.service
    if (!s) return { text: "", color: root.barForeground }
    if (s.job === "copy" && s.jobVault === v.name) return { text: "copying\u2026", color: Color.accent }
    if (s.job === "push" && s.waiting.indexOf(v.name) !== -1) return { text: "pushing\u2026", color: Color.accent }
    if (s.errors[v.name]) return { text: "copy failed", color: Color.urgent }
    var c = s.stale[v.name] || 0
    if (c > 0) return { text: c + (c === 1 ? " file" : " files") + " to copy", color: Color.accent }
    var n = s.pending[v.name] || 0
    if (n > 0) return { text: n + (n === 1 ? " change" : " changes") + " to push", color: Color.accent }
    if (s.unpushed.indexOf(v.name) !== -1) return { text: "ready to push", color: Color.accent }
    if (!s.owns(v)) return { text: "from " + (v.machineName || "another machine"), color: Util.alpha(root.barForeground, 0.6) }
    if (s.pushedAt[v.name]) return { text: "synced " + Safe.shortTime(new Date(s.pushedAt[v.name]), new Date()), color: Color.accent }
    return { text: "never synced", color: Util.alpha(root.barForeground, 0.6) }
  }

  readonly property string headline: {
    var s = root.service
    if (!s) return "Starting"
    if (s.configError) return "Config problem"
    if (root.creating) return "New vault"
    if (root.resetting) return "Delete local data"
    if (s.connecting) return "Connecting\u2026"
    if (s.job === "copy") return "Copying " + s.jobVault
    if (s.job === "push") return "Pushing to GitHub"
    if (s.job === "reset") return "Deleting local data\u2026"
    if (s.pushError) return "Push failed"
    if (!s.repoUrl) return "Not connected"
    if (root.sortedVaults.length === 0) return "No vaults yet"
    for (var k in s.errors) return "Copy failed"
    if (root.outdatedCount > 0) return root.outdatedCount + (root.outdatedCount === 1 ? " vault" : " vaults") + " to copy"
    if (root.waitingCount > 0) return root.waitingCount + (root.waitingCount === 1 ? " vault" : " vaults") + " to push"
    return root.sortedVaults.length + (root.sortedVaults.length === 1 ? " vault" : " vaults")
  }

  readonly property string detail: {
    var s = root.service
    if (!s) return ""
    if (s.configError) return s.configError
    if (root.creating) return "Saved in the repository as <date>-<name>/"
    if (root.resetting) return "Start again as on a fresh install"
    if (s.busy) return s.stage
    if (s.pushError) return s.pushError
    if (!s.repoUrl) return "Enter a GitHub repository URL and press Connect"
    if (root.sortedVaults.length === 0) return "Create your first vault"
    return ""
  }

  function connect() {
    if (!root.service || !root.typedRepo) return
    urlField.text = root.typedRepo
    root.service.connect(root.typedRepo)
  }

  // Under the URL: the check in progress, why it failed, or what it found.
  readonly property var connectStatus: {
    var s = root.service
    if (!s) return { text: "", bad: false }
    if (urlField.invalid) return { text: "Use a URL like https://github.com/you/backups", bad: true }
    if (s.connecting) return { text: "\u2026  " + s.stage, bad: false }
    if (s.connectError) return { text: s.connectError, bad: true }
    if (root.urlChanged) return { text: "Press Connect to check this repository and switch to it.", bad: false }
    if (!s.repoUrl) return { text: "", bad: false }
    var c = s.connection && s.connection.url === s.repoUrl ? s.connection : null
    if (!c) return { text: "Not checked yet \u00b7 press Check to verify access", bad: false }
    var text = "Connected \u00b7 push access checked " + Safe.shortTime(new Date(c.at), new Date())
    if (c.empty) text += " \u00b7 empty repository"
    else text += " \u00b7 " + c.vaults.length + (c.vaults.length === 1 ? " vault" : " vaults") + " on " + c.branch
    if (c.imported > 0) text += " \u00b7 " + c.imported + " added here"
    return { text: text, bad: false }
  }

  function startCreate() {
    dateField.text = Safe.isoDate(new Date())
    nameField.text = ""
    root.createError = ""
    root.creating = true
    nameField.forceActiveFocus()
  }

  function createVault() {
    if (!root.service) return
    var error = root.service.createVault(dateField.text, nameField.text)
    root.createError = error
    if (error) return
    root.chosen = Safe.cleanDate(dateField.text) + "-" + nameField.text.trim()
    root.creating = false
  }

  // The bar mark's state; see Logo.qml.
  readonly property string logoPhase: {
    var s = root.service
    if (!s) return "idle"
    if (s.busy) return "busy"
    if (s.configError || s.pushError || Object.keys(s.errors).length > 0) return "error"
    return root.waitingCount > 0 || root.outdatedCount > 0 ? "waiting" : "idle"
  }

  // What the new vault will be called, or "".
  readonly property string newName: Safe.vaultName(Safe.cleanDate(dateField.text) + "-" + nameField.text.trim())

  // ------------------------------------------------------------ bar icon

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    iconComponent: Component {
      Logo {
        phase: root.logoPhase
        color: button.foreground
      }
    }
    active: root.service !== null && (Object.keys(root.service.errors).length > 0 || root.service.configError !== ""
      || root.service.pushError !== "")
    tooltipText: root.opened ? "" : Safe.hostText("File Vault: " + root.headline, 60)
    onPressed: function(b) { root.toggle() }
  }

  // ------------------------------------------------------------ popup parts

  // The connected repository as owner/repo, for the header pill.
  readonly property string repoSlug: root.service && root.service.repoUrl
    ? root.service.repoUrl.slice("https://github.com/".length) : ""

  // The connection line's dot: checked, not checked yet, or failed.
  readonly property color connectColor: root.connectStatus.bad ? Color.urgent
    : root.connected && !root.urlChanged && root.service.connection !== null && !root.service.connecting ? Color.accent
    : Util.alpha(root.barForeground, 0.45)

  component Caption: Text {
    width: parent ? parent.width : 0
    textFormat: Text.PlainText
    wrapMode: Text.WordWrap
    color: Util.alpha(root.barForeground, 0.85)
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
    lineHeight: 1.1
  }

  // Quiet help text.
  component Hint: Caption {
    color: Util.alpha(root.barForeground, 0.5)
  }

  // A problem, in the urgent colour.
  component Warning: Caption {
    color: Util.alpha(Color.urgent, 0.95)
  }

  // A line with a coloured dot in front: a state.
  component StatusLine: Item {
    id: statusLine
    property string text: ""
    property color dot: root.barForeground
    width: parent ? parent.width : 0
    implicitHeight: statusText.implicitHeight

    Rectangle {
      id: statusDot
      x: Style.space(2)
      y: Math.round(statusText.font.pixelSize * 0.35)
      width: Style.space(7)
      height: width
      radius: width / 2
      color: statusLine.dot
    }

    Text {
      id: statusText
      anchors.left: statusDot.right
      anchors.leftMargin: Style.space(8)
      anchors.right: parent.right
      textFormat: Text.PlainText
      wrapMode: Text.WordWrap
      text: statusLine.text
      color: Util.alpha(root.barForeground, 0.8)
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }
  }

  // A small rounded label: a fact about the chosen vault.
  component Chip: Rectangle {
    id: chip
    property string text: ""
    property color tint: root.barForeground
    implicitWidth: chipText.implicitWidth + Style.space(14)
    implicitHeight: chipText.implicitHeight + Style.space(6)
    radius: height / 2
    color: Util.alpha(chip.tint, 0.12)
    border.width: 1
    border.color: Util.alpha(chip.tint, 0.25)

    Text {
      id: chipText
      anchors.centerIn: parent
      textFormat: Text.PlainText
      text: chip.text
      color: chip.tint
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
      font.bold: true
    }
  }

  // A header tool: an icon button like Omarchy's own panels have.
  component Tool: PanelActionButton {
    foreground: Util.alpha(root.barForeground, 0.85)
    hoverColor: Color.accent
    fontFamily: Style.font.family
  }

  component Separator: PanelSeparator {
    width: parent ? parent.width : 0
    foreground: root.barForeground
  }

  component Section: PanelSectionHeader {
    foreground: root.barForeground
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      // Escape leaves the new-vault or delete view first, then closes.
      onCloseRequested: {
        if (root.creating) root.creating = false
        else if (root.resetting) root.resetting = false
        else root.close()
      }
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(10)

        // ---------------------------------------------------------- header

        // The mark, the name, what's happening, and the tools: delete local data, GitHub, the local repository in
        // the file manager, config.json in the editor.
        PanelHero {
          width: parent.width
          title: "File Vault"
          meta: Safe.plain(root.headline, 80)
          foreground: root.barForeground
          fontFamily: Style.font.family
          iconComponent: Component {
            Logo {
              implicitWidth: Style.font.display * 1.5
              implicitHeight: Style.font.display * 1.5
              phase: root.logoPhase
              color: root.barForeground
            }
          }
          trailingControl: Component {
            Row {
              visible: !root.creating && !root.resetting
              spacing: Style.space(2)

              Tool {
                iconText: "\u{f05e8}"   // nf-md-delete_forever
                tooltipText: "Delete all local data"
                hoverColor: Color.urgent
                enabled: root.service !== null && !root.busy
                onClicked: root.resetting = true
              }
              Tool {
                iconText: "\u{f02a4}"   // nf-md-github
                tooltipText: "Open on GitHub"
                enabled: root.connected
                onClicked: root.service.openRepo()
              }
              Tool {
                iconText: "\u{f0770}"   // nf-md-folder_open
                tooltipText: "Open the local repository"
                enabled: root.ready && root.connected
                onClicked: root.service.openFolder()
              }
              Tool {
                iconText: "\u{f03eb}"   // nf-md-pencil
                tooltipText: "Edit config.json"
                enabled: root.service !== null
                onClicked: { root.service.editConfig(); root.close() }
              }
            }
          }
        }

        Caption {
          visible: text !== ""
          text: Safe.plain(root.detail, 300)
          color: root.service && (root.service.configError || (root.service.pushError && !root.busy))
            ? Util.alpha(Color.urgent, 0.95) : Util.alpha(root.barForeground, 0.75)
        }

        Caption {
          visible: root.service !== null && root.service.notice !== ""
          text: root.service ? root.service.notice : ""
        }

        // Entries config.json had that were left out, until the next change
        // here saves the file without them.
        Warning {
          readonly property var list: root.service ? root.service.problems : []
          visible: list.length > 0
          text: list.length === 0 ? "" : "Ignored in config.json: " + list[0]
            + (list.length > 1 ? " (and " + (list.length - 1) + " more)" : "")
            + ". The next change here removes it; fix it with Edit to keep it."
        }

        Separator {}

        // ---------------------------------------------------------- new vault view

        Column {
          id: createView
          visible: root.creating
          width: parent.width
          spacing: Style.space(10)

          Section { text: "DATE AND NAME" }

          Row {
            width: parent.width
            spacing: Style.space(6)

            TextField {
              id: dateField
              width: Style.space(116)
              placeholderText: "yyyy-mm-dd"
              maximumLength: 10
              foreground: root.barForeground
              enabled: root.ready
              onTextChanged: root.createError = ""
              onAccepted: nameField.forceActiveFocus()
            }

            Text {
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: "\u2013"
              color: Util.alpha(root.barForeground, 0.5)
              font.family: Style.font.family
              font.pixelSize: Style.font.body
            }

            TextField {
              id: nameField
              width: parent.width - dateField.width - Style.space(10) - 2 * parent.spacing
              placeholderText: "name, e.g. omarchy"
              maximumLength: 64
              foreground: root.barForeground
              enabled: root.ready
              onTextChanged: root.createError = ""
              onAccepted: root.createVault()
            }
          }

          StatusLine {
            readonly property bool bad: root.createError !== "" || (!root.newName && nameField.text.trim() !== "")
            text: root.createError !== "" ? root.createError
              : root.newName ? "Creates " + root.newName + "/ in " + (root.repoSlug || "the repository")
              : !Safe.cleanDate(dateField.text) ? "Use a date like 2026-10-01."
              : "Letters, digits, dots, dashes or underscores."
            dot: bad ? Color.urgent : root.newName ? Color.accent : Util.alpha(root.barForeground, 0.45)
          }

          Hint {
            text: "Files are pushed as they are, not encrypted: keep the repository private. "
              + "Files that look like keys or credentials are skipped."
          }

          Row {
            width: parent.width
            spacing: Style.space(8)

            Button {
              width: (parent.width - parent.spacing) / 2
              text: "Cancel"
              iconText: "\u{f004d}"   // nf-md-arrow_left
              bordered: true
              foreground: root.barForeground
              onClicked: root.creating = false
            }

            Button {
              width: (parent.width - parent.spacing) / 2
              text: "Create"
              iconText: "\u{f0415}"   // nf-md-plus
              bordered: true
              selected: enabled
              enabled: root.ready && root.newName !== ""
              opacity: enabled ? 1 : 0.5
              foreground: root.barForeground
              onClicked: root.createVault()
            }
          }
        }

        // ---------------------------------------------------------- delete view

        Column {
          id: resetView
          visible: root.resetting
          width: parent.width
          spacing: Style.space(10)

          Section { text: "DELETED FROM THIS MACHINE" }

          StatusLine {
            text: "Settings \u00b7 ~/.config/file-vault\nthe repository, vaults and picked paths"
            dot: Color.urgent
          }

          StatusLine {
            text: "Local repositories \u00b7 ~/.local/share/file-vault"
            dot: Color.urgent
          }

          Hint {
            text: "GitHub and your own files are not touched, and the plugin stays installed. "
              + "Connect the same repository again to get your vaults back."
          }

          Warning {
            visible: root.waitingCount > 0
            text: root.waitingCount + (root.waitingCount === 1 ? " vault has" : " vaults have")
              + " copied changes that aren't on GitHub yet: they will be lost. Push first to keep them."
            font.bold: true
          }

          Row {
            width: parent.width
            spacing: Style.space(8)

            Button {
              width: (parent.width - parent.spacing) / 2
              text: "Cancel"
              iconText: "\u{f004d}"   // nf-md-arrow_left
              bordered: true
              foreground: root.barForeground
              onClicked: root.resetting = false
            }

            Button {
              width: (parent.width - parent.spacing) / 2
              text: "Delete everything"
              iconText: "\u{f05e8}"   // nf-md-delete_forever
              bordered: true
              enabled: root.service !== null && !root.busy
              opacity: enabled ? 1 : 0.5
              foreground: Color.urgent
              onClicked: {
                root.service.resetAll()
                root.resetting = false
                root.chosen = ""
              }
            }
          }
        }

        // ---------------------------------------------------------- main view

        Column {
          id: mainView
          visible: !root.creating && !root.resetting
          width: parent.width
          spacing: Style.space(10)

          // ------------------------------------------------ repository

          Section { text: "REPOSITORY" }

          Row {
            width: parent.width
            spacing: Style.space(8)

            TextField {
              id: urlField
              width: parent.width - connectButton.width - parent.spacing
              placeholderText: "https://github.com/you/backups"
              maximumLength: 200
              foreground: root.barForeground
              enabled: root.ready && !root.busy
              property bool invalid: text.trim() !== "" && Safe.repoUrl(text) === ""
              // Typing doesn't change anything: only Connect does.
              onTextChanged: if (activeFocus && root.service) root.service.connectError = ""
              onAccepted: root.connect()
            }

            // Connect, or Check again for the connected repository.
            Button {
              id: connectButton
              height: urlField.height
              text: root.service && root.service.connecting ? "Checking" : root.urlChanged || !root.connected ? "Connect" : "Check"
              iconText: root.service && root.service.connecting ? "\u{f04e6}" : "\u{f0337}"   // nf-md-sync / nf-md-link
              iconSpinning: root.service !== null && root.service.connecting
              tooltipText: "Check access to the repository and link its vaults"
              bordered: true
              selected: enabled && (root.urlChanged || !root.connected)
              enabled: root.ready && !root.busy && root.typedRepo !== ""
              opacity: enabled || (root.service && root.service.connecting) ? 1 : 0.5
              foreground: root.barForeground
              onClicked: root.connect()
            }
          }

          StatusLine {
            visible: text !== ""
            text: Safe.plain(root.connectStatus.text, 300)
            dot: root.connectColor
          }

          Separator {}

          // ------------------------------------------------ vault

          Section { text: "VAULT" }

          Row {
            visible: root.sortedVaults.length > 0
            width: parent.width
            spacing: Style.space(8)

            Dropdown {
              id: vaultPicker
              width: parent.width - newButton.width - parent.spacing
              showLabel: false
              foreground: root.barForeground
              fontFamily: Style.font.family
              // Names only: the chips below show the chosen vault's state.
              options: root.sortedVaults.map(function(v) { return { value: v.name, label: v.name } })
              onChanged: function(v) { root.chosen = v }

              // Picking an option assigns `value` inside Dropdown, which would
              // end a plain binding; this keeps it following the chosen vault
              // (a new vault, or one removed in the editor).
              Binding on value { value: root.chosen }
            }

            Button {
              id: newButton
              // A square the dropdown's height, so the row lines up.
              width: vaultPicker.height
              height: vaultPicker.height
              iconText: "\u{f0415}"   // nf-md-plus
              tooltipText: "New vault"
              bordered: true
              enabled: root.ready && root.connected
              opacity: enabled ? 1 : 0.5
              foreground: root.barForeground
              onClicked: root.startCreate()
            }
          }

          Button {
            visible: root.sortedVaults.length === 0
            width: parent.width
            text: "New vault"
            iconText: "\u{f0415}"   // nf-md-plus
            bordered: true
            selected: enabled
            enabled: root.ready && root.connected
            opacity: enabled ? 1 : 0.5
            foreground: root.barForeground
            onClicked: root.startCreate()
          }

          // The chosen vault at a glance, with its tools: every file, and
          // the changes waiting, each in a terminal.
          Item {
            visible: root.chosenVault !== null
            width: parent.width
            height: Math.max(chips.implicitHeight, vaultTools.implicitHeight)

            Row {
              id: chips
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(6)

              Chip {
                readonly property var status: root.chosenVault ? root.vaultStatus(root.chosenVault) : null
                text: status ? status.text : ""
                tint: status ? status.color : root.barForeground
              }
              Chip {
                readonly property int count: root.chosenVault ? root.chosenVault.sources.length : 0
                text: count + (count === 1 ? " item" : " items")
              }
            }

            Row {
              id: vaultTools
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Tool {
                iconText: "\u{f0279}"   // nf-md-format_list_bulleted
                tooltipText: "Every file, in a terminal"
                enabled: root.ready && root.chosenVault !== null && root.chosenVault.sources.length > 0
                onClicked: { root.service.showFiles(root.chosen); root.close() }
              }
              Tool {
                iconText: "\u{f08aa}"   // nf-md-file_compare
                tooltipText: "Changes waiting to push, in a terminal"
                enabled: root.ready && !root.busy && root.chosenVault !== null && root.chosenVault.repoUrl !== ""
                  && root.service.cloned
                onClicked: { root.service.showDiff(root.chosen); root.close() }
              }
            }
          }

          // A vault from another machine is read-only here until adopted.
          StatusLine {
            visible: root.chosenVault !== null && !root.chosenOwned
            text: root.chosenVault ? "Made on " + (root.chosenVault.machineName || "another machine")
              + ". Copying mirrors this machine's files, so anything that machine had and this one doesn't"
              + " would be removed from the vault on the next push. Adopt it only to back up this machine's"
              + " files into it." : ""
            dot: Util.alpha(root.barForeground, 0.45)
          }

          Button {
            visible: root.chosenVault !== null && !root.chosenOwned
            width: parent.width
            text: "Adopt on this machine"
            iconText: "\u{f0337}"   // nf-md-link
            bordered: true
            enabled: root.ready && !root.busy
            opacity: enabled ? 1 : 0.5
            foreground: root.barForeground
            onClicked: root.service.adopt(root.chosen)
          }

          Row {
            visible: root.chosenVault !== null
            width: parent.width
            spacing: Style.space(8)

            Button {
              width: (parent.width - parent.spacing) / 2
              text: "Add files"
              iconText: "\u{f0214}"   // nf-md-file
              bordered: true
              enabled: root.ready && !root.service.picking && root.chosenOwned
              opacity: enabled ? 1 : 0.5
              foreground: root.barForeground
              onClicked: { root.service.pick(root.chosen, false); root.close() }
            }

            Button {
              width: (parent.width - parent.spacing) / 2
              text: "Add folders"
              iconText: "\u{f024b}"   // nf-md-folder
              bordered: true
              enabled: root.ready && !root.service.picking && root.chosenOwned
              opacity: enabled ? 1 : 0.5
              foreground: root.barForeground
              onClicked: { root.service.pick(root.chosen, true); root.close() }
            }
          }

          Hint {
            visible: root.chosenVault !== null && root.chosenOwned
            text: "Ctrl+H shows hidden files in the picker \u00b7 remove items with \u{f03eb}"
          }

          Warning {
            visible: root.service !== null && root.service.pickError !== ""
            text: root.service ? root.service.pickError : ""
          }

          Separator { visible: root.chosenVault !== null }

          // ------------------------------------------------ sync

          // Two steps: copy this vault's files into the local repository,
          // then push the local repository to GitHub.
          Section {
            visible: root.chosenVault !== null
            text: "SYNC"
          }

          Row {
            visible: root.chosenVault !== null
            width: parent.width
            spacing: Style.space(6)

            Button {
              id: copyButton
              width: (parent.width - arrow.width - 2 * parent.spacing) / 2
              text: root.chosenCopying ? "Copying\u2026" : "Copy to repo"
              iconText: root.chosenCopying ? "\u{f04e6}" : "\u{f018f}"   // nf-md-sync / nf-md-content_copy
              iconSpinning: root.chosenCopying
              tooltipText: "Step 1: copy this vault's files into the local repository (nothing is sent)"
              bordered: true
              selected: enabled && (root.chosenStale > 0 || root.waitingCount === 0)
              enabled: root.ready && !root.busy && root.chosenVault !== null && root.chosenVault.repoUrl !== ""
                && root.chosenVault.sources.length > 0 && root.chosenOwned
              opacity: enabled || root.chosenCopying ? 1 : 0.5
              foreground: root.barForeground
              onClicked: root.service.copyFiles(root.chosen)
            }

            Text {
              id: arrow
              anchors.verticalCenter: copyButton.verticalCenter
              textFormat: Text.PlainText
              text: "\u{f0142}"   // nf-md-chevron_right
              color: root.waitingCount > 0 ? Color.accent : Util.alpha(root.barForeground, 0.35)
              font.family: Style.font.family
              font.pixelSize: Style.font.body + 4
            }

            Button {
              width: copyButton.width
              text: root.pushing ? "Pushing\u2026"
                : root.waitingCount > 0 ? "Push " + root.waitingCount + (root.waitingCount === 1 ? " vault" : " vaults")
                : "Push to GitHub"
              iconText: root.pushing ? "\u{f04e6}" : "\u{f0167}"   // nf-md-sync / nf-md-cloud_upload
              iconSpinning: root.pushing
              tooltipText: "Step 2: commit what was copied, for every vault, and push it to GitHub"
              bordered: true
              selected: enabled && root.waitingCount > 0
              enabled: root.ready && !root.busy && root.connected && root.waitingCount > 0
              opacity: enabled || root.pushing ? 1 : 0.5
              foreground: root.barForeground
              onClicked: root.service.push()
            }
          }

          Hint {
            visible: root.chosenVault !== null && root.chosenVault.repoUrl === ""
            text: root.connected ? "Not linked to a repository yet: press Check to link it to this one."
              : "Not linked to a repository yet: connect one to link it."
          }

          // Set by hand in config.json only.
          Warning {
            visible: root.chosenVault !== null && root.chosenVault.includeSecrets
            text: "Secrets included: keys and credentials in this vault are pushed to GitHub unencrypted."
          }

          Warning {
            readonly property string error: root.service && root.chosen ? (root.service.errors[root.chosen] || "") : ""
            visible: error !== ""
            text: error
          }

          // The last copy, and the last push that included this vault.
          Hint {
            visible: text !== "" && !root.chosenCopying
            text: {
              var v = root.chosenVault
              if (!v) return ""
              var parts = []
              var pushed = root.service.pushedAt[v.name]
              if (v.lastSummary) parts.push("Last copy \u00b7 " + v.lastSummary)
              if (pushed) parts.push("pushed " + Safe.shortTime(new Date(pushed), new Date()))
              return parts.join(" \u00b7 ")
            }
          }

          // The chosen vault couldn't be checked for changed files.
          Warning {
            readonly property string reason: root.service && root.chosen ? (root.service.checkFailed[root.chosen] || "") : ""
            visible: reason !== "" && root.chosenOwned
            text: "Couldn't check for changed files: " + reason
          }

          // When files were last checked; it happens every hour and when this opens.
          Hint {
            visible: root.chosenVault !== null && root.chosenOwned && root.service.checkedAt > 0
            text: root.service && root.service.checkedAt > 0
              ? "Checked for changed files at " + Safe.shortTime(new Date(root.service.checkedAt), new Date()) : ""
          }

          // The last copy skipped something: the list tool shows it all.
          Warning {
            readonly property var entry: root.service && root.chosen ? root.service.skipped[root.chosen] : undefined
            visible: entry !== undefined && entry.count > 0 && !root.chosenCopying
            text: entry ? entry.count + " skipped: " + Safe.plain(Safe.shortPath(entry.list[0] || "", root.service.home), 120)
              + (entry.count > 1 ? ", \u2026 (\u{f0279} lists them all)" : "") : ""
          }
        }
      }
    }
  }
}
