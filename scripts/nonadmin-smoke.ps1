#Requires -Version 5.1
<#
.SYNOPSIS
    TC-15: the Windows bundle installed, started and read by a standard (non-administrator) account
    whose console is in code page 866.

.DESCRIPTION
    Run elevated on a disposable CI runner (never on an owner's computer): it creates a local
    standard account with a random password that is never printed, lets it read the bundle, and runs
    this script again as that account (-Inner, Start-Process -Credential). As that account:

    1. identity: the process's own SID is the created account's, and Administrators
       (S-1-5-32-544) is not among its enabled groups (WindowsPrincipal.IsInRole by SID);
    2. the bundle is unpacked into a folder of the account with a Cyrillic name (made from code
       points: this file stays ASCII) and set up offline, TOW starts;
    3. under `chcp 866`, `tow.cmd keys status --json` read through cmd.exe's pipe parses as JSON and
       its key path is the Cyrillic folder, decoded (no "?", no mojibake); `tow.cmd status`'s human
       output decodes from cp866;
    4. the folders are private to that account (scripts/permissions-smoke.py with the install's
       own Python);
    5. TOW stops.
    The result goes to a JSON file the outer run reads and checks; the account is removed at the end.

.EXAMPLE
    pwsh scripts/nonadmin-smoke.ps1 -Zip dist/TOW-windows-x64.zip -Port 18962
#>
param(
    [Parameter(Mandatory = $true)][string]$Zip,
    [int]$Port = 18962,
    [switch]$Inner,
    [string]$Result = ''
)

$ErrorActionPreference = 'Stop'
if ($Port -eq 8787) { throw 'nonadmin-smoke: never on 8787 (a real TOW may run there)' }
$AdministratorsSid = 'S-1-5-32-544'
$UsersSid = 'S-1-5-32-545'
$AccountName = 'towsmoke'

function Say([string]$Text) { Write-Host "nonadmin-smoke: $Text" }

if ($Inner) {
    $report = [ordered]@{ ok = $false; steps = @() }
    try {
        # 1. Who runs this.
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        $report.sid = $identity.User.Value
        $report.admin = $principal.IsInRole([Security.Principal.SecurityIdentifier]$AdministratorsSid)
        $report.steps += "identity $($report.sid), administrator: $($report.admin)"

        # The account's own folders, from its token (a process started with -Credential may carry
        # the starting account's environment, whose folders this account cannot open).
        $local = [Environment]::GetFolderPath('LocalApplicationData')
        $env:LOCALAPPDATA = $local
        $env:APPDATA = [Environment]::GetFolderPath('ApplicationData')
        $env:USERPROFILE = [Environment]::GetFolderPath('UserProfile')
        $env:TEMP = (New-Item -ItemType Directory -Force -Path (Join-Path $local 'Temp')).FullName
        $env:TMP = $env:TEMP
        $report.profile = $env:USERPROFILE

        # 2. Unpack into a Cyrillic folder of this account, set up offline, start.
        $cyrillic = -join [char[]](0x0422, 0x0435, 0x0441, 0x0442)
        $base = Join-Path $local ("TOW nonadmin $cyrillic")
        if (Test-Path -LiteralPath $base) { Remove-Item -LiteralPath $base -Recurse -Force }
        New-Item -ItemType Directory -Path $base | Out-Null
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($Zip, $base)
        $root = Join-Path $base 'TOW'
        $report.root = $root
        $config = Join-Path $root 'config.yaml'
        $text = [IO.File]::ReadAllText($config) -replace '(?m)^port:.*$', "port: $Port"
        [IO.File]::WriteAllText($config, $text, (New-Object Text.UTF8Encoding($false)))
        Get-ChildItem env: | Where-Object { $_.Name -like 'UV_*' -or $_.Name -like 'TOW_*' } | ForEach-Object { Remove-Item "env:$($_.Name)" }
        $env:UV_OFFLINE = '1'
        $env:TOW_NO_BROWSER = '1'
        $launcher = Join-Path $root 'app\scripts\tow.cmd'
        & cmd.exe /d /c call "`"$launcher`"" setup
        if ($LASTEXITCODE) { throw "tow setup exited with $LASTEXITCODE" }
        & cmd.exe /d /c call "`"$launcher`"" start
        if ($LASTEXITCODE) { throw "tow start exited with $LASTEXITCODE" }
        $report.steps += 'unpacked, set up offline and started by this account'

        # 3. cp866: machine JSON through cmd's pipe, and the human text decoded from cp866.
        $json = Join-Path $base 'keys.json'
        $human = Join-Path $base 'status.txt'
        & cmd.exe /d /c "chcp 866 >nul & call `"$launcher`" keys status --json > `"$json`""
        if ($LASTEXITCODE) { throw "tow keys status --json under chcp 866 exited with $LASTEXITCODE" }
        $bytes = [IO.File]::ReadAllBytes($json)
        $report.json_ascii = -not ($bytes | Where-Object { $_ -gt 127 })
        $keys = [Text.Encoding]::ASCII.GetString($bytes) | ConvertFrom-Json
        $paths = @($keys.PSObject.Properties | ForEach-Object { $_.Value } | Where-Object { $_ -is [string] -and $_ -like '*master.key' })
        $report.key_paths = $paths
        $expected = Join-Path $root 'keys\master.key'
        $report.key_path_decoded = [bool]($paths | Where-Object { $_ -eq $expected })
        & cmd.exe /d /c "chcp 866 >nul & call `"$launcher`" status > `"$human`" 2>&1"
        if ($LASTEXITCODE) { throw "tow status under chcp 866 exited with $LASTEXITCODE" }
        $report.status_text = [Text.Encoding]::GetEncoding(866).GetString([IO.File]::ReadAllBytes($human)).Trim()
        $report.steps += 'chcp 866: JSON parsed, human status read'

        # 4. Private to this account (the install's own Python runs the stdlib-only check).
        $python = Get-ChildItem -LiteralPath (Join-Path $root 'runtime\python') -Directory -Filter 'cpython-3*' |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'python.exe') } | Select-Object -First 1
        & (Join-Path $python.FullName 'python.exe') (Join-Path $PSScriptRoot 'permissions-smoke.py') --root $root
        $report.private = ($LASTEXITCODE -eq 0)

        # 5. Stop.
        & cmd.exe /d /c call "`"$launcher`"" stop
        $report.stopped = ($LASTEXITCODE -eq 0)
        $report.ok = $true
    } catch {
        $report.error = "$_"
        try { & cmd.exe /d /c call "`"$(Join-Path $report.root 'app\scripts\tow.cmd')`"" stop } catch { }
    }
    $report | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $Result -Encoding UTF8
    exit 0
}

# --- the outer run: elevated, on a disposable runner --------------------------------------------
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.SecurityIdentifier]$AdministratorsSid)) { throw 'nonadmin-smoke: run it elevated (it creates an account)' }
if (-not $env:GITHUB_ACTIONS) { throw 'nonadmin-smoke: CI runners only (it creates a local account)' }

$shared = Join-Path $env:SystemDrive 'nonadmin-smoke'
New-Item -ItemType Directory -Force -Path $shared | Out-Null
Copy-Item -LiteralPath (Resolve-Path $Zip).Path -Destination (Join-Path $shared 'TOW-windows-x64.zip')
foreach ($name in 'nonadmin-smoke.ps1', 'permissions-smoke.py') {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $shared $name)
}
$resultFile = Join-Path $shared 'result.json'

# A random password, never printed: 24 bytes of the system's generator, plus classes the policy wants.
$random = New-Object byte[] 24
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($random)
$plain = [Convert]::ToBase64String($random) + 'aA1!'
if ($env:GITHUB_ACTIONS) { Write-Host "::add-mask::$plain" }
$secure = ConvertTo-SecureString $plain -AsPlainText -Force
$plain = $null
$account = New-LocalUser -Name $AccountName -Password $secure -PasswordNeverExpires -AccountNeverExpires -Description 'TOW CI smoke'
try {
    Add-LocalGroupMember -SID $UsersSid -Member $AccountName
    $sid = $account.SID.Value
    Say "created the standard account $AccountName ($sid)"
    & icacls $shared /grant "*${sid}:(OI)(CI)M" /Q | Out-Null
    if ($LASTEXITCODE) { throw "icacls exited with $LASTEXITCODE" }
    $credential = New-Object Management.Automation.PSCredential(".\$AccountName", $secure)
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$(Join-Path $shared 'nonadmin-smoke.ps1')`"",
        '-Inner', '-Zip', "`"$(Join-Path $shared 'TOW-windows-x64.zip')`"", '-Port', "$Port", '-Result', "`"$resultFile`"")
    $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -Credential $credential -LoadUserProfile `
        -WorkingDirectory $shared -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $shared 'inner.out') -RedirectStandardError (Join-Path $shared 'inner.err')
    if (-not $process.WaitForExit(1200 * 1000)) {
        Stop-Process -Id $process.Id -Force  # the process this script started
        throw 'nonadmin-smoke: the run as the standard account did not end within 20 minutes'
    }
    Get-Content -LiteralPath (Join-Path $shared 'inner.out') -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  | $_" }
    Get-Content -LiteralPath (Join-Path $shared 'inner.err') -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  ! $_" }
    if (-not (Test-Path -LiteralPath $resultFile)) { throw 'nonadmin-smoke: the standard account wrote no result' }
    $report = Get-Content -LiteralPath $resultFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $report | ConvertTo-Json -Depth 4 | Write-Host
    $failed = @()
    if (-not $report.ok) { $failed += "the run failed: $($report.error)" }
    if ($report.sid -ne $sid) { $failed += "it ran as $($report.sid), not as $AccountName ($sid)" }
    if ($report.admin -ne $false) { $failed += 'it ran with the Administrators group' }
    if (-not $report.json_ascii) { $failed += 'the --json output is not ASCII' }
    if (-not $report.key_path_decoded) { $failed += "the key path is not the Cyrillic folder: $($report.key_paths -join ', ')" }
    if (-not $report.status_text -or $report.status_text.Contains([string][char]0xFFFD)) {
        $failed += "tow status under chcp 866 does not decode: $($report.status_text)"
    }
    if (-not $report.private) { $failed += 'the folders are not private to the account (above)' }
    if (-not $report.stopped) { $failed += 'tow stop failed' }
    if ($failed) { throw ('nonadmin-smoke: FAILED: ' + ($failed -join '; ')) }
    Say 'passed: a standard account installed, started, read (cp866) and stopped TOW'
} finally {
    Remove-LocalUser -Name $AccountName -ErrorAction SilentlyContinue
}
exit 0
