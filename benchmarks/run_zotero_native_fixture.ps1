param(
  [string]$Target = "output/zotero/native-fixture-run",
  [int]$FixturePort = 8771,
  [int]$DebuggerPort = 2832,
  [switch]$LiveModels,
  [switch]$VisualFixture,
  [switch]$VisualLive,
  [switch]$SufficiencyFixture,
  [string]$ModelConnection = "",
  [ValidateSet("tfidf", "multimodal")][string]$Backend = "tfidf",
  [string]$Python = "D:\miniconda3\envs\deepagents\python.exe",
  [string]$Zotero = "C:\Program Files\Zotero\zotero.exe"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\")).Path.TrimEnd([char[]]"\/")
$targetPath = [IO.Path]::GetFullPath((Join-Path $root $Target))
$targetProfile = Join-Path $targetPath "profile"
$server = $null
$zoteroProcess = $null

function Wait-Port([int]$Port, [int]$Seconds = 30) {
  $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
  while ([DateTime]::UtcNow -lt $deadline) {
    $client = [Net.Sockets.TcpClient]::new()
    try {
      if ($client.ConnectAsync('127.0.0.1', $Port).Wait(500) -and $client.Connected) { return }
    } catch {} finally { $client.Dispose() }
    Start-Sleep -Milliseconds 250
  }
  throw "端口 $Port 在 $Seconds 秒内没有 ready。"
}

function Assert-PortFree([int]$Port) {
  if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    throw "端口 $Port 已被占用；为保护正式服务，验收脚本不会终止现有进程。"
  }
}

try {
  if (-not (Test-Path -LiteralPath $Python)) { throw "Python 不存在：$Python" }
  if (-not (Test-Path -LiteralPath $Zotero)) { throw "Zotero 不存在：$Zotero" }
  Assert-PortFree $FixturePort
  Assert-PortFree $DebuggerPort
  if (-not $targetPath.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "验收输出必须位于当前项目目录内。"
  }
  if ($LiveModels -and -not $ModelConnection) { throw "真实模型验收必须明确填写 ModelConnection。" }
  if ($VisualFixture -and ($LiveModels -or $Backend -ne "tfidf")) { throw "视觉 fixture 仅允许非付费 scripted + tfidf 路径。" }
  if ($VisualLive -and (-not $VisualFixture -or -not $ModelConnection -or $LiveModels)) { throw "VisualLive 必须配合 VisualFixture 和明确的 ModelConnection；最多 2 次真实视觉调用。" }
  if ($SufficiencyFixture -and ($LiveModels -or $VisualLive)) { throw "充分性 fixture 只允许本地 scripted 路径，不增加真实模型调用。" }
  $qaTimeout = if ($LiveModels -or $VisualLive) { 180 } else { 30 }
  $prepareArgs = @("benchmarks/prepare_zotero_fixture.py", "--target", $targetPath, "--fixture-port", $FixturePort, "--qa-timeout", $qaTimeout)
  if ($VisualFixture) { $prepareArgs += "--visual-fixture" }
  if ($SufficiencyFixture) { $prepareArgs += "--sufficiency-fixture" }
  & $Python @prepareArgs
  if ($LASTEXITCODE -ne 0) { throw "隔离 profile 准备失败。" }
  $serverArgs = @("benchmarks/serve_knowledge_fixture.py", "--port", $FixturePort,
    "--root", (Join-Path $targetPath "backend"), "--backend", $Backend)
  if ($LiveModels) { $serverArgs += @("--qa-live", "--model-connection", $ModelConnection) }
  else { $serverArgs += "--qa-scripted" }
  if ($VisualFixture) { $serverArgs += "--visual-fixture" }
  if ($VisualLive) { $serverArgs += @("--visual-live", "--model-connection", $ModelConnection) }
  $server = Start-Process -FilePath $Python -ArgumentList $serverArgs -WorkingDirectory $root `
    -RedirectStandardOutput (Join-Path $targetPath "server.log") `
    -RedirectStandardError (Join-Path $targetPath "server.err.log") -WindowStyle Hidden -PassThru
  Wait-Port $FixturePort
  $zoteroProcess = Start-Process -FilePath $Zotero -ArgumentList @(
    "-no-remote", "-profile", $targetProfile, "-start-debugger-server", $DebuggerPort
  ) -WindowStyle Hidden -PassThru
  Wait-Port $DebuggerPort
  & $Python "benchmarks/install_zotero_fixture.py" --fixture-root $targetPath --debugger-port $DebuggerPort
  if ($LASTEXITCODE -ne 0) { throw "隔离插件安装失败，已停止后续等待。" }

  $resultPath = Join-Path $targetPath "result.json"
  for ($i = 0; $i -lt 180; $i++) {
    if (Test-Path -LiteralPath $resultPath) {
      $result = Get-Content -LiteralPath $resultPath -Raw -Encoding UTF8 | ConvertFrom-Json
      if ($result.phase -in @("passed", "failed")) {
        $result | ConvertTo-Json -Depth 10
        if ($result.phase -ne "passed") { throw "隔离 Zotero 验收失败：$($result.error)" }
        return
      }
    }
    Start-Sleep -Seconds 1
  }
  throw "等待隔离 Zotero 验收结果超时。"
}
finally {
  if ($zoteroProcess) {
    Get-CimInstance Win32_Process | Where-Object {
      $_.Name -eq "zotero.exe" -and ($_.ProcessId -eq $zoteroProcess.Id -or $_.CommandLine -like "*$targetPath*")
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  }
  if ($server) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
}
