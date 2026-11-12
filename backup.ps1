param(
    [string]$Destination = "$env:USERPROFILE\Desktop\AgenticTradeBackup"
)

$ErrorActionPreference = "Stop"
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
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

Write-Host "Backup created at: $target"