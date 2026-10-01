# Master Plan — HamiCloud + HamiKnowledge

Single tracking file for both projects. It links the two roadmaps, fixes the execution order, and holds the milestone checklists you tick as you go.

**Status legend:** `[ ]` not started · `[~]` in progress · `[x]` closed with a committed evidence record

**Current position:** HamiCloud M1 — open.

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

## A.1 M0 — Design baseline `[x]`

- **Status:** Closed
- **Closed Date:** 2026-09-26
- **Evidence Record:** [`docs/evidence/M0.md`](docs/evidence/M0.md)

## A.2 M1 — First live application `[ ]`

- **Status:** Reopened
- **Reopened Date:** 2026-10-01
- **Reason:** Closure rejected in engineering review. Workload runners only tested against fake clientset; real cluster deployment evidence required; dashboard hard-coded links and static labels must be removed.
- **Evidence Record:** [`docs/evidence/M1.md`](docs/evidence/M1.md)

## A.3 M2 — Usable MVP `[ ]`

- **Status:** Reopened
- **Reopened Date:** 2026-10-01
- **Reason:** Closure rejected in engineering review. Demos were not real recordings (UUIDs, resource names, log lines contradicted code); HTTPS URL and DLQ UI not met; superseded release race in ScanUnadmittedReleases and recovery defect (skipping RECOVERY_PENDING) must be resolved.
- **Evidence Record:** [`docs/evidence/M2.md`](docs/evidence/M2.md)

## A.4 M3 — Source-to-URL `[ ]`

- **Status:** Reopened
- **Reopened Date:** 2026-10-01
- **Reason:** Closure rejected in engineering review. BuildService was a simulator inventing digests/logs and writing HEALTHY and current_release_id via API; remove simulator, enforce repo allowlist, real BuildKit task, and registry push.
- **Evidence Record:** [`docs/evidence/M3.md`](docs/evidence/M3.md)

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
