$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $projectRoot ".mineru_tunnel.pid"
$healthUrl = "http://127.0.0.1:30000/health"

try {
    $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
    if ($response.StatusCode -eq 200) {
        Write-Host "MinerU tunnel is already available at http://127.0.0.1:30000"
        exit 0
    }
} catch {
    # No healthy local endpoint yet, so start the tunnel below.
}

$sshArgs = @(
    "-N",
    "-L", "30000:127.0.0.1:30000",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "BatchMode=yes",
    "-i", "C:/Users/Allon/.ssh/id_rsa",
    "-p", "36969",
    "dingone@10.10.129.37"
)

$tunnelProcess = Start-Process `
    -FilePath "ssh.exe" `
    -ArgumentList $sshArgs `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 3
if ($tunnelProcess.HasExited) {
    throw "SSH tunnel failed to start. Exit code: $($tunnelProcess.ExitCode)"
}

Set-Content -LiteralPath $pidFile -Value $tunnelProcess.Id -Encoding ascii
$response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 5
if ($response.StatusCode -ne 200) {
    throw "SSH tunnel started, but MinerU health check returned $($response.StatusCode)."
}

Write-Host "MinerU tunnel started. PID: $($tunnelProcess.Id)"
Write-Host "API docs: http://127.0.0.1:30000/docs"
