@echo off
rem uv's settings of a runtime install (docs/PORTABLE.md 1): runtime\uv.toml, which uv reads instead of
rem the account's and the system's uv.toml (UV_CONFIG_FILE, set by tow-env.cmd). Called before uv runs
rem (tow.cmd, tow-setup.cmd), with tow-env.cmd's TOW_ROOT and TOW_APP. The lines up to the marker are
rem TOW's: app\scripts\runtime-uv.toml, the [tool.uv] settings of app\pyproject.toml (the tests keep
rem them equal, and scripts\update.py writes the same); the owner's lines below the marker stay (all
rem of a file without one). Also when an older updater brought this code here and wrote no file.
rem PowerShell cmdlets only (Constrained Language Mode too); a temporary file, then a rename.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; $mark = '# --- your settings below ---'; $head = @(Get-Content -LiteralPath (Join-Path $env:TOW_APP 'scripts\runtime-uv.toml') -Encoding UTF8); $file = Join-Path $env:TOW_ROOT 'runtime\uv.toml'; $now = @(); if (Test-Path -LiteralPath $file -PathType Leaf) { $now = @(Get-Content -LiteralPath $file -Encoding UTF8) }; $own = $now; for ($k = 0; $k -lt $now.Count; $k++) { if ($now[$k].Trim() -eq $mark) { $own = @($now | Select-Object -Skip ($k + 1)); break } }; $want = @($head) + @($own); $same = $want.Count -eq $now.Count; for ($k = 0; $same -and $k -lt $want.Count; $k++) { if ($want[$k] -cne $now[$k]) { $same = $false } }; if ($same) { exit 0 }; $temp = Join-Path $env:TOW_ROOT 'runtime\.uv.toml.tmp'; Set-Content -LiteralPath $temp -Value $want -Encoding UTF8; Move-Item -LiteralPath $temp -Destination $file -Force; exit 0"
