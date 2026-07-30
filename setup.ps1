#!/usr/bin/env pwsh
<#
.SYNOPSIS
    copilot-tools setup for native Windows (PowerShell).

.DESCRIPTION
    Locates a Python 3.10+ interpreter and hands off to setup_tools.py, which
    installs the operator/handoff console scripts, runtime extensions, and
    configuration templates. Windows has no legacy bash install to migrate
    away from (that only exists for Linux/WSL/macOS), so this script is a
    thin, version-checked launcher rather than a migration.

.PARAMETER SetupArgs
    Arguments forwarded verbatim to setup_tools.py, e.g. --yes or
    --skip-package.

.EXAMPLE
    ./setup.ps1 --yes
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$SetupArgs = @()
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$MinPythonMajor = 3
$MinPythonMinor = 10

function Write-Info  { param([string]$Message) Write-Host "  [OK] $Message" }
function Write-WarnMsg { param([string]$Message) Write-Host "  [!] $Message" -ForegroundColor Yellow }
function Write-ErrMsg { param([string]$Message) Write-Host "  [X] $Message" -ForegroundColor Red }

Write-Host ""
Write-Host "=== Copilot Tools Setup ==="
Write-Host ""

# ── Locate Python 3.10+ ──────────────────────────────────────────
# Candidates are tried in order; each is a launcher exe plus the args needed
# to select Python 3 with it (the `py` launcher needs `-3`, plain python*
# executables don't). Using an array of hashtables (rather than parallel
# arrays or comma-separated tuples) avoids PowerShell's comma/array
# unpacking pitfalls when iterating.
$Candidates = @(
    @{ Exe = 'py'; Args = @('-3') }
    @{ Exe = 'python'; Args = @() }
    @{ Exe = 'python3'; Args = @() }
)

Write-Host "Locating Python..."
$PythonExe = $null
$PythonArgs = @()

foreach ($candidate in $Candidates) {
    $cmd = Get-Command $candidate.Exe -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }

    try {
        $verOutput = & $candidate.Exe @($candidate.Args) -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
    } catch {
        continue
    }
    if (-not $verOutput) { continue }

    $parts = $verOutput.Trim().Split('.')
    if ($parts.Count -lt 2) { continue }
    $major = [int]$parts[0]
    $minor = [int]$parts[1]

    if ($major -gt $MinPythonMajor -or ($major -eq $MinPythonMajor -and $minor -ge $MinPythonMinor)) {
        $PythonExe = $candidate.Exe
        $PythonArgs = $candidate.Args
        break
    }
}

if (-not $PythonExe) {
    Write-ErrMsg "Python $MinPythonMajor.$MinPythonMinor+ is required. Install it from https://python.org or the Microsoft Store, then re-run."
    exit 1
}

$versionString = (& $PythonExe @PythonArgs --version 2>&1)
Write-Info "Using '$PythonExe $($PythonArgs -join ' ')' ($versionString)"
Write-Host ""

# ── Hand off to the cross-platform Python installer ─────────────
Write-Host "Running Python setup (package, extensions, templates)..."
$setupToolsPath = Join-Path $ScriptDir 'setup_tools.py'
& $PythonExe @PythonArgs $setupToolsPath @SetupArgs
$status = $LASTEXITCODE
Write-Host ""

if ($status -ne 0) {
    Write-ErrMsg "Python setup failed (exit $status)."
    exit $status
}

exit 0
