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
    profile folders) are compared before and after. At the top of the profile folders only names
    TOW, uv or Python could make count (uv, python, pip, tow, the test folder's name); other
    programs of a CI runner come and go there, and what was ignored is printed. TEMP and TMP of
    every TOW process point at an empty folder of the test, which must still be empty at the end.

    The Windows folder limit (docs/PORTABLE.md 3) is read from the bundle's own tow-start.cmd: the
    deepest program or library of the prepared folder must load from a folder of that length
    (a full path of at most 250 characters). -RootLength makes the install folder exactly that long;
    with -ExpectTooLong the start file must refuse it before anything of the folder runs.

.EXAMPLE
    pwsh scripts/bundle-smoke.ps1 -Zip dist/TOW-windows-x64.zip -Offline
#>
param(
    [Parameter(Mandatory = $true)][string]$Zip,
    [int]$Port = 18877,
    [string]$Base = $(if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }),
    [switch]$Offline,
    [switch]$Move,
    [switch]$Keep,
    [int]$RootLength = 0,
    [switch]$ExpectTooLong
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

# Entries at the top of the profile folders that TOW, uv or Python could make: other programs of a
# CI runner (a browser's Mozilla folder once) come and go there during the smoke on their own.
function Test-Ours([string]$Name) {
    return $Name -match '(?i)uv|python|pip|tow' -or $Name.Contains($installName, [StringComparison]::OrdinalIgnoreCase)
}

# The places that changed between two Get-Outside states; entries of the profile folders that are
# not ours are printed as runner noise and not counted.
function Compare-Outside($Before, $After) {
    $changed = @()
    foreach ($place in $Before.Keys) {
        if ("$($Before[$place])" -eq "$($After[$place])") { continue }
        if ($place -like 'entries of *') {
            $old = @("$($Before[$place])" -split '\|' | Where-Object { $_ })
            $new = @("$($After[$place])" -split '\|' | Where-Object { $_ })
            $diff = @($new | Where-Object { $old -notcontains $_ } | ForEach-Object { "+$_" }) +
                @($old | Where-Object { $new -notcontains $_ } | ForEach-Object { "-$_" })
            $noise = @($diff | Where-Object { -not (Test-Ours $_.Substring(1)) })
            if ($noise.Count) { Write-Host "  ignored as runner noise in $($place.Substring(11)): $($noise -join ', ')" }
            if ($noise.Count -eq $diff.Count) { continue }
        }
        $changed += $place
    }
    return , $changed
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
if ($RootLength) {
    $pad = $RootLength - $folder.Length - '\TOW'.Length
    if ($pad -lt 0 -or $pad -gt 200) { throw "bundle-smoke: cannot make a $RootLength-character folder under $Base" }
    $folder += 'L' * $pad
}
$root = Join-Path $folder 'TOW'
if ($RootLength -and $root.Length -ne $RootLength) { throw "bundle-smoke: the folder is $($root.Length) characters, not $RootLength" }
# TEMP and TMP of the TOW processes only (restored at the end): nothing may be left there.
$sentinel = Join-Path $folder 'system-temp'
$installName = Split-Path -Leaf $folder
$saved = @{ TEMP = $env:TEMP; TMP = $env:TMP; TOW_NO_BROWSER = $env:TOW_NO_BROWSER; HTTP_PROXY = $env:HTTP_PROXY; HTTPS_PROXY = $env:HTTPS_PROXY; ALL_PROXY = $env:ALL_PROXY; NO_PROXY = $env:NO_PROXY }
# The bundle runs as on a new computer: no TOW or uv variables of this machine (a developer's
# machine may name its own install's master key in TOW_MASTER_KEY_FILE; setup-uv sets UV_*).
foreach ($name in @(Get-ChildItem env: | Where-Object { $_.Name -like 'TOW_*' -or $_.Name -like 'UV_*' } | ForEach-Object Name)) {
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
    $startText = Get-Content -LiteralPath (Join-Path $root 'app\scripts\tow-start.cmd') -Raw
    $limit = [int][regex]::Match($startText, 'TOW_ROOT:~(\d+),1').Groups[1].Value
    if (-not $limit) { throw 'bundle-smoke: no folder limit in app\scripts\tow-start.cmd' }
    # Without Windows long paths a longer folder than this gets the start file's warning (docs/PORTABLE.md 3).
    $warnAt = [int][regex]::Match($startText, 'TOW_ROOT:~(\d+),1%"=="" set "TOW_LONG=1"').Groups[1].Value
    if (-not $warnAt) { throw 'bundle-smoke: no long-path warning length in app\scripts\tow-start.cmd' }
    $longPathsOn = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -ErrorAction SilentlyContinue).LongPathsEnabled -eq 1
    $expectWarning = (-not $longPathsOn) -and $root.Length -gt $warnAt
    Step "the install folder has $($root.Length) characters; the limit is $limit (without long paths $warnAt; long paths on: $longPathsOn)"
    New-Item -ItemType Directory -Path $sentinel | Out-Null
    $env:TEMP = $sentinel; $env:TMP = $sentinel

    $env:TOW_NO_BROWSER = '1'
    if ($ExpectTooLong) {
        if ($root.Length -le $limit) { throw "bundle-smoke: -ExpectTooLong needs a folder over $limit characters" }
        Step 'a folder over the limit: Start TOW.cmd must refuse it before anything runs'
        $env:HTTP_PROXY = 'http://127.0.0.1:9'; $env:HTTPS_PROXY = 'http://127.0.0.1:9'; $env:ALL_PROXY = 'http://127.0.0.1:9'
        $output = '' | & (Join-Path $root 'Start TOW.cmd') 2>&1 | Out-String
        $code = $LASTEXITCODE
        Write-Host $output
        if ($code -ne 3 -or $output -notmatch "longer than $limit characters") { throw "bundle-smoke: no refusal of a long folder (exit $code)" }
        if ($output -match 'Preparing TOW|internet' -or (Test-Path -LiteralPath (Join-Path $root 'app\.venv'))) { throw 'bundle-smoke: the refused folder was prepared' }
        if (@(Get-ChildItem -LiteralPath $sentinel -Force).Count) { throw 'bundle-smoke: the refusal wrote into TEMP' }
        Step 'passed: refused before setup, nothing written'
        # exit, not return: a return ends this script with the refusal's exit code still in
        # $LASTEXITCODE (the finally below still runs and removes the folder).
        exit 0
    }
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
    if (($output -match 'Windows long paths are off') -ne $expectWarning) {
        throw "bundle-smoke: the long-path warning (expected: $expectWarning) was wrong for a $($root.Length)-character folder"
    }
    if ($code -ne 0) { throw "Start TOW.cmd exited with $code" }
    if ($output -match 'internet') { throw 'the first start needed the internet' }
    Step ("first start took {0:n0} s" -f $watch.Elapsed.TotalSeconds)
    foreach ($name in 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY') { Set-Item "env:$name" $saved[$name] }

    if (-not (Wait-Until { Test-Health } 30)) { throw "/healthz does not answer on $Port" }
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -NoProxy
    Step "healthz: ok=$($health.ok) version=$($health.version)"
    if (-not (Test-Path -LiteralPath (Join-Path $root 'keys\master.key'))) { throw 'no keys\master.key after the first start' }
    if (Test-Path -LiteralPath (Join-Path $root 'app\.venv\Scripts\python.exe')) { Step 'app\.venv was built' } else { throw 'no app\.venv' }
    # The deepest program or library TOW loads: uv, CPython's own programs, DLLs and modules (not
    # its test modules), and the environment (not uv's cache, templates or pip; links are not
    # followed). From a folder of $limit characters its full path must stay within 250: Windows
    # refuses 251 and more, long paths on or off.
    $loaded = '^(runtime\\bin\\|runtime\\python\\cpython-[^\\]+\\(DLLs\\)?[^\\]+$|app\\\.venv\\)'
    $binaries = @(Get-ChildItem -LiteralPath $root -Recurse -File -Force -Include *.exe, *.dll, *.pyd |
            ForEach-Object { $_.FullName.Substring($root.Length + 1) } |
            Where-Object { $_ -match $loaded -and $_ -notmatch '\\_test[^\\]*\.pyd$' } |
            Sort-Object Length -Descending)
    Step "deepest program or library: $($binaries[0].Length) $($binaries[0]) (then $($binaries[1].Length) $($binaries[1]))"
    if ($limit + 1 + $binaries[0].Length -gt 250) { throw "bundle-smoke: $($binaries[0]) does not load from a folder of $limit characters: lower the limit" }

    $status = & (Join-Path $root 'app\scripts\tow.cmd') status --json | ConvertFrom-Json
    Step "tow status: running=$($status.running) version=$($status.version)"
    if (-not $status.running) { throw 'tow status says TOW is not running' }

    Step 'second start: it finds TOW running'
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $output = '' | & (Join-Path $root 'Start TOW.cmd') 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "the second Start TOW.cmd exited with $LASTEXITCODE`n$output" }
    Step ("second start took {0:n1} s" -f $watch.Elapsed.TotalSeconds)

    Step 'setup refuses to rebuild the environment under a running service'
    $venvConfig = Join-Path $root 'app/.venv/pyvenv.cfg'
    $venvBefore = (Get-FileHash -LiteralPath $venvConfig).Hash
    $output = & (Join-Path $root 'app/scripts/tow.cmd') setup 2>&1 | Out-String
    $code = $LASTEXITCODE
    if ($code -ne 3 -or $output -notmatch 'TOW is running') { throw 'bundle-smoke: running setup was not refused' }
    if ((Get-FileHash -LiteralPath $venvConfig).Hash -ne $venvBefore -or -not (Test-Health)) {
        throw 'bundle-smoke: running setup changed the environment or stopped the service'
    }

    Step 'Stop TOW.cmd'
    $output = '' | & (Join-Path $root 'Stop TOW.cmd') 2>&1 | Out-String
    Write-Host $output
    if (-not (Wait-Until { -not (Test-Health) } 60)) { throw 'TOW still answers after Stop TOW.cmd' }

    if ($Move) {
        $original = @{}
        foreach ($relative in 'config.yaml', 'keys/master.key') {
            $original[$relative] = (Get-FileHash -LiteralPath (Join-Path $root $relative)).Hash
        }
        # A folder of the same length for -RootLength ("TOW" with a Cyrillic T).
        $movedRoot = if ($RootLength) { Join-Path $folder ([string][char]0x0422 + 'OW') } else { Join-Path $folder "relocated $cyrillic TOW" }
        $owned = [IO.Path]::GetFullPath($folder).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
        foreach ($target in $root, $movedRoot) {
            if (-not [IO.Path]::GetFullPath($target).StartsWith($owned, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'bundle-smoke: move outside the owned test folder refused'
            }
        }
        if (Test-Path -LiteralPath $movedRoot) { throw 'bundle-smoke: move destination already exists' }
        Step 'moving the stopped install, including its existing absolute-path environment'
        Move-Item -LiteralPath $root -Destination $movedRoot
        $root = $movedRoot
        $output = & (Join-Path $root 'app\scripts\tow.cmd') status --json 2>&1 | Out-String
        if ($LASTEXITCODE -ne 3 -or $output -notmatch 'moved or copied') { throw "bundle-smoke: the moved install ran its old environment (exit $LASTEXITCODE)" }
        Step 'before setup the moved install refuses to run its old environment'
        if ($Offline) {
            $env:HTTP_PROXY = 'http://127.0.0.1:9'; $env:HTTPS_PROXY = 'http://127.0.0.1:9'; $env:ALL_PROXY = 'http://127.0.0.1:9'
            $env:NO_PROXY = ''
        }
        $output = '' | & (Join-Path $root 'Start TOW.cmd') 2>&1 | Out-String
        $code = $LASTEXITCODE
        Write-Host $output
        if ($code -ne 0 -or ($Offline -and $output -match 'internet')) {
            throw "bundle-smoke: relocated start failed, exit $code"
        }
        if (-not (Wait-Until { Test-Health } 30)) { throw 'bundle-smoke: moved install is unhealthy' }
        foreach ($relative in $original.Keys) {
            if ((Get-FileHash -LiteralPath (Join-Path $root $relative)).Hash -ne $original[$relative]) {
                throw "bundle-smoke: persistent file changed after move: $relative"
            }
        }
        $saved['TOW_MOVE_ROOT'] = $env:TOW_MOVE_ROOT
        $env:TOW_MOVE_ROOT = $root
        & (Join-Path $root 'app/.venv/Scripts/python.exe') -c 'import os, sys, tow; from pathlib import Path; root=Path(os.environ["TOW_MOVE_ROOT"]).resolve(); assert Path(sys.base_prefix).resolve().is_relative_to(root); assert Path(tow.__file__).resolve().is_relative_to(root)'
        if ($LASTEXITCODE) { throw 'bundle-smoke: moved Python or module belongs to the old location' }
        $output = '' | & (Join-Path $root 'Stop TOW.cmd') 2>&1 | Out-String
        $code = $LASTEXITCODE
        Write-Host $output
        if ($code -ne 0 -or -not (Wait-Until { -not (Test-Health) } 60)) { throw 'bundle-smoke: moved service did not stop' }
        foreach ($name in 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY') { Set-Item "env:$name" $saved[$name] }
        Step 'relocated install rebuilt offline, kept its key and config, started and stopped'
    }

    $left = @(Get-ChildItem -LiteralPath $sentinel -Force -Name)
    if ($left.Count) { throw "bundle-smoke: TOW wrote into the system TEMP: $($left -join ', ')" }
    $after = Get-Outside
    $changed = Compare-Outside $before $after
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
# A refusal this run expected (-ExpectTooLong) leaves its start file's exit code in
# $LASTEXITCODE, and a CI step in pwsh ends with that code: a passed smoke says so itself.
exit 0
