import asyncio
import base64
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import uuid
import httpx
import jsonschema
import psycopg2
import pytest
import yaml
from fastapi.testclient import TestClient
from openapi_spec_validator import validate as validate_openapi
from referencing import Registry
from referencing.jsonschema import DRAFT202012
from sqlalchemy.exc import IntegrityError

from app.core.db_errors import violated_constraint
from app.main import app

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CONTRACTS_DIR = os.path.join(REPO_ROOT, "contracts")
OPENAPI_SPEC = os.path.join(CONTRACTS_DIR, "openapi", "v1.yaml")
EVENTS_DIR = os.path.join(CONTRACTS_DIR, "events")
ADR_0003 = os.path.join(REPO_ROOT, "docs", "adr", "ADR-0003-delivery-semantics-and-idempotent-execution.md")
TEST_DATABASE_URL_SYNC = "postgresql://hamicloud:hamicloud_secret@localhost:5432/hamicloud_test"


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
# T9 Tests — Mandatory Idempotency-Key, Async Concurrency, Replays & Conflicts
# =============================================================================

def test_t9_mandatory_idempotency_key_on_all_5_mutating_routes(client: TestClient, clean_db):
    """Verify missing Idempotency-Key on any of the 5 mutating routes returns 422 VALIDATION_ERROR."""
    ws_id, app_id, auth_headers = create_test_workspace_and_app(client)

    # Initial deployment
    deploy_key = f"idemp-dep-{uuid.uuid4().hex[:8]}"
    deploy_payload = {
        "image_digest": "registry.example.com/web@sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "port": 8080,
    }
    dep_res = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=deploy_payload,
        headers={"Idempotency-Key": deploy_key, **auth_headers},
    )
    assert dep_res.status_code == 202
    release_id = dep_res.json()["operation_id"]

    # Initial job
    job_key = f"idemp-job-{uuid.uuid4().hex[:8]}"
    job_payload = {
        "name": "test-job",
        "image_digest": "registry.example.com/batch@sha256:2222222222222222222222222222222222222222222222222222222222222222",
    }
    job_res = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json=job_payload,
        headers={"Idempotency-Key": job_key, **auth_headers},
    )
    assert job_res.status_code == 202
    job_id = job_res.json()["operation_id"]

    # Mark job as failed for rerun test
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'FAILED' WHERE id = %s", (job_id,))
    conn.commit()
    conn.close()

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
        assert "idempotency-key" in str(data.get("details")).lower()


def test_t9_idempotency_key_length_validation(client: TestClient, clean_db):
    """Verify Idempotency-Key min_length=1 and max_length=255 validation returns 422."""
    ws_id, _, auth_headers = create_test_workspace_and_app(client)
    job_payload = {
        "name": "len-test-job",
        "image_digest": "registry.example.com/batch@sha256:1111111111111111111111111111111111111111111111111111111111111111",
    }

    # 1. Key length 256 (> 255) -> 422
    too_long_key = "k" * 256
    r_long = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json=job_payload,
        headers={"Idempotency-Key": too_long_key, **auth_headers},
    )
    assert r_long.status_code == 422
    assert r_long.json()["error_code"] == "VALIDATION_ERROR"

    # 2. Empty key ("") -> 422
    r_empty = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json=job_payload,
        headers={"Idempotency-Key": "", **auth_headers},
    )
    assert r_empty.status_code == 422
    assert r_empty.json()["error_code"] == "VALIDATION_ERROR"

    # 3. Valid key length 255 -> 202
    valid_key = "k" * 255
    r_valid = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json=job_payload,
        headers={"Idempotency-Key": valid_key, **auth_headers},
    )
    assert r_valid.status_code == 202


def test_t9_idempotency_replay_and_conflict_all_5_endpoints(client: TestClient, clean_db):
    """Verify replay -> 202 identical body & no extra outbox event; diff body -> 409 on all endpoints."""
    ws_id, app_id, auth_headers = create_test_workspace_and_app(client)

    # -------------------------------------------------------------------------
    # 1. submit_job
    # -------------------------------------------------------------------------
    submit_key = f"key-submit-{uuid.uuid4().hex[:8]}"
    job_payload = {
        "name": "replay-job",
        "image_digest": "registry.example.com/job@sha256:1111111111111111111111111111111111111111111111111111111111111111",
    }
    r1 = client.post(f"/v1/workspaces/{ws_id}/jobs", json=job_payload, headers={"Idempotency-Key": submit_key, **auth_headers})
    assert r1.status_code == 202
    job_id = r1.json()["operation_id"]

    # Replay identical -> 202 identical response
    r1_replay = client.post(f"/v1/workspaces/{ws_id}/jobs", json=job_payload, headers={"Idempotency-Key": submit_key, **auth_headers})
    assert r1_replay.status_code == 202
    assert r1_replay.json() == r1.json()

    # Replay diff body -> 409
    r1_diff = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json=dict(job_payload, name="different-name"),
        headers={"Idempotency-Key": submit_key, **auth_headers},
    )
    assert r1_diff.status_code == 409
    assert r1_diff.json()["error_code"] == "IDEMPOTENCY_CONFLICT"

    # Outbox count for job.submitted.v1 must be exactly 1
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM outbox_events WHERE topic = 'job.submitted.v1' AND payload_json->>'job_id' = %s", (job_id,))
        assert cur.fetchone()[0] == 1
    conn.close()

    # -------------------------------------------------------------------------
    # 2. deploy_release
    # -------------------------------------------------------------------------
    deploy_key = f"key-deploy-{uuid.uuid4().hex[:8]}"
    deploy_payload = {
        "image_digest": "registry.example.com/app@sha256:2222222222222222222222222222222222222222222222222222222222222222",
        "port": 8080,
    }
    r2 = client.post(f"/v1/apps/{app_id}/deployments", json=deploy_payload, headers={"Idempotency-Key": deploy_key, **auth_headers})
    assert r2.status_code == 202
    release_1_id = r2.json()["operation_id"]

    # Replay identical -> 202
    r2_replay = client.post(f"/v1/apps/{app_id}/deployments", json=deploy_payload, headers={"Idempotency-Key": deploy_key, **auth_headers})
    assert r2_replay.status_code == 202
    assert r2_replay.json() == r2.json()

    # Replay diff body -> 409
    r2_diff = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=dict(deploy_payload, port=8081),
        headers={"Idempotency-Key": deploy_key, **auth_headers},
    )
    assert r2_diff.status_code == 409
    assert r2_diff.json()["error_code"] == "IDEMPOTENCY_CONFLICT"

    # Outbox count for release_1_id must be exactly 1
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM outbox_events WHERE topic = 'app.deployment.requested.v1' AND payload_json->>'release_id' = %s", (release_1_id,))
        assert cur.fetchone()[0] == 1
    conn.close()

    # Deploy release 2 for rollback test
    r2_second = client.post(
        f"/v1/apps/{app_id}/deployments",
        json={"image_digest": "registry.example.com/app@sha256:3333333333333333333333333333333333333333333333333333333333333333", "port": 8082},
        headers={"Idempotency-Key": f"k-dep2-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert r2_second.status_code == 202
    release_2_id = r2_second.json()["operation_id"]

    # -------------------------------------------------------------------------
    # 3. rollback_release
    # -------------------------------------------------------------------------
    rb_key = f"key-rb-{uuid.uuid4().hex[:8]}"
    rb_payload = {"target_release_id": release_1_id}
    r3 = client.post(f"/v1/apps/{app_id}/rollbacks", json=rb_payload, headers={"Idempotency-Key": rb_key, **auth_headers})
    assert r3.status_code == 202
    rb_op_id = r3.json()["operation_id"]

    # Replay identical -> 202
    r3_replay = client.post(f"/v1/apps/{app_id}/rollbacks", json=rb_payload, headers={"Idempotency-Key": rb_key, **auth_headers})
    assert r3_replay.status_code == 202
    assert r3_replay.json() == r3.json()

    # Replay diff body -> 409
    r3_diff = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json={"target_release_id": release_2_id},
        headers={"Idempotency-Key": rb_key, **auth_headers},
    )
    assert r3_diff.status_code == 409
    assert r3_diff.json()["error_code"] == "IDEMPOTENCY_CONFLICT"

    # Outbox count for rollback release must be exactly 1
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM outbox_events WHERE topic = 'app.deployment.requested.v1' AND payload_json->>'release_id' = %s", (rb_op_id,))
        assert cur.fetchone()[0] == 1
    conn.close()

    # -------------------------------------------------------------------------
    # 4. cancel_job
    # -------------------------------------------------------------------------
    cancel_key = f"key-cancel-{uuid.uuid4().hex[:8]}"
    r4 = client.post(f"/v1/jobs/{job_id}/cancel", headers={"Idempotency-Key": cancel_key, **auth_headers})
    assert r4.status_code == 202
    assert r4.json()["operation_id"] == job_id

    # Replay identical -> 202
    r4_replay = client.post(f"/v1/jobs/{job_id}/cancel", headers={"Idempotency-Key": cancel_key, **auth_headers})
    assert r4_replay.status_code == 202
    assert r4_replay.json() == r4.json()

    # Outbox count for cancel must be exactly 1
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM outbox_events WHERE topic = 'job.cancellation.requested.v1' AND payload_json->>'job_id' = %s", (job_id,))
        assert cur.fetchone()[0] == 1
    conn.close()

    # -------------------------------------------------------------------------
    # 5. rerun_job
    # -------------------------------------------------------------------------
    # Mark job as FAILED so rerun is allowed
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'FAILED' WHERE id = %s", (job_id,))
    conn.commit()
    conn.close()

    rerun_key = f"key-rerun-{uuid.uuid4().hex[:8]}"
    r5 = client.post(f"/v1/jobs/{job_id}/reruns", headers={"Idempotency-Key": rerun_key, **auth_headers})
    assert r5.status_code == 202
    rerun_job_id = r5.json()["operation_id"]

    # Replay identical -> 202
    r5_replay = client.post(f"/v1/jobs/{job_id}/reruns", headers={"Idempotency-Key": rerun_key, **auth_headers})
    assert r5_replay.status_code == 202
    assert r5_replay.json() == r5.json()

    # Outbox count for rerun must be exactly 1
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM outbox_events WHERE topic = 'job.submitted.v1' AND payload_json->>'job_id' = %s", (rerun_job_id,))
        assert cur.fetchone()[0] == 1
    conn.close()


def test_t9_cancel_job_idempotent_no_duplicate_outbox_when_already_cancel_requested(client: TestClient, clean_db):
    """Verify calling cancel on job already in CANCEL_REQUESTED does not emit duplicate outbox event."""
    ws_id, _, auth_headers = create_test_workspace_and_app(client)

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

    # Second cancel call with new key on already CANCEL_REQUESTED job
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


@pytest.mark.asyncio
async def test_t9_concurrent_identical_requests_atomic_single_operation(clean_db):
    """Concurrent identical requests on single event loop: exactly 1 op created, all return identical 202."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        auth_headers = {"X-Dev-Subject": "test-user"}
        ws_resp = await async_client.post("/v1/workspaces", json={"name": "WS", "slug": f"ws-{uuid.uuid4().hex[:8]}"}, headers=auth_headers)
        assert ws_resp.status_code == 201
        ws_id = ws_resp.json()["id"]

        shared_key = f"concurrent-key-{uuid.uuid4().hex[:8]}"
        job_payload = {
            "name": "concurrent-job",
            "image_digest": "registry.example.com/job@sha256:5555555555555555555555555555555555555555555555555555555555555555",
        }

        tasks = [
            async_client.post(
                f"/v1/workspaces/{ws_id}/jobs",
                json=job_payload,
                headers={"Idempotency-Key": shared_key, **auth_headers},
            )
            for _ in range(10)
        ]
        results = await asyncio.gather(*tasks)

    for r in results:
        assert r.status_code == 202, f"Concurrent request returned {r.status_code}: {r.text}"

    op_ids = {r.json()["operation_id"] for r in results}
    assert len(op_ids) == 1, f"Expected exactly 1 distinct operation_id, got {op_ids}"
    single_op_id = list(op_ids)[0]

    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE id = %s", (single_op_id,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM idempotency_records WHERE idempotency_key = %s", (shared_key,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM outbox_events WHERE topic = 'job.submitted.v1'")
        assert cur.fetchone()[0] == 1
    conn.close()


@pytest.mark.asyncio
async def test_t9_concurrent_deploys_increment_generation_and_emit_events(clean_db):
    """8 concurrent deploys with distinct keys assert desired_generation reaches 9 and emits 8 events."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        auth_headers = {"X-Dev-Subject": "test-deployer"}
        ws_res = await async_client.post("/v1/workspaces", json={"name": "Dep WS", "slug": f"ws-{uuid.uuid4().hex[:8]}"}, headers=auth_headers)
        assert ws_res.status_code == 201
        ws_id = ws_res.json()["id"]

        app_res = await async_client.post(
            f"/v1/workspaces/{ws_id}/apps",
            json={"name": "Dep App", "slug": f"app-{uuid.uuid4().hex[:8]}", "workload_type": "HTTP_SERVICE"},
            headers=auth_headers,
        )
        assert app_res.status_code == 201
        app_id = app_res.json()["id"]

        tasks = [
            async_client.post(
                f"/v1/apps/{app_id}/deployments",
                json={
                    "image_digest": f"registry.example.com/app@sha256:{str(i)*64}",
                    "port": 8000 + i,
                },
                headers={"Idempotency-Key": f"dep-concurrent-{i}-{uuid.uuid4().hex[:6]}", **auth_headers},
            )
            for i in range(1, 9)
        ]
        results = await asyncio.gather(*tasks)

    for r in results:
        assert r.status_code == 202, f"Deploy returned {r.status_code}: {r.text}"

    # Verify desired_generation reaches 9 (1 initial + 8 = 9)
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT desired_generation FROM applications WHERE id = %s", (app_id,))
        desired_gen = cur.fetchone()[0]
        assert desired_gen == 9, f"Expected desired_generation 9, got {desired_gen}"

        # Verify exactly 8 outbox events were emitted with generations 2..9
        cur.execute("""
            SELECT (payload_json->>'generation')::int
            FROM outbox_events
            WHERE topic = 'app.deployment.requested.v1'
            ORDER BY id ASC
        """)
        emitted_gens = [row[0] for row in cur.fetchall()]
        assert len(emitted_gens) == 8
        assert sorted(emitted_gens) == list(range(2, 10)), f"Expected generations 2..9, got {emitted_gens}"
    conn.close()


@pytest.mark.asyncio
async def test_t9_concurrent_cancels_emit_single_event(clean_db):
    """8 concurrent cancels assert exactly 1 cancellation outbox event."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        auth_headers = {"X-Dev-Subject": "test-canceler"}
        ws_res = await async_client.post("/v1/workspaces", json={"name": "Cancel WS", "slug": f"ws-{uuid.uuid4().hex[:8]}"}, headers=auth_headers)
        assert ws_res.status_code == 201
        ws_id = ws_res.json()["id"]

        j_res = await async_client.post(
            f"/v1/workspaces/{ws_id}/jobs",
            json={"name": "concurrent-cancel-job", "image_digest": "registry.example.com/job@sha256:6666666666666666666666666666666666666666666666666666666666666666"},
            headers={"Idempotency-Key": f"k-sub-{uuid.uuid4().hex[:6]}", **auth_headers},
        )
        assert j_res.status_code == 202
        job_id = j_res.json()["operation_id"]

        tasks = [
            async_client.post(
                f"/v1/jobs/{job_id}/cancel",
                headers={"Idempotency-Key": f"cancel-concurrent-{i}-{uuid.uuid4().hex[:6]}", **auth_headers},
            )
            for i in range(8)
        ]
        results = await asyncio.gather(*tasks)

    for r in results:
        assert r.status_code == 202

    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM outbox_events WHERE topic = 'job.cancellation.requested.v1'")
        cancel_events = cur.fetchone()[0]
        assert cancel_events == 1, f"Expected exactly 1 cancel event, got {cancel_events}"
    conn.close()


# =============================================================================
# T10 Tests — Idempotency Record Expiration (24h Retention & ADR-0003)
# =============================================================================

def test_t10_idempotency_record_retention_exact_sentence_contract():
    """Verify OpenAPI spec declares exact retention sentence and length limits on all 5 mutating routes."""
    with open(OPENAPI_SPEC, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    mutating_routes = [
        "/v1/workspaces/{workspace_id}/jobs",
        "/v1/apps/{app_id}/deployments",
        "/v1/apps/{app_id}/rollbacks",
        "/v1/jobs/{job_id}/cancel",
        "/v1/jobs/{job_id}/reruns",
    ]
    expected_desc = "Retained for at least 24 hours; after that the key may be treated as new."

    for path in mutating_routes:
        op = spec["paths"][path]["post"]
        idemp_params = [p for p in op.get("parameters", []) if p.get("in") == "header" and p.get("name") == "Idempotency-Key"]
        assert len(idemp_params) == 1, f"Missing Idempotency-Key on {path}"
        param = idemp_params[0]
        assert param["description"] == expected_desc, f"Mismatch on {path}: {param['description']}"
        assert param["schema"]["minLength"] == 1
        assert param["schema"]["maxLength"] == 255


def test_t10_idempotency_record_expiration_and_read_path_deletion(client: TestClient, clean_db):
    """Verify unexpired record hits cache; expired record is deleted upon read and treated as new."""
    ws_id, _, auth_headers = create_test_workspace_and_app(client)
    test_key = f"key-exp-{uuid.uuid4().hex[:8]}"
    endpoint = f"/v1/workspaces/{ws_id}/jobs"
    job_payload = {
        "name": "exp-test-job",
        "image_digest": "registry.example.com/job@sha256:7777777777777777777777777777777777777777777777777777777777777777",
    }

    # 1. Initial request -> 202
    r1 = client.post(endpoint, json=job_payload, headers={"Idempotency-Key": test_key, **auth_headers})
    assert r1.status_code == 202
    op1_id = r1.json()["operation_id"]

    # 2. Unexpired replay -> returns cached 202 with op1_id, no new outbox event
    r2 = client.post(endpoint, json=job_payload, headers={"Idempotency-Key": test_key, **auth_headers})
    assert r2.status_code == 202
    assert r2.json()["operation_id"] == op1_id

    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM outbox_events WHERE topic = 'job.submitted.v1'")
        assert cur.fetchone()[0] == 1

        # 3. Simulate passage of time by setting expires_at to 1 hour in the past
        past_time = datetime.now(timezone.utc) - timedelta(hours=1)
        cur.execute("UPDATE idempotency_records SET expires_at = %s WHERE idempotency_key = %s", (past_time, test_key))
    conn.commit()
    conn.close()

    # 4. Next request with the same key treats it as new
    r3 = client.post(endpoint, json=job_payload, headers={"Idempotency-Key": test_key, **auth_headers})
    assert r3.status_code == 202
    op3_id = r3.json()["operation_id"]
    assert op3_id != op1_id, "Expired key did not create a new operation!"

    # 5. Verify a new outbox event was emitted and expires_at is refreshed > now
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM outbox_events WHERE topic = 'job.submitted.v1'")
        assert cur.fetchone()[0] == 2

        cur.execute("SELECT expires_at FROM idempotency_records WHERE idempotency_key = %s", (test_key,))
        new_expires_at = cur.fetchone()[0]
        assert new_expires_at > datetime.now(timezone.utc)
    conn.close()


def test_t10_adr0003_documents_retention_read_path_and_sweeper():
    """Verify ADR-0003 documents 24-hour retention, read-path deletion, and M1 sweeper."""
    assert os.path.exists(ADR_0003), f"Missing ADR-0003 at {ADR_0003}"
    with open(ADR_0003, "r", encoding="utf-8") as f:
        content = f.read()
    assert "24 hours" in content, "ADR-0003 missing 24-hour retention contract documentation!"
    assert "deletes the expired record upon read" in content, "ADR-0003 missing read-path deletion documentation!"
    assert "sweeper" in content.lower(), "ADR-0003 missing sweeper documentation!"
    assert "M1" in content, "ADR-0003 missing M1 sweeper ownership documentation!"


# =============================================================================
# T11 Tests — Release Number Allocation & Real PostgreSQL Integrity Checks
# =============================================================================

def test_t11_release_number_allocation_max_plus_one_after_deletion(client: TestClient, clean_db):
    """Verify release number is allocated as MAX(release_number) + 1, even if middle release deleted."""
    ws_id, app_id, auth_headers = create_test_workspace_and_app(client)

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

    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT release_number FROM releases WHERE application_id = %s ORDER BY release_number", (app_id,))
        numbers = [row[0] for row in cur.fetchall()]
        assert numbers == [1, 2, 3]

        # Delete middle release
        cur.execute("DELETE FROM releases WHERE application_id = %s AND release_number = 2", (app_id,))
    conn.commit()
    conn.close()

    # Deploy 4th release: must allocate MAX(1, 3) + 1 = 4
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


def test_t11_real_postgresql_integrity_error_bubbles_to_500(client: TestClient, clean_db):
    """Verify non-idempotency PostgreSQL constraint violation bubbles up to 500 INTERNAL_SERVER_ERROR."""
    ws_id, _, auth_headers = create_test_workspace_and_app(client)

    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE jobs ADD CONSTRAINT chk_test_job_name_min CHECK (length(name) >= 5)")
        conn.commit()

        # Submit job with name "abc" (length 3, violating CHECK constraint)
        resp = client.post(
            f"/v1/workspaces/{ws_id}/jobs",
            json={"name": "abc", "image_digest": "registry.example.com/job@sha256:1111111111111111111111111111111111111111111111111111111111111111"},
            headers={"Idempotency-Key": f"k-fail-{uuid.uuid4().hex[:6]}", **auth_headers},
        )
        assert resp.status_code == 500
        data = resp.json()
        assert data["error_code"] == "INTERNAL_SERVER_ERROR"
        assert "correlation_id" in data
        assert resp.headers.get("X-Correlation-ID") == data["correlation_id"]
    finally:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS chk_test_job_name_min")
        conn.commit()
        conn.close()


def test_t11_user_data_with_constraint_name_substring_not_mistaken_for_conflict(client: TestClient, clean_db):
    """Verify user data containing 'uq_idempotency_workspace_key' does not get mistaken for idempotency conflict."""
    ws_id, _, auth_headers = create_test_workspace_and_app(client)

    # Job name contains the constraint name substring
    name_with_sub = "uq_idempotency_workspace_key"
    resp = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={"name": name_with_sub, "image_digest": "registry.example.com/job@sha256:1111111111111111111111111111111111111111111111111111111111111111"},
        headers={"Idempotency-Key": f"k-sub-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"


def test_t11_rerun_job_with_98_char_name_truncates_safely(client: TestClient, clean_db):
    """Verify rerun creates job with length <= 100 even when original job name is 98 chars."""
    ws_id, _, auth_headers = create_test_workspace_and_app(client)

    name_98 = "j" * 98
    submit_res = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={"name": name_98, "image_digest": "registry.example.com/job@sha256:1111111111111111111111111111111111111111111111111111111111111111"},
        headers={"Idempotency-Key": f"k-sub-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert submit_res.status_code == 202
    job_id = submit_res.json()["operation_id"]

    # Mark job failed
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'FAILED' WHERE id = %s", (job_id,))
    conn.commit()
    conn.close()

    # Rerun job
    rerun_res = client.post(
        f"/v1/jobs/{job_id}/reruns",
        headers={"Idempotency-Key": f"k-rerun-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert rerun_res.status_code == 202
    new_job_id = rerun_res.json()["operation_id"]

    # Verify new job name length in DB
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM jobs WHERE id = %s", (new_job_id,))
        new_name = cur.fetchone()[0]
    conn.close()
    assert len(new_name) <= 100, f"New job name exceeded 100 chars: {len(new_name)}"
    assert new_name.endswith("-rerun")


# =============================================================================
# T12 Tests — Unified Error Responses & Cursor Pagination
# =============================================================================

def test_t12_starlette_error_responses_standard_envelope(client: TestClient):
    """Verify unknown paths (404) and wrong methods (405) return standard ErrorResponse envelope."""
    # 1. Unknown route -> 404
    r404 = client.get("/v1/nonexistent_route_abc")
    assert r404.status_code == 404
    d404 = r404.json()
    assert d404["error_code"] == "NOT_FOUND"
    assert "correlation_id" in d404
    assert r404.headers.get("X-Correlation-ID") == d404["correlation_id"]

    # 2. Method not allowed -> 405
    r405 = client.post("/healthz")
    assert r405.status_code == 405
    d405 = r405.json()
    assert d405["error_code"] == "METHOD_NOT_ALLOWED"
    assert "correlation_id" in d405
    assert r405.headers.get("X-Correlation-ID") == d405["correlation_id"]


def test_t12_pagination_cursor_decode_overflow_returns_400(client: TestClient, clean_db):
    """Verify cursor with timestamp overflow (year 99999999) returns 400 with 'Invalid pagination cursor'."""
    ws_id, _, auth_headers = create_test_workspace_and_app(client)

    # Encode invalid year cursor
    raw_cursor = "99999999-01-01T00:00:00+00:00|5ba5fb57-0f6a-485b-a113-fd19d329591f"
    b64_cursor = base64.b64encode(raw_cursor.encode()).decode()

    resp = client.get(f"/v1/workspaces/{ws_id}/jobs?cursor={b64_cursor}", headers=auth_headers)
    assert resp.status_code == 400
    data = resp.json()
    assert data["error_code"] == "BAD_REQUEST"
    assert data["message"] == "Invalid pagination cursor"
    assert "correlation_id" in data
    assert resp.headers.get("X-Correlation-ID") == data["correlation_id"]


def test_t12_cursor_pagination_workspace_jobs(client: TestClient, clean_db):
    """Verify cursor pagination on GET /v1/workspaces/{ws}/jobs walks at least 2 pages, caps limit at 100."""
    ws_id, _, auth_headers = create_test_workspace_and_app(client)

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

    traversed_ids = [j["id"] for j in p1_data["items"] + p2_data["items"] + p3_data["items"]]
    assert len(traversed_ids) == 5
    assert set(traversed_ids) == set(created_job_ids)

    # Limit capped at 100 (101 returns 422)
    p_invalid_limit = client.get(f"/v1/workspaces/{ws_id}/jobs?limit=101", headers=auth_headers)
    assert p_invalid_limit.status_code == 422
    assert p_invalid_limit.json()["error_code"] == "VALIDATION_ERROR"


def test_t12_cursor_pagination_app_releases(client: TestClient, clean_db):
    """Verify cursor pagination on GET /v1/apps/{app}/releases walks at least 2 pages, caps limit at 100."""
    ws_id, app_id, auth_headers = create_test_workspace_and_app(client)

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

    traversed_ids = [r["id"] for r in p1_data["items"] + p2_data["items"] + p3_data["items"]]
    assert len(traversed_ids) == 5
    assert set(traversed_ids) == set(created_rel_ids)

    # Limit capped at 100
    p_invalid_limit = client.get(f"/v1/apps/{app_id}/releases?limit=101", headers=auth_headers)
    assert p_invalid_limit.status_code == 422


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

            if "requestBody" in op:
                content = op["requestBody"].get("content", {})
                json_content = content.get("application/json", {})
                assert "example" in json_content or "examples" in json_content, (
                    f"Operation '{op_id}' ({method.upper()} {path}) missing example in requestBody content"
                )

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


# =============================================================================
# T14 Tests — Outbox Event Schema Validation
# =============================================================================

def test_t14_outbox_payloads_validate_against_event_schemas(client: TestClient, clean_db):
    """Verify all outbox payloads inserted by mutating endpoints validate against JSON schemas."""
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
    """Verify /readyz returns 200 when DB and Redis are up, 503 when either is down."""
    res = client.get("/readyz")
    assert res.status_code == 200
    data = res.json()
    assert data["ready"] is True
    assert data["database"] is True
    assert data["redis"] is True

    assert hasattr(app.state, "redis")
    assert app.state.redis is not None

    async def broken_ping():
        raise ConnectionError("Redis down")

    monkeypatch.setattr(app.state.redis, "ping", broken_ping)
    res_down = client.get("/readyz")
    assert res_down.status_code == 503
    down_data = res_down.json()
    assert down_data["error_code"] == "SERVICE_UNAVAILABLE"
    assert down_data["details"]["redis"] is False


# =============================================================================
# T13 Tests — Live Response & Documented Request Examples Validation
# =============================================================================

def test_t13_live_api_responses_validate_against_openapi_schemas(client: TestClient, clean_db):
    """Validate live responses for every response body type returned by the API against OpenAPI component schemas."""
    with open(OPENAPI_SPEC, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    resource = DRAFT202012.create_resource(spec)
    registry = Registry().with_resource("urn:openapi", resource)

    def validate_body(data: dict, schema_name: str) -> None:
        validator = jsonschema.Draft202012Validator(
            {"$ref": f"urn:openapi#/components/schemas/{schema_name}"},
            registry=registry,
        )
        validator.validate(data)

    auth_headers = {"X-Dev-Subject": "contract-tester"}

    # 1. HealthResponse: GET /healthz -> 200
    health_resp = client.get("/healthz")
    assert health_resp.status_code == 200
    validate_body(health_resp.json(), "HealthResponse")

    # 2. ReadinessResponse: GET /readyz -> 200
    readyz_resp = client.get("/readyz")
    assert readyz_resp.status_code == 200
    validate_body(readyz_resp.json(), "ReadinessResponse")

    # 3. WorkspaceResponse: POST /v1/workspaces -> 201
    unique_slug = f"ws-{uuid.uuid4().hex[:8]}"
    ws_resp = client.post("/v1/workspaces", json={"name": "Contracts WS", "slug": unique_slug}, headers=auth_headers)
    assert ws_resp.status_code == 201
    ws_data = ws_resp.json()
    validate_body(ws_data, "WorkspaceResponse")
    ws_id = ws_data["id"]

    # 4. ApplicationResponse: POST /v1/workspaces/{ws}/apps -> 201
    app_slug = f"svc-{uuid.uuid4().hex[:6]}"
    app_resp = client.post(
        f"/v1/workspaces/{ws_id}/apps",
        json={"name": "Contract App", "slug": app_slug, "workload_type": "HTTP_SERVICE"},
        headers=auth_headers,
    )
    assert app_resp.status_code == 201
    app_data = app_resp.json()
    validate_body(app_data, "ApplicationResponse")
    app_id = app_data["id"]

    # 5. AcceptedOperationResponse on Job Submission: POST /v1/workspaces/{ws}/jobs -> 202
    job_payload = {
        "name": "contract-job",
        "image_digest": "registry.example.com/job@sha256:1111111111111111111111111111111111111111111111111111111111111111",
    }
    job_resp = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json=job_payload,
        headers={"Idempotency-Key": f"idemp-job-{uuid.uuid4().hex[:8]}", **auth_headers},
    )
    assert job_resp.status_code == 202
    job_data = job_resp.json()
    validate_body(job_data, "AcceptedOperationResponse")
    job_id = job_data["operation_id"]

    # 6. AcceptedOperationResponse on Job Cancellation: POST /v1/jobs/{job}/cancel -> 202
    cancel_resp = client.post(
        f"/v1/jobs/{job_id}/cancel",
        headers={"Idempotency-Key": f"idemp-cancel-{uuid.uuid4().hex[:8]}", **auth_headers},
    )
    assert cancel_resp.status_code == 202
    validate_body(cancel_resp.json(), "AcceptedOperationResponse")

    # 7. AcceptedOperationResponse on Job Rerun: POST /v1/jobs/{job}/reruns -> 202
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'FAILED' WHERE id = %s", (job_id,))
    conn.commit()
    conn.close()

    rerun_resp = client.post(
        f"/v1/jobs/{job_id}/reruns",
        headers={"Idempotency-Key": f"idemp-rerun-{uuid.uuid4().hex[:8]}", **auth_headers},
    )
    assert rerun_resp.status_code == 202
    validate_body(rerun_resp.json(), "AcceptedOperationResponse")

    # 8. AcceptedOperationResponse on App Deploy: POST /v1/apps/{app}/deployments -> 202
    deploy_payload = {
        "image_digest": "registry.example.com/app@sha256:2222222222222222222222222222222222222222222222222222222222222222",
        "port": 8080,
    }
    deploy_resp = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=deploy_payload,
        headers={"Idempotency-Key": f"idemp-deploy-{uuid.uuid4().hex[:8]}", **auth_headers},
    )
    assert deploy_resp.status_code == 202
    deploy_data = deploy_resp.json()
    validate_body(deploy_data, "AcceptedOperationResponse")
    release_id = deploy_data["operation_id"]

    # 9. AcceptedOperationResponse on App Rollback: POST /v1/apps/{app}/rollbacks -> 202
    client.post(
        f"/v1/apps/{app_id}/deployments",
        json=dict(deploy_payload, port=8081),
        headers={"Idempotency-Key": f"idemp-deploy2-{uuid.uuid4().hex[:8]}", **auth_headers},
    )
    rollback_resp = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json={"target_release_id": release_id},
        headers={"Idempotency-Key": f"idemp-rb-{uuid.uuid4().hex[:8]}", **auth_headers},
    )
    assert rollback_resp.status_code == 202
    validate_body(rollback_resp.json(), "AcceptedOperationResponse")

    # 10. OperationStatusResponse: GET /v1/operations/{id} -> 200
    job_op_resp = client.get(f"/v1/operations/{job_id}", headers=auth_headers)
    assert job_op_resp.status_code == 200
    job_op_data = job_op_resp.json()
    validate_body(job_op_data, "OperationStatusResponse")

    # 11. JobDetailsResponse with at least one Attempt: GET /v1/jobs/{job} -> 200
    attempt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO job_attempts (
                id, job_id, workspace_id, attempt_number, state, resource_uid, lease_epoch,
                exit_code, failure_reason, started_at, finished_at, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            attempt_id, job_id, ws_id, 1, 'RUNNING', 'pod-exec-worker-1', 1,
            None, None, now, None, now, now
        ))
    conn.commit()
    conn.close()

    job_details_resp = client.get(f"/v1/jobs/{job_id}", headers=auth_headers)
    assert job_details_resp.status_code == 200
    job_details_data = job_details_resp.json()
    assert len(job_details_data["attempts"]) >= 1
    validate_body(job_details_data, "JobDetailsResponse")


def test_t13_documented_request_examples_execute_successfully(client: TestClient, clean_db):
    """Verify that documented example request payloads in the OpenAPI spec execute successfully."""
    with open(OPENAPI_SPEC, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    auth_headers = {"X-Dev-Subject": "spec-example-tester"}

    # 1. createWorkspace example
    ws_ex = spec["paths"]["/v1/workspaces"]["post"]["requestBody"]["content"]["application/json"]["example"]
    ws_ex_payload = dict(ws_ex, slug=f"ex-ws-{uuid.uuid4().hex[:6]}")
    r_ws = client.post("/v1/workspaces", json=ws_ex_payload, headers=auth_headers)
    assert r_ws.status_code == 201
    ws_id = r_ws.json()["id"]

    # 2. createApplication example
    app_ex = spec["paths"]["/v1/workspaces/{workspace_id}/apps"]["post"]["requestBody"]["content"]["application/json"]["example"]
    app_ex_payload = dict(app_ex, slug=f"ex-app-{uuid.uuid4().hex[:6]}")
    r_app = client.post(f"/v1/workspaces/{ws_id}/apps", json=app_ex_payload, headers=auth_headers)
    assert r_app.status_code == 201

    # 3. submitJob example
    job_ex = spec["paths"]["/v1/workspaces/{workspace_id}/jobs"]["post"]["requestBody"]["content"]["application/json"]["example"]
    job_key = spec["paths"]["/v1/workspaces/{workspace_id}/jobs"]["post"]["parameters"][1]["schema"]["example"]
    r_job = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json=job_ex,
        headers={"Idempotency-Key": f"{job_key}-{uuid.uuid4().hex[:4]}", **auth_headers},
    )
    assert r_job.status_code == 202


# =============================================================================
# Review fixes — slug races, key normalization, and 422 in the contract
# =============================================================================

@pytest.mark.asyncio
async def test_concurrent_duplicate_workspace_slug_returns_409_not_500(clean_db):
    """The slug pre-check is not atomic; the losing racers must still get 409, never 500."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        auth_headers = {"X-Dev-Subject": "slug-racer"}
        slug = f"race-ws-{uuid.uuid4().hex[:8]}"
        results = await asyncio.gather(*[
            async_client.post("/v1/workspaces", json={"name": "Race WS", "slug": slug}, headers=auth_headers)
            for _ in range(6)
        ])

    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 1, f"Expected exactly one winner, got {codes}"
    assert set(codes) == {201, 409}, f"Losing racers must return 409, got {codes}"
    for r in results:
        if r.status_code == 409:
            body = r.json()
            assert body["error_code"] == "CONFLICT"
            assert slug in body["message"]
            assert r.headers.get("X-Correlation-ID") == body["correlation_id"]


@pytest.mark.asyncio
async def test_concurrent_duplicate_application_slug_returns_409_not_500(clean_db):
    """Same race on application slugs, which are unique per workspace."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        auth_headers = {"X-Dev-Subject": "slug-racer-2"}
        ws = await async_client.post(
            "/v1/workspaces", json={"name": "Race WS2", "slug": f"race-ws2-{uuid.uuid4().hex[:8]}"}, headers=auth_headers
        )
        assert ws.status_code == 201
        ws_id = ws.json()["id"]

        slug = f"race-app-{uuid.uuid4().hex[:8]}"
        results = await asyncio.gather(*[
            async_client.post(
                f"/v1/workspaces/{ws_id}/apps",
                json={"name": "Race App", "slug": slug, "workload_type": "HTTP_SERVICE"},
                headers=auth_headers,
            )
            for _ in range(6)
        ])

    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 1, f"Expected exactly one winner, got {codes}"
    assert set(codes) == {201, 409}, f"Losing racers must return 409, got {codes}"


def test_idempotency_key_is_trimmed_and_blank_key_rejected(client: TestClient, clean_db):
    """A padded key is the same key, and a key that is blank once stripped is rejected."""
    ws_id, app_id, auth_headers = create_test_workspace_and_app(client)
    body = {
        "name": "trim-job",
        "image_digest": "registry.example.com/job@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    }
    key = f"trim-{uuid.uuid4().hex[:8]}"

    first = client.post(f"/v1/workspaces/{ws_id}/jobs", json=body, headers={"Idempotency-Key": key, **auth_headers})
    padded = client.post(
        f"/v1/workspaces/{ws_id}/jobs", json=body, headers={"Idempotency-Key": f"  {key}  ", **auth_headers}
    )
    assert first.status_code == 202
    assert padded.status_code == 202
    assert padded.json()["operation_id"] == first.json()["operation_id"]

    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT idempotency_key FROM idempotency_records WHERE workspace_id = %s", (ws_id,))
        stored = [row[0] for row in cur.fetchall()]
    conn.close()
    assert stored == [key], f"Key stored un-normalized: {stored!r}"

    blank = client.post(f"/v1/workspaces/{ws_id}/jobs", json=body, headers={"Idempotency-Key": "   ", **auth_headers})
    assert blank.status_code == 422
    blank_body = blank.json()
    assert blank_body["error_code"] == "VALIDATION_ERROR"
    assert blank.headers.get("X-Correlation-ID") == blank_body["correlation_id"]


def test_contract_documents_422_on_every_operation_that_can_emit_it(client: TestClient, clean_db):
    """Malformed path/query parameters return 422, so the published contract must declare it."""
    with open(OPENAPI_SPEC, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    ws_id, app_id, auth_headers = create_test_workspace_and_app(client)
    bad_id = "not-a-uuid"
    live_422 = [
        (f"/v1/jobs/{bad_id}", "/v1/jobs/{job_id}"),
        (f"/v1/operations/{bad_id}", "/v1/operations/{operation_id}"),
        (f"/v1/operations/{bad_id}/events", "/v1/operations/{operation_id}/events"),
        (f"/v1/workspaces/{ws_id}/jobs?limit=101", "/v1/workspaces/{workspace_id}/jobs"),
        (f"/v1/apps/{app_id}/releases?limit=101", "/v1/apps/{app_id}/releases"),
    ]
    for url, spec_path in live_422:
        resp = client.get(url, headers=auth_headers)
        assert resp.status_code == 422, f"{url} returned {resp.status_code}"
        assert resp.json()["error_code"] == "VALIDATION_ERROR"
        assert "422" in spec["paths"][spec_path]["get"]["responses"], (
            f"GET {spec_path} returns 422 but the contract does not document it"
        )


# =============================================================================
# Integrity handling — every write path, and the constraint-name helper itself
# =============================================================================

@contextmanager
def temporary_check_constraint(table: str, name: str, expression: str):
    """Add a CHECK constraint for the duration of a test, then always drop it."""
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    try:
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression})")
        conn.commit()
        yield
    finally:
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        conn.commit()
        conn.close()


def assert_unmapped_integrity_500(resp) -> None:
    """An integrity violation that no write path claims is a 500, never a 409."""
    assert resp.status_code == 500, f"Expected 500, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["error_code"] == "INTERNAL_SERVER_ERROR"
    assert resp.headers.get("X-Correlation-ID") == body["correlation_id"]
    # The constraint name belongs in the log, not in the response body.
    assert "UQ_" not in body["message"].upper()
    assert "CHK_" not in body["message"].upper()


def test_integrity_submit_job_non_idempotency_violation_returns_500(client: TestClient, clean_db, caplog):
    ws_id, _, auth_headers = create_test_workspace_and_app(client)
    digest = "registry.example.com/job@sha256:" + "1" * 64
    with caplog.at_level(logging.ERROR, logger="app.core.db_errors"):
        with temporary_check_constraint("jobs", "chk_tmp_job_name", "length(name) >= 5"):
            resp = client.post(
                f"/v1/workspaces/{ws_id}/jobs",
                json={"name": "abc", "image_digest": digest},
                headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:8]}", **auth_headers},
            )
    assert_unmapped_integrity_500(resp)

    # The violation is invisible to the caller, so it has to reach the log with
    # the constraint name and the original exception attached.
    records = [r for r in caplog.records if r.name == "app.core.db_errors"]
    assert len(records) == 1, f"Expected one logged violation, got {len(records)}"
    assert "chk_tmp_job_name" in records[0].getMessage()
    assert records[0].exc_info is not None


def test_integrity_rerun_job_non_idempotency_violation_returns_500(client: TestClient, clean_db):
    ws_id, _, auth_headers = create_test_workspace_and_app(client)
    digest = "registry.example.com/job@sha256:" + "2" * 64
    submitted = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={"name": "rerun-me", "image_digest": digest},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:8]}", **auth_headers},
    )
    assert submitted.status_code == 202
    job_id = submitted.json()["operation_id"]

    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'FAILED' WHERE id = %s", (job_id,))
    conn.commit()
    conn.close()

    # The constraint goes on after the original job exists, so only the rerun's
    # insert, whose name ends in -rerun, can violate it.
    with temporary_check_constraint("jobs", "chk_tmp_no_rerun", "name NOT LIKE '%-rerun'"):
        resp = client.post(
            f"/v1/jobs/{job_id}/reruns",
            headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:8]}", **auth_headers},
        )
    assert_unmapped_integrity_500(resp)


def test_integrity_cancel_job_non_idempotency_violation_returns_500(client: TestClient, clean_db):
    ws_id, _, auth_headers = create_test_workspace_and_app(client)
    digest = "registry.example.com/job@sha256:" + "3" * 64
    submitted = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={"name": "cancel-me", "image_digest": digest},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:8]}", **auth_headers},
    )
    assert submitted.status_code == 202
    job_id = submitted.json()["operation_id"]

    with temporary_check_constraint(
        "outbox_events", "chk_tmp_no_cancel_topic", "topic <> 'job.cancellation.requested.v1'"
    ):
        resp = client.post(
            f"/v1/jobs/{job_id}/cancel",
            headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:8]}", **auth_headers},
        )
    assert_unmapped_integrity_500(resp)


def test_integrity_deploy_release_non_idempotency_violation_returns_500(client: TestClient, clean_db):
    ws_id, app_id, auth_headers = create_test_workspace_and_app(client)
    digest = "registry.example.com/app@sha256:" + "4" * 64
    with temporary_check_constraint("releases", "chk_tmp_release_number", "release_number < 0"):
        resp = client.post(
            f"/v1/apps/{app_id}/deployments",
            json={"image_digest": digest, "port": 8080},
            headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:8]}", **auth_headers},
        )
    assert_unmapped_integrity_500(resp)


def test_integrity_rollback_release_non_idempotency_violation_returns_500(client: TestClient, clean_db):
    ws_id, app_id, auth_headers = create_test_workspace_and_app(client)
    digest = "registry.example.com/app@sha256:" + "5" * 64
    deployed = client.post(
        f"/v1/apps/{app_id}/deployments",
        json={"image_digest": digest, "port": 8080},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:8]}", **auth_headers},
    )
    assert deployed.status_code == 202
    release_id = deployed.json()["operation_id"]

    # Only the rollback's new release can violate this, since the deployed one
    # already exists with release_number 1.
    with temporary_check_constraint("releases", "chk_tmp_first_release_only", "release_number <= 1"):
        resp = client.post(
            f"/v1/apps/{app_id}/rollbacks",
            json={"target_release_id": release_id},
            headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:8]}", **auth_headers},
        )
    assert_unmapped_integrity_500(resp)


def test_integrity_create_workspace_non_slug_violation_returns_500_not_409(client: TestClient, clean_db):
    """A constraint other than uq_workspace_slug must not be reported as a duplicate slug."""
    with temporary_check_constraint("workspaces", "chk_tmp_ws_name", "length(name) >= 5"):
        resp = client.post(
            "/v1/workspaces",
            json={"name": "ab", "slug": f"int-ws-{uuid.uuid4().hex[:8]}"},
            headers={"X-Dev-Subject": "integrity-user"},
        )
    assert_unmapped_integrity_500(resp)


def test_integrity_create_application_non_slug_violation_returns_500_not_409(client: TestClient, clean_db):
    """A constraint other than uq_application_workspace_slug must not be reported as a duplicate slug."""
    ws_id, _, auth_headers = create_test_workspace_and_app(client)
    with temporary_check_constraint("applications", "chk_tmp_app_name", "length(name) >= 5"):
        resp = client.post(
            f"/v1/workspaces/{ws_id}/apps",
            json={"name": "ab", "slug": f"int-app-{uuid.uuid4().hex[:8]}", "workload_type": "HTTP_SERVICE"},
            headers=auth_headers,
        )
    assert_unmapped_integrity_500(resp)


def test_violated_constraint_reads_only_the_driver_reported_name():
    """The helper reads the driver's attribute and never parses the error text."""

    class FakeDriverError(Exception):
        def __init__(self, constraint_name=None):
            super().__init__("duplicate key value violates unique constraint")
            if constraint_name is not None:
                self.constraint_name = constraint_name

    def wrap(orig):
        return IntegrityError("INSERT ...", {}, orig)

    # asyncpg shape: SQLAlchemy's dialect wrapper carries the real error as __cause__
    dialect_wrapper = FakeDriverError()
    dialect_wrapper.__cause__ = FakeDriverError("uq_idempotency_workspace_key")
    assert violated_constraint(wrap(dialect_wrapper)) == "uq_idempotency_workspace_key"

    # A driver that exposes the attribute directly
    assert violated_constraint(wrap(FakeDriverError("uq_workspace_slug"))) == "uq_workspace_slug"

    # No attribute anywhere, even though the message names a constraint
    named_only_in_text = Exception('violates unique constraint "uq_workspace_slug"')
    assert violated_constraint(wrap(named_only_in_text)) is None

    # An empty name is no name
    assert violated_constraint(wrap(FakeDriverError(""))) is None


def test_running_migrations_does_not_disable_application_loggers():
    """alembic's fileConfig must not silence the app.* loggers it finds already created.

    The session fixture runs `alembic upgrade head` in this process before any
    test, so a regression here makes every application log line vanish without
    failing anything else.
    """
    for name in ("app", "app.core.db_errors", "app.main"):
        assert logging.getLogger(name).disabled is False, f"logger {name} was disabled"


# =============================================================================
# Mutation Guards & Full T13 Live Documented Response Comparison
# =============================================================================

def test_t11_rollback_release_allocates_max_plus_one_after_deleting_middle_release(client: TestClient, clean_db):
    """Verify rollback allocates MAX(release_number)+1, not COUNT(*)+1, even when a middle release is deleted."""
    ws_id, app_id, auth_headers = create_test_workspace_and_app(client)

    # Deploy release 1, 2, 3
    rel_ids = []
    for i in range(1, 4):
        resp = client.post(
            f"/v1/apps/{app_id}/deployments",
            json={"image_digest": f"registry.example.com/app@sha256:{str(i)*64}", "port": 8080 + i},
            headers={"Idempotency-Key": f"k-dep-{i}-{uuid.uuid4().hex[:6]}", **auth_headers},
        )
        assert resp.status_code == 202
        rel_ids.append(resp.json()["operation_id"])

    # Verify releases 1, 2, 3 in DB
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, release_number FROM releases WHERE application_id = %s ORDER BY release_number",
            (app_id,),
        )
        rows = cur.fetchall()
        assert [r[1] for r in rows] == [1, 2, 3]

        # Delete middle release (release_number 2)
        cur.execute("DELETE FROM releases WHERE id = %s", (rel_ids[1],))
    conn.commit()
    conn.close()

    # Now releases remaining are [1, 3] (count = 2, max = 3)
    # Roll back targeting release 1
    rb_resp = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json={"target_release_id": rel_ids[0]},
        headers={"Idempotency-Key": f"k-rb-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert rb_resp.status_code == 202
    new_release_id = rb_resp.json()["operation_id"]

    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("SELECT release_number FROM releases WHERE id = %s", (new_release_id,))
        new_rel_num = cur.fetchone()[0]
    conn.close()

    # If count(*)+1 was used, new_rel_num would be 2+1=3 (which would violate uniqueness of release_number).
    # With MAX(release_number)+1, new_rel_num MUST be 3+1 = 4.
    assert new_rel_num == 4, f"Expected release_number 4 (MAX+1), got {new_rel_num}"


def test_t10_idempotency_ttl_minimum_24_hours_on_fresh_record(client: TestClient, clean_db):
    """Verify that a freshly written idempotency record has expires_at at least 24 hours after created_at."""
    ws_id, _, auth_headers = create_test_workspace_and_app(client)
    key = f"ttl-check-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={"name": "ttl-job", "image_digest": "registry.example.com/job@sha256:" + "a" * 64},
        headers={"Idempotency-Key": key, **auth_headers},
    )
    assert resp.status_code == 202

    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT created_at, expires_at FROM idempotency_records WHERE workspace_id = %s AND idempotency_key = %s",
            (ws_id, key),
        )
        row = cur.fetchone()
    conn.close()
    assert row is not None, "Idempotency record not found"
    created_at, expires_at = row
    ttl_diff = (expires_at - created_at).total_seconds()
    # 24 hours = 86400 seconds (allow 1 second slop for database timestamp rounding)
    assert ttl_diff >= 86399, f"Expected idempotency TTL >= 24h (86400s), but got {ttl_diff}s (expires_at={expires_at}, created_at={created_at})"


def test_t10_rollback_replay_succeeds_even_if_target_release_deleted(client: TestClient, clean_db):
    """Verify that an idempotent replay of rollback returns cached 202 even if target release was subsequently deleted."""
    ws_id, app_id, auth_headers = create_test_workspace_and_app(client)

    # Deploy initial release
    dep1 = client.post(
        f"/v1/apps/{app_id}/deployments",
        json={"image_digest": "registry.example.com/app@sha256:" + "b" * 64, "port": 8080},
        headers={"Idempotency-Key": f"k-dep-1-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert dep1.status_code == 202
    target_rel_id = dep1.json()["operation_id"]

    # Deploy second release
    dep2 = client.post(
        f"/v1/apps/{app_id}/deployments",
        json={"image_digest": "registry.example.com/app@sha256:" + "c" * 64, "port": 8081},
        headers={"Idempotency-Key": f"k-dep-2-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert dep2.status_code == 202

    # Initial rollback targeting release 1
    rb_key = f"rb-key-{uuid.uuid4().hex[:8]}"
    rb_payload = {"target_release_id": target_rel_id}
    rb_resp = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json=rb_payload,
        headers={"Idempotency-Key": rb_key, **auth_headers},
    )
    assert rb_resp.status_code == 202
    op_id = rb_resp.json()["operation_id"]

    # Now delete the target release from the database
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM releases WHERE id = %s", (target_rel_id,))
    conn.commit()
    conn.close()

    # Replay with same key and payload: must return stored 202, NOT 404
    replay_resp = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json=rb_payload,
        headers={"Idempotency-Key": rb_key, **auth_headers},
    )
    assert replay_resp.status_code == 202
    assert replay_resp.json()["operation_id"] == op_id


def test_t13_live_responses_match_documented_response_examples(client: TestClient, clean_db):
    """For each documented behavior in the OpenAPI contract, send request and compare live response body with documented example."""
    with open(OPENAPI_SPEC, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    auth_headers = {"X-Dev-Subject": "spec-comparison-tester"}

    # 1. GET /healthz (200)
    doc_health = spec["paths"]["/healthz"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
    res_health = client.get("/healthz")
    assert res_health.status_code == 200
    live_health = res_health.json()
    assert live_health["status"] == doc_health["status"]
    assert live_health["version"] == doc_health["version"]
    assert "timestamp" in live_health

    # 2. GET /readyz (200)
    doc_ready = spec["paths"]["/readyz"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
    res_ready = client.get("/readyz")
    assert res_ready.status_code == 200
    live_ready = res_ready.json()
    assert live_ready["ready"] == doc_ready["ready"]
    assert live_ready["database"] == doc_ready["database"]
    assert live_ready["redis"] == doc_ready["redis"]
    assert "timestamp" in live_ready

    # 3. POST /v1/workspaces (201)
    doc_ws_req = spec["paths"]["/v1/workspaces"]["post"]["requestBody"]["content"]["application/json"]["example"]
    doc_ws_res = spec["paths"]["/v1/workspaces"]["post"]["responses"]["201"]["content"]["application/json"]["example"]
    res_ws = client.post("/v1/workspaces", json=doc_ws_req, headers=auth_headers)
    assert res_ws.status_code == 201
    live_ws = res_ws.json()
    assert live_ws["name"] == doc_ws_res["name"]
    assert live_ws["slug"] == doc_ws_res["slug"]
    assert "id" in live_ws
    ws_id = live_ws["id"]

    # 4. POST /v1/workspaces/{workspace_id}/apps (201)
    doc_app_req = spec["paths"]["/v1/workspaces/{workspace_id}/apps"]["post"]["requestBody"]["content"]["application/json"]["example"]
    doc_app_res = spec["paths"]["/v1/workspaces/{workspace_id}/apps"]["post"]["responses"]["201"]["content"]["application/json"]["example"]
    res_app = client.post(f"/v1/workspaces/{ws_id}/apps", json=doc_app_req, headers=auth_headers)
    assert res_app.status_code == 201
    live_app = res_app.json()
    assert live_app["name"] == doc_app_res["name"]
    assert live_app["slug"] == doc_app_res["slug"]
    assert live_app["workload_type"] == doc_app_res["workload_type"]
    assert live_app["desired_generation"] == doc_app_res["desired_generation"]
    assert live_app["current_release_id"] == doc_app_res["current_release_id"]
    app_id = live_app["id"]

    def get_example_body(content_map: dict) -> dict:
        json_content = content_map.get("application/json", {})
        if "example" in json_content:
            return json_content["example"]
        if "examples" in json_content:
            first_val = next(iter(json_content["examples"].values()))
            if isinstance(first_val, dict) and "value" in first_val:
                return first_val["value"]
            return first_val
        return {}

    # 5. POST /v1/workspaces/{workspace_id}/jobs (202)
    doc_job_req = spec["paths"]["/v1/workspaces/{workspace_id}/jobs"]["post"]["requestBody"]["content"]["application/json"]["example"]
    doc_job_res = get_example_body(spec["paths"]["/v1/workspaces/{workspace_id}/jobs"]["post"]["responses"]["202"]["content"])
    res_job = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json=doc_job_req,
        headers={"Idempotency-Key": f"k-job-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert res_job.status_code == 202
    live_job = res_job.json()
    assert live_job["status"] == doc_job_res["status"]
    assert live_job["status_url"] == f"/v1/operations/{live_job['operation_id']}"
    job_id = live_job["operation_id"]

    # 6. GET /v1/jobs/{job_id} (200)
    doc_job_det = spec["paths"]["/v1/jobs/{job_id}"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
    res_job_det = client.get(f"/v1/jobs/{job_id}", headers=auth_headers)
    assert res_job_det.status_code == 200
    live_job_det = res_job_det.json()
    assert live_job_det["name"] == doc_job_det["name"]
    assert live_job_det["state"] == doc_job_det["state"]
    assert live_job_det["current_attempt_number"] == doc_job_det["current_attempt_number"]
    assert live_job_det["attempts"] == doc_job_det["attempts"]

    # 7. POST /v1/apps/{app_id}/deployments (202)
    doc_dep_req = spec["paths"]["/v1/apps/{app_id}/deployments"]["post"]["requestBody"]["content"]["application/json"]["example"]
    doc_dep_res = get_example_body(spec["paths"]["/v1/apps/{app_id}/deployments"]["post"]["responses"]["202"]["content"])
    res_dep = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=doc_dep_req,
        headers={"Idempotency-Key": f"k-dep-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert res_dep.status_code == 202
    live_dep = res_dep.json()
    assert live_dep["status"] == doc_dep_res["status"]
    assert live_dep["status_url"] == f"/v1/operations/{live_dep['operation_id']}"
    rel_id = live_dep["operation_id"]

    # 8. GET /v1/operations/{operation_id} (200) - for fresh release operation
    doc_op_res = spec["paths"]["/v1/operations/{operation_id}"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
    res_op = client.get(f"/v1/operations/{rel_id}", headers=auth_headers)
    assert res_op.status_code == 200
    live_op = res_op.json()
    assert live_op["operation_kind"] == doc_op_res["operation_kind"]
    # This verifies the example was corrected from ACCEPTED to IMAGE_READY!
    assert live_op["status"] == doc_op_res["status"]
    assert live_op["status"] == "IMAGE_READY"
    assert live_op["status_url"] == f"/v1/operations/{rel_id}"

    # 9. POST /v1/apps/{app_id}/rollbacks (202)
    doc_rb_res = get_example_body(spec["paths"]["/v1/apps/{app_id}/rollbacks"]["post"]["responses"]["202"]["content"])
    res_rb = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json={"target_release_id": rel_id},
        headers={"Idempotency-Key": f"k-rb-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert res_rb.status_code == 202
    live_rb = res_rb.json()
    assert live_rb["status"] == doc_rb_res["status"]
    assert live_rb["status_url"] == f"/v1/operations/{live_rb['operation_id']}"

    # 10. POST /v1/jobs/{job_id}/cancel (202)
    doc_canc_res = get_example_body(spec["paths"]["/v1/jobs/{job_id}/cancel"]["post"]["responses"]["202"]["content"])
    res_canc = client.post(
        f"/v1/jobs/{job_id}/cancel",
        headers={"Idempotency-Key": f"k-canc-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert res_canc.status_code == 202
    live_canc = res_canc.json()
    assert live_canc["status"] == doc_canc_res["status"]
    assert live_canc["status_url"] == f"/v1/operations/{live_canc['operation_id']}"

    # 11. POST /v1/jobs/{job_id}/reruns (202)
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'FAILED' WHERE id = %s", (job_id,))
    conn.commit()
    conn.close()

    doc_rerun_res = get_example_body(spec["paths"]["/v1/jobs/{job_id}/reruns"]["post"]["responses"]["202"]["content"])
    res_rerun = client.post(
        f"/v1/jobs/{job_id}/reruns",
        headers={"Idempotency-Key": f"k-rerun-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert res_rerun.status_code == 202
    live_rerun = res_rerun.json()
    assert live_rerun["status"] == doc_rerun_res["status"]
    assert live_rerun["status_url"] == f"/v1/operations/{live_rerun['operation_id']}"

    # 12. GET /v1/workspaces/{workspace_id}/jobs (200)
    doc_job_list = spec["paths"]["/v1/workspaces/{workspace_id}/jobs"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
    res_job_list = client.get(f"/v1/workspaces/{ws_id}/jobs", headers=auth_headers)
    assert res_job_list.status_code == 200
    live_job_list = res_job_list.json()
    assert live_job_list["next_cursor"] == doc_job_list["next_cursor"]
    assert isinstance(live_job_list["items"], list)
    assert len(live_job_list["items"]) >= 1

    # 13. GET /v1/apps/{app_id}/releases (200)
    doc_rel_list = spec["paths"]["/v1/apps/{app_id}/releases"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
    res_rel_list = client.get(f"/v1/apps/{app_id}/releases", headers=auth_headers)
    assert res_rel_list.status_code == 200
    live_rel_list = res_rel_list.json()
    assert live_rel_list["next_cursor"] == doc_rel_list["next_cursor"]
    assert isinstance(live_rel_list["items"], list)
    assert len(live_rel_list["items"]) >= 1

    # 14. Documented 404 Route-Specific Examples:
    doc_404_examples = spec["components"]["responses"]["404NotFound"]["content"]["application/json"]["examples"]

    # 14a. JobNotFound
    missing_id = str(uuid.uuid4())
    res_404_job = client.get(f"/v1/jobs/{missing_id}", headers=auth_headers)
    assert res_404_job.status_code == 404
    live_404_job = res_404_job.json()
    assert live_404_job["error_code"] == doc_404_examples["JobNotFound"]["value"]["error_code"]
    assert live_404_job["message"] == doc_404_examples["JobNotFound"]["value"]["message"]
    assert live_404_job["message"] == "Job not found"

    # 14b. WorkspaceNotFound
    res_404_ws = client.post(
        f"/v1/workspaces/{missing_id}/apps",
        json={"name": "Test App", "slug": f"app-{uuid.uuid4().hex[:6]}", "workload_type": "HTTP_SERVICE"},
        headers=auth_headers,
    )
    assert res_404_ws.status_code == 404
    live_404_ws = res_404_ws.json()
    assert live_404_ws["error_code"] == doc_404_examples["WorkspaceNotFound"]["value"]["error_code"]
    assert live_404_ws["message"] == doc_404_examples["WorkspaceNotFound"]["value"]["message"]
    assert live_404_ws["message"] == "Workspace not found"

    # 14c. ApplicationNotFound
    res_404_app = client.post(
        f"/v1/apps/{missing_id}/deployments",
        json={"image_digest": "registry.example.com/app@sha256:" + "d" * 64, "port": 8080},
        headers={"Idempotency-Key": f"k-dep-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert res_404_app.status_code == 404
    live_404_app = res_404_app.json()
    assert live_404_app["error_code"] == doc_404_examples["ApplicationNotFound"]["value"]["error_code"]
    assert live_404_app["message"] == doc_404_examples["ApplicationNotFound"]["value"]["message"]
    assert live_404_app["message"] == "Application not found"

    # 14d. TargetReleaseNotFound
    res_404_tb = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json={"target_release_id": missing_id},
        headers={"Idempotency-Key": f"k-rb-{uuid.uuid4().hex[:6]}", **auth_headers},
    )
    assert res_404_tb.status_code == 404
    live_404_tb = res_404_tb.json()
    assert live_404_tb["error_code"] == doc_404_examples["TargetReleaseNotFound"]["value"]["error_code"]
    assert live_404_tb["message"] == doc_404_examples["TargetReleaseNotFound"]["value"]["message"]
    assert live_404_tb["message"] == "Target release not found for this application"

    # 14e. OperationNotFound
    res_404_op = client.get(f"/v1/operations/{missing_id}", headers=auth_headers)
    assert res_404_op.status_code == 404
    live_404_op = res_404_op.json()
    assert live_404_op["error_code"] == doc_404_examples["OperationNotFound"]["value"]["error_code"]
    assert live_404_op["message"] == doc_404_examples["OperationNotFound"]["value"]["message"]
    assert live_404_op["message"] == "Operation not found"

    # 15. Documented 401 Unauthorized example
    doc_401 = spec["components"]["responses"]["401Unauthorized"]["content"]["application/json"]["example"]
    res_401 = client.get(f"/v1/jobs/{missing_id}")
    assert res_401.status_code == 401
    live_401 = res_401.json()
    assert live_401["error_code"] == doc_401["error_code"]
    assert live_401["message"] == doc_401["message"]

    # 16. Documented 405 MethodNotAllowed example
    doc_405 = spec["components"]["responses"]["405MethodNotAllowed"]["content"]["application/json"]["example"]
    res_405 = client.post("/healthz")
    assert res_405.status_code == 405
    live_405 = res_405.json()
    assert live_405["error_code"] == doc_405["error_code"]
    assert live_405["message"] == doc_405["message"]

    # 17. Documented 422 ValidationError example
    doc_422 = spec["components"]["responses"]["422UnprocessableEntity"]["content"]["application/json"]["examples"]["ValidationError"]["value"]
    res_422 = client.post("/v1/workspaces", json={}, headers=auth_headers)
    assert res_422.status_code == 422
    live_422 = res_422.json()
    assert live_422["error_code"] == doc_422["error_code"]
    assert live_422["message"] == doc_422["message"]

    # 18. Documented 422 MissingIdempotencyKey example
    doc_422_idemp = spec["components"]["responses"]["422UnprocessableEntity"]["content"]["application/json"]["examples"]["MissingIdempotencyKey"]["value"]
    res_422_idemp = client.post(f"/v1/workspaces/{ws_id}/jobs", json=doc_job_req, headers=auth_headers)
    assert res_422_idemp.status_code == 422
    live_422_idemp = res_422_idemp.json()
    assert live_422_idemp["error_code"] == doc_422_idemp["error_code"]
    assert live_422_idemp["message"] == doc_422_idemp["message"]
    assert live_422_idemp["details"]["errors"] == doc_422_idemp["details"]["errors"]

    # 19. Documented 409 IdempotencyConflict and IdenticalReplay
    doc_409_conflict = spec["components"]["responses"]["409Conflict"]["content"]["application/json"]["examples"]["IdempotencyConflict"]["value"]
    test_idemp_key = f"k-replay-{uuid.uuid4().hex[:6]}"
    orig_res = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json=doc_job_req,
        headers={"Idempotency-Key": test_idemp_key, **auth_headers},
    )
    assert orig_res.status_code == 202
    orig_body = orig_res.json()

    # IdenticalReplay: replay body equals original 202
    replay_res = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json=doc_job_req,
        headers={"Idempotency-Key": test_idemp_key, **auth_headers},
    )
    assert replay_res.status_code == 202
    assert replay_res.json() == orig_body

    # IdempotencyConflict: same key with different body
    conflict_res = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={**doc_job_req, "name": "conflicting-job-name"},
        headers={"Idempotency-Key": test_idemp_key, **auth_headers},
    )
    assert conflict_res.status_code == 409
    live_409 = conflict_res.json()
    assert live_409["error_code"] == doc_409_conflict["error_code"]
    assert live_409["message"] == doc_409_conflict["message"]

