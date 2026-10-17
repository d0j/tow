#!/bin/sh
# BusyBox and musl smoke (D06, TC-07), inside an Alpine container (the workflow runs it with docker):
#
#   docker run --rm -v "$PWD:/src:ro" -v "$archive_dir:/in:ro" alpine@sha256:... sh /src/scripts/alpine-smoke.sh [port]
#
# /src is this checkout (read-only), /in holds tow-source.tar.gz of it. On a real BusyBox system:
# 1. install.sh is refused before it writes anything: BusyBox's wget cannot keep the downloads to
#    HTTPS, and there is no curl ("install curl");
# 2. the updater's process probe (scripts/update.py System.command_line): BusyBox's ps has no -ww,
#    so the /proc fallback must run - and say the command line of a process this script started;
# 3. with curl added, install.sh installs TOW for musl (uv's x86_64-unknown-linux-musl, uv's musl
#    Python, the lock's musllinux wheels), TOW starts, answers, says it runs, and stops - the row
#    "linux-x86_64-musl" of the dependency contract (docs/PORTABLE.md 1b). Never port 8787.
set -eu

port=${1:-18961}
[ "$port" != 8787 ] || {
    echo "alpine-smoke: never on 8787" >&2
    exit 2
}
say() { printf '%s\n' "alpine-smoke: $*"; }
fail() {
    printf '%s\n' "alpine-smoke: FAILED: $*" >&2
    [ ! -f "${dir:-/nonexistent}/data/logs/run.log" ] || tail -n 40 "$dir/data/logs/run.log" >&2
    exit 1
}
archive=/in/tow-source.tar.gz
[ -f "$archive" ] || fail "no $archive"
[ -f /etc/alpine-release ] || fail "not an Alpine system"
say "Alpine $(cat /etc/alpine-release), $(uname -m)"

say '1. install.sh on BusyBox without curl'
! command -v curl >/dev/null 2>&1 || fail 'curl is installed: the refusal is not tested'
wget --help 2>&1 | grep -q BusyBox || fail 'wget is not BusyBox wget'
refused=/tmp/tow-refused
if TOW_INSTALL_SOURCE=$archive sh /src/install/install.sh --dir "$refused" --port "$port" >/tmp/refusal.log 2>&1; then
    fail 'install.sh ran with BusyBox wget alone'
fi
cat /tmp/refusal.log
grep -q 'install curl' /tmp/refusal.log || fail 'install.sh did not ask for curl'
[ ! -e "$refused" ] || fail "the refused install left $refused"

say '2. the updater reads a command line through /proc when ps has no -ww'
apk add --no-cache python3 >/dev/null
if ps -ww -o command= -p 1 >/dev/null 2>&1; then
    fail 'this ps takes -ww: the fallback is not tested'
fi
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY' || fail 'the /proc fallback of update.py'
import importlib.util, subprocess, sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("tow_update", "/src/scripts/update.py")
update = importlib.util.module_from_spec(spec)
sys.modules["tow_update"] = update
spec.loader.exec_module(update)
calls = []
original = update.System._proc_command_line


def spy(pid):
    calls.append(pid)
    return original(pid)


update.System._proc_command_line = staticmethod(spy)
child = subprocess.Popen(["sleep", "300"])
try:
    found = update.System(Path("/tmp/no-app")).command_line(child.pid)
finally:
    child.kill()
    child.wait()
print(f"command line of {child.pid}: {found!r}; /proc read for {calls}")
assert calls == [child.pid], "the /proc fallback did not run"
assert found == "sleep 300", found
PY

say '3. musl: install.sh with curl, start, status, stop'
apk add --no-cache curl >/dev/null
dir="/tmp/tow musl Тест"
sums=/tmp/SHA256SUMS
printf '%s  tow-source.tar.gz\n' "$(sha256sum "$archive" | cut -d ' ' -f 1)" >"$sums"
status=0
TOW_INSTALL_SOURCE=$archive TOW_INSTALL_SUMS=$sums sh /src/install/install.sh --dir "$dir" --port "$port" \
    </dev/null >/tmp/install.log 2>&1 || status=$?
cat /tmp/install.log
[ "$status" = 0 ] || fail "install.sh exited with $status"
grep -q 'unknown-linux-musl)' /tmp/install.log || fail 'install.sh did not take the musl uv'
TOW_NO_BROWSER=1 "$dir/app/scripts/tow" start || fail 'tow start'
wget -q -O - "http://127.0.0.1:$port/healthz" | grep -q '"ok": *true' || fail '/healthz'
"$dir/app/scripts/tow" status --json | grep -q '"running": true' || fail 'tow status: not running'
"$dir/app/scripts/tow" stop || fail 'tow stop'
tries=0
while wget -q -T 3 -O /dev/null "http://127.0.0.1:$port/healthz" 2>/dev/null; do
    tries=$((tries + 1))
    [ "$tries" -lt 60 ] || fail 'TOW still answers 60 s after tow stop'
    sleep 1
done
say passed
