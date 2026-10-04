# scripts/demos/failed_job.ps1
# Automates M2 failed job and retry budget exhaustion demo:
# Submits a job with max_retries = 1 that exits with code 42,
# observes attempt 1 failure and backoff, observes attempt 2 failure,
# verifies terminal FAILED state when budget is exhausted, and writes raw evidence.

param(
    [int]$Port = 8088,
    [string]$DatabaseUrl = "postgres://hamicloud:hamicloud_secret@localhost:5432/hamicloud?sslmode=disable"
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path "$PSScriptRoot/../..").Path
$RawDir = Join-Path $RootDir "docs/evidence/demos/raw/failed-job"
New-Item -ItemType Directory -Force -Path $RawDir | Out-Null

$ApiUrl = "http://127.0.0.1:$Port"
$env:ENVIRONMENT = "development"
$env:RUNTIME_DATABASE_URL = $DatabaseUrl
$env:DATABASE_URL = $DatabaseUrl.Replace("postgres://", "postgresql+asyncpg://").Split("?")[0]

function Run-Psql([string]$query) {
    if (Get-Command psql -ErrorAction SilentlyContinue) {
        & psql -U hamicloud -d hamicloud -h localhost -c $query
    } else {
        & docker exec hamicloud-postgres psql -U hamicloud -d hamicloud -c $query
    }
}

# 0. Always rebuild runtime binaries to ensure running latest code
$schedulerExe = Join-Path $RootDir "bin/hamicloud-scheduler.exe"
$executorExe = Join-Path $RootDir "bin/hamicloud-executor.exe"
Write-Host "Building Go runtime binaries..."
Push-Location (Join-Path $RootDir "runtime")
go build -o ../bin/hamicloud-scheduler.exe ./cmd/hamicloud-scheduler
go build -o ../bin/hamicloud-executor.exe ./cmd/hamicloud-executor
Pop-Location

# 1. Ensure API server is running
$apiProcess = $null
$startedApi = $false
try {
    $null = Invoke-RestMethod -Uri "$ApiUrl/healthz" -Method GET -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
    Write-Host "API server already reachable at $ApiUrl"
} catch {
    Write-Host "Starting API server on port $Port..."
    $pythonExe = Join-Path $RootDir ".venv/Scripts/python.exe"
    $apiProcess = Start-Process -FilePath $pythonExe -ArgumentList "-m", "uvicorn", "app.main:app", "--port", "$Port", "--host", "127.0.0.1" -WorkingDirectory (Join-Path $RootDir "apps/api") -PassThru
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
    $wsSlug = "demo-failed-job-$rnd"
    $wsBody = @{
        name = "Failed Job Demo $rnd"
        slug = $wsSlug
    } | ConvertTo-Json

    $wsRes = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces" -Method POST -UseBasicParsing -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"} -Body $wsBody
    $workspaceId = $wsRes.id
    Write-Host "Created workspace $workspaceId ($wsSlug)"

    # 3. Submit failing job (sys.exit(42), max_retries = 1)
    $idempKey = "idemp-failed-job-$rnd"
    $jobPayload = @{
        name = "fatal-process"
        image_digest = "docker.io/library/python:3.12-alpine"
        command_args = @("python", "-c", "import sys; sys.exit(42)")
        timeout_seconds = 30
        max_retries = 1
    } | ConvertTo-Json

    $submitResRaw = Invoke-WebRequest -Uri "$ApiUrl/v1/workspaces/$workspaceId/jobs" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"; "Idempotency-Key"=$idempKey} `
        -Body $jobPayload
    
    Set-Content -Path (Join-Path $RawDir "01-submit-job.json") -Value $submitResRaw.Content -Encoding utf8
    $submitRes = $submitResRaw.Content | ConvertFrom-Json
    $jobId = $submitRes.operation_id
    Write-Host "Submitted job $jobId with command exit(42) and max_retries=1"

    # 4. Run scheduler to admit attempt 1
    Write-Host "Admitting attempt 1 with scheduler..."
    $schedAdmit1 = & $schedulerExe --run-once 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "02-scheduler-admit-1.log") -Value $schedAdmit1 -Encoding utf8

    # 5. Run executor for attempt 1 with workspace scoping
    Write-Host "Executing attempt 1 with executor (exits with status 42)..."
    $env:WORKLOAD_WORKSPACE_ID = $workspaceId
    $execAttempt1 = & $executorExe --run-once 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "03-executor-attempt-1.log") -Value $execAttempt1 -Encoding utf8

    # 6. Wait 6.0 seconds for retry backoff (> 5.0s backoff)
    Write-Host "Waiting 6.0s for retry backoff..."
    Start-Sleep -Seconds 6

    # 7. Run scheduler requeue & admission of attempt 2
    Write-Host "Requeuing attempt 2 with scheduler..."
    $schedRequeue = & $schedulerExe --run-once 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "04-scheduler-requeue.log") -Value $schedRequeue -Encoding utf8

    # 8. Run executor for attempt 2 (exits 42, budget exhausted -> terminal FAILED)
    Write-Host "Executing attempt 2 with executor (budget exhausted)..."
    $env:WORKLOAD_WORKSPACE_ID = $workspaceId
    $execAttempt2 = & $executorExe --run-once 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "05-executor-attempt-2.log") -Value $execAttempt2 -Encoding utf8

    # 9. Final API state check
    Write-Host "Fetching final job status via API..."
    $finalJobRaw = Invoke-WebRequest -Uri "$ApiUrl/v1/jobs/$jobId" -Method GET -UseBasicParsing -Headers @{"X-Dev-Subject"="alice"}
    Set-Content -Path (Join-Path $RawDir "06-get-job-final.json") -Value $finalJobRaw.Content -Encoding utf8
    $finalJob = $finalJobRaw.Content | ConvertFrom-Json

    # 10. Query database attempts table
    $finalDbAttempts = Run-Psql "SELECT attempt_number, state, exit_code, failure_reason, started_at IS NOT NULL as has_started, finished_at IS NOT NULL as has_finished FROM job_attempts WHERE job_id = '$jobId' ORDER BY attempt_number;" | Out-String
    Set-Content -Path (Join-Path $RawDir "07-db-attempts.txt") -Value $finalDbAttempts -Encoding utf8

    # 11. Strict assertions: job and attempt state validation
    Write-Host "Verifying strict assertions for failed job demo..."
    if ($finalJob.id -ne $jobId) {
        throw "Assertion failed: final job id ($($finalJob.id)) does not match submitted job id ($jobId)!"
    }
    if ($finalJob.state -ne "FAILED") {
        throw "Assertion failed: final job state is '$($finalJob.state)', expected 'FAILED'!"
    }
    if ($finalJob.current_attempt_number -ne 2) {
        throw "Assertion failed: final job current_attempt_number is $($finalJob.current_attempt_number), expected 2!"
    }

    $att1Check = Run-Psql "SELECT state || '|' || COALESCE(exit_code::text, '') FROM job_attempts WHERE job_id = '$jobId' AND attempt_number = 1;" | Out-String
    if ($att1Check -notmatch "FAILED\|42") {
        throw "Assertion failed: Attempt 1 is not FAILED with exit code 42. Actual: $att1Check"
    }

    $att2Check = Run-Psql "SELECT state || '|' || COALESCE(exit_code::text, '') FROM job_attempts WHERE job_id = '$jobId' AND attempt_number = 2;" | Out-String
    if ($att2Check -notmatch "FAILED\|42") {
        throw "Assertion failed: Attempt 2 is not FAILED with exit code 42. Actual: $att2Check"
    }

    Write-Host "Failed job demo completed successfully with all assertions passing. Raw output written to $RawDir"
} finally {
    if ($startedApi -and $apiProcess) {
        Write-Host "Stopping demo API server..."
        Stop-Process -Id $apiProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
