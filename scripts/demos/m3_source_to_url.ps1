# scripts/demos/m3_source_to_url.ps1
# Automates M3 end-to-end Source-to-URL demo with real Git commits, isolated image builds,
# local OCI registry pushes, webhook deduplication, and Kubernetes deployment:
# 1. Commit 1 -> Build -> Registry Push -> K8s Rollout -> HEALTHY
# 2. Commit 2 -> Signed GitHub Webhook (+ Duplicate replay check) -> Build -> K8s Rollout -> HEALTHY
# 3. Commit 3 (Broken Dockerfile) -> Build -> BUILD_FAILED while Commit 2 remains HEALTHY & serving.

param(
    [int]$Port = 8089,
    [string]$DatabaseUrl = "postgres://hamicloud:hamicloud_secret@localhost:5432/hamicloud?sslmode=disable"
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path "$PSScriptRoot/../..").Path
$RawDir = Join-Path $RootDir "docs/evidence/demos/raw/m3-source-to-url"
New-Item -ItemType Directory -Force -Path $RawDir | Out-Null

$ApiUrl = "http://127.0.0.1:$Port"
$env:ENVIRONMENT = "development"
$env:RUNTIME_DATABASE_URL = $DatabaseUrl
$env:DATABASE_URL = $DatabaseUrl.Replace("postgres://", "postgresql+asyncpg://").Split("?")[0]
$env:INGRESS_DOMAIN = "hamicloud.local"
$env:REGISTRY_HOST = "localhost:5000"

$rnd = [Guid]::NewGuid().ToString().Substring(0, 8)
$gitReposRoot = Join-Path ([System.IO.Path]::GetTempPath()) "hc-m3-repos-$rnd"
$repoDir = Join-Path $gitReposRoot "hami9/m3-live-demo"
New-Item -ItemType Directory -Force -Path $repoDir | Out-Null
$env:BUILD_GIT_REPOS_DIR = $gitReposRoot

function Get-GitHubSignature([string]$Body, [string]$Secret) {
    $hmac = New-Object System.Security.Cryptography.HMACSHA256
    $hmac.Key = [System.Text.Encoding]::UTF8.GetBytes($Secret)
    $hashBytes = $hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Body))
    $hex = ($hashBytes | ForEach-Object { $_.ToString("x2") }) -join ""
    return "sha256=$hex"
}

# 0. Build Go runtime binaries
$schedulerExe = Join-Path $RootDir "bin/hamicloud-scheduler.exe"
$executorExe = Join-Path $RootDir "bin/hamicloud-executor.exe"
Write-Host "Building Go runtime binaries..."
Push-Location (Join-Path $RootDir "runtime")
go build -o ../bin/hamicloud-scheduler.exe ./cmd/hamicloud-scheduler
go build -o ../bin/hamicloud-executor.exe ./cmd/hamicloud-executor
Pop-Location

# 1. Initialize real local Git repository for https://github.com/hami9/m3-live-demo.git
Write-Host "Initializing local Git repository at $repoDir..."
Push-Location $repoDir
git init -b main | Out-Null
git config user.email "demo@hamicloud.local"
git config user.name "HamiCloud M3 Demo"

$dockerfileCommit1 = @"
FROM docker.io/library/nginx:alpine
RUN printf 'HamiCloud M3 Commit 1\n' > /usr/share/nginx/html/index.html && printf 'ok\n' > /usr/share/nginx/html/healthz
"@
Set-Content -Path (Join-Path $repoDir "Dockerfile") -Value $dockerfileCommit1 -Encoding ascii
git add Dockerfile
git commit -m "feat: commit 1 initial service" | Out-Null
$commit1Sha = (& git rev-parse HEAD).Trim()
Pop-Location
Write-Host "Created Commit 1: $commit1Sha"

# 2. Ensure API server is running
$apiProcess = $null
$startedApi = $false
$appId = $null
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

    # 3. Create workspace, connect repository, and create application
    $wsSlug = "m3-live-$rnd"
    $wsBody = @{
        name = "M3 Source-to-URL Workspace $rnd"
        slug = $wsSlug
    } | ConvertTo-Json
    $wsRes = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces" -Method POST -UseBasicParsing -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"} -Body $wsBody
    $workspaceId = $wsRes.id
    $env:WORKLOAD_WORKSPACE_ID = $workspaceId
    Write-Host "Created workspace $workspaceId ($wsSlug)"

    $webhookSecret = "whsec_m3_live_demo_secret_123456"
    $repoBody = @{
        name = "m3-live-demo"
        repo_url = "https://github.com/hami9/m3-live-demo.git"
        webhook_secret = $webhookSecret
        default_branch = "main"
    } | ConvertTo-Json
    $repoRes = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces/$workspaceId/repositories" -Method POST -UseBasicParsing -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"} -Body $repoBody
    $repositoryId = $repoRes.id
    Write-Host "Connected repository $repositoryId (https://github.com/hami9/m3-live-demo.git)"

    $appSlug = "m3-web-$rnd"
    $appBody = @{
        name = "M3 Source App $rnd"
        slug = $appSlug
        workload_type = "HTTP_SERVICE"
        repository_id = $repositoryId
        dockerfile_path = "Dockerfile"
        context_dir = "."
        git_branch = "main"
    } | ConvertTo-Json
    $appRes = Invoke-RestMethod -Uri "$ApiUrl/v1/workspaces/$workspaceId/apps" -Method POST -UseBasicParsing -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"} -Body $appBody
    $appId = $appRes.id
    Write-Host "Created application $appId ($appSlug)"

    # =========================================================================
    # PHASE A: Commit 1 -> Build -> Registry Push -> K8s Rollout -> HEALTHY
    # =========================================================================
    $build1Body = @{
        commit_sha = $commit1Sha
        git_ref = "main"
        commit_message = "feat: commit 1 initial service"
        port = 80
        health_path = "/healthz"
    } | ConvertTo-Json
    $build1Raw = Invoke-WebRequest -Uri "$ApiUrl/v1/apps/$appId/builds" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"} -Body $build1Body
    Set-Content -Path (Join-Path $RawDir "01-commit1-trigger-build.json") -Value $build1Raw.Content -Encoding utf8
    $build1Res = $build1Raw.Content | ConvertFrom-Json
    $release1Id = $build1Res.id
    Write-Host "Triggered Build 1 (Release $release1Id) for Commit 1 ($commit1Sha)"

    Write-Host "Running Executor to build and push OCI image for Commit 1..."
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $execBuild1Log = & $executorExe --run-once 2>&1 | Out-String
    $ErrorActionPreference = $prevEap
    Set-Content -Path (Join-Path $RawDir "02-commit1-executor-build.log") -Value $execBuild1Log -Encoding utf8

    Write-Host "Running Scheduler to admit Release 1..."
    $ErrorActionPreference = "Continue"
    $schedAdmit1Log = & $schedulerExe --run-once 2>&1 | Out-String
    $ErrorActionPreference = $prevEap
    Set-Content -Path (Join-Path $RawDir "03-commit1-scheduler-admit.log") -Value $schedAdmit1Log -Encoding utf8

    Write-Host "Running Executor to deploy Release 1 to Kubernetes..."
    $ErrorActionPreference = "Continue"
    $execDeploy1Log = & $executorExe --run-once 2>&1 | Out-String
    $ErrorActionPreference = $prevEap
    Set-Content -Path (Join-Path $RawDir "04-commit1-executor-deploy.log") -Value $execDeploy1Log -Encoding utf8

    $rels1Raw = Invoke-WebRequest -Uri "$ApiUrl/v1/apps/$appId/releases" -Method GET -UseBasicParsing -Headers @{"X-Dev-Subject"="alice"}
    Set-Content -Path (Join-Path $RawDir "05-commit1-releases.json") -Value $rels1Raw.Content -Encoding utf8

    $pod1Ip = (& kubectl get pods -l "hamicloud.io/release-id=$release1Id" -o jsonpath="{.items[0].status.podIP}" 2>&1).Trim()
    $http1Out = & docker exec desktop-control-plane curl -s "http://${pod1Ip}:80/" 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "06-commit1-http-response.txt") -Value $http1Out -Encoding utf8
    Write-Host "Commit 1 Pod ($pod1Ip) HTTP response: $($http1Out.Trim())"

    # =========================================================================
    # PHASE B: Commit 2 -> Signed Webhook (+ Duplicate Check) -> Build -> HEALTHY
    # =========================================================================
    Push-Location $repoDir
    $dockerfileCommit2 = @"
FROM docker.io/library/nginx:alpine
RUN printf 'HamiCloud M3 Commit 2\n' > /usr/share/nginx/html/index.html && printf 'ok\n' > /usr/share/nginx/html/healthz
"@
    Set-Content -Path (Join-Path $repoDir "Dockerfile") -Value $dockerfileCommit2 -Encoding ascii
    git add Dockerfile
    git commit -m "feat: commit 2 updated response" | Out-Null
    $commit2Sha = (& git rev-parse HEAD).Trim()
    Pop-Location
    Write-Host "Created Commit 2: $commit2Sha"

    $webhookPayload = @{
        ref = "refs/heads/main"
        after = $commit2Sha
        head_commit = @{
            id = $commit2Sha
            message = "feat: commit 2 updated response"
        }
    } | ConvertTo-Json -Compress
    $sig2 = Get-GitHubSignature -Body $webhookPayload -Secret $webhookSecret
    $deliveryId2 = "delivery-m3-commit2-$rnd"

    $wh1Raw = Invoke-WebRequest -Uri "$ApiUrl/v1/webhooks/github/$repositoryId" -Method POST -UseBasicParsing `
        -Headers @{
            "Content-Type" = "application/json"
            "X-Hub-Signature-256" = $sig2
            "X-GitHub-Delivery" = $deliveryId2
            "X-GitHub-Event" = "push"
        } -Body $webhookPayload
    Set-Content -Path (Join-Path $RawDir "07-commit2-webhook-processed.json") -Value $wh1Raw.Content -Encoding utf8
    $wh1Res = $wh1Raw.Content | ConvertFrom-Json
    $release2Id = $wh1Res.release_id
    Write-Host "Processed signed GitHub webhook -> Release 2 ($release2Id)"

    # Replay identical webhook delivery ID to verify deduplication
    $wh2Raw = Invoke-WebRequest -Uri "$ApiUrl/v1/webhooks/github/$repositoryId" -Method POST -UseBasicParsing `
        -Headers @{
            "Content-Type" = "application/json"
            "X-Hub-Signature-256" = $sig2
            "X-GitHub-Delivery" = $deliveryId2
            "X-GitHub-Event" = "push"
        } -Body $webhookPayload
    Set-Content -Path (Join-Path $RawDir "08-commit2-webhook-duplicate.json") -Value $wh2Raw.Content -Encoding utf8
    $wh2Res = $wh2Raw.Content | ConvertFrom-Json
    if ($wh2Res.status -ne "DUPLICATE") {
        throw "Expected duplicate webhook status DUPLICATE, got $($wh2Res.status)"
    }
    Write-Host "Verified duplicate webhook deduplication: status=$($wh2Res.status)"

    Write-Host "Running Executor to build and push OCI image for Commit 2..."
    $ErrorActionPreference = "Continue"
    $execBuild2Log = & $executorExe --run-once 2>&1 | Out-String
    $ErrorActionPreference = $prevEap
    Set-Content -Path (Join-Path $RawDir "09-commit2-executor-build.log") -Value $execBuild2Log -Encoding utf8

    Write-Host "Running Scheduler to admit Release 2 and supersede Release 1..."
    $ErrorActionPreference = "Continue"
    $schedAdmit2Log = & $schedulerExe --run-once 2>&1 | Out-String
    $ErrorActionPreference = $prevEap
    Set-Content -Path (Join-Path $RawDir "10-commit2-scheduler-admit.log") -Value $schedAdmit2Log -Encoding utf8

    Write-Host "Running Executor to deploy Release 2 to Kubernetes..."
    $ErrorActionPreference = "Continue"
    $execDeploy2Log = & $executorExe --run-once 2>&1 | Out-String
    $ErrorActionPreference = $prevEap
    Set-Content -Path (Join-Path $RawDir "11-commit2-executor-deploy.log") -Value $execDeploy2Log -Encoding utf8

    $pod2Ip = (& kubectl get pods -l "hamicloud.io/release-id=$release2Id" -o jsonpath="{.items[0].status.podIP}" 2>&1).Trim()
    $http2Out = & docker exec desktop-control-plane curl -s "http://${pod2Ip}:80/" 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "12-commit2-http-response.txt") -Value $http2Out -Encoding utf8
    Write-Host "Commit 2 Pod ($pod2Ip) HTTP response: $($http2Out.Trim())"

    # =========================================================================
    # PHASE C: Commit 3 (Broken Build) -> BUILD_FAILED while Release 2 serves
    # =========================================================================
    Push-Location $repoDir
    $dockerfileCommit3 = @"
FROM docker.io/library/nginx:alpine
RUN echo "Simulating broken compile stage" && exit 1
"@
    Set-Content -Path (Join-Path $repoDir "Dockerfile") -Value $dockerfileCommit3 -Encoding ascii
    git add Dockerfile
    git commit -m "chore: broken build commit 3" | Out-Null
    $commit3Sha = (& git rev-parse HEAD).Trim()
    Pop-Location
    Write-Host "Created broken Commit 3: $commit3Sha"

    $build3Body = @{
        commit_sha = $commit3Sha
        git_ref = "main"
        commit_message = "chore: broken build commit 3"
    } | ConvertTo-Json
    $build3Raw = Invoke-WebRequest -Uri "$ApiUrl/v1/apps/$appId/builds" -Method POST -UseBasicParsing `
        -Headers @{"Content-Type"="application/json"; "X-Dev-Subject"="alice"} -Body $build3Body
    $build3Res = $build3Raw.Content | ConvertFrom-Json
    $release3Id = $build3Res.id
    Write-Host "Triggered Build 3 (Release $release3Id) for broken Commit 3 ($commit3Sha)"

    Write-Host "Running Executor on broken Release 3 (expecting BUILD_FAILED)..."
    $ErrorActionPreference = "Continue"
    $execBuild3Log = & $executorExe --run-once 2>&1 | Out-String
    $ErrorActionPreference = $prevEap
    Set-Content -Path (Join-Path $RawDir "13-commit3-failed-executor-build.log") -Value $execBuild3Log -Encoding utf8

    # Capture final releases, app state, k8s resources, and live HTTP check
    $finalRelsRaw = Invoke-WebRequest -Uri "$ApiUrl/v1/apps/$appId/releases" -Method GET -UseBasicParsing -Headers @{"X-Dev-Subject"="alice"}
    Set-Content -Path (Join-Path $RawDir "14-final-releases.json") -Value $finalRelsRaw.Content -Encoding utf8
    $finalRels = ($finalRelsRaw.Content | ConvertFrom-Json).items

    $finalAppRaw = Invoke-WebRequest -Uri "$ApiUrl/v1/apps/$appId" -Method GET -UseBasicParsing -Headers @{"X-Dev-Subject"="alice"}
    Set-Content -Path (Join-Path $RawDir "15-final-app-details.json") -Value $finalAppRaw.Content -Encoding utf8
    $finalApp = $finalAppRaw.Content | ConvertFrom-Json

    $k8sResources = & kubectl get deploy,svc,ingress,pods -l "hamicloud.io/application-id=$appId" -o wide 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "16-k8s-resources.txt") -Value $k8sResources -Encoding utf8

    $httpPostFailOut = & docker exec desktop-control-plane curl -s "http://${pod2Ip}:80/" 2>&1 | Out-String
    Set-Content -Path (Join-Path $RawDir "17-post-failure-http-response.txt") -Value $httpPostFailOut -Encoding utf8

    # Assertions
    $r1 = $finalRels | Where-Object { $_.id -eq $release1Id }
    $r2 = $finalRels | Where-Object { $_.id -eq $release2Id }
    $r3 = $finalRels | Where-Object { $_.id -eq $release3Id }

    if ($r1.commit_sha -ne $commit1Sha -or -not ($r1.image_digest -match "@sha256:[0-9a-f]{64}$")) {
        throw "Release 1 validation failed: commit=$($r1.commit_sha) digest=$($r1.image_digest)"
    }
    if ($r2.commit_sha -ne $commit2Sha -or $r2.status -ne "HEALTHY" -or -not ($r2.image_digest -match "@sha256:[0-9a-f]{64}$")) {
        throw "Release 2 validation failed: status=$($r2.status) commit=$($r2.commit_sha) digest=$($r2.image_digest)"
    }
    if ($r1.image_digest -eq $r2.image_digest) {
        throw "Expected distinct image digests for Commit 1 and Commit 2!"
    }
    if ($r3.commit_sha -ne $commit3Sha -or $r3.status -ne "BUILD_FAILED") {
        throw "Release 3 validation failed: expected BUILD_FAILED, got $($r3.status)"
    }
    if ($finalApp.current_release_id -ne $release2Id) {
        throw "Application current_release_id was modified after failed build! Expected $release2Id, got $($finalApp.current_release_id)"
    }
    if ($httpPostFailOut.Trim() -ne "HamiCloud M3 Commit 2") {
        throw "Serving pod response after failed build did not match Commit 2: $($httpPostFailOut.Trim())"
    }

    Write-Host "M3 Source-to-URL live demo completed successfully! All assertions passed."
    Write-Host "Commit 1 ($commit1Sha) -> Release 1 ($release1Id) -> $($r1.image_digest) [$($r1.status)]"
    Write-Host "Commit 2 ($commit2Sha) -> Release 2 ($release2Id) -> $($r2.image_digest) [$($r2.status)]"
    Write-Host "Commit 3 ($commit3Sha) -> Release 3 ($release3Id) -> [$($r3.status)] (Serving Release remains $release2Id)"
} finally {
    if ($appId) {
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & kubectl delete deploy,svc,ingress -l "hamicloud.io/application-id=$appId" --ignore-not-found=true 2>&1 | Out-Null
        $ErrorActionPreference = $prevEap
    }
    if (Test-Path $gitReposRoot) {
        Remove-Item -Recurse -Force $gitReposRoot -ErrorAction SilentlyContinue
    }
    if ($startedApi -and $apiProcess) {
        Write-Host "Stopping demo API server..."
        Stop-Process -Id $apiProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
