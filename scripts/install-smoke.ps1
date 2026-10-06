#Requires -Version 7.0
<#
.SYNOPSIS
    Smoke test of install/install.ps1 on Windows PowerShell 5.1 with a local bundle zip.

.DESCRIPTION
    Installs TOW-windows-x64.zip (checked against a SHA256SUMS made for it) with install.ps1 run by
    powershell.exe 5.1 into a folder with a space, checks /healthz and `tow status`, uninstalls it
    keeping the data, installs again around the data (the same keys\master.key), checks it again,
    uninstalls and purges what is left. TOW_NO_BROWSER=1; never port 8787. The CI workflows run it
    after scripts/bundle-smoke.ps1 (installers.yml on pull requests, release.yml on a tag).

.EXAMPLE
    pwsh scripts/install-smoke.ps1 -Zip dist/TOW-windows-x64.zip -Port 18878
#>
param(
    [Parameter(Mandatory = $true)][string]$Zip,
    [int]$Port = 18878,
    [string]$Base = $(if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() })
)

$ErrorActionPreference = 'Stop'
if ($Port -eq 8787) { throw 'install-smoke: never on 8787 (a real TOW may run there)' }
$installer = Join-Path (Split-Path -Parent $PSScriptRoot) 'install/install.ps1'
$zip = (Resolve-Path $Zip).Path
$sums = Join-Path $Base ('SHA256SUMS.install-smoke.' + [guid]::NewGuid().ToString('N').Substring(0, 6))
$dir = Join-Path $Base ('TOW install test ' + [guid]::NewGuid().ToString('N').Substring(0, 6))
"$((Get-FileHash $zip -Algorithm SHA256).Hash.ToLowerInvariant())  TOW-windows-x64.zip" | Set-Content -Encoding ascii $sums
# The installer runs as on a new computer: no uv or TOW variables of this machine.
Get-ChildItem env: | Where-Object { $_.Name -like 'UV_*' -or $_.Name -like 'TOW_*' } | ForEach-Object { Remove-Item "env:$($_.Name)" }
$env:TOW_INSTALL_SOURCE = $zip; $env:TOW_INSTALL_SUMS = $sums; $env:TOW_NO_BROWSER = '1'

function Test-Health {
    try { return (Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -NoProxy -TimeoutSec 5).ok -eq $true }
    catch { return $false }
}

try {
    if (Test-Health) { throw "install-smoke: something already answers on port $Port" }
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer -Dir $dir -Port $Port
    if ($LASTEXITCODE) { throw "install.ps1 exited with $LASTEXITCODE" }
    if (-not (Test-Health)) { throw '/healthz does not answer' }
    & (Join-Path $dir 'app\scripts\tow.cmd') status
    # Uninstall keeping the data, install again around it (same key), then purge what is left.
    pwsh -NoProfile -File $installer -Dir $dir -Uninstall -Yes
    if (Test-Path -LiteralPath (Join-Path $dir 'app')) { throw 'the uninstall left app' }
    $key = (Get-FileHash (Join-Path $dir 'keys\master.key')).Hash
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer -Dir $dir -Port $Port
    if ($LASTEXITCODE) { throw "the reinstall exited with $LASTEXITCODE" }
    if ((Get-FileHash (Join-Path $dir 'keys\master.key')).Hash -ne $key) { throw 'the reinstall replaced keys\master.key' }
    if (-not (Test-Health)) { throw '/healthz does not answer after the reinstall' }
    pwsh -NoProfile -File $installer -Dir $dir -Uninstall -Yes
    pwsh -NoProfile -File $installer -Dir $dir -Uninstall -Yes -Purge
    if (Test-Path -LiteralPath $dir) { throw "-Purge left $dir" }
    Write-Host 'install-smoke: passed' -ForegroundColor Green
}
finally {
    if ((Test-Health) -and (Test-Path -LiteralPath (Join-Path $dir 'app\scripts\tow.cmd'))) {
        & (Join-Path $dir 'app\scripts\tow.cmd') stop | Out-Null
    }
    Remove-Item -LiteralPath $sums -Force -ErrorAction SilentlyContinue
}
