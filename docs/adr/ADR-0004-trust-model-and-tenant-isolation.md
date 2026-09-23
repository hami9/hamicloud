# ADR-0004: Trust Model, Tenant Isolation, and Security Boundaries

- **Status:** Accepted
- **Date:** 2026-09-12
- **Authors:** HamiCloud Engineering Agent, hami9
- **Deciders:** hami9 (Project Owner)

---

## Context and Problem Statement

Multi-tenant platforms face severe threats if tenant isolation is treated as an afterthought:
1. Cross-tenant data leakage via API endpoints or shared object storage.
2. Container breakouts or privilege escalation onto host Kubernetes nodes.
3. Cross-site scripting (XSS) or session hijacking via tenant-generated logs or malicious HTTP responses.
4. Plaintext secret leakage through observability pipelines or message brokers.

HamiCloud must define clear, verifiable security boundaries from day one.

---

## Decision Drivers

1. **Defense in Depth:** Multiple independent barriers: API authorization, database row constraints, Kubernetes namespaces, network policies, and container security profiles.
2. **Restricted Workload Baseline:** Follow the Kubernetes [Restricted Pod Security Standard](https://kubernetes.io/docs/concepts/security/pod-security-standards/).
3. **Secret Confidentiality:** Secrets must remain confidential even if logs, traces, or broker queues are inspected.
4. **Origin & Cookie Scoping:** Browser isolation between the HamiCloud dashboard and user-deployed HTTP applications.

---

## Decision

### 1. Tenancy Model: Workspaces
- **Workspace:** The root isolation boundary for applications, jobs, releases, quotas, and secrets.
- **RBAC Roles:**
  - `owner`: Manage workspace members, delete workspace, full read/write.
  - `developer`: Deploy applications, run jobs, view logs, trigger rollbacks.
  - `viewer`: Read-only access to applications, jobs, and deployment statuses.
- **Mandatory Authorization Invariant:** Every API endpoint receiving an entity ID (e.g. `GET /v1/jobs/{job_id}`) MUST perform a tenant membership check ensuring the authenticated caller belongs to the owning workspace.

### 2. Kubernetes Workload Sandboxing
- **Namespace per Workspace:** Tenant workloads run in dedicated namespaces: `hc-ws-{workspace_id}`.
- **Pod Security Standard (`Restricted`):**
  - `securityContext.runAsNonRoot: true`
  - `securityContext.allowPrivilegeEscalation: false`
  - `securityContext.capabilities.drop: ["ALL"]`
  - `securityContext.seccompProfile.type: "RuntimeDefault"`
  - Host mounts (including `/var/run/docker.sock`), host IPC, host PID, and host network are strictly forbidden.
- **Network Policies:** Default deny ingress between different tenant namespaces. Explicit egress allowed for DNS and approved public endpoints.

### 3. Secret Protection
- Secrets are encrypted before being written to PostgreSQL using AES-256-GCM with a platform encryption key.
- Base64 is recognized as an encoding, NOT encryption.
- **Secrets are NEVER included in:**
  - NATS JetStream event bodies
  - Structured logs or stack traces
  - OpenTelemetry spans
  - API response bodies (the API returns only secret metadata: key version, name, created date)

### 4. Origin & Browser Session Security
- **Domain Separation:**
  - Dashboard origin: `dash.hamicloud.internal` (or custom control domain).
  - Tenant applications: `*.apps.hamicloud.internal` (or dedicated tenant subdomain).
- **Session Cookie Scoping:**
  - Dashboard session cookies are scoped strictly to the exact dashboard host (`domain: dash.hamicloud.internal`).
  - Wildcard cookies (`.hamicloud.internal`) are prohibited to prevent tenant apps from reading dashboard session tokens.
  - Flags: `HttpOnly; Secure; SameSite=Lax`.
- **Untrusted Output Rendering:**
  - Application logs and job output streams are treated as untrusted raw text; the dashboard UI sanitizes and escapes all strings prior to rendering to neutralize stored XSS.

### 5. Implementation Status & Development Seam (2026-09-14 — Decision D1)
- **Status:** In M0, tenant membership is enforced against real database `workspace_memberships` records for every tenant route, returning byte-identical 404s for non-members.
- **Development Seam Notice:** In development mode (`ENVIRONMENT=development`), caller identity is established via the `X-Dev-Subject` header. This is strictly an engineering and development seam (Decision D1) to enable automated test suites and local workflows without an external IdP, and is **NOT a security control**. In any non-development environment (`ENVIRONMENT=production` or `staging`), or when `ENVIRONMENT` is unset, the seam is inactive and the API rejects requests with HTTP 401 Unauthorized until OIDC bearer token authentication lands in Milestone M1.

### 6. Relational Workspace Scoping and Deletion Semantics (Decision D11)
- **Tenant Table Foreign Keys (`ON DELETE CASCADE`):** All tenant-scoped entities (`applications`, `releases`, `deployments`, `jobs`, `job_attempts`, `execution_intents`, `outbox_events`, `idempotency_records`, `workspace_memberships`, `secrets`, `quotas`) have a mandatory `workspace_id NOT NULL` referencing `workspaces(id)` with `ON DELETE CASCADE`. When a workspace is deleted, all tenant resources, queue intents, idempotency records, and pending/published outbox events are automatically and atomically cascaded.
- **Audit Event Retention (`ON DELETE SET NULL`):** `audit_events.workspace_id` is nullable and references `workspaces(id)` with `ON DELETE SET NULL`. Audit records must outlive tenant workspace deletion for regulatory compliance, security forensics, and operational audit history.
- **Deduplication Ledger Scope (`consumed_events`):** `consumed_events` is explicitly exempt from `workspace_id` scoping. It serves exclusively as an internal broker-level deduplication ledger tracking processed NATS JetStream event IDs per handler (`(event_id, handler)`). It is not tenant-owned data, carries no tenant state, and must guarantee handler idempotency across system-level and multi-workspace event processing without tenant coupling.

---

## Consequences

### Positive
- Strict isolation prevents cross-tenant data leakage.
- Container escape risks are minimized by dropping all Linux capabilities and enforcing non-root execution.
- Security defaults are part of the baseline codebase rather than patched in later.

### Negative / Tradeoffs
- Workloads requiring root permissions or privileged device access are unsupported in HamiCloud v1.
- Initial local development in `kind` requires configuring a CNI supporting NetworkPolicy enforcement (e.g., Calico or Kind default netpol) for isolation tests.
