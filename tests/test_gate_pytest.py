"""Run only the gate's test-run clauses, from scripts/gate.ps1's own text, with fake tools."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "scripts/gate.ps1"
# The gate's $RequireCoverage assignment and the if statement that chooses the coverage run.
COVERAGE_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:TOW_TEST_GATE, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw 'gate parse failed' }
$assignments = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $node.Left.Extent.Text -eq '$RequireCoverage'
}, $true))
$clauses = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
    $node.Clauses[0].Item1.Extent.Text -eq '$LASTEXITCODE -eq 0' -and
    $node.Extent.Text.Contains("Skip 'coverage'")
}, $true))
if ($assignments.Count -ne 1 -or $clauses.Count -ne 1) { throw 'one coverage choice required' }
$script:steps = [System.Collections.Generic.List[string]]::new()
$script:skipped = [System.Collections.Generic.List[string]]::new()
function Step([string]$Name, [scriptblock]$Command) { $script:steps.Add($Name) }
function Skip([string]$Name, [string]$Why) { $script:skipped.Add($Name) }
$PytestCov = 'pytest-cov>=7.1'
. ([scriptblock]::Create($assignments[0].Extent.Text))
$global:LASTEXITCODE = 1  # pytest-cov could not be had
$failure = $null
try { . ([scriptblock]::Create($clauses[0].Extent.Text)) }
catch { $failure = $_.Exception.Message }
@{failure=$failure; steps=@($script:steps.ToArray()); skipped=@($script:skipped.ToArray())} |
    ConvertTo-Json -Compress
"""


def _pwsh():
    shell = shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is not installed")
    return shell


def _run(harness, **variables):
    environment = {key: value for key, value in os.environ.items() if key not in {"CI", "TOW_GATE_REQUIRE_COVERAGE"}}
    environment.update(TOW_TEST_GATE=str(GATE), **variables)
    done = subprocess.run(
        [_pwsh(), "-NoProfile", "-NonInteractive", "-Command", harness],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


@pytest.mark.allow_system
@pytest.mark.parametrize("variables", [{"CI": "true"}, {"TOW_GATE_REQUIRE_COVERAGE": "1"}])
def test_ci_never_skips_coverage_silently(variables):
    result = _run(COVERAGE_HARNESS, **variables)
    assert "coverage is required here" in result["failure"]
    assert result["steps"] == result["skipped"] == []


@pytest.mark.allow_system
def test_a_developer_machine_without_pytest_cov_runs_the_tests_and_says_so():
    result = _run(COVERAGE_HARNESS, CI="false")
    assert result == {"failure": None, "steps": ["pytest (parallel, random order)"], "skipped": ["coverage"]}


# The gate's own Invoke-WithDeadline, run on a synthetic program tree that never ends by itself.
DEADLINE_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:TOW_TEST_GATE, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw 'gate parse failed' }
$names = 'Get-StartTime', 'Add-ProcessTree', 'Stop-ProcessTree', 'Invoke-WithDeadline'
$functions = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $names
}, $true))
if ($functions.Count -ne $names.Count) { throw 'the deadline functions are required' }
$functions | ForEach-Object { . ([scriptblock]::Create($_.Extent.Text)) }
$arguments = @($env:TOW_TEST_ARGS | ConvertFrom-Json)
$global:LASTEXITCODE = 0
$failure = $null
Set-Location -LiteralPath $env:TOW_TEST_PIDS  # the gate runs from its checkout, set by Set-Location
$clock = [System.Diagnostics.Stopwatch]::StartNew()
try { Invoke-WithDeadline ([int]$env:TOW_TEST_DEADLINE) $env:TOW_TEST_PYTHON $arguments *> $null }
catch { $failure = $_.Exception.Message }
$elapsed = $clock.Elapsed.TotalSeconds
$code = $LASTEXITCODE
$ids = @(Get-ChildItem -LiteralPath $env:TOW_TEST_PIDS -Filter '*.pid' |
    ForEach-Object { [int](Get-Content -Raw $_.FullName) })
$alive = @()
$until = [DateTime]::UtcNow.AddSeconds(15)
do {
    $alive = @($ids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
    if ($alive.Count -eq 0) { break }
    Start-Sleep -Milliseconds 200
} while ([DateTime]::UtcNow -lt $until)
$alive | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }  # never leave a hung tree
@{failure=$failure; code=$code; elapsed=$elapsed; started=$ids.Count; alive=@($alive)} | ConvertTo-Json -Compress
"""
# A test run that hangs with a worker: it records its pid, starts a child that records its own,
# and both sleep far past the deadline.
TREE = """
import os, subprocess, sys, time
from pathlib import Path

folder = Path(sys.argv[1])
if len(sys.argv) > 2:
    (folder / "worker.pid").write_text(str(os.getpid()))
else:
    subprocess.Popen([sys.executable, __file__, str(folder), "worker"])
    (folder / "main.pid").write_text(str(os.getpid()))
time.sleep(300)
"""


def _deadline(tmp_path, deadline, arguments):
    import sys

    pids = tmp_path / "pids"
    pids.mkdir()
    return _run(
        DEADLINE_HARNESS,
        TOW_TEST_DEADLINE=str(deadline),
        TOW_TEST_PYTHON=sys.executable,
        TOW_TEST_PIDS=str(pids),
        TOW_TEST_ARGS=json.dumps([str(argument) if argument != "{pids}" else str(pids) for argument in arguments]),
    )


@pytest.mark.allow_system
def test_a_hung_test_run_is_stopped_at_its_deadline_with_its_whole_tree(tmp_path):
    script = tmp_path / "tree.py"
    script.write_text(TREE, encoding="utf-8")
    # 20 s: both Python processes must start before the deadline, also on a slow Windows runner.
    result = _deadline(tmp_path, 20, [script, "{pids}"])
    assert result["failure"] == "gate failed: the test run did not finish within 20 s"
    assert result["code"] == 124
    assert result["started"] == 2, "the synthetic run and its worker both started before the deadline"
    assert result["alive"] == [], "the whole tree is stopped"
    assert 20 <= result["elapsed"] < 75


@pytest.mark.allow_system
def test_a_finished_test_run_keeps_its_exit_code_and_the_gates_location(tmp_path):
    # The child records its working folder at an absolute path: a relative file lands wherever the
    # child starts, and when that was wrong it left cwd.txt in the checkout.
    record = "import os, sys; open(sys.argv[1], 'w').write(os.getcwd()); raise SystemExit(3)"
    result = _deadline(tmp_path, 60, ["-c", record, tmp_path / "cwd.txt"])
    assert (result["failure"], result["code"], result["alive"]) == (None, 3, [])
    assert result["elapsed"] < 60
    assert Path((tmp_path / "cwd.txt").read_text(encoding="utf-8")) == tmp_path / "pids"


# Every full test run of the gate goes through the deadline, and the passed tree is recorded
# only after them, inside the same try: a deadline failure throws past it.
SHAPE_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:TOW_TEST_GATE, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw 'gate parse failed' }
$runs = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.CommandAst] -and
    @($node.CommandElements | Where-Object { $_.Extent.Text -eq 'pytest' }).Count -gt 0
}, $true) | ForEach-Object {
    @{name=$_.GetCommandName(); collect=$_.Extent.Text.Contains('--collect-only'); at=$_.Extent.StartOffset}
})
$stamps = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.CommandAst] -and $node.GetCommandName() -eq 'Save-PassedTree'
}, $true))
$try = $stamps[0].Parent
while ($try -and $try -isnot [System.Management.Automation.Language.TryStatementAst]) { $try = $try.Parent }
function Get-Calls([string]$Name) {
    @($ast.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.CommandAst] -and $node.GetCommandName() -eq $Name
    }, $true) | Where-Object {
        $parent = $_.Parent
        while ($parent -and $parent -isnot [System.Management.Automation.Language.FunctionDefinitionAst]) {
            $parent = $parent.Parent
        }
        -not $parent  # the gate's own flow, not a function's body
    } | ForEach-Object { $_.Extent.StartOffset })
}
@{runs=@($runs); stamps=$stamps.Count; stamp_at=$stamps[0].Extent.StartOffset;
  try_from=$try.Body.Extent.StartOffset; try_to=$try.Body.Extent.EndOffset;
  listed=@(Get-Calls 'Get-UntrackedFiles'); checked=@(Get-Calls 'Assert-NoNewFiles')} |
    ConvertTo-Json -Compress -Depth 4
"""


@pytest.mark.allow_system
def test_every_full_test_run_has_the_deadline_and_the_pass_is_recorded_after_it():
    result = _run(SHAPE_HARNESS)
    full = [run for run in result["runs"] if not run["collect"]]
    assert len(full) == 2, result  # with and without coverage
    assert {run["name"] for run in full} == {"Invoke-ToolWithDeadline"}
    assert result["stamps"] == 1
    assert all(result["try_from"] <= run["at"] < result["stamp_at"] < result["try_to"] for run in full)


@pytest.mark.allow_system
def test_every_test_run_is_followed_by_the_check_for_new_files():
    """Collection (-Quick) and the full run: the untracked files are listed once before them, and
    each is checked after it, before the tree is recorded as passed."""
    result = _run(SHAPE_HARNESS)
    assert len(result["listed"]) == 1
    assert len(result["checked"]) == 2
    for run in result["runs"]:
        assert result["listed"][0] < run["at"]
        assert any(run["at"] < at for at in result["checked"]), run
    assert all(at < result["stamp_at"] for at in result["checked"])


# The gate's own check for new files, in a throwaway repository.
NEW_FILES_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:TOW_TEST_GATE, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw 'gate parse failed' }
$names = 'Get-UntrackedFiles', 'Assert-NoNewFiles'
$functions = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $names
}, $true))
if ($functions.Count -ne $names.Count) { throw 'the check for new files is required' }
$functions | ForEach-Object { . ([scriptblock]::Create($_.Extent.Text)) }
Set-Location -LiteralPath $env:TOW_TEST_REPO
$before = @(Get-UntrackedFiles)
$quiet = $null
try { Assert-NoNewFiles $before } catch { $quiet = $_.Exception.Message }
Set-Content -LiteralPath (Join-Path $env:TOW_TEST_REPO 'cwd.txt') -Value 'x'
Set-Content -LiteralPath (Join-Path $env:TOW_TEST_REPO 'ignored/state.json') -Value '{}'
$failure = $null
try { Assert-NoNewFiles $before } catch { $failure = $_.Exception.Message }
@{before=@($before); quiet=$quiet; failure=$failure} | ConvertTo-Json -Compress
"""


@pytest.mark.allow_system
def test_a_file_a_test_run_leaves_in_the_checkout_fails_the_gate(tmp_path):
    repo = tmp_path / "checkout"
    (repo / "ignored").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=60)
    (repo / ".gitignore").write_text("/ignored/\n", encoding="utf-8")
    (repo / "notes.txt").write_text("there before the run", encoding="utf-8")
    result = _run(NEW_FILES_HARNESS, TOW_TEST_REPO=str(repo))
    assert result["before"] == [".gitignore", "notes.txt"]
    assert result["quiet"] is None  # nothing new: the gate goes on
    assert result["failure"] == "gate failed: the test run left files in the checkout: cwd.txt"
