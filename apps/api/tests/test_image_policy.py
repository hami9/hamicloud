import uuid
import pytest
from fastapi.testclient import TestClient

from app.core.image_policy import validate_image_policy
from app.schemas.common import ErrorCode

pytestmark = pytest.mark.usefixtures("clean_db")


def test_validate_image_policy_direct_unit():
    # 1. Allowed prefixes pass
    validate_image_policy("ghcr.io/hami9/worker:v1")
    validate_image_policy("registry.example.com/app@sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")
    validate_image_policy("quay.io/minio/minio:latest")
    validate_image_policy("sha256:abcdef1234567890")

    # 2. Unapproved image raises 422 with IMAGE_POLICY_VIOLATION
    with pytest.raises(Exception) as exc_info:
        validate_image_policy("untrusted-registry.io/crypto-miner@sha256:9999")
    
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error_code"] == ErrorCode.IMAGE_POLICY_VIOLATION.value


def test_deploy_release_rejects_unapproved_image(client: TestClient):
    auth_headers = {"X-Dev-Subject": "policy-dev"}
    unique_slug = f"ws-{uuid.uuid4().hex[:8]}"

    # Setup workspace & app
    ws_resp = client.post("/v1/workspaces", json={"name": "Policy WS", "slug": unique_slug}, headers=auth_headers)
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    app_resp = client.post(
        f"/v1/workspaces/{ws_id}/apps",
        json={"name": "Policy App", "slug": "policy-app", "workload_type": "HTTP_SERVICE"},
        headers=auth_headers,
    )
    assert app_resp.status_code == 201
    app_id = app_resp.json()["id"]

    # Attempt to deploy unapproved image
    unapproved_image = "malicious.io/payload@sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
    deploy_resp = client.post(
        f"/v1/apps/{app_id}/deployments",
        json={"image_digest": unapproved_image, "port": 8080},
        headers={**auth_headers, "Idempotency-Key": f"idemp-{uuid.uuid4().hex[:8]}"},
    )
    assert deploy_resp.status_code == 422, deploy_resp.text
    body = deploy_resp.json()
    assert body["error_code"] == "IMAGE_POLICY_VIOLATION"
    assert "violates platform image policy" in body["message"]

    # Deploy approved image succeeds
    approved_image = "registry.example.com/valid-app@sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
    valid_deploy = client.post(
        f"/v1/apps/{app_id}/deployments",
        json={"image_digest": approved_image, "port": 8080},
        headers={**auth_headers, "Idempotency-Key": f"idemp-{uuid.uuid4().hex[:8]}"},
    )
    assert valid_deploy.status_code == 202
    assert valid_deploy.json()["status"] == "ACCEPTED"


def test_submit_job_rejects_unapproved_image(client: TestClient):
    auth_headers = {"X-Dev-Subject": "job-policy-dev"}
    unique_slug = f"ws-{uuid.uuid4().hex[:8]}"

    ws_resp = client.post("/v1/workspaces", json={"name": "Job Policy WS", "slug": unique_slug}, headers=auth_headers)
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    # Attempt to submit job with unapproved image
    unapproved_image = "evil.registry.org/crypto-job@sha256:5555"
    submit_resp = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={"name": "unapproved-job", "image_digest": unapproved_image},
        headers={**auth_headers, "Idempotency-Key": f"idemp-{uuid.uuid4().hex[:8]}"},
    )
    assert submit_resp.status_code == 422, submit_resp.text
    body = submit_resp.json()
    assert body["error_code"] == "IMAGE_POLICY_VIOLATION"

    # Submit job with approved image
    approved_image = "ghcr.io/hami9/batch-worker@sha256:7777"
    valid_submit = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={"name": "approved-job", "image_digest": approved_image},
        headers={**auth_headers, "Idempotency-Key": f"idemp-{uuid.uuid4().hex[:8]}"},
    )
    assert valid_submit.status_code == 202
    assert valid_submit.json()["status"] == "ACCEPTED"
