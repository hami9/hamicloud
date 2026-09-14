import uuid
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("clean_db")


def test_workspace_creation_and_duplicate_slug(client: TestClient):
    unique_slug = f"ws-{uuid.uuid4().hex[:8]}"
    payload = {"name": "Test Workspace", "slug": unique_slug}

    # 1. Create workspace
    resp = client.post("/v1/workspaces", json=payload)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["slug"] == unique_slug
    assert "id" in data
    ws_id = data["id"]

    # 2. Duplicate slug returns 409 Conflict
    dup_resp = client.post("/v1/workspaces", json=payload)
    assert dup_resp.status_code == 409
    assert dup_resp.json()["error_code"] == "CONFLICT"


def test_application_and_deployment_flow(client: TestClient):
    unique_slug = f"ws-{uuid.uuid4().hex[:8]}"
    ws_resp = client.post("/v1/workspaces", json={"name": "App WS", "slug": unique_slug})
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    # 1. Create Application
    app_slug = f"svc-{uuid.uuid4().hex[:6]}"
    app_resp = client.post(
        f"/v1/workspaces/{ws_id}/apps",
        json={"name": "My Web Service", "slug": app_slug, "workload_type": "HTTP_SERVICE"},
    )
    assert app_resp.status_code == 201, app_resp.text
    app_data = app_resp.json()
    app_id = app_data["id"]
    assert app_data["desired_generation"] == 1

    # 2. Deploy Release with Idempotency-Key
    idemp_key = f"idemp-deploy-{uuid.uuid4().hex[:8]}"
    deploy_payload = {
        "image_digest": "registry.example.com/web@sha256:7f83b1657ff1fc53b92dc18148a1d65dfc2d4b1fa3d677284addd200126d9069",
        "port": 8080,
        "health_path": "/healthz",
    }
    deploy_resp = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=deploy_payload,
        headers={"Idempotency-Key": idemp_key, "X-Workspace-ID": ws_id},
    )
    assert deploy_resp.status_code == 202, deploy_resp.text
    deploy_data = deploy_resp.json()
    assert deploy_data["status"] == "ACCEPTED"
    release_id = deploy_data["operation_id"]

    # 3. Retransmit with exact same Idempotency-Key -> returns cached 202
    dup_deploy_resp = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=deploy_payload,
        headers={"Idempotency-Key": idemp_key, "X-Workspace-ID": ws_id},
    )
    assert dup_deploy_resp.status_code == 202
    assert dup_deploy_resp.json()["operation_id"] == release_id

    # 4. Retransmit with same key but different payload -> 409 Conflict
    diff_payload = dict(deploy_payload, port=9090)
    conflict_resp = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=diff_payload,
        headers={"Idempotency-Key": idemp_key, "X-Workspace-ID": ws_id},
    )
    assert conflict_resp.status_code == 409

    # 5. Rollback to target release
    rollback_resp = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json={"target_release_id": release_id},
        headers={"X-Workspace-ID": ws_id},
    )
    assert rollback_resp.status_code == 202, rollback_resp.text
    assert rollback_resp.json()["status"] == "ACCEPTED"


def test_job_flow_with_isolation_and_lifecycle(client: TestClient):
    unique_slug = f"ws-{uuid.uuid4().hex[:8]}"
    ws_resp = client.post("/v1/workspaces", json={"name": "Job WS", "slug": unique_slug})
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
        headers={"Idempotency-Key": idemp_key},
    )
    assert job_resp.status_code == 202, job_resp.text
    job_id = job_resp.json()["operation_id"]

    # 2. Get Job details
    get_resp = client.get(f"/v1/jobs/{job_id}", headers={"X-Workspace-ID": ws_id})
    assert get_resp.status_code == 200, get_resp.text
    job_details = get_resp.json()
    assert job_details["id"] == job_id
    assert job_details["state"] == "QUEUED"
    assert job_details["workspace_id"] == ws_id

    # 3. Tenant isolation: querying with foreign workspace returns 404
    foreign_ws = str(uuid.uuid4())
    isolated_resp = client.get(f"/v1/jobs/{job_id}", headers={"X-Workspace-ID": foreign_ws})
    assert isolated_resp.status_code == 404

    # 4. Cancel Job
    cancel_resp = client.post(f"/v1/jobs/{job_id}/cancel", headers={"X-Workspace-ID": ws_id})
    assert cancel_resp.status_code == 202, cancel_resp.text

    # Verify state updated to CANCEL_REQUESTED
    check_resp = client.get(f"/v1/jobs/{job_id}", headers={"X-Workspace-ID": ws_id})
    assert check_resp.json()["state"] == "CANCEL_REQUESTED"


def test_operations_status_and_stream(client: TestClient):
    unique_slug = f"ws-{uuid.uuid4().hex[:8]}"
    ws_resp = client.post("/v1/workspaces", json={"name": "Ops WS", "slug": unique_slug})
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    # Submit job to get operation ID
    job_resp = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={
            "name": "stream-job",
            "image_digest": "registry.example.com/img@sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        },
        headers={"Idempotency-Key": f"idemp-{uuid.uuid4().hex[:8]}"},
    )
    assert job_resp.status_code == 202
    op_id = job_resp.json()["operation_id"]

    # 1. Get Operation status
    status_resp = client.get(f"/v1/operations/{op_id}", headers={"X-Workspace-ID": ws_id})
    assert status_resp.status_code == 200, status_resp.text
    resp_data = status_resp.json()
    assert resp_data["operation_id"] == op_id
    assert resp_data["operation_kind"] == "JOB"
    assert resp_data["status"] == "QUEUED"
    assert resp_data["status_url"] == f"/v1/operations/{op_id}"

    # 2. Get Operation status with wrong workspace returns 404
    wrong_ws = str(uuid.uuid4())
    bad_resp = client.get(f"/v1/operations/{op_id}", headers={"X-Workspace-ID": wrong_ws})
    assert bad_resp.status_code == 404

    # 3. Stream operation events returns 501 Not Implemented in M0
    stream_resp = client.get(f"/v1/operations/{op_id}/events", headers={"X-Workspace-ID": ws_id})
    assert stream_resp.status_code == 501
    err = stream_resp.json()
    assert err["error_code"] == "NOT_IMPLEMENTED"
    assert "correlation_id" in err
    assert stream_resp.headers.get("X-Correlation-ID") == err["correlation_id"]


def test_all_202_and_operation_status_responses_validate_against_openapi_schemas(client: TestClient):
    """Validate live responses from every 202 endpoint and from GET /v1/operations/{id} against OpenAPI component schemas."""
    import yaml
    import jsonschema
    import psycopg2
    from tests.conftest import OPENAPI_SPEC, TEST_DATABASE_URL_SYNC

    with open(OPENAPI_SPEC, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    schemas = spec["components"]["schemas"]
    accepted_schema = schemas["AcceptedOperationResponse"]
    op_status_schema = schemas["OperationStatusResponse"]

    # Create workspace
    unique_slug = f"ws-{uuid.uuid4().hex[:8]}"
    ws_resp = client.post("/v1/workspaces", json={"name": "Contracts WS", "slug": unique_slug})
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    # 1. POST /v1/workspaces/{ws}/jobs -> 202 AcceptedOperationResponse
    job_payload = {
        "name": "contract-job",
        "image_digest": "registry.example.com/job@sha256:1111111111111111111111111111111111111111111111111111111111111111",
    }
    job_resp = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json=job_payload,
        headers={"Idempotency-Key": f"idemp-job-{uuid.uuid4().hex[:8]}"},
    )
    assert job_resp.status_code == 202
    job_data = job_resp.json()
    jsonschema.validate(job_data, accepted_schema)
    job_id = job_data["operation_id"]

    # 2. POST /v1/jobs/{job}/cancel -> 202 AcceptedOperationResponse
    cancel_resp = client.post(f"/v1/jobs/{job_id}/cancel", headers={"X-Workspace-ID": ws_id})
    assert cancel_resp.status_code == 202
    jsonschema.validate(cancel_resp.json(), accepted_schema)

    # 3. POST /v1/jobs/{job}/reruns -> 202 AcceptedOperationResponse
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'FAILED' WHERE id = %s", (job_id,))
    conn.commit()
    conn.close()

    rerun_resp = client.post(f"/v1/jobs/{job_id}/reruns", headers={"X-Workspace-ID": ws_id})
    assert rerun_resp.status_code == 202
    jsonschema.validate(rerun_resp.json(), accepted_schema)

    # 4. POST /v1/apps/{app}/deployments -> 202 AcceptedOperationResponse
    app_slug = f"svc-{uuid.uuid4().hex[:6]}"
    app_resp = client.post(
        f"/v1/workspaces/{ws_id}/apps",
        json={"name": "Contract App", "slug": app_slug, "workload_type": "HTTP_SERVICE"},
    )
    assert app_resp.status_code == 201
    app_id = app_resp.json()["id"]

    deploy_payload = {
        "image_digest": "registry.example.com/app@sha256:2222222222222222222222222222222222222222222222222222222222222222",
        "port": 8080,
    }
    deploy_resp = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=deploy_payload,
        headers={"Idempotency-Key": f"idemp-deploy-{uuid.uuid4().hex[:8]}", "X-Workspace-ID": ws_id},
    )
    assert deploy_resp.status_code == 202
    deploy_data = deploy_resp.json()
    jsonschema.validate(deploy_data, accepted_schema)
    release_id = deploy_data["operation_id"]

    # 5. POST /v1/apps/{app}/rollbacks -> 202 AcceptedOperationResponse
    deploy2_resp = client.post(
        f"/v1/apps/{app_id}/deployments",
        json=dict(deploy_payload, port=8081),
        headers={"Idempotency-Key": f"idemp-deploy2-{uuid.uuid4().hex[:8]}", "X-Workspace-ID": ws_id},
    )
    assert deploy2_resp.status_code == 202

    rollback_resp = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json={"target_release_id": release_id},
        headers={"X-Workspace-ID": ws_id},
    )
    assert rollback_resp.status_code == 202
    jsonschema.validate(rollback_resp.json(), accepted_schema)

    # 6. GET /v1/operations/{id} for Job -> 200 OperationStatusResponse
    job_op_resp = client.get(f"/v1/operations/{job_id}", headers={"X-Workspace-ID": ws_id})
    assert job_op_resp.status_code == 200
    job_op_data = job_op_resp.json()
    assert job_op_data["operation_kind"] == "JOB"
    assert job_op_data["details"] is None
    jsonschema.validate(job_op_data, op_status_schema)

    # 7. GET /v1/operations/{id} for Release -> 200 OperationStatusResponse
    rel_op_resp = client.get(f"/v1/operations/{release_id}", headers={"X-Workspace-ID": ws_id})
    assert rel_op_resp.status_code == 200
    rel_op_data = rel_op_resp.json()
    assert rel_op_data["operation_kind"] == "RELEASE"
    assert rel_op_data["details"] is None
    jsonschema.validate(rel_op_data, op_status_schema)

