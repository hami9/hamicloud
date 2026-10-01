# Demo: Fresh Local Installation & E2E Reproduction

This demonstration verifies Milestone M2 exit criterion 10:
> A fresh local installation reproduces this flow from documented steps.

Recorded from live repository execution.

---

## 1. Environment & Prerequisites

- **Host OS:** Windows 11 x64 (PowerShell 7 / WSL2)
- **Runtimes:** Python 3.14, Go 1.23, Node.js 24+, Docker & Compose v2
- **Infrastructure Services:** PostgreSQL 16 Alpine, Redis 7.2 Alpine, NATS JetStream 2.10, MinIO S3, Keycloak 24.0.5

---

## 2. Step-by-Step Reproduction from README.md

### Step 1: Start Infrastructure Containers

```bash
docker compose -f deploy/compose/docker-compose.yml up -d
```

**Verification:**
```bash
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
```

```text
NAMES                IMAGE                              STATUS
hamicloud-keycloak   quay.io/keycloak/keycloak:24.0.5   Up (healthy)
hamicloud-postgres   postgres:16-alpine                 Up (healthy)
hamicloud-nats       nats:2.10-alpine                   Up (healthy)
hamicloud-redis      redis:7.2-alpine                   Up (healthy)
hamicloud-minio      quay.io/minio/minio:latest         Up (healthy)
```

### Step 2: Configure Python Virtual Environment & Pinned Dependencies

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -e "./apps/api[dev]"
```

### Step 3: Run Database Migrations

```bash
.\.venv\Scripts\python.exe -m alembic -c migrations/alembic.ini upgrade head
```

**Live Migration History:**
```text
0005_source_builds_and_repos -> 0006_add_superseded_status (head), add superseded status to release check constraints
0004_add_outbox_next_attempt_at -> 0005_source_builds_and_repos, add repositories, source builds, and webhook deliveries
0003_add_recovery_pending_state -> 0004_add_outbox_next_attempt_at, add next_attempt_at column to outbox_events
0002_close_schema_gaps -> 0003_add_recovery_pending_state, add recovery pending state to job check constraints
0001_baseline_schema -> 0002_close_schema_gaps, close schema gaps
<base> -> 0001_baseline_schema, baseline schema
```

### Step 4: Execute Verification Gates

#### 1. API Test Suite
```bash
.\.venv\Scripts\python.exe -m pytest apps/api/tests/ -v
```
```text
=========== 116 passed, 1 skipped, 4 warnings in 135.23s (0:02:15) ============
```

#### 2. Static Typing (mypy)
```bash
cd apps/api && ..\..\.venv\Scripts\python.exe -m mypy --explicit-package-bases app
```
```text
Success: no issues found in 38 source files
```

#### 3. Linter (ruff)
```bash
.\.venv\Scripts\python.exe -m ruff check apps/api
```
```text
All checks passed!
```

#### 4. Migration Schema Alignment
```bash
.\.venv\Scripts\python.exe -m alembic -c migrations/alembic.ini check
```
```text
No new upgrade operations detected.
```

#### 5. OpenAPI Contract Validation
```bash
.\.venv\Scripts\python.exe -m openapi_spec_validator contracts/openapi/v1.yaml
```
```text
contracts/openapi/v1.yaml: OK
```

#### 6. Go Runtime Tests
```bash
cd runtime && go test -v ./...
```
```text
PASS
ok      github.com/hami9/hamicloud/runtime/internal/bus         0.228s
ok      github.com/hami9/hamicloud/runtime/internal/config      0.089s
ok      github.com/hami9/hamicloud/runtime/internal/domain      0.091s
ok      github.com/hami9/hamicloud/runtime/internal/executor    1.035s
ok      github.com/hami9/hamicloud/runtime/internal/reconciler  1.523s
ok      github.com/hami9/hamicloud/runtime/internal/scheduler   0.814s
ok      github.com/hami9/hamicloud/runtime/internal/store       4.210s
```

#### 7. Web Frontend Build
```bash
cd apps/web && npm run build
```
```text
vite v8.3.1 building client environment for production...
✓ 18 modules transformed.
dist/index.html                   0.45 kB │ gzip:  0.29 kB
dist/assets/index-CakGEGqm.css   17.25 kB │ gzip:  3.90 kB
dist/assets/index-BD3M7Bg-.js   252.58 kB │ gzip: 76.10 kB
✓ built in 1.15s
```

---

## 3. Real Cluster Deployment Step

```text
NOT RUN: No live Kubernetes cluster available in the execution environment
(kind not installed; kubectl connection to 127.0.0.1:61083 refused).
Milestones M1 and M2 are rejected and remain OPEN pending live cluster deployment verification.
```
