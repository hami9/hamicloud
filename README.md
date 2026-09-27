# HamiCloud

HamiCloud is a self-hosted distributed application runtime designed to run HTTP services and finite background jobs on Kubernetes. It provides a control plane backed by PostgreSQL with durable execution intents, transactional outbox events, and deterministic state machine transitions. The system focuses on explicit operational boundaries, durable audit trails, and automated recovery when components fail.

## Current Status

HamiCloud M0 design baseline is closed with committed evidence records (`docs/evidence/M0.md`). Implemented in the repository:

- **Control API (FastAPI):** Tenant workspaces, application releases, finite jobs, transactional outbox dispatcher (NATS JetStream), OIDC token authentication, and idempotency engine.
- **PostgreSQL Database:** Bidirectional Alembic migrations defining durable state machine tables, execution intents with bounded leases, quotas, and outbox events.
- **Contract Specifications:** Published OpenAPI 3.1 specification (`contracts/openapi/v1.yaml`) and JetStream event schemas (`contracts/events/`).
- **Go Runtime Engine:** Go scheduler and execution worker daemons with intent claim loops, lease heartbeats, and retry/backoff policies.
- **Web Dashboard:** React + Vite single-page dashboard prototype in `apps/web`.

## How this project is built

HamiCloud is designed and directed by @hami9 and implemented with AI coding agents.

- The owner writes the roadmap, sets the scope of each milestone and makes the design
  decisions. Open questions are recorded as numbered decisions and wait for the owner's
  answer before work continues (see M0-WORK-ORDER.md, section 2).
- Antigravity, an AI coding agent, implements each phase from a written work order.
- Claude Code reviews every phase independently: it re-runs the tests, runs mutation
  tests and live probes, and rejects work whose claims it cannot reproduce.
- Each phase is accepted by the owner before the next one starts.

Progress is tracked in MASTER-PLAN.md. The engineering log is WORKLOG.md.

## Target Architecture

The following diagram represents the planned target architecture for the system:

```mermaid
flowchart TB
    U[Developer / Client] --> API[Control API\nFastAPI]
    API --> DB[(PostgreSQL 16\nState & Outbox)]
    DB --> DISPATCH[Outbox Dispatcher]
    DISPATCH --> BUS[NATS JetStream\nWork Notifications]
    BUS --> SCHED[Go Scheduler]
    SCHED --> DB
    SCHED --> EXEC[Execution Intents]
    EXEC --> WORK[Go Execution Workers]
    WORK --> KAPI[Kubernetes API]
    KAPI --> APP[HTTP Services]
    KAPI --> JOB[Finite Jobs]
    WORK --> DB
```

### Component Responsibilities

- **Control API (FastAPI):** Validates input, records tenant requests, writes desired state and outbox events within PostgreSQL transactions, and serves the public HTTP contract. It does not schedule workloads or call Kubernetes APIs directly.
- **Scheduler (Go):** Evaluates queued jobs and unapplied releases, reserves workspace quotas, and creates durable execution intents in PostgreSQL. It determines when work is eligible to run, while Kubernetes handles pod placement.
- **Execution Workers (Go):** Reconciles execution intents with the Kubernetes API using lease epochs, updates attempt outcomes, and cleans up terminated workloads.
- **PostgreSQL:** Authoritative store of system truth (state, attempts, releases, outbox events, and idempotency records). Queue messages only serve as wake-up notifications.
- **NATS JetStream:** Transports at-least-once notifications between the control plane and background workers.
- **Kubernetes:** Runs containers, manages pod lifecycles, and enforces resource limits.

## Local Setup

### Prerequisites

- Python 3.12+
- Go 1.23+
- Docker and Docker Compose
- PostgreSQL 16 (local installation or via Docker Compose)

### 1. Start Infrastructure Services

Start the local background services (PostgreSQL 16, Redis 7, NATS JetStream, MinIO, Keycloak):

```bash
docker compose -f deploy/compose/docker-compose.yml up -d
```

> [!NOTE]
> - **Redis Port:** Published on host port `6380` (via `${REDIS_HOST_PORT:-6380}`) to avoid collisions with any existing local Redis services (e.g. `aegis-redis` on port 6379).
> - **Keycloak (Reference IdP):** Runs on `http://localhost:8080` with admin credentials (`admin` / `adminpassword`) and pre-configured realm `hamicloud` with test users `alice` and `bob`.
> - **MinIO (S3 compatible):** Runs on `http://localhost:9000` (API) and `http://localhost:9001` (Console) with credentials `minioadmin` / `minioadminpassword`.

### 2. Configure Python Environment

Create a virtual environment at repository root (`.venv`) with Python 3.12 and install the API package with pinned development dependencies:

```bash
python -m venv .venv

# On Linux/macOS:
source .venv/bin/activate

# On Windows:
.venv\Scripts\activate

pip install -e "./apps/api[dev]"
```

### 3. Run Database Migrations

Apply database migrations using Alembic:

```bash
# On Linux/macOS:
DATABASE_URL_SYNC="postgresql://hamicloud:hamicloud_secret@localhost:5432/hamicloud" alembic -c migrations/alembic.ini upgrade head

# On Windows (PowerShell):
$env:DATABASE_URL_SYNC="postgresql://hamicloud:hamicloud_secret@localhost:5432/hamicloud"
alembic -c migrations/alembic.ini upgrade head
```

### 4. Run Verification Gates

Verify tests, types, linting, specs, and database alignment:

```bash
# Run API test suite
pytest apps/api/tests -v

# Check formatting and linting
ruff check apps/api

# Type check strictly
cd apps/api && mypy --explicit-package-bases app && cd ../..

# Verify database migrations are aligned with models
alembic -c migrations/alembic.ini check

# Validate OpenAPI contract
openapi-spec-validator contracts/openapi/v1.yaml

# Run Go runtime checks, format check, and tests
cd runtime
test -z "$(gofmt -l .)" || (echo "Unformatted Go files:" && gofmt -l . && exit 1)
go vet ./...
go test -v ./...
cd ..
```

### 5. Start the Control API and Runtime Daemons

Configure `ARTIFACTS_DIR` to ensure the Control API and the Go Executor daemon share the same storage directory for finite job outputs:

```bash
# On Linux/macOS (from repository root):
export ARTIFACTS_DIR="$(pwd)/var/artifacts"

# On Windows (PowerShell, from repository root):
$env:ARTIFACTS_DIR = "$((Get-Location).Path)\var/artifacts"
```

Run the development API server:

```bash
uvicorn app.main:app --app-dir apps/api --reload --port 8000
```

In development (`ENVIRONMENT=development`), requests authenticate using the header `X-Dev-Subject: <username>`. In production, this header is disabled and the API requires Bearer authentication.

Start the Go Scheduler and Execution Worker daemons:

```bash
# In separate terminals (inheriting ARTIFACTS_DIR and RUNTIME_DATABASE_URL):
cd runtime && go run ./cmd/hamicloud-scheduler
cd runtime && go run ./cmd/hamicloud-executor
```

## Repository Layout

```text
hamicloud/
  apps/
    api/                      # FastAPI control plane service and test suite
    web/                      # React + Vite dashboard UI prototype
  contracts/
    events/                   # NATS JetStream event schemas
    openapi/                  # OpenAPI 3.1 specification
  deploy/
    compose/                  # Local development compose definitions
  docs/
    adr/                      # Architecture Decision Records
  migrations/                 # Alembic database migrations
    versions/                 # Migration version scripts
  runtime/
    cmd/
      hamicloud-executor/     # Worker daemon entry point
      hamicloud-scheduler/    # Scheduler daemon entry point
    internal/
      config/                 # Runtime configuration and environment parsing
      domain/                 # Workload state machine and domain definitions
```

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.
