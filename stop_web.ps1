$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $projectDir ".web_app.pid"
if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Host "Research assistant PID file does not exist."
    exit 0
}

$processId = [int](Get-Content -LiteralPath $pidFile)
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
if ($null -eq $process) {
    Remove-Item -LiteralPath $pidFile -Force
    Write-Host "Removed stale research assistant PID file."
    exit 0
}

$listener = Get-NetTCPConnection `
    -LocalAddress "127.0.0.1" `
    -LocalPort 8765 `
    -State Listen `
    -ErrorAction SilentlyContinue
$isResearchAssistant = `
    $process.Name -eq "python.exe" -and `
    $process.CommandLine -like "*web_app.py*" -and `
    $null -ne $listener -and `
    $listener.OwningProcess -contains $processId
if (-not $isResearchAssistant) {
    throw "PID $processId does not belong to this research assistant; it was not stopped."
}

# The validated web process may own a PDF translation worker and its children.
# Stop that exact tree so a forced app stop does not leave paid model work running.
& taskkill.exe /PID $processId /T /F | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Could not stop the research assistant process tree."
}
Remove-Item -LiteralPath $pidFile -Force
Write-Host "Research assistant stopped."
