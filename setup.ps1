# ═══════════════════════════════════════════════════════════════════
# copilot-tools setup (Windows) - Configure your environment for the
# full Copilot CLI power-user toolkit.
#
# Usage:  .\setup.ps1
#
# What this does:
#   1. Probes native prerequisites (node, npm, git, copilot, optional wsl).
#   2. Installs Node-based extensions globally to
#      %USERPROFILE%\.copilot\extensions\ via directory junctions
#      (falls back to symlink or copy).
#   3. Installs templates (Windows-flavored MCP config + a copilot-
#      instructions.md with a WSL-note header) into %USERPROFILE%\.copilot\.
#      Uses the same hash-manifest smart-upgrade logic as setup.sh.
#   4. Creates %USERPROFILE%\.local\bin\ on PATH and drops operator.cmd /
#      handoff.cmd shims that forward to `wsl operator` / `wsl handoff`.
#      Lets agents call commands by bare name on any platform.
#   5. Installs the Anvil agent plugin (best effort).
#   6. If WSL is installed, probes WSL prereqs and shells `bash setup.sh`
#      inside the default distro so operator/handoff work there too.
#
# Idempotent: safe to re-run. Use .\upgrade.ps1 for one-step pull-and-resync.
# ═══════════════════════════════════════════════════════════════════
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$SkipWsl,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # makes Invoke-WebRequest etc. quieter

$ScriptDir       = Split-Path -Parent $MyInvocation.MyCommand.Path
$CopilotDir      = Join-Path $env:USERPROFILE '.copilot'
$OperatorHome    = if ($env:COPILOT_OPERATOR_HOME) { $env:COPILOT_OPERATOR_HOME } else { Join-Path $env:USERPROFILE '.operator' }
$LocalBin        = Join-Path $env:USERPROFILE '.local\bin'
$ExtensionsDir   = Join-Path $CopilotDir 'extensions'
$TemplateMani    = Join-Path $OperatorHome '.template-manifest.json'

function Info($msg) { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  [!]  $msg" -ForegroundColor Yellow }
function Err ($msg) { Write-Host "  [X]  $msg" -ForegroundColor Red }
function Ask ($msg) {
    if ($NonInteractive) { return $false }
    $ans = Read-Host "  -> $msg [y/N]"
    return ($ans -match '^[Yy]')
}

function Check-Cmd($name) {
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if ($c) { Info "$name found: $($c.Source)"; return $true }
    Err "$name not found"; return $false
}

function Get-FileSha256($path) {
    if (-not (Test-Path $path)) { return $null }
    (Get-FileHash -Algorithm SHA256 -Path $path).Hash.ToLower()
}

function Load-Manifest {
    if (Test-Path $TemplateMani) {
        try { return (Get-Content -Raw $TemplateMani | ConvertFrom-Json) } catch { return @{} }
    }
    return [PSCustomObject]@{}
}

function Save-Manifest($manifest) {
    $dir = Split-Path -Parent $TemplateMani
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $manifest | ConvertTo-Json -Depth 5 | Out-File -Encoding utf8 $TemplateMani
}

# Smart template install. Same semantics as setup.sh:
#   - Missing dest        -> install fresh.
#   - Dest == src hash    -> no-op silent.
#   - Dest == shipped     -> auto-upgrade silent (user hasn't edited).
#   - Dest != both        -> prompt, back up .bak.
function Install-Template($src, $dest, $label, [scriptblock]$BeforeWrite) {
    $destDir = Split-Path -Parent $dest
    if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }

    $manifest = Load-Manifest

    # Compute what we WOULD write (post-BeforeWrite) and hash THAT, not the raw source.
    # If we just hashed $src, BeforeWrite-mutated templates would never match the
    # written file and we'd prompt on every re-run.
    $srcRaw = Get-Content -Raw $src
    $effective = if ($BeforeWrite) { & $BeforeWrite $srcRaw } else { $srcRaw }
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        # Write to a temp file with the SAME encoding we'll use for the real write
        # so the hash matches byte-for-byte.
        $effective | Out-File -Encoding utf8 -NoNewline $tmp
        $effectiveHash = (Get-FileHash -Algorithm SHA256 -Path $tmp).Hash.ToLower()
    } finally {
        Remove-Item -Force $tmp -ErrorAction SilentlyContinue
    }

    $destHash = Get-FileSha256 $dest
    $shippedHash = $null
    if ($manifest.PSObject.Properties.Match($dest).Count -gt 0) {
        $shippedHash = $manifest.$dest
    }

    $action = $null
    if (-not (Test-Path $dest)) {
        $action = 'install'
    } elseif ($destHash -eq $effectiveHash) {
        $action = 'skip-uptodate'
    } elseif ($shippedHash -and $destHash -eq $shippedHash) {
        $action = 'auto-upgrade'
    } else {
        $action = 'prompt'
    }

    switch ($action) {
        'install' {
            $effective | Out-File -Encoding utf8 -NoNewline $dest
            Info "Installed $label"
        }
        'skip-uptodate' { }
        'auto-upgrade' {
            $effective | Out-File -Encoding utf8 -NoNewline $dest
            Info "Auto-upgraded $label (no local edits detected)"
        }
        'prompt' {
            if (Ask "$label has local edits AND a newer version ships. Overwrite (current saved as .bak)?") {
                Copy-Item -Force -Path $dest -Destination "$dest.bak"
                $effective | Out-File -Encoding utf8 -NoNewline $dest
                Info "Updated $label (previous saved to $dest.bak)"
            } else {
                Warn "Skipped $label (kept your version)"
            }
        }
    }

    # Always record the effective hash so the next run knows "this is what we
    # shipped" and can auto-upgrade if the user hasn't edited it.
    $manifest | Add-Member -NotePropertyName $dest -NotePropertyValue $effectiveHash -Force
    Save-Manifest $manifest
}

# Install one extension subdir under %USERPROFILE%\.copilot\extensions\.
# Strategy: junction -> symlink -> copy. Junctions need no admin and survive
# cross-volume restrictions are NOT a thing for them - but they must be on
# the same volume as the SOURCE *and* be on NTFS. Symlinks need Developer
# Mode or admin. Copy is the last resort and gets re-mirrored on upgrade.
#
# Copy-fallback installs drop a sentinel file ('.copilot-tools-managed')
# inside the target so future runs can tell "we copied this last time, safe
# to refresh" from "user-owned directory we should not touch".
function Install-Extension($srcDir, $name) {
    $target = Join-Path $ExtensionsDir $name
    $sentinel = Join-Path $target ".copilot-tools-managed"

    # If something already exists and points to the right place, no-op.
    if (Test-Path $target) {
        $item = Get-Item $target -Force
        $linkTarget = $null
        if ($item.LinkType) { $linkTarget = $item.Target | Select-Object -First 1 }
        if ($linkTarget) {
            try {
                $resolvedLink = (Resolve-Path $linkTarget -ErrorAction Stop).Path
                $resolvedSrc  = (Resolve-Path $srcDir).Path
                if ($resolvedLink -eq $resolvedSrc) {
                    Info "Extension '$name' already linked correctly"
                    return
                }
            } catch { }
            # Wrong target but it IS a link we own (junction/symlink). Safe to remove.
            Remove-Item -Force -Recurse $target
        } elseif (Test-Path $sentinel) {
            # Setup-owned copy from a previous run. Refresh it so 'git pull'
            # changes propagate (matches what upgrade.ps1 docs promise).
            Info "Refreshing managed copy-fallback extension '$name'"
            Remove-Item -Force -Recurse $target
        } else {
            # Real directory we didn't create — mirrors setup.sh behavior:
            # don't clobber a user-owned extension. Tell them and skip.
            Warn "Extension '$name' already exists as a real directory at $target."
            Warn "  Setup will NOT overwrite user-owned extension content."
            Warn "  If you want this repo's version, remove or rename that directory and re-run."
            return
        }
    }

    # 1. Try junction (no admin, but same-volume only).
    try {
        New-Item -ItemType Junction -Path $target -Value $srcDir -ErrorAction Stop | Out-Null
        Info "Extension '$name' installed as junction -> $srcDir"
        return
    } catch { }

    # 2. Try symlink (needs Developer Mode or admin).
    try {
        New-Item -ItemType SymbolicLink -Path $target -Value $srcDir -ErrorAction Stop | Out-Null
        Info "Extension '$name' installed as symlink -> $srcDir"
        return
    } catch { }

    # 3. Fall back to a mirror copy. Re-running setup (or upgrade.ps1) refreshes it.
    Warn "Extension '$name' fell back to file copy (junction/symlink unavailable on this volume)."
    Warn "  Source: $srcDir"
    Warn "  Re-run .\upgrade.ps1 after every 'git pull' to pick up new changes."
    Copy-Item -Recurse -Force -Path $srcDir -Destination $target
    # Drop sentinel so the next run knows this is a managed copy and refreshes it.
    Set-Content -Path $sentinel -Value "Managed by copilot-tools setup.ps1. Do not edit." -Encoding ASCII
    Info "Extension '$name' copied to $target (marked as managed for auto-refresh)"
}

# Drop a Windows shim into %USERPROFILE%\.local\bin so agent instructions can
# call `operator ...` / `handoff ...` regardless of platform. The shim is a
# .cmd file (works from cmd.exe, PowerShell, pwsh, Explorer's Run dialog).
function Install-WslShim($name) {
    $shim = Join-Path $LocalBin "$name.cmd"
    # Forward to WSL safely:
    #   1. 'wsl --exec' bypasses wsl.exe's own default-shell parsing, so cmd.exe
    #      %* arguments are NOT re-tokenized by an outer shell (no injection
    #      via $(), backticks, semicolons, globs).
    #   2. We then explicitly run 'bash -lc' which is a LOGIN shell. It sources
    #      /etc/profile and ~/.profile, putting ~/.local/bin on PATH so the
    #      shims setup.sh installed are found. ('wsl --' alone runs a NON-login
    #      shell which does not source .profile on Debian/Ubuntu.)
    #   3. The -c body is a fixed template ('exec NAME "$@"') — user-supplied
    #      arguments arrive as positional params ($1, $2, ...) and "$@" forwards
    #      them with quoting intact. They are never interpolated into the body.
    # Window-side tokenization note: the inner \" pair is a Windows-CLI escape
    # ('\"' inside "..." is a literal '"'). wsl.exe's argv parser uses Windows
    # rules, so the entire -c argument arrives at bash as: exec NAME "$@"
    $shimLine = 'wsl --exec bash -lc "exec ' + $name + ' \"$@\"" bash %*'
    $lines = @(
        '@echo off',
        "REM Auto-generated by copilot-tools setup.ps1. Forwards $name to WSL.",
        'REM Edit setup.ps1 to regenerate.',
        $shimLine
    )
    [System.IO.File]::WriteAllLines($shim, $lines, [System.Text.Encoding]::ASCII)
    Info "Installed shim ${name}.cmd -> wsl --exec bash -lc 'exec $name ...'"
}

# Ensure $LocalBin is on the user's PATH (persistent + this-process).
function Ensure-OnPath($dir) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = if ($userPath) { $userPath -split ';' } else { @() }
    if ($parts -notcontains $dir) {
        $newPath = if ($userPath) { "$userPath;$dir" } else { $dir }
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        Info "Added $dir to your user PATH (open a new terminal to pick it up)"
    }
    # Always add for THIS process so subsequent commands in this run can find shims.
    if (($env:Path -split ';') -notcontains $dir) {
        $env:Path = "$env:Path;$dir"
    }
}

# Test whether the default WSL distro has a given command.
function Wsl-HasCmd($cmd) {
    $null = & wsl -- bash -lc "command -v $cmd" 2>$null
    return ($LASTEXITCODE -eq 0)
}

# Convert C:\path\to\thing -> /mnt/c/path/to/thing for handing to wsl --cd.
function To-WslPath($winPath) {
    $p = (& wsl -- wslpath -a "$winPath" 2>$null)
    if ($LASTEXITCODE -eq 0 -and $p) { return $p.Trim() }
    # Fallback when wslpath isn't available
    if ($winPath -match '^([A-Za-z]):(.*)$') {
        $drive = $matches[1].ToLower()
        $rest = $matches[2] -replace '\\','/'
        return "/mnt/$drive$rest"
    }
    return $winPath
}

Write-Host ""
Write-Host "=== Copilot Tools Setup (Windows) ===" -ForegroundColor Cyan
Write-Host ""

# -- Step 1: Prerequisites --------------------------------------
Write-Host "Checking native prerequisites..."
$missing = 0
foreach ($cmd in @('node','npm','git')) {
    if (-not (Check-Cmd $cmd)) { $missing++ }
}
if (-not (Check-Cmd 'copilot')) {
    Err "GitHub Copilot CLI is required. Install with: npm install -g @github/copilot"
    $missing++
}
$hasWsl = $false
if (-not $SkipWsl) {
    $wslCmd = Get-Command wsl -ErrorAction SilentlyContinue
    if ($wslCmd) {
        # wsl --status returns 0 on a working installation, non-zero if no distros etc.
        $null = & wsl --status 2>$null
        if ($LASTEXITCODE -eq 0) { $hasWsl = $true; Info "WSL is installed and ready" }
        else { Warn "wsl.exe present but no working distro. Install one with: wsl --install" }
    } else {
        Warn "WSL not installed. operator/handoff require WSL on Windows. Install with: wsl --install"
    }
}
if ($missing -gt 0) {
    Err "$missing prerequisite(s) missing. Install them and re-run."
    exit 1
}
Write-Host ""

# -- Step 2: Directory scaffolding ------------------------------
Write-Host "Setting up directories..."
foreach ($d in @($OperatorHome, (Join-Path $OperatorHome 'restart'), (Join-Path $OperatorHome 'projects'), $CopilotDir, $ExtensionsDir, $LocalBin)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}
Info "Created %USERPROFILE%\.operator\ + %USERPROFILE%\.copilot\extensions\ + %USERPROFILE%\.local\bin\"
Write-Host ""

# -- Step 3: PATH + WSL shims -----------------------------------
Write-Host "Installing WSL shims into $LocalBin ..."
Ensure-OnPath $LocalBin
if ($hasWsl) {
    Install-WslShim 'operator'
    Install-WslShim 'handoff'
} else {
    Warn "Skipping operator.cmd / handoff.cmd shims (need WSL). Re-run setup.ps1 after installing WSL."
}
Write-Host ""

# -- Step 4: Extensions -----------------------------------------
Write-Host "Installing extensions into $ExtensionsDir ..."
$extSrc = Join-Path $ScriptDir 'extensions'
if (Test-Path $extSrc) {
    Get-ChildItem -Directory -Path $extSrc | ForEach-Object {
        Install-Extension $_.FullName $_.Name
    }
} else {
    Warn "No extensions/ directory in repo - skipping."
}
Write-Host ""

# -- Step 5: Templates -----------------------------------------
Write-Host "Installing templates into $CopilotDir ..."
$mcpSrc = Join-Path $ScriptDir 'templates\mcp-config.windows.json'
if (-not (Test-Path $mcpSrc)) { $mcpSrc = Join-Path $ScriptDir 'templates\mcp-config.json' }
Install-Template $mcpSrc (Join-Path $CopilotDir 'mcp-config.json') 'MCP config'

# Stamp a Windows note at the top of copilot-instructions.md explaining the
# WSL bridge. This way, agents reading the global instructions know that
# operator/handoff commands route through WSL on Windows.
$instSrc = Join-Path $ScriptDir 'templates\copilot-instructions.md'
$winHeader = @'
> **Windows note**: This machine runs Copilot CLI on Windows. The `operator`
> and `handoff` commands referenced below are .cmd shims in
> `%USERPROFILE%\.local\bin\` that forward to `wsl operator` / `wsl handoff`.
> The actual bash scripts run inside WSL. State paths like `~/.operator/` and
> `~/.copilot/` refer to the WSL home directory when used from within those
> commands, and to `%USERPROFILE%\` when used by the native Copilot CLI.

'@
Install-Template $instSrc (Join-Path $CopilotDir 'copilot-instructions.md') 'Copilot instructions' {
    param($contents)
    if ($contents.StartsWith('> **Windows note**')) { return $contents }
    return $winHeader + $contents
}
Write-Host ""

# -- Step 6: Anvil plugin --------------------------------------
Write-Host "Installing Anvil agent plugin..."
try {
    $existing = & copilot extensions list 2>$null
    if ($existing -match 'anvil') {
        Info "Anvil already installed"
    } else {
        & copilot install burkeholland/anvil 2>$null
        if ($LASTEXITCODE -eq 0) { Info "Installed Anvil from burkeholland/anvil" }
        else { Warn "Could not auto-install Anvil. Install manually: copilot install burkeholland/anvil" }
    }
} catch {
    Warn "Could not auto-install Anvil. Install manually: copilot install burkeholland/anvil"
}
Write-Host ""

# -- Step 7: WSL bridge ----------------------------------------
if ($hasWsl -and -not $SkipWsl) {
    Write-Host "Checking WSL prerequisites..."
    $wslMissing = @()
    foreach ($c in @('bash','git','tmux','sqlite3','python3','copilot')) {
        if (Wsl-HasCmd $c) { Info "WSL has $c" }
        else { Warn "WSL missing $c"; $wslMissing += $c }
    }
    if ($wslMissing.Count -gt 0) {
        Warn "Install in WSL with (Debian/Ubuntu): sudo apt-get install -y $($wslMissing -join ' ')"
        Warn "For copilot CLI in WSL: npm install -g @github/copilot"
    }
    Write-Host ""

    Write-Host "Bridging into WSL to run setup.sh ..."
    $wslRepo = To-WslPath $ScriptDir
    # `wsl --cd` sets the working directory inside WSL before exec'ing.
    & wsl --cd "$wslRepo" -- bash ./setup.sh
    if ($LASTEXITCODE -eq 0) {
        Info "WSL-side setup completed"
    } else {
        Warn "WSL-side setup exited with code $LASTEXITCODE. Re-run inside WSL to debug:"
        Warn "  wsl --cd $wslRepo bash ./setup.sh"
    }
    Write-Host ""
} else {
    Write-Host "Skipping WSL bridge (WSL not available or -SkipWsl)."
    Write-Host ""
}

# -- Done -----------------------------------------------------
Write-Host "=== Setup Complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Open a NEW terminal so the updated PATH takes effect."
if ($hasWsl) {
    Write-Host "  2. Try: operator help    (runs 'wsl operator help' under the hood)"
    Write-Host "  3. Start a loop: operator --loop --name myproject --agent=anvil:anvil"
    Write-Host "  4. Review %USERPROFILE%\.copilot\copilot-instructions.md and customize"
    Write-Host "  5. To upgrade later: .\upgrade.ps1"
} else {
    Write-Host "  2. Install WSL: wsl --install   then re-run .\setup.ps1"
    Write-Host "  3. Review %USERPROFILE%\.copilot\copilot-instructions.md and customize"
    Write-Host "  4. To upgrade later: .\upgrade.ps1"
}
Write-Host ""
