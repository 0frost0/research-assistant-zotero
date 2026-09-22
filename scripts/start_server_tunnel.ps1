param([int]$LocalPort = 6333)
$ErrorActionPreference = 'Stop'
if ($LocalPort -lt 1024 -or $LocalPort -gt 65535) { throw 'Invalid local port' }
$projectDir = Split-Path -Parent $PSScriptRoot
# Web runs locally again; only this project's Qdrant uses this old-server tunnel.
if ($LocalPort -ne 6333) { throw 'Web is local. This script forwards old-server Qdrant on port 6333 only.' }
$arguments = @('-N','-o','BatchMode=yes','-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3','-L',"127.0.0.1:${LocalPort}:127.0.0.1:6333",'siat-pro6000d-dingone')
$process = Start-Process -FilePath ssh -ArgumentList $arguments -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 1
if ($process.HasExited) { throw 'SSH tunnel failed; check whether the local port is already occupied' }
$process.Id | Set-Content -LiteralPath (Join-Path $projectDir ".server_tunnel_${LocalPort}.pid") -Encoding ascii
Write-Output "Old-server Qdrant: http://127.0.0.1:$LocalPort (SSH PID $($process.Id))"
