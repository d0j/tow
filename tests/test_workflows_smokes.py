"""The smokes of a real install (.github/workflows/smokes.yml, TOW 1.33.1) and their scripts.

What these tests keep: the reusable workflow has a callable wrapper that publishes nothing
(installers.yml, dispatchable on a branch) and the release publishes only after it passed on the
release's own artifacts; every job is bounded, on its own port (never 8787) and checks out the
commit it was given; the substitutes run weekly and by hand only and say what they substitute;
the jobs run the scripts tests/test_smoke_scripts.py checks.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _flow(name: str) -> tuple[str, dict]:
    text = (WORKFLOWS / f"{name}.yml").read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


def _runs(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job.get("steps", []))


# --- the reusable workflow and its callers -----------------------------------------------------------


def test_smokes_is_reusable_with_a_dispatchable_wrapper_that_publishes_nothing():
    _text, flow = _flow("smokes")
    assert set(flow[True]) == {"workflow_call"}  # YAML 1.1 reads `on` as True
    inputs = flow[True]["workflow_call"]["inputs"]
    assert inputs["ref"] == {"description": "The commit to test", "required": True, "type": "string"}
    assert inputs["release"]["type"] == inputs["extended"]["type"] == "boolean"
    assert inputs["release"]["default"] is False
    assert inputs["extended"]["default"] is False
    # The wrapper: installers.yml, dispatchable by hand on any branch, read-only, no release step.
    installers_text, installers = _flow("installers")
    assert "workflow_dispatch" in installers[True]
    assert installers["jobs"]["smokes"] == {
        "uses": "./.github/workflows/smokes.yml",
        "with": {"ref": "${{ github.sha }}", "extended": "${{ github.event_name != 'pull_request' }}"},
    }
    assert ".github/workflows/smokes.yml" in installers[True]["pull_request"]["paths"]
    for text in (installers_text, _flow("smokes")[0]):
        assert "gh release upload" not in text
        assert "gh release create" not in text
        assert "contents: write" not in text


def test_the_release_publishes_only_after_the_smokes_of_its_own_artifacts():
    _text, release = _flow("release")
    jobs = release["jobs"]
    assert jobs["smokes"] == {
        "needs": ["source", "windows"],
        "uses": "./.github/workflows/smokes.yml",
        "with": {"ref": "${{ needs.source.outputs.commit }}", "release": True},
    }
    assert "smokes" in jobs["publish"]["needs"]
    # The release's artifacts are the ones tested: `source` and `windows`, by name; the publish job
    # downloads those two only (the smokes' logs are artifacts of the same run).
    _smokes_text, smokes = _flow("smokes")
    assert smokes["env"] == {
        "SOURCE_ARTIFACT": "${{ inputs.release && 'source' || 'smokes-source' }}",
        "BUNDLE_ARTIFACT": "${{ inputs.release && 'windows' || 'smokes-bundle' }}",
    }
    assert {step["with"]["name"] for step in jobs["windows"]["steps"] if "upload-artifact" in step.get("uses", "")} == {
        "windows"
    }
    downloads = [step["with"] for step in jobs["publish"]["steps"] if "download-artifact" in step.get("uses", "")]
    assert downloads == [{"name": "source", "path": "artifacts"}, {"name": "windows", "path": "artifacts"}]
    # The required check keeps its name (branch protection requires "Windows bundle smoke").
    assert _flow("ci")[1]["jobs"]["bundle"]["name"] == "Windows bundle smoke"


def test_every_smokes_job_is_bounded_on_its_own_port_and_on_the_given_commit():
    _text, flow = _flow("smokes")
    jobs = flow["jobs"]
    assert set(jobs) == {
        "build",
        "web-update",
        "crash",
        "arch",
        "alpine",
        "nonadmin",
        "tls",
        "longpaths",
        "macos",
        "netns",
    }
    assert jobs["build"]["if"] == "${{ !inputs.release }}"
    for job_id, job in jobs.items():
        assert job["timeout-minutes"] <= 60, job_id
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"] == {"ref": "${{ inputs.ref }}", "persist-credentials": False}, job_id
            if step.get("uses", "").startswith("astral-sh/setup-uv@"):
                assert step["with"]["save-cache"] is False, job_id
        if job_id == "build":
            continue
        assert job["needs"] == "build", job_id
        condition = "(inputs.release || needs.build.result == 'success')"
        assert job["if"].startswith("${{ !cancelled() "), job_id
        assert condition in job["if"], job_id
        # The substitutes of other systems: weekly and by hand only (the wrapper's `extended`).
        assert ("inputs.extended" in job["if"]) is (job_id not in {"web-update", "crash"}), job_id
    runs = "\n".join(_runs(job) for job in jobs.values())
    found = {int(port) for port in re.findall(r"\b(1[89]\d{3})\b", runs)}
    assert not re.search(r"\b8787\b", runs)
    assert {18958, 18959, 18961, 18962, 18963, 18964, 18965, 18966, 18967, 18968} <= found


def test_the_always_on_smokes_keep_their_logs_and_use_the_scripts():
    _text, flow = _flow("smokes")
    jobs = flow["jobs"]
    web = _runs(jobs["web-update"])
    assert "scripts/web-update-smoke.py --zip artifacts/TOW-windows-x64.zip" in web
    assert "set -o pipefail" in web  # the tee keeps the smoke's exit code
    assert jobs["web-update"]["runs-on"] == "windows-latest"
    crash = jobs["crash"]
    assert crash["strategy"]["matrix"]["os"] == ["windows-latest", "ubuntu-24.04", "macos-latest"]
    assert "scripts/crash-smoke.py" in _runs(crash)
    assert "--strict" in _runs(crash)  # the permissions failure leg must run on a runner
    for job_id in ("web-update", "crash", "alpine", "netns"):
        uploads = [step for step in jobs[job_id]["steps"] if "upload-artifact" in step.get("uses", "")]
        assert uploads, job_id
        assert uploads[-1]["if"] == "always()", job_id
        assert uploads[-1]["with"]["name"].startswith("smokes-logs-"), job_id


def test_the_substitutes_say_what_they_stand_for():
    _text, flow = _flow("smokes")
    jobs = flow["jobs"]
    # TC-07: the runner's own architecture is asserted, not taken from its label.
    rows = {row["os"]: row["machine"] for row in jobs["arch"]["strategy"]["matrix"]["include"]}
    assert rows == {"ubuntu-24.04-arm": "aarch64", "macos-15-intel": "x86_64"}
    arch = _runs(jobs["arch"])
    assert "machine=$(uname -m)" in arch
    assert "sysctl.proc_translated" in arch  # an Intel Mac, not Rosetta on Apple silicon
    assert "scripts/install-smoke.sh" in arch
    # D06: a pinned Alpine image (by digest), the checkout read-only.
    alpine = _runs(jobs["alpine"])
    assert re.search(r"alpine:3\.\d+@sha256:[0-9a-f]{64}", alpine)
    assert '-v "$PWD:/src:ro"' in alpine
    assert "sh /src/scripts/alpine-smoke.sh" in alpine
    # TC-15: a standard account on the disposable runner.
    assert "./scripts/nonadmin-smoke.ps1 -Zip artifacts/TOW-windows-x64.zip" in _runs(jobs["nonadmin"])
    # B14: Windows PowerShell 5.1 downloads a real release over HTTPS into a path with brackets.
    tls = _runs(jobs["tls"])
    assert "powershell.exe -NoProfile -ExecutionPolicy Bypass -File install/install.ps1 -Dir $dir" in tls
    assert r"'[x] TOW\TOW'" in tls
    assert "TOW_INSTALL_SOURCE" not in tls  # the real download, not a local file
    # D04: the registry value set, then read back in each later (new) process.
    steps = jobs["longpaths"]["steps"]
    names = [step.get("name", "") for step in steps]
    off = names.index("Turn long paths off on this disposable runner")
    assert "Set-ItemProperty -Path $key -Name LongPathsEnabled -Value 0" in steps[off]["run"]
    checked = [
        step
        for step in steps[off + 1 :]
        if "bundle-smoke.ps1" in step.get("run", "") or "update-smoke.py" in step.get("run", "")
    ]
    assert len(checked) == 2
    for step in checked:
        assert "LongPathsEnabled is $value in this new process, not 0" in step["run"]
    longpaths = _runs(jobs["longpaths"])
    # Without long paths the limit is the start file's warning length (110), not 172: Python's own
    # modules lie up to 115 characters below the folder, past 260 from a folder of 172.
    assert "-Offline -Move -RootLength 110" in longpaths
    assert "-Offline -RootLength 111" in longpaths
    assert "-RootLength 172" not in longpaths
    assert "-RootLength 173 -ExpectTooLong" in longpaths
    start = (Path(__file__).resolve().parents[1] / "scripts" / "tow-start.cmd").read_text(encoding="utf-8")
    assert 'if not defined TOW_READY if not "%TOW_ROOT:~110,1%"=="" set "TOW_LONG=1"' in start
    smoke = (Path(__file__).resolve().parents[1] / "scripts" / "bundle-smoke.ps1").read_text(encoding="utf-8")
    assert """'TOW_ROOT:~(\\d+),1%"=="" set "TOW_LONG=1"'""" in smoke  # the warning length read from it
    assert "($output -match 'Windows long paths are off') -ne $expectWarning" in smoke
    assert "--root-length 110" in longpaths  # the update at the same long-paths-off limit
    # D05, D07, TC-17 on macOS.
    macos = _runs(jobs["macos"])
    assert "scripts/macos-smoke.sh rosetta" in macos
    assert "scripts/macos-smoke.sh gatekeeper" in macos
    assert "update-smoke.py" in macos
    assert "--nfd" in macos
    nfd = next(step for step in jobs["macos"]["steps"] if "install-smoke.sh" in step.get("run", ""))
    assert nfd["env"] == {"TOW_SMOKE_NFD": "1"}
    # D01: two network namespaces of one runner, labelled as such (not a second device).
    netns = jobs["netns"]
    assert netns["runs-on"] == "ubuntu-24.04"
    assert netns["name"] == "separate network stacks on one runner (Linux)"
    assert "scripts/netns-smoke.py --source" in _runs(netns)
    assert "set -o pipefail" in _runs(netns)


def test_the_update_jobs_have_time_for_the_entry_file_legs():
    for name in ("installers", "release"):
        job = _flow(name)[1]["jobs"]["update"]
        assert 60 <= job["timeout-minutes"] <= 90, name


def test_the_autostart_job_hands_the_source_archive_to_the_update_test():
    job = _flow("autostart")[1]["jobs"]["autostart"]
    step = next(step for step in job["steps"] if "test_real_autostart.py" in step.get("run", ""))
    assert step["env"] == {"TOW_REAL_AUTOSTART_SOURCE": "${{ runner.temp }}/tow-source.tar.gz"}
    assert 'git archive --format=tar.gz --prefix=tow/ -o "$RUNNER_TEMP/tow-source.tar.gz" HEAD' in _runs(job)


def test_real_runs_qbittorrent_on_windows_with_cyrillic_windows_paths_and_the_native_probes():
    _text, flow = _flow("real")
    job = flow["jobs"]["clients-windows"]
    assert job["runs-on"] == "windows-latest"
    assert job["env"]["TOW_REAL_QBITTORRENT"].startswith("127.0.0.1:")
    assert re.fullmatch(r"\d+\.\d+\.\d+", job["env"]["QBITTORRENT_VERSION"])  # pinned, never "latest"
    runs = _runs(job)
    assert "choco install qbittorrent --version $env:QBITTORRENT_VERSION" in runs
    # The program says "v5.2.4": the check drops the "v" and compares the whole version.
    assert "if ((\"$version\" -replace '^v', '') -ne $env:QBITTORRENT_VERSION)" in runs
    # qBittorrent 5.2 answers a login without "Ok.": the session it gives must read the version.
    assert 'curl -fsS --max-time 5 -b "$jar" http://127.0.0.1:18080/api/v2/app/version' in runs
    assert "grep -qx 'Ok.'" not in runs
    assert "Get-FileHash" in runs  # provenance recorded
    assert "Get-AuthenticodeSignature" in runs
    assert "-k qbittorrent --basetemp $base" in runs
    assert "(-join [char[]](0x0422, 0x0435, 0x0441, 0x0442))" in runs  # a Cyrillic folder on the runner's drive
    assert "Stop-Process -Id" in runs  # only the PID it started
    probes = flow["jobs"]["probes"]
    assert probes["env"] == {"TOW_REAL_PROBES": "1"}
    assert probes["strategy"]["matrix"]["os"] == ["ubuntu-24.04", "macos-latest"]
    assert "tests/integration/test_real_probes.py" in _runs(probes)
    # The variables that let real-system tests through the guard stay in real.yml / autostart.yml.
    assert "TOW_REAL_" not in _flow("smokes")[0]


def test_real_records_the_client_spellings_of_reserved_names_and_checks_tows_keys():
    """B36: on Linux (qBittorrent, Transmission) and Windows (qBittorrent) the spellings are
    recorded as an artifact, then TOW's keys are checked against that record; both run (and the
    record is kept) also when an earlier client test failed."""
    _text, flow = _flow("real")
    for job_id, artifact in (("clients", "client-names-linux"), ("clients-windows", "client-names-windows")):
        steps = flow["jobs"][job_id]["steps"]
        record = next(i for i, step in enumerate(steps) if "-k test_record_the_spellings" in step.get("run", ""))
        keys = next(i for i, step in enumerate(steps) if "-k test_tow_keys_agree" in step.get("run", ""))
        upload = next(i for i, step in enumerate(steps) if step.get("with", {}).get("name") == artifact)
        assert record < keys < upload, job_id
        folder = "${{ runner.temp }}/client-names"
        utf8 = {"PYTHONUTF8": "1"} if job_id == "clients-windows" else {}  # names in a Windows log
        assert steps[record]["env"] == {"TOW_REAL_CLIENT_NAMES_OUT": folder, **utf8}, job_id
        assert steps[keys]["env"] == {"TOW_REAL_CLIENT_NAMES": "1", "TOW_REAL_CLIENT_NAMES_DIR": folder, **utf8}, job_id
        assert steps[record]["if"] == steps[keys]["if"] == "${{ !cancelled() }}", job_id
        assert steps[upload]["if"] == "always()", job_id
        assert steps[upload]["with"]["path"] == folder, job_id
        assert "tests/integration/test_real_client_names.py" in steps[record]["run"], job_id
        assert all(steps[i]["timeout-minutes"] <= 15 for i in (record, keys)), job_id
    assert "TOW_REAL_TRANSMISSION" in flow["jobs"]["clients"]["env"]


def test_real_mounts_the_c03_and_c04_file_systems_on_linux_and_macos():
    """tmpfs, vfat and exFAT (the kernel's drivers and FUSE) on Ubuntu, exFAT and FAT disk images
    on macOS; the images are made in the job, the test mounts and unmounts them itself."""
    _text, flow = _flow("real")
    job = flow["jobs"]["filesystems"]
    assert job["strategy"]["matrix"]["os"] == ["ubuntu-24.04", "macos-latest"]
    assert job["env"] == {"TOW_REAL_FILESYSTEMS": "1"}
    steps = {step.get("if"): step for step in job["steps"] if "TOW_REAL_FS_IMAGES" in step.get("run", "")}
    linux, macos = steps["runner.os == 'Linux'"]["run"], steps["runner.os == 'macOS'"]["run"]
    for tool in ("mkfs.vfat", "mkfs.exfat", "exfat-fuse", "modprobe exfat", "truncate -s 64M"):
        assert tool in linux, tool
    assert 'echo "TOW_REAL_FS=tmpfs vfat exfat exfat-fuse"' in linux
    assert "hdiutil create -size 64m -fs ExFAT" in macos
    assert 'hdiutil create -size 64m -fs "MS-DOS FAT32"' in macos
    assert 'echo "TOW_REAL_FS=exfat msdos"' in macos
    test = next(step for step in job["steps"] if "pytest" in step.get("run", ""))
    assert "tests/integration/test_real_filesystems.py" in test["run"]
    assert test["timeout-minutes"] <= 10
