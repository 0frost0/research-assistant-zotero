$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
$python = if ($env:RESEARCH_ASSISTANT_PYTHON) { $env:RESEARCH_ASSISTANT_PYTHON } else { "D:\miniconda3\envs\deepagents\python.exe" }
$translationDir = Join-Path $projectDir ".translation_env"
if (-not (Test-Path (Join-Path $translationDir "Scripts/python.exe"))) {
    & $python -m venv $translationDir
    if ($LASTEXITCODE) { throw "Cannot create isolated translation environment" }
}
& (Join-Path $translationDir "Scripts/python.exe") -m pip install -r (Join-Path $projectDir "deployment/translation/requirements-lock.txt")
if ($LASTEXITCODE) { throw "Translation dependency installation failed" }
& (Join-Path $translationDir "Scripts/python.exe") -m pip check
if ($LASTEXITCODE) { throw "Translation dependency versions are incompatible" }
& npm.cmd install --prefix (Join-Path $projectDir ".reader_assets") --ignore-scripts --no-audit --no-fund pdfjs-dist@5.4.149
if ($LASTEXITCODE) { throw "PDF.js installation failed" }
Write-Host "Reader assets and isolated translation runtime installed. Configure translation before use."
