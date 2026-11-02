$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
# Reuse the existing named volume. Never remove/recreate user data.
docker volume create research-assistant-qdrant-data
if ($LASTEXITCODE -ne 0) { throw 'Docker is unavailable. Start Docker Desktop first.' }
docker compose -f "$projectDir/deployment/qdrant/compose.qdrant.yaml" up -d
if ($LASTEXITCODE -ne 0) { throw 'Qdrant startup failed; existing data retained.' }
