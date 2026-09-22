import uuid
from datetime import datetime, timezone
import jsonschema
import psycopg2
import pytest
import referencing
from referencing.jsonschema import DRAFT202012
import yaml
from fastapi.testclient import TestClient

from tests.conftest import OPENAPI_SPEC, TEST_DATABASE_URL_SYNC

pytestmark = pytest.mark.usefixtures("clean_db")


def test_workspace_creation_and_duplicate_slug(client: TestClient):
    # 1. Unauthenticated workspace creation returns 401
    unauth_resp = client.post("/v1/workspaces", json={"name": "No Auth", "slug": "no-auth-ws"})
    assert unauth_resp.status_code == 401
    assert unauth_resp.json()["error_code"] == "UNAUTHORIZED"
    assert unauth_resp.headers.get("www-authenticate") == "Bearer"

    # 2. Authenticated workspace creation
    unique_slug = f"ws-{uuid.uuid4().hex[:8]}"
    payload = {"name": "Test Workspace", "slug": unique_slug}
    resp = client.post("/v1/workspaces", json=payload, headers={"X-Dev-Subject": "user-creator"})
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["slug"] == unique_slug
    assert "id" in data
    ws_id = data["id"]

    # Verify caller is recorded as OWNER in workspace_memberships
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT role FROM workspace_memberships WHERE workspace_id = %s AND user_subject = %s",
            (ws_id, "user-creator"),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "OWNER"
    conn.close()

    # 3. Duplicate slug returns 409 Conflict
    dup_resp = client.post("/v1/workspaces", json=payload, headers={"X-Dev-Subject": "user-creator"})
    assert dup_resp.status_code == 409
    assert dup_resp.json()["error_code"] == "CONFLICT"


def test_application_and_deployment_flow(client: TestClient):
    auth_headers = {"X-Dev-Subject": "user-dev"}
    unique_slug = f"ws-{uuid.uuid4().hex[:8]}"
    ws_resp = client.post("/v1/workspaces", json={"name": "App WS", "slug": unique_slug}, headers=auth_headers)
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    # 1. Create Application
    app_slug = f"svc-{uuid.uuid4().hex[:6]}"
    app_resp = client.post(
        f"/v1/workspaces/{ws_id}/apps",
        json={"name": "My Web Service", "slug": app_slug, "workload_type": "HTTP_SERVICE"},
        headers=auth_headers,
    )
    assert app_resp.status_code == 201, app_resp.text
    app_data = app_resp.json()
    app_id = app_data["id"]
    assert app_data["desired_generation"] == 1

    # 2. Deploy Release with Idempotency-Key (no X-Workspace-ID header needed)
    idemp_key = f"idemp-deploy-{uuid.uuid4().hex[:8]}"
    deploy_payload = {
        "image_digest": "registry.example.com/web@sha256:7f83b1657ff1fc53b92dc18148a1d65dfc2d4b1fa3d677284addd200126d9069",
        "port": 8080,
        "health_path": "/healthz",
    }
    deploy_resp = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=deploy_payload,
        headers={"Idempotency-Key": idemp_key, **auth_headers},
    )
    assert deploy_resp.status_code == 202, deploy_resp.text
    deploy_data = deploy_resp.json()
    assert deploy_data["status"] == "ACCEPTED"
    release_id = deploy_data["operation_id"]

    # 3. Retransmit with exact same Idempotency-Key -> returns cached 202
    dup_deploy_resp = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=deploy_payload,
        headers={"Idempotency-Key": idemp_key, **auth_headers},
    )
    assert dup_deploy_resp.status_code == 202
    assert dup_deploy_resp.json()["operation_id"] == release_id

    # 4. Retransmit with same key but different payload -> 409 Conflict
    diff_payload = dict(deploy_payload, port=9090)
    conflict_resp = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=diff_payload,
        headers={"Idempotency-Key": idemp_key, **auth_headers},
    )
    assert conflict_resp.status_code == 409

    # 5. Rollback to target release
    rollback_resp = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json={"target_release_id": release_id},
        headers={"Idempotency-Key": f"idemp-rb-{uuid.uuid4().hex[:8]}", **auth_headers},
    )
    assert rollback_resp.status_code == 202, rollback_resp.text
    assert rollback_resp.json()["status"] == "ACCEPTED"


def test_job_flow_and_lifecycle(client: TestClient):
    auth_headers = {"X-Dev-Subject": "user-jobs"}
    unique_slug = f"ws-{uuid.uuid4().hex[:8]}"
    ws_resp = client.post("/v1/workspaces", json={"name": "Job WS", "slug": unique_slug}, headers=auth_headers)
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    # 1. Submit Job with Idempotency-Key
    idemp_key = f"idemp-job-{uuid.uuid4().hex[:8]}"
    job_payload = {
        "name": "daily-aggregation",
        "image_digest": "registry.example.com/batch@sha256:4b227777d4dd1fc61c6f884f48641d02b4d121d3fd328cb08b5531fcacdabf8a",
        "command_args": ["run", "--date", "2026-09-12"],
        "timeout_seconds": 300,
        "max_retries": 2,
    }
    job_resp = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json=job_payload,
        headers={"Idempotency-Key": idemp_key, **auth_headers},
    )
    assert job_resp.status_code == 202, job_resp.text
    job_id = job_resp.json()["operation_id"]

    # 2. Get Job details (no X-Workspace-ID or workspace_id query param needed)
    get_resp = client.get(f"/v1/jobs/{job_id}", headers=auth_headers)
    assert get_resp.status_code == 200, get_resp.text
    job_details = get_resp.json()
    assert job_details["id"] == job_id
    assert job_details["state"] == "QUEUED"
    assert job_details["workspace_id"] == ws_id

    # 3. Cancel Job
    cancel_resp = client.post(
        f"/v1/jobs/{job_id}/cancel",
        headers={"Idempotency-Key": f"idemp-cancel-{uuid.uuid4().hex[:8]}", **auth_headers},
    )
    assert cancel_resp.status_code == 202, cancel_resp.text

    # Verify state updated to CANCEL_REQUESTED
    check_resp = client.get(f"/v1/jobs/{job_id}", headers=auth_headers)
    assert check_resp.json()["state"] == "CANCEL_REQUESTED"


def test_operations_status_and_stream(client: TestClient):
    auth_headers = {"X-Dev-Subject": "user-ops"}
    unique_slug = f"ws-{uuid.uuid4().hex[:8]}"
    ws_resp = client.post("/v1/workspaces", json={"name": "Ops WS", "slug": unique_slug}, headers=auth_headers)
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    # Submit job to get operation ID
    job_resp = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={
            "name": "stream-job",
            "image_digest": "registry.example.com/img@sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        },
        headers={"Idempotency-Key": f"idemp-{uuid.uuid4().hex[:8]}", **auth_headers},
    )
    assert job_resp.status_code == 202
    op_id = job_resp.json()["operation_id"]

    # 1. Get Operation status (authorized member)
    status_resp = client.get(f"/v1/operations/{op_id}", headers=auth_headers)
    assert status_resp.status_code == 200, status_resp.text
    resp_data = status_resp.json()
    assert resp_data["operation_id"] == op_id
    assert resp_data["operation_kind"] == "JOB"
    assert resp_data["status"] == "QUEUED"
    assert resp_data["status_url"] == f"/v1/operations/{op_id}"

    # 2. Stream operation events returns 501 Not Implemented in M0 for authorized caller
    stream_resp = client.get(f"/v1/operations/{op_id}/events", headers=auth_headers)
    assert stream_resp.status_code == 501
    err = stream_resp.json()
    assert err["error_code"] == "NOT_IMPLEMENTED"
    assert "correlation_id" in err
    assert stream_resp.headers.get("X-Correlation-ID") == err["correlation_id"]


def test_tenant_isolation_two_workspaces_and_subjects(client: TestClient):
    """T7 Done-when: Validate tenant isolation across 2 real workspaces and 2 subjects.

    For every tenant route:
    1. A member succeeds (200, 201, 202, or 501).
    2. A non-member using a REAL resource ID gets 404, byte-identical to a missing ID.
    3. A request with no identity gets 401 Unauthorized.
    4. A viewer's mutation gets 403 Forbidden.
    """
    sub_alice = "sub-alice-owner"
    sub_bob = "sub-bob-intruder"
    sub_charlie = "sub-charlie-viewer"

    headers_alice = {"X-Dev-Subject": sub_alice}
    headers_bob = {"X-Dev-Subject": sub_bob}
    headers_charlie = {"X-Dev-Subject": sub_charlie}

    # 1. Alice creates Workspace 1
    ws1_resp = client.post(
        "/v1/workspaces",
        json={"name": "Alice Workspace", "slug": f"alice-ws-{uuid.uuid4().hex[:6]}"},
        headers=headers_alice,
    )
    assert ws1_resp.status_code == 201
    ws1_id = ws1_resp.json()["id"]

    # 2. Bob creates Workspace 2 (Bob is a real tenant in the system, but NOT a member of WS 1)
    ws2_resp = client.post(
        "/v1/workspaces",
        json={"name": "Bob Workspace", "slug": f"bob-ws-{uuid.uuid4().hex[:6]}"},
        headers=headers_bob,
    )
    assert ws2_resp.status_code == 201
    ws2_id = ws2_resp.json()["id"]

    # Bob succeeds in creating an application in his own workspace (ws2)
    bob_own_app = client.post(
        f"/v1/workspaces/{ws2_id}/apps",
        json={"name": "Bob App", "slug": f"bob-app-{uuid.uuid4().hex[:6]}", "workload_type": "HTTP_SERVICE"},
        headers=headers_bob,
    )
    assert bob_own_app.status_code == 201

    # 3. Add Charlie as VIEWER in Workspace 1 directly into workspace_memberships
    now = datetime.now(timezone.utc)
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workspace_memberships (id, workspace_id, user_subject, role, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), ws1_id, sub_charlie, "VIEWER", now, now),
        )
    conn.commit()
    conn.close()

    # 4. Create real resources in WS 1 owned by Alice
    # App
    app_resp = client.post(
        f"/v1/workspaces/{ws1_id}/apps",
        json={"name": "Isolation App", "slug": f"iso-app-{uuid.uuid4().hex[:6]}", "workload_type": "HTTP_SERVICE"},
        headers=headers_alice,
    )
    assert app_resp.status_code == 201
    app_id = app_resp.json()["id"]

    # Deployment / Release
    deploy_payload = {
        "image_digest": "registry.example.com/app@sha256:3333333333333333333333333333333333333333333333333333333333333333",
        "port": 8080,
    }
    deploy_resp = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=deploy_payload,
        headers={"Idempotency-Key": f"idemp-deploy-{uuid.uuid4().hex[:8]}", **headers_alice},
    )
    assert deploy_resp.status_code == 202
    release_id = deploy_resp.json()["operation_id"]

    # Job
    job_payload = {
        "name": "isolation-job",
        "image_digest": "registry.example.com/worker@sha256:4444444444444444444444444444444444444444444444444444444444444444",
    }
    job_resp = client.post(
        f"/v1/workspaces/{ws1_id}/jobs",
        json=job_payload,
        headers={"Idempotency-Key": f"idemp-job-{uuid.uuid4().hex[:8]}", **headers_alice},
    )
    assert job_resp.status_code == 202
    job_id = job_resp.json()["operation_id"]

    # Helper: assert response is byte-identical 404 to a missing ID
    def assert_byte_identical_404(missing_resp, non_member_resp, expected_message: str):
        assert missing_resp.status_code == 404
        assert non_member_resp.status_code == 404
        m_json = missing_resp.json()
        nm_json = non_member_resp.json()
        assert m_json["error_code"] == "NOT_FOUND"
        assert nm_json["error_code"] == "NOT_FOUND"
        assert m_json["message"] == expected_message
        assert nm_json["message"] == expected_message
        # Byte-identical structure and message (excluding correlation_id)
        m_clean = {k: v for k, v in m_json.items() if k != "correlation_id"}
        nm_clean = {k: v for k, v in nm_json.items() if k != "correlation_id"}
        assert m_clean == nm_clean

    def assert_401_unauthorized(resp):
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "UNAUTHORIZED"
        assert resp.json()["message"] == "Authentication required"
        assert resp.headers.get("www-authenticate") == "Bearer"

    def assert_403_forbidden(resp):
        assert resp.status_code == 403
        assert resp.json()["error_code"] == "FORBIDDEN"
        assert resp.json()["message"] == "Insufficient role for this operation"

    # =========================================================================
    # Route 1: POST /v1/workspaces/{ws}/apps (Create App)
    # =========================================================================
    missing_ws = str(uuid.uuid4())
    app_create_body = {"name": "New App", "slug": f"new-app-{uuid.uuid4().hex[:6]}", "workload_type": "HTTP_SERVICE"}

    # Missing ID vs Non-member
    r1_missing = client.post(f"/v1/workspaces/{missing_ws}/apps", json=app_create_body, headers=headers_alice)
    r1_non_member = client.post(f"/v1/workspaces/{ws1_id}/apps", json=app_create_body, headers=headers_bob)
    assert_byte_identical_404(r1_missing, r1_non_member, "Workspace not found")

    # No identity
    r1_no_auth = client.post(f"/v1/workspaces/{ws1_id}/apps", json=app_create_body)
    assert_401_unauthorized(r1_no_auth)

    # Viewer mutation
    r1_viewer = client.post(f"/v1/workspaces/{ws1_id}/apps", json=app_create_body, headers=headers_charlie)
    assert_403_forbidden(r1_viewer)

    # Member succeeds
    r1_member = client.post(
        f"/v1/workspaces/{ws1_id}/apps",
        json={"name": "Alice App 2", "slug": f"app2-{uuid.uuid4().hex[:6]}", "workload_type": "HTTP_SERVICE"},
        headers=headers_alice,
    )
    assert r1_member.status_code == 201

    # =========================================================================
    # Route 2: POST /v1/apps/{app_id}/deployments (Deploy)
    # =========================================================================
    missing_app = str(uuid.uuid4())
    deploy_body = {"image_digest": "registry.example.com/app@sha256:5555555555555555555555555555555555555555555555555555555555555555", "port": 8080}

    r2_missing = client.post(
        f"/v1/apps/{missing_app}/deployments",
        json=deploy_body,
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_alice},
    )
    r2_non_member = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=deploy_body,
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_bob},
    )
    assert_byte_identical_404(r2_missing, r2_non_member, "Application not found")

    r2_no_auth = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=deploy_body,
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}"},
    )
    assert_401_unauthorized(r2_no_auth)

    r2_viewer = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=deploy_body,
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_charlie},
    )
    assert_403_forbidden(r2_viewer)

    r2_member = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=deploy_body,
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_alice},
    )
    assert r2_member.status_code == 202

    # =========================================================================
    # Route 3: POST /v1/apps/{app_id}/rollbacks (Rollback)
    # =========================================================================
    rollback_body = {"target_release_id": release_id}

    r3_missing = client.post(
        f"/v1/apps/{missing_app}/rollbacks",
        json=rollback_body,
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_alice},
    )
    r3_non_member = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json=rollback_body,
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_bob},
    )
    assert_byte_identical_404(r3_missing, r3_non_member, "Application not found")

    r3_no_auth = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json=rollback_body,
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}"},
    )
    assert_401_unauthorized(r3_no_auth)

    r3_viewer = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json=rollback_body,
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_charlie},
    )
    assert_403_forbidden(r3_viewer)

    r3_member = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json=rollback_body,
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_alice},
    )
    assert r3_member.status_code == 202

    # =========================================================================
    # Route 4: POST /v1/workspaces/{ws}/jobs (Submit Job)
    # =========================================================================
    job_submit_body = {
        "name": "iso-job-2",
        "image_digest": "registry.example.com/worker@sha256:6666666666666666666666666666666666666666666666666666666666666666",
    }

    r4_missing = client.post(
        f"/v1/workspaces/{missing_ws}/jobs",
        json=job_submit_body,
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_alice},
    )
    r4_non_member = client.post(
        f"/v1/workspaces/{ws1_id}/jobs",
        json=job_submit_body,
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_bob},
    )
    assert_byte_identical_404(r4_missing, r4_non_member, "Workspace not found")

    r4_no_auth = client.post(
        f"/v1/workspaces/{ws1_id}/jobs",
        json=job_submit_body,
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}"},
    )
    assert_401_unauthorized(r4_no_auth)

    r4_viewer = client.post(
        f"/v1/workspaces/{ws1_id}/jobs",
        json=job_submit_body,
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_charlie},
    )
    assert_403_forbidden(r4_viewer)

    r4_member = client.post(
        f"/v1/workspaces/{ws1_id}/jobs",
        json=job_submit_body,
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_alice},
    )
    assert r4_member.status_code == 202

    # =========================================================================
    # Route 5: GET /v1/jobs/{job_id} (Get Job)
    # =========================================================================
    missing_job = str(uuid.uuid4())

    r5_missing = client.get(f"/v1/jobs/{missing_job}", headers=headers_alice)
    r5_non_member = client.get(f"/v1/jobs/{job_id}", headers=headers_bob)
    assert_byte_identical_404(r5_missing, r5_non_member, "Job not found")

    r5_no_auth = client.get(f"/v1/jobs/{job_id}")
    assert_401_unauthorized(r5_no_auth)

    # Read endpoint: both OWNER (Alice) and VIEWER (Charlie) succeed
    r5_member = client.get(f"/v1/jobs/{job_id}", headers=headers_alice)
    assert r5_member.status_code == 200
    r5_viewer = client.get(f"/v1/jobs/{job_id}", headers=headers_charlie)
    assert r5_viewer.status_code == 200

    # =========================================================================
    # Route 6: POST /v1/jobs/{job_id}/cancel (Cancel Job)
    # =========================================================================
    # Submit job to cancel
    cancel_target_resp = client.post(
        f"/v1/workspaces/{ws1_id}/jobs",
        json=job_submit_body,
        headers={"Idempotency-Key": f"k-canc-{uuid.uuid4().hex[:6]}", **headers_alice},
    )
    target_job_id = cancel_target_resp.json()["operation_id"]

    r6_missing = client.post(
        f"/v1/jobs/{missing_job}/cancel",
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_alice},
    )
    r6_non_member = client.post(
        f"/v1/jobs/{target_job_id}/cancel",
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_bob},
    )
    assert_byte_identical_404(r6_missing, r6_non_member, "Job not found")

    r6_no_auth = client.post(
        f"/v1/jobs/{target_job_id}/cancel",
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}"},
    )
    assert_401_unauthorized(r6_no_auth)

    r6_viewer = client.post(
        f"/v1/jobs/{target_job_id}/cancel",
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_charlie},
    )
    assert_403_forbidden(r6_viewer)

    r6_member = client.post(
        f"/v1/jobs/{target_job_id}/cancel",
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_alice},
    )
    assert r6_member.status_code == 202

    # =========================================================================
    # Route 7: POST /v1/jobs/{job_id}/reruns (Rerun Job)
    # =========================================================================
    # Submit a job and set state to FAILED
    rerun_target_resp = client.post(
        f"/v1/workspaces/{ws1_id}/jobs",
        json=job_submit_body,
        headers={"Idempotency-Key": f"k-rerun-{uuid.uuid4().hex[:6]}", **headers_alice},
    )
    rerun_job_id = rerun_target_resp.json()["operation_id"]
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'FAILED' WHERE id = %s", (rerun_job_id,))
    conn.commit()
    conn.close()

    r7_missing = client.post(
        f"/v1/jobs/{missing_job}/reruns",
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_alice},
    )
    r7_non_member = client.post(
        f"/v1/jobs/{rerun_job_id}/reruns",
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_bob},
    )
    assert_byte_identical_404(r7_missing, r7_non_member, "Job not found")

    r7_no_auth = client.post(
        f"/v1/jobs/{rerun_job_id}/reruns",
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}"},
    )
    assert_401_unauthorized(r7_no_auth)

    r7_viewer = client.post(
        f"/v1/jobs/{rerun_job_id}/reruns",
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_charlie},
    )
    assert_403_forbidden(r7_viewer)

    r7_member = client.post(
        f"/v1/jobs/{rerun_job_id}/reruns",
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}", **headers_alice},
    )
    assert r7_member.status_code == 202

    # =========================================================================
    # Route 8: GET /v1/operations/{id} (Operation Status)
    # =========================================================================
    missing_op = str(uuid.uuid4())

    # 8a. Test with Release operation ID
    r8_missing = client.get(f"/v1/operations/{missing_op}", headers=headers_alice)
    r8_non_member = client.get(f"/v1/operations/{release_id}", headers=headers_bob)
    assert_byte_identical_404(r8_missing, r8_non_member, "Operation not found")

    r8_no_auth = client.get(f"/v1/operations/{release_id}")
    assert_401_unauthorized(r8_no_auth)

    r8_member = client.get(f"/v1/operations/{release_id}", headers=headers_alice)
    assert r8_member.status_code == 200
    assert r8_member.json()["operation_kind"] == "RELEASE"
    r8_viewer = client.get(f"/v1/operations/{release_id}", headers=headers_charlie)
    assert r8_viewer.status_code == 200

    # 8b. Test with Job operation ID
    r8_job_missing = client.get(f"/v1/operations/{missing_op}", headers=headers_alice)
    r8_job_non_member = client.get(f"/v1/operations/{job_id}", headers=headers_bob)
    assert_byte_identical_404(r8_job_missing, r8_job_non_member, "Operation not found")

    r8_job_no_auth = client.get(f"/v1/operations/{job_id}")
    assert_401_unauthorized(r8_job_no_auth)

    r8_job_member = client.get(f"/v1/operations/{job_id}", headers=headers_alice)
    assert r8_job_member.status_code == 200
    assert r8_job_member.json()["operation_kind"] == "JOB"
    r8_job_viewer = client.get(f"/v1/operations/{job_id}", headers=headers_charlie)
    assert r8_job_viewer.status_code == 200

    # =========================================================================
    # Route 9: GET /v1/operations/{id}/events (Operation Events)
    # =========================================================================
    # 9a. Test with Release operation ID
    r9_missing = client.get(f"/v1/operations/{missing_op}/events", headers=headers_alice)
    r9_non_member = client.get(f"/v1/operations/{release_id}/events", headers=headers_bob)
    assert_byte_identical_404(r9_missing, r9_non_member, "Operation not found")

    r9_no_auth = client.get(f"/v1/operations/{release_id}/events")
    assert_401_unauthorized(r9_no_auth)

    # For authorized caller (member), returns 501 Not Implemented
    r9_member = client.get(f"/v1/operations/{release_id}/events", headers=headers_alice)
    assert r9_member.status_code == 501
    r9_viewer = client.get(f"/v1/operations/{release_id}/events", headers=headers_charlie)
    assert r9_viewer.status_code == 501

    # 9b. Test with Job operation ID
    r9_job_missing = client.get(f"/v1/operations/{missing_op}/events", headers=headers_alice)
    r9_job_non_member = client.get(f"/v1/operations/{job_id}/events", headers=headers_bob)
    assert_byte_identical_404(r9_job_missing, r9_job_non_member, "Operation not found")

    r9_job_no_auth = client.get(f"/v1/operations/{job_id}/events")
    assert_401_unauthorized(r9_job_no_auth)

    r9_job_member = client.get(f"/v1/operations/{job_id}/events", headers=headers_alice)
    assert r9_job_member.status_code == 501
    r9_job_viewer = client.get(f"/v1/operations/{job_id}/events", headers=headers_charlie)
    assert r9_job_viewer.status_code == 501

    # =========================================================================
    # Route 10: GET /v1/workspaces/{ws}/jobs (List Workspace Jobs)
    # =========================================================================
    r10_missing = client.get(f"/v1/workspaces/{missing_ws}/jobs", headers=headers_alice)
    r10_non_member = client.get(f"/v1/workspaces/{ws1_id}/jobs", headers=headers_bob)
    assert_byte_identical_404(r10_missing, r10_non_member, "Workspace not found")

    r10_no_auth = client.get(f"/v1/workspaces/{ws1_id}/jobs")
    assert_401_unauthorized(r10_no_auth)

    r10_member = client.get(f"/v1/workspaces/{ws1_id}/jobs", headers=headers_alice)
    assert r10_member.status_code == 200
    r10_viewer = client.get(f"/v1/workspaces/{ws1_id}/jobs", headers=headers_charlie)
    assert r10_viewer.status_code == 200

    # =========================================================================
    # Route 11: GET /v1/apps/{app}/releases (List App Releases)
    # =========================================================================
    r11_missing = client.get(f"/v1/apps/{missing_app}/releases", headers=headers_alice)
    r11_non_member = client.get(f"/v1/apps/{app_id}/releases", headers=headers_bob)
    assert_byte_identical_404(r11_missing, r11_non_member, "Application not found")

    r11_no_auth = client.get(f"/v1/apps/{app_id}/releases")
    assert_401_unauthorized(r11_no_auth)

    r11_member = client.get(f"/v1/apps/{app_id}/releases", headers=headers_alice)
    assert r11_member.status_code == 200
    r11_viewer = client.get(f"/v1/apps/{app_id}/releases", headers=headers_charlie)
    assert r11_viewer.status_code == 200


def test_all_api_responses_validate_against_openapi_schemas(client: TestClient):
    """Validate live responses for every response body type returned by the API against OpenAPI component schemas.

    Uses `referencing` library (replacing deprecated jsonschema.RefResolver) to resolve $ref pointers.
    Covers /healthz, /readyz, 201s, 202s, 200s, 401, 403, 404, 409, and 501.
    """
    with open(OPENAPI_SPEC, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    resource = DRAFT202012.create_resource(spec)
    registry = referencing.Registry().with_resource("urn:openapi", resource)

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

    # 4. ApplicationResponse: POST /v1/workspaces/{ws}/apps -> 201 (current_release_id: null vs type: [string, 'null'])
    app_slug = f"svc-{uuid.uuid4().hex[:6]}"
    app_resp = client.post(
        f"/v1/workspaces/{ws_id}/apps",
        json={"name": "Contract App", "slug": app_slug, "workload_type": "HTTP_SERVICE"},
        headers=auth_headers,
    )
    assert app_resp.status_code == 201
    app_data = app_resp.json()
    assert app_data["current_release_id"] is None
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
    deploy2_resp = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=dict(deploy_payload, port=8081),
        headers={"Idempotency-Key": f"idemp-deploy2-{uuid.uuid4().hex[:8]}", **auth_headers},
    )
    assert deploy2_resp.status_code == 202

    rollback_resp = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json={"target_release_id": release_id},
        headers={"Idempotency-Key": f"idemp-rb-{uuid.uuid4().hex[:8]}", **auth_headers},
    )
    assert rollback_resp.status_code == 202
    validate_body(rollback_resp.json(), "AcceptedOperationResponse")

    # 10. OperationStatusResponse for Job: GET /v1/operations/{id} -> 200
    job_op_resp = client.get(f"/v1/operations/{job_id}", headers=auth_headers)
    assert job_op_resp.status_code == 200
    job_op_data = job_op_resp.json()
    assert job_op_data["operation_kind"] == "JOB"
    assert job_op_data["details"] is None
    validate_body(job_op_data, "OperationStatusResponse")

    # 11. OperationStatusResponse for Release: GET /v1/operations/{id} -> 200
    rel_op_resp = client.get(f"/v1/operations/{release_id}", headers=auth_headers)
    assert rel_op_resp.status_code == 200
    rel_op_data = rel_op_resp.json()
    assert rel_op_data["operation_kind"] == "RELEASE"
    assert rel_op_data["details"] is None
    validate_body(rel_op_data, "OperationStatusResponse")

    # 12. JobDetailsResponse with at least one Attempt: GET /v1/jobs/{job} -> 200
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
        cur.execute("UPDATE jobs SET current_attempt_number = 1, state = 'RUNNING' WHERE id = %s", (job_id,))
    conn.commit()
    conn.close()

    job_details_resp = client.get(f"/v1/jobs/{job_id}", headers=auth_headers)
    assert job_details_resp.status_code == 200
    job_details_data = job_details_resp.json()
    assert len(job_details_data["attempts"]) == 1
    assert job_details_data["attempts"][0]["exit_code"] is None
    validate_body(job_details_data, "JobDetailsResponse")

    # 13. ErrorResponse: 401 Unauthorized
    err_401_resp = client.get(f"/v1/jobs/{job_id}")
    assert err_401_resp.status_code == 401
    validate_body(err_401_resp.json(), "ErrorResponse")

    # 14. ErrorResponse: 403 Forbidden
    now = datetime.now(timezone.utc)
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workspace_memberships (id, workspace_id, user_subject, role, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), ws_id, "viewer-only", "VIEWER", now, now),
        )
    conn.commit()
    conn.close()
    err_403_resp = client.post(
        f"/v1/workspaces/{ws_id}/apps",
        json={"name": "Forbidden App", "slug": f"fb-{uuid.uuid4().hex[:6]}", "workload_type": "HTTP_SERVICE"},
        headers={"X-Dev-Subject": "viewer-only"},
    )
    assert err_403_resp.status_code == 403
    validate_body(err_403_resp.json(), "ErrorResponse")

    # 15. ErrorResponse: 404 Not Found
    err_404_resp = client.get(f"/v1/jobs/{uuid.uuid4()}", headers=auth_headers)
    assert err_404_resp.status_code == 404
    validate_body(err_404_resp.json(), "ErrorResponse")

    # 16. ErrorResponse: 409 Conflict
    err_409_resp = client.post("/v1/workspaces", json={"name": "Dup WS", "slug": unique_slug}, headers=auth_headers)
    assert err_409_resp.status_code == 409
    validate_body(err_409_resp.json(), "ErrorResponse")

    # 17. ErrorResponse: 501 Not Implemented
    err_501_resp = client.get(f"/v1/operations/{job_id}/events", headers=auth_headers)
    assert err_501_resp.status_code == 501
    validate_body(err_501_resp.json(), "ErrorResponse")


def test_submit_job_authorizes_before_idempotency_replay(client: TestClient):
    """Assert submit_job authorizes workspace membership BEFORE checking idempotency records.

    A non-member attempting to replay a member's idempotency key must receive 404 (Workspace not found),
    byte-identical to a non-existent workspace, whether replayed with:
      - the identical request payload (preventing 202 replay leakage)
      - a different request payload (preventing 409 conflict leakage)
    """
    headers_alice = {"X-Dev-Subject": "alice-owner"}
    headers_bob = {"X-Dev-Subject": "bob-intruder"}

    # Alice creates Workspace 1
    ws1_resp = client.post(
        "/v1/workspaces",
        json={"name": "Alice WS", "slug": f"alice-idemp-{uuid.uuid4().hex[:6]}"},
        headers=headers_alice,
    )
    assert ws1_resp.status_code == 201
    ws1_id = ws1_resp.json()["id"]

    # Bob creates Workspace 2 (Bob is not a member of WS 1)
    ws2_resp = client.post(
        "/v1/workspaces",
        json={"name": "Bob WS", "slug": f"bob-idemp-{uuid.uuid4().hex[:6]}"},
        headers=headers_bob,
    )
    assert ws2_resp.status_code == 201

    # Alice submits a job with an idempotency key
    idemp_key = f"key-alice-{uuid.uuid4().hex[:8]}"
    alice_body = {
        "name": "data-pipeline",
        "image_digest": "registry.example.com/data@sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "command_args": ["run"],
    }
    alice_resp = client.post(
        f"/v1/workspaces/{ws1_id}/jobs",
        json=alice_body,
        headers={"Idempotency-Key": idemp_key, **headers_alice},
    )
    assert alice_resp.status_code == 202

    # Missing workspace reference for byte-identical 404 comparison
    missing_ws_id = str(uuid.uuid4())
    missing_resp = client.post(
        f"/v1/workspaces/{missing_ws_id}/jobs",
        json=alice_body,
        headers={"Idempotency-Key": idemp_key, **headers_alice},
    )
    assert missing_resp.status_code == 404
    missing_clean = {k: v for k, v in missing_resp.json().items() if k != "correlation_id"}

    # 1. Non-member (Bob) replays Alice's key with the SAME body -> must get 404, NOT 202
    replay_same_resp = client.post(
        f"/v1/workspaces/{ws1_id}/jobs",
        json=alice_body,
        headers={"Idempotency-Key": idemp_key, **headers_bob},
    )
    assert replay_same_resp.status_code == 404
    replay_same_clean = {k: v for k, v in replay_same_resp.json().items() if k != "correlation_id"}
    assert replay_same_clean == missing_clean, "Replay with same body leaked info (not byte-identical 404)"

    # 2. Non-member (Bob) replays Alice's key with a DIFFERENT body -> must get 404, NOT 409
    bob_diff_body = {
        "name": "malicious-takeover",
        "image_digest": "registry.example.com/exploit@sha256:abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdef",
        "command_args": ["rm", "-rf", "/"],
    }
    replay_diff_resp = client.post(
        f"/v1/workspaces/{ws1_id}/jobs",
        json=bob_diff_body,
        headers={"Idempotency-Key": idemp_key, **headers_bob},
    )
    assert replay_diff_resp.status_code == 404
    replay_diff_clean = {k: v for k, v in replay_diff_resp.json().items() if k != "correlation_id"}
    assert replay_diff_clean == missing_clean, "Replay with diff body leaked info (not byte-identical 404)"


def test_fail_closed_environment_defaults_and_rejects_dev_header_when_non_development(client: TestClient, monkeypatch):
    """Verify fail-closed behavior: unset, production, and staging all return 401 even with X-Dev-Subject."""
    from app.core.config import Settings, settings

    # 1. Verify default Settings instantiation with ENVIRONMENT unset in env fails closed to production
    with monkeypatch.context() as m:
        m.delenv("ENVIRONMENT", raising=False)
        fresh_settings = Settings()
        assert fresh_settings.ENVIRONMENT != "development", "Default ENVIRONMENT must fail closed!"
        assert fresh_settings.ENVIRONMENT == "production"

    # 2. Verify unset/empty, production, and staging all return 401 even with X-Dev-Subject
    payload = {"name": "Mallory WS", "slug": f"mallory-{uuid.uuid4().hex[:6]}"}
    dev_headers = {"X-Dev-Subject": "mallory"}

    for env_val in ["production", "staging", ""]:
        monkeypatch.setattr(settings, "ENVIRONMENT", env_val)
        resp = client.post("/v1/workspaces", json=payload, headers=dev_headers)
        assert resp.status_code == 401, f"Expected 401 when ENVIRONMENT='{env_val}', got {resp.status_code}"
        assert resp.headers.get("www-authenticate") == "Bearer"
        data = resp.json()
        assert data["error_code"] == "UNAUTHORIZED"
        assert data["message"] == "Authentication required"

