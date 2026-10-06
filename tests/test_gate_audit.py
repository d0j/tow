"""Run only the gate's audit clause, with isolated temporary files and fake tools."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "scripts/gate.ps1"
HARNESS = r"""
$ErrorActionPreference = 'Stop'
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:TOW_TEST_GATE, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw 'gate parse failed' }
$clauses = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
    $node.Clauses[0].Item1.Extent.Text -eq '$Audit'
}, $true))
if ($clauses.Count -ne 1) { throw 'one audit clause required' }
$script:body = [scriptblock]::Create($clauses[0].Extent.Text)
$script:label = 'outer-fixture'
$script:inside = $false
$script:rows = [System.Collections.Generic.List[object]]::new()
$script:exports = [System.Collections.Generic.List[string]]::new()
function Step([string]$Name, [scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) { throw ('fixture failure: ' + $Name) }
}
function uv {
    $index = [Array]::IndexOf($args, '-o')
    if ($index -lt 0) { throw 'unexpected export command' }
    $target = [IO.Path]::GetFullPath([string]$args[$index + 1])
    if ([IO.Path]::GetDirectoryName($target) -ne $env:TOW_TEST_TEMP) {
        throw 'export escaped isolated folder'
    }
    if (-not [IO.File]::Exists($target) -or (Get-Item -LiteralPath $target).Length -ne 0) {
        throw 'export target was not reserved empty'
    }
    $script:exports.Add($target)
    [IO.File]::WriteAllText($target, $script:label)
    $global:LASTEXITCODE = if ($env:TOW_TEST_CASE -eq 'export_failure') { 7 } else { 0 }
}
function uvx {
    $index = [Array]::IndexOf($args, '-r')
    if ($index -lt 0) { throw 'unexpected audit command' }
    $target = [string]$args[$index + 1]
    $expected = $script:label
    if ($env:TOW_TEST_CASE -eq 'overlap' -and -not $script:inside) {
        $script:inside = $true
        $script:label = 'inner-fixture'
        & $script:body
        $script:label = $expected
    }
    $script:rows.Add(@{expected=$expected; actual=[IO.File]::ReadAllText($target)})
    $global:LASTEXITCODE = if ($env:TOW_TEST_CASE -eq 'audit_failure') { 9 } else { 0 }
}
$Audit = $true
$failure = $null
try { & $script:body }
catch { $failure = $_.Exception.Message }
@{failure=$failure; rows=@($script:rows.ToArray()); exports=@($script:exports.ToArray())} |
    ConvertTo-Json -Depth 5 -Compress
"""


@pytest.mark.allow_system
@pytest.mark.parametrize("case", ["success", "overlap", "export_failure", "audit_failure"])
def test_gate_audit_owns_its_requirements_and_cleans_up(tmp_path, case):
    shell = shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is not installed")
    scratch = tmp_path / "audit temp"
    scratch.mkdir()
    environment = {
        **os.environ,
        "TEMP": str(scratch),
        "TMP": str(scratch),
        "TMPDIR": str(scratch),
        "TOW_TEST_TEMP": str(scratch),
        "TOW_TEST_GATE": str(GATE),
        "TOW_TEST_CASE": case,
    }
    done = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", HARNESS],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    result = json.loads(done.stdout)
    assert result["failure"] == {
        "export_failure": "fixture failure: export locked requirements",
        "audit_failure": "fixture failure: pip-audit",
    }.get(case), result
    assert all(row["expected"] == row["actual"] for row in result["rows"]), result
    expected_exports = 2 if case == "overlap" else 1
    assert len(result["exports"]) == len(set(result["exports"])) == expected_exports
    expected_audits = 0 if case == "export_failure" else expected_exports
    assert len(result["rows"]) == expected_audits
    assert list(scratch.iterdir()) == []
