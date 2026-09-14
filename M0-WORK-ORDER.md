# M0 Work Order — HamiCloud

**For:** the engineering agent working on this repository
**Source:** review of 2026-09-13 covering commit `ca14896` plus the uncommitted working tree
**Priority:** finish this work order before any other HamiCloud work. `HamiCloud-System-Prompt.md` and `HamiCloud-Roadmap.md` still govern *how* you work (invariants, stack, code standards). If a task here conflicts with either of them, stop and ask the owner.
**Lifetime:** temporary. It is a list of instructions, not a checklist. Delete it when M0 closes (task T30).

---

## 0. Rules for this round

The previous round marked M0 "COMPLETED & VERIFIED" when 12 of 45 acceptance boxes held. These rules exist to make sure that doesn't happen again.

1. **One checklist.** `MASTER-PLAN.md` §A.1 is the only place where M0 progress is recorded. Tick boxes there and nowhere else. When you close a box's gap, tick it and delete its `— gap:` note. Do not add checkboxes to this file.
2. **A tick requires proof.** Before ticking, check the artifact the box names (the migration, the ADR, the contract, the CI run — whichever it says) and run the task's *Done when* check. Record the command and its real output in a WORKLOG entry.
3. **Report outcomes honestly:** `PASS / FAIL / NOT RUN / INCONCLUSIVE`. Never write PASS for a check you did not run. If a check cannot run on this machine, write NOT RUN and say why.
4. **Never pass a check by weakening it.** Do not loosen a test, the contract, an ADR, or the checklist wording to make something green.
5. **OWNER items belong to hami9.** Prepare what you can, then stop and ask. Never tick a box whose task is marked OWNER.
6. **Contract before code** (System Prompt §6). If a task needs a decision that the Roadmap doesn't make, or that contradicts it, and the decision isn't settled in §2 below, ask one question and wait.
7. **Git:** Conventional Commits, one commit per phase. Do not create a remote, push, or change repository settings; those are OWNER actions.
8. **`WORKLOG.md` is append-only.** Correct an earlier claim with a new dated entry. Never edit old entries.
9. **Tests ship with the code.** Every task that changes behavior includes the test that would catch its failure. Transaction and constraint guarantees are tested against the real PostgreSQL, never a mock.

---

## 1. Verified state at review time

Treat these as facts; there is no need to rediscover them.

| Area | State |
| --- | --- |
| Tests | 11 pytest tests and the Go domain tests pass, and `go vet` is clean. None of them catch the defects below. |
| OpenAPI | Valid at `ca14896`. **Invalid in the working tree**: the reruns 202 schema is `null`. |
| Lint / types | `ruff check apps/api`: 152 errors (135 already at `ca14896`) with the unpinned ruff 0.16.7. Strict mypy: 1 error. |
| Schema | `alembic check` fails: the ORM models and migration `0001` have drifted. |
| CI | No git remote is configured, so CI has never run. `runtime/go.sum` is missing, so the Go job would fail. |
| Tenant isolation | None. No authentication exists, and the `X-Workspace-ID` check is skipped when the header is absent or empty. Confirmed with live requests. |
| Go runtime | Scheduler and executor only load config and wait for a signal. Nothing drains the outbox, and nothing opens a database connection. |
| Toolchain | `.venv` is Python 3.14; CI and `pyproject.toml` target 3.12, which is not installed locally. Go 1.27.1 is at `C:\Program Files\Go\bin` (not on the bash PATH); CI pins 1.23. |
| Race detector | `go test -race` cannot run on this machine: gcc fails on the space in `C:\Users\pc city`. Local race results are **NOT RUN**; only CI can produce them. |
| Dev database | Holds dozens of leftover rows from test runs and review probes. The probes also deleted one release, backdated one idempotency record, and forced some jobs to `FAILED`. |

### How to run the checks

```bash
# Git Bash, from the repository root
export DATABASE_URL="postgresql+asyncpg://hamicloud:hamicloud_secret@localhost:5432/hamicloud"
export DATABASE_URL_SYNC="postgresql://hamicloud:hamicloud_secret@localhost:5432/hamicloud"
export REDIS_URL="redis://localhost:6380/0"

./.venv/Scripts/python.exe -m pytest apps/api/tests -v
./.venv/Scripts/python.exe -m alembic -c migrations/alembic.ini check
./.venv/Scripts/python.exe -m ruff check apps/api
(cd apps/api && ../../.venv/Scripts/python.exe -m mypy app --explicit-package-bases)
./.venv/Scripts/python.exe -c "import yaml; from openapi_spec_validator import validate; validate(yaml.safe_load(open('contracts/openapi/v1.yaml', encoding='utf-8'))); print('VALID')"
```

```powershell
# PowerShell, Go
$env:PATH = "C:\Program Files\Go\bin;$env:PATH"
Set-Location runtime; go vet ./...; go test ./...
```

Without `--explicit-package-bases`, mypy aborts on duplicate module names. `openapi-spec-validator` was installed into `.venv` by hand during the review; add it to the dev dependencies (T24).

---

## 2. Decisions — read before starting

Each decision has a default. Unless the owner has changed one, work with the default. Items marked **OWNER** must be answered by the owner before the tasks that depend on them.

| ID | Decision | Default | Blocks |
| --- | --- | --- | --- |
| D1 | How M0 establishes the caller's identity before OIDC arrives in M1 | A single `get_caller` dependency. With `ENVIRONMENT=development` it reads a dev-only subject header; in any other environment it returns 401. The membership lookup behind it is real. Label it in code and in ADR-0004 as a development seam, not a security control. | T7 |
| D2 | `GET /v1/operations/{operation_id}` is not in the Roadmap's API table | Approve it and add it to the Roadmap table, since every 202 `status_url` already points at it. **OWNER** approves the Roadmap edit. | T4 |
| D3 | Pagination needs at least one list endpoint, and the Roadmap defines none | Add `GET /v1/workspaces/{ws}/jobs` and `GET /v1/apps/{app}/releases` to the Roadmap table. **OWNER** approves. | T12 |
| D4 | A cancellation that races a workload finishing | The logical job ends `CANCELLED`; the attempt keeps `SUCCEEDED` and its exit code. There is no `CANCEL_REQUESTED → SUCCEEDED` edge. | T20, T21 |
| D5 | Go transitions the Roadmap diagram does not allow: `QUEUED→CANCELLED`, `ADMITTED→CANCELLED`, `ADMITTED→FAILED`, `RETRY_WAIT→CANCELLED`, `CANCEL_REQUESTED→FAILED`, `CANCEL_REQUESTED→SUCCEEDED` | Remove them all, following the Roadmap literally. The owner may keep one only by amending the Roadmap first, with a reason. | T21 |
| D6 | Python version | 3.12 everywhere. **OWNER** installs 3.12 locally; then recreate `.venv`. | T24 |
| D7 | Keycloak version for the local reference identity provider | **OWNER** pins it. Do not guess a version. | T27 |
| D8 | GitHub repository and remote | **OWNER** creates and pushes. | T28 |
| D9 | Commit `MASTER-PLAN.md` (currently untracked) | Yes, in the Phase 1 commit. | Phase 1 |
| D10 | Reset the dev database after the new migrations are tested on it | Yes (`alembic downgrade base && alembic upgrade head`). **OWNER** confirms before you run it. | T29 |
| D11 | `ON DELETE` for the new `workspace_id` foreign keys | `CASCADE`, like the other tenant tables. `audit_events` keeps `SET NULL` because audit records outlive a workspace; state this in ADR-0004. | T16, T20 |

---

## 3. Work order

Work through the phases in order. Tasks within a phase may be done in any order. Line numbers were correct at review time; locate code by function name.

### Phase 1 — Repair what the last round broke

#### T1 · Repair the OpenAPI document — `blocker`

- **Problem:** In `contracts/openapi/v1.yaml`, the `/operations/{operation_id}` block was inserted between `schema:` and its `$ref` in the `rerunJob` 202 response. The reruns response schema became `null` and the whole document fails validation. Every tool generated from the contract rejects it.
- **Do:** Put `$ref: '#/components/schemas/AcceptedOperationResponse'` back under the reruns 202 `schema:`. Re-add `/operations/{operation_id}` as a separate sibling path.
- **Done when:** the validator command in §1 prints `VALID`.

#### T2 · Guard the contracts with tests — `major`

- **Problem:** Nothing in the repository reads the contract files, which is why T1 went unnoticed.
- **Do:** Add `apps/api/tests/test_contracts.py`. It must check that:
  - the OpenAPI document validates;
  - every file in `contracts/events/` is valid JSON Schema;
  - every outbox topic the API emits has a schema file in `contracts/events/`.
  Keep one registry of emitted topics in code, not scattered string literals.
- **Done when:** the tests pass, and they fail if you reintroduce the T1 splice or delete an event schema (try both, then revert).

#### T3 · Remove the fake event stream — `major`

- **Problem:** `stream_operation_events` in `operations.py` yields a hard-coded "connected" event and two "progress" events with no real event source. `Last-Event-ID` renumbers events but replays nothing. The test asserts the fake output. The Roadmap requires an *authorized SSE stream with a reconnect cursor and bounded retention*, which is not M0 scope.
- **Do:** Make the endpoint return `501` with the standard error envelope (`error_code: NOT_IMPLEMENTED`) until the real stream lands. Keep the path in the contract, documented as not yet implemented, with the cursor and retention semantics described. Delete the assertions that lock in the fake events.
- **Done when:** a request returns `501` with `ErrorResponse` and a correlation ID, and a test asserts exactly that.

#### T4 · Give operation status its own contract — `major` — depends on D2

- **Problem:** `GET /v1/operations/{id}` returns `IMAGE_READY`, `QUEUED` and similar, but its declared schema (`AcceptedOperationResponse`) allows only `status: [ACCEPTED]`. The new test also locks in the violating value. And `status_url` is inconsistent: `submit_job` points to `/v1/jobs/{id}`, while deploy points to `/v1/operations/{id}`.
- **Do:**
  - Once D2 is approved, add the endpoint to the Roadmap API table.
  - Define `OperationStatusResponse` in the contract and in Pydantic, with an operation kind and a `status` enum listing the real values.
  - Make every 202 return one `status_url` form, `/v1/operations/{operation_id}`.
- **Done when:** a test validates live responses from this endpoint against the OpenAPI schema.

#### T5 · Correct the records — `major`

- **Do:**
  - Append a dated correction entry to `WORKLOG.md` that withdraws "COMPLETED & VERIFIED" and corrects each false claim:
    - "enforces caller workspace membership" — there is no authentication or membership check.
    - "7 tests passed" — there were 11 at review.
    - "13 table-driven test cases … 0 race warnings" — there are 14 subtests; the race detector cannot run locally, and CI has never run.
    - Moving MinIO to `:latest` violates the pinning rule.
  - Add this banner at the top of `docs/evidence/M0-baseline-evidence.md`:
    `> SUPERSEDED — the PASS claims below were not verified. M0 status lives in MASTER-PLAN.md §A.1.`
    Do not edit the rest of that file; T30 deletes it.
- **Done when:** both edits are committed and no older WORKLOG entry was modified.

#### T6 · Isolate the test database — `major`

- **Problem:** `conftest.py` runs every test against the dev database with no rollback, so each run leaves permanent rows behind.
- **Do:** Run the suite against a dedicated database (for example `hamicloud_test`), created and migrated with `alembic upgrade head` once per session. Give each test clean state through transaction rollback or truncation. Tests must never touch the dev database.
- **Done when:** row counts in the dev database are identical before and after a full test run (show both counts in the WORKLOG), and the suite passes twice in a row.

**Commit Phase 1** (includes the current working tree and `MASTER-PLAN.md`, per D9).

---

### Phase 2 — Tenant isolation

#### T7 · Enforce membership on every tenant route — `blocker` — depends on D1

- **Problem:**
  - No authentication exists.
  - The `if x_workspace_id:` checks in `apps.py`, `jobs.py` and `operations.py` are skipped when the header is absent or empty.
  - `create_application` and `submit_job` check nothing, so anyone can create apps or queue jobs in any workspace.
  - `get_job` also accepts a `workspace_id` query parameter that overrides the header.
  - `create_workspace` makes every workspace owned by the literal subject `"default-admin"`.
  - `workspace_memberships` is never read.
- **Do:**
  - Add `get_caller` (D1) and one authorization dependency that looks up `workspace_memberships` for the caller's subject.
  - For path-scoped routes (`/workspaces/{ws}/…`), check membership of the path workspace.
  - For ID-scoped routes (`/jobs/{id}`, `/apps/{id}`, `/operations/{id}`), load the resource, then check membership of its `workspace_id`.
  - A caller who is not a member gets the **same 404 body** as a missing ID. A member with too low a role gets 403.
  - Roles follow ADR-0004 §1: `viewer` reads; `developer` deploys, runs, cancels, reruns and rolls back; `owner` can do everything.
  - Delete every `X-Workspace-ID` read and the `workspace_id` query parameter. The client never supplies the workspace for authorization.
  - `create_workspace` records the caller as `OWNER`.
  - Cover all tenant routes: create app, deploy, rollback, submit job, get job, cancel, rerun, operation status, and operation events.
- **Done when:** tests use two real workspaces and two subjects, and for every route listed show that:
  - a member succeeds;
  - a non-member using a *real* resource ID gets 404, byte-identical to a missing ID;
  - a request with no identity gets 401;
  - a viewer's mutation gets 403.

  The old test that used a random UUID as the "foreign" workspace is replaced.
- **Unblocks:** "Every ID-based lookup checks workspace membership."

#### T8 · Publish the authorization contract — `major`

- **Do:**
  - Add a `securitySchemes` entry for OIDC bearer tokens plus global `security`.
  - Document 401, 403 and 404 on every tenant route.
  - State in the API description that workspace access comes from the authenticated subject's membership.
  - Keep the development identity header out of the public contract; document it only in the README development section.
- **Done when:** the spec validates, and every tenant operation lists 401 and 404.

**Commit Phase 2.**

---

### Phase 3 — Idempotency and the API contract

#### T9 · One idempotency path for every mutating endpoint — `major`

- **Problem:**
  - `rollback_release` applies idempotency only when a key happens to be sent; `rerun_job` never does. Repeating either request creates duplicate work (confirmed live).
  - The same lookup, hash and store logic is copied three times.
  - `cancel_job` takes no row lock and writes a new outbox event on every call.
  - `rerun_job` takes no row lock.
- **Do:**
  - Require `Idempotency-Key` on submit, deploy, rollback, rerun and cancel, in both the code and the contract.
  - Move lookup, hashing, storage and race handling into one shared module used by all five.
  - Lock the parent row in `rerun_job` and `cancel_job`.
  - A cancel on a job already in `CANCEL_REQUESTED` returns the original operation without writing another outbox event.
- **Done when:** for each of the five endpoints, tests show that:
  - same key and same body return the same `operation_id`, with exactly one domain row and one outbox event;
  - same key and a different body return 409 with `error_code: IDEMPOTENCY_CONFLICT`;
  - a missing key returns the error envelope (T12).

  Also required: a concurrency test in which N truly overlapping requests (async client, not sequential `TestClient` calls) produce one operation.
- **Unblocks:** "Same key + different body → conflict." and "Same key + identical body → the original accepted operation is returned." (the committed examples come from T13).

#### T10 · Enforce and publish key retention — `major`

- **Problem:** `expires_at` is written but never read, so retention is effectively unlimited. A record backdated 400 days still replayed. The 24 hours appears only in code.
- **Do:**
  - Lookups ignore records whose `expires_at` has passed.
  - Every `Idempotency-Key` parameter description in the contract states: "Retained for at least 24 hours; after that the key may be treated as new."
  - Record in ADR-0003 which component purges expired rows, and in which milestone.
- **Done when:** a test with a backdated record gets a new operation, and the contract text is present.
- **Unblocks:** "Idempotency key retention (at least 24 hours) is stated in the published contract."

#### T11 · Correct failure handling in deploy and rollback — `major`

- **Problem:**
  - `release_number = COUNT(*) + 1`. After any release is deleted, every deploy returns 409 and every rollback returns 500, permanently.
  - The `except IntegrityError` handlers report *any* integrity violation as a concurrency conflict.
  - `rollback_release` commits with no handler at all.
- **Do:**
  - Allocate `release_number` as `MAX + 1` under the existing application-row lock.
  - Replay the stored response only when the violated constraint is `uq_idempotency_workspace_key`; re-raise anything else as a 500 with the envelope. Before relying on how asyncpg exposes the constraint name through SQLAlchemy's `exc.orig`, verify it against the installed versions.
- **Done when:** deleting a middle release and then deploying returns 202 with the next number, and a forced non-idempotency violation returns 500, not 409.

#### T12 · Complete the error and pagination contract — `major` — depends on D3

- **Problem:**
  - Request-validation failures return FastAPI's raw `{"detail": …}` with no `error_code` and no correlation ID.
  - `error_code` is an unconstrained string, and its only example (`WORKSPACE_QUOTA_EXCEEDED`) is never emitted.
  - The `X-Correlation-ID` response header is undocumented.
  - Pagination is not defined anywhere.
- **Do:**
  - Add a `RequestValidationError` handler that returns `ErrorResponse` with `VALIDATION_ERROR`, the Pydantic errors in `details`, and the correlation ID.
  - Turn `error_code` into an enum of exactly the codes the API emits, and keep the enum and the handlers in sync.
  - Document `X-Correlation-ID` as a response header on every response.
  - Define cursor pagination once (a `limit` with a documented maximum, a `cursor`, and a page envelope carrying `next_cursor`), and implement it on the D3 endpoints.
- **Done when:** a test checks that every error path returns the envelope with a `correlation_id` equal to the response header, and a pagination test walks at least two pages.
- **Unblocks:** "Structured error codes, correlation ID and pagination are defined."

#### T13 · Commit concrete request/response examples in the contract — `major`

- **Do:** Add OpenAPI `examples` for each behavior the M0 section requires:
  - a job submitted without a key, and its error;
  - the same key with a different body, and the 409;
  - the same key with an identical body, and the original 202;
  - a deploy returning 202 with an operation ID;
  - a non-member lookup returning 404;
  - a paginated list.

  Each example must match what the running API actually returns.
- **Done when:** a test sends each example request and checks that the live response matches the documented example.
- **Unblocks:** "Submitting a job without an idempotency key is rejected." together with T9/T10.

#### T14 · Complete the event contracts — `minor`

- **Problem:** `cancel_job` emits `job.cancellation.requested.v1`, which has no schema file. Fields the producers always set are marked optional.
- **Do:**
  - Add `contracts/events/job.cancellation.requested.v1.json`.
  - Make `port` and `health_path` required in `app.deployment.requested.v1`.
  - Make `command_args`, `timeout_seconds` and `max_retries` required in `job.submitted.v1`.
  - Leave `is_rollback`, `target_release_id` and `parent_job_id` optional.
- **Done when:** a test validates outbox rows written by the real endpoints against their schemas.

#### T15 · Fix the readiness probe — `minor`

- **Problem:** `readyz` imports `redis.asyncio` inside the handler and opens a new client on every probe. The lifespan startup does nothing.
- **Do:** Create one Redis client in `lifespan` and close it on shutdown. Add tests with Redis reachable and unreachable. If CI runs those tests, add a Redis service to the CI job.
- **Done when:** both readiness tests pass.

**Commit Phase 3.**

---

### Phase 4 — Schema

Add new Alembic revisions. **Do not edit `0001`**, because it has already been applied. Make each migration work on a non-empty table (write the backfill), and test it on the current dev database before T29 resets it.

#### T16 · Close the schema gaps — `major`

- **Do:**
  - `outbox_events`: add `schema_version` (NOT NULL) and `workspace_id` (NOT NULL, FK per D11). Backfill both, and set them at every producer through one outbox helper.
  - `consumed_events`: replace `consumer_group` with a handler name, unique on `(event_id, handler)`.
  - `job_attempts`: add `workspace_id` (NOT NULL, FK), backfilled from `jobs`.
  - `execution_intents`:
    - add `resource_uid`;
    - add a unique constraint on `(resource_type, resource_id, target_generation)` so that duplicate intents fail;
    - replace the polymorphic `resource_id` with typed nullable foreign keys `job_attempt_id` and `release_id`, plus a CHECK that exactly the one matching `resource_type` is set.
  - `applications.current_release_id`: add a foreign key to `releases.id` with `ON DELETE SET NULL`, created after both tables exist.
  - Add CHECK constraints on the state and status columns, which currently accept any string.
- **Done when:** the WORKLOG shows `\d` output for each changed table, and the T18 constraint tests pass.
- **Unblocks:** "Unique consumed-event record per handler…", "`lease_epoch` … and `resource_uid` …", "Foreign keys and workspace scoping on every tenant table.", "Outbox table with event ID, schema version and publish status."

#### T17 · Make the migration and the models agree — `major`

- **Problem:** `alembic check` reports pending operations: mismatched index names, two indexes the models declare but the migration never created, enum-versus-VARCHAR types, and unique constraint versus unique index.
- **Do:** Resolve every difference, deciding item by item which side is correct. Never shrink an existing column.
- **Done when:** `alembic check` prints `No new upgrade operations detected.`

#### T18 · Replace `test_models.py` with real constraint tests — `major`

- **Problem:** `test_models.py` builds ORM objects and asserts back the values it just set, with no database. It would pass if every constraint were deleted, yet the evidence record cites it as "constraint validation".
- **Do:** Against the test database, deliberately violate each constraint the schema boxes rely on and assert that the database rejects it:
  - idempotency uniqueness;
  - attempt-number uniqueness;
  - consumed event per handler;
  - intent uniqueness;
  - an orphan `workspace_id`;
  - an invalid state value.
- **Done when:** every one of those tests passes, and each fails if you drop its constraint in a scratch database (check at least one this way and record the result).

#### T19 · Name the migration owner — `minor`

- **Do:** Add `.github/CODEOWNERS` with `/migrations/ @hami9`, and state in ADR-0005 that `hami9` owns migrations and that Python migrations own the shared schema.
- **Unblocks:** "One migration owner is named; Python migrations own the shared schema." (together with T17)

**Commit Phase 4.**

---

### Phase 5 — Design records

#### T20 · ADR fixes — `major`

- **ADR-0002:**
  - Its reconciliation scan query (`status = 'QUEUED'`) covers jobs only. Name a scan, with its period, for every kind of accepted work:
    - queued jobs;
    - releases whose desired generation has no applied intent;
    - pending cancellations;
    - `PENDING` outbox rows older than a stated age.
  - State who deletes published outbox rows after the 7-day retention.
  - **Unblocks:** "Describes what happens when a notification is lost, and names the periodic reconciliation scan that repairs it."
- **ADR-0003:**
  - Add the Roadmap requirement: Kubernetes can start a job's program more than once, so **workload code must tolerate being started more than once**. Applications that modify external systems need their own idempotency keys or fencing at the destination.
  - Replace "eliminate race conditions, duplicate execution, and state corruption" (line 16), because it is the exactly-once claim the ADR itself forbids.
  - Replace "Total protection against zombie executor updates" (line 90): database fencing protects database state, not a stale process's external API calls.
  - Add the job state machine exactly as T21 defines it, including `RECOVERY_PENDING` and decision D4.
  - Add the key retention and purge ownership from T10.
  - **Unblocks:** "States that workload code must tolerate being started more than once."
- **ADR-0004:**
  - State the v1 posture: invited users, a reviewed image allowlist, a single cluster, and no anonymous code execution before M4.
  - Rename §2 "Kubernetes Workload Sandboxing" to "Workload Hardening Baseline". Add: *a namespace is a management boundary, not a hostile-code sandbox; tenant pods share the node kernel.*
  - Add a "Limits" section naming what v1 does **not** defend against: container escape through the shared kernel, CPU and kernel side channels, noisy neighbours beyond quota enforcement, malicious code inside an allowlisted image, a compromised platform operator or encryption key.
  - Replace line 76 ("Strict isolation prevents cross-tenant data leakage.") with a design goal and the test that must prove it (the M4 two-workspace negative tests).
  - Add a dated implementation-status note: authentication is a development seam until OIDC lands in M1 (D1).
  - Record the `audit_events` `SET NULL` choice (D11).
  - **Unblocks:** all three ADR-4 boxes.
- **ADR-0005:** It lists version floors (`3.12+`, `1.23+`, `2.10+`), which is not a tested matrix. Move the tested versions to T26 and keep ADR-0005 as the policy.

**Commit Phase 5.**

---

### Phase 6 — Go runtime

#### T21 · Make the job state machine match the Roadmap, from one source — `major` — depends on D4, D5

- **Problem:**
  - The new `CANCEL_REQUESTED → SUCCEEDED` edge is not in the Roadmap. Because `ValidateTransition(current, next)` has no memory, it also legalizes `QUEUED → CANCEL_REQUESTED → SUCCEEDED` for a job that never ran.
  - Five older edges also depart from the Roadmap (D5).
  - `RECOVERY_PENDING` (Roadmap: when an attempt's node is unreachable) exists in no enum.
  - The Python write path (`cancel_job`, `rerun_job`) validates no transitions, and the two implementations can drift.
  - The Go tests check sample transitions, so widening the table would go unnoticed.
- **Do:**
  - Add `contracts/state-machines/job.v1.json` listing every state and every legal edge, exactly as the Roadmap diagram and D4/D5 define them, plus `RECOVERY_PENDING` with its edges as recorded in ADR-0003.
  - Go and Python each load that file in a test and assert that their transition table is **exactly equal** to it.
  - Validate transitions in the Python write path.
  - Add `RECOVERY_PENDING` to the Go enum, the Python enum, both OpenAPI enums, and the T16 CHECK constraint.
- **Done when:**
  - both equality tests pass;
  - adding any extra edge to either table makes its test fail (try it once and record the result);
  - an API test shows that an illegal transition is rejected.

#### T22 · Replace the silent DSN rewrite in `config.go` — `minor`

- **Problem:** `LoadFromEnv` quietly rewrites any `postgresql+…://` URL to `postgres://…`, and nothing tests it. It also cannot matter yet, because nothing in the runtime connects to the database.
- **Do:**
  - Read a dedicated `RUNTIME_DATABASE_URL` in pgx form, and add it to `.env.example`.
  - If the value uses the SQLAlchemy `postgresql+driver://` form, return an error that names the variable. Do not rewrite it.
  - Add a table-driven `config_test.go` covering valid values, missing values, the rejected SQLAlchemy form, and invalid integers.
- **Done when:** `go test ./internal/config/` passes.

#### T23 · Record `go.sum` — `major`

- **Do:** Run `go mod tidy` in `runtime/` and commit `go.sum`. Tidy will drop the currently unused `pgx`, `nats.go` and `uuid` requirements; add them back when code uses them.
- **Done when:** `runtime/go.sum` exists and `go build ./...` succeeds.

**Commit Phase 6.**

---

### Phase 7 — Toolchain, CI, pins, bootstrap

#### T24 · Pin the Python toolchain — `major` — depends on D6

- **Do:**
  - Recreate `.venv` with Python 3.12.
  - Pin the dev tools exactly, including ruff, mypy, pytest and `openapi-spec-validator`, and commit a lock file generated in that environment.
  - Fix the ruff findings. The `F821` errors in `app/models/*` are SQLAlchemy string annotations: import those names under `TYPE_CHECKING` rather than suppressing the rule.
  - Fix the one mypy error, the untyped `call_next` in `correlation_id_middleware`.
- **Done when:** `ruff check apps/api` reports 0 errors and strict mypy (the §1 command) reports 0, both with the pinned versions.

#### T25 · Complete the CI pipeline — `major`

- **Do:** Extend `.github/workflows/ci.yml`:
  - **Python job:** mypy; `alembic check` after `alembic upgrade head`; the T2 contract tests; a Redis service if T15's tests need it.
  - **Go job:** a `gofmt -l` gate (the current step is named "format and vet" but only runs vet); keep `-race`, which works on the Ubuntu runners.
  - **Pinning:** pin the actions to commit SHAs and service images to digests.
- **Done when:** every step of the file runs green on GitHub (requires T28).
- **Unblocks:** "Lint, type-check and contract validation are part of the pipeline, not manual steps."

#### T26 · Commit the tested compatibility matrix — `major`

- **Do:** Create `docs/compatibility-matrix.md` with the exact versions and image digests that a green CI run actually used, linked to that run.
  - Read every value from a real source: the CI log, `pip freeze`, `go version`, `docker image inspect`. **Never invent a version or digest.**
  - List components with no test yet (Kubernetes, Traefik, BuildKit and so on) as `not yet tested`.
- **Done when:** every row cites where its value came from.
- **Unblocks:** "The tested compatibility matrix is committed to the repository."

#### T27 · Pin images and add the reference identity provider — `major` — depends on D7

- **Problem:** MinIO uses `:latest`, and no image is pinned by digest. There is no identity provider in Compose, although the M0 bootstrap requires the reference one (Roadmap: Keycloak for local reference).
- **Do:**
  - Pin every image in `deploy/compose/docker-compose.yml` and in CI by digest (`name:tag@sha256:…`), resolving real digests with `docker buildx imagetools inspect <image>:<tag>`. Replace MinIO `latest` with a dated `RELEASE.` tag.
  - Add Keycloak at the D7 version with a healthcheck and a committed development realm import (one client and two test users for the two-workspace tests).
  - Update the README quickstart: Python 3.12, where `.venv` lives (the README says `apps/api/.venv`, but the repository uses the root `.venv` — pick one), MinIO, Keycloak, and the verification steps.
  - Prove the bootstrap works from a clean state: clone into a fresh directory, follow the README word for word, and record every place you had to deviate.
  - Note that `aegis-redis` already holds port 6379 on this machine.
- **Done when:**
  - `grep -nE "image:" deploy/compose/docker-compose.yml .github/workflows/ci.yml | grep -v "@sha256:"` prints nothing;
  - the clean-clone run is recorded in the WORKLOG with no unexplained deviations.
- **Unblocks:** "Versions and container digests are pinned — no `latest` anywhere." and "Local bootstrap (PostgreSQL, Redis, NATS, reference identity provider) is documented and reproducible from a clean machine."

#### T28 · First green run and the red path — **OWNER** — depends on D8

- **Owner:** create the GitHub repository, add the remote, push.
- **Agent, after the first green run:**
  - Record its URL.
  - On a throwaway branch, deliberately break one assertion, push it, and confirm the run fails. Record that URL.
  - Delete the branch.
- **Done when:** both URLs are recorded in the WORKLOG, ready for the evidence record.
- **Unblocks:** "CI runs on the current commit and passes." and "The red path was tested…"

**Commit Phase 7.**

---

### Phase 8 — Close-out

#### T29 · Reset the dev database — depends on D10

Once the Phase 4 migrations are proven on the current data, and the owner confirms, run `alembic downgrade base && alembic upgrade head` to clear the review probe data.

#### T30 · The falsification review, then closing M0 — **OWNER**

1. **Owner:** carry out MASTER-PLAN "The real review": about 30 minutes writing the first P1 endpoint and migration against the contracts, listing every point where a decision had to be invented. The agent does not do this on the owner's behalf.
2. **Agent:** fold each of those decisions into the relevant ADR, schema or contract.
3. **When every §A.1 box is ticked and the owner agrees to close M0**, follow MASTER-PLAN §5:
   - Write `docs/evidence/M0.md` as a frozen snapshot: the filled §A.1 checklist plus the System Prompt §9 record, with the commit SHA and the CI links.
   - Delete `docs/evidence/M0-baseline-evidence.md`.
   - Collapse §A.1 in `MASTER-PLAN.md` to three lines: closed status, date, link.
   - Delete this file.

---

## 4. Reporting

At the end of each phase, report to the owner (in Persian, per System Prompt §8):

- tasks completed, with their *Done when* results as `PASS / FAIL / NOT RUN / INCONCLUSIVE`;
- MASTER-PLAN boxes ticked, and the evidence for each;
- anything skipped or blocked, and why;
- open questions.
