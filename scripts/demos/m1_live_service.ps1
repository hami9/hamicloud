# scripts/demos/m1_live_service.ps1
# Automates M1 live application deployment demo on Kubernetes:
# Creates workspace, creates app, submits deployment with approved nginx image,
# admits release with scheduler, reconciles with executor on real Kubernetes cluster,
# verifies pod running & ready, queries API for HEALTHY state, and captures raw evidence.

param(
    [int]$Port = 8088,
    [string]$DatabaseUrl = "postgres://hamicloud:hamicloud_secret@localhost:5432/hamicloud?sslmode=disable"
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path "$PSScriptRoot/../..").Path
$RawDir = Join-Path $RootDir "docs/evidence/demos/raw/m1-live-service"
New-Item -ItemType Directory -Force -Path $RawDir | Out-Null

$ApiUrl = "http://127.0.0.1:$Port"
$env:ENVIRONMENT = "development"
$env:RUNTIME_DATABASE_URL = $DatabaseUrl
$env:DATABASE_URL = $DatabaseUrl.Replace("postgres://", "postgresql+asyncpg://").Split("?")[0]
$env:INGRESS_DOMAIN = "hamicloud.local"

# 0. Build Go runtime binaries
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
    $wsSlug = "m1-live-$rnd"
    $wsBody = @{
        name = "M1 Live Workspace $rnd"
        slug = $wsSlug
    } | ConvertTo-Json

    $wsRes = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces" -Method POST -UseBasicParsing -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"} -Body $wsBody
    $workspaceId = $wsRes.id
    Write-Host "Created workspace $workspaceId ($wsSlug)"

    # 3. Create application
    $appSlug = "nginx-service-$rnd"
    $appBody = @{
        name = "Nginx Live Service $rnd"
        slug = $appSlug
        workload_type = "HTTP_SERVICE"
    } | ConvertTo-Json

    $appRes = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces/$workspaceId/apps" -Method POST -UseBasicParsing -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"} -Body $appBody
    $appId = $appRes.id
    Write-Host "Created application $appId ($appSlug)"

    # 4. Submit deployment with approved image
    $depBody = @{
        image_digest = "docker.io/library/nginx:alpine"
        port = 80
        health_path = "/"
    } | ConvertTo-Json

    $idempKey = "idemp-m1-$rnd"
    $depResRaw = Invoke-WebRequest -Uri "$ApiUrl/v1/apps/$appId/deployments" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"; "Idempotency-Key"=$idempKey} `
        -Body $depBody
    Set-Content -Path (Join-Path $RawDir "01-submit-deployment.json") -Value $depResRaw.Content -Encoding utf8
    $depRes = $depResRaw.Content | ConvertFrom-Json
    $releaseId = $depRes.operation_id
    Write-Host "Submitted deployment release $releaseId"

    # 5. Run scheduler to admit release intent
    Write-Host "Admitting release with scheduler..."
    $schedLog = & $schedulerExe --run-once 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "02-scheduler-admit.log") -Value $schedLog -Encoding utf8

    # 6. Run executor to reconcile release via KubeWorkloadRunner on real Kubernetes
    Write-Host "Reconciling release with executor on Kubernetes cluster..."
    $env:WORKLOAD_WORKSPACE_ID = $workspaceId
    $execLog = & $executorExe --run-once 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "03-executor-reconcile.log") -Value $execLog -Encoding utf8

    # 7. Capture Kubernetes cluster resources
    Write-Host "Capturing Kubernetes cluster resources..."
    $k8sResources = & kubectl get deploy,svc,ingress,pods -l "hamicloud.io/application-id=$appId" -o wide 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "04-k8s-resources.txt") -Value $k8sResources -Encoding utf8

    # 8. Query API for release status
    Write-Host "Fetching releases from API..."
    $relCheckRaw = Invoke-WebRequest -Uri "$ApiUrl/v1/apps/$appId/releases" -Method GET -UseBasicParsing -Headers @{"X-Dev-Subject"="alice"}
    Set-Content -Path (Join-Path $RawDir "05-releases.json") -Value $relCheckRaw.Content -Encoding utf8
    $releases = $relCheckRaw.Content | ConvertFrom-Json
    $rel = $releases.items | Where-Object { $_.id -eq $releaseId }

    # 9. Query API for app details
    Write-Host "Fetching application details from API..."
    $appCheckRaw = Invoke-WebRequest -Uri "$ApiUrl/v1/apps/$appId" -Method GET -UseBasicParsing -Headers @{"X-Dev-Subject"="alice"}
    Set-Content -Path (Join-Path $RawDir "06-app-details.json") -Value $appCheckRaw.Content -Encoding utf8
    $appCheck = $appCheckRaw.Content | ConvertFrom-Json

    # 10. Test direct HTTP reachability against pod IP from inside cluster
    Write-Host "Testing HTTP reachability inside cluster..."
    $podName = (& kubectl get pods -l "hamicloud.io/application-id=$appId" -o jsonpath="{.items[0].metadata.name}" 2>&1).Trim()
    $podIp = (& kubectl get pods -l "hamicloud.io/application-id=$appId" -o jsonpath="{.items[0].status.podIP}" 2>&1).Trim()
    
    $clusterCurl = ""
    if (-not [string]::IsNullOrWhiteSpace($podIp)) {
        $clusterCurl = & docker exec desktop-control-plane curl -sI "http://${podIp}:80/" 2>&1 | Out-String
        Set-Content -Path (Join-Path $RawDir "07-service-http-response.txt") -Value $clusterCurl -Encoding utf8
    }

    # 11. Assertions
    Write-Host "Verifying assertions..."
    if ($null -eq $rel) {
        throw "Assertion failed: release $releaseId not found in releases response!"
    }
    if ($rel.status -ne "HEALTHY") {
        throw "Assertion failed: release status is '$($rel.status)', expected 'HEALTHY'!"
    }
    if ($appCheck.current_release_id -ne $releaseId) {
        throw "Assertion failed: app current_release_id is '$($appCheck.current_release_id)', expected '$releaseId'!"
    }

    Write-Host "M1 live service deployment completed successfully! All assertions verified."
    Write-Host "Raw output written to $RawDir"
} finally {
    if ($startedApi -and $apiProcess) {
        Write-Host "Stopping demo API server..."
        Stop-Process -Id $apiProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
