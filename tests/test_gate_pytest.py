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
