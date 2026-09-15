# Master Plan — HamiCloud + HamiKnowledge

Single tracking file for both projects. It links the two roadmaps, fixes the execution order, and holds the milestone checklists you tick as you go.

**Status legend:** `[ ]` not started · `[~]` in progress · `[x]` closed with a committed evidence record

**Current position:** HamiCloud M0 — open. 13 of 45 boxes pass.

---

## 1. The two projects

| Project | One line | Roadmap | System prompt |
| --- | --- | --- | --- |
| **HamiCloud** | A self-hosted runtime: deploy an HTTP service or a finite job from a dashboard, see what happened, and recover when it fails. | `HamiCloud-Roadmap.md` | `HamiCloud-System-Prompt.md` |
| **HamiKnowledge** | A multi-user RAG workspace: upload technical documents, ask English/Persian questions, get answers with checkable citations and visible cost. | `HamiKnowledge-Roadmap.md` | `HamiKnowledge-System-Prompt.md` |

Both are planned work. Nothing here is evidence that something already exists, passes, or performs at a stated number.

## 2. Execution order — not negotiable

1. **HamiCloud** through M6 (v1.0): ~240–322 hours, 16–22 weeks at 15 hours/week.
2. **HamiKnowledge** through A5 (v1.0): ~88–119 hours, 6–8 weeks after that.

Combined: **328–441 hours**. Do not develop both implementations at once. HamiKnowledge runs as a tenant application on HamiCloud, so the second project is the practical test of the first project's public interfaces.

## 3. Ownership of the integration contract

HamiKnowledge submits ingestion as a finite HamiCloud job, using a service identity scoped to its own HamiCloud project and short-lived document-specific credentials.

- **HamiCloud owns** the runtime API, execution and retries. The authoritative description lives in `HamiCloud-Roadmap.md`.
- **HamiKnowledge owns** document versions, index validity and AI quality.
- `HamiKnowledge-Roadmap.md` **references** that contract; it does not redefine it. If the API changes, change the HamiCloud roadmap first.
- Keep PostgreSQL roles and databases separate even on one server. The HamiCloud control database is never exposed to the AI application.

## 4. Shared conventions — edit both roadmaps together

Duplicated in both files on purpose. Change one, change the other in the same commit:

- The README checklist common to both repositories.
- "How to close a milestone" — the evidence-record format.
- The weekly working rhythm and the stop rule.
- Benchmark honesty rules: targets are not results; report `PASS / FAIL / NOT RUN / INCONCLUSIVE`; never lower a target after a failing run to relabel it as passed.

## 5. How to close any milestone

An evidence record containing: milestone ID, commit SHA, environment, acceptance criteria, automated test links, manual demo results, measured outcomes and remaining limitations.

A mock-only test cannot establish live provider behavior. A local cluster test cannot establish remote host recovery. Record each validation boundary explicitly. If a correctness or isolation gate fails, the milestone stays open. For a performance target: either meet it, or make a dated scope revision with a measured rationale and rerun. **Do not mark unperformed checks as passed.**

**An unchecked box is an open milestone.** A milestone is closed when every box under it is checked *and* its evidence record is committed.

### The lifecycle of a milestone checklist

This file is the only living checklist. There is no second copy to drift out of sync with.

1. **Open** — the milestone's boxes live here, in full. Tick them here as work lands. Nowhere else.
2. **Close** — copy the filled-in section verbatim to `docs/evidence/<milestone>.md`, add the commit SHA and the links, and never touch that file again. It is a snapshot, not a duplicate: a dead record of what was true at one commit.
3. **Collapse** — replace the body here with three lines: the closed status, the date, and a link to the evidence file. The detail is preserved in the snapshot; keeping it here too would grow this file into a wall by M6.

A closed milestone that later turns out to be wrong does not get its old checklist edited back in. Open a new dated entry that says what was wrong and reopen the box.

---

# Part A — HamiCloud

## A.1 M0 — Design baseline `[~]`

Close M0 only when the contracts are specific enough that P1 can be implemented without inventing them mid-code.

Review 2026-09-13 covered commit `ca14896` plus the uncommitted working tree. A box is ticked only when the named artifact was checked and it holds. Each unticked box shows its gap inline.

### Architecture decision records

Four records exist, each short and decided — not a list of options.

**ADR-1 — Responsibility split**

- [x] States which decisions belong to the FastAPI control API, the Go scheduler, the Go execution workers, and Kubernetes.
- [x] Answers, in one sentence with no "it depends": *who decides that a job is allowed to start right now?*
- [x] States that HamiCloud chooses eligible work and Kubernetes chooses node placement.

**ADR-2 — Durable state**

- [x] States that PostgreSQL owns truth and NATS only wakes processing up.
- [ ] Describes what happens when a notification is lost, and names the periodic reconciliation scan that repairs it. — **gap:** the named scan (`status = 'QUEUED'`) covers jobs only; a lost deployment or cancellation notification has no repair.
- [x] States that a missing notification must never erase accepted work.

**ADR-3 — Delivery semantics**

- [x] States **at-least-once** delivery with idempotent control-plane transitions, explicitly.
- [x] Explicitly rejects any claim of exactly-once execution or exactly-once external side effects.
- [ ] States that workload code must tolerate being started more than once. — **gap:** absent from ADR-0003, which instead promises to "eliminate … duplicate execution".
- [x] Distinguishes an application retry (new attempt) from a broker redelivery (no new attempt).

**ADR-4 — Trust model**

- [ ] States the v1 posture: invited users, reviewed image allowlist, single cluster. — **gap:** none of the three appears in ADR-0004.
- [ ] Documents the **limits**, not only the controls — what this design does *not* defend against. — **gap:** "Tradeoffs" lists feature limits only; no threats are named.
- [ ] States that a namespace is a management boundary, not a hostile-code sandbox. — **gap:** ADR-0004 §2 is titled "Workload Sandboxing" and presents the namespace as the sandbox.

### Initial schema

Cheap now, expensive at M2. Verify each exists in the migration, not only in prose.

- [x] Unique constraint on `(workspace, endpoint, idempotency_key)`, plus a stored request-body hash for conflict detection.
- [ ] Unique consumed-event record **per handler**, not per `event_id` alone. — **gap:** unique on `(event_id, consumer_group)`; a consumer group is not a handler.
- [x] Logical job state stored separately from attempt state.
- [ ] `lease_epoch` present on the attempt/intent, and `resource_uid` present for matching the Kubernetes object. — **gap:** `resource_uid` exists on `job_attempts` only, not on `execution_intents` (service releases).
- [ ] Foreign keys and workspace scoping on every tenant table. — **gap:** `outbox_events` and `job_attempts` have no `workspace_id`; `applications.current_release_id` and `execution_intents.resource_id` have no foreign key.
- [ ] Outbox table with event ID, schema version and publish status. — **gap:** no `schema_version` column.
- [x] Quota reservation table with reservation ID, expiry and release status.
- [ ] One migration owner is named; Python migrations own the shared schema. — **gap:** no owner is named, and `alembic check` fails: the migration and the ORM models have drifted.

### API contract examples

Each behavior has a concrete request/response example committed, not just a description.

- [ ] Submitting a job without an idempotency key is rejected. — **gap:** rejected with 422, but in FastAPI's raw shape instead of `ErrorResponse`; no example in the contract and no test.
- [ ] Same key + different body → conflict. — **gap:** works, but the only example is a deploy test; nothing in the OpenAPI.
- [ ] Same key + identical body → the original accepted operation is returned. — **gap:** holds only when a key is sent; keyless rollbacks and all reruns create duplicates.
- [x] Deploying returns `202` with an operation ID, not a final result.
- [x] Every ID-based lookup checks workspace membership.
- [ ] Structured error codes, correlation ID and pagination are defined. — **gap:** pagination is undefined, `error_code` is unconstrained, and 422 responses bypass the error envelope.
- [ ] Idempotency key retention (at least 24 hours) is stated in the published contract. — **gap:** 24h exists only in code, and nothing reads `expires_at`.

### Pins and environment

- [ ] Versions and container digests are pinned — no `latest` anywhere. — **gap:** MinIO uses `:latest`, and no image is pinned by digest.
- [ ] The tested compatibility matrix is committed to the repository. — **gap:** ADR-0005 lists version floors (`3.12+`, `1.23+`), not a tested set.
- [ ] Local bootstrap (PostgreSQL, Redis, NATS, reference identity provider) is documented and reproducible from a clean machine. — **gap:** no identity provider in compose or in the quickstart.

### CI

- [ ] CI runs on the current commit and passes. — **gap:** no git remote is configured, so CI has never run. The Go job would fail (`runtime/go.sum` is missing), and ruff reports 152 errors.
- [ ] **The red path was tested:** a test was deliberately broken and CI failed as expected. A green badge that cannot go red is worse than no badge.
- [ ] Lint, type-check and contract validation are part of the pipeline, not manual steps. — **gap:** only ruff runs. No mypy and no OpenAPI or event-schema validation, so the spec broke in this diff and nothing caught it.

### The real review

Self-approval is weak evidence on a solo project. Replace it with a falsification attempt:

- [ ] Spend ~30 minutes writing the **first P1 endpoint and first migration against these contracts**. — not started.
- [ ] Record every point where you hesitated and had to invent a decision — each one is a place the contract is silent.
- [ ] Fold those decisions back into the relevant ADR or schema before closing M0.

### Evidence record

- [ ] Milestone ID: `M0`
- [ ] Commit SHA:
- [ ] Environment:
- [ ] Link to the four ADRs:
- [ ] Link to the migration and OpenAPI examples:
- [ ] Link to the first passing CI run:
- [ ] Link to the red-path CI run (deliberate failure):
- [ ] Remaining limitations and open questions:

## A.2 M1 — First live application `[ ]`

P1, 28–36 hours. OIDC, workspace API, approved image catalog, PostgreSQL/outbox, basic Go reconciliation, Deployment/Service/routing, dashboard and logs.

- [ ] A clean environment reaches a working service URL through the UI.
- [ ] Invalid readiness is visible to the user, not silent.
- [ ] Evidence record committed.
- [ ] **Re-estimate the remaining phases using actual time spent.**

## A.3 M2 — Usable MVP `[ ]`

P2, 36–48 hours. JetStream dispatch, intents, leases, retry policy, cancellation, artifact access, DLQ UI, manual service rollback.

- [ ] An invited user signs in and creates a workspace/project.
- [ ] Deploys an approved image, sees its HTTPS URL, opens a working HTTP service.
- [ ] Sees rollout progress, readiness failure and logs in the dashboard.
- [ ] Runs a finite job and downloads its authorized output.
- [ ] A controlled transient failure retries within the declared budget.
- [ ] Repeating the same submission returns the same operation.
- [ ] Cancels work and sees its confirmed final state.
- [ ] Rolls a service back to its previous healthy image/configuration.
- [ ] Restarting API, scheduler or executor does not silently lose accepted work.
- [ ] A fresh local installation reproduces this flow from documented steps.
- [ ] Duplicate submission, worker restart and failed job demos recorded.
- [ ] Evidence record committed.

## A.4 M3 — Source-to-URL `[ ]`

P3, 36–48 hours. Connected approved repository, signed webhook validation, commit resolution, isolated BuildKit task, registry push, digest-linked release.

- [ ] Two commits produce traceable releases.
- [ ] A failed build preserves the currently serving application.
- [ ] A duplicate webhook does not duplicate a build.
- [ ] Evidence record committed.

## A.5 M4 — Multi-user readiness `[ ]`

P4, 36–48 hours. Full RBAC, namespace policy, resource quotas, Redis rate limits, encrypted secrets, fair admission, audit export, network isolation.

- [ ] Two-workspace negative tests pass across API, logs, artifacts, secrets and network.
- [ ] A noisy workspace cannot bypass admission limits.
- [ ] Evidence record committed.
- [ ] **Gate:** until M4 passes, all execution stays in a private environment with trusted users and reviewed workloads.

## A.6 M5 — Operational evidence `[ ]`

P5, 32–44 hours. Dashboards, tracing, alerts, scale experiments, load tests, backup/restore, failure runbooks.

- [ ] Benchmark report published with raw data and honest target outcomes.
- [ ] Restore drill completed and reported.
- [ ] Controller failure report published.
- [ ] Alert-to-runbook exercise performed.
- [ ] 7-day observation window completed.
- [ ] Evidence record committed.

## A.7 M6 — HamiCloud v1.0 `[ ]`

P6, 20–28 hours. Helm release, clean install/upgrade checks, CI promotion, documentation, demo, limitations, security review closure.

- [ ] M0–M6 all have linked evidence; MVP journeys work from a clean installation.
- [ ] Source-to-image-to-URL works for an approved connected repository.
- [ ] Idempotency, retries, cancellation, DLQ inspection and rollback behave as documented.
- [ ] Scheduler/worker failure tests preserve consistent logical state.
- [ ] CI validates the release commit; the same image digests are promoted and smoke-tested.
- [ ] No unresolved critical/high security finding without an explicit scoped assessment. Any cross-tenant exposure or host privilege escalation blocks release.
- [ ] README, demo, release notes and limitations complete.
- [ ] Evidence record committed.

**Gate:** HamiKnowledge does not start before this box is ticked.

---

# Part B — HamiKnowledge

Starts only after M6. Checklists here are exit criteria; expand each into a full review list when you reach it, the way M0 was expanded.

## B.1 A0 — Evaluation baseline defined `[ ]`

R0, 8–10 hours. Licensed corpus, task boundaries, dataset split, quality rubric, model/license selection, CPU feasibility check.

- [ ] Development data and frozen test manifest committed.
- [ ] Test queries, relevance labels and answer rubrics are excluded from retrieval and tuning.
- [ ] Embedding and reranker model revisions pinned.

## B.2 A1 — Searchable workspace `[ ]`

R1, 14–18 hours. Auth, workspaces, uploads, parsing, chunks, source versions, ingestion jobs, lexical search, basic UI.

- [ ] Two users ingest separate content; duplicates and failures behave predictably.
- [ ] Evidence record committed.
- [ ] **Re-estimate remaining phases using actual time spent.**

## B.3 A2 — RAG MVP candidate `[ ]`

R2, 14–20 hours. Embeddings, vector search, RRF hybrid retrieval, cross-encoder reranking, cited answers, abstention.

- [ ] A question opens its supporting passage at the correct source location.
- [ ] All retrieval variants produce evaluation output from the same framework.
- [ ] An unsupported question produces an explicit insufficient-evidence response.

## B.4 A3 — Evaluated MVP `[ ]`

R3, 16–22 hours. Frozen eval run, ablations, latency/cost measurement, tracing, budgets, regression policy.

- [ ] B0–B3 compared on the same corpus, queries, model and rubric.
- [ ] Per-language results published with paired confidence intervals.
- [ ] The chosen default is justified by evidence, including negative results.
- [ ] Each query shows latency and usage/cost.

## B.5 A4 — Multi-user v1 candidate `[ ]`

R4, 10–15 hours. Deletion/reindex/revocation, concurrent budget tests, injection tests, bounded read-only tools.

- [ ] Revoked content is excluded from queries, previews and scoped caches.
- [ ] No cross-workspace exposure in the test matrix. Any confirmed exposure blocks release.
- [ ] The tool loop respects scope, step count, deadline and budget.
- [ ] Budgets hold under concurrent requests; no unreserved provider call.

## B.6 A5 — HamiKnowledge v1.0 `[ ]`

R5, 8–10 hours. HamiCloud deployment, CI quality gates, clean quickstart, release notes, model/data documentation, demo.

- [ ] HamiCloud deployment and the independent developer quickstart both work.
- [ ] Model/data documentation, traces, measured cost/latency, CI results and demo complete.
- [ ] Evidence record committed.

---

## If you are an AI agent

Load **one** project's system prompt and **one** roadmap per session. Do not pull scope from the other project into your answer. When a request crosses the boundary — for example, changing how ingestion jobs are submitted — say which project owns the decision and stop for confirmation before editing the other roadmap.

## Stop rule

Stop adding features when a v1 completion gate passes. Publish the release and its evidence, gather feedback from a small number of users, and write a new roadmap only for demonstrated needs.
