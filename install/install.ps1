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
    [switch]$Purge,
    [switch]$AdoptData
)

function Install-Tow {
    param([string]$Dir, [string]$Version, [int]$Port, [bool]$Autostart, [bool]$Uninstall, [bool]$Yes, [bool]$Purge, [bool]$AdoptData)

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
    $marker = Join-Path $Dir '.tow-install'
    foreach ($path in @($Dir, (Join-Path $Dir 'app'), $launcher, $marker)) {
        if (Test-Path -LiteralPath $path) {
            if ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "TOW: $path is a link; give the real installation folder"
            }
        }
    }
    $marked = (Test-Path -LiteralPath $marker -PathType Leaf) -and
        ([IO.File]::ReadAllText($marker).Trim() -eq 'TOW portable install v1')
    $project = Join-Path $Dir 'app\pyproject.toml'
    $legacy = (Test-Path -LiteralPath $launcher -PathType Leaf) -and
        -not (Test-Path -LiteralPath (Join-Path $Dir 'app\.git')) -and
        (Test-Path -LiteralPath $project -PathType Leaf) -and
        ([IO.File]::ReadAllText($project) -match '(?m)^name = "tow"\s*$')

    if ($Uninstall) {
        if (-not $marked -and -not $legacy -and -not $AdoptData) { throw "TOW: no TOW install in $Dir (give its folder with -Dir)" }
        if (-not $marked -and -not $legacy -and $AdoptData -and
            (-not (Test-Path -LiteralPath (Join-Path $Dir 'config.yaml') -PathType Leaf) -or
             -not (Test-Path -LiteralPath (Join-Path $Dir 'keys\master.key') -PathType Leaf) -or
             -not (Test-Path -LiteralPath (Join-Path $Dir 'data') -PathType Container))) {
            throw "TOW: cannot adopt $Dir; old settings, master key and data folder are required"
        }
        Say "this removes TOW from $Dir"
        if (Test-Path -LiteralPath (Join-Path $Dir 'keys\master.key')) {
            Say "your master key is $Dir\keys\master.key: keep a copy if you may restore a backup or a .towx file later"
        }
        if (-not $Yes -and -not (Ask "Remove TOW from ${Dir}? [y/N]" $false)) { Say 'nothing was removed'; return }
        $keep = -not $Purge
        if (-not $Purge -and -not $Yes) {
            $keep = Ask "Keep your data, keys, settings and backups (data, keys, config.yaml, backup) in ${Dir}? [Y/n]" $true
        }
        if ($keep -and -not $marked) { [IO.File]::WriteAllText($marker, "TOW portable install v1`n") }
        # Native programs' error output is not a PowerShell error here (Windows PowerShell 5.1).
        $ErrorActionPreference = 'Continue'
        if ($legacy) {
            & $launcher autostart off *> $null
            & $launcher stop
        }
        $ErrorActionPreference = 'Stop'
        $kept = @('data', 'keys', 'config.yaml', 'backup', '.tow-install')
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
    $keptNames = @('data', 'keys', 'config.yaml', 'backup', '.tow-install')
    $present = @(if (Test-Path -LiteralPath $Dir) { Get-ChildItem -LiteralPath $Dir -Force | ForEach-Object { $_.Name } })
    $kept = @($present | Where-Object { $keptNames -contains $_ }).Count -gt 0
    if (@($present | Where-Object { $keptNames -notcontains $_ }).Count) {
        throw "TOW: $Dir is not empty: choose another folder with -Dir"
    }
    if ($kept -and -not $marked -and -not $AdoptData) {
        throw "TOW: $Dir has data without a TOW install marker; use -AdoptData only for your old TOW folder"
    }
    if ($kept) { Say "found the data of an earlier TOW in $Dir ($($present -join ', ')): it is kept" }
    if ($env:PROCESSOR_ARCHITECTURE -eq 'x86' -and -not $env:PROCESSOR_ARCHITEW6432) {
        throw 'TOW: TOW needs 64-bit Windows'
    }
    # docs/PORTABLE.md 3: without Windows long paths, a folder path over 110 characters lets files
    # deep in app\.venv and runtime\ pass 260 characters, and the first start fails inside uv or Python.
    $longPaths = (Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -ErrorAction SilentlyContinue).LongPathsEnabled
    if ($Dir.Length -gt 110 -and $longPaths -ne 1) {
        throw "TOW: the folder path $Dir is longer than 110 characters and Windows long paths are off, so TOW could not be prepared there. Choose a shorter folder with -Dir (for example C:\TOW), or turn long paths on (LongPathsEnabled)."
    }

    # Folders for this account, SYSTEM and Administrators only: in a drive root (D:\TOW) the
    # install would inherit "Authenticated Users: modify", and every account of the PC could
    # change the program TOW runs (app, runtime, the start files) and read the master key. Only
    # a folder this run creates or this account owns is changed (never through a link): another
    # account's keeps its permissions. TOW itself checks again at every start, but only after
    # the code in the folder has run - so the folder is closed here, before anything is in it.
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $icacls = Join-Path $env:SystemRoot 'System32\icacls.exe'
    function Close-Folder([string]$Folder, [bool]$New) {
        try {
            $item = Get-Item -LiteralPath $Folder -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
            $owner = (Get-Acl -LiteralPath $Folder).GetOwner([Security.Principal.SecurityIdentifier]).Value
        }
        catch { return $false }
        if (-not $New -and $owner -ne $sid) { return $false }
        $ErrorActionPreference = 'Continue'
        & $icacls $Folder /inheritance:r /grant:r "*${sid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /Q | Out-Null
        return $LASTEXITCODE -eq 0
    }

    $created = -not (Test-Path -LiteralPath $Dir)
    $work = Join-Path $Dir '.install'
    $installed = $false
    $rootClosed = $false
    $moved = New-Object System.Collections.Generic.List[string]
    $oldConfig = if (Test-Path -LiteralPath (Join-Path $Dir 'config.yaml') -PathType Leaf) {
        [IO.File]::ReadAllBytes((Join-Path $Dir 'config.yaml'))
    } else { $null }
    try {
        New-Item -ItemType Directory -Force -Path $Dir | Out-Null
        $rootClosed = Close-Folder $Dir $created
        if (-not $rootClosed) { Say "could not limit $Dir to your account (is it another account's folder?); its keys and data folders are limited instead" }
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
        if (-not $marked) {
            [IO.File]::WriteAllText($marker, "TOW portable install v1`n")
            $moved.Add($marker)
        }
        $installed = $true
    }
    finally {
        if (-not $installed) {
            if ($null -ne $oldConfig -and (Test-Path -LiteralPath $Dir)) {
                [IO.File]::WriteAllBytes((Join-Path $Dir 'config.yaml'), $oldConfig)
            }
            if ($created) { Remove-Item -LiteralPath $Dir -Recurse -Force -ErrorAction SilentlyContinue }
            elseif ($kept) {
                # Only what this installer put there: the earlier TOW's data stays as it was.
                foreach ($path in @($moved) + @($work)) { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue }
            }
            else { Get-ChildItem -LiteralPath $Dir -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue }
        }
    }

    # keys\ and data\ inherit from a closed folder; when it stays open they are closed on their own.
    foreach ($name in @('keys', 'data')) {
        $folder = Join-Path $Dir $name
        $new = -not (Test-Path -LiteralPath $folder)
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        if ($rootClosed) { continue }
        if (-not (Close-Folder $folder $new)) { Say "could not limit $folder to your account (TOW tries again when it starts)" }
    }

    Say "installed in $Dir; starting it (the first start takes a minute)"
    # The keyboard stays with this window: the start file's "press a key" returns at once.
    '' | & (Join-Path $Dir 'Start TOW.cmd')
    if ($LASTEXITCODE -ne 0) { throw "TOW: TOW is installed in $Dir but did not start: the reason is above" }
    if ($Autostart) {
        & $launcher autostart on
        if ($LASTEXITCODE -ne 0) { Say 'autostart is not on (the reason is above)' }
    }
    # The port of config.yaml: a reinstall around kept data keeps its own (8787 is only the default).
    $shown = $Port
    if (-not $shown) {
        $shown = 8787
        $configured = [regex]::Match([IO.File]::ReadAllText((Join-Path $Dir 'config.yaml')), '(?m)^port:\s*(\d+)\s*$')
        if ($configured.Success) { $shown = [int]$configured.Groups[1].Value }
    }
    $page = "http://127.0.0.1:$shown"
    Write-Host ''
    Write-Host "TOW is installed in $Dir and running: $page"
    Write-Host "  Start:     double-click `"Start TOW.cmd`" in $Dir"
    Write-Host "  Stop:      `"Stop TOW.cmd`"    Update: `"Update TOW.cmd`""
    # `& ` first: PowerShell runs a quoted path only as a command.
    if (-not $Autostart) { Write-Host "  Autostart: & `"$launcher`" autostart on" }
    Write-Host "  Back up:   $Dir\keys\master.key (it opens your saved passwords)"
    Write-Host "  Remove:    & ([scriptblock]::Create((irm https://github.com/$repo/releases/latest/download/install.ps1))) -Uninstall -Dir `"$Dir`""
}

try {
    Install-Tow -Dir $Dir -Version $Version -Port $Port -Autostart $Autostart.IsPresent -Uninstall $Uninstall.IsPresent -Yes $Yes.IsPresent -Purge $Purge.IsPresent -AdoptData $AdoptData.IsPresent
}
catch {
    # A refusal is a sentence, not a PowerShell error record with its script position.
    $host.UI.WriteErrorLine($_.Exception.Message)
    # Run as a file its exit code says so; under `irm | iex` an exit would close the window.
    if ($PSCommandPath) { exit 1 }
}
