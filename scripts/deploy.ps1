<#
.SYNOPSIS
    Update this TOW runtime install to a git ref (tag or commit) and restart it.

.DESCRIPTION
    Since 1.18 a thin Windows wrapper around scripts\update.py (the same steps on every OS: see
    its description and docs\PORTABLE.md, section 4). It finds the install's base Python - the one
    the app\.venv was made from, not the venv itself, so uv sync can replace the venv's files - and
    runs update.py with it. The parameters are the same as before.

    update.py stops TOW (tow run), snapshots data\ and config.yaml into
    <runtime>\backup\update-<time>-before-<tag>, checks out the ref, runs uv sync with the
    launchers' environment (runtime\python, runtime\cache), starts TOW, requires /healthz to
    report the new version and /health.json to answer, and rolls back code (and data when the new
    version changed it) if not. <runtime>\update-state.json records the run. A target older
    than v1.18.0 is refused.

    Runs in Windows PowerShell 5.1 and PowerShell 7 (a git install may have only 5.1), so: ASCII
    only (5.1 reads a file without a BOM in the ANSI code page), no ?? / ?: / && / ||, and
    -LiteralPath for every path (a folder may have [ ] in its name). When the execution policy
    refuses scripts: powershell -ExecutionPolicy Bypass -File <runtime>\app\scripts\deploy.ps1 -Ref <tag>

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
    # app\.venv\pyvenv.cfg names the interpreter the venv was made from ("home = <folder>"); uv
    # writes it as UTF-8, which 5.1 would read in the ANSI code page without -Encoding.
    $cfg = Join-Path $App '.venv\pyvenv.cfg'
    if (Test-Path -LiteralPath $cfg -PathType Leaf) {
        $line = Get-Content -LiteralPath $cfg -Encoding UTF8 | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
        if ($line) {
            $candidate = Join-Path (($line -replace '^\s*home\s*=\s*', '').Trim()) 'python.exe'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
    }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        # -X utf8: the path comes back as UTF-8, which the console encoding set below reads.
        $found = & $launcher.Source -3 -X utf8 -c 'import sys; print(sys.executable)' 2>$null | Select-Object -First 1
        if ($LASTEXITCODE -eq 0 -and $found) { return $found.Trim() }
    }
    throw "no base Python found for $App (app\.venv\pyvenv.cfg is missing); run update.py with any Python 3.11+"
}

# update.py writes UTF-8. When this script's output goes to a file or another program, PowerShell
# would read it in the console's old code page and garble every non-English letter and the ellipsis.
$encoding = [Console]::OutputEncoding
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch { }
try {
    $python = Find-BasePython
    & $python (Join-Path $PSScriptRoot 'update.py') --ref $Ref --health-timeout $HealthTimeoutSec --wait-minutes $CheckWaitMinutes --keep $KeepSnapshots
    $code = $LASTEXITCODE
} finally {
    try { [Console]::OutputEncoding = $encoding } catch { }
}
exit $code
