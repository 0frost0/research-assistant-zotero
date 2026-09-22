$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $projectRoot ".mineru_tunnel.pid"

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Host "No MinerU tunnel PID file was found."
    exit 0
}

$tunnelPid = [int](Get-Content -LiteralPath $pidFile -Raw).Trim()
$process = Get-Process -Id $tunnelPid -ErrorAction SilentlyContinue

if ($null -eq $process) {
    Write-Host "The recorded tunnel process is no longer running."
} elseif ($process.ProcessName -ne "ssh") {
    throw "PID $tunnelPid is not an SSH process; it will not be stopped."
} else {
    Stop-Process -Id $tunnelPid
    Write-Host "MinerU SSH tunnel stopped. PID: $tunnelPid"
}

Remove-Item -LiteralPath $pidFile -Force
