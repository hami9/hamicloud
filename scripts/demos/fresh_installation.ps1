# scripts/demos/fresh_installation.ps1
# Automates M2 fresh local installation verification:
# Runs all documented verification gates verbatim and captures raw output.

$ErrorActionPreference = "Continue"

$RootDir = (Resolve-Path "$PSScriptRoot/../..").Path
$RawDir = Join-Path $RootDir "docs/evidence/demos/raw/fresh-installation"
New-Item -ItemType Directory -Force -Path $RawDir | Out-Null

$pythonExe = Join-Path $RootDir ".venv/Scripts/python.exe"

# 1. Docker ps
Write-Host "Capturing docker ps status..."
$dockerPs = & docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}" | Out-String
Set-Content -Path (Join-Path $RawDir "01-docker-ps.txt") -Value $dockerPs -Encoding utf8
foreach ($c in @("hamicloud-postgres", "hamicloud-redis", "hamicloud-nats", "hamicloud-minio", "hamicloud-keycloak")) {
    if ($dockerPs -notmatch "$c\s+.*\bhealthy\b") {
        throw "Assertion failed: Container $c is not in healthy state!"
    }
}

# 2. Alembic history
Write-Host "Capturing alembic history..."
$alembicHistory = & $pythonExe -m alembic -c (Join-Path $RootDir "migrations/alembic.ini") history 2>&1 | Out-String
Set-Content -Path (Join-Path $RawDir "02-alembic-history.txt") -Value $alembicHistory -Encoding utf8
if ($LASTEXITCODE -ne 0) {
    throw "Assertion failed: alembic history failed with exit code $LASTEXITCODE"
}

# 3. Alembic check
Write-Host "Capturing alembic check..."
$alembicCheck = & $pythonExe -m alembic -c (Join-Path $RootDir "migrations/alembic.ini") check 2>&1 | Out-String
Set-Content -Path (Join-Path $RawDir "03-alembic-check.txt") -Value $alembicCheck -Encoding utf8
if ($LASTEXITCODE -ne 0) {
    throw "Assertion failed: alembic check failed with exit code $LASTEXITCODE"
}

# 4. Pytest test suite
Write-Host "Running pytest test suite (this may take ~2 minutes)..."
$pytestOut = & $pythonExe -m pytest (Join-Path $RootDir "apps/api/tests") 2>&1 | Out-String
Set-Content -Path (Join-Path $RawDir "04-pytest.txt") -Value $pytestOut -Encoding utf8
if ($LASTEXITCODE -ne 0) {
    throw "Assertion failed: pytest failed with exit code $LASTEXITCODE"
}
if ($pytestOut -match "(\d+)\s+skipped") {
    throw "Assertion failed: pytest had skipped tests! (Expected 0 skipped). Output snippet: $Matches[0]"
}
if ($pytestOut -notmatch "(\d+)\s+passed") {
    throw "Assertion failed: pytest did not complete with passed tests!"
}

# 5. Mypy type check
Write-Host "Running mypy..."
Push-Location (Join-Path $RootDir "apps/api")
$mypyOut = & $pythonExe -m mypy --explicit-package-bases app 2>&1 | Out-String
$mypyCode = $LASTEXITCODE
Pop-Location
Set-Content -Path (Join-Path $RawDir "05-mypy.txt") -Value $mypyOut -Encoding utf8
if ($mypyCode -ne 0) {
    throw "Assertion failed: mypy failed with exit code $mypyCode"
}

# 6. Ruff linter check
Write-Host "Running ruff check..."
$ruffOut = & $pythonExe -m ruff check (Join-Path $RootDir "apps/api") 2>&1 | Out-String
$ruffCode = $LASTEXITCODE
Set-Content -Path (Join-Path $RawDir "06-ruff.txt") -Value $ruffOut -Encoding utf8
if ($ruffCode -ne 0) {
    throw "Assertion failed: ruff failed with exit code $ruffCode"
}

# 7. OpenAPI spec validator
Write-Host "Running OpenAPI spec validator..."
$openapiOut = & $pythonExe -m openapi_spec_validator (Join-Path $RootDir "contracts/openapi/v1.yaml") 2>&1 | Out-String
$openapiCode = $LASTEXITCODE
Set-Content -Path (Join-Path $RawDir "07-openapi-validator.txt") -Value $openapiOut -Encoding utf8
if ($openapiCode -ne 0) {
    throw "Assertion failed: openapi_spec_validator failed with exit code $openapiCode"
}

# 8. Go runtime tests
Write-Host "Running Go runtime tests..."
Push-Location (Join-Path $RootDir "runtime")
$goOut = & go test -v ./... 2>&1 | Out-String
$goCode = $LASTEXITCODE
Pop-Location
Set-Content -Path (Join-Path $RawDir "08-go-test.txt") -Value $goOut -Encoding utf8
if ($goCode -ne 0) {
    throw "Assertion failed: go test failed with exit code $goCode"
}

# 9. Web frontend build
Write-Host "Running web frontend build..."
Push-Location (Join-Path $RootDir "apps/web")
$webOut = & npm run build 2>&1 | Out-String
$webCode = $LASTEXITCODE
Pop-Location
Set-Content -Path (Join-Path $RawDir "09-web-build.txt") -Value $webOut -Encoding utf8
if ($webCode -ne 0) {
    throw "Assertion failed: npm run build failed with exit code $webCode"
}

Write-Host "Fresh installation demo completed successfully with all assertions passing. Raw output written to $RawDir"
