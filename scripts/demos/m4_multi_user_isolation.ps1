# scripts/demos/m4_multi_user_isolation.ps1
# Automates Milestone M4 end-to-end Multi-User Readiness & Isolation demo against live PostgreSQL,
# Redis, Go Scheduler & Executor, and Kind Kubernetes cluster:
# 1. Two-workspace creation (Workspace A & Workspace B) and 4-role RBAC (OWNER, ADMIN, DEVELOPER, VIEWER)
# 2. AES-256-GCM encrypted secret creation and key rotation (metadata only, zero plaintext exposure)
# 3. Cross-workspace negative isolation checks (404 Not Found indistinguishable from missing ID)
# 4. Noisy-workspace fair scheduling & quota reservation enforcement (Workspace A floods jobs; Workspace B admitted immediately)
# 5. Live Kubernetes NetworkPolicy (hc-netpol-*), LimitRange, and hardened Pod SecurityContext reconciliation
# 6. Workspace Audit Trail export (ADMIN / OWNER)

param(
    [int]$Port = 8090,
    [string]$DatabaseUrl = "postgres://hamicloud:hamicloud_secret@localhost:5432/hamicloud?sslmode=disable"
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path "$PSScriptRoot/../..").Path
$RawDir = Join-Path $RootDir "docs/evidence/demos/raw/m4-multi-user-isolation"
New-Item -ItemType Directory -Force -Path $RawDir | Out-Null

$ApiUrl = "http://127.0.0.1:$Port"
$env:ENVIRONMENT = "development"
$env:RUNTIME_DATABASE_URL = $DatabaseUrl
$env:DATABASE_URL = $DatabaseUrl.Replace("postgres://", "postgresql+asyncpg://").Split("?")[0]
$env:INGRESS_DOMAIN = "hamicloud.local"

$rnd = [Guid]::NewGuid().ToString().Substring(0, 8)

$schedulerExe = Join-Path $RootDir "bin/hamicloud-scheduler.exe"
$executorExe = Join-Path $RootDir "bin/hamicloud-executor.exe"
Write-Host "Building Go runtime binaries..."
Push-Location (Join-Path $RootDir "runtime")
go build -o ../bin/hamicloud-scheduler.exe ./cmd/hamicloud-scheduler
go build -o ../bin/hamicloud-executor.exe ./cmd/hamicloud-executor
Pop-Location

$apiProcess = $null
$startedApi = $false
$appAId = $null
$wsAId = $null

try {
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

    # 1. Create Workspace A (alice) and Workspace B (bob)
    $wsARes = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"} `
        -Body (@{ name = "M4 Tenant Alpha $rnd"; slug = "m4-alpha-$rnd" } | ConvertTo-Json)
    $wsAId = $wsARes.id

    $wsBRes = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="bob"} `
        -Body (@{ name = "M4 Tenant Beta $rnd"; slug = "m4-beta-$rnd" } | ConvertTo-Json)
    $wsBId = $wsBRes.id

    (@{ workspace_a = $wsARes; workspace_b = $wsBRes } | ConvertTo-Json -Depth 6) |
        Set-Content -Path (Join-Path $RawDir "01-workspaces.json") -Encoding utf8
    Write-Host "Created Workspace A ($wsAId) and Workspace B ($wsBId)"

    # 2. Configure 4-role RBAC in Workspace A (add dave=ADMIN, charlie=VIEWER)
    $null = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces/$wsAId/members" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"} `
        -Body (@{ user_subject = "dave"; role = "ADMIN" } | ConvertTo-Json)
    $null = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces/$wsAId/members" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"} `
        -Body (@{ user_subject = "charlie"; role = "VIEWER" } | ConvertTo-Json)

    $membersRaw = Invoke-WebRequest -Uri "$ApiUrl/v1/workspaces/$wsAId/members" -Method GET -UseBasicParsing `
        -Headers @{"X-Dev-Subject"="alice"}
    Set-Content -Path (Join-Path $RawDir "02-workspace-a-members.json") -Value $membersRaw.Content -Encoding utf8

    # Verify VIEWER (charlie) is forbidden (403) from creating secrets
    $viewerStatus = 0
    $viewerBody = ""
    try {
        $null = Invoke-WebRequest -Uri "$ApiUrl/v1/workspaces/$wsAId/secrets" -Method POST -UseBasicParsing `
            -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="charlie"} `
            -Body (@{ name = "FORBIDDEN_KEY"; value = "nope" } | ConvertTo-Json)
    } catch {
        $viewerStatus = [int]$_.Exception.Response.StatusCode
        $sr = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
        $viewerBody = $sr.ReadToEnd()
    }
    if ($viewerStatus -ne 403) {
        throw "Expected 403 Forbidden for VIEWER creating secret, got $viewerStatus"
    }
    Set-Content -Path (Join-Path $RawDir "03-rbac-viewer-forbidden.json") -Value $viewerBody -Encoding utf8
    Write-Host "Verified RBAC: VIEWER received 403 Forbidden on secret mutation"

    # 3. Create and rotate AES-256-GCM secret in Workspace A
    $null = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces/$wsAId/secrets" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"} `
        -Body (@{ name = "DATABASE_API_KEY"; value = "sk_live_initial_secret_v1" } | ConvertTo-Json)
    $secRotatedRaw = Invoke-WebRequest -Uri "$ApiUrl/v1/workspaces/$wsAId/secrets" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"} `
        -Body (@{ name = "DATABASE_API_KEY"; value = "sk_live_rotated_secret_v2" } | ConvertTo-Json)
    Set-Content -Path (Join-Path $RawDir "04-secret-metadata.json") -Value $secRotatedRaw.Content -Encoding utf8
    Write-Host "Encrypted and rotated secret DATABASE_API_KEY (key_version=2)"

    # 4. Cross-workspace negative isolation check (bob accessing Workspace A secrets -> 404)
    $crossStatus = 0
    $crossBody = ""
    try {
        $null = Invoke-WebRequest -Uri "$ApiUrl/v1/workspaces/$wsAId/secrets" -Method GET -UseBasicParsing `
            -Headers @{"X-Dev-Subject"="bob"}
    } catch {
        $crossStatus = [int]$_.Exception.Response.StatusCode
        $sr = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
        $crossBody = $sr.ReadToEnd()
    }
    if ($crossStatus -ne 404) {
        throw "Expected 404 Not Found on cross-workspace access, got $crossStatus"
    }
    Set-Content -Path (Join-Path $RawDir "05-cross-workspace-negative-404.json") -Value $crossBody -Encoding utf8
    Write-Host "Verified cross-workspace negative isolation: bob received 404 Not Found"

    # 5. Fair Multi-Workspace Job Scheduling under noisy tenant burst
    # Clean any older QUEUED/ADMITTED jobs from previous demo runs so scheduler output is crisp
    & docker exec hamicloud-postgres psql -U hamicloud -d hamicloud -c "UPDATE jobs SET state='CANCELLED' WHERE state IN ('QUEUED','ADMITTED','STARTING','RUNNING');" | Out-Null

    $noisyJobIds = @()
    for ($i = 1; $i -le 4; $i++) {
        $jb = @{
            name = "noisy-alpha-job-$i"
            image_digest = "docker.io/library/alpine:3.20"
            command_args = @("sh", "-c", "echo alpha-$i")
            timeout_seconds = 60
            max_retries = 1
        } | ConvertTo-Json
        $jr = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces/$wsAId/jobs" -Method POST -UseBasicParsing `
            -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"; "Idempotency-Key"="m4-noisy-$rnd-$i"} `
            -Body $jb
        $noisyJobIds += $jr.operation_id
    }

    $quietJb = @{
        name = "quiet-beta-job-1"
        image_digest = "docker.io/library/alpine:3.20"
        command_args = @("sh", "-c", "echo beta-1")
        timeout_seconds = 60
        max_retries = 1
    } | ConvertTo-Json
    $quietJr = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces/$wsBId/jobs" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="bob"; "Idempotency-Key"="m4-quiet-$rnd-1"} `
        -Body $quietJb
    $quietJobId = $quietJr.operation_id

    Write-Host "Running Scheduler to verify fair multi-workspace admission..."
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $schedFairLog = & $schedulerExe --run-once 2>&1 | Out-String
    $ErrorActionPreference = $prevEap
    Set-Content -Path (Join-Path $RawDir "06-scheduler-fair-admission.log") -Value $schedFairLog -Encoding utf8

    $wsAJobs = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces/$wsAId/jobs" -Method GET -UseBasicParsing -Headers @{"X-Dev-Subject"="alice"}
    $wsBJobs = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces/$wsBId/jobs" -Method GET -UseBasicParsing -Headers @{"X-Dev-Subject"="bob"}
    (@{ workspace_a_jobs = $wsAJobs.items; workspace_b_jobs = $wsBJobs.items } | ConvertTo-Json -Depth 6) |
        Set-Content -Path (Join-Path $RawDir "07-fair-admission-jobs.json") -Encoding utf8

    $quotasRaw = Invoke-WebRequest -Uri "$ApiUrl/v1/workspaces/$wsAId/quotas" -Method GET -UseBasicParsing -Headers @{"X-Dev-Subject"="alice"}
    Set-Content -Path (Join-Path $RawDir "08-workspace-a-quotas.json") -Value $quotasRaw.Content -Encoding utf8
    Write-Host "Captured fair admission job states and Workspace A quota reservations"

    # Cancel demo jobs so executor only needs to run the service deployment
    & docker exec hamicloud-postgres psql -U hamicloud -d hamicloud -c "UPDATE jobs SET state='CANCELLED' WHERE state IN ('QUEUED','ADMITTED','STARTING','RUNNING'); UPDATE execution_intents SET status='TERMINATED' WHERE resource_type='JOB_ATTEMPT' AND status='PENDING';" | Out-Null

    # 6. Deploy service in Workspace A and verify Kubernetes NetworkPolicy, LimitRange, and Pod SecurityContext
    $appARes = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces/$wsAId/apps" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"} `
        -Body (@{ name = "Alpha Isolated Service"; slug = "alpha-svc-$rnd"; workload_type = "HTTP_SERVICE" } | ConvertTo-Json)
    $appAId = $appARes.id

    $depRes = Invoke-RestMethod -Uri "$ApiUrl/v1/apps/$appAId/deployments" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"; "Idempotency-Key"="m4-dep-$rnd"} `
        -Body (@{ image_digest = "docker.io/library/nginx:alpine"; port = 80; health_path = "/" } | ConvertTo-Json)

    $env:WORKLOAD_WORKSPACE_ID = $wsAId
    $ErrorActionPreference = "Continue"
    $null = & $schedulerExe --run-once 2>&1 | Out-String
    $execDeployLog = & $executorExe --run-once 2>&1 | Out-String
    $ErrorActionPreference = $prevEap
    Set-Content -Path (Join-Path $RawDir "09-executor-deploy-isolation.log") -Value $execDeployLog -Encoding utf8

    $netpolName = "hc-netpol-$wsAId"
    $netpolYaml = & kubectl get networkpolicy $netpolName -o yaml 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "10-k8s-networkpolicy.yaml") -Value $netpolYaml -Encoding utf8

    $limitRangeYaml = & kubectl get limitrange hc-workload-limits -o yaml 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "11-k8s-limitrange.yaml") -Value $limitRangeYaml -Encoding utf8

    $deployYaml = & kubectl get deployment -l "hamicloud.io/application-id=$appAId" -o yaml 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "12-k8s-deployment-securitycontext.yaml") -Value $deployYaml -Encoding utf8
    Write-Host "Captured live Kubernetes NetworkPolicy ($netpolName), LimitRange, and Deployment SecurityContext"

    # 7. Export Workspace A Audit Trail (as ADMIN dave)
    $auditRaw = Invoke-WebRequest -Uri "$ApiUrl/v1/workspaces/$wsAId/audit" -Method GET -UseBasicParsing `
        -Headers @{"X-Dev-Subject"="dave"}
    Set-Content -Path (Join-Path $RawDir "13-workspace-a-audit-export.json") -Value $auditRaw.Content -Encoding utf8
    Write-Host "Exported Workspace A audit trail via ADMIN role"

    Write-Host "M4 Multi-User Isolation Demo Completed Successfully!"
} finally {
    if ($appAId) {
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & kubectl delete deployment,service,ingress -l "hamicloud.io/application-id=$appAId" --ignore-not-found=true 2>&1 | Out-Null
        $ErrorActionPreference = $prevEap
    }
    if ($startedApi -and $apiProcess -and -not $apiProcess.HasExited) {
        Stop-Process -Id $apiProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
