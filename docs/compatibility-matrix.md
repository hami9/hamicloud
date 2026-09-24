# Tested Compatibility Matrix

This document records the exact runtime versions, toolchain dependencies, and container image digests validated in the automated test suite and CI pipeline.

**Authoritative CI Run:** [Run 36027409142](https://github.com/hami9/hamicloud/actions/runs/36027409142)  
**Commit:** `8ce85ab` (main branch)

---

## 1. Runtimes & Languages

| Component | Policy Floor (ADR-0005) | Tested Version | Provenance / Evidence | Status |
| :--- | :--- | :--- | :--- | :---: |
| **Python** | 3.12+ | `3.12.9` (CI runner) / `3.14.5` (local dev) | GitHub Actions `setup-python@v5` log in Run 36027409142; `python --version` | **TESTED** |
| **Go** | 1.23+ | `1.23.6` (CI runner) / `1.27.1` (local dev) | GitHub Actions `setup-go@v5` log in Run 36027409142; `go version` | **TESTED** |

---

## 2. Infrastructure Services (Container Digests)

All container images are pinned by immutable cryptographic digest. Multi-architecture manifests are resolved via `docker buildx imagetools inspect`.

| Service | Pinned Image Reference & Digest | Source / Evidence | Status |
| :--- | :--- | :--- | :---: |
| **PostgreSQL** | `postgres:16-alpine@sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685` | `docker pull postgres:16-alpine`; CI service `postgres`; `deploy/compose/docker-compose.yml` | **TESTED** |
| **Redis (Compose)** | `redis:7.2-alpine@sha256:ccd6aa8d45ff3f033d6fa15b8cc1a50579f65c89f38cf9bb607a954c4f2128ed` | `docker pull redis:7.2-alpine`; `deploy/compose/docker-compose.yml` | **TESTED** |
| **Redis (CI)** | `redis:7-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499` | `docker pull redis:7-alpine`; `.github/workflows/ci.yml` | **TESTED** |
| **NATS JetStream** | `nats:2.10-alpine@sha256:b83efabe3e7def1e0a4a31ec6e078999bb17c80363f881df35edc70fcb6bb927` | `docker pull nats:2.10-alpine`; `deploy/compose/docker-compose.yml` | **TESTED** |
| **MinIO** | `quay.io/minio/minio:latest@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e` | `docker inspect hamicloud-minio`; `deploy/compose/docker-compose.yml` | **TESTED** |
| **Keycloak (IdP)** | `quay.io/keycloak/keycloak:24.0.5@sha256:f8ade94c1d0ad2f2fa7734a455fee5392764f402c43ca35e9af6bf63a2541dc9` | Decision D7 (pinned by owner); `docker pull quay.io/keycloak/keycloak:24.0.5`; `deploy/compose/docker-compose.yml` | **TESTED** |

---

## 3. Python Core & Development Toolchain

Exact package versions active during test and build execution:

| Package | Tested Version | Provenance / Evidence | Status |
| :--- | :--- | :--- | :---: |
| **FastAPI** | `0.141.1` | `pip freeze` | **TESTED** |
| **Starlette** | `1.6.0` | `pip freeze` | **TESTED** |
| **SQLAlchemy** | `2.0.52` | `pip freeze` | **TESTED** |
| **Alembic** | `1.20.0` | `pip freeze` | **TESTED** |
| **Pydantic** | `2.13.5` | `pip freeze` | **TESTED** |
| **asyncpg** | `0.31.0` | `pip freeze` | **TESTED** |
| **psycopg2-binary** | `2.9.13` | `pip freeze` | **TESTED** |
| **redis** | `8.1.0` | `pip freeze` | **TESTED** |
| **httpx** | `0.28.1` | `pip freeze` | **TESTED** |
| **pytest** | `9.1.1` | `pip freeze` | **TESTED** |
| **pytest-asyncio** | `1.4.0` | `pip freeze` | **TESTED** |
| **ruff** | `0.16.7` | `pip freeze` | **TESTED** |
| **mypy** | `2.3.1` | `pip freeze` | **TESTED** |
| **jsonschema** | `4.26.0` | `pip freeze` | **TESTED** |
| **openapi-spec-validator** | `0.9.0` | `pip freeze` | **TESTED** |

---

## 4. Components Not Yet Tested (Scheduled for Milestones M1–M4)

The following architectural components are defined in the Roadmap and Architecture documents but do not yet have automated pipeline tests in Milestone M0:

| Component | Target Version | Milestone Scheduled | Status |
| :--- | :--- | :--- | :---: |
| **Kubernetes Engine** | 1.30+ | Milestone M4 (Clustering & K8s Controller) | `not yet tested` |
| **Traefik Proxy** | v3.0+ | Milestone M3 (Ingress & Routing) | `not yet tested` |
| **BuildKit** | 0.13+ | Milestone M2 (Build Engine) | `not yet tested` |
| **Cilium CNI** | 1.15+ | Milestone M4 (Network Policies & Encryption) | `not yet tested` |
