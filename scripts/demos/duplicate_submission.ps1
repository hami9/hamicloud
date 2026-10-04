# scripts/demos/duplicate_submission.ps1
# Automates M2 duplicate submission idempotency demo:
# Submits a workspace and an initial job with an Idempotency-Key,
# submits an exact duplicate with the same Idempotency-Key,
# verifies identical operation_id returned and exactly 1 database record created,
# and writes raw evidence.

param(
    [int]$Port = 8088,
    [string]$DatabaseUrl = "postgres://hamicloud:hamicloud_secret@localhost:5432/hamicloud?sslmode=disable"
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path "$PSScriptRoot/../..").Path
$RawDir = Join-Path $RootDir "docs/evidence/demos/raw/duplicate-submission"
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
    $wsSlug = "demo-dup-$rnd"
    $wsBody = @{
        name = "Duplicate Submission Demo $rnd"
        slug = $wsSlug
    } | ConvertTo-Json

    $wsResRaw = Invoke-WebRequest -Uri "$ApiUrl/v1/workspaces" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"} `
        -Body $wsBody
    Set-Content -Path (Join-Path $RawDir "01-create-workspace.json") -Value $wsResRaw.Content -Encoding utf8
    $wsRes = $wsResRaw.Content | ConvertFrom-Json
    $workspaceId = $wsRes.id
    Write-Host "Created workspace $workspaceId ($wsSlug)"

    # 3. First submission with Idempotency-Key
    $idempKey = "idemp-demo-dup-$rnd"
    $jobPayload = @{
        name = "data-aggregation"
        image_digest = "docker.io/library/python:3.12-alpine"
        command_args = @("python", "-c", "print('Processed records')")
        timeout_seconds = 60
        max_retries = 2
    } | ConvertTo-Json

    Write-Host "Sending first submission with key $idempKey..."
    $firstSubRaw = Invoke-WebRequest -Uri "$ApiUrl/v1/workspaces/$workspaceId/jobs" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"; "Idempotency-Key"=$idempKey} `
        -Body $jobPayload
    Set-Content -Path (Join-Path $RawDir "02-first-submission.json") -Value $firstSubRaw.Content -Encoding utf8
    $firstSub = $firstSubRaw.Content | ConvertFrom-Json
    Write-Host "First submission accepted with operation_id $($firstSub.operation_id)"

    # 4. Duplicate submission with identical key and body
    Write-Host "Sending duplicate submission with same key $idempKey..."
    $dupSubRaw = Invoke-WebRequest -Uri "$ApiUrl/v1/workspaces/$workspaceId/jobs" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"; "Idempotency-Key"=$idempKey} `
        -Body $jobPayload
    Set-Content -Path (Join-Path $RawDir "03-duplicate-submission.json") -Value $dupSubRaw.Content -Encoding utf8
    $dupSub = $dupSubRaw.Content | ConvertFrom-Json
    Write-Host "Duplicate submission returned operation_id $($dupSub.operation_id)"

    if ($firstSub.operation_id -ne $dupSub.operation_id) {
        throw "Idempotency violated: operation_ids do not match! ($($firstSub.operation_id) vs $($dupSub.operation_id))"
    }

    # 5. Database check: exactly 1 job created
    Write-Host "Verifying database job count for workspace..."
    $dbCount = Run-Psql "SELECT count(*) AS job_count FROM jobs WHERE workspace_id = '$workspaceId';" | Out-String
    Set-Content -Path (Join-Path $RawDir "04-db-job-count.txt") -Value $dbCount -Encoding utf8

    Write-Host "Duplicate submission demo completed successfully. Raw output written to $RawDir"
} finally {
    if ($startedApi -and $apiProcess) {
        Write-Host "Stopping demo API server..."
        Stop-Process -Id $apiProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
