$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$healthUrl = "http://127.0.0.1:30001/health"
try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
} catch { $health = $null }
if ($health) {
    if ($health.model_id -ne "Qwen/Qwen3-VL-Embedding-2B") {
        throw "Port 30001 belongs to another service."
    }
    Write-Host "Unified embedding tunnel is already available."
    exit 0
}
$sshArgs = @("-N", "-L", "127.0.0.1:30001:127.0.0.1:30001",
    "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3", "-o", "BatchMode=yes",
    "-i", "C:/Users/Allon/.ssh/id_rsa", "-p", "36969", "dingone@10.10.129.37")
$process = Start-Process -FilePath "ssh.exe" -ArgumentList $sshArgs -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 2
if ($process.HasExited) { throw "Embedding SSH tunnel failed to start." }
Set-Content -LiteralPath (Join-Path $projectRoot ".embedding_tunnel.pid") -Value $process.Id -Encoding ascii
$health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 10
if ($health.model_id -ne "Qwen/Qwen3-VL-Embedding-2B") { throw "Unexpected embedding service." }
Write-Host "Unified embedding tunnel: http://127.0.0.1:30001"
