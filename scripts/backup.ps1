#!/usr/bin/env pwsh
# backup.ps1 - one-click backup of the live trading state on TrueNAS.
#
# Backs up the cache dir (portfolio JSON + switching.db + trackers) three ways:
#   1. a timestamped tar.gz on the NAS (kept, with pruning)
#   2. a copy pulled down to this Windows machine (off-box)
#   3. (optional) a ZFS snapshot, the gold standard - pass -Dataset
#
# Run this BEFORE every deploy. Secrets (.env) are deliberately NOT backed up.
#
# First-time setup: ssh-copy-id root@<truenas-ip>  (so SSH needs no password)
#
# Usage:
#   .\backup.ps1                              # tar on NAS + pull to .\backups
#   .\backup.ps1 -NoPull                      # NAS archive only
#   .\backup.ps1 -Dataset Pool_1/Configs      # also take a ZFS snapshot
#   .\backup.ps1 -Keep 20                     # retain 20 NAS archives (default 10)

param(
    [string] $Remote      = "root@192.168.0.81",                      # <-- your TrueNAS
    [string] $StackPath   = "/mnt/Pool_1/Configs/dockge2/Stacks/stocks",
    [string] $CacheSubdir = "data/cache",     # relative to StackPath
    [string] $LocalDir    = "",               # default: <script dir>\backups
    [int]    $Keep        = 10,               # NAS archives to retain
    [string] $Dataset     = "",               # optional ZFS dataset to snapshot
    [switch] $NoPull
)

$ErrorActionPreference = "Stop"
$ts      = Get-Date -Format "yyyyMMdd-HHmmss"
$archive = "cache-$ts.tar.gz"

Write-Host ""
Write-Host "==============================================================" -ForegroundColor Cyan
Write-Host " Switching backup" -ForegroundColor Cyan
Write-Host "   remote:    $Remote"
Write-Host "   stack:     $StackPath"
Write-Host "   cache:     $CacheSubdir"
Write-Host "   archive:   $archive"
Write-Host "==============================================================" -ForegroundColor Cyan

# --- 1. tar the cache dir on the NAS ---
Write-Host ""
Write-Host "[1/4] Creating archive on $Remote ..." -ForegroundColor Cyan
$remoteTar = "cd '$StackPath' && mkdir -p backups && tar -czf 'backups/$archive' '$CacheSubdir' && ls -lh 'backups/$archive'"
ssh $Remote $remoteTar
if ($LASTEXITCODE -ne 0) { throw "Remote tar failed (exit $LASTEXITCODE)." }

# --- 2. verify the archive actually contains the state files ---
Write-Host ""
Write-Host "[2/4] Verifying archive contents..." -ForegroundColor Cyan
$verify = "cd '$StackPath' && tar -tzf 'backups/$archive' | grep -E 'portfolio|tracker|skipped|memory|switching.db'"
$contents = ssh $Remote $verify
if ([string]::IsNullOrWhiteSpace($contents)) {
    Write-Host "  WARNING: no state files found in the archive - check CacheSubdir!" -ForegroundColor Yellow
} else {
    Write-Host $contents
    Write-Host "  OK - state files present." -ForegroundColor Green
}

# --- optional ZFS snapshot ---
if ($Dataset) {
    Write-Host ""
    Write-Host "[*]   Taking ZFS snapshot $Dataset@switching-$ts ..." -ForegroundColor Cyan
    ssh $Remote "zfs snapshot '$Dataset@switching-$ts'"
    if ($LASTEXITCODE -ne 0) { throw "ZFS snapshot failed (exit $LASTEXITCODE)." }
    Write-Host "  snapshot created." -ForegroundColor Green
}

# --- 3. prune old NAS archives, keeping the most recent $Keep ---
Write-Host ""
Write-Host "[3/4] Pruning NAS archives (keeping $Keep most recent)..." -ForegroundColor Cyan
$prune = "cd '$StackPath/backups' && ls -t cache-*.tar.gz 2>/dev/null | tail -n +$($Keep + 1) | xargs -r rm -f; echo retained=`$(ls -1 cache-*.tar.gz 2>/dev/null | wc -l)"
ssh $Remote $prune

# --- 4. pull a copy down to Windows ---
if ($NoPull) {
    Write-Host ""
    Write-Host "[4/4] Skipping local pull (-NoPull). Backup lives on the NAS." -ForegroundColor Green
    return
}
if (-not $LocalDir) { $LocalDir = Join-Path $PSScriptRoot "backups" }
New-Item -ItemType Directory -Force -Path $LocalDir | Out-Null
Write-Host ""
Write-Host "[4/4] Pulling copy to $LocalDir ..." -ForegroundColor Cyan
scp "${Remote}:$StackPath/backups/$archive" "$LocalDir/"
if ($LASTEXITCODE -ne 0) { throw "scp pull failed (exit $LASTEXITCODE)." }

$local = Join-Path $LocalDir $archive
Write-Host ""
Write-Host "==============================================================" -ForegroundColor Green
Write-Host " Backup complete." -ForegroundColor Green
Write-Host "   NAS:   $StackPath/backups/$archive"
Write-Host "   local: $local"
Write-Host "==============================================================" -ForegroundColor Green
