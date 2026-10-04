# Demo: Fresh Local Installation & E2E Reproduction

This demonstration verifies Milestone M2 exit criterion 10:
> A fresh local installation reproduces this flow from documented steps.

Recorded via automated script `scripts/demos/fresh_installation.ps1` executing documented installation and verification steps.
All raw outputs are captured in `docs/evidence/demos/raw/fresh-installation/`.
**Reproduction command:**
```powershell
powershell -ExecutionPolicy Bypass -File ./scripts/demos/fresh_installation.ps1
```

---

## 1. Environment & Prerequisites

- **Host OS:** Windows 10/11 x64 (PowerShell / WSL2)
- **Runtimes:** Python 3.14, Go 1.23, Node.js 24+, Docker & Compose v2
- **Infrastructure Services:** PostgreSQL 16 Alpine, Redis 7.2 Alpine, NATS JetStream 2.10, MinIO S3, Keycloak 24.0.5

---

## 2. Step-by-Step Reproduction from README.md Quoting Raw Evidence

### Step 1: Start Infrastructure Containers

```bash
docker compose -f deploy/compose/docker-compose.yml up -d
```

**Verbatim Container Status (`01-docker-ps.txt`):**
```text
NAMES                IMAGE                              STATUS
hamicloud-keycloak   quay.io/keycloak/keycloak:24.0.5   Up 2 hours (healthy)
hamicloud-postgres   postgres:16-alpine                 Up 2 hours (healthy)
hamicloud-nats       nats:2.10-alpine                   Up 2 hours (healthy)
hamicloud-redis      redis:7.2-alpine                   Up 2 hours (healthy)
hamicloud-minio      quay.io/minio/minio:latest         Up 2 hours (healthy)
```

All 5 core infrastructure containers, including Keycloak, are healthy.

---

### Step 2: Configure Python Virtual Environment & Pinned Dependencies

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -e "./apps/api[dev]"
```

---

### Step 3: Run Database Migrations

```bash
.\.venv\Scripts\python.exe -m alembic -c migrations/alembic.ini upgrade head
```

**Verbatim Migration History (`02-alembic-history.txt`):**
```text
0005_source_builds_and_repos -> 0006_add_superseded_status (head), add superseded status to release check constraints
0004_add_outbox_next_attempt_at -> 0005_source_builds_and_repos, add repositories, source builds, and webhook deliveries
0003_add_recovery_pending_state -> 0004_add_outbox_next_attempt_at, add next_attempt_at column to outbox_events
0002_close_schema_gaps -> 0003_add_recovery_pending_state, add recovery pending state to job check constraints
0001_baseline_schema -> 0002_close_schema_gaps, close schema gaps
<base> -> 0001_baseline_schema, baseline schema
```

---

### Step 4: Execute Verification Gates

#### 1. API Test Suite
```bash
.\.venv\Scripts\python.exe -m pytest apps/api/tests/
```
**Verbatim Test Suite Output (`04-pytest.txt`):**
```text
============================= test session starts =============================
platform win32 -- Python 3.14.5, pytest-9.1.1, pluggy-1.6.0
rootdir: E:\project\hamicloud\apps\api
configfile: pyproject.toml
plugins: anyio-4.15.1, asyncio-1.4.0
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 117 items

apps\api\tests\test_api_flows.py ........                                [  6%]
apps\api\tests\test_application_endpoints.py ...                         [  9%]
apps\api\tests\test_auth_oidc.py .............                           [ 20%]
apps\api\tests\test_contracts.py .......                                 [ 26%]
apps\api\tests\test_health.py ..                                         [ 28%]
apps\api\tests\test_image_policy.py ...                                  [ 30%]
apps\api\tests\test_job_state_machine.py ......                          [ 35%]
apps\api\tests\test_m1_live_application_e2e.py ..                        [ 37%]
apps\api\tests\test_m2_jobs_e2e.py .......                               [ 43%]
apps\api\tests\test_m3_source_to_url.py .......                          [ 49%]
apps\api\tests\test_models.py .......                                    [ 55%]
apps\api\tests\test_operations.py ....                                   [ 58%]
apps\api\tests\test_outbox_dispatcher.py .....                           [ 63%]
apps\api\tests\test_phase3_contracts_idempotency.py .................... [ 80%]
.......................                                                  [100%]

============================== warnings summary ===============================
...
================= 117 passed, 4 warnings in 175.50s (0:02:55) =================
```

#### 2. Static Typing (mypy)
```bash
cd apps/api && ..\..\.venv\Scripts\python.exe -m mypy --explicit-package-bases app
```
**Verbatim Output (`05-mypy.txt`):**
```text
Success: no issues found in 38 source files
```

#### 3. Linter (ruff)
```bash
.\.venv\Scripts\python.exe -m ruff check apps/api
```
**Verbatim Output (`06-ruff.txt`):**
```text
All checks passed!
```

#### 4. Migration Schema Alignment (alembic check)
```bash
.\.venv\Scripts\python.exe -m alembic -c migrations/alembic.ini check
```
**Verbatim Output (`03-alembic-check.txt`):**
```text
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume default database schema 'public'.
No new upgrade operations detected.
```

#### 5. OpenAPI Contract Validation
```bash
.\.venv\Scripts\python.exe -m openapi_spec_validator contracts/openapi/v1.yaml
```
**Verbatim Output (`07-openapi-validator.txt`):**
```text
contracts/openapi/v1.yaml: OK
```

#### 6. Go Runtime Tests
```bash
cd runtime && go test -v ./...
```
**Verbatim Output Summary (`08-go-test.txt`):**
```text
PASS
ok  	github.com/hami9/hamicloud/runtime/internal/bus         0.228s
ok  	github.com/hami9/hamicloud/runtime/internal/config      0.089s
ok  	github.com/hami9/hamicloud/runtime/internal/domain      0.091s
ok  	github.com/hami9/hamicloud/runtime/internal/executor    1.035s
ok  	github.com/hami9/hamicloud/runtime/internal/reconciler  1.523s
ok  	github.com/hami9/hamicloud/runtime/internal/scheduler   0.814s
ok  	github.com/hami9/hamicloud/runtime/internal/store       9.626s
```

#### 7. Web Frontend Build
```bash
cd apps/web && npm run build
```
**Verbatim Output (`09-web-build.txt`):**
```text
vite v8.3.1 building client environment for production...
transforming...
✓ 18 modules transformed.
rendering chunks...
computing gzip size...
dist/index.html                   0.45 kB │ gzip:  0.29 kB
dist/assets/index-CakGEGqm.css   17.25 kB │ gzip:  3.90 kB
dist/assets/index-BD3M7Bg-.js   252.58 kB │ gzip: 76.10 kB
✓ built in 7.75s
```

---

## 3. Real Cluster Deployment Step

```text
NOT RUN: No live Kubernetes cluster available in the execution environment
(kind not installed; kubectl connection to 127.0.0.1:61083 refused).
Milestones M1 and M2 are rejected and remain OPEN pending live cluster deployment verification.
```
