param(
    [string]$BackupPath
)

$ErrorActionPreference = "Stop"
# The scripts/ directory holds this helper; the project root is its parent.
$source = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

if (-not $BackupPath) {
    $backupRoot = "$env:USERPROFILE\Desktop\BetterCallTraderBackup"
    if (-not (Test-Path $backupRoot)) {
        throw "Backup folder not found: $backupRoot"
    }
    $BackupPath = (Get-ChildItem $backupRoot -Directory | Sort-Object Name -Descending | Select-Object -First 1).FullName
}

if (-not (Test-Path $BackupPath)) {
    throw "Backup path not found: $BackupPath"
}

Write-Host "Restoring from: $BackupPath"
$confirm = Read-Host "This will overwrite local .env, DB, log, audit trail, ML model and .\data. Continue? (y/N)"
if ($confirm -ne "y") {
    Write-Host "Restore cancelled."
    exit 0
}

foreach ($file in @("paper_portfolio.db", ".env", "bot_history.log", "bot_audit.jsonl", "ml_model.json")) {
    $srcFile = Join-Path $BackupPath $file
    if (Test-Path $srcFile) {
        Copy-Item $srcFile $source -Force
        Write-Host "Restored $file"
    }
}

# Docker Compose state copied as .\data inside the backup.
$backupData = Join-Path $BackupPath "data"
if (Test-Path $backupData) {
    $localData = Join-Path $source "data"
    New-Item -ItemType Directory -Force -Path $localData | Out-Null
    foreach ($file in @("paper_portfolio.db", "bot_history.log", "bot_audit.jsonl", "ml_model.json")) {
        $srcFile = Join-Path $backupData $file
        if (Test-Path $srcFile) {
            Copy-Item $srcFile $localData -Force
            Write-Host "Restored data\$file"
        }
    }
}

Write-Host "Restore completed."
