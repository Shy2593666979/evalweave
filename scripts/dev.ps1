$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is not running. Start Docker Desktop and run this command again."
}
uv sync
corepack pnpm --dir web install --frozen-lockfile
docker compose -f deploy/docker-compose.yaml up -d --wait

$backendProcess = Start-Process -FilePath "uv" -ArgumentList "run", "evalweave-api", "--config", "config/application.yaml" -WorkingDirectory $repoRoot -NoNewWindow -PassThru
$workerProcess = Start-Process -FilePath "uv" -ArgumentList "run", "evalweave-worker", "--config", "config/application.yaml" -WorkingDirectory $repoRoot -NoNewWindow -PassThru

try {
    corepack pnpm --dir web dev
} finally {
    foreach ($process in @($backendProcess, $workerProcess)) {
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id
        }
    }
}
