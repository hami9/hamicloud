import hashlib
import hmac
import json
import uuid
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.release import ReleaseStatus

SECRET_KEY = "super-secret-webhook-key-12345"


def create_signature(body: bytes, secret: str = SECRET_KEY) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


@pytest.mark.anyio
async def test_m3_connect_repository_and_link_to_app():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Create workspace
        ws_slug = f"ws-repo-{uuid.uuid4().hex[:6]}"
        ws_resp = await client.post(
            "/v1/workspaces",
            json={"name": "Repo Workspace", "slug": ws_slug},
            headers={"X-Dev-Subject": "alice"},
        )
        assert ws_resp.status_code == 201
        ws_id = ws_resp.json()["id"]

        # 2. Connect repository
        repo_resp = await client.post(
            f"/v1/workspaces/{ws_id}/repositories",
            json={
                "name": "sample-backend",
                "repo_url": "https://github.com/org/sample-backend.git",
                "webhook_secret": SECRET_KEY,
                "default_branch": "main",
            },
            headers={"X-Dev-Subject": "alice"},
        )
        assert repo_resp.status_code == 201
        repo_data = repo_resp.json()
        assert repo_data["name"] == "sample-backend"
        assert repo_data["default_branch"] == "main"
        repo_id = repo_data["id"]

        # 3. Create application linked to repository
        app_slug = f"app-repo-{uuid.uuid4().hex[:6]}"
        app_resp = await client.post(
            f"/v1/workspaces/{ws_id}/apps",
            json={
                "name": "Sample Service",
                "slug": app_slug,
                "repository_id": repo_id,
                "dockerfile_path": "Dockerfile.prod",
                "context_dir": ".",
                "git_branch": "main",
            },
            headers={"X-Dev-Subject": "alice"},
        )
        assert app_resp.status_code == 201
        app_data = app_resp.json()
        assert app_data["repository_id"] == repo_id
        assert app_data["dockerfile_path"] == "Dockerfile.prod"
        assert app_data["git_branch"] == "main"

        # 4. Inspect repository
        get_repo_resp = await client.get(
            f"/v1/repositories/{repo_id}",
            headers={"X-Dev-Subject": "alice"},
        )
        assert get_repo_resp.status_code == 200
        assert get_repo_resp.json()["id"] == repo_id


@pytest.mark.anyio
async def test_m3_signed_webhook_validation():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create workspace & repo
        ws_slug = f"ws-sec-{uuid.uuid4().hex[:6]}"
        ws_resp = await client.post(
            "/v1/workspaces",
            json={"name": "Security WS", "slug": ws_slug},
            headers={"X-Dev-Subject": "alice"},
        )
        ws_id = ws_resp.json()["id"]

        repo_resp = await client.post(
            f"/v1/workspaces/{ws_id}/repositories",
            json={
                "name": "security-repo",
                "repo_url": "https://github.com/org/security.git",
                "webhook_secret": SECRET_KEY,
                "default_branch": "main",
            },
            headers={"X-Dev-Subject": "alice"},
        )
        repo_id = repo_resp.json()["id"]

        payload_bytes = json.dumps({"ref": "refs/heads/main", "after": "1111222233334444555566667777888899990000"}).encode("utf-8")

        # Case 1: Missing signature header
        resp1 = await client.post(
            f"/v1/webhooks/github/{repo_id}",
            content=payload_bytes,
            headers={"X-GitHub-Delivery": "deliv-sec-1", "X-GitHub-Event": "push", "Content-Type": "application/json"},
        )
        assert resp1.status_code == 401

        # Case 2: Invalid signature (tampered body or wrong secret)
        resp2 = await client.post(
            f"/v1/webhooks/github/{repo_id}",
            content=payload_bytes,
            headers={
                "X-Hub-Signature-256": "sha256=0000000000000000000000000000000000000000000000000000000000000000",
                "X-GitHub-Delivery": "deliv-sec-2",
                "X-GitHub-Event": "push",
                "Content-Type": "application/json",
            },
        )
        assert resp2.status_code == 401

        # Case 3: Missing delivery header
        valid_sig = create_signature(payload_bytes, SECRET_KEY)
        resp3 = await client.post(
            f"/v1/webhooks/github/{repo_id}",
            content=payload_bytes,
            headers={
                "X-Hub-Signature-256": valid_sig,
                "X-GitHub-Event": "push",
                "Content-Type": "application/json",
            },
        )
        assert resp3.status_code == 400

        # Case 4: Valid signature and headers (ping)
        ping_bytes = json.dumps({"zen": "Keep it logically awesome."}).encode("utf-8")
        ping_sig = create_signature(ping_bytes, SECRET_KEY)
        resp4 = await client.post(
            f"/v1/webhooks/github/{repo_id}",
            content=ping_bytes,
            headers={
                "X-Hub-Signature-256": ping_sig,
                "X-GitHub-Delivery": "deliv-ping-1",
                "X-GitHub-Event": "ping",
                "Content-Type": "application/json",
            },
        )
        assert resp4.status_code == 200
        assert resp4.json()["status"] == "PING_ACKNOWLEDGED"


@pytest.mark.anyio
async def test_m3_duplicate_webhook_does_not_duplicate_build():
    """Verifies M3 criterion 3: A duplicate webhook does not duplicate a build."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create workspace, repo, and linked app
        ws_slug = f"ws-dup-{uuid.uuid4().hex[:6]}"
        ws_resp = await client.post(
            "/v1/workspaces",
            json={"name": "Dup WS", "slug": ws_slug},
            headers={"X-Dev-Subject": "alice"},
        )
        ws_id = ws_resp.json()["id"]

        repo_resp = await client.post(
            f"/v1/workspaces/{ws_id}/repositories",
            json={
                "name": "dup-repo",
                "repo_url": "https://github.com/org/dup.git",
                "webhook_secret": SECRET_KEY,
                "default_branch": "main",
            },
            headers={"X-Dev-Subject": "alice"},
        )
        repo_id = repo_resp.json()["id"]

        app_resp = await client.post(
            f"/v1/workspaces/{ws_id}/apps",
            json={
                "name": "Dup App",
                "slug": f"dup-app-{uuid.uuid4().hex[:6]}",
                "repository_id": repo_id,
                "git_branch": "main",
            },
            headers={"X-Dev-Subject": "alice"},
        )
        app_id = app_resp.json()["id"]

        payload_bytes = json.dumps({
            "ref": "refs/heads/main",
            "after": "aaaabbbbccccddddeeeeffff0000111122223333",
            "head_commit": {"id": "aaaabbbbccccddddeeeeffff0000111122223333", "message": "Initial commit"},
        }).encode("utf-8")
        sig = create_signature(payload_bytes, SECRET_KEY)
        delivery_id = f"delivery-fixed-{uuid.uuid4().hex[:8]}"

        # 1. First webhook delivery
        resp1 = await client.post(
            f"/v1/webhooks/github/{repo_id}",
            content=payload_bytes,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Delivery": delivery_id,
                "X-GitHub-Event": "push",
                "Content-Type": "application/json",
            },
        )
        assert resp1.status_code == 202
        data1 = resp1.json()
        assert data1["status"] == "PROCESSED"
        assert data1["release_id"] is not None

        # Check releases count is 1
        rels_resp1 = await client.get(
            f"/v1/apps/{app_id}/releases",
            headers={"X-Dev-Subject": "alice"},
        )
        assert len(rels_resp1.json()["items"]) == 1

        # 2. Second webhook delivery with the exact same X-GitHub-Delivery
        resp2 = await client.post(
            f"/v1/webhooks/github/{repo_id}",
            content=payload_bytes,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Delivery": delivery_id,
                "X-GitHub-Event": "push",
                "Content-Type": "application/json",
            },
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["status"] == "DUPLICATE"

        # Check releases count is STILL exactly 1
        rels_resp2 = await client.get(
            f"/v1/apps/{app_id}/releases",
            headers={"X-Dev-Subject": "alice"},
        )
        assert len(rels_resp2.json()["items"]) == 1


@pytest.mark.anyio
async def test_m3_two_commits_produce_traceable_releases():
    """Verifies M3 criterion 1: Two commits produce traceable releases."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ws_slug = f"ws-two-{uuid.uuid4().hex[:6]}"
        ws_resp = await client.post(
            "/v1/workspaces",
            json={"name": "Two Commits WS", "slug": ws_slug},
            headers={"X-Dev-Subject": "alice"},
        )
        ws_id = ws_resp.json()["id"]

        repo_resp = await client.post(
            f"/v1/workspaces/{ws_id}/repositories",
            json={
                "name": "two-commit-repo",
                "repo_url": "https://github.com/org/two-commit.git",
                "webhook_secret": SECRET_KEY,
                "default_branch": "main",
            },
            headers={"X-Dev-Subject": "alice"},
        )
        repo_id = repo_resp.json()["id"]

        app_slug = f"two-app-{uuid.uuid4().hex[:6]}"
        app_resp = await client.post(
            f"/v1/workspaces/{ws_id}/apps",
            json={
                "name": "Two App",
                "slug": app_slug,
                "repository_id": repo_id,
                "git_branch": "main",
            },
            headers={"X-Dev-Subject": "alice"},
        )
        app_id = app_resp.json()["id"]

        # Commit A
        commit_a = "1111111111111111111111111111111111111111"
        payload_a = json.dumps({
            "ref": "refs/heads/main",
            "after": commit_a,
            "head_commit": {"id": commit_a, "message": "feat: add user endpoint"},
        }).encode("utf-8")
        resp_a = await client.post(
            f"/v1/webhooks/github/{repo_id}",
            content=payload_a,
            headers={
                "X-Hub-Signature-256": create_signature(payload_a),
                "X-GitHub-Delivery": "delivery-commit-a",
                "X-GitHub-Event": "push",
                "Content-Type": "application/json",
            },
        )
        assert resp_a.status_code == 202
        rel_a_id = resp_a.json()["release_id"]

        # Process build A to healthy
        proc_a = await client.post(
            f"/v1/releases/{rel_a_id}/process-build?succeed=true",
            headers={"X-Dev-Subject": "alice"},
        )
        assert proc_a.status_code == 200
        rel_a_data = proc_a.json()
        assert rel_a_data["status"] == ReleaseStatus.HEALTHY.value
        assert rel_a_data["commit_sha"] == commit_a
        assert "registry.hamicloud.local" in rel_a_data["image_digest"]

        # Verify application now serves Release A
        app_check_1 = (await client.get(f"/v1/apps/{app_id}", headers={"X-Dev-Subject": "alice"})).json()
        assert app_check_1["current_release_id"] == rel_a_id

        # Commit B
        commit_b = "2222222222222222222222222222222222222222"
        payload_b = json.dumps({
            "ref": "refs/heads/main",
            "after": commit_b,
            "head_commit": {"id": commit_b, "message": "feat: optimize sql queries"},
        }).encode("utf-8")
        resp_b = await client.post(
            f"/v1/webhooks/github/{repo_id}",
            content=payload_b,
            headers={
                "X-Hub-Signature-256": create_signature(payload_b),
                "X-GitHub-Delivery": "delivery-commit-b",
                "X-GitHub-Event": "push",
                "Content-Type": "application/json",
            },
        )
        assert resp_b.status_code == 202
        rel_b_id = resp_b.json()["release_id"]

        # Process build B to healthy
        proc_b = await client.post(
            f"/v1/releases/{rel_b_id}/process-build?succeed=true",
            headers={"X-Dev-Subject": "alice"},
        )
        assert proc_b.status_code == 200
        rel_b_data = proc_b.json()
        assert rel_b_data["status"] == ReleaseStatus.HEALTHY.value
        assert rel_b_data["commit_sha"] == commit_b

        # Verify application now serves Release B
        app_check_2 = (await client.get(f"/v1/apps/{app_id}", headers={"X-Dev-Subject": "alice"})).json()
        assert app_check_2["current_release_id"] == rel_b_id

        # Trace both releases via API
        rels_list = (await client.get(f"/v1/apps/{app_id}/releases", headers={"X-Dev-Subject": "alice"})).json()
        items = rels_list["items"]
        assert len(items) == 2
        # Items are ordered by created_at desc (Release B first, then Release A)
        assert items[0]["commit_sha"] == commit_b
        assert items[0]["release_number"] == 2
        assert items[1]["commit_sha"] == commit_a
        assert items[1]["release_number"] == 1


@pytest.mark.anyio
async def test_m3_failed_build_preserves_currently_serving_application():
    """Verifies M3 criterion 2: A failed build preserves the currently serving application."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ws_slug = f"ws-fail-{uuid.uuid4().hex[:6]}"
        ws_resp = await client.post(
            "/v1/workspaces",
            json={"name": "Fail Test WS", "slug": ws_slug},
            headers={"X-Dev-Subject": "alice"},
        )
        ws_id = ws_resp.json()["id"]

        repo_resp = await client.post(
            f"/v1/workspaces/{ws_id}/repositories",
            json={
                "name": "fail-repo",
                "repo_url": "https://github.com/org/fail.git",
                "webhook_secret": SECRET_KEY,
                "default_branch": "main",
            },
            headers={"X-Dev-Subject": "alice"},
        )
        repo_id = repo_resp.json()["id"]

        app_slug = f"fail-app-{uuid.uuid4().hex[:6]}"
        app_resp = await client.post(
            f"/v1/workspaces/{ws_id}/apps",
            json={
                "name": "Fail App",
                "slug": app_slug,
                "repository_id": repo_id,
                "git_branch": "main",
            },
            headers={"X-Dev-Subject": "alice"},
        )
        app_id = app_resp.json()["id"]

        # Step 1: Deploy a healthy initial release
        commit_good = "abcdef0123456789abcdef0123456789abcdef01"
        payload_good = json.dumps({
            "ref": "refs/heads/main",
            "after": commit_good,
            "head_commit": {"id": commit_good, "message": "Initial working release"},
        }).encode("utf-8")
        resp_good = await client.post(
            f"/v1/webhooks/github/{repo_id}",
            content=payload_good,
            headers={
                "X-Hub-Signature-256": create_signature(payload_good),
                "X-GitHub-Delivery": "delivery-good-1",
                "X-GitHub-Event": "push",
                "Content-Type": "application/json",
            },
        )
        assert resp_good.status_code == 202
        rel_good_id = resp_good.json()["release_id"]

        # Build healthy
        proc_good = await client.post(
            f"/v1/releases/{rel_good_id}/process-build?succeed=true",
            headers={"X-Dev-Subject": "alice"},
        )
        assert proc_good.status_code == 200
        assert proc_good.json()["status"] == ReleaseStatus.HEALTHY.value

        # Verify app serves rel_good_id
        app_serving_initial = (await client.get(f"/v1/apps/{app_id}", headers={"X-Dev-Subject": "alice"})).json()
        assert app_serving_initial["current_release_id"] == rel_good_id

        # Step 2: Trigger a failing build with a bad commit
        commit_bad = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        payload_bad = json.dumps({
            "ref": "refs/heads/main",
            "after": commit_bad,
            "head_commit": {"id": commit_bad, "message": "broken: invalid dockerfile"},
        }).encode("utf-8")
        resp_bad = await client.post(
            f"/v1/webhooks/github/{repo_id}",
            content=payload_bad,
            headers={
                "X-Hub-Signature-256": create_signature(payload_bad),
                "X-GitHub-Delivery": "delivery-bad-2",
                "X-GitHub-Event": "push",
                "Content-Type": "application/json",
            },
        )
        assert resp_bad.status_code == 202
        rel_bad_id = resp_bad.json()["release_id"]

        # Process build with FAILURE
        error_msg = "BuildKit parse error: unknown instruction 'FOOBAR' in Dockerfile line 4"
        proc_bad = await client.post(
            f"/v1/releases/{rel_bad_id}/process-build?succeed=false&failure_reason={error_msg}",
            headers={"X-Dev-Subject": "alice"},
        )
        assert proc_bad.status_code == 200
        bad_data = proc_bad.json()
        assert bad_data["status"] == ReleaseStatus.BUILD_FAILED.value
        assert bad_data["status_reason"] == error_msg
        assert "unknown instruction 'FOOBAR'" in bad_data["build_logs"]

        # CRITICAL ASSERTION: The application MUST still serve the healthy release!
        app_serving_after_failure = (await client.get(f"/v1/apps/{app_id}", headers={"X-Dev-Subject": "alice"})).json()
        assert app_serving_after_failure["current_release_id"] == rel_good_id
        assert app_serving_after_failure["current_release_id"] != rel_bad_id
