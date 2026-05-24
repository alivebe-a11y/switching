#!/usr/bin/env pwsh
# deploy.ps1 - one-click deploy from Windows.
#
# Pushes your committed code to GitHub, then SSHes into TrueNAS and runs the
# canonical scripts/deploy.sh there (build the shared image once, recreate ALL
# active services, print a verification summary). Same GitHub image as a manual
# TrueNAS deploy - no divergent local image, and no logging into the Dockge shell.
#
# This is the version-controlled copy. The active launcher usually lives one
# level up (e.g. C:\Users\...\switch\deploy.ps1); the repo-detection below works
# from either location, so the two copies can be byte-identical.
#
# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads .ps1 files as the
# system ANSI codepage, so a UTF-8 em-dash or smart-quote will be mis-decoded and
# break parsing. Use plain - and straight quotes only.
#
# First-time setup (so SSH does not prompt for a password):
#   ssh-copy-id root@<truenas-ip>
#   (or paste ~/.ssh/id_rsa.pub into TrueNAS UI > Credentials > SSH Keys)
#
# Usage:
#   .\deploy.ps1                       # push + deploy all four active services
#   .\deploy.ps1 -Services dashboard   # deploy a subset (still builds once)
#   .\deploy.ps1 -SkipPush             # deploy already-pushed code (no git push)
#   .\deploy.ps1 -Force                # push/deploy even with uncommitted changes
#   .\deploy.ps1 -NoLogs               # skip the log tail at the end

param(
    [string]   $Remote    = "root@192.168.0.81",                      # <-- your TrueNAS
    [string]   $StackPath = "/mnt/Pool_1/Configs/dockge2/Stacks/stocks",
    [string[]] $Services  = @(),        # empty = deploy.sh default four
    [string]   $Branch    = "main",
    [string]   $RepoDir   = "",         # auto-detected if empty
    [switch]   $SkipPush,
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

# --- 1. push committed code to GitHub ---
if ($SkipPush) {
    Write-Host ""
    Write-Host "[1/3] Skipping git push (-SkipPush)." -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "[1/3] Pushing committed code to GitHub..." -ForegroundColor Cyan
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

# --- 2. trigger the canonical deploy.sh on TrueNAS ---
Write-Host ""
Write-Host "[2/3] Building image + recreating services on $Remote ..." -ForegroundColor Cyan
$deployUrl  = "https://raw.githubusercontent.com/$Repo/main/scripts/deploy.sh"
$pipeTarget = "BRANCH='$Branch' bash"
if ($Services.Count -gt 0) {
    $pipeTarget += " -s -- " + ($Services -join ' ')
}
$remoteCmd = "cd '$StackPath' && curl -fsSL '$deployUrl' | $pipeTarget"
ssh $Remote $remoteCmd
if ($LASTEXITCODE -ne 0) { throw "Remote deploy failed (exit $LASTEXITCODE)." }

# --- 3. tail logs ---
if ($NoLogs) {
    Write-Host ""
    Write-Host "[3/3] Done (log tail skipped via -NoLogs)." -ForegroundColor Green
    return
}
if ($Services.Count -gt 0) { $tail = $Services } else { $tail = $DefaultServices }
$tailList = $tail -join ' '
Write-Host ""
Write-Host "[3/3] Tailing logs for: $tailList   (Ctrl+C to stop)" -ForegroundColor Green
ssh $Remote "cd '$StackPath' && docker compose logs $tailList --tail 40 -f"
