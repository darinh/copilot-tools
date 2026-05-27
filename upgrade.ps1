# ═══════════════════════════════════════════════════════════════════
# copilot-tools upgrade (Windows) - Pull the latest changes and
# re-run setup.ps1.
#
# Usage: .\upgrade.ps1
#
# Steps:
#   1. git fetch + fast-forward (refuses if uncommitted changes or
#      diverged history would lose work).
#   2. Re-run setup.ps1, which:
#        - refreshes extension junctions/copies,
#        - smart-upgrades templates,
#        - regenerates operator.cmd / handoff.cmd shims,
#        - bridges into WSL to run setup.sh + migration there.
# ═══════════════════════════════════════════════════════════════════
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$SkipWsl
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

function Info($m) { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Err ($m) { Write-Host "  [X]  $m" -ForegroundColor Red }

Write-Host ""
Write-Host "=== Copilot Tools Upgrade (Windows) ===" -ForegroundColor Cyan
Write-Host ""

# 1. Must be a git checkout.
& git rev-parse --git-dir 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Err "$ScriptDir is not a git checkout. Clone the repo and re-run from there."
    exit 1
}

# 2. Refuse on uncommitted changes - would clobber.
$dirty = & git status --porcelain
if ($dirty) {
    Err "Uncommitted changes detected. Commit or stash them first:"
    & git status --short
    exit 1
}

# 3. Fetch + fast-forward.
$branch = (& git rev-parse --abbrev-ref HEAD).Trim()
Info "On branch: $branch"

& git fetch --quiet
if ($LASTEXITCODE -ne 0) { Err "git fetch failed."; exit 1 }

$before = (& git rev-parse HEAD).Trim()
& git pull --ff-only --quiet
if ($LASTEXITCODE -ne 0) {
    Err "Cannot fast-forward. Your branch has diverged from origin."
    Err "Resolve manually (rebase/merge) and re-run."
    exit 1
}
$after = (& git rev-parse HEAD).Trim()

if ($before -eq $after) {
    Info "Already up to date."
} else {
    Info "Updated $before -> $after"
    & git --no-pager log --oneline "$before..$after" | ForEach-Object { "    $_" } | Write-Host
}
Write-Host ""

# 4. Re-run setup.
$setupArgs = @()
if ($SkipWsl) { $setupArgs += '-SkipWsl' }
& "$ScriptDir\setup.ps1" @setupArgs
exit $LASTEXITCODE
