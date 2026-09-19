import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import glob
import json
import os
import uuid
import jsonschema
import psycopg2
import pytest
import yaml
from fastapi.testclient import TestClient
from openapi_spec_validator import validate as validate_openapi
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from app.core.config import settings
from app.core.idempotency import handle_idempotency_race
from app.main import app
from sqlalchemy.exc import IntegrityError

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CONTRACTS_DIR = os.path.join(REPO_ROOT, "contracts")
OPENAPI_SPEC = os.path.join(CONTRACTS_DIR, "openapi", "v1.yaml")
EVENTS_DIR = os.path.join(CONTRACTS_DIR, "events")
ADR_0003 = os.path.join(REPO_ROOT, "docs", "adr", "ADR-0003-delivery-semantics-and-idempotent-execution.md")
TEST_DATABASE_URL_SYNC = f"postgresql://hamicloud:hamicloud_secret@localhost:5432/hamicloud_test"


# =============================================================================
# Helpers
# =============================================================================

def get_openapi_validator(schema_ref: str):
    """Build a validator for a schema in OpenAPI spec using referencing library."""
    with open(OPENAPI_SPEC, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    resource = DRAFT202012.create_resource(spec)
    registry = Registry().with_resource("urn:openapi", resource)
    return jsonschema.Draft202012Validator(
        {"$ref": f"urn:openapi{schema_ref}"},
        registry=registry,
    )


def create_test_workspace_and_app(client: TestClient, subject: str = "test-user"):
    headers = {"X-Dev-Subject": subject}
    ws_slug = f"ws-{uuid.uuid4().hex[:8]}"
    ws_resp = client.post("/v1/workspaces", json={"name": "Test WS", "slug": ws_slug}, headers=headers)
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    app_slug = f"app-{uuid.uuid4().hex[:8]}"
    app_resp = client.post(
        f"/v1/workspaces/{ws_id}/apps",
        json={"name": "Test App", "slug": app_slug, "workload_type": "HTTP_SERVICE"},
        headers=headers,
    )
    assert app_resp.status_code == 201
    app_id = app_resp.json()["id"]

    return ws_id, app_id, headers


# =============================================================================
# T9 Tests — Mandatory Idempotency-Key on Mutating Routes & Atomic Execution
# =============================================================================

def test_t9_mandatory_idempotency_key_on_all_5_mutating_routes(client: TestClient, clean_db):
    """Verify that missing Idempotency-Key on any of the 5 mutating routes returns 422 VALIDATION_ERROR."""
    ws_id, app_id, auth_headers = create_test_workspace_and_app(client)

    # First create a deployment to have a release
    deploy_key = f"idemp-dep-{uuid.uuid4().hex[:8]}"
    deploy_payload = {"image_digest": "registry.example.com/web@sha256:1111111111111111111111111111111111111111111111111111111111111111", "port": 8080}
    dep_res = client.post(f"/v1/apps/{app_id}/deployments", json=deploy_payload, headers={"Idempotency-Key": deploy_key, **auth_headers})
    assert dep_res.status_code == 202
    release_id = dep_res.json()["operation_id"]

    # First create a job
    job_key = f"idemp-job-{uuid.uuid4().hex[:8]}"
    job_payload = {"name": "test-job", "image_digest": "registry.example.com/batch@sha256:2222222222222222222222222222222222222222222222222222222222222222"}
    job_res = client.post(f"/v1/workspaces/{ws_id}/jobs", json=job_payload, headers={"Idempotency-Key": job_key, **auth_headers})
    assert job_res.status_code == 202
    job_id = job_res.json()["operation_id"]

    # Mark job as failed for rerun test
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'FAILED' WHERE id = %s", (job_id,))
    conn.commit()
    conn.close()

    # The 5 mutating routes without Idempotency-Key:
    mutating_calls = [
        ("POST", f"/v1/workspaces/{ws_id}/jobs", job_payload),
        ("POST", f"/v1/apps/{app_id}/deployments", deploy_payload),
        ("POST", f"/v1/apps/{app_id}/rollbacks", {"target_release_id": release_id}),
        ("POST", f"/v1/jobs/{job_id}/cancel", None),
        ("POST", f"/v1/jobs/{job_id}/reruns", None),
    ]

    for method, path, body in mutating_calls:
        resp = client.request(method, path, json=body, headers=auth_headers)
        assert resp.status_code == 422, f"Expected 422 for {path} without Idempotency-Key, got {resp.status_code}"
        data = resp.json()
        assert data["error_code"] == "VALIDATION_ERROR"
        assert "correlation_id" in data
        assert resp.headers.get("X-Correlation-ID") == data["correlation_id"]
        # Ensure details describe the missing header
        assert "Idempotency-Key" in str(data.get("details"))


def test_t9_idempotency_replay_and_conflict(client: TestClient, clean_db):
    """Verify identical retry returns original 202; different body returns 409 IDEMPOTENCY_CONFLICT."""
    ws_id, app_id, auth_headers = create_test_workspace_and_app(client)
    idemp_key = f"key-{uuid.uuid4().hex[:8]}"

    payload1 = {
        "image_digest": "registry.example.com/web@sha256:3333333333333333333333333333333333333333333333333333333333333333",
        "port": 8080,
    }
    # 1. Initial submission -> 202 Accepted
    resp1 = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=payload1,
        headers={"Idempotency-Key": idemp_key, **auth_headers},
    )
    assert resp1.status_code == 202
    op_id = resp1.json()["operation_id"]

    # 2. Replay with identical key and identical payload -> 202 with original operation_id
    resp2 = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=payload1,
        headers={"Idempotency-Key": idemp_key, **auth_headers},
    )
    assert resp2.status_code == 202
    assert resp2.json()["operation_id"] == op_id

    # 3. Replay with same key but different payload -> 409 Conflict with IDEMPOTENCY_CONFLICT
    payload_diff = dict(payload1, port=9000)
    resp3 = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=payload_diff,
        headers={"Idempotency-Key": idemp_key, **auth_headers},
    )
    assert resp3.status_code == 409
    data3 = resp3.json()
    assert data3["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert "Idempotency key reused with different" in data3["message"]


def test_t9_cancel_job_idempotent_no_duplicate_outbox_when_already_cancel_requested(client: TestClient, clean_db):
    """Verify that calling cancel on a job already in CANCEL_REQUESTED does not emit duplicate outbox event."""
    ws_id, _, auth_headers = create_test_workspace_and_app(client)

    # Submit job
    submit_key = f"job-sub-{uuid.uuid4().hex[:8]}"
    job_payload = {
        "name": "cancel-target",
        "image_digest": "registry.example.com/job@sha256:4444444444444444444444444444444444444444444444444444444444444444",
    }
    j_resp = client.post(f"/v1/workspaces/{ws_id}/jobs", json=job_payload, headers={"Idempotency-Key": submit_key, **auth_headers})
    assert j_resp.status_code == 202
    job_id = j_resp.json()["operation_id"]

    # First cancel call
    k1 = f"canc-1-{uuid.uuid4().hex[:8]}"
    c1_resp = client.post(f"/v1/jobs/{job_id}/cancel", headers={"Idempotency-Key": k1, **auth_headers})
    assert c1_resp.status_code == 202

    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM outbox_events WHERE topic = 'job.cancellation.requested.v1'")
        count_first = cur.fetchone()[0]
    conn.close()
    assert count_first == 1

    # Second cancel call (even with a new idempotency key) on already CANCEL_REQUESTED job
    k2 = f"canc-2-{uuid.uuid4().hex[:8]}"
    c2_resp = client.post(f"/v1/jobs/{job_id}/cancel", headers={"Idempotency-Key": k2, **auth_headers})
    assert c2_resp.status_code == 202
    assert c2_resp.json()["operation_id"] == job_id

    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM outbox_events WHERE topic = 'job.cancellation.requested.v1'")
        count_second = cur.fetchone()[0]
    conn.close()
    assert count_second == 1, "Duplicate outbox event was emitted for cancel_job!"


def test_t9_concurrent_identical_requests_atomic_single_operation(clean_db):
    """Concurrent identical requests: exactly 1 operation created, all return identical 202 response."""
    with TestClient(app) as client:
        ws_id, _, auth_headers = create_test_workspace_and_app(client)

    shared_key = f"concurrent-key-{uuid.uuid4().hex[:8]}"
    job_payload = {
        "name": "concurrent-job",
        "image_digest": "registry.example.com/job@sha256:5555555555555555555555555555555555555555555555555555555555555555",
    }

    def execute_request():
        with TestClient(app) as c:
            return c.post(
                f"/v1/workspaces/{ws_id}/jobs",
                json=job_payload,
                headers={"Idempotency-Key": shared_key, **auth_headers},
            )

    # Launch 10 concurrent identical requests
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(execute_request) for _ in range(10)]
        results = [f.result() for f in futures]

    # Verify all 10 return 202
    for r in results:
        assert r.status_code == 202, f"Concurrent request returned {r.status_code}: {r.text}"

    # Verify all 10 return identical operation_id
    op_ids = {r.json()["operation_id"] for r in results}
    assert len(op_ids) == 1, f"Expected exactly 1 distinct operation_id, got {op_ids}"
    single_op_id = list(op_ids)[0]

    # Verify database state: exactly 1 job, 1 idempotency record, 1 outbox event
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE id = %s", (single_op_id,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM idempotency_records WHERE idempotency_key = %s", (shared_key,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM outbox_events WHERE topic = 'job.submitted.v1'")
        assert cur.fetchone()[0] == 1
    conn.close()


# =============================================================================
# T10 Tests — Idempotency Record Expiration (24h Retention & ADR-0003)
# =============================================================================

def test_t10_idempotency_record_expiration_after_24_hours(client: TestClient, clean_db):
    """Verify an idempotency record older than 24 hours is treated as non-existent and a new operation is admitted."""
    ws_id, _, auth_headers = create_test_workspace_and_app(client)
    expired_key = f"exp-key-{uuid.uuid4().hex[:8]}"
    endpoint = f"/v1/workspaces/{ws_id}/jobs"
    old_op_id = str(uuid.uuid4())

    job_payload = {
        "name": "expired-job",
        "image_digest": "registry.example.com/job@sha256:6666666666666666666666666666666666666666666666666666666666666666",
    }

    # Manually insert an expired idempotency record (25 hours ago)
    past_time = datetime.now(timezone.utc) - timedelta(hours=25)
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO idempotency_records (
                id, workspace_id, endpoint, idempotency_key, request_hash,
                response_code, response_body, expires_at, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            str(uuid.uuid4()), ws_id, endpoint, expired_key, "dummy-hash",
            202, json.dumps({"operation_id": old_op_id, "status": "ACCEPTED", "status_url": f"/v1/operations/{old_op_id}"}),
            past_time, past_time, past_time,
        ))
    conn.commit()
    conn.close()

    # Submit request with the expired key
    resp = client.post(endpoint, json=job_payload, headers={"Idempotency-Key": expired_key, **auth_headers})
    assert resp.status_code == 202
    new_op_id = resp.json()["operation_id"]

    # Verify a new operation was created, not the cached expired one
    assert new_op_id != old_op_id


def test_t10_adr0003_documents_retention_and_sweeper():
    """Verify ADR-0003 documents the 24-hour retention period and background sweeper task ownership in M1."""
    assert os.path.exists(ADR_0003), f"Missing ADR-0003 at {ADR_0003}"
    with open(ADR_0003, "r", encoding="utf-8") as f:
        content = f.read()
    assert "24 hours" in content, "ADR-0003 missing explicit 24-hour retention contract documentation!"
    assert "sweeper" in content.lower(), "ADR-0003 missing background sweeper documentation!"
    assert "M1" in content, "ADR-0003 missing M1 sweeper ownership documentation!"


# =============================================================================
# T11 Tests — Release Number Allocation & Pessimistic Lock Integrity
# =============================================================================

def test_t11_release_number_allocation_max_plus_one_after_deletion(client: TestClient, clean_db):
    """Verify release number is allocated as MAX(release_number) + 1, even if a middle release is deleted."""
    ws_id, app_id, auth_headers = create_test_workspace_and_app(client)

    # Deploy 3 releases
    for i in range(1, 4):
        deploy_payload = {
            "image_digest": f"registry.example.com/app@sha256:{str(i)*64}",
            "port": 8080 + i,
        }
        res = client.post(
            f"/v1/apps/{app_id}/deployments",
            json=deploy_payload,
            headers={"Idempotency-Key": f"k-dep-{i}-{uuid.uuid4().hex[:6]}", **auth_headers},
        )
        assert res.status_code == 202

    # Check DB release numbers: [1, 2, 3]
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT release_number FROM releases WHERE application_id = %s ORDER BY release_number", (app_id,))
        numbers = [row[0] for row in cur.fetchall()]
        assert numbers == [1, 2, 3]

        # Delete middle release (release_number = 2)
        cur.execute("DELETE FROM releases WHERE application_id = %s AND release_number = 2", (app_id,))
    conn.commit()
    conn.close()

    # Deploy 4th release: must allocate MAX(1, 3) + 1 = 4 (NOT 2 + 1 = 3, which would collide)
    deploy_payload_4 = {
        "image_digest": "registry.example.com/app@sha256:7777777777777777777777777777777777777777777777777777777777777777",
        "port": 8084,
    }
    res4 = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=deploy_payload_4,
        headers={"Idempotency-Key": f"k-dep-4-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert res4.status_code == 202

    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT release_number FROM releases WHERE application_id = %s ORDER BY release_number", (app_id,))
        numbers = [row[0] for row in cur.fetchall()]
    conn.close()
    assert numbers == [1, 3, 4], f"Expected release numbers [1, 3, 4], got {numbers}"


@pytest.mark.asyncio
async def test_t11_non_idempotency_integrity_error_raises_500():
    """Verify handle_idempotency_race re-raises non-idempotency IntegrityErrors as HTTP 500."""
    class FakeCause:
        constraint_name = "fk_other_table"

    class FakeOrig:
        __cause__ = FakeCause()
        constraint_name = "fk_other_table"

    fake_exc = IntegrityError("statement", {}, FakeOrig())

    class FakeSession:
        async def rollback(self):
            pass

    with pytest.raises(Exception) as exc_info:
        await handle_idempotency_race(
            FakeSession(), fake_exc, uuid.uuid4(), "/test", "key", "hash"
        )
    assert exc_info.value.status_code == 500


# =============================================================================
# T12 Tests — Unified Error Responses & Cursor Pagination (D3)
# =============================================================================

def test_t12_validation_error_envelope_and_correlation_id(client: TestClient):
    """Submitting invalid body returns 422 with ErrorResponse envelope and error_code VALIDATION_ERROR."""
    resp = client.post("/v1/workspaces", json={"name": "Bad Slug WS", "slug": "UPPERCASE_AND_UNDERSCORE!"}, headers={"X-Dev-Subject": "user"})
    assert resp.status_code == 422
    data = resp.json()
    assert data["error_code"] == "VALIDATION_ERROR"
    assert "Request validation failed" in data["message"]
    assert "correlation_id" in data
    assert resp.headers.get("X-Correlation-ID") == data["correlation_id"]
    assert "errors" in data["details"]


def test_t12_error_envelope_on_all_status_codes(client: TestClient, clean_db):
    """Verify uniform error envelope across 400, 401, 403, 404, 409, 422, 501."""
    ws_id, app_id, auth_headers = create_test_workspace_and_app(client)

    # 400: Malformed cursor on list endpoint
    r400 = client.get(f"/v1/workspaces/{ws_id}/jobs?cursor=not-valid-base64-garbage!", headers=auth_headers)
    assert r400.status_code == 400
    assert r400.json()["error_code"] == "BAD_REQUEST"

    # 401: No auth on tenant route
    r401 = client.get(f"/v1/jobs/{uuid.uuid4()}")
    assert r401.status_code == 401
    assert r401.json()["error_code"] == "UNAUTHORIZED"

    # 403: Viewer role mutation
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO workspace_memberships (id, workspace_id, user_subject, role, created_at, updated_at) VALUES (%s, %s, %s, %s, NOW(), NOW())", (str(uuid.uuid4()), ws_id, "charlie-viewer", "VIEWER"))
    conn.commit()
    conn.close()

    r403 = client.post(f"/v1/workspaces/{ws_id}/apps", json={"name": "App", "slug": f"app-{uuid.uuid4().hex[:6]}", "workload_type": "HTTP_SERVICE"}, headers={"X-Dev-Subject": "charlie-viewer"})
    assert r403.status_code == 403
    assert r403.json()["error_code"] == "FORBIDDEN"

    # 404: Not found
    r404 = client.get(f"/v1/jobs/{uuid.uuid4()}", headers=auth_headers)
    assert r404.status_code == 404
    assert r404.json()["error_code"] == "NOT_FOUND"

    # 409: Duplicate slug
    ws_data = client.get(f"/v1/workspaces/{ws_id}", headers=auth_headers)
    # create another workspace with same slug
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT slug FROM workspaces WHERE id = %s", (ws_id,))
        slug = cur.fetchone()[0]
    conn.close()
    r409 = client.post("/v1/workspaces", json={"name": "Dup", "slug": slug}, headers=auth_headers)
    assert r409.status_code == 409
    assert r409.json()["error_code"] == "CONFLICT"

    # 422: Validation error
    r422 = client.post(f"/v1/workspaces/{ws_id}/jobs", json={}, headers={"Idempotency-Key": "k", **auth_headers})
    assert r422.status_code == 422
    assert r422.json()["error_code"] == "VALIDATION_ERROR"

    # 501: Not implemented (authorized operation)
    j_501 = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={"name": "job-501", "image_digest": "registry.example.com/job@sha256:1111111111111111111111111111111111111111111111111111111111111111"},
        headers={"Idempotency-Key": f"k-501-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert j_501.status_code == 202
    op_501_id = j_501.json()["operation_id"]
    r501 = client.get(f"/v1/operations/{op_501_id}/events", headers=auth_headers)
    assert r501.status_code == 501
    assert r501.json()["error_code"] == "NOT_IMPLEMENTED"

    # Verify all responses have matching X-Correlation-ID header
    for resp in (r400, r401, r403, r404, r409, r422, r501):
        assert resp.headers.get("X-Correlation-ID") == resp.json()["correlation_id"]


def test_t12_cursor_pagination_workspace_jobs(client: TestClient, clean_db):
    """Verify cursor pagination on GET /v1/workspaces/{ws}/jobs walks at least 2 pages, caps limit at 100."""
    ws_id, _, auth_headers = create_test_workspace_and_app(client)

    # Seed 5 jobs
    created_job_ids = []
    for i in range(5):
        j_resp = client.post(
            f"/v1/workspaces/{ws_id}/jobs",
            json={"name": f"job-{i}", "image_digest": f"registry.example.com/job@sha256:{str(i)*64}"},
            headers={"Idempotency-Key": f"k-seed-{i}-{uuid.uuid4().hex[:6]}", **auth_headers},
        )
        assert j_resp.status_code == 202
        created_job_ids.append(j_resp.json()["operation_id"])

    # Page 1: limit 2
    p1 = client.get(f"/v1/workspaces/{ws_id}/jobs?limit=2", headers=auth_headers)
    assert p1.status_code == 200
    p1_data = p1.json()
    assert len(p1_data["items"]) == 2
    assert p1_data["next_cursor"] is not None

    # Page 2: limit 2 with cursor
    p2 = client.get(f"/v1/workspaces/{ws_id}/jobs?limit=2&cursor={p1_data['next_cursor']}", headers=auth_headers)
    assert p2.status_code == 200
    p2_data = p2.json()
    assert len(p2_data["items"]) == 2
    assert p2_data["next_cursor"] is not None

    # Page 3: final page with 1 item
    p3 = client.get(f"/v1/workspaces/{ws_id}/jobs?limit=2&cursor={p2_data['next_cursor']}", headers=auth_headers)
    assert p3.status_code == 200
    p3_data = p3.json()
    assert len(p3_data["items"]) == 1
    assert p3_data["next_cursor"] is None

    # Verify all 5 jobs were traversed without duplicates
    traversed_ids = [j["id"] for j in p1_data["items"] + p2_data["items"] + p3_data["items"]]
    assert len(traversed_ids) == 5
    assert set(traversed_ids) == set(created_job_ids)

    # Test limit capped at 100 (101 returns 422)
    p_invalid_limit = client.get(f"/v1/workspaces/{ws_id}/jobs?limit=101", headers=auth_headers)
    assert p_invalid_limit.status_code == 422
    assert p_invalid_limit.json()["error_code"] == "VALIDATION_ERROR"

    # Test invalid cursor returns 400
    p_invalid_cur = client.get(f"/v1/workspaces/{ws_id}/jobs?cursor=bad-cursor!", headers=auth_headers)
    assert p_invalid_cur.status_code == 400
    assert p_invalid_cur.json()["error_code"] == "BAD_REQUEST"


def test_t12_cursor_pagination_app_releases(client: TestClient, clean_db):
    """Verify cursor pagination on GET /v1/apps/{app}/releases walks at least 2 pages, caps limit at 100."""
    ws_id, app_id, auth_headers = create_test_workspace_and_app(client)

    # Seed 5 releases
    created_rel_ids = []
    for i in range(5):
        r_resp = client.post(
            f"/v1/apps/{app_id}/deployments",
            json={"image_digest": f"registry.example.com/app@sha256:{str(i)*64}", "port": 8080 + i},
            headers={"Idempotency-Key": f"k-rel-{i}-{uuid.uuid4().hex[:6]}", **auth_headers},
        )
        assert r_resp.status_code == 202
        created_rel_ids.append(r_resp.json()["operation_id"])

    # Page 1: limit 2
    p1 = client.get(f"/v1/apps/{app_id}/releases?limit=2", headers=auth_headers)
    assert p1.status_code == 200
    p1_data = p1.json()
    assert len(p1_data["items"]) == 2
    assert p1_data["next_cursor"] is not None

    # Page 2: limit 2 with cursor
    p2 = client.get(f"/v1/apps/{app_id}/releases?limit=2&cursor={p1_data['next_cursor']}", headers=auth_headers)
    assert p2.status_code == 200
    p2_data = p2.json()
    assert len(p2_data["items"]) == 2
    assert p2_data["next_cursor"] is not None

    # Page 3: final page with 1 item
    p3 = client.get(f"/v1/apps/{app_id}/releases?limit=2&cursor={p2_data['next_cursor']}", headers=auth_headers)
    assert p3.status_code == 200
    p3_data = p3.json()
    assert len(p3_data["items"]) == 1
    assert p3_data["next_cursor"] is None

    # Verify all 5 releases were traversed without duplicates
    traversed_ids = [r["id"] for r in p1_data["items"] + p2_data["items"] + p3_data["items"]]
    assert len(traversed_ids) == 5
    assert set(traversed_ids) == set(created_rel_ids)

    # Test limit capped at 100 (101 returns 422)
    p_invalid_limit = client.get(f"/v1/apps/{app_id}/releases?limit=101", headers=auth_headers)
    assert p_invalid_limit.status_code == 422
    assert p_invalid_limit.json()["error_code"] == "VALIDATION_ERROR"

    # Test invalid cursor returns 400
    p_invalid_cur = client.get(f"/v1/apps/{app_id}/releases?cursor=bad-cursor!", headers=auth_headers)
    assert p_invalid_cur.status_code == 400
    assert p_invalid_cur.json()["error_code"] == "BAD_REQUEST"


# =============================================================================
# T13 Tests — OpenAPI Spec Completeness, Examples, and Headers
# =============================================================================

def test_t13_openapi_spec_validator_passes():
    """Verify openapi-spec-validator validates contracts/openapi/v1.yaml."""
    with open(OPENAPI_SPEC, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    validate_openapi(spec)


def test_t13_all_schema_examples_validate_against_their_schemas():
    """Verify that every schema example validates against its own schema definition."""
    with open(OPENAPI_SPEC, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    schemas = spec.get("components", {}).get("schemas", {})
    resource = DRAFT202012.create_resource(spec)
    registry = Registry().with_resource("urn:openapi", resource)

    for schema_name, schema_def in schemas.items():
        validator = jsonschema.Draft202012Validator(
            {"$ref": f"urn:openapi#/components/schemas/{schema_name}"},
            registry=registry,
        )
        # Check top-level example
        if "example" in schema_def:
            validator.validate(schema_def["example"])


def test_t13_every_endpoint_has_request_and_2xx_response_examples():
    """Verify every endpoint has at least one example for request (if body exists) and 2xx response."""
    with open(OPENAPI_SPEC, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    paths = spec.get("paths", {})
    for path, path_item in paths.items():
        for method in ("get", "post", "put", "delete", "patch"):
            op = path_item.get(method)
            if not op:
                continue
            op_id = op.get("operationId", f"{method.upper()} {path}")

            # Request body example
            if "requestBody" in op:
                content = op["requestBody"].get("content", {})
                json_content = content.get("application/json", {})
                assert "example" in json_content or "examples" in json_content, (
                    f"Operation '{op_id}' ({method.upper()} {path}) missing example in requestBody content"
                )

            # 2xx response example
            responses = op.get("responses", {})
            success_status = next((k for k in responses if k.startswith("2")), None)
            if success_status and success_status != "204":
                resp_obj = responses[success_status]
                content = resp_obj.get("content", {})
                if "application/json" in content:
                    json_content = content["application/json"]
                    assert "example" in json_content or "examples" in json_content, (
                        f"Operation '{op_id}' ({method.upper()} {path}) missing example in {success_status} response content"
                    )


def test_t13_all_operations_document_correlation_id_and_idempotency_retention():
    """Verify X-Correlation-ID is documented on all operations and Idempotency-Key retention is documented."""
    with open(OPENAPI_SPEC, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    paths = spec.get("paths", {})
    mutating_routes = {
        "/workspaces/{workspace_id}/jobs": "post",
        "/apps/{app_id}/deployments": "post",
        "/apps/{app_id}/rollbacks": "post",
        "/jobs/{job_id}/cancel": "post",
        "/jobs/{job_id}/reruns": "post",
    }

    for path, path_item in paths.items():
        for method in ("get", "post", "put", "delete", "patch"):
            op = path_item.get(method)
            if not op:
                continue
            op_id = op.get("operationId", f"{method.upper()} {path}")

            # Verify 2xx response documents X-Correlation-ID
            success_status = next((k for k in op.get("responses", {}) if k.startswith("2")), None)
            if success_status:
                resp_headers = op["responses"][success_status].get("headers", {})
                assert "X-Correlation-ID" in resp_headers, (
                    f"Operation '{op_id}' ({method.upper()} {path}) missing X-Correlation-ID in {success_status} response"
                )

    # Verify mutating operations require Idempotency-Key and document retention
    for path, method in mutating_routes.items():
        op = paths[path][method]
        op_id = op.get("operationId")
        headers = [p for p in op.get("parameters", []) if p.get("in") == "header" and p.get("name") == "Idempotency-Key"]
        assert len(headers) == 1, f"Mutating operation '{op_id}' missing Idempotency-Key parameter!"
        idemp_param = headers[0]
        assert idemp_param.get("required") is True, f"Idempotency-Key must be required on '{op_id}'!"
        desc = idemp_param.get("description", "")
        assert "24 hours" in desc, f"Idempotency-Key description on '{op_id}' must document 24 hours retention contract!"


# =============================================================================
# T14 Tests — Outbox Event Schema Validation
# =============================================================================

def test_t14_outbox_payloads_validate_against_event_schemas(client: TestClient, clean_db):
    """Verify that all outbox payloads inserted by mutating endpoints validate against JSON schemas in contracts/events/."""
    ws_id, app_id, auth_headers = create_test_workspace_and_app(client)

    # 1. Trigger job.submitted.v1
    j_res = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={"name": "contract-job", "image_digest": "registry.example.com/img@sha256:8888888888888888888888888888888888888888888888888888888888888888"},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert j_res.status_code == 202
    job_id = j_res.json()["operation_id"]

    # 2. Trigger app.deployment.requested.v1
    dep_res = client.post(
        f"/v1/apps/{app_id}/deployments",
        json={"image_digest": "registry.example.com/app@sha256:9999999999999999999999999999999999999999999999999999999999999999", "port": 8080},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert dep_res.status_code == 202
    release_id = dep_res.json()["operation_id"]

    # 3. Trigger app.deployment.requested.v1 with rollback
    # Deploy a second release so we can rollback to the first
    client.post(
        f"/v1/apps/{app_id}/deployments",
        json={"image_digest": "registry.example.com/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "port": 8081},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    rb_res = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json={"target_release_id": release_id},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert rb_res.status_code == 202

    # 4. Trigger job.cancellation.requested.v1
    c_res = client.post(f"/v1/jobs/{job_id}/cancel", headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **auth_headers})
    assert c_res.status_code == 202

    # Query outbox events from DB
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT topic, payload_json FROM outbox_events")
        rows = cur.fetchall()
    conn.close()

    assert len(rows) >= 4

    for topic, payload in rows:
        schema_file = os.path.join(EVENTS_DIR, f"{topic}.json")
        assert os.path.exists(schema_file), f"Missing schema for topic {topic}"
        with open(schema_file, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validator = jsonschema.Draft202012Validator(schema)
        validator.validate(payload)


# =============================================================================
# T15 Tests — Readiness Probe Connection Pool and Behavior
# =============================================================================

def test_t15_readyz_healthy_and_unhealthy_and_lifespan_redis(client: TestClient, monkeypatch):
    """Verify /readyz returns 200 when DB and Redis are up, 503 when either is down, and reuses lifespan client pool."""
    # 1. Healthy state -> 200
    res = client.get("/readyz")
    assert res.status_code == 200
    data = res.json()
    assert data["ready"] is True
    assert data["database"] is True
    assert data["redis"] is True

    # Verify Redis client is stored on app.state.redis from lifespan
    assert hasattr(app.state, "redis")
    assert app.state.redis is not None

    # 2. Redis unreachable -> 503
    async def broken_ping():
        raise ConnectionError("Redis down")

    monkeypatch.setattr(app.state.redis, "ping", broken_ping)
    res_down = client.get("/readyz")
    assert res_down.status_code == 503
    down_data = res_down.json()
    assert down_data["error_code"] == "SERVICE_UNAVAILABLE"
    assert down_data["details"]["redis"] is False
