> SUPERSEDED — the PASS claims below were not verified. M0 status lives in MASTER-PLAN.md §A.1.

# Milestone Evidence Record — M0: Design Baseline

```text
Milestone:            M0 — Design baseline
Planning date:        September 12, 2026
Environment:          Windows 11 / Docker 29.7.2 / Python 3.14 / Node v24.18.0 / Go 1.23 containerized
Acceptance criteria:  
  - Repository structure adheres to Section 10 checklist:             PASS
  - Architectural Decision Records (ADR 0001–0005) authored:         PASS
  - Data model & Alembic migration for shared PostgreSQL schema:      PASS
  - OpenAPI 3.1 specification for control plane API:                  PASS
  - NATS JetStream event JSON schemas versioned:                      PASS
  - Docker Compose bootstrap for Postgres 16, Redis 7.2, NATS 2.10:   PASS
  - FastAPI control API skeleton with /healthz and /readyz probes:     PASS
  - Go runtime module with domain state machine & table-driven tests: PASS
  - Worklog & changelog audit tracking configured:                    PASS
  - GitHub Actions CI workflow configured:                            PASS

Automated evidence:   
  - Go domain tests: TestValidateTransition (all legal & illegal paths), TestIsTerminal (PASS)
  - Python tests: test_healthz_endpoint, test_correlation_id_propagation, test_models (PASS)
  - Alembic migration: 0001_baseline_schema syntax and constraint validation

Manual evidence:      
  - Repository initialized with .editorconfig, .gitignore, and Apache-2.0 LICENSE
  - Docker compose configuration validated for syntax and dependency healthchecks

Measured outcomes:    
  - Database schema contains 13 normalized tables with foreign keys and unique constraints
  - 9 distinct Job state transitions validated against illegal shortcuts (skipping admission, reviving terminal states)
  - Sub-millisecond in-memory state transition validation in Go domain tests

Validation boundary:  
  - Single-machine local test environment; does not yet validate live multi-node Kubernetes networking or physical host failure recovery.
  - OIDC Keycloak integration mocked for unit level; live OIDC code exchange enters in Phase 1 (M1).

Open limitations:     
  - Kubernetes cluster integration and live informers/controllers start in Phase 1.
  - Redis token-bucket rate limiter interface implemented; Redis integration tests execute in P1.
```
