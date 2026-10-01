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

        # Verify simulator endpoint is removed (404 Not Found)
        # Invariant: No API path may write HEALTHY or current_release_id; only runtime may after real rollout
        proc_a = await client.post(
            f"/v1/releases/{rel_a_id}/process-build?succeed=true",
            headers={"X-Dev-Subject": "alice"},
        )
        assert proc_a.status_code == 404

        # Verify application current_release_id is NOT updated by webhook or API
        app_check_1 = (await client.get(f"/v1/apps/{app_id}", headers={"X-Dev-Subject": "alice"})).json()
        assert app_check_1["current_release_id"] is None

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

        # Trace both releases via API
        rels_list = (await client.get(f"/v1/apps/{app_id}/releases", headers={"X-Dev-Subject": "alice"})).json()
        items = rels_list["items"]
        assert len(items) == 2
        # Items are ordered by created_at desc (Release B first, then Release A)
        assert items[0]["id"] == rel_b_id
        assert items[0]["commit_sha"] == commit_b
        assert items[0]["release_number"] == 2
        assert items[0]["status"] == ReleaseStatus.REQUESTED.value
        assert items[1]["id"] == rel_a_id
        assert items[1]["commit_sha"] == commit_a
        assert items[1]["release_number"] == 1
        assert items[1]["status"] == ReleaseStatus.REQUESTED.value


@pytest.mark.anyio
async def test_m3_non_push_github_events_ignored():
    """Verifies M3 rule: Treat only X-GitHub-Event 'push' as a push event."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ws_slug = f"ws-evt-{uuid.uuid4().hex[:6]}"
        ws_resp = await client.post(
            "/v1/workspaces",
            json={"name": "Event WS", "slug": ws_slug},
            headers={"X-Dev-Subject": "alice"},
        )
        ws_id = ws_resp.json()["id"]

        repo_resp = await client.post(
            f"/v1/workspaces/{ws_id}/repositories",
            json={
                "name": "event-repo",
                "repo_url": "https://github.com/org/events.git",
                "webhook_secret": SECRET_KEY,
                "default_branch": "main",
            },
            headers={"X-Dev-Subject": "alice"},
        )
        repo_id = repo_resp.json()["id"]

        # Send pull_request event
        pr_payload = json.dumps({"action": "opened", "pull_request": {"number": 42}}).encode("utf-8")
        resp_pr = await client.post(
            f"/v1/webhooks/github/{repo_id}",
            content=pr_payload,
            headers={
                "X-Hub-Signature-256": create_signature(pr_payload),
                "X-GitHub-Delivery": "delivery-pr-1",
                "X-GitHub-Event": "pull_request",
                "Content-Type": "application/json",
            },
        )
        assert resp_pr.status_code == 200
        pr_data = resp_pr.json()
        assert pr_data["status"] == "IGNORED_NON_PUSH_EVENT"
        assert pr_data["event_type"] == "pull_request"
        assert pr_data.get("release_id") is None


@pytest.mark.anyio
async def test_m3_repository_url_validation_and_allowlist():
    """Verifies M3 requirement: Validate repo_url and enforce repository allowlist policy."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ws_slug = f"ws-val-{uuid.uuid4().hex[:6]}"
        ws_resp = await client.post(
            "/v1/workspaces",
            json={"name": "Val WS", "slug": ws_slug},
            headers={"X-Dev-Subject": "alice"},
        )
        ws_id = ws_resp.json()["id"]

        # Invalid protocol: ftp://
        bad_proto = await client.post(
            f"/v1/workspaces/{ws_id}/repositories",
            json={
                "name": "bad-proto",
                "repo_url": "ftp://github.com/org/repo.git",
                "webhook_secret": SECRET_KEY,
            },
            headers={"X-Dev-Subject": "alice"},
        )
        assert bad_proto.status_code == 422
        bad_proto_data = bad_proto.json()
        assert "Invalid repository URL" in (bad_proto_data.get("message") or bad_proto_data.get("detail") or "")

        # Unauthorized host: https://untrusted-attacker.com/repo.git
        bad_host = await client.post(
            f"/v1/workspaces/{ws_id}/repositories",
            json={
                "name": "bad-host",
                "repo_url": "https://untrusted-attacker.com/repo.git",
                "webhook_secret": SECRET_KEY,
            },
            headers={"X-Dev-Subject": "alice"},
        )
        assert bad_host.status_code == 422
        bad_host_data = bad_host.json()
        assert "REPOSITORY_POLICY_VIOLATION" in (bad_host_data.get("message") or bad_host_data.get("detail") or "")


@pytest.mark.anyio
async def test_m3_trigger_build_no_invented_commit_sha():
    """Verifies M3 requirement: Remove invented commit SHA (uuid4 hex) in trigger_build."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ws_slug = f"ws-trig-{uuid.uuid4().hex[:6]}"
        ws_resp = await client.post(
            "/v1/workspaces",
            json={"name": "Trigger WS", "slug": ws_slug},
            headers={"X-Dev-Subject": "alice"},
        )
        ws_id = ws_resp.json()["id"]

        repo_resp = await client.post(
            f"/v1/workspaces/{ws_id}/repositories",
            json={
                "name": "trigger-repo",
                "repo_url": "https://github.com/org/trigger.git",
                "webhook_secret": SECRET_KEY,
            },
            headers={"X-Dev-Subject": "alice"},
        )
        repo_id = repo_resp.json()["id"]

        app_resp = await client.post(
            f"/v1/workspaces/{ws_id}/apps",
            json={
                "name": "Trigger App",
                "slug": f"trig-app-{uuid.uuid4().hex[:6]}",
                "repository_id": repo_id,
            },
            headers={"X-Dev-Subject": "alice"},
        )
        app_id = app_resp.json()["id"]

        # Trigger build without commit_sha -> commit_sha must be None, NOT an invented uuid4 hex
        trig_none = await client.post(
            f"/v1/apps/{app_id}/builds",
            json={},
            headers={"X-Dev-Subject": "alice"},
        )
        assert trig_none.status_code == 202
        assert trig_none.json()["commit_sha"] is None

        # Trigger build with invalid commit_sha format
        trig_invalid = await client.post(
            f"/v1/apps/{app_id}/builds",
            json={"commit_sha": "not-valid-hex!"},
            headers={"X-Dev-Subject": "alice"},
        )
        assert trig_invalid.status_code == 422
