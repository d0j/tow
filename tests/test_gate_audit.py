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
$pins = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $node.Left.Extent.Text -eq '$PipAudit'
}, $true))
if ($pins.Count -ne 1) { throw 'one pinned audit tool required' }
. ([scriptblock]::Create($pins[0].Extent.Text))
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
    # The audit tool runs outside the lock: one exact version, never the newest of the day.
    if ($args[0] -ne '--from' -or $args[1] -notmatch '^pip-audit==\d+(\.\d+)+$' -or $args[2] -ne 'pip-audit') {
        throw 'unpinned audit tool'
    }
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
if ($env:TOW_TEST_LEAK -eq '1') {
    # Negative control: leave only the requirements this invocation reserved, not arbitrary files.
    function Remove-Item([string]$LiteralPath, [switch]$Force, [string]$ErrorAction) {
        if (-not $script:exports.Contains([IO.Path]::GetFullPath($LiteralPath))) {
            throw 'unexpected cleanup target'
        }
    }
}
$Audit = $true
$failure = $null
try { & $script:body }
catch { $failure = $_.Exception.Message }
@{failure=$failure; rows=@($script:rows.ToArray()); exports=@($script:exports.ToArray())} |
    ConvertTo-Json -Depth 5 -Compress
"""


def _borrowed_files(scratch: Path) -> dict[Path, bytes]:
    # Unix .NET owns .dotnet runtime/IPC data, not the gate. Give it an existing directory:
    # https://github.com/dotnet/runtime/blob/v10.0.13/src/coreclr/pal/src/include/pal/sharedmemory.h
    # Its presence is no reason to ignore a new root entry or a gate-owned requirements file.
    originals = {
        scratch / ".dotnet" / "runtime-owned": b"runtime fixture",
        scratch / "borrowed.txt": b"another operation's file",
    }
    for path, content in originals.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return originals


def _assert_cleanup(scratch: Path, exports: list[str], originals: dict[Path, bytes]) -> None:
    remaining = [Path(value) for value in exports if Path(value).exists()]
    assert not remaining, f"gate-owned requirements remain: {remaining}"
    expected = {scratch / path.relative_to(scratch).parts[0] for path in originals}
    assert set(scratch.iterdir()) == expected, "unexpected temporary entries remain"
    for path, content in originals.items():
        assert path.read_bytes() == content, f"borrowed file changed: {path}"


def _run_audit(tmp_path: Path, case: str, *, leak: bool = False) -> tuple[dict, Path, dict[Path, bytes]]:
    shell = shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is not installed")
    scratch = tmp_path / "audit temp"
    scratch.mkdir()
    originals = _borrowed_files(scratch)
    environment = {
        **os.environ,
        "TEMP": str(scratch),
        "TMP": str(scratch),
        "TMPDIR": str(scratch),
        "TOW_TEST_TEMP": str(scratch),
        "TOW_TEST_GATE": str(GATE),
        "TOW_TEST_CASE": case,
        "TOW_TEST_LEAK": "1" if leak else "0",
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
    return json.loads(done.stdout), scratch, originals


@pytest.mark.allow_system
@pytest.mark.parametrize("case", ["success", "overlap", "export_failure", "audit_failure"])
def test_gate_audit_owns_its_requirements_and_cleans_up(tmp_path, case):
    result, scratch, originals = _run_audit(tmp_path, case)
    assert result["failure"] == {
        "export_failure": "fixture failure: export locked requirements",
        "audit_failure": "fixture failure: pip-audit",
    }.get(case), result
    assert all(row["expected"] == row["actual"] for row in result["rows"]), result
    expected_exports = 2 if case == "overlap" else 1
    assert len(result["exports"]) == len(set(result["exports"])) == expected_exports
    expected_audits = 0 if case == "export_failure" else expected_exports
    assert len(result["rows"]) == expected_audits
    _assert_cleanup(scratch, result["exports"], originals)


@pytest.mark.allow_system
@pytest.mark.parametrize("case", ["success", "overlap", "export_failure", "audit_failure"])
def test_a_gate_owned_requirements_leak_fails_the_cleanup_check(tmp_path, case):
    result, scratch, originals = _run_audit(tmp_path, case, leak=True)
    assert result["failure"] == {
        "export_failure": "fixture failure: export locked requirements",
        "audit_failure": "fixture failure: pip-audit",
    }.get(case), result
    assert len(result["exports"]) == (2 if case == "overlap" else 1)
    assert all(Path(value).is_file() for value in result["exports"])
    with pytest.raises(AssertionError, match="gate-owned requirements remain"):
        _assert_cleanup(scratch, result["exports"], originals)


def test_an_unrecorded_requirements_leak_fails_the_cleanup_check(tmp_path):
    originals = _borrowed_files(tmp_path)
    (tmp_path / "unexpected-requirements.txt").write_text("leaked", encoding="utf-8")
    with pytest.raises(AssertionError, match="unexpected temporary entries remain"):
        _assert_cleanup(tmp_path, [], originals)
