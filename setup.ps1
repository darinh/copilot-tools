#!/usr/bin/env pwsh
<#
.SYNOPSIS
    copilot-tools setup for native Windows (PowerShell).

.DESCRIPTION
    Locates a Python 3.10+ interpreter -- installing one with winget if the
    machine has none -- and hands off to setup_tools.py, which provisions the
    remaining prerequisites (terminal multiplexer, git, Copilot CLI), the
    operator/handoff console scripts, runtime extensions, configuration
    templates, Anvil, spec-kit, and the MCP servers. Windows has no legacy
    bash install to migrate away from (that only exists for Linux/WSL/macOS),
    so this script has no migration step.

.PARAMETER SetupArgs
    Arguments forwarded verbatim to setup_tools.py, e.g. --yes,
    --skip-package, --skip-optional, --status, or --check-only.

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

# Windows installers extend PATH in the registry and broadcast a change this
# already-running process never sees, so PATH is re-read after installing.
function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:PATH = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

function Find-Python {
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
            return @{ Exe = $candidate.Exe; Args = $candidate.Args }
        }
    }
    return $null
}

Write-Host "Locating Python..."
$found = Find-Python

if (-not $found) {
    # Setup installs what is missing rather than handing the user a homework
    # list, so a machine without a usable Python gets one here.
    Write-WarnMsg "Python $MinPythonMajor.$MinPythonMinor+ not found - installing it with winget..."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        & winget install --id Python.Python.3.12 --exact --silent `
            --accept-package-agreements --accept-source-agreements --disable-interactivity
        Update-SessionPath
        $found = Find-Python
    } else {
        Write-WarnMsg "winget is unavailable on this machine."
    }
}

if (-not $found) {
    Write-ErrMsg "Could not install Python $MinPythonMajor.$MinPythonMinor+ automatically."
    Write-ErrMsg "Install it from https://python.org or the Microsoft Store, then re-run."
    exit 1
}

$PythonExe = $found.Exe
$PythonArgs = $found.Args

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
