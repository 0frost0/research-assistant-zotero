$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $projectDir ".web_app.pid"
$pythonPath = if ($env:RESEARCH_ASSISTANT_PYTHON) {
    $env:RESEARCH_ASSISTANT_PYTHON
} else {
    "D:\miniconda3\envs\deepagents\python.exe"
}
# Loopback readiness must not wait for external model services.
$healthUrl = "http://127.0.0.1:8765/api/health"
$logDir = Join-Path ([System.IO.Path]::GetTempPath()) "research_assistant_logs"
$stdoutLog = Join-Path $logDir "web_stdout.log"
$stderrLog = Join-Path $logDir "web_stderr.log"

New-Item -ItemType Directory -Path $logDir -Force | Out-Null

function Test-LocalWebHealth {
    # Native curl is verified in both PowerShell versions on this machine.
    # Explicit loopback proxy bypass; do not alter system proxy settings.
    $status = & curl.exe --noproxy '*' --connect-timeout 1 --max-time 2 --silent --output NUL --write-out '%{http_code}' 'http://127.0.0.1:8765/api/health'
    return ($LASTEXITCODE -eq 0 -and $status -eq '200')
}

try {
    if (Test-LocalWebHealth) {
        Write-Host "Research assistant is already running at http://127.0.0.1:8765/"
        exit 0
    }
} catch {
    # No healthy local endpoint yet, so start the service below.
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Project Python was not found: $pythonPath"
}

$process = Start-Process `
    -FilePath $pythonPath `
    -ArgumentList "web_app.py" `
    -WorkingDirectory $projectDir `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -WindowStyle Hidden `
    -PassThru

$startupTimer = [System.Diagnostics.Stopwatch]::StartNew()
while ($startupTimer.Elapsed.TotalSeconds -lt 60) {
    Start-Sleep -Seconds 1
    if ($process.HasExited) {
        $detail = if (Test-Path $stderrLog) {
            (Get-Content $stderrLog -Tail 20) -join [Environment]::NewLine
        } else {
            "No stderr log was created."
        }
        throw "Research assistant failed to start.`n$detail"
    }
    try {
        if (Test-LocalWebHealth) {
            Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii
            Write-Host "Research assistant started. PID: $($process.Id)"
            Write-Host "Web page: http://127.0.0.1:8765/"
            exit 0
        }
    } catch {
        # The Python environment needs several seconds to import model libraries.
    }
}

Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
throw "Research assistant did not become healthy within 60 seconds. Log: $stderrLog"
