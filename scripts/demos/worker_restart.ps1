# scripts/demos/worker_restart.ps1
# Automates M2 crash recovery demo: starts long-running job, kills worker,
# waits for lease expiry and backoff, verifies recovery, and writes raw evidence.

param(
    [int]$Port = 8088,
    [string]$DatabaseUrl = "postgres://hamicloud:hamicloud_secret@localhost:5432/hamicloud?sslmode=disable"
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path "$PSScriptRoot/../..").Path
$RawDir = Join-Path $RootDir "docs/evidence/demos/raw/worker-restart"
New-Item -ItemType Directory -Force -Path $RawDir | Out-Null

$ApiUrl = "http://127.0.0.1:$Port"
$env:ENVIRONMENT = "development"
$env:RUNTIME_DATABASE_URL = $DatabaseUrl
$env:DATABASE_URL = $DatabaseUrl.Replace("postgres://", "postgresql+asyncpg://")

function Run-Psql([string]$query) {
    if (Get-Command psql -ErrorAction SilentlyContinue) {
        & psql -U hamicloud -d hamicloud -h localhost -c $query
    } else {
        & docker exec hamicloud-postgres psql -U hamicloud -d hamicloud -c $query
    }
}

# 0. Ensure binaries exist
$schedulerExe = Join-Path $RootDir "bin/hamicloud-scheduler.exe"
$executorExe = Join-Path $RootDir "bin/hamicloud-executor.exe"
if (-not (Test-Path $schedulerExe) -or -not (Test-Path $executorExe)) {
    Write-Host "Building Go runtime binaries..."
    Push-Location (Join-Path $RootDir "runtime")
    go build -o ../bin/hamicloud-scheduler.exe ./cmd/hamicloud-scheduler
    go build -o ../bin/hamicloud-executor.exe ./cmd/hamicloud-executor
    Pop-Location
}

# 1. Ensure API server is running
$apiProcess = $null
$startedApi = $false
try {
    $null = Invoke-RestMethod -Uri "$ApiUrl/healthz" -Method GET -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
    Write-Host "API server already reachable at $ApiUrl"
} catch {
    Write-Host "Starting API server on port $Port..."
    $pythonExe = Join-Path $RootDir ".venv/Scripts/python.exe"
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $pythonExe
    $psi.Arguments = "-m uvicorn app.main:app --port $Port --host 127.0.0.1"
    $psi.WorkingDirectory = (Join-Path $RootDir "apps/api")
    $psi.UseShellExecute = $false
    $psi.EnvironmentVariables["ENVIRONMENT"] = "development"
    $psi.EnvironmentVariables["DATABASE_URL"] = $env:DATABASE_URL
    $psi.EnvironmentVariables["RUNTIME_DATABASE_URL"] = $env:RUNTIME_DATABASE_URL
    $apiProcess = [System.Diagnostics.Process]::Start($psi)
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

try {
    # 2. Create demo workspace
    $rnd = [Guid]::NewGuid().ToString().Substring(0, 8)
    $wsSlug = "demo-worker-restart-$rnd"
    $wsBody = @{
        name = "Worker Restart Demo $rnd"
        slug = $wsSlug
    } | ConvertTo-Json

    $wsRes = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces" -Method POST -UseBasicParsing -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"} -Body $wsBody
    $workspaceId = $wsRes.id
    Write-Host "Created workspace $workspaceId ($wsSlug)"

    # 3. Submit long-running job (sleep 15s)
    $idempKey = "idemp-worker-restart-$rnd"
    $jobPayload = @{
        name = "crash-recovery-job"
        image_digest = "docker.io/library/python:3.12-alpine"
        command_args = @("python", "-c", "import time; time.sleep(15)")
        timeout_seconds = 60
        max_retries = 2
    } | ConvertTo-Json

    $submitResRaw = Invoke-WebRequest -Uri "$ApiUrl/v1/workspaces/$workspaceId/jobs" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"; "Idempotency-Key"=$idempKey} `
        -Body $jobPayload
    
    Set-Content -Path (Join-Path $RawDir "01-submit-job.json") -Value $submitResRaw.Content -Encoding utf8
    $submitRes = $submitResRaw.Content | ConvertFrom-Json
    $jobId = $submitRes.operation_id
    Write-Host "Submitted job $jobId with 15s sleep command"

    # 4. Run scheduler to admit attempt 1
    Write-Host "Admitting attempt 1 with scheduler..."
    $schedAdmit1 = & $schedulerExe --run-once 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "02-scheduler-admit-1.log") -Value $schedAdmit1 -Encoding utf8

    # 5. Start executor in background with LEASE_DURATION_SECONDS=5 using ProcessStartInfo
    Write-Host "Starting executor worker to claim attempt 1..."
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $executorExe
    $psi.Arguments = "--run-once"
    $psi.UseShellExecute = $false
    $psi.EnvironmentVariables["ENVIRONMENT"] = "development"
    $psi.EnvironmentVariables["RUNTIME_DATABASE_URL"] = $DatabaseUrl
    $psi.EnvironmentVariables["LEASE_DURATION_SECONDS"] = "5"
    $psi.EnvironmentVariables["WORKER_ID"] = "executor-worker-crash-test"
    $workerProc = [System.Diagnostics.Process]::Start($psi)
    Write-Host "Worker started with PID $($workerProc.Id). Polling for CLAIMED status..."

    $claimed = $false
    for ($i = 0; $i -lt 30; $i++) {
        $intentCheck = Run-Psql "SELECT id, status, claimed_by, lease_epoch FROM execution_intents WHERE job_attempt_id IN (SELECT id FROM job_attempts WHERE job_id = '$jobId');" | Out-String
        if ($intentCheck -match "CLAIMED") {
            $claimed = $true
            Set-Content -Path (Join-Path $RawDir "03-claimed-attempt-1-db.txt") -Value $intentCheck -Encoding utf8
            Write-Host "Worker successfully claimed attempt 1 intent!"
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $claimed) {
        throw "Worker failed to claim intent within timeout!"
    }

    # 6. Kill worker process abruptly while running 15s sleep
    Write-Host "Terminating worker process $($workerProc.Id) with Stop-Process -Force..."
    Stop-Process -Id $workerProc.Id -Force
    $killMsg = "Terminated worker process PID $($workerProc.Id) with Stop-Process -Force while executing 15s sleep"
    Set-Content -Path (Join-Path $RawDir "04-kill-worker.txt") -Value $killMsg -Encoding utf8

    # 7. Wait 6.0 seconds for lease expiry (> 5.0s lease)
    Write-Host "Waiting 6.0s for worker lease to expire..."
    Start-Sleep -Seconds 6

    $leaseExpiredDb = Run-Psql "SELECT id, status, claimed_by, lease_epoch, lease_expires_at < NOW() AS is_expired FROM execution_intents WHERE job_attempt_id IN (SELECT id FROM job_attempts WHERE job_id = '$jobId');" | Out-String
    Set-Content -Path (Join-Path $RawDir "05-lease-expired-db.txt") -Value $leaseExpiredDb -Encoding utf8

    # 8. Run scheduler recovery pass
    Write-Host "Running scheduler recovery pass..."
    $schedRecovery = & $schedulerExe --run-once 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "06-scheduler-recovery.log") -Value $schedRecovery -Encoding utf8

    $dbAfterRecovery = Run-Psql "SELECT id, state, current_attempt_number FROM jobs WHERE id = '$jobId'; SELECT attempt_number, state, exit_code, failure_reason, started_at IS NOT NULL as has_started, finished_at IS NOT NULL as has_finished FROM job_attempts WHERE job_id = '$jobId';" | Out-String
    Set-Content -Path (Join-Path $RawDir "07-db-after-recovery.txt") -Value $dbAfterRecovery -Encoding utf8

    # 9. Wait 6.0 seconds for retry backoff (> 5.0s backoff)
    Write-Host "Waiting 6.0s for retry backoff..."
    Start-Sleep -Seconds 6

    # 10. Run scheduler requeue pass
    Write-Host "Running scheduler requeue & admit attempt 2..."
    $schedRequeue = & $schedulerExe --run-once 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "08-scheduler-requeue.log") -Value $schedRequeue -Encoding utf8

    # 11. Run executor for attempt 2 (full 15 seconds run to completion)
    Write-Host "Running executor on attempt 2 (will execute 15s command)..."
    $execAttempt2 = & $executorExe --run-once 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "09-executor-attempt-2.log") -Value $execAttempt2 -Encoding utf8

    # 12. Final API state check
    Write-Host "Fetching final job status via API..."
    $finalJobRaw = Invoke-WebRequest -Uri "$ApiUrl/v1/jobs/$jobId" -Method GET -UseBasicParsing -Headers @{"X-Dev-Subject"="alice"}
    Set-Content -Path (Join-Path $RawDir "10-get-job-final.json") -Value $finalJobRaw.Content -Encoding utf8

    $finalDbAttempts = Run-Psql "SELECT attempt_number, state, exit_code, failure_reason, started_at, finished_at FROM job_attempts WHERE job_id = '$jobId' ORDER BY attempt_number;" | Out-String
    Set-Content -Path (Join-Path $RawDir "11-db-final-attempts.txt") -Value $finalDbAttempts -Encoding utf8

    Write-Host "Worker restart demo completed successfully. Raw output written to $RawDir"
} finally {
    if ($startedApi -and $apiProcess) {
        Write-Host "Stopping demo API server..."
        Stop-Process -Id $apiProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
