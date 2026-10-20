# Milestone M5 — Operational Evidence Live Demo & Benchmarks
# Executes live API/Scheduler/Kubernetes benchmarks, PostgreSQL+Artifact restore drill,
# controller failure recovery drill, and alert-to-runbook exercise.

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$Port = 8091
$ApiUrl = "http://127.0.0.1:$Port"

$env:ENVIRONMENT = "development"
$env:DATABASE_URL = "postgresql+asyncpg://hamicloud:hamicloud_secret@localhost:5432/hamicloud"
$env:DATABASE_URL_SYNC = "postgresql://hamicloud:hamicloud_secret@localhost:5432/hamicloud"
$env:REDIS_URL = "redis://localhost:6379/0"
$env:NATS_URL = "nats://localhost:4222"
$env:OUTBOX_DISPATCHER_ENABLED = "true"
$env:WORKLOAD_RUNNER_MODE = "kubernetes"
$env:WORKLOAD_NAMESPACE = "default"
$env:INGRESS_BASE_DOMAIN = "hamicloud.local"
$env:RATE_LIMIT_REQUESTS_PER_MINUTE = "1000"
$env:HAMICLOUD_API_URL = $ApiUrl

Write-Host "Building Go runtime binaries..."
Push-Location (Join-Path $RootDir "runtime")
go build -o ../bin/hamicloud-scheduler.exe ./cmd/hamicloud-scheduler
go build -o ../bin/hamicloud-executor.exe ./cmd/hamicloud-executor
Pop-Location

$pythonExe = Join-Path $RootDir ".venv/Scripts/python.exe"
$apiProcess = $null
$startedApi = $false

try {
    try {
        $null = Invoke-RestMethod -Uri "$ApiUrl/healthz" -Method GET -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        Write-Host "API server already reachable at $ApiUrl"
    } catch {
        Write-Host "Starting API server on port $Port..."
        $apiProcess = Start-Process -FilePath $pythonExe `
            -ArgumentList "-m", "uvicorn", "app.main:app", "--port", "$Port", "--host", "127.0.0.1" `
            -WorkingDirectory (Join-Path $RootDir "apps/api") -PassThru
        $startedApi = $true

        $ready = $false
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Milliseconds 500
            try {
                $h = Invoke-RestMethod -Uri "$ApiUrl/healthz" -Method GET -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
                if ($h.status -eq "ok") {
                    $ready = $true
                    break
                }
            } catch {}
        }
        if (-not $ready) {
            throw "Failed to start API server on port $Port"
        }
        Write-Host "API server ready on port $Port."
    }

    & $pythonExe (Join-Path $PSScriptRoot "m5_runner.py")
    if ($LASTEXITCODE -ne 0) {
        throw "m5_runner.py failed with exit code $LASTEXITCODE"
    }
    Write-Host "M5 Operational Evidence Demo & Benchmarks Completed Successfully!"
} finally {
    if ($startedApi -and $apiProcess -and -not $apiProcess.HasExited) {
        Stop-Process -Id $apiProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
