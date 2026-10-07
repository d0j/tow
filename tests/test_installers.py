"""install/install.sh (Linux, macOS) and install/install.ps1 (Windows): portable syntax, the
checks they make, and the uninstall of install.sh on a stub install. The full installs run in
the release workflow (.github/workflows/release.yml)."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SH = ROOT / "install" / "install.sh"
PS1 = ROOT / "install" / "install.ps1"


def _code_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


# --- install.sh --------------------------------------------------------------------------------


def test_install_sh_is_plain_posix_sh():
    text = SH.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    assert "\r" not in text
    bashisms = {
        r"\[\[": "[[ ]]",
        r"^\s*function\s": "function",
        r"^\s*local\s": "local",
        r"\$'": "$'...'",
        r"\becho\s+-[en]": "echo -e/-n",
        r"^\s*source\s": "source",
        r"<<<": "here-string",
        r"\$\{[A-Za-z_]+//": "${var//}",
        r"\[\s[^]]*\s==\s": "== in [ ]",
        r"&>": "&>",
        r"\$RANDOM|\bdeclare\b|\bpushd\b|\bshopt\b": "bash builtins",
        r"^\s*[A-Za-z_]+=\(": "arrays",
    }
    found = [
        f"{name}: {line}"
        for line in _code_lines(text)
        for pattern, name in bashisms.items()
        if re.search(pattern, line)
    ]
    assert found == []


@pytest.mark.allow_system
def test_install_sh_parses_in_a_posix_shell():
    shell = shutil.which("dash") or shutil.which("sh")
    if shell is None:
        pytest.skip("no sh on this machine")
    done = subprocess.run([shell, "-n", str(SH)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_install_sh_checks_every_download_and_never_overwrites():
    text = SH.read_text(encoding="utf-8")
    assert "SOURCE_ASSET=tow-source.tar.gz" in text
    assert "releases/download/$tag/SHA256SUMS" in text
    assert 'fetch "$uv_url.sha256"' in text  # uv against its own release's checksum
    assert text.count('= "$expected"') >= 2  # GitHub's archive, else the release's copy
    assert "TOW is already installed in $dir" in text
    assert '[ -n "$dir" ] || dir=$HOME/TOW' in text
    assert "sudo" not in "".join(_code_lines(text))
    # A failed installation leaves the folder as it found it.
    assert "trap cleanup EXIT" in text
    assert text.index("trap - EXIT") > text.index('"$dir/app/scripts/tow" setup')


def test_install_sh_runs_nothing_until_it_is_read_to_the_end():
    # curl | sh runs what has arrived: a cut download must not run half an installation.
    text = SH.read_text(encoding="utf-8")
    assert text.endswith('\nmain "$@"\n')
    body = text[: text.index("\nmain() {\n")]
    commands = [line for line in _code_lines(body) if not line.startswith((" ", "}"))]
    # Before main: only settings and the two message helpers.
    assert all(re.match(r"(set -eu|[A-Z_]+=\S+|say\(\) |die\(\) \{)", line) for line in commands), commands


def test_install_sh_pins_the_uv_of_the_windows_bundle():
    spec = importlib.util.spec_from_file_location("tow_build_bundle_pins", ROOT / "scripts" / "build-bundle.py")
    assert spec is not None
    assert spec.loader is not None
    bundle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bundle)
    assert f"UV_VERSION={bundle.UV_VERSION}\n" in SH.read_text(encoding="utf-8")


def test_install_sh_makes_start_files_that_call_the_apps_scripts():
    text = SH.read_text(encoding="utf-8")
    assert 'start_file="$dir/Start TOW.command"' in text  # macOS: Finder runs it in Terminal
    assert 'start_file="$dir/start-tow"' in text  # Linux
    assert '\'exec "$(dirname "$0")/app/scripts/tow-start" "$@"\'' in text
    assert '\'exec "$(dirname "$0")/app/scripts/tow" stop\'' in text
    assert 'chmod 755 "$start_file" "$stop_file" "$update_file"' in text


def _posix(path: Path) -> str:
    """The path as the shell sees it (Git for Windows' sh wants /c/... for C:\\...)."""
    text = path.as_posix()
    match = re.match(r"^([A-Za-z]):/(.*)$", text)
    return f"/{match.group(1).lower()}/{match.group(2)}" if match else text


@pytest.fixture
def stub_install(tmp_path) -> Path:
    """An install whose tow launcher only writes down what it was asked."""
    root = tmp_path / "my TOW"
    scripts = root / "app" / "scripts"
    scripts.mkdir(parents=True)
    tow = scripts / "tow"
    tow.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >>"$(dirname "$0")/../../../calls.txt"\n', encoding="utf-8")
    tow.chmod(0o755)
    (root / "app" / "pyproject.toml").write_text('[project]\nname = "tow"\n', encoding="utf-8")
    for folder in ("data", "keys", "backup", "runtime/bin"):
        (root / folder).mkdir(parents=True)
    (root / "keys" / "master.key").write_text("not a real key", encoding="utf-8")
    (root / "data" / "state.json").write_text("{}", encoding="utf-8")
    for name in ("config.yaml", "start-tow", "stop-tow", ".update.lock"):
        (root / name).write_text("x", encoding="utf-8")
    return root


def _uninstall(stub_install: Path, home: Path, *options: str) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("dash") or shutil.which("sh")
    if shell is None:
        pytest.skip("no sh on this machine")
    env = {**os.environ, "HOME": _posix(home)}
    env.pop("XDG_DATA_HOME", None)
    return subprocess.run(
        [shell, _posix(SH), "--uninstall", "--dir", _posix(stub_install), *options],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        check=False,
    )


@pytest.mark.allow_system
def test_uninstall_keeps_the_data_and_removes_its_menu_entry(stub_install, tmp_path):
    home = tmp_path / "home"
    menu = home / ".local" / "share" / "applications"
    menu.mkdir(parents=True)
    (menu / "tow.desktop").write_text(f'Exec="{_posix(stub_install)}/start-tow"\n', encoding="utf-8")

    done = _uninstall(stub_install, home, "--yes")

    assert done.returncode == 0, done.stdout + done.stderr
    assert sorted(os.listdir(stub_install)) == [".tow-install", "backup", "config.yaml", "data", "keys"]
    assert (stub_install / "keys" / "master.key").is_file()
    assert (tmp_path / "calls.txt").read_text(encoding="utf-8").split("\n")[:2] == ["autostart off", "stop"]
    assert not (menu / "tow.desktop").exists()
    assert "master.key" in done.stdout


@pytest.mark.allow_system
def test_uninstall_with_purge_removes_everything_and_never_asks_without_a_terminal(stub_install, tmp_path):
    home = tmp_path / "home"
    menu = home / ".local" / "share" / "applications"
    menu.mkdir(parents=True)
    (menu / "tow.desktop").write_text('Exec="/elsewhere/TOW/start-tow"\n', encoding="utf-8")
    done = _uninstall(stub_install, home, "--yes", "--purge")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not stub_install.exists()
    assert (menu / "tow.desktop").exists()  # another install's entry stays


@pytest.mark.allow_system
def test_uninstall_refuses_a_folder_that_is_not_tow(tmp_path):
    (tmp_path / "other").mkdir()
    done = _uninstall(tmp_path / "other", tmp_path / "home", "--yes")
    assert done.returncode == 1
    assert "no TOW install" in done.stderr
    assert (tmp_path / "other").exists()


@pytest.mark.allow_system
@pytest.mark.parametrize("purge", [(), ("--purge",)])
def test_uninstall_never_touches_a_foreign_folder_with_generic_tow_names(tmp_path, purge):
    foreign = tmp_path / "foreign"
    (foreign / "data").mkdir(parents=True)
    (foreign / "keys").mkdir()
    (foreign / "config.yaml").write_text("other app", encoding="utf-8")
    sentinel = foreign / "important.txt"
    sentinel.write_text("keep", encoding="utf-8")

    done = _uninstall(foreign, tmp_path / "home", "--yes", *purge)

    assert done.returncode == 1
    assert "no TOW install" in done.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.allow_system
def test_uninstall_can_purge_data_kept_by_an_older_installer_only_when_adopted(tmp_path):
    old = tmp_path / "old data"
    (old / "data").mkdir(parents=True)
    (old / "keys").mkdir()
    (old / "config.yaml").write_text("settings", encoding="utf-8")
    (old / "keys" / "master.key").write_text("key", encoding="utf-8")
    assert _uninstall(old, tmp_path / "home", "--yes", "--purge").returncode == 1
    done = _uninstall(old, tmp_path / "home", "--yes", "--purge", "--adopt-data")
    assert done.returncode == 0, done.stderr
    assert not old.exists()


# --- install.ps1 -------------------------------------------------------------------------------


def test_install_ps1_runs_from_irm_iex_on_windows_powershell():
    text = PS1.read_text(encoding="utf-8")
    assert text.isascii()  # irm decodes a release asset in any code page
    code = "\n".join(_code_lines(text))
    assert not re.search(r"(?im)^\s*exit\b", code)  # under iex an exit closes the owner's window
    assert "??" not in code  # Windows PowerShell 5.1 has no ?? and no ternary
    assert not re.search(r"\s\?\s.*\s:\s", code)
    assert "-UseBasicParsing" in code
    assert "SecurityProtocolType]::Tls12" in code
    assert "function Install-Tow" in code  # its settings stay inside the function


def test_install_ps1_checks_the_zip_and_never_overwrites():
    text = PS1.read_text(encoding="utf-8")
    assert "$asset = 'TOW-windows-x64.zip'" in text
    assert "releases/latest/download" in text
    assert "Get-FileHash -LiteralPath $zip -Algorithm SHA256" in text
    assert "TOW is already installed in $Dir" in text
    assert "Join-Path $HOME 'TOW'" in text
    assert "$env:TOW_INSTALL_DIR" in text
    assert "& (Join-Path $Dir 'Start TOW.cmd')" in text
    for option in ("[string]$Version", "[switch]$Autostart", "[switch]$Uninstall", "[switch]$Yes", "[switch]$Purge"):
        assert option in text


def test_install_ps1_closes_the_install_folder_before_anything_is_written_in_it():
    text = PS1.read_text(encoding="utf-8")
    grant = "/inheritance:r /grant:r \"*${sid}:(OI)(CI)F\" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F'"
    assert grant in text
    assert "Join-Path $env:SystemRoot 'System32\\icacls.exe'" in text
    # Only a folder this run created or this account owns, never through a link.
    assert "GetOwner([Security.Principal.SecurityIdentifier])" in text
    assert "if (-not $New -and $owner -ne $sid) { return $false }" in text
    assert "[IO.FileAttributes]::ReparsePoint) { return $false }" in text
    close_root = text.index("$rootClosed = Close-Folder $Dir $created")
    assert close_root < text.index("Invoke-WebRequest") < text.index("ExtractToDirectory")
    # keys\ and data\ on their own when the folder stays open (another account's).
    assert "foreach ($name in @('keys', 'data'))" in text
    assert "if (-not (Close-Folder $folder $new))" in text
    assert text.index("Close-Folder $folder $new") < text.index("& (Join-Path $Dir 'Start TOW.cmd')")


@pytest.mark.allow_system
@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_install_ps1_parses(shell):
    program = shutil.which(shell)
    if program is None:
        pytest.skip(f"no {shell} on this machine")
    script = (
        "$errors = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{PS1}', [ref]$null, [ref]$errors); "
        "$errors | ForEach-Object { $_.ToString() }; if ($errors) { exit 1 }"
    )
    done = subprocess.run(
        [program, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.allow_system
def test_install_ps1_refuses_foreign_data_folder_even_with_purge(tmp_path):
    shell = shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is not installed")
    foreign = tmp_path / "foreign data"
    (foreign / "data").mkdir(parents=True)
    (foreign / "config.yaml").write_text("other app", encoding="utf-8")
    sentinel = foreign / "important.txt"
    sentinel.write_text("keep", encoding="utf-8")
    done = subprocess.run(
        [shell, "-NoProfile", "-File", str(PS1), "-Dir", str(foreign), "-Uninstall", "-Yes", "-Purge"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode != 0
    assert "no TOW install" in done.stderr
    # A sentence, not a PowerShell error record (QA: "At ...install.ps1:72 char:...", CategoryInfo).
    assert done.stderr.strip().startswith("TOW: no TOW install in ")
    assert "CategoryInfo" not in done.stderr
    assert "char:" not in done.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_install_ps1_prints_a_runnable_autostart_hint_and_the_real_port():
    text = PS1.read_text(encoding="utf-8")
    # A quoted path alone is only a string in PowerShell: `& ` runs it.
    assert 'Write-Host "  Autostart: & `"$launcher`" autostart on"' in text
    # A reinstall around kept data shows the port of its config.yaml, not 8787 whatever it says.
    assert "'(?m)^port:\\s*(\\d+)\\s*$'" in text
    assert '$page = "http://127.0.0.1:$shown"' in text
    assert "if ($PSCommandPath) { exit 1 }" in text  # a file run fails; `irm | iex` keeps its window


# --- the release workflow ----------------------------------------------------------------------


def _workflow(name):
    import yaml

    text = (ROOT / ".github" / "workflows" / f"{name}.yml").read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


def _runs(job):
    return "\n".join(step.get("run", "") for step in job["steps"])


@pytest.mark.parametrize("name", ["ci", "release", "installers"])
def test_every_workflow_pins_its_actions_and_only_reads_by_default(name):
    text, flow = _workflow(name)
    # Every action pinned to a commit (the repository requires it); checkout and setup-uv to the
    # very ones CI uses.
    uses = re.findall(r"uses: (\S+)", text)
    assert uses
    assert all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", use) for use in uses), uses
    ci_pins = set(re.findall(r"uses: (\S+@[0-9a-f]{40})", _workflow("ci")[0]))
    assert {use for use in uses if use.startswith(("actions/checkout@", "astral-sh/setup-uv@"))} <= ci_pins
    assert flow["permissions"] == {"contents": "read"}
    assert "persist-credentials: false" in text
    for job_id, job in flow["jobs"].items():
        if job_id not in ("source", "publish"):
            assert "permissions" not in job, job_id


def test_installers_run_the_release_smokes_before_a_tag():
    _text, flow = _workflow("installers")
    _release_text, release = _workflow("release")
    triggers = flow[True]  # YAML 1.1 reads the key `on` as True
    paths = triggers["pull_request"]["paths"]
    for path in ("scripts/**", "install/**", "src/tow/update*", "src/tow/web_update.py"):
        assert path in paths
    # What the updater relies on in the code it installs (before: a change there ran no update test).
    for path in ("src/tow/store.py", "src/tow/supervisor/layout.py", "src/tow/cli.py", "src/tow/locales/**"):
        assert path in paths
    assert triggers["schedule"]
    assert "workflow_dispatch" in triggers
    jobs = flow["jobs"]
    # The same smokes as the release (after the step that makes the source archive).
    tail = [step.get("run") for step in jobs["windows"]["steps"][-3:]]
    assert tail == [step.get("run") for step in release["jobs"]["windows"]["steps"][-4:-1]]
    assert jobs["update"]["strategy"] == release["jobs"]["update"]["strategy"]
    assert jobs["update"]["steps"][-1]["run"] == release["jobs"]["update"]["steps"][-1]["run"]
    assert "git archive --format=tar.gz --prefix=tow/ -o tow-source.tar.gz HEAD" in _runs(jobs["update"])
    # Weekly and by hand (never on a pull request) every large selection scenario is measured;
    # the gate measures only the costliest of each kind.
    selection = jobs["selection"]
    assert selection["if"] == "github.event_name != 'pull_request'"
    assert selection["steps"][-1]["env"] == {"TOW_SELECTION_WORK": "all"}
    assert "tests/test_selection_work_bounds.py" in selection["steps"][-1]["run"]


def test_ci_keeps_the_required_check_names_and_audits_once():
    _text, flow = _workflow("ci")
    jobs = flow["jobs"]
    # Branch protection requires these names: gate (<os>) x4 and install.sh (<os>) x3.
    assert jobs["gate"]["name"] == "gate (${{ matrix.os }})"
    assert jobs["install"]["name"] == "install.sh (${{ matrix.os }})"
    assert jobs["gate"]["strategy"]["matrix"]["os"] == [
        "windows-latest",
        "ubuntu-latest",
        "ubuntu-26.04",
        "macos-latest",
    ]
    assert jobs["install"]["strategy"]["matrix"]["os"] == ["ubuntu-latest", "ubuntu-26.04", "macos-latest"]
    # uv.lock is the same on every OS: pip-audit runs on one runner only.
    audited = [row["os"] for row in jobs["gate"]["strategy"]["matrix"]["include"] if row.get("audit")]
    assert audited == ["ubuntu-latest"]
    assert "${{ matrix.audit && '-Audit' || '' }}" in _runs(jobs["gate"])


def test_the_release_workflow_tests_everything_before_it_uploads():
    _text, flow = _workflow("release")
    jobs = flow["jobs"]
    # The gate is not run again: the tag must be on origin/main and its commit must have passed ci.
    assert "gate" not in jobs
    assert jobs["source"]["permissions"] == {"contents": "read", "actions": "read"}
    source = _runs(jobs["source"])
    assert 'git merge-base --is-ancestor "$commit" refs/remotes/origin/main' in source
    assert '[ "$message" = "TOW ${TAG#v}" ]' in source
    assert "actions/workflows/ci.yml/runs?head_sha=$COMMIT" in source
    assert "completed/success" in source
    assert jobs["source"]["steps"][0]["with"]["fetch-depth"] == 0
    # Write access only where the release is touched, and only after every test passed.
    writers = [name for name, job in jobs.items() if job.get("permissions", {}).get("contents") == "write"]
    assert writers == ["publish"]
    assert jobs["publish"]["permissions"] == {"contents": "write"}
    assert set(jobs["publish"]["needs"]) == {"source", "windows", "posix", "update"}
    for job_id in ("windows", "posix", "update"):
        assert jobs[job_id]["needs"] == ["source"]
    windows = _runs(jobs["windows"])
    assert "scripts/build-bundle.py --out dist --source tow-source.tar.gz" in windows
    assert "bundle-smoke.ps1 -Zip dist/TOW-windows-x64.zip -Offline" in windows
    assert "install-smoke.ps1 -Zip dist/TOW-windows-x64.zip" in windows
    assert jobs["posix"]["strategy"]["matrix"]["os"] == ["ubuntu-latest", "ubuntu-26.04", "macos-latest"]
    assert "scripts/install-smoke.sh" in jobs["posix"]["steps"][-1]["run"]
    assert "scripts/update-smoke.py --previous" in _runs(jobs["update"])


def test_the_release_stays_a_draft_until_every_asset_is_read_back():
    _text, flow = _workflow("release")
    publish = _runs(flow["jobs"]["publish"])
    assert "sha256sum TOW-windows-x64.zip install.ps1 install.sh tow-source.tar.gz > SHA256SUMS" in publish
    create = next(line for line in publish.splitlines() if "gh release create" in line)
    assert "--draft" in create
    assert "--verify-tag" in create
    order = [
        publish.index("gh release upload"),
        publish.index('[ "$(names)" = "$expected" ] || {'),
        publish.index("cmp readback/SHA256SUMS assets/SHA256SUMS"),
        publish.index("--draft=false"),
    ]
    assert order == sorted(order)
    assert "sha256sum -c SHA256SUMS" in publish
    # A public release's files are never replaced; its notes and title are never edited.
    assert publish.index("is public and complete") < publish.index("gh release upload")
    for line in publish.splitlines():
        if "gh release edit" in line:
            assert "--notes" not in line
            assert "--title" not in line


def test_the_windows_installer_smoke_covers_powershell_5_and_the_data():
    text = (ROOT / "scripts" / "install-smoke.ps1").read_text(encoding="utf-8")
    assert "powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer" in text  # 5.1
    assert "-Uninstall -Yes -Purge" in text
    assert "the reinstall replaced keys\\master.key" in text


@pytest.mark.allow_system
@pytest.mark.parametrize("name", ["bundle-smoke.ps1", "install-smoke.ps1"])
def test_the_windows_smoke_scripts_parse(name):
    program = shutil.which("pwsh")
    if program is None:
        pytest.skip("no pwsh on this machine")
    script = (
        "$errors = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{ROOT / 'scripts' / name}', [ref]$null,"
        " [ref]$errors); $errors | ForEach-Object { $_.ToString() }; if ($errors) { exit 1 }"
    )
    done = subprocess.run(
        [program, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.allow_system
def test_the_bundle_smoke_ignores_runner_noise_but_not_ours(tmp_path):
    # CI: a Mozilla folder appeared in AppData during the smoke, which failed as "the bundle
    # wrote outside its folder". Only names TOW, uv or Python could make count there.
    program = shutil.which("pwsh")
    if program is None:
        pytest.skip("no pwsh on this machine")
    probe = tmp_path / "probe.ps1"
    probe.write_text(
        "$ast = [Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$null, [ref]$null)\n"
        "$functions = $ast.FindAll({ $args[0] -is [Management.Automation.Language.FunctionDefinitionAst] }, $true)\n"
        "foreach ($f in $functions) {\n"
        "  if ($f.Name -in 'Test-Ours', 'Compare-Outside') { . ([scriptblock]::Create($f.Extent.Text)) } }\n"
        "$installName = 'tow smoke abc123'\n"
        "$before = [ordered]@{ 'uv' = 3; 'entries of R' = 'A|B'; 'entries of L' = 'X' }\n"
        "function Probe([int]$uv, [string]$r, [string]$l) {\n"
        "  $after = [ordered]@{ 'uv' = $uv; 'entries of R' = $r; 'entries of L' = $l }\n"
        "  '=' + ((Compare-Outside $before $after) -join ',') }\n"
        "Probe 3 'A|B|Mozilla' ''\n"
        "Probe 3 'A|B|Python' 'X'\n"
        "Probe 3 'A|B' 'X|old tow smoke ABC123'\n"
        "Probe 5 'A|B' 'X'\n",
        encoding="utf-8",
    )
    done = subprocess.run(
        [program, "-NoProfile", "-NonInteractive", "-File", str(probe), str(ROOT / "scripts" / "bundle-smoke.ps1")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    lines = done.stdout.splitlines()
    assert "  ignored as runner noise in R: +Mozilla" in lines
    assert "  ignored as runner noise in L: -X" in lines
    assert [line for line in lines if line.startswith("=")] == ["=", "=entries of R", "=entries of L", "=uv"]


@pytest.mark.allow_system
def test_the_posix_install_smoke_ignores_runner_noise_in_home():
    shell = shutil.which("dash") or shutil.which("sh")
    if shell is None:
        pytest.skip("no sh on this machine")
    text = (ROOT / "scripts" / "install-smoke.sh").read_text(encoding="utf-8")
    assert '"$(ls -A "$HOME" | grep -iE "$ours" |' in text  # only our names are compared
    found = re.search(r"^ours=.*?$.*?^noise\(\) \{.*?^\}$", text, re.MULTILINE | re.DOTALL)
    assert found is not None
    before, after = "a\\nb\\nX", "a\\nb\\nMozilla\\npython3\\ntow smoke 1"
    script = found.group(0) + f"\nnoise \"$(printf '{before}')\" \"$(printf '{after}')\"\n"
    done = subprocess.run([shell, "-c", script], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["Mozilla", "X"]


@pytest.mark.parametrize(("workflow", "job_id"), [("ci", "gate"), ("ci", "install"), ("release", "posix")])
def test_linux_runners_keep_both_lts_versions_without_renaming_required_checks(workflow, job_id):
    import yaml

    flow = yaml.safe_load((ROOT / ".github" / "workflows" / f"{workflow}.yml").read_text(encoding="utf-8"))
    job = flow["jobs"][job_id]
    matrix = job["strategy"]["matrix"]
    overrides = {row["os"]: row["runner"] for row in matrix.get("include", [])}
    assert overrides == {"ubuntu-latest": "ubuntu-24.04"}
    assert len(matrix["os"]) == len(set(matrix["os"]))
    actual = {overrides.get(label, label) for label in matrix["os"]}
    expected = {"ubuntu-24.04", "ubuntu-26.04", "macos-latest"}
    if job_id == "gate":
        expected.add("windows-latest")
    assert actual == expected
    assert job["runs-on"] == "${{ matrix.runner || matrix.os }}"
    prefix = "gate" if job_id == "gate" else "install.sh"
    assert job["name"] == f"{prefix} (${{{{ matrix.os }}}})"


def test_release_archive_and_publish_use_a_pinned_linux_runner():
    import yaml

    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))["jobs"]
    assert jobs["source"]["runs-on"] == jobs["publish"]["runs-on"] == "ubuntu-24.04"


def test_release_jobs_build_and_publish_the_commit_the_source_job_checked():
    # Before: each later job checked out the tag again (a tag moved in between built unchecked
    # code), uv's cache came from other runs, and every stable tag became "latest", an older
    # line's fix too.
    import yaml

    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    jobs = yaml.safe_load(text)["jobs"]
    assert jobs["source"]["outputs"] == {"commit": "${{ steps.tag.outputs.commit }}"}
    assert any(step.get("id") == "tag" and "GITHUB_OUTPUT" in step["run"] for step in jobs["source"]["steps"])
    checkouts = setups = 0
    for name, job in jobs.items():
        for step in job["steps"]:
            uses = step.get("uses", "")
            if uses.startswith("actions/checkout@"):
                checkouts += 1
                want = "${{ env.TAG }}" if name == "source" else "${{ needs.source.outputs.commit }}"
                assert step["with"]["ref"] == want, name
                if name != "source":
                    assert "source" in job["needs"], name
            if uses.startswith("astral-sh/setup-uv@"):
                setups += 1
                assert step["with"]["enable-cache"] is False, name
    assert checkouts == len(jobs)
    assert setups == 2
    publish = "\n".join(step.get("run", "") for step in jobs["publish"]["steps"])
    assert "--latest=$latest" in publish
    assert "\nlatest=true\n" not in publish
    assert "sort -V | tail -n 1" in publish
    assert '[ "$highest" != "$TAG" ] || latest=true' in publish
    assert "git get-tar-commit-id" in text


def test_the_smoke_scripts_never_use_the_live_port():
    for name in ("bundle-smoke.ps1", "install-smoke.sh", "install-smoke.ps1", "update-smoke.py"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "never on 8787" in text
        assert "TOW_NO_BROWSER" in text
