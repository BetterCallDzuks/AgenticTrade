param(
    [string]$Destination = "$env:USERPROFILE\Desktop\BetterCallTraderBackup"
)

$ErrorActionPreference = "Stop"
# The scripts/ directory holds this helper; the project root is its parent.
$source = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$target = Join-Path $Destination $timestamp
New-Item -ItemType Directory -Force -Path $target | Out-Null

$files = @("paper_portfolio.db", ".env", "bot_history.log", "bot_audit.jsonl", "ml_model.json")
foreach ($file in $files) {
    $path = Join-Path $source $file
    if (Test-Path $path) {
        Copy-Item $path $target -Force
    }
}

# Docker Compose keeps the same files under .\data.
$dataFiles = @("paper_portfolio.db", "bot_history.log", "bot_audit.jsonl", "ml_model.json")
$dataDir = Join-Path $source "data"
if (Test-Path $dataDir) {
    $targetData = Join-Path $target "data"
    foreach ($file in $dataFiles) {
        $path = Join-Path $dataDir $file
        if (Test-Path $path) {
            New-Item -ItemType Directory -Force -Path $targetData | Out-Null
            Copy-Item $path $targetData -Force
        }
    }
}

Write-Host "Backup created at: $target"
