param(
  [string]$Target = "output/zotero/real-questions-python-20261118",
  [int]$Port = 8776,
  [int]$DebuggerPort = 2837,
  [string]$Python = "D:\miniconda3\envs\deepagents\python.exe",
  [switch]$GoBackend
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..").Path
$targetPath = (Resolve-Path (Join-Path $root $Target)).Path
$nativeEngine = (Get-Content -Raw -Encoding UTF8 "$targetPath/run-config.json" | ConvertFrom-Json).native_engine
if ($nativeEngine -and $GoBackend) { throw "Native engine mode starts its own Go backend" }
if (Test-Path (Join-Path $targetPath "result.json")) { throw "Refusing completed or partial run directory; prepare a new target" }
$server = $null
$zotero = $null
$worker = $null
try {
  if ($nativeEngine) { $Port = 8765 }
  $ports = @($Port, $DebuggerPort)
  if ($GoBackend) { $ports += 8778 }
  foreach ($p in $ports) {
    if (Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue) { throw "Port $p occupied" }
  }
  if ($GoBackend) {
    & $Python -c "import secrets,pathlib; pathlib.Path(r'$targetPath/worker-token').write_text(secrets.token_urlsafe(32))"
    $worker = Start-Process $Python -ArgumentList @("-X", "utf8", "-m", "research_assistant.web.inference_worker", "--root", "$Target/backend", "--token-file", "$Target/worker-token") -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$targetPath/worker.log" -RedirectStandardError "$targetPath/worker.err.log"
    $server = Start-Process "$root/output/zotero/researchd.exe" -ArgumentList @("-root", "$Target/backend", "-worker-token-file", "$Target/worker-token", "-listen", "127.0.0.1:$Port") -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$targetPath/server.log" -RedirectStandardError "$targetPath/server.err.log"
  } elseif (-not $nativeEngine) {
    $server = Start-Process $Python -ArgumentList @("-X", "utf8", "scripts/serve_knowledge.py", "--root", "$Target/backend", "--port", $Port) -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$targetPath/server.log" -RedirectStandardError "$targetPath/server.err.log"
  }
  $zotero = Start-Process "C:\Program Files\Zotero\zotero.exe" -ArgumentList @("-no-remote", "-profile", "$targetPath/profile", "-start-debugger-server", $DebuggerPort) -PassThru
  foreach ($p in $ports) {
    if ($nativeEngine -and $p -eq $Port) { continue }
    $ready = $false
    for ($i=0; $i -lt 120; $i++) {
      $client = [Net.Sockets.TcpClient]::new()
      try { $ready = $client.ConnectAsync("127.0.0.1", $p).Wait(500) -and $client.Connected } catch {} finally { $client.Dispose() }
      if ($ready) { break }
      Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw "Port $p not ready" }
  }
  & $Python -X utf8 benchmarks/install_zotero_fixture.py --fixture-root $targetPath --debugger-port $DebuggerPort
  if ($LASTEXITCODE -ne 0) { throw "Native install failed" }
  for ($i=0; $i -lt 1800; $i++) {
    $resultPath = Join-Path $targetPath "result.json"
    if (Test-Path $resultPath) {
      $result = Get-Content -Raw -Encoding UTF8 $resultPath | ConvertFrom-Json
      if ($result.phase -in @("completed", "failed")) {
        $result | ConvertTo-Json -Depth 10
        if ($result.phase -eq "failed") { throw "Native run failed: $($result.error)" }
        return
      }
    }
    Start-Sleep -Seconds 1
  }
  throw "Native run deadline exceeded"
} finally {
  if ($zotero) {
    Get-CimInstance Win32_Process -Filter "Name='zotero.exe'" | Where-Object {
      $_.ProcessId -eq $zotero.Id -or $_.CommandLine -like "*$targetPath*"
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  }
  if ($server) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
  if ($worker) { Stop-Process -Id $worker.Id -Force -ErrorAction SilentlyContinue }
}
