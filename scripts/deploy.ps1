#!/usr/bin/env pwsh
# deploy.ps1 - one-click deploy from Windows.
#
# Pushes your committed code to GitHub, then SSHes into TrueNAS and runs the
# canonical scripts/deploy.sh there (build the shared image once, recreate ALL
# active services, print a verification summary). Same GitHub image as a manual
# TrueNAS deploy - no divergent local image, and no logging into the Dockge shell.
#
# First-time setup (so SSH does not prompt for a password):
#   ssh-copy-id root@<truenas-ip>
#   (or paste ~/.ssh/id_rsa.pub into TrueNAS UI > Credentials > SSH Keys)
#
# Usage:
#   .\deploy.ps1                       # backup + push + deploy all four active services
#   .\deploy.ps1 -Services dashboard   # deploy a subset (still backs up + builds once)
#   .\deploy.ps1 -SkipPush             # deploy already-pushed code (no git push)
#   .\deploy.ps1 -SkipBackup           # EMERGENCY: skip the pre-deploy backup
#   .\deploy.ps1 -Snapshot Pool_1/Configs   # ZFS snapshot too (gold standard)
#   .\deploy.ps1 -Force                # push/deploy even with uncommitted changes
#   .\deploy.ps1 -NoLogs               # skip the log tail at the end
#
# Backup is the first step and aborts the deploy if it fails. This is deliberate -
# we never want a deploy to land on top of an un-backed-up state. Use -SkipBackup
# only when the backup itself is broken and you must ship a fix.

param(
    [string]   $Remote    = "root@192.168.0.81",                      # <-- your TrueNAS
    [string]   $StackPath = "/mnt/Pool_1/Configs/dockge2/Stacks/stocks",
    [string[]] $Services  = @(),        # empty = deploy.sh default four
    [string]   $Branch    = "main",
    [string]   $RepoDir   = "",         # auto-detected if empty
    [string]   $Snapshot  = "",         # ZFS dataset for the optional snapshot (passed through to backup.ps1)
    [switch]   $SkipPush,
    [switch]   $SkipBackup,
    [switch]   $Force,
    [switch]   $NoLogs
)

$ErrorActionPreference = "Stop"
$Repo = "alivebe-a11y/switching"
$DefaultServices = @("paper-trade", "paper-trade-uk", "trade-t212", "dashboard")

# --- locate the git repo (works whether this script sits in switch\ or the repo) ---
if (-not $RepoDir) {
    $candidates = @(
        (Join-Path $PSScriptRoot 'switching'),   # launcher in parent folder (switch\deploy.ps1)
        $PSScriptRoot,                            # launcher in repo root
        (Split-Path $PSScriptRoot -Parent)        # launcher in repo\scripts\
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path (Join-Path $c '.git'))) { $RepoDir = $c; break }
    }
}
if (-not $RepoDir -or -not (Test-Path (Join-Path $RepoDir '.git'))) {
    throw "Could not find the switching git repo near $PSScriptRoot. Pass -RepoDir explicitly."
}

Write-Host ""
Write-Host "==============================================================" -ForegroundColor Cyan
Write-Host " Switching deploy  (Windows -> GitHub -> TrueNAS)" -ForegroundColor Cyan
Write-Host "   repo dir: $RepoDir"
Write-Host "   remote:   $Remote"
Write-Host "   branch:   $Branch"
if ($Services.Count -gt 0) { Write-Host "   services: $($Services -join ', ')" }
else                       { Write-Host "   services: (default four)" }
Write-Host "==============================================================" -ForegroundColor Cyan

# --- 1. backup live state BEFORE shipping any code ---
# Rationale: a deploy can land on top of a working DB and the new code can
# silently corrupt it. The backup is our recovery path. If the backup fails
# we MUST NOT proceed - aborting is the safe default. -SkipBackup is an
# emergency lever (broken backup script + critical hotfix).
if ($SkipBackup) {
    Write-Host ""
    Write-Host "[1/4] ! SKIPPING BACKUP (-SkipBackup) - if this deploy corrupts state, recovery is from the LAST backup." -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "[1/4] Backing up live state on $Remote ..." -ForegroundColor Cyan
    $backupScript = Join-Path $PSScriptRoot "backup.ps1"
    if (-not (Test-Path $backupScript)) {
        # Fall back to the version-controlled mirror inside the repo
        $backupScript = Join-Path $RepoDir "scripts\backup.ps1"
    }
    if (-not (Test-Path $backupScript)) {
        throw "Could not find backup.ps1 (looked in $PSScriptRoot and $RepoDir\scripts). Re-run with -SkipBackup if you must, but FIX the backup script first."
    }
    $backupArgs = @{ Remote = $Remote; StackPath = $StackPath }
    if ($Snapshot) { $backupArgs['Dataset'] = $Snapshot }
    try {
        & $backupScript @backupArgs
        if ($LASTEXITCODE -ne 0) { throw "backup.ps1 returned exit $LASTEXITCODE" }
    } catch {
        Write-Host ""
        Write-Host "==============================================================" -ForegroundColor Red
        Write-Host " DEPLOY ABORTED - backup failed" -ForegroundColor Red
        Write-Host "   reason: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "   No code was pushed and no services were touched." -ForegroundColor Red
        Write-Host "   Fix the backup (or re-run with -SkipBackup for emergency hotfix)." -ForegroundColor Red
        Write-Host "==============================================================" -ForegroundColor Red
        throw "Backup failed - deploy aborted."
    }
}

# --- 2. push committed code to GitHub ---
if ($SkipPush) {
    Write-Host ""
    Write-Host "[2/4] Skipping git push (-SkipPush)." -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "[2/4] Pushing committed code to GitHub..." -ForegroundColor Cyan
    $dirty = git -C $RepoDir status --porcelain
    if ($dirty -and -not $Force) {
        Write-Host "Uncommitted changes detected:" -ForegroundColor Yellow
        git -C $RepoDir status --short
        throw "Commit your changes first - a GitHub deploy only ships PUSHED commits. Re-run with -Force to deploy the last pushed commit anyway."
    }
    if ($dirty -and $Force) {
        Write-Host "  -Force: the uncommitted changes above will NOT ship; deploying last commit." -ForegroundColor Yellow
    }
    git -C $RepoDir push origin HEAD
    if ($LASTEXITCODE -ne 0) { throw "git push failed (exit $LASTEXITCODE)." }
}

# --- 3. trigger the canonical deploy.sh on TrueNAS ---
Write-Host ""
Write-Host "[3/4] Building image + recreating services on $Remote ..." -ForegroundColor Cyan
$deployUrl  = "https://raw.githubusercontent.com/$Repo/main/scripts/deploy.sh"
$pipeTarget = "BRANCH='$Branch' bash"
if ($Services.Count -gt 0) {
    $pipeTarget += " -s -- " + ($Services -join ' ')
}
$remoteCmd = "cd '$StackPath' && curl -fsSL '$deployUrl' | $pipeTarget"
ssh $Remote $remoteCmd
if ($LASTEXITCODE -ne 0) { throw "Remote deploy failed (exit $LASTEXITCODE)." }

# --- 4. tail logs ---
if ($NoLogs) {
    Write-Host ""
    Write-Host "[4/4] Done (log tail skipped via -NoLogs)." -ForegroundColor Green
    return
}
if ($Services.Count -gt 0) { $tail = $Services } else { $tail = $DefaultServices }
$tailList = $tail -join ' '
Write-Host ""
Write-Host "[4/4] Tailing logs for: $tailList   (Ctrl+C to stop)" -ForegroundColor Green
ssh $Remote "cd '$StackPath' && docker compose logs $tailList --tail 40 -f"
