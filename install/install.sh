#!/bin/sh
# Install TOW on Linux or macOS, into one folder (default ~/TOW). No sudo; nothing is written
# outside that folder (only --desktop adds a menu entry, and --autostart a systemd unit or a
# LaunchAgent, when asked).
#
#   curl -LsSf https://github.com/d0j/tow/releases/latest/download/install.sh | sh
#   curl -LsSf https://github.com/d0j/tow/releases/latest/download/install.sh | sh -s -- --autostart
#
# Options (or the variables in brackets):
#   --dir DIR        where to install [TOW_INSTALL_DIR]; default ~/TOW
#   --version TAG    a release, e.g. v1.22.0 [TOW_VERSION]; default the latest
#   --port N         the web page's port [TOW_INSTALL_PORT]; default 8787
#   --autostart      start TOW with the computer (tow autostart on)
#   --desktop        Linux: add TOW to the applications menu (~/.local/share/applications)
#   --uninstall      remove TOW from DIR (asks first; keeps data/, keys/, config.yaml, backup/)
#   --yes            do not ask (with --uninstall)
#   --purge          with --uninstall: remove the data, keys and backups too
#   --adopt-data     reuse or remove data left by an older installer without an install marker
# For tests: TOW_INSTALL_SOURCE=<source .tar.gz> and TOW_INSTALL_SUMS=<SHA256SUMS> install from
# local files instead of the release.
#
# What it does: downloads uv (pinned, checked against its release's checksum) into
# DIR/runtime/bin, the release's source archive (checked against the release's SHA256SUMS) into
# DIR/app, copies config.example.yaml to DIR/config.yaml, runs `app/scripts/tow setup` (Python
# and the packages inside DIR/runtime, the master key in DIR/keys) and creates the start files.
set -eu

REPO=d0j/tow
UV_VERSION=0.12.23
SOURCE_ASSET=tow-source.tar.gz

say() { printf '%s\n' "TOW: $*"; }
die() {
    printf '%s\n' "TOW: $*" >&2
    exit 1
}

dir=${TOW_INSTALL_DIR:-}
version=${TOW_VERSION:-}
port=${TOW_INSTALL_PORT:-}
autostart=no
desktop=no
uninstall=no
yes=no
purge=no
adopt_data=no
while [ $# -gt 0 ]; do
    case $1 in
        --dir) [ $# -ge 2 ] || die "--dir needs a folder"; dir=$2; shift ;;
        --dir=*) dir=${1#--dir=} ;;
        --version) [ $# -ge 2 ] || die "--version needs a tag, e.g. v1.22.0"; version=$2; shift ;;
        --version=*) version=${1#--version=} ;;
        --port) [ $# -ge 2 ] || die "--port needs a number"; port=$2; shift ;;
        --port=*) port=${1#--port=} ;;
        --autostart) autostart=yes ;;
        --desktop) desktop=yes ;;
        --uninstall) uninstall=yes ;;
        --yes | -y) yes=yes ;;
        --purge) purge=yes ;;
        --adopt-data) adopt_data=yes ;;
        -h | --help)
            if [ -f "$0" ]; then
                sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
            else
                printf '%s\n' "Options: --dir DIR, --version TAG, --port N, --autostart, --desktop, --uninstall [--yes] [--purge], --adopt-data" \
                    "See https://github.com/$REPO/blob/main/docs/install.md"
            fi
            exit 0
            ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
done
case $port in
    '' | *[!0-9]*) [ -z "$port" ] || die "--port needs a number, not $port" ;;
esac

[ -n "$dir" ] || dir=$HOME/TOW
case $dir in
    /*) ;;
    *) dir=$(pwd)/$dir ;;
esac
marker=$dir/.tow-install
[ ! -L "$dir" ] && [ ! -L "$dir/app" ] && [ ! -L "$dir/app/scripts/tow" ] ||
    die "$dir contains a symbolic link in its install path: give the real installation folder"
marked=no
if [ -f "$marker" ] && [ ! -L "$marker" ] && [ "$(cat "$marker")" = 'TOW portable install v1' ]; then
    marked=yes
fi
legacy=no
if [ -x "$dir/app/scripts/tow" ] && [ ! -d "$dir/app/.git" ] && [ -f "$dir/app/pyproject.toml" ] &&
    grep -Fqx 'name = "tow"' "$dir/app/pyproject.toml"; then
    legacy=yes
fi

# --- helpers -----------------------------------------------------------------------------------

fetch() { # URL FILE
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --https-only -O "$2" "$1"
    else
        die "curl or wget is needed"
    fi
}

sha256() { # FILE
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d ' ' -f 1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d ' ' -f 1
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 "$1" | sed 's/^.*= *//'
    else
        die "sha256sum, shasum or openssl is needed to check the downloads"
    fi
}

listed() { # SUMS NAME: the SHA-256 the sums file gives for NAME
    awk -v name="$2" '{ file = $2; sub(/^\*/, "", file); if (file == name) { print tolower($1); exit } }' "$1"
}

latest_tag() {
    url=https://github.com/$REPO/releases/latest
    if command -v curl >/dev/null 2>&1; then
        location=$(curl -fsSLI --proto '=https' -o /dev/null -w '%{url_effective}' "$url")
    else
        location=$(wget -q -S --max-redirect=0 -O /dev/null "$url" 2>&1 | sed -n 's/^ *[Ll]ocation: *//p' | tr -d '\r' | tail -n 1)
    fi
    case $location in
        */releases/tag/*) printf '%s\n' "${location##*/releases/tag/}" ;;
        *) die "could not find the latest release at $url" ;;
    esac
}

ask() { # QUESTION DEFAULT(y|n): yes or no from the terminal (stdin may be the script itself)
    [ -r /dev/tty ] || die "no terminal to ask in: add --yes"
    printf '%s ' "$1" >/dev/tty
    read -r answer </dev/tty || answer=
    case $answer in
        [Yy]*) printf 'y\n' ;;
        [Nn]*) printf 'n\n' ;;
        *) printf '%s\n' "$2" ;;
    esac
}

desktop_file() {
    printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}/applications/tow.desktop"
}

# --- uninstall ---------------------------------------------------------------------------------

if [ "$uninstall" = yes ]; then
    if [ "$marked" = no ] && [ "$legacy" = no ] && [ "$adopt_data" = no ]; then
        die "no TOW install in $dir (give its folder with --dir)"
    fi
    if [ "$marked" = no ] && [ "$legacy" = no ] && [ "$adopt_data" = yes ] &&
        { [ ! -f "$dir/config.yaml" ] || [ ! -f "$dir/keys/master.key" ] || [ ! -d "$dir/data" ]; }; then
        die "cannot adopt $dir: the old settings, master key and data folder are not all present"
    fi
    say "this removes TOW from $dir"
    [ ! -f "$dir/keys/master.key" ] || say "your master key is $dir/keys/master.key: keep a copy if you may restore a backup or a .towx file later"
    if [ "$yes" != yes ] && [ "$(ask "Remove TOW from $dir? [y/N]" n)" != y ]; then
        die "nothing was removed"
    fi
    keep=y
    if [ "$purge" = yes ]; then
        keep=n
    elif [ "$yes" != yes ]; then
        keep=$(ask "Keep your data, keys, settings and backups (data/, keys/, config.yaml, backup/) in $dir? [Y/n]" y)
    fi
    if [ "$keep" = y ] && [ "$marked" = no ]; then
        printf '%s\n' 'TOW portable install v1' >"$marker"
        chmod 600 "$marker"
    fi
    if [ "$legacy" = yes ]; then
        "$dir/app/scripts/tow" autostart off >/dev/null 2>&1 || true
        "$dir/app/scripts/tow" stop || true
    fi
    entry=$(desktop_file)
    if [ -f "$entry" ] && grep -F "$dir/start-tow" "$entry" >/dev/null 2>&1; then
        rm -f "$entry"
        say "removed the menu entry $entry"
    fi
    if [ "$keep" = y ]; then
        for item in "$dir"/* "$dir"/.[!.]*; do
            [ -e "$item" ] || continue
            case ${item##*/} in
                data | keys | config.yaml | backup | .tow-install) ;;
                *) rm -rf "$item" ;;
            esac
        done
        say "TOW was removed; data/, keys/, config.yaml and backup/ are still in $dir"
    else
        rm -rf "$dir"
        say "TOW was removed with all its data ($dir)"
    fi
    exit 0
fi

# --- install -----------------------------------------------------------------------------------

system=$(uname -s)
case $system in
    Linux) os=linux ;;
    Darwin) os=macos ;;
    *) die "this installer is for Linux and macOS; on Windows use install.ps1 (see https://github.com/$REPO)" ;;
esac

if [ -e "$dir/app" ]; then
    die "TOW is already installed in $dir. To update it: $dir/app/scripts/tow update --ref latest (it prints the command)"
fi
# What an uninstall that kept the data leaves: TOW is installed again around it, nothing of it changes.
kept=no
if [ -d "$dir" ]; then
    for item in "$dir"/* "$dir"/.[!.]*; do
        [ -e "$item" ] || continue
        case ${item##*/} in
            data | keys | config.yaml | backup | .tow-install) kept=yes ;;
            *) die "$dir is not empty: choose another folder with --dir" ;;
        esac
    done
fi
[ "$kept" = no ] || [ "$marked" = yes ] || [ "$adopt_data" = yes ] ||
    die "$dir has data without a TOW install marker: use --adopt-data only if it is your old TOW folder"
[ "$kept" = no ] || say "found the data of an earlier TOW in $dir: it is kept"
command -v tar >/dev/null 2>&1 || die "tar is needed"

arch=$(uname -m)
case $os-$arch in
    linux-x86_64 | linux-amd64) target=x86_64-unknown-linux ;;
    linux-aarch64 | linux-arm64) target=aarch64-unknown-linux ;;
    linux-armv7l | linux-armv7*) target=armv7-unknown-linux ;;
    macos-arm64) target=aarch64-apple-darwin ;;
    macos-x86_64)
        target=x86_64-apple-darwin
        say "an Intel Mac needs a Rust toolchain and the Xcode command line tools for one package (docs/PORTABLE.md)"
        ;;
    *) die "this computer ($system $arch) is not supported" ;;
esac
if [ "$os" = linux ]; then
    libc=musl
    if ldd --version 2>&1 | grep -qi -e glibc -e 'gnu libc'; then
        libc=gnu
    fi
    case $target in
        armv7-*) target=$target-${libc}eabihf ;;
        *) target=$target-$libc ;;
    esac
fi

created=no
config_created=no
config_saved=no
[ -e "$dir" ] || created=yes
mkdir -p "$dir"
work=$dir/.install
cleanup() { # a failed installation leaves the folder as it found it: absent or empty
    status=$?
    if [ "$status" -ne 0 ] && [ "$config_saved" = yes ]; then
        cp "$work/config.before" "$dir/config.yaml"
    fi
    rm -rf "$work"
    if [ "$status" -ne 0 ]; then
        if [ "$created" = yes ]; then
            rm -rf "$dir"
        elif [ "$kept" = yes ]; then
            # Only what this installer put there: the earlier TOW's data stays as it was.
            rm -rf "$dir/app" "$dir/runtime" "$dir/start-tow" "$dir/stop-tow" "$dir/update-tow" \
                "$dir/Start TOW.command" "$dir/Stop TOW.command" "$dir/Update TOW.command"
            [ "$config_created" = no ] || rm -f "$dir/config.yaml"
        else
            rm -rf "$dir"/* "$dir"/.[!.]*
        fi
        if [ "$kept" = yes ]; then
            printf '%s\n' "TOW: the installation failed; the earlier data in $dir is as it was" >&2
        else
            printf '%s\n' "TOW: the installation failed; nothing is left in $dir" >&2
        fi
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM
mkdir -p "$work"
if [ -f "$dir/config.yaml" ]; then
    cp "$dir/config.yaml" "$work/config.before"
    config_saved=yes
fi

# The release: its SHA256SUMS and its source archive.
if [ -n "${TOW_INSTALL_SOURCE:-}" ]; then
    archive=$TOW_INSTALL_SOURCE
    [ -f "$archive" ] || die "no file $archive"
    if [ -n "${TOW_INSTALL_SUMS:-}" ]; then
        expected=$(listed "$TOW_INSTALL_SUMS" "$SOURCE_ASSET")
        [ -n "$expected" ] || die "$TOW_INSTALL_SUMS lists no $SOURCE_ASSET"
        [ "$(sha256 "$archive")" = "$expected" ] || die "$archive does not match $TOW_INSTALL_SUMS"
    else
        say "a local archive without TOW_INSTALL_SUMS: not checked"
    fi
    tag=local
else
    [ -n "$version" ] || version=$(latest_tag)
    tag=$version
    say "TOW $tag"
    fetch "https://github.com/$REPO/releases/download/$tag/SHA256SUMS" "$work/SHA256SUMS" ||
        die "release $tag has no SHA256SUMS (is $tag a release of TOW?)"
    expected=$(listed "$work/SHA256SUMS" "$SOURCE_ASSET")
    [ -n "$expected" ] || die "the SHA256SUMS of $tag lists no $SOURCE_ASSET"
    archive=$work/$SOURCE_ASSET
    # GitHub's archive of the tag; if GitHub ever builds it differently, the copy in the release.
    if ! { fetch "https://github.com/$REPO/archive/refs/tags/$tag.tar.gz" "$archive" && [ "$(sha256 "$archive")" = "$expected" ]; }; then
        fetch "https://github.com/$REPO/releases/download/$tag/$SOURCE_ASSET" "$archive" ||
            die "could not download the source of $tag"
        [ "$(sha256 "$archive")" = "$expected" ] || die "the source archive of $tag does not match its SHA256SUMS"
    fi
fi
mkdir -p "$work/source"
tar -xzf "$archive" -C "$work/source"
rm -f "$work/source/pax_global_header" # an old tar's view of GitHub's commit note
set -- "$work"/source/*
[ $# -eq 1 ] && [ -d "$1" ] || die "the source archive should hold one folder"
mv "$1" "$dir/app"
[ -x "$dir/app/scripts/tow" ] || die "the source archive holds no scripts/tow"

# uv, pinned, checked against the checksum its release publishes.
uv_name=uv-$target.tar.gz
uv_url=https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$uv_name
say "uv $UV_VERSION ($target)"
fetch "$uv_url" "$work/$uv_name" || die "could not download $uv_url"
fetch "$uv_url.sha256" "$work/$uv_name.sha256" || die "could not download $uv_url.sha256"
[ "$(sha256 "$work/$uv_name")" = "$(cut -d ' ' -f 1 <"$work/$uv_name.sha256" | tr 'A-F' 'a-f')" ] ||
    die "$uv_name does not match its checksum"
mkdir -p "$work/uv" "$dir/runtime/bin"
tar -xzf "$work/$uv_name" -C "$work/uv"
uv_binary=$(find "$work/uv" -type f -name uv | head -n 1)
[ -n "$uv_binary" ] || die "$uv_name holds no uv"
cp "$uv_binary" "$dir/runtime/bin/uv"
chmod 755 "$dir/runtime/bin/uv"

if [ ! -e "$dir/config.yaml" ]; then # the settings of an earlier TOW stay
    cp "$dir/app/config.example.yaml" "$dir/config.yaml"
    config_created=yes
fi
if [ -n "$port" ]; then
    sed "s/^port:.*/port: $port/" "$dir/config.yaml" >"$work/config.yaml"
    cp "$work/config.yaml" "$dir/config.yaml"
fi

# Python, the packages and the master key, all inside the folder.
"$dir/app/scripts/tow" setup

# The start files: double-click on macOS (Terminal runs them), commands on Linux.
if [ "$os" = macos ]; then
    start_file="$dir/Start TOW.command"
    stop_file="$dir/Stop TOW.command"
    update_file="$dir/Update TOW.command"
else
    start_file="$dir/start-tow"
    stop_file="$dir/stop-tow"
    update_file="$dir/update-tow"
fi
printf '%s\n' '#!/bin/sh' '# TOW: start it and open its page in the browser (the first time after a move it prepares itself).' \
    'exec "$(dirname "$0")/app/scripts/tow-start" "$@"' >"$start_file"
printf '%s\n' '#!/bin/sh' '# TOW: stop it (a check that is running finishes first).' \
    'exec "$(dirname "$0")/app/scripts/tow" stop' >"$stop_file"
printf '%s\n' '#!/bin/sh' '# TOW: update it to the latest release (or: update-tow v1.23.0); it goes back by itself on failure.' \
    'root=$(dirname "$0")' \
    'for python in "$root"/runtime/python/cpython-3*/bin/python3; do [ -x "$python" ] && break; done' \
    'update="$root/app/scripts/update.py"' \
    'if [ -f "$root/.update-switch.json" ] || [ ! -f "$update" ]; then update="$root/runtime/update.py"; fi' \
    'exec "$python" "$update" --ref "${1:-latest}"' >"$update_file"
chmod 755 "$start_file" "$stop_file" "$update_file"
printf '%s\n' 'TOW portable install v1' >"$marker"
chmod 600 "$marker"

# Installed: from here on a failure is said, and the install stays.
trap - EXIT
rm -rf "$work"
if [ "$desktop" = yes ]; then
    if [ "$os" != linux ]; then
        say "--desktop is for Linux; on macOS double-click \"Start TOW.command\" (or drag it to the Dock)"
    else
        case $dir in
            *[\"\`\$\\%]*) say "the folder name has a character a menu entry cannot hold: no menu entry" ;;
            *)
                entry=$(desktop_file)
                mkdir -p "$(dirname "$entry")"
                printf '%s\n' '[Desktop Entry]' 'Type=Application' 'Name=TOW' 'Comment=Torrent topic watcher' \
                    "Exec=\"$dir/start-tow\"" "Icon=$dir/app/src/tow/static/favicon.svg" 'Terminal=false' \
                    'Categories=Network;' >"$entry"
                say "added TOW to the applications menu ($entry)"
                ;;
        esac
    fi
fi

if [ "$autostart" = yes ]; then
    "$dir/app/scripts/tow" autostart on || {
        say "autostart is not on (the reason is above)"
        autostart=no
    }
fi

page=http://127.0.0.1:${port:-8787}
printf '\n%s\n' "TOW is installed in $dir"
if [ "$os" = macos ]; then
    printf '%s\n' "  Start:      double-click \"Start TOW.command\" in $dir (or: \"$dir/app/scripts/tow\" run)"
    printf '%s\n' "  Stop:       \"Stop TOW.command\"    Update: \"Update TOW.command\""
else
    printf '%s\n' "  Start:      \"$start_file\" (or, in this terminal: \"$dir/app/scripts/tow\" run)"
    printf '%s\n' "  Stop:       \"$stop_file\"    Update: \"$update_file\""
fi
printf '%s\n' "  The page:   $page"
[ "$autostart" = yes ] || printf '%s\n' "  Autostart:  \"$dir/app/scripts/tow\" autostart on"
printf '%s\n' "  Back up:    $dir/keys/master.key (it opens your saved passwords)"
printf '%s\n' "  Remove:     curl -LsSf https://github.com/$REPO/releases/latest/download/install.sh | sh -s -- --uninstall --dir \"$dir\""
printf '%s\n' "TOW установлен в $dir. Запуск: $(basename "$start_file"), страница: $page. Сохраните копию keys/master.key."
