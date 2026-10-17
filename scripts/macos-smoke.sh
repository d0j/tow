#!/bin/sh
# macOS substitutes on an Apple silicon runner (smokes.yml, weekly and by hand):
#
#   sh scripts/macos-smoke.sh rosetta    tow-source.tar.gz [port]   (D05)
#   sh scripts/macos-smoke.sh gatekeeper tow-source.tar.gz [port]   (D07)
#
# rosetta: the runner is verified to be Apple silicon with Rosetta (installed when missing), a
# shell under `arch -x86_64` is verified to be translated, and install.sh run in it must choose
# uv's aarch64-apple-darwin (said in its log) and install arm64 programs (`lipo -archs`); then
# TOW starts, answers and stops.
#
# gatekeeper: after a normal install (install.sh downloads with curl, which sets no quarantine),
# the program TOW actually launches - the executable of its running `tow run` process, links
# resolved - carries no quarantine. Then that very file is quarantined as a browser download
# would be (com.apple.quarantine, read back on the resolved file), `spctl --assess` is recorded,
# and TOW is started again: it must not hang, nor die by a signal; whether Gatekeeper lets an
# unsigned command-line program run is recorded with its expectation (the job summary), not
# assumed. Removing the attribute must let TOW start again. Never port 8787; the install is
# removed at the end; the record goes to $RUNNER_TEMP/macos-smoke.
set -eu

mode=$1
archive=$2
port=${3:-18963}
[ "$port" != 8787 ] || {
    echo "macos-smoke: never on 8787" >&2
    exit 2
}
[ "$(uname -s)" = Darwin ] || {
    echo "macos-smoke: macOS only" >&2
    exit 2
}
here=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
base=${RUNNER_TEMP:-${TMPDIR:-/tmp}}
record="$base/macos-smoke"
mkdir -p "$record"
dir="$base/tow $mode Тест $$"
sums="$base/SHA256SUMS.$mode.$$"
log="$record/$mode.log"

say() { printf '%s\n' "macos-smoke: $*" | tee -a "$log"; }
fail() {
    say "FAILED: $*"
    [ ! -f "$dir/data/logs/run.log" ] || tail -n 40 "$dir/data/logs/run.log" >&2
    exit 1
}
note() { [ -z "${GITHUB_STEP_SUMMARY:-}" ] || printf '%s\n' "$*" >>"$GITHUB_STEP_SUMMARY"; }
healthy() { curl -fsS --noproxy '*' --max-time 3 "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; }
cleanup() {
    [ ! -x "$dir/app/scripts/tow" ] || "$dir/app/scripts/tow" stop >/dev/null 2>&1 || true
    rm -rf "$dir" "$sums"
}
trap cleanup EXIT

printf '%s  tow-source.tar.gz\n' "$(shasum -a 256 "$archive" | cut -d ' ' -f 1)" >"$sums"
for name in $(env | sed -n 's/^\(UV_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$name"; done
export TOW_NO_BROWSER=1

install() { # [arch -x86_64]: install.sh from the local archive, its output in the log
    status=0
    TOW_INSTALL_SOURCE=$archive TOW_INSTALL_SUMS=$sums "$@" /bin/sh "$here/install/install.sh" --dir "$dir" --port "$port" \
        </dev/null >"$record/$mode-install.log" 2>&1 || status=$?
    cat "$record/$mode-install.log" >>"$log"
    [ "$status" = 0 ] || fail "install.sh exited with $status (see $mode-install.log)"
}

start_stop() {
    "$dir/app/scripts/tow" start >>"$log" 2>&1 || fail 'tow start'
    healthy || fail 'TOW does not answer'
    "$dir/app/scripts/tow" stop >>"$log" 2>&1 || fail 'tow stop'
}

case $mode in
    rosetta)
        [ "$(uname -m)" = arm64 ] && [ "$(sysctl -n hw.optional.arm64)" = 1 ] || fail 'not an Apple silicon runner'
        if ! arch -x86_64 /usr/bin/true 2>/dev/null; then
            say 'installing Rosetta'
            softwareupdate --install-rosetta --agree-to-license >>"$log" 2>&1 || fail 'Rosetta cannot be installed'
        fi
        [ "$(arch -x86_64 /bin/sh -c 'uname -m')" = x86_64 ] || fail 'a shell under arch -x86_64 is not x86_64'
        [ "$(arch -x86_64 /bin/sh -c 'sysctl -n sysctl.proc_translated')" = 1 ] || fail 'the shell is not translated'
        say 'a translated shell runs install.sh'
        install arch -x86_64
        grep -q 'runs under Rosetta' "$record/$mode-install.log" || fail 'install.sh did not notice Rosetta'
        grep -q '(aarch64-apple-darwin)' "$record/$mode-install.log" || fail 'install.sh did not choose aarch64-apple-darwin'
        uv_archs=$(lipo -archs "$dir/runtime/bin/uv") || fail "lipo cannot read $dir/runtime/bin/uv"
        # The interpreter itself: bin/ also holds python3.X-config, a shell script lipo cannot read.
        python=$(find "$dir/runtime/python" -path '*/bin/python3*' ! -name '*-config' -type f | sort | head -n 1)
        [ -n "$python" ] || fail "no Python interpreter under $dir/runtime/python"
        python_archs=$(lipo -archs "$python") || fail "lipo cannot read $python"
        say "uv: $uv_archs; Python ($python): $python_archs"
        [ "$uv_archs" = arm64 ] || fail "uv is $uv_archs, not arm64"
        case $python_archs in *arm64*) ;; *) fail "Python is $python_archs, not arm64" ;; esac
        start_stop
        note "Rosetta: install.sh in a translated shell chose aarch64-apple-darwin; uv $uv_archs, Python $python_archs; TOW started and stopped."
        ;;
    gatekeeper)
        install
        "$dir/app/scripts/tow" start >>"$log" 2>&1 || fail 'tow start'
        healthy || fail 'TOW does not answer'
        venv_python="$dir/app/.venv/bin/python"
        pid=$("$venv_python" -c 'import json, sys; print(json.load(open(sys.argv[1]))["pid"])' "$dir/data/run/run.pid")
        # The executable the kernel runs for that process (lsof's first "txt" entry), links resolved.
        launched=$(lsof -a -p "$pid" -d txt -Fn | sed -n 's/^n//p' | head -n 1)
        [ -n "$launched" ] || fail "no executable found for tow run ($pid)"
        program=$("$venv_python" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$launched")
        case $program in "$dir"/*) ;; *) fail "tow run ($pid) runs $program, not a program of the install" ;; esac
        say "tow run (pid $pid) runs $program"
        "$dir/app/scripts/tow" stop >>"$log" 2>&1 || fail 'tow stop'
        if xattr -p com.apple.quarantine "$program" >/dev/null 2>&1; then
            fail "the installed $program carries a quarantine after a curl install"
        fi
        say 'after the install: no quarantine on the program TOW launches'
        xattr -w com.apple.quarantine "0081;$(printf '%x' "$(date +%s)");Safari;$(uuidgen)" "$program"
        xattr -p com.apple.quarantine "$program" >>"$log" 2>&1 || fail 'the quarantine was not set on the launched program'
        spctl --status >"$record/spctl-status.txt" 2>&1 || true
        assess=0
        spctl --assess --type execute -vv "$program" >"$record/spctl-assess.txt" 2>&1 || assess=$?
        say "spctl: $(cat "$record/spctl-status.txt"); assess exit $assess: $(tr '\n' ' ' <"$record/spctl-assess.txt")"
        started=0
        "$dir/app/scripts/tow" start --wait 60 >>"$log" 2>&1 || started=$?
        case $started in
            0) healthy || fail 'tow start said ready, but TOW does not answer'
               outcome='started: Gatekeeper let the quarantined command-line program run' ;;
            3) outcome='refused: tow start reported that TOW could not run (exit 3)' ;;
            *) fail "tow start of the quarantined program ended with $started (a signal or a hang, not a refusal)" ;;
        esac
        say "quarantined: $outcome"
        "$dir/app/scripts/tow" stop >>"$log" 2>&1 || true
        xattr -d com.apple.quarantine "$program"
        start_stop
        say 'without the quarantine TOW starts again'
        note "Gatekeeper (expected: curl sets no quarantine; a quarantined unsigned command-line program is recorded, either outcome is a clear exit): spctl $(cat "$record/spctl-status.txt"), assess exit $assess; $outcome."
        ;;
    *) fail "unknown mode $mode (rosetta, gatekeeper)" ;;
esac
say passed
