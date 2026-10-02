#Requires -Version 7.0
<#
.SYNOPSIS
    Smoke test of the Windows bundle: unpack, double-click "Start TOW.cmd", check, stop, clean up.

.DESCRIPTION
    Unpacks TOW-windows-x64.zip into a folder whose path has a space and Cyrillic letters (as
    under a user named in Russian), sets a test port in its config.yaml (never 8787: a real TOW
    may run there), runs "Start TOW.cmd" with TOW_NO_BROWSER=1 and the keyboard closed, waits for
    /healthz, runs `tow status`, starts again (it only finds TOW running), stops it with
    "Stop TOW.cmd" and removes the folder. -Offline points every proxy variable at a closed port
    for the first start: the bundle must start without the internet. Places outside the folder
    where uv or Python could write (uv's own folders, the Python registry keys, the top of the
    profile folders) are compared before and after.

.EXAMPLE
    pwsh scripts/bundle-smoke.ps1 -Zip dist/TOW-windows-x64.zip -Offline
#>
param(
    [Parameter(Mandatory = $true)][string]$Zip,
    [int]$Port = 18877,
    [string]$Base = $(if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }),
    [switch]$Offline,
    [switch]$Keep
)

$ErrorActionPreference = 'Stop'
if ($Port -eq 8787) { throw 'bundle-smoke: never on 8787 (a real TOW may run there)' }
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Step([string]$Text) { Write-Host "bundle-smoke: $Text" -ForegroundColor Cyan }

# Where uv, Python or an installer could write outside the folder.
function Get-Outside {
    $places = @(
        (Join-Path $env:LOCALAPPDATA 'uv'), (Join-Path $env:APPDATA 'uv'), (Join-Path $env:APPDATA 'Python'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python'), (Join-Path $HOME '.local\bin'), (Join-Path $HOME '.cache')
    )
    $state = [ordered]@{}
    foreach ($place in $places) {
        $state[$place] = if (Test-Path -LiteralPath $place) { (Get-ChildItem -LiteralPath $place -Force -Recurse -Depth 2 -ErrorAction SilentlyContinue | Measure-Object).Count } else { 'absent' }
    }
    foreach ($key in 'HKCU:\Software\Python', 'HKCU:\Software\Classes\Python.File') {
        $state[$key] = if (Test-Path $key) { (Get-ChildItem $key -Recurse -ErrorAction SilentlyContinue | Measure-Object).Count } else { 'absent' }
    }
    foreach ($folder in $HOME, $env:APPDATA, $env:LOCALAPPDATA) {
        $state["entries of $folder"] = ((Get-ChildItem -LiteralPath $folder -Force -Name -ErrorAction SilentlyContinue) | Sort-Object) -join '|'
    }
    return $state
}

function Wait-Until([scriptblock]$Condition, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return [bool](& $Condition)
}

function Test-Health {
    try { return (Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -NoProxy -TimeoutSec 3).ok -eq $true }
    catch { return $false }
}

$cyrillic = -join [char[]](0x0422, 0x0435, 0x0441, 0x0442)  # "Test" in Russian: the file stays ASCII
$folder = Join-Path $Base ("tow smoke $cyrillic " + [guid]::NewGuid().ToString('N').Substring(0, 6))
$root = Join-Path $folder 'TOW'
$saved = @{ TOW_NO_BROWSER = $env:TOW_NO_BROWSER; HTTP_PROXY = $env:HTTP_PROXY; HTTPS_PROXY = $env:HTTPS_PROXY; ALL_PROXY = $env:ALL_PROXY; NO_PROXY = $env:NO_PROXY }
# The bundle runs as on a new computer: no TOW variables of this machine (a developer's machine
# may name its own install's master key in TOW_MASTER_KEY_FILE).
foreach ($name in @(Get-ChildItem env: | Where-Object Name -like 'TOW_*' | ForEach-Object Name)) {
    $saved[$name] = (Get-Item "env:$name").Value
    Remove-Item "env:$name"
}
$failed = $null
try {
    if (Test-Health) { throw "bundle-smoke: something already answers on port $Port" }
    $before = Get-Outside
    Step "unpacking into $folder"
    [IO.Compression.ZipFile]::ExtractToDirectory((Resolve-Path $Zip).Path, $folder)
    $config = Join-Path $root 'config.yaml'
    $text = (Get-Content -LiteralPath $config -Raw) -replace '(?m)^port:.*$', "port: $Port"
    [IO.File]::WriteAllText($config, $text, [Text.UTF8Encoding]::new($false))

    $env:TOW_NO_BROWSER = '1'
    if ($Offline) {
        # Any download would go to a closed port and fail: the first start must not need one.
        $env:HTTP_PROXY = 'http://127.0.0.1:9'; $env:HTTPS_PROXY = 'http://127.0.0.1:9'; $env:ALL_PROXY = 'http://127.0.0.1:9'
        $env:NO_PROXY = ''
    }
    Step 'first start: Start TOW.cmd'
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $output = '' | & (Join-Path $root 'Start TOW.cmd') 2>&1 | Out-String
    $code = $LASTEXITCODE
    $watch.Stop()
    Write-Host $output
    if ($code -ne 0) { throw "Start TOW.cmd exited with $code" }
    if ($output -match 'were not enough') { throw 'the first start needed the internet' }
    Step ("first start took {0:n0} s" -f $watch.Elapsed.TotalSeconds)
    foreach ($name in 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY') { Set-Item "env:$name" $saved[$name] }

    if (-not (Wait-Until { Test-Health } 30)) { throw "/healthz does not answer on $Port" }
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -NoProxy
    Step "healthz: ok=$($health.ok) version=$($health.version)"
    if (-not (Test-Path -LiteralPath (Join-Path $root 'keys\master.key'))) { throw 'no keys\master.key after the first start' }
    if (Test-Path -LiteralPath (Join-Path $root 'app\.venv\Scripts\python.exe')) { Step 'app\.venv was built' } else { throw 'no app\.venv' }

    $status = & (Join-Path $root 'app\scripts\tow.cmd') status --json | ConvertFrom-Json
    Step "tow status: running=$($status.running) version=$($status.version)"
    if (-not $status.running) { throw 'tow status says TOW is not running' }

    Step 'second start: it finds TOW running'
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $output = '' | & (Join-Path $root 'Start TOW.cmd') 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "the second Start TOW.cmd exited with $LASTEXITCODE`n$output" }
    Step ("second start took {0:n1} s" -f $watch.Elapsed.TotalSeconds)

    Step 'Stop TOW.cmd'
    $output = '' | & (Join-Path $root 'Stop TOW.cmd') 2>&1 | Out-String
    Write-Host $output
    if (-not (Wait-Until { -not (Test-Health) } 60)) { throw 'TOW still answers after Stop TOW.cmd' }

    $after = Get-Outside
    $changed = @($before.Keys | Where-Object { "$($before[$_])" -ne "$($after[$_])" })
    foreach ($place in $changed) { Write-Host "  outside the folder: $place`n    before: $($before[$place])`n    after:  $($after[$place])" -ForegroundColor Yellow }
    if ($changed.Count) { throw "the bundle wrote outside its folder: $($changed -join ', ')" }
    $size = (Get-ChildItem -LiteralPath $root -Recurse -File -Force | Measure-Object -Sum Length).Sum
    Step ("passed; the folder after the first start: {0:n1} MiB" -f ($size / 1MB))
}
catch {
    $failed = $_
}
finally {
    foreach ($name in $saved.Keys) { Set-Item "env:$name" $saved[$name] }
    if ((Test-Health) -and (Test-Path -LiteralPath (Join-Path $root 'app\scripts\tow.cmd'))) {
        & (Join-Path $root 'app\scripts\tow.cmd') stop | Out-Null
    }
    if ($failed) {
        $log = Join-Path $root 'data\logs\run.log'
        if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Tail 40 | Write-Host }
    }
    if (-not $Keep) {
        for ($i = 0; $i -lt 10 -and (Test-Path -LiteralPath $folder); $i++) {
            Remove-Item -LiteralPath $folder -Recurse -Force -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $folder) { Start-Sleep -Seconds 1 }
        }
    }
}
if ($failed) { throw $failed }
