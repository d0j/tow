#!/bin/sh
# Smoke test of install/install.sh on Linux or macOS (CI on every commit, the release workflow on a tag):
#
#   sh scripts/install-smoke.sh tow-source.tar.gz [port]
#
# Installs from the local source archive (checked against a SHA256SUMS made for it) into a
# folder with a space, starts TOW with its start file (TOW_NO_BROWSER=1), checks /healthz and
# `tow status`, starts again (it only finds TOW running), stops it with the stop file, removes it
# with --uninstall --yes (data, keys and config stay), installs again around them (same key),
# starts, checks and stops it again, uninstalls, then --purge (nothing stays). uv's and Python's
# places outside the folder are compared before and after. Never port 8787.
set -eu

archive=$1
port=${2:-18877}
[ "$port" != 8787 ] || {
    echo "install-smoke: never on 8787 (a real TOW may run there)" >&2
    exit 2
}
here=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
installer=$here/install/install.sh
base=${RUNNER_TEMP:-${TMPDIR:-/tmp}}
dir="$base/tow smoke $$"
sums="$base/SHA256SUMS.smoke.$$"

say() { printf '%s\n' "install-smoke: $*"; }
fail() {
    printf '%s\n' "install-smoke: FAILED: $*" >&2
    [ ! -f "$dir/data/logs/run.log" ] || tail -n 40 "$dir/data/logs/run.log" >&2
    exit 1
}
sha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d ' ' -f 1; }
healthy() { curl -fsS --noproxy '*' --max-time 3 "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; }
outside() { # where uv or Python could write outside the folder
    for place in "$HOME/.local/share/uv" "$HOME/.cache/uv" "$HOME/.local/bin" "$HOME/Library/Caches/uv" \
        "$HOME/.config/uv" "$HOME/.local/share/applications"; do
        if [ -e "$place" ]; then
            printf '%s %s\n' "$place" "$(find "$place" 2>/dev/null | wc -l | tr -d ' ')"
        else
            printf '%s absent\n' "$place"
        fi
    done
    printf 'home: %s\n' "$(ls -A "$HOME" | tr '\n' '|')"
}
cleanup() {
    [ ! -x "$dir/app/scripts/tow" ] || "$dir/app/scripts/tow" stop >/dev/null 2>&1 || true
    rm -rf "$dir" "$sums"
}
trap cleanup EXIT

if healthy; then fail "something already answers on port $port"; fi
before=$(outside)
printf '%s  tow-source.tar.gz\n' "$(sha "$archive")" >"$sums"

say "install.sh into $dir"
TOW_INSTALL_SOURCE=$archive TOW_INSTALL_SUMS=$sums sh "$installer" --dir "$dir" --port "$port"
case $(uname -s) in
    Darwin) start="$dir/Start TOW.command" stop="$dir/Stop TOW.command" ;;
    *) start="$dir/start-tow" stop="$dir/stop-tow" ;;
esac
[ -x "$start" ] && [ -x "$stop" ] || fail "no start and stop files"
[ -f "$dir/keys/master.key" ] || fail "no keys/master.key"

say "start: $start"
TOW_NO_BROWSER=1 "$start" </dev/null || fail "the start file exited with $?"
healthy || fail "/healthz does not answer on $port"
curl -fsS --noproxy '*' "http://127.0.0.1:$port/healthz"
echo
"$dir/app/scripts/tow" status
"$dir/app/scripts/tow" status --json | grep -q '"running": true' || fail "tow status: not running"

say "second start: it finds TOW running"
TOW_NO_BROWSER=1 "$start" </dev/null || fail "the second start exited with $?"

say "stop: $stop"
"$stop" </dev/null || fail "the stop file exited with $?"
tries=0
while healthy; do
    tries=$((tries + 1))
    [ "$tries" -lt 60 ] || fail "TOW still answers after the stop file"
    sleep 1
done

say 'move the stopped install, keeping its old absolute-path environment'
key_before=$(sha "$dir/keys/master.key")
config_before=$(sha "$dir/config.yaml")
moved="$base/tow relocated $$"
case $dir in "$base"/*) ;; *) fail 'move source is outside the owned test base' ;; esac
case $moved in "$base"/*) ;; *) fail 'move destination is outside the owned test base' ;; esac
[ ! -e "$moved" ] || fail 'move destination exists'
mv -- "$dir" "$moved"
dir=$moved
case $(uname -s) in
    Darwin) start="$dir/Start TOW.command" stop="$dir/Stop TOW.command" ;;
    *) start="$dir/start-tow" stop="$dir/stop-tow" ;;
esac
UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never TOW_NO_BROWSER=1 "$start" </dev/null || fail 'relocated start failed offline'
healthy || fail 'relocated service is unhealthy'
[ "$(sha "$dir/keys/master.key")" = "$key_before" ] || fail 'move replaced the master key'
[ "$(sha "$dir/config.yaml")" = "$config_before" ] || fail 'move changed config'
TOW_MOVE_ROOT=$dir "$dir/app/.venv/bin/python" -c 'import os, sys, tow; from pathlib import Path; root=Path(os.environ["TOW_MOVE_ROOT"]).resolve(); assert Path(sys.base_prefix).resolve().is_relative_to(root); assert Path(tow.__file__).resolve().is_relative_to(root)' || fail 'moved Python or module belongs to the old folder'
"$stop" </dev/null || fail 'moved service did not stop'
tries=0
while healthy; do
    tries=$((tries + 1))
    [ "$tries" -lt 60 ] || fail 'moved service still answers after stop'
    sleep 1
done
say 'relocated install rebuilt offline, preserved its key and config, started and stopped'

say "uninstall --yes (keeps data, keys, config.yaml, backup)"
sh "$installer" --uninstall --yes --dir "$dir" </dev/null
left=$(ls -A "$dir" | tr '\n' ' ')
[ "$left" = ".tow-install backup config.yaml data keys " ] ||
    [ "$left" = ".tow-install config.yaml data keys " ] || fail "left after uninstall: $left"
key_before=$(sha "$dir/keys/master.key")

say "install again around the kept data (same key, same settings)"
TOW_INSTALL_SOURCE="$archive" TOW_INSTALL_SUMS="$sums" sh "$installer" --dir "$dir" --port "$port" </dev/null
[ "$(sha "$dir/keys/master.key")" = "$key_before" ] || fail "the reinstall replaced keys/master.key"
TOW_NO_BROWSER=1 "$start" </dev/null || fail "the start file after the reinstall exited with $?"
healthy || fail "/healthz does not answer after the reinstall"
"$dir/app/scripts/tow" status --json | grep -q '"running": true' || fail "tow status after the reinstall: not running"
"$stop" </dev/null || fail "the stop file after the reinstall exited with $?"
tries=0
while healthy; do
    tries=$((tries + 1))
    [ "$tries" -lt 60 ] || fail "TOW still answers after the stop file (reinstall)"
    sleep 1
done
sh "$installer" --uninstall --yes --dir "$dir" </dev/null

say "--purge of what an uninstall kept (no app left)"
[ ! -e "$dir/app" ] || fail "app/ is still there"
sh "$installer" --uninstall --yes --purge --dir "$dir" </dev/null
[ ! -e "$dir" ] || fail "--purge left $dir"

after=$(outside)
if [ "$before" != "$after" ]; then
    printf 'before:\n%s\nafter:\n%s\n' "$before" "$after" >&2
    fail "the install wrote outside its folder"
fi
say "passed"
