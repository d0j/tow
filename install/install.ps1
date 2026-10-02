<#
.SYNOPSIS
    Install TOW on Windows into one folder (default $HOME\TOW) and start it.

.DESCRIPTION
    irm https://github.com/d0j/tow/releases/latest/download/install.ps1 | iex

    With options:
    & ([scriptblock]::Create((irm https://github.com/d0j/tow/releases/latest/download/install.ps1))) -Dir D:\TOW -Autostart

    Downloads TOW-windows-x64.zip of the latest release (or -Version), checks it against the
    release's SHA256SUMS, unpacks it into -Dir (or $env:TOW_INSTALL_DIR) and runs "Start TOW.cmd"
    there: the first start prepares TOW inside the folder without the internet and opens
    http://127.0.0.1:8787. Nothing is written outside the folder (-Autostart adds the one Task
    Scheduler task "TOW", on request). An existing install is never overwritten: it is updated
    with "Update TOW.cmd". -Uninstall removes TOW after asking (data, keys, settings and backups
    stay unless -Purge). Windows PowerShell 5.1 and PowerShell 7.

    For tests: $env:TOW_INSTALL_SOURCE (a local zip) and $env:TOW_INSTALL_SUMS (its SHA256SUMS);
    $env:TOW_NO_BROWSER=1 opens no browser.
#>
param(
    [string]$Dir = $(if ($env:TOW_INSTALL_DIR) { $env:TOW_INSTALL_DIR } else { Join-Path $HOME 'TOW' }),
    [string]$Version = $env:TOW_VERSION,
    [int]$Port = $(if ($env:TOW_INSTALL_PORT) { [int]$env:TOW_INSTALL_PORT } else { 0 }),
    [switch]$Autostart,
    [switch]$Uninstall,
    [switch]$Yes,
    [switch]$Purge
)

function Install-Tow {
    param([string]$Dir, [string]$Version, [int]$Port, [bool]$Autostart, [bool]$Uninstall, [bool]$Yes, [bool]$Purge)

    # Everything stays inside this function: `irm | iex` runs in the caller's session, which
    # must keep its own settings (and an `exit` would close its window).
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'  # Windows PowerShell downloads slowly with a progress bar
    $repo = 'd0j/tow'
    $asset = 'TOW-windows-x64.zip'

    function Say([string]$Text) { Write-Host "TOW: $Text" }
    function Ask([string]$Question, [bool]$Default) {
        if ($Yes) { return $Default }
        $answer = "$(Read-Host $Question)".Trim().ToLowerInvariant()
        # y or n; also the Russian d(a) and n(et). The file stays ASCII: irm reads it in any code page.
        if ($answer.StartsWith('y') -or $answer.StartsWith([string][char]0x0434)) { return $true }
        if ($answer.StartsWith('n') -or $answer.StartsWith([string][char]0x043D)) { return $false }
        return $Default
    }

    $Dir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Dir)
    $launcher = Join-Path $Dir 'app\scripts\tow.cmd'

    if ($Uninstall) {
        $hasLauncher = Test-Path -LiteralPath $launcher
        $remnants = @('data', 'keys', 'config.yaml', 'backup') | Where-Object { Test-Path -LiteralPath (Join-Path $Dir $_) }
        if (-not $hasLauncher -and -not $remnants) { throw "TOW: no TOW install in $Dir (give its folder with -Dir)" }
        Say "this removes TOW from $Dir"
        if (Test-Path -LiteralPath (Join-Path $Dir 'keys\master.key')) {
            Say "your master key is $Dir\keys\master.key: keep a copy if you may restore a backup or a .towx file later"
        }
        if (-not $Yes -and -not (Ask "Remove TOW from ${Dir}? [y/N]" $false)) { Say 'nothing was removed'; return }
        $keep = -not $Purge
        if (-not $Purge -and -not $Yes) {
            $keep = Ask "Keep your data, keys, settings and backups (data, keys, config.yaml, backup) in ${Dir}? [Y/n]" $true
        }
        # Native programs' error output is not a PowerShell error here (Windows PowerShell 5.1).
        $ErrorActionPreference = 'Continue'
        if ($hasLauncher) {
            & $launcher autostart off *> $null
            & $launcher stop
        }
        $ErrorActionPreference = 'Stop'
        $kept = @('data', 'keys', 'config.yaml', 'backup')
        foreach ($item in @(Get-ChildItem -LiteralPath $Dir -Force)) {
            if ($keep -and $kept -contains $item.Name) { continue }
            Remove-Item -LiteralPath $item.FullName -Recurse -Force
        }
        if ($keep) { Say "TOW was removed; data, keys, config.yaml and backup are still in $Dir" }
        else {
            Remove-Item -LiteralPath $Dir -Recurse -Force
            Say "TOW was removed with all its data ($Dir)"
        }
        return
    }

    if (Test-Path -LiteralPath (Join-Path $Dir 'app')) {
        throw "TOW: TOW is already installed in $Dir. To update it, double-click `"Update TOW.cmd`" there."
    }
    # What an uninstall that kept the data leaves: TOW is installed again around it, nothing of it changes.
    $keptNames = @('data', 'keys', 'config.yaml', 'backup')
    $present = @(if (Test-Path -LiteralPath $Dir) { Get-ChildItem -LiteralPath $Dir -Force | ForEach-Object { $_.Name } })
    $kept = @($present | Where-Object { $keptNames -contains $_ }).Count -gt 0
    if (@($present | Where-Object { $keptNames -notcontains $_ }).Count) {
        throw "TOW: $Dir is not empty: choose another folder with -Dir"
    }
    if ($kept) { Say "found the data of an earlier TOW in $Dir ($($present -join ', ')): it is kept" }
    if ($env:PROCESSOR_ARCHITECTURE -eq 'x86' -and -not $env:PROCESSOR_ARCHITEW6432) {
        throw 'TOW: TOW needs 64-bit Windows'
    }

    $created = -not (Test-Path -LiteralPath $Dir)
    $work = Join-Path $Dir '.install'
    $installed = $false
    $moved = New-Object System.Collections.Generic.List[string]
    try {
        New-Item -ItemType Directory -Force -Path $work | Out-Null
        if ($env:TOW_INSTALL_SOURCE) {
            $zip = (Resolve-Path -LiteralPath $env:TOW_INSTALL_SOURCE).Path
            $sums = if ($env:TOW_INSTALL_SUMS) { (Resolve-Path -LiteralPath $env:TOW_INSTALL_SUMS).Path } else { $null }
            if (-not $sums) { Say 'a local zip without TOW_INSTALL_SUMS: not checked' }
        }
        else {
            # TLS 1.2 for GitHub on Windows PowerShell 5.1.
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            $base = if ($Version) { "https://github.com/$repo/releases/download/$Version" } else { "https://github.com/$repo/releases/latest/download" }
            $zip = Join-Path $work $asset
            $sums = Join-Path $work 'SHA256SUMS'
            Say "downloading $base/$asset"
            Invoke-WebRequest -UseBasicParsing -Uri "$base/SHA256SUMS" -OutFile $sums
            Invoke-WebRequest -UseBasicParsing -Uri "$base/$asset" -OutFile $zip
        }
        if ($sums) {
            $line = Get-Content -LiteralPath $sums | Where-Object { $_ -match "^([0-9a-fA-F]{64})\s+\*?$([regex]::Escape($asset))\s*$" } | Select-Object -First 1
            if (-not $line) { throw "TOW: SHA256SUMS lists no $asset" }
            $expected = ($line -split '\s+')[0].ToLowerInvariant()
            $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actual -ne $expected) { throw "TOW: $asset does not match the release's SHA256SUMS" }
            Say 'the download matches the release''s SHA256SUMS'
        }

        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $unpacked = Join-Path $work 'unpacked'
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, $unpacked)
        $top = Join-Path $unpacked 'TOW'
        if (-not (Test-Path -LiteralPath (Join-Path $top 'Start TOW.cmd'))) { throw "TOW: $asset holds no TOW\Start TOW.cmd" }
        foreach ($item in @(Get-ChildItem -LiteralPath $top -Force)) {
            $target = Join-Path $Dir $item.Name
            if (Test-Path -LiteralPath $target) { continue }  # the settings of an earlier TOW stay
            Move-Item -LiteralPath $item.FullName -Destination $target
            $moved.Add($target)
        }
        Remove-Item -LiteralPath $work -Recurse -Force
        if ($Port) {
            $config = Join-Path $Dir 'config.yaml'
            $text = [IO.File]::ReadAllText($config) -replace '(?m)^port:.*$', "port: $Port"
            [IO.File]::WriteAllText($config, $text, (New-Object Text.UTF8Encoding $false))
        }
        $installed = $true
    }
    finally {
        if (-not $installed) {
            if ($created) { Remove-Item -LiteralPath $Dir -Recurse -Force -ErrorAction SilentlyContinue }
            elseif ($kept) {
                # Only what this installer put there: the earlier TOW's data stays as it was.
                foreach ($path in @($moved) + @($work)) { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue }
            }
            else { Get-ChildItem -LiteralPath $Dir -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue }
        }
    }

    Say "installed in $Dir; starting it (the first start takes a minute)"
    # The keyboard stays with this window: the start file's "press a key" returns at once.
    '' | & (Join-Path $Dir 'Start TOW.cmd')
    if ($LASTEXITCODE -ne 0) { throw "TOW: TOW is installed in $Dir but did not start: the reason is above" }
    if ($Autostart) {
        & $launcher autostart on
        if ($LASTEXITCODE -ne 0) { Say 'autostart is not on (the reason is above)' }
    }
    $page = "http://127.0.0.1:$(if ($Port) { $Port } else { 8787 })"
    Write-Host ''
    Write-Host "TOW is installed in $Dir and running: $page"
    Write-Host "  Start:     double-click `"Start TOW.cmd`" in $Dir"
    Write-Host "  Stop:      `"Stop TOW.cmd`"    Update: `"Update TOW.cmd`""
    if (-not $Autostart) { Write-Host "  Autostart: `"$launcher`" autostart on" }
    Write-Host "  Back up:   $Dir\keys\master.key (it opens your saved passwords)"
    Write-Host "  Remove:    & ([scriptblock]::Create((irm https://github.com/$repo/releases/latest/download/install.ps1))) -Uninstall -Dir `"$Dir`""
}

Install-Tow -Dir $Dir -Version $Version -Port $Port -Autostart $Autostart.IsPresent -Uninstall $Uninstall.IsPresent -Yes $Yes.IsPresent -Purge $Purge.IsPresent
