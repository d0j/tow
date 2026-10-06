# Installing TOW

Step by step, for people who have never installed a program from GitHub. Pick your system, follow the steps, and
TOW opens in your browser. Русская версия: [ru/install.md](ru/install.md).

- [Windows](#windows)
- [macOS](#macos)
- [Linux](#linux)
- [Where TOW lives](#where-tow-lives)
- [Start, stop, update](#start-stop-update)
- [Back up the master key](#back-up-the-master-key)
- [Remove TOW](#remove-tow)
- [If something goes wrong](#if-something-goes-wrong)
- [Manual install with git](#manual-install-with-git)

TOW keeps everything in one folder: the program, its own Python, your settings and data. It installs nothing into
Windows, macOS or Linux, and needs no administrator rights.

## Windows

Windows 10 or 11, 64-bit.

### The zip file (easiest)

1. Download [TOW-windows-x64.zip](https://github.com/d0j/tow/releases/latest/download/TOW-windows-x64.zip) (about
   50 MB).
2. Optional, saves one question later: right-click the zip → **Properties** → tick **Unblock** → **OK**.
3. Right-click the zip → **Extract All…** → choose a folder, for example `C:\TOW` or your user folder → **Extract**.
   You get a folder `TOW` with `Start TOW.cmd` inside.
4. Double-click **Start TOW.cmd**.
   - Windows may say **"Windows protected your PC"**. Click **More info**, then **Run anyway**. It says this about
     every program that is not from the Microsoft Store and has no paid signature.
   - It may also ask **"Do you want to run this file?"** — click **Run**.
5. A black window opens and says **"Preparing TOW…"**. The first start takes a minute or two and needs no internet.
6. The window shows where your **master key** is and waits: copy that file now (see
   [Back up the master key](#back-up-the-master-key)), then press any key. The window closes; TOW keeps running.
7. Your browser opens **<http://127.0.0.1:8787>**. That is TOW.

Next time, double-click **Start TOW.cmd** again: if TOW is running, it only opens the page.

### One line in PowerShell

The same result without clicking through Explorer. Open PowerShell: right-click the **Start** button →
**Terminal** (or **Windows PowerShell**). Paste and press Enter:

```powershell
irm https://github.com/d0j/tow/releases/latest/download/install.ps1 | iex
```

It downloads the zip, checks it against the release's checksum file, unpacks it into `TOW` in your user folder and
starts it. With options (another folder, start with Windows, another port):

```powershell
& ([scriptblock]::Create((irm https://github.com/d0j/tow/releases/latest/download/install.ps1))) -Dir D:\TOW -Autostart -Port 8788
```

## macOS

A Mac with Apple silicon (M1 or newer). Intel Macs work only with developer tools installed (see
[If something goes wrong](#if-something-goes-wrong)).

1. Open **Terminal**: press **Cmd + Space**, type `Terminal`, press **Enter**.
2. Paste this line and press **Enter**:

   ```sh
   curl -LsSf https://github.com/d0j/tow/releases/latest/download/install.sh | sh
   ```

   It takes a few minutes: it downloads TOW, its own Python and the libraries into `TOW` in your home folder. At the
   end it shows where the **master key** is — copy it (see [Back up the master key](#back-up-the-master-key)).
3. Start TOW: in Finder choose **Go → Home**, open **TOW**, double-click **Start TOW.command**. A Terminal window
   opens, TOW starts, and the browser opens **<http://127.0.0.1:8787>**. You can close the Terminal window; TOW keeps
   running.

To start TOW whenever you sign in, add `-s -- --autostart` to the line in step 2 (after `sh`), or later run
`~/TOW/app/scripts/tow autostart on`.

Keep the `TOW` folder and the folders your torrents download into out of **Documents**, **Desktop** and
**Downloads**: macOS does not let programs in the background read them.

## Linux

Any 64-bit distribution (x86-64 or ARM, a Raspberry Pi with a 64-bit system too) with `curl` (or `wget`) and `tar`.

1. Open a terminal: on Ubuntu press **Ctrl + Alt + T**; elsewhere find **Terminal** in the applications menu.
2. Paste this line and press **Enter**:

   ```sh
   curl -LsSf https://github.com/d0j/tow/releases/latest/download/install.sh | sh
   ```

   It installs into `~/TOW` and shows where the **master key** is — copy it.
3. Start TOW: `~/TOW/start-tow`. The browser opens **<http://127.0.0.1:8787>** (on a server without a screen it
   prints the address instead).

Options go after `sh -s --`, for example:

```sh
curl -LsSf https://github.com/d0j/tow/releases/latest/download/install.sh | sh -s -- --autostart --desktop
```

| Option | Does |
|---|---|
| `--autostart` | start TOW when you sign in (a systemd user service) |
| `--desktop` | add TOW to the applications menu |
| `--dir DIR` | install somewhere else than `~/TOW` |
| `--port N` | another port than 8787 |
| `--version v1.22.0` | a particular release |

On a server, to start TOW at boot before anyone signs in: `~/TOW/app/scripts/tow autostart on --without-login`,
then `sudo loginctl enable-linger $USER`. To open the page from another computer, see
[Remote access](../README.md#remote-access).

## Where TOW lives

```
TOW/
  Start TOW.cmd / Start TOW.command / start-tow     start (and open the page)
  Stop TOW.cmd  / Stop TOW.command  / stop-tow      stop
  Update TOW.cmd / Update TOW.command / update-tow  update to the latest release
  config.yaml      settings
  data/            your topics, history, logs
  keys/master.key  the master key
  backup/          night copies
  app/             the program
  runtime/         its Python and libraries
```

Move or copy the whole folder: the start file prepares it again in the new place.

## Start, stop, update

| | Windows | macOS | Linux |
|---|---|---|---|
| Start, open the page | `Start TOW.cmd` | `Start TOW.command` | `~/TOW/start-tow` |
| Stop | `Stop TOW.cmd` | `Stop TOW.command` | `~/TOW/stop-tow` |
| Start with the computer | `app\scripts\tow.cmd autostart on` | `~/TOW/app/scripts/tow autostart on` | the same |
| Update to the latest release | `Update TOW.cmd` | `Update TOW.command` | `~/TOW/update-tow` |
| Go back to an earlier release | `"Update TOW.cmd" v1.23.0` in a terminal | `~/TOW/update-tow v1.23.0` | the same |

An update stops TOW, keeps a copy of your data and settings in `backup/`, puts in the new version, starts it and
checks it. If anything fails, the previous version comes back by itself. It needs the internet. When TOW is
already the latest release, it says so and does nothing. It goes back
only to a version that can read your data: not before v1.23.0 once v1.23 has run (it says so and changes
nothing).

## Back up the master key

The first start creates `TOW/keys/master.key`. It encrypts every saved password and token. Backups and `.towx`
files do not contain it, on purpose. **Copy it to a USB stick or a password manager now.** Without it, saved
passwords cannot be read, not even from a backup.

## Remove TOW

| | |
|---|---|
| Windows | `& ([scriptblock]::Create((irm https://github.com/d0j/tow/releases/latest/download/install.ps1))) -Uninstall` |
| macOS, Linux | `curl -LsSf https://github.com/d0j/tow/releases/latest/download/install.sh \| sh -s -- --uninstall` |

It asks before it removes anything, turns autostart off and stops TOW. Your `data`, `keys`, `config.yaml` and
`backup` stay in the folder unless you say otherwise (or add `-Purge` / `--purge`). `-Dir` / `--dir` names another
folder than the default. Or simply stop TOW, turn autostart off and delete the folder.

What stays is picked up again: installing into the same folder later keeps your data, key and settings. To delete
what stayed, run the remove command again with `-Purge` / `--purge`.
The installer marks a TOW folder so removal cannot mistake another program's `data` and `config.yaml` for TOW.
For data kept by an older installer without this marker, use `-AdoptData` / `--adopt-data` only after checking
that the folder is your former TOW install.

## If something goes wrong

| What you see | What to do |
|---|---|
| "Windows protected your PC" | **More info** → **Run anyway**. Or before extracting: zip → **Properties** → **Unblock**. |
| "TOW did not start", and `data/logs/run.log` says port 8787 is in use | Another program uses that port (often a second TOW). Open `config.yaml` in the TOW folder, change `port: 8787` to `port: 8788`, start again and open <http://127.0.0.1:8788>. |
| The page does not open | Wait a minute and open <http://127.0.0.1:8787> by hand. `tow status` says whether TOW runs; the reason of a failed start is in `TOW/data/logs/run.log`. |
| The installer cannot download (company network, antivirus) | It needs `github.com` and, on macOS and Linux, `pypi.org`. Behind a proxy set `HTTPS_PROXY` first. The Windows zip needs no internet for its first start. |
| macOS asks whether Terminal or Python may access a folder | Allow it, or keep TOW and your download folders out of Documents, Desktop and Downloads. |
| An Intel Mac stops at "cryptography" | Install the developer tools (`xcode-select --install`) and Rust (<https://rustup.rs>), then run the installer again. |
| "TOW is already installed" | Use the update file instead, or remove TOW first. |

## Manual install with git

For developers and servers: a clone that `deploy.ps1` / `tow update` move between tags with git. You need
[git](https://git-scm.com/downloads) and [uv](https://docs.astral.sh/uv/) 0.12 or newer; `setup` installs Python
3.14 and the libraries inside the TOW folder.

**Windows** (PowerShell):

```powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
# open a new terminal, then:
git clone https://github.com/d0j/tow "$HOME\TOW\app"
cd "$HOME\TOW\app"
Copy-Item config.example.yaml ..\config.yaml
.\scripts\tow.cmd setup
.\scripts\tow.cmd run
```

Start with Windows (one task, no console window): `.\scripts\tow.cmd autostart on`.

**Linux:**

```sh
sudo apt install git          # or: dnf install git / pacman -S git
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/d0j/tow ~/TOW/app
cd ~/TOW/app
cp config.example.yaml ../config.yaml
./scripts/tow setup
./scripts/tow run
```

Start at login (systemd user unit): `./scripts/tow autostart on`. Start at boot without a login:
`./scripts/tow autostart on --without-login`, then `sudo loginctl enable-linger $USER`.

**macOS:**

```sh
brew install git uv           # or: xcode-select --install, and the uv installer above
git clone https://github.com/d0j/tow ~/TOW/app
cd ~/TOW/app
cp config.example.yaml ../config.yaml
./scripts/tow setup
./scripts/tow run
```

Start at login (LaunchAgent): `./scripts/tow autostart on`.

Update a clone: `.\scripts\deploy.ps1 -Ref v1.22.0` on Windows; on Linux and macOS `./scripts/tow update --ref
v1.22.0` prints the command. Details: [PORTABLE.md](PORTABLE.md).
