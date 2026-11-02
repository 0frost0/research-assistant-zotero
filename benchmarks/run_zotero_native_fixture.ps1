param(
  [string]$Target = "output/zotero/native-fixture-run",
  [int]$FixturePort = 8771,
  [int]$DebuggerPort = 2832,
  [string]$Python = "D:\miniconda3\envs\deepagents\python.exe",
  [string]$Zotero = "C:\Program Files\Zotero\zotero.exe"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\")).Path
$targetPath = [IO.Path]::GetFullPath((Join-Path $root $Target))
$targetProfile = Join-Path $targetPath "profile"
$server = $null
$zoteroProcess = $null

function Wait-Port([int]$Port, [int]$Seconds = 30) {
  for ($i = 0; $i -lt $Seconds; $i++) {
    if (Test-NetConnection 127.0.0.1 -Port $Port -InformationLevel Quiet) { return }
    Start-Sleep -Seconds 1
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

  $server = Start-Process -FilePath $Python -ArgumentList @(
    "benchmarks/serve_knowledge_fixture.py", "--port", $FixturePort, "--qa-scripted"
  ) -WorkingDirectory $root -RedirectStandardOutput (Join-Path $root "output/zotero/fixture-server.log") `
    -RedirectStandardError (Join-Path $root "output/zotero/fixture-server.err.log") -WindowStyle Hidden -PassThru
  Wait-Port $FixturePort

  & $Python "benchmarks/prepare_zotero_fixture.py" --target $targetPath
  $zoteroProcess = Start-Process -FilePath $Zotero -ArgumentList @(
    "-no-remote", "-profile", $targetProfile, "-start-debugger-server", $DebuggerPort
  ) -WindowStyle Hidden -PassThru
  Wait-Port $DebuggerPort
  & $Python "benchmarks/install_zotero_fixture.py" --fixture-root $targetPath

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
