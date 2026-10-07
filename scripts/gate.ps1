<#
.SYNOPSIS
    TOW quality gate: the checks every commit and release must pass.

.DESCRIPTION
    Runs from any location; works on the checkout that contains this script.
    Full gate: uv.lock, ruff format + check, mypy, compileall, whitespace of every tracked file,
    pytest on every core (pytest-xdist) in a random order (the seed is printed) with branch
    coverage and its threshold (pyproject [tool.coverage]), then the wheel smoke. On success it
    records the tree it passed on, so the pre-push hook lets exactly that content through
    without running it again.
    -Quick skips the test suite and the wheel smoke; it only collects the tests (every test
    module must import).
    -Staged checks what the next commit contains (the index), not the working tree: the
    staged files are exported to a temp directory and checked there (the pre-commit hook).
    The wheel smoke builds the wheel into a temp directory, checks it carries every
    template, asset and module, and (when the dependencies are available offline or
    online) installs it into a throwaway venv and runs `tow version`.
    -Audit also checks the locked runtime dependencies for known vulnerabilities (needs network).

.EXAMPLE
    pwsh scripts/gate.ps1
    pwsh scripts/gate.ps1 -Quick
    pwsh scripts/gate.ps1 -Staged
#>
param([switch]$Quick, [switch]$Staged, [switch]$Audit)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
# git's empty tree: a diff against it covers every tracked file, not only the uncommitted changes.
$EmptyTree = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
$Skipped = [System.Collections.Generic.List[string]]::new()
$PytestCov = 'pytest-cov>=7.1'

function Step([string]$Name, [scriptblock]$Command) {
    Write-Host "gate: $Name" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "gate failed: $Name" }
}

function Skip([string]$Name, [string]$Why) {
    Write-Host "gate: SKIPPED $Name - $Why" -ForegroundColor Yellow
    $Skipped.Add($Name)
}

# Runs a tool from the locked environment. In -Staged mode the tools come from the checkout's
# .venv (the snapshot directory has none, and must not get one).
function Invoke-Tool {
    if ($Staged) { uv run --frozen --no-sync @args } else { uv run --frozen @args }
}

# The tree object of the working tree as it is (tracked and untracked, not ignored): equal to
# the tree of a commit made from it with `git add -A`. A scratch index keeps the real one intact.
function Get-WorkingTree {
    $index = [IO.Path]::GetTempFileName()
    $saved = $env:GIT_INDEX_FILE
    try {
        Copy-Item -Force (git rev-parse --git-path index) $index
        $env:GIT_INDEX_FILE = $index
        git add -A 2>$null
        $tree = git write-tree
        if ($LASTEXITCODE -ne 0) { return $null }
        return $tree
    }
    finally {
        $env:GIT_INDEX_FILE = $saved
        Remove-Item -Force $index -ErrorAction SilentlyContinue
    }
}

function Save-PassedTree([string]$Tree) {
    $file = Join-Path (git rev-parse --git-common-dir) 'tow-gate-passed'
    $lines = @(if (Test-Path $file) { Get-Content $file }) + $Tree | Select-Object -Unique -Last 200
    Set-Content -Path $file -Value $lines -Encoding ascii
}

# Every package file the installed app needs at runtime, checked against the source tree,
# so a template, asset or module left out of the wheel fails here and not after a deploy.
# (A file, not a pipe: some hosts do not pass stdin to python, which then waits at its prompt.)

function Test-Wheel {
    $dir = Join-Path ([IO.Path]::GetTempPath()) ("tow-wheel-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    try {
        Step 'wheel build' { uv build --wheel --quiet -o $dir }
        $wheel = Get-ChildItem $dir -Filter 'tow-*.whl' | Select-Object -First 1
        if (-not $wheel) { throw 'gate failed: wheel build produced no tow-*.whl' }
        Step 'wheel contents' { uv run --frozen python scripts/wheel_check.py $wheel.FullName (Join-Path $PWD 'src/tow') }
        # Install smoke: needs the dependencies in uv's cache (or the network); offline it is skipped.
        $venv = Join-Path $dir 'venv'
        uv venv --quiet $venv 2>$null
        if ($LASTEXITCODE -eq 0) { uv pip install --quiet --python $venv $wheel.FullName 2>$null }
        if ($LASTEXITCODE -ne 0) {
            Skip 'wheel install smoke (tow version)' 'dependencies not available offline'
            return
        }
        $tow = @('Scripts/tow.exe', 'bin/tow') | ForEach-Object { Join-Path $venv $_ } | Where-Object { Test-Path $_ } | Select-Object -First 1
        # The installed app runs against the throwaway directory, never a real config or data.
        $saved = $env:TOW_HOME, $env:TOW_CONFIG
        $env:TOW_HOME = Join-Path $dir 'home'
        $env:TOW_CONFIG = Join-Path $dir 'config.yaml'
        try { Step 'wheel install smoke (tow version)' { & $tow version } }
        finally { $env:TOW_HOME, $env:TOW_CONFIG = $saved }
    }
    finally {
        Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue
    }
}

$snapshot = $null
$savedEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    if ($Staged) {
        $Quick = $true
        Step 'environment' { uv sync --frozen --quiet }
        $snapshot = Join-Path ([IO.Path]::GetTempPath()) ("tow-staged-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
        Step 'export the staged files' { git checkout-index --all --prefix="$snapshot/" }
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $Root '.venv'
        Set-Location $snapshot
    }
    $treeBefore = if (-not $Quick) { Get-WorkingTree } else { $null }

    Step 'uv.lock is current' { uv lock --check }
    Step 'ruff format' { Invoke-Tool ruff format --check src tests }
    Step 'ruff check' { Invoke-Tool ruff check src tests }
    Step 'mypy' { Invoke-Tool mypy }
    Step 'compileall' { Invoke-Tool python -m compileall -q src tests }
    if ($Staged) {
        Step 'whitespace (staged files)' { git -C $Root diff --cached --check $EmptyTree }
    }
    else {
        Step 'whitespace (tracked files)' { git diff --check $EmptyTree }
    }
    if ($Quick) {
        # Without the suite, at least every test module imports and every test is found: a broken
        # import in a test file used to pass the pre-commit gate and fail only the full one.
        Step 'pytest --collect-only' {
            $collected = Invoke-Tool pytest --collect-only -q -p no:cacheprovider 2>&1
            if ($LASTEXITCODE -ne 0) { $collected | Write-Host } else { $collected | Select-Object -Last 1 | Write-Host }
        }
    }
    else {
        # pytest-xdist (a locked dev dependency) spreads the suite over every core; the workers
        # share one shuffle seed (tests/conftest.py), and tests that share an expensive module
        # fixture stay together (--dist loadgroup, `xdist_group`).
        $parallel = @('-n', 'auto', '--dist', 'loadgroup')
        # pytest-cov is not a locked dependency (an offline lock cannot add it): `uv run --with`
        # brings it in from uv's cache or the network; without either, coverage is skipped.
        Invoke-Tool --with $PytestCov python -c 'import pytest_cov' 2>$null
        if ($LASTEXITCODE -eq 0) {
            Step 'pytest (parallel, random order, branch coverage, threshold in pyproject)' {
                Invoke-Tool --with $PytestCov pytest -q -ra -p no:cacheprovider @parallel --test-order random --cov --cov-report=
            }
        }
        else {
            Skip 'coverage' "$PytestCov is not available offline"
            Step 'pytest (parallel, random order)' { Invoke-Tool pytest -q -ra -p no:cacheprovider @parallel --test-order random }
        }
        Test-Wheel
    }
    if ($Audit) {
        # Reserve a file per invocation: concurrent worktrees must audit their own lock.
        $requirements = [IO.Path]::GetTempFileName()
        try {
            Step 'export locked requirements' { uv export --frozen --no-dev --no-hashes --no-emit-project --format requirements-txt -o $requirements }
            Step 'pip-audit' { uvx pip-audit --strict -r $requirements --progress-spinner off }
        }
        finally {
            Remove-Item -LiteralPath $requirements -Force -ErrorAction SilentlyContinue
        }
    }
    if ($treeBefore -and $treeBefore -eq (Get-WorkingTree)) { Save-PassedTree $treeBefore }
}
finally {
    $env:UV_PROJECT_ENVIRONMENT = $savedEnvironment
    Set-Location $Root
    if ($snapshot) { Remove-Item -Recurse -Force $snapshot -ErrorAction SilentlyContinue }
}
if ($Skipped.Count) {
    Write-Host "gate: all checks passed; SKIPPED: $($Skipped -join ', ')" -ForegroundColor Yellow
}
else {
    Write-Host 'gate: all checks passed' -ForegroundColor Green
}
