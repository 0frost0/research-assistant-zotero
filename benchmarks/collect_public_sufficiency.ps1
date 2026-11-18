param(
    [string]$SshHost = 'research-eval',
    [string]$RemoteRoot = '/home/researcher/workspace/research-assistant-zotero-github/runtime/public-sufficiency-v1-20261115',
    [string]$Data = 'output/public-sufficiency-v1-20261115/data',
    [Parameter(Mandatory = $true)][string]$Destination,
    [string]$Python = 'D:\miniconda3\envs\deepagents\python.exe'
)
# One bounded transfer attempt. Never starts/restarts inference or waits on a PID.
$ErrorActionPreference = 'Stop'
if (Test-Path $Destination) { throw 'Use a new destination; previous receipts are preserved.' }
if (-not (Test-Path $Python)) { throw 'Python executable missing.' }
if (-not (Test-Path "$Data/manifest.json")) { throw 'Frozen data manifest missing.' }
New-Item -ItemType Directory -Path $Destination | Out-Null
foreach ($run in @('qwen3-8b-dev-v2', 'qwen3-8b-heldout-v1')) {
    $target = Join-Path $Destination $run
    New-Item -ItemType Directory -Path $target | Out-Null
    foreach ($name in @('complete.json', 'manifest.json', 'predictions.jsonl')) {
        & scp -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 "${SshHost}:${RemoteRoot}/${run}/${name}" $target
        if ($LASTEXITCODE -ne 0) { throw "Receipt transfer failed: $run/$name. No quality result has been produced." }
    }
}
& $Python "$PSScriptRoot/finalize_public_sufficiency.py" --data $Data --development "$Destination/qwen3-8b-dev-v2" --heldout "$Destination/qwen3-8b-heldout-v1" --output "$Destination/scored"
if ($LASTEXITCODE -ne 0) { throw 'Final verification failed. Downloaded files preserved for diagnosis.' }
Write-Output "Verified report: $Destination/scored/summary.json"
