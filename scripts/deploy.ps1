#Requires -Version 7.0
<#
.SYNOPSIS
    Update this TOW runtime install to a git ref (tag or commit) and restart it.

.DESCRIPTION
    Since 1.18 a thin Windows wrapper around scripts\update.py (the same steps on every OS: see
    its description and README, "Обновление"). It finds the install's base Python - the one the
    app\.venv was made from, not the venv itself, so uv sync can replace the venv's files - and
    runs update.py with it. The parameters are the same as before.

    update.py stops TOW (tow run), snapshots data\ and config.yaml into
    <runtime>\backup\update-<time>-before-<tag>, checks out the ref, runs uv sync with the
    launchers' environment (runtime\python, runtime\cache), starts TOW, requires /healthz to
    report the new version and /health.json to answer, and rolls back code (and data when the new
    version changed it) if not. <runtime>\update-state.json records the run. A target older
    than v1.18.0 is refused.

.EXAMPLE
    <runtime>\app\scripts\deploy.ps1 -Ref v1.18.0
#>
param(
    [Parameter(Mandatory = $true)][string]$Ref,
    [int]$HealthTimeoutSec = 90,
    [int]$CheckWaitMinutes = 15,
    [int]$KeepSnapshots = 5
)

$ErrorActionPreference = 'Stop'
$App = Split-Path -Parent $PSScriptRoot

function Find-BasePython {
    # app\.venv\pyvenv.cfg names the interpreter the venv was made from ("home = <folder>").
    $cfg = Join-Path $App '.venv\pyvenv.cfg'
    if (Test-Path $cfg) {
        $line = Get-Content $cfg | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
        if ($line) {
            $candidate = Join-Path (($line -replace '^\s*home\s*=\s*', '').Trim()) 'python.exe'
            if (Test-Path $candidate) { return $candidate }
        }
    }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $found = & $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $found) { return $found.Trim() }
    }
    throw "no base Python found for $App (app\.venv\pyvenv.cfg is missing); run update.py with any Python 3.11+"
}

$python = Find-BasePython
& $python (Join-Path $PSScriptRoot 'update.py') --ref $Ref --health-timeout $HealthTimeoutSec --wait-minutes $CheckWaitMinutes --keep $KeepSnapshots
exit $LASTEXITCODE
