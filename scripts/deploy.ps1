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

function Test-Updater([string]$Path) {
    # A Python update.py runs on (3.11 or newer); -X utf8: its path comes back as UTF-8.
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $check = 'import sys; print(sys.executable) if sys.version_info >= (3, 11) else sys.exit(3)'
    # All of its output, not Select-Object -First 1: PowerShell 7 then stops the pipeline before
    # the program ends, and $LASTEXITCODE kept the previous Python's refusal.
    $found = @(& $Path -X utf8 -c $check 2>$null)
    if ($LASTEXITCODE -eq 0 -and $found.Count -gt 0) { return ([string]$found[0]).Trim() }
    return $null
}

function Find-BasePython {
    # In this function only: Windows PowerShell 5.1 turns a native program's stderr sent to $null
    # into errors, and 'Stop' ended the script there instead of at the message below.
    $ErrorActionPreference = 'Continue'
    $pythons = Join-Path (Split-Path -Parent $App) 'runtime\python'
    # app\.venv\pyvenv.cfg names the interpreter the venv was made from ("home = <folder>"); uv
    # writes it as UTF-8, which 5.1 would read in the ANSI code page without -Encoding. The
    # rule of "Update TOW.cmd": that folder's name in this install's runtime\python first (a
    # moved or copied install, even while the old folder is still there), else the newest real
    # cpython-3.X.Y there by number (never uv's cpython-3.X link, never a pre-release), else the
    # folder itself (an install from before 1.18 with a Python of its own).
    $cfg = Join-Path $App '.venv\pyvenv.cfg'
    $venvHome = $null
    if (Test-Path -LiteralPath $cfg -PathType Leaf) {
        $line = Get-Content -LiteralPath $cfg -Encoding UTF8 | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
        if ($line) { $venvHome = ($line -replace '^\s*home\s*=\s*', '').Trim() }
    }
    $candidates = @()
    if ($venvHome) { $candidates += Join-Path (Join-Path $pythons (Split-Path -Leaf $venvHome)) 'python.exe' }
    if (Test-Path -LiteralPath $pythons -PathType Container) {
        $candidates += @(Get-ChildItem -LiteralPath $pythons -Directory |
                Where-Object { -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -and $_.Name -match '^cpython-3\.(\d+)\.(\d+)-' } |
                Sort-Object { [int]([regex]::Match($_.Name, '^cpython-3\.(\d+)\.(\d+)-').Groups[1].Value) }, { [int]([regex]::Match($_.Name, '^cpython-3\.(\d+)\.(\d+)-').Groups[2].Value) } -Descending |
                ForEach-Object { Join-Path $_.FullName 'python.exe' })
    }
    if ($venvHome) { $candidates += Join-Path $venvHome 'python.exe' }
    foreach ($candidate in $candidates) {
        $found = Test-Updater $candidate
        if ($found) { return $found }
    }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        # Every Python the launcher knows (py -0p: " -V:3.12 *  C:\...\python.exe", the default
        # one marked), the default first; only one update.py runs on (3.11 or newer) is taken.
        # "py -3" alone may name 3.10, and it does not see a Python that uv installed.
        $paths = @()
        foreach ($row in @(& $launcher.Source -0p 2>$null)) {
            if ([string]$row -match '^\s*-\S+\s+(\*\s+)?(\S.*\.exe)\s*$') {
                if ($Matches[1]) { $paths = @($Matches[2]) + $paths } else { $paths += $Matches[2] }
            }
        }
        foreach ($path in $paths) {
            $found = Test-Updater $path
            if ($found) { return $found }
        }
    }
    throw "no base Python found for ${App}: no Python 3.11 or newer (none in $pythons, none named by app\.venv\pyvenv.cfg, none the py launcher knows); run update.py with any Python 3.11+"
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
