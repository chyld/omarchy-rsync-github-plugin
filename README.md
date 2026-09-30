<p align="center">
  <img src="icon.svg" alt="File Vault" width="128">
</p>

<h1 align="center">File Vault</h1>

<p align="center">
  <b>Back up the files that matter to GitHub, right from the Omarchy bar.</b><br>
  Dated vaults · full paths kept · rsync under the hood · never force-pushes · not encrypted
</p>

---

Back up hand-picked files and folders to one GitHub repository, from the
Omarchy bar.

> **Not encrypted.** Files are pushed to GitHub as they are. Anyone who can
> read the repository, or who gets hold of a GitHub token for it, can read
> them. Use a private repository, and don't rely on the name "vault": files
> that look like keys or credentials are skipped (see [Secrets](#secrets)),
> but that is a heuristic, not a guarantee.

- **Vaults** are dated folders in the repository, like `2026-10-01-omarchy`.
  One repository holds many vaults.
- **Full paths** are kept: `~/.bashrc` is saved as
  `2026-10-01-omarchy/home/<you>/.bashrc`, `/etc/hosts` as
  `2026-10-01-omarchy/etc/hosts`.
- **Sync is two steps.** **1 · Copy to repo** brings the vault's folder in
  the local repository up to date with rsync: only files whose size or
  modification time changed are copied, and a file you deleted, or removed
  from the vault, is removed too, so the folder stays an exact copy. Nothing
  is sent. **2 · Push to GitHub** commits what was copied (one commit per
  vault), brings in what others pushed and pushes. Where GitHub and this
  machine both changed the same vault file, this machine's copy wins. It
  never force-pushes.

## Install

```bash
omarchy plugin add https://github.com/chyld/omarchy-rsync-github-plugin --enable
```

## Use

1. Create a repository on GitHub (private recommended).
2. Click the File Vault mark (a hexagon) in the bar, paste its URL and press **Connect**. File
   Vault checks that GitHub is reachable, that you're logged in, that the
   repository exists and that your account can push to it (a dry-run push
   that sends nothing), then clones it. Only then is it saved.
3. Press **+** next to Vaults: pick a date (today by default), type a name,
   and press **Create**. The vault is linked to the connected repository.
4. **Add files** / **Add folders** opens the file picker. Press Ctrl+H in it
   to see hidden files like `.bashrc`.
5. Press **1 · Copy to repo**, check the changes with the diff icon if you
   like, then press **2 · Push to GitHub**. A vault always goes to the
   repository it is linked to.

### Knowing when to sync

Every hour, and each time you open the popup, File Vault checks each of
this machine's vaults for files that changed since its last copy: new,
edited or deleted files, and picked paths added or removed. It only
flags them: the bar mark's core lights up, the vault shows **N files to
copy**, and Copy to repo is highlighted. It never copies or pushes by
itself. The check is local and read-only (no git, no network); a file only
touched, or saved again unchanged, doesn't count. Copied changes not
pushed yet light the core too, and show as **to push**.

Connecting another repository switches the vault list to that repository's
vaults; the others are kept, and come back when you connect their
repository again. Vaults made before any repository was connected are linked
to the first one you connect.

### A new machine

Install File Vault, paste the same repository URL and press **Connect**.
Every vault already in the repository is added to the list, with the files
and folders it was made from: each vault folder holds its definition,
`.file-vault.json` (its picked paths, and a hash of the machine it came
from). Nothing is copied to or from your disk.

A vault from another machine is **read-only** here: Copy mirrors this
machine's files, so anything the other machine had and this one doesn't
would be removed from the vault on the next push. Press **Adopt on this
machine** only to back up this machine's files into it; otherwise make a
new vault. Restoring a vault's files onto this machine isn't part of File
Vault; the files are all on GitHub.

The popup's tools:

| Icon | Does |
| --- | --- |
| Delete forever (header) | **Delete local data**: deletes the settings (`~/.config/file-vault`) and the local repositories (`~/.local/share/file-vault`), as a fresh install has it, after a confirmation that warns about copied changes not pushed yet. GitHub, your own files and the plugin itself are not touched. |
| GitHub (header) | Opens the connected repository on GitHub. |
| Pencil (header) | Opens `config.json` in your default editor. Remove items, rename or delete vaults there (a vault's folder on GitHub stays); the plugin reloads when you save. A file that isn't valid JSON is never saved over: the popup shows the line and column to fix. |
| Folder (header) | Opens the local copy of the repository in your file manager (after connecting). |
| Diff (vault) | Shows the vault's copied changes that aren't pushed yet as a colored `git diff` in a terminal; with none waiting, its last pushed change. |
| List (vault) | Shows every file a sync would copy, and everything it would skip, in a terminal (`less`; `q` closes it). |

Requirements: Omarchy 4, `git`, `rsync`, `python3`, `zenity`, and a GitHub login git
can use (`gh auth login`, then `gh auth setup-git`).

## What it does on your system

| Where | What |
| --- | --- |
| `~/.config/file-vault/config.json` | The repository URL, your vaults and what each holds (0600). What waits to be pushed, and when each vault was last pushed, are read from git each time, never stored here. |
| `~/.local/share/file-vault/repos/<owner>/<repo>` | The local copy of the repository. It belongs to the plugin: File Vault only counts changes made by **1 · Copy to repo**, so don't edit files in it by hand. |
| GitHub | Only Connect and Push touch the network. The hourly check doesn't. |

To remove everything File Vault keeps on this machine, use **Delete local
data** in the popup; to uninstall the plugin as well,
`omarchy plugin remove chyld.file-vault` afterwards.

File Vault plans each sync itself, then hands rsync the exact list of files
to copy (`--files-from`). While planning, it skips and lists: `.git` folders inside picked
folders, files over 100 MB (GitHub refuses them), files you can't read (it
never uses sudo), and sockets, FIFOs and devices. Symlinks inside picked
folders are stored as links; a picked path that is itself a symlink is
followed. A vault is limited to 100,000 files and 2 GB.

## Secrets

While planning a copy, File Vault skips, and lists as skipped, files that
look like secrets:

- private keys: `id_rsa`, `id_ed25519` and the other `id_*` keys, `*.key`,
  `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, and any file that starts with a
  `-----BEGIN … PRIVATE KEY` block (a `.pem` holding only a certificate is
  kept);
- credentials: `.env` and `.env.*` (not `.env.example`, `.sample`,
  `.template` or `.dist`), `.netrc`, `.git-credentials`, `.pgpass`,
  `~/.aws/credentials`, `~/.config/gh/hosts.yml`, `~/.docker/config.json`,
  `~/.kube/config`, and GnuPG's private keys.

This catches the usual places, not a token pasted into any other file.
Public keys (`id_ed25519.pub`), `~/.ssh/config` and `known_hosts` are
copied.

To back up secrets anyway, set `"includeSecrets": true` on the vault in
`config.json` (the pencil). The popup then warns that the vault's keys and
credentials are pushed unencrypted.

## Design

- ID `chyld.file-vault`; kinds `service` + `bar-widget`.
- `Service.qml` owns all state and is the only writer of `config.json`;
  `Widget.qml` (one per monitor) only shows it. Which vault is chosen is
  per popup.
- `Service.qml` runs the long jobs (connect, copy, push, reset) one at a
  time on one runner, and the short commands (load, save, status, folder)
  in a queue on another; asking again for a waiting command replaces it.
- `engine.py` is the entry point; the work is in `fv/`: `common` (limits
  and validation), `places` (data folders, the lock, reset), `config`
  (config.json and vault definitions), `proc` (running git and rsync),
  `plan` (what a copy takes and skips, secrets included), `sync` (connect,
  copy, push, status) and `views` (list and diff). rsync copies the planned
  files, comparing size and time to the nanosecond. `engine.py` runs as an
  argv array under `/usr/bin/timeout` with a minimal environment
  (`Runner.qml`). Git runs with hooks, fsmonitor and commit signing off,
  and no terminal prompts.
- The same validation is written twice, in `Safe.js` for the shell and in
  `fv/common.py`. `tests/vectors.json` holds the cases both must agree on,
  and both test suites run them.
- Check is read-only too: it plans each of this machine's vaults as Copy
  would, and compares with the vault folder (size and time, then the bytes
  when only the time differs, and the execute bit, which git keeps). It
  reads the vaults from `config.json` and runs no git. The service runs it
  hourly, when the popup opens and after a load, never during a job.
- Status is read-only: it reads the index and history, and never stages
  anything. When each vault was last pushed comes from `git log`, cached in
  the local copy's `.git` folder until GitHub's branch moves.
- `keepLoaded` is set so a plugin hot-reload (installing or editing any
  plugin) doesn't kill a copy or push part way. The catch: after changing
  or updating this plugin, run `omarchy restart shell` to load the new
  `Service.qml`.
- One File Vault command at a time touches the local repositories: the
  engine takes a lock (`~/.local/share/file-vault/lock`) for status, copy,
  push, connect, diff and reset, and clears a git `index.lock` left by a
  killed git. Temporary files live in `~/.local/share/file-vault/tmp`, never
  in a repository's work tree, so a killed run can't leave anything to be
  committed.
- Out of scope: restoring a vault's files onto a machine. Deferred: removing a vault's folder from GitHub,
  scheduled syncs (the hourly check only flags what needs syncing).

## Tests

```bash
python3 -m unittest discover -s tests   # engine, against a local bare repo
node --test tests/                       # Safe.js and Commands.js
omarchy plugin validate .
```
