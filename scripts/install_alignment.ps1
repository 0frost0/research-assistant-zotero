$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$alignmentPython = Join-Path $projectDir '.alignment_env/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $alignmentPython)) {
    & 'D:/miniconda3/envs/deepagents/python.exe' -m venv (Join-Path $projectDir '.alignment_env')
    if ($LASTEXITCODE) { throw 'Failed to create alignment environment' }
}
& $alignmentPython -m pip install -r (Join-Path $projectDir 'requirements-alignment.txt')
if ($LASTEXITCODE) { throw 'Failed to install alignment dependencies' }
& $alignmentPython (Join-Path $PSScriptRoot 'install_alignment.py')
if ($LASTEXITCODE) { throw 'Failed to download alignment model' }
