import os
import uuid
import psycopg2
import pytest
import yaml
from fastapi.testclient import TestClient

from app.models.job import JobState
from app.models.release import ReleaseStatus
from app.schemas.common import OperationKind, OperationStatus
from tests.conftest import TEST_DATABASE_URL_SYNC

pytestmark = pytest.mark.usefixtures("clean_db")


def test_job_state_validation():
    assert JobState.QUEUED.value == "QUEUED"
    assert JobState.CANCEL_REQUESTED.value == "CANCEL_REQUESTED"
    assert JobState.SUCCEEDED.value == "SUCCEEDED"
    assert JobState.RECOVERY_PENDING.value == "RECOVERY_PENDING"


def test_operation_status_enum_covers_all_job_and_release_states():
    """Verify OperationStatus covers every JobState and ReleaseStatus without omission."""
    op_statuses = {s.value for s in OperationStatus}

    job_states = {s.value for s in JobState}
    missing_job_states = job_states - op_statuses
    assert not missing_job_states, f"OperationStatus is missing JobState(s): {missing_job_states}"

    release_statuses = {s.value for s in ReleaseStatus}
    missing_release_statuses = release_statuses - op_statuses
    assert not missing_release_statuses, f"OperationStatus is missing ReleaseStatus(es): {missing_release_statuses}"


def test_openapi_operation_status_response_enum_covers_all_states():
    """Verify OpenAPI OperationStatusResponse status enum matches OperationStatus."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    openapi_path = os.path.join(repo_root, "contracts", "openapi", "v1.yaml")

    with open(openapi_path, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    openapi_statuses = set(
        spec["components"]["schemas"]["OperationStatusResponse"]["properties"]["status"]["enum"]
    )
    python_op_statuses = {s.value for s in OperationStatus}
    assert openapi_statuses == python_op_statuses, (
        f"Mismatch between OpenAPI OperationStatusResponse and Python OperationStatus:\n"
        f"Missing from OpenAPI: {python_op_statuses - openapi_statuses}\n"
        f"Extra in OpenAPI: {openapi_statuses - python_op_statuses}"
    )


def test_get_operation_status_for_job_in_recovery_pending(client: TestClient):
    """Verify GET /v1/operations/{id} succeeds (200 OK) when a job is in RECOVERY_PENDING state."""
    auth_headers = {"X-Dev-Subject": "op-tester"}
    unique_slug = f"ws-{uuid.uuid4().hex[:8]}"

    # Create workspace
    ws_resp = client.post(
        "/v1/workspaces",
        json={"name": "Op Status WS", "slug": unique_slug},
        headers=auth_headers,
    )
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    # Submit job
    submit_resp = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={"name": "test-job", "image_digest": "sha256:abcd1234efgh5678"},
        headers={**auth_headers, "Idempotency-Key": f"idemp-{uuid.uuid4().hex[:8]}"},
    )
    assert submit_resp.status_code == 202
    job_id = submit_resp.json()["operation_id"]

    # Manually transition job in DB to RECOVERY_PENDING (e.g. following crash recovery)
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET state = 'RECOVERY_PENDING' WHERE id = %s",
            (job_id,),
        )
    conn.commit()
    conn.close()

    # Query GET /v1/operations/{job_id}
    op_resp = client.get(f"/v1/operations/{job_id}", headers=auth_headers)
    assert op_resp.status_code == 200, op_resp.text
    data = op_resp.json()
    assert data["operation_id"] == job_id
    assert data["operation_kind"] == OperationKind.JOB.value
    assert data["status"] == "RECOVERY_PENDING"
