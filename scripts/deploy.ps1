#!/usr/bin/env pwsh
# deploy.ps1 - one-stop deploy from Windows.
#
# Full lifecycle: backup live state -> verify GitHub sync -> push if needed ->
# SSH into TrueNAS and run scripts/deploy.sh (build image once, recreate ALL
# active services, print verification summary). Same GitHub image as a manual
# TrueNAS deploy - no divergent local image, no Dockge shell needed.
#
# Step-by-step:
#   [1/4] Backup  -- tars the live cache on TrueNAS + pulls a local copy. Aborts
#                    the deploy if backup fails (rollback path before forward path).
#   [2/4] Sync    -- git fetch origin, then:
#                      ahead only  -> push commits to GitHub (normal deploy)
#                      in sync     -> skip push, deploy current GitHub HEAD
#                      behind      -> ABORT with "git pull" instructions
#                      diverged    -> ABORT with "git pull --rebase" instructions
#   [3/4] Build   -- SSH to TrueNAS, curl scripts/deploy.sh from GitHub, rebuild
#                    image once, recreate all four active services.
#   [4/4] Logs    -- tail logs for the deployed services (Ctrl+C to stop).
#
# First-time setup (so SSH does not prompt for a password):
#   ssh-copy-id root@<truenas-ip>
#   (or paste ~/.ssh/id_rsa.pub into TrueNAS UI > Credentials > SSH Keys)
#
# Usage:
#   .\deploy.ps1                       # backup + sync/push + deploy all four active services
#   .\deploy.ps1 -Services dashboard   # deploy a subset (still backs up + builds once)
#   .\deploy.ps1 -SkipPush             # deploy already-pushed code (no git push or sync check)
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
$DefaultServices = @("paper-trade", "paper-trade-uk", "trade-t212", "trade-t212-uk", "dashboard")

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

# --- 2. verify GitHub sync state, then push if needed ---
if ($SkipPush) {
    Write-Host ""
    Write-Host "[2/4] Skipping git push (-SkipPush)." -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "[2/4] Checking GitHub sync state..." -ForegroundColor Cyan

    # Uncommitted-change gate. Distinguish TRACKED modifications (real work a
    # GitHub deploy would silently leave behind) from UNTRACKED files (local-only
    # noise like .claude/ or scratch files - never part of a commit, so they must
    # NOT block a deploy). Only tracked changes abort.
    $trackedDirty = git -C $RepoDir status --porcelain --untracked-files=no
    if ($trackedDirty -and -not $Force) {
        Write-Host "Uncommitted changes to TRACKED files detected:" -ForegroundColor Yellow
        git -C $RepoDir status --short --untracked-files=no
        throw "Commit your changes first - a GitHub deploy only ships PUSHED commits. Re-run with -Force to deploy the last pushed commit anyway."
    }
    if ($trackedDirty -and $Force) {
        Write-Host "  -Force: tracked changes above will NOT ship; deploying last commit." -ForegroundColor Yellow
    }

    # Untracked files are informational only - they never ship (not committed),
    # so a permanent local folder (.claude/, etc.) won't block future deploys.
    $untracked = @(git -C $RepoDir ls-files --others --exclude-standard)
    if ($untracked.Count -gt 0) {
        Write-Host "  Note: $($untracked.Count) untracked file(s) present - these will not ship (not committed):" -ForegroundColor Gray
        $untracked | Select-Object -First 5 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        if ($untracked.Count -gt 5) { Write-Host "    ... and $($untracked.Count - 5) more" -ForegroundColor DarkGray }
    }

    # Fetch remote state so we can compare accurately
    Write-Host "  Fetching from origin..." -ForegroundColor Gray
    git -C $RepoDir fetch origin 2>$null
    if ($LASTEXITCODE -ne 0) { throw "git fetch failed (exit $LASTEXITCODE). Check network / GitHub access." }

    # How many commits ahead/behind is local vs origin/<Branch>?
    $ahead  = [int](git -C $RepoDir rev-list "origin/$Branch..HEAD"       --count 2>$null)
    $behind = [int](git -C $RepoDir rev-list "HEAD..origin/$Branch"       --count 2>$null)
    $sha    = (git -C $RepoDir rev-parse --short HEAD 2>$null).Trim()

    if ($ahead -eq 0 -and $behind -eq 0) {
        # Already in sync -- the server needs a rebuild, but there is nothing to push
        Write-Host "  Local is already in sync with origin/$Branch ($sha)." -ForegroundColor Green
        Write-Host "  Nothing to push -- deploying the current GitHub HEAD." -ForegroundColor Gray

    } elseif ($ahead -gt 0 -and $behind -eq 0) {
        # Normal deploy: local has new commits to ship
        Write-Host "  Local is $ahead commit(s) ahead of origin/$Branch -- pushing..." -ForegroundColor Cyan
        git -C $RepoDir push origin HEAD
        if ($LASTEXITCODE -ne 0) { throw "git push failed (exit $LASTEXITCODE)." }
        Write-Host "  Pushed OK." -ForegroundColor Green

    } elseif ($behind -gt 0 -and $ahead -eq 0) {
        # Local is stale: remote has commits we don't have -- deploying now would re-ship old code
        Write-Host ""
        Write-Host "==============================================================" -ForegroundColor Yellow
        Write-Host " SYNC WARNING: local is $behind commit(s) BEHIND origin/$Branch" -ForegroundColor Yellow
        Write-Host "   Run:  git pull origin $Branch" -ForegroundColor Yellow
        Write-Host "   Then: .\deploy.ps1" -ForegroundColor Yellow
        Write-Host "   (Use -SkipPush to force-deploy what is already on GitHub.)" -ForegroundColor Yellow
        Write-Host "==============================================================" -ForegroundColor Yellow
        throw "Local branch is behind origin/$Branch by $behind commit(s). Pull first to avoid deploying stale code."

    } else {
        # Diverged: local and remote have different commits -- need a rebase/merge
        Write-Host ""
        Write-Host "==============================================================" -ForegroundColor Red
        Write-Host " SYNC ERROR: branches have DIVERGED" -ForegroundColor Red
        Write-Host "   Local:  $ahead commit(s) ahead of origin/$Branch" -ForegroundColor Red
        Write-Host "   Remote: $behind commit(s) ahead of local" -ForegroundColor Red
        Write-Host "   Resolve: git pull --rebase origin $Branch" -ForegroundColor Red
        Write-Host "   Then:   .\deploy.ps1" -ForegroundColor Red
        Write-Host "==============================================================" -ForegroundColor Red
        throw "Branches have diverged ($ahead ahead, $behind behind). Resolve before deploying."
    }
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
