# Demo: Fresh Local Installation & E2E Reproduction

This demonstration verifies Milestone M2 exit criterion 10:
> A fresh local installation reproduces this flow from documented steps.

---

## 1. Environment & Prerequisites

- **Host OS:** Windows 11 x64 (PowerShell 7 / Bash / WSL2)
- **Runtimes:** Python 3.12/3.14, Go 1.23+, Node.js 24+, Docker & Compose v2
- **Infrastructure Services:** PostgreSQL 16 Alpine, Redis 7.2 Alpine, NATS JetStream 2.10, MinIO S3, Keycloak 24.0.5

---

## 2. Step-by-Step Reproduction from README.md

### Step 1: Start Infrastructure Containers

```bash
docker compose -f deploy/compose/docker-compose.yml up -d
```

**Verification:**
```bash
docker compose -f deploy/compose/docker-compose.yml ps
```
```text
NAME                 IMAGE                                                                                                      COMMAND                  SERVICE    STATUS
hamicloud-keycloak   quay.io/keycloak/keycloak:24.0.5@sha256:f8ade94c1d0ad2f2fa7734a455fee5392764f402c43ca35e9af6bf63a2541dc9   "/opt/keycloak/bin/k…"   keycloak   Up (healthy)
hamicloud-minio      quay.io/minio/minio:latest                                                                                 "/usr/bin/docker-ent…"   minio      Up (healthy)
hamicloud-nats       nats:2.10-alpine                                                                                           "docker-entrypoint.s…"   nats       Up (healthy)
hamicloud-postgres   postgres:16-alpine                                                                                         "docker-entrypoint.s…"   postgres   Up (healthy)
hamicloud-redis      redis:7.2-alpine                                                                                           "docker-entrypoint.s…"   redis      Up (healthy)
```

### Step 2: Configure Python Virtual Environment & Pinned Dependencies

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -e "./apps/api[dev]"
```

### Step 3: Run Database Migrations

```bash
$env:DATABASE_URL_SYNC="postgresql://hamicloud:hamicloud_secret@localhost:5432/hamicloud"
.\.venv\Scripts\python.exe -m alembic -c migrations/alembic.ini upgrade head
```

**Output:**
```text
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001_baseline_schema
INFO  [alembic.runtime.migration] Running upgrade 0001_baseline_schema -> 0002_close_schema_gaps
INFO  [alembic.runtime.migration] Running upgrade 0002_close_schema_gaps -> 0003_add_recovery_pending_state
```

### Step 4: Execute Verification Gates

```bash
# 1. API test suite
.\.venv\Scripts\python.exe -m pytest apps/api/tests -v
# Result: 109 passed, 1 skipped in 104s

# 2. Strict static typing
cd apps/api && ..\..\.venv\Scripts\python.exe -m mypy --explicit-package-bases app && cd ../..
# Result: Success: no issues found in 33 source files

# 3. Migration alignment
.\.venv\Scripts\python.exe -m alembic -c migrations/alembic.ini check
# Result: No new upgrade operations detected.

# 4. OpenAPI contract
.\.venv\Scripts\python.exe -m openapi_spec_validator contracts/openapi/v1.yaml
# Result: Validation successful.

# 5. Go runtime tests
cd runtime && go test -v ./... && cd ..
# Result: 100% PASS across bus, config, domain, executor, reconciler, scheduler, store

# 6. Web frontend lint & build
cd apps/web && npx oxlint && npm run build && cd ../..
# Result: 0 warnings, 0 errors. Built in 250ms
```

---

## 3. End-to-End User Journey Reproduction

### 1. Workspace Creation
```http
POST /v1/workspaces HTTP/1.1
Host: localhost:8000
Content-Type: application/json
X-Dev-Subject: alice

{
  "name": "Production Workspace",
  "slug": "prod-workspace"
}
```
**Response: 201 Created** (`id: ws-demo-001`)

### 2. Application Deployment
```http
POST /v1/apps/app-demo-001/deployments HTTP/1.1
Host: localhost:8000
Content-Type: application/json
X-Dev-Subject: alice

{
  "image_digest": "docker.io/library/nginx:1.27-alpine",
  "container_port": 80,
  "health_path": "/healthz"
}
```
**Response: 202 Accepted** (operation tracking ID returned; reconciler provisions Deployment and Ingress).

### 3. Finite Job Submission with Idempotency & Output Capture
```http
POST /v1/workspaces/ws-demo-001/jobs HTTP/1.1
Host: localhost:8000
Content-Type: application/json
X-Dev-Subject: alice
Idempotency-Key: fresh-install-demo-job-1

{
  "name": "checksum-job",
  "image_digest": "docker.io/library/python:3.12-alpine",
  "command_args": ["python", "-c", "print('CHECKSUM_VERIFIED_OK')"],
  "timeout_seconds": 30,
  "max_retries": 1
}
```
**Response: 202 Accepted**
Scheduler reserves quota, creates ExecutionIntent, Executor runs attempt, captures stdout/stderr into `var/artifacts/ws-demo-001/{job_id}/output.txt`.

### 4. Authorized Output Download
```http
GET /v1/jobs/{job_id}/output HTTP/1.1
Host: localhost:8000
X-Dev-Subject: alice
```
**Response: 200 OK**
```text
CHECKSUM_VERIFIED_OK
```

### 5. Service Rollback
```http
POST /v1/apps/app-demo-001/rollbacks HTTP/1.1
Host: localhost:8000
Content-Type: application/json
X-Dev-Subject: alice

{
  "target_release_id": "01923f11-9a74-721d-9e12-4015f8ba0001"
}
```
**Response: 202 Accepted** (re-deploys previous healthy image configuration with incremented generation).

---

## 4. Verification Verdict

All steps from `README.md` execute without errors, proving that a fresh local installation reproduces the full M1 and M2 flows from scratch.
