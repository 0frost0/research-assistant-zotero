$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonPath = if ($env:RESEARCH_ASSISTANT_PYTHON) { $env:RESEARCH_ASSISTANT_PYTHON } else { "D:\miniconda3\envs\deepagents\python.exe" }

# Unit tests first; the benchmark uses Qwen/Qdrant but never the answer-generation API.
& $pythonPath -B -X utf8 -m unittest discover -s (Join-Path $projectDir "tests") -t $projectDir -p "test*.py"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $pythonPath -B -X utf8 (Join-Path $projectDir "benchmarks\run_retrieval_evaluation.py") --fail-on-regression
exit $LASTEXITCODE
