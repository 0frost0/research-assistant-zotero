param([int]$LocalPort = 8765)
$ErrorActionPreference = 'Stop'
if ($LocalPort -lt 1024 -or $LocalPort -gt 65535) { throw 'Invalid local port' }
$projectDir = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $projectDir ".agent_assistant_tunnel_${LocalPort}.pid"
$healthUrl = "http://127.0.0.1:${LocalPort}/api/health"

if (Test-Path -LiteralPath $pidFile) {
    $savedPid = 0
    [void][int]::TryParse((Get-Content -Raw -LiteralPath $pidFile).Trim(), [ref]$savedPid)
    if ($savedPid -gt 0) {
        $savedProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$savedPid" -ErrorAction SilentlyContinue
        $forward = "127.0.0.1:${LocalPort}:127.0.0.1:8765"
        if ($savedProcess -and $savedProcess.Name -eq 'ssh.exe' -and
            $savedProcess.CommandLine -like "*${forward}*" -and
            $savedProcess.CommandLine -like '*agent-assistant*') {
            $status = & curl.exe --noproxy '*' --connect-timeout 1 --max-time 3 --silent --output NUL --write-out '%{http_code}' $healthUrl
            if ($LASTEXITCODE -eq 0 -and $status -eq '200') {
                Write-Output "Research assistant already available: http://127.0.0.1:${LocalPort}/ (SSH PID $savedPid)"
                exit 0
            }
            throw "Recorded agent-assistant tunnel PID $savedPid is running, but the remote web health check failed"
        }
    }
}

$listener = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
    $ownerSummary = if ($owner) { "$($owner.Name) PID $($owner.ProcessId)" } else { "PID $($listener.OwningProcess)" }
    throw "Local port $LocalPort is occupied by $ownerSummary; it is not the recorded agent-assistant tunnel"
}

$arguments = @(
    '-N','-o','BatchMode=yes','-o','ExitOnForwardFailure=yes',
    '-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3',
    '-L',"127.0.0.1:${LocalPort}:127.0.0.1:8765",'agent-assistant'
)
$process = Start-Process -FilePath ssh.exe -ArgumentList $arguments -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 2
if ($process.HasExited) { throw 'agent-assistant SSH tunnel failed' }
$process.Id | Set-Content -LiteralPath $pidFile -Encoding ascii
$status = & curl.exe --noproxy '*' --connect-timeout 2 --max-time 10 --silent --output NUL --write-out '%{http_code}' $healthUrl
if ($LASTEXITCODE -ne 0 -or $status -ne '200') { throw 'Tunnel is running, but the remote web service is not healthy' }
Write-Output "Research assistant: http://127.0.0.1:${LocalPort}/ (SSH PID $($process.Id))"
