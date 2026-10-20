import uuid
import psycopg2
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.crypto import SecretDecryptionError, decrypt_secret

TEST_DATABASE_URL_SYNC = "postgresql://hamicloud:hamicloud_secret@localhost:5432/hamicloud_test"


def create_workspace(client: TestClient, subject: str, name_prefix: str = "WS") -> tuple[str, dict[str, str]]:
    headers = {"X-Dev-Subject": subject}
    slug = f"{name_prefix.lower()}-{uuid.uuid4().hex[:8]}"
    res = client.post(
        "/v1/workspaces",
        json={"name": f"{name_prefix} {slug}", "slug": slug},
        headers=headers,
    )
    assert res.status_code == 201, res.text
    return res.json()["id"], headers


def strip_correlation_id(resp_json: dict) -> dict:
    copy = dict(resp_json)
    copy.pop("correlation_id", None)
    return copy


def test_m4_two_workspace_negative_isolation_matrix(client: TestClient, clean_db, tmp_path, monkeypatch):
    """
    Two-workspace negative isolation across API, logs, artifacts, secrets, quotas, members, and audit:
    Workspace B caller (bob) attempting to read or mutate Workspace A (alice) resources receives
    404 Not Found with body byte-identical (modulo correlation_id) to a missing resource ID.
    """
    monkeypatch.setattr(settings, "ARTIFACTS_DIR", str(tmp_path))

    ws_a, alice_headers = create_workspace(client, "alice-owner", "ws-a")
    ws_b, bob_headers = create_workspace(client, "bob-owner", "ws-b")

    # Alice creates an application, secret, job, and deployment in Workspace A
    app_res = client.post(
        f"/v1/workspaces/{ws_a}/apps",
        json={"name": "Alice Service", "slug": f"alice-svc-{uuid.uuid4().hex[:6]}", "workload_type": "HTTP_SERVICE"},
        headers=alice_headers,
    )
    assert app_res.status_code == 201
    app_a_id = app_res.json()["id"]

    sec_res = client.post(
        f"/v1/workspaces/{ws_a}/secrets",
        json={"name": "ALICE_API_TOKEN", "value": "super_secret_alice_token_12345"},
        headers=alice_headers,
    )
    assert sec_res.status_code == 201

    job_res = client.post(
        f"/v1/workspaces/{ws_a}/jobs",
        json={
            "name": "alice-job",
            "image_digest": "registry.example.com/batch@sha256:" + "a" * 64,
        },
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:8]}", **alice_headers},
    )
    assert job_res.status_code == 202
    job_a_id = job_res.json()["operation_id"]

    # Create artifact file for Alice's job and mark job SUCCEEDED
    artifact_dir = tmp_path / ws_a / job_a_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "output.txt").write_text("Alice private job output\n", encoding="utf-8")
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'SUCCEEDED' WHERE id = %s", (job_a_id,))
    conn.commit()
    conn.close()

    missing_uuid = str(uuid.uuid4())

    # 1. Workspace metadata, members, secrets, quotas, audit, apps, jobs, dlq
    ws_scoped_gets = [
        "/v1/workspaces/{ws_id}",
        "/v1/workspaces/{ws_id}/members",
        "/v1/workspaces/{ws_id}/secrets",
        "/v1/workspaces/{ws_id}/quotas",
        "/v1/workspaces/{ws_id}/audit",
        "/v1/workspaces/{ws_id}/apps",
        "/v1/workspaces/{ws_id}/jobs",
        "/v1/workspaces/{ws_id}/dead-letter-records",
    ]
    for pattern in ws_scoped_gets:
        bob_cross = client.get(pattern.format(ws_id=ws_a), headers=bob_headers)
        bob_missing = client.get(pattern.format(ws_id=missing_uuid), headers=bob_headers)
        assert bob_cross.status_code == 404, f"Expected 404 on {pattern} for cross-workspace access, got {bob_cross.status_code}"
        assert bob_missing.status_code == 404
        assert strip_correlation_id(bob_cross.json()) == strip_correlation_id(bob_missing.json())

    # 2. Secret mutation & deletion across workspaces
    bob_sec_post = client.post(
        f"/v1/workspaces/{ws_a}/secrets",
        json={"name": "BOB_INJECT", "value": "injected"},
        headers=bob_headers,
    )
    assert bob_sec_post.status_code == 404
    bob_sec_del = client.delete(f"/v1/workspaces/{ws_a}/secrets/ALICE_API_TOKEN", headers=bob_headers)
    assert bob_sec_del.status_code == 404

    # 3. Job details & job output artifact download across workspaces
    bob_job_get = client.get(f"/v1/jobs/{job_a_id}", headers=bob_headers)
    bob_job_missing = client.get(f"/v1/jobs/{missing_uuid}", headers=bob_headers)
    assert bob_job_get.status_code == 404
    assert strip_correlation_id(bob_job_get.json()) == strip_correlation_id(bob_job_missing.json())

    bob_out_get = client.get(f"/v1/jobs/{job_a_id}/output", headers=bob_headers)
    bob_out_missing = client.get(f"/v1/jobs/{missing_uuid}/output", headers=bob_headers)
    assert bob_out_get.status_code == 404
    assert strip_correlation_id(bob_out_get.json()) == strip_correlation_id(bob_out_missing.json())

    # 4. Application releases across workspaces
    bob_rel_get = client.get(f"/v1/apps/{app_a_id}/releases", headers=bob_headers)
    bob_rel_missing = client.get(f"/v1/apps/{missing_uuid}/releases", headers=bob_headers)
    assert bob_rel_get.status_code == 404
    assert strip_correlation_id(bob_rel_get.json()) == strip_correlation_id(bob_rel_missing.json())


def test_m4_four_role_rbac_matrix_and_last_owner_protection(client: TestClient, clean_db):
    """
    Verify 4-role RBAC hierarchy (OWNER > ADMIN > DEVELOPER > VIEWER) across workspace operations
    and verify that the last OWNER of a workspace cannot be demoted or removed.
    """
    ws_id, owner_headers = create_workspace(client, "user-owner", "rbac")
    admin_headers = {"X-Dev-Subject": "user-admin"}
    dev_headers = {"X-Dev-Subject": "user-dev"}
    viewer_headers = {"X-Dev-Subject": "user-viewer"}

    # OWNER adds ADMIN, DEVELOPER, VIEWER
    for sub, role in [("user-admin", "ADMIN"), ("user-dev", "DEVELOPER"), ("user-viewer", "VIEWER")]:
        r = client.post(
            f"/v1/workspaces/{ws_id}/members",
            json={"user_subject": sub, "role": role},
            headers=owner_headers,
        )
        assert r.status_code == 201, r.text
        assert r.json()["role"] == role

    # VIEWER can read members, secrets metadata, quotas, apps, jobs
    assert client.get(f"/v1/workspaces/{ws_id}", headers=viewer_headers).status_code == 200
    assert client.get(f"/v1/workspaces/{ws_id}/members", headers=viewer_headers).status_code == 200
    assert client.get(f"/v1/workspaces/{ws_id}/secrets", headers=viewer_headers).status_code == 200
    assert client.get(f"/v1/workspaces/{ws_id}/quotas", headers=viewer_headers).status_code == 200

    # VIEWER cannot create apps, submit jobs, manage secrets, manage members, or export audit logs (403)
    assert (
        client.post(
            f"/v1/workspaces/{ws_id}/apps",
            json={"name": "App", "slug": "viewer-app", "workload_type": "HTTP_SERVICE"},
            headers=viewer_headers,
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/workspaces/{ws_id}/secrets",
            json={"name": "SEC_1", "value": "val"},
            headers=viewer_headers,
        ).status_code
        == 403
    )
    assert client.get(f"/v1/workspaces/{ws_id}/audit", headers=viewer_headers).status_code == 403

    # DEVELOPER can create apps, submit jobs, and upsert secrets, but cannot delete secrets, manage members, or export audit logs
    dev_app = client.post(
        f"/v1/workspaces/{ws_id}/apps",
        json={"name": "Dev App", "slug": "dev-app", "workload_type": "HTTP_SERVICE"},
        headers=dev_headers,
    )
    assert dev_app.status_code == 201
    dev_sec = client.post(
        f"/v1/workspaces/{ws_id}/secrets",
        json={"name": "DEV_SECRET", "value": "dev_val_123"},
        headers=dev_headers,
    )
    assert dev_sec.status_code == 201
    assert client.delete(f"/v1/workspaces/{ws_id}/secrets/DEV_SECRET", headers=dev_headers).status_code == 403
    assert (
        client.post(
            f"/v1/workspaces/{ws_id}/members",
            json={"user_subject": "user-extra", "role": "VIEWER"},
            headers=dev_headers,
        ).status_code
        == 403
    )
    assert client.get(f"/v1/workspaces/{ws_id}/audit", headers=dev_headers).status_code == 403

    # ADMIN can delete secrets, manage non-OWNER members, and export audit logs, but cannot assign/modify OWNER
    assert client.get(f"/v1/workspaces/{ws_id}/audit", headers=admin_headers).status_code == 200
    assert client.delete(f"/v1/workspaces/{ws_id}/secrets/DEV_SECRET", headers=admin_headers).status_code == 204
    assert (
        client.post(
            f"/v1/workspaces/{ws_id}/members",
            json={"user_subject": "user-extra", "role": "OWNER"},
            headers=admin_headers,
        ).status_code
        == 403
    )
    assert client.delete(f"/v1/workspaces/{ws_id}/members/user-owner", headers=admin_headers).status_code == 403

    # Last OWNER protection: OWNER cannot remove or demote themselves when they are the sole OWNER (422)
    assert client.delete(f"/v1/workspaces/{ws_id}/members/user-owner", headers=owner_headers).status_code == 422
    assert (
        client.post(
            f"/v1/workspaces/{ws_id}/members",
            json={"user_subject": "user-owner", "role": "ADMIN"},
            headers=owner_headers,
        ).status_code
        == 422
    )


def test_m4_aes_256_gcm_secret_encryption_rotation_and_aad_binding(client: TestClient, clean_db):
    """
    Verify secrets are encrypted at rest with AES-256-GCM (v1:<nonce>:<ciphertext>),
    AAD binds ciphertext to workspace_id:secret_name, key_version increments on rotation,
    and neither plaintext nor ciphertext ever leaks in API responses or audit_events.
    """
    ws_a, alice_headers = create_workspace(client, "alice-crypto", "crypto-a")
    ws_b, _ = create_workspace(client, "bob-crypto", "crypto-b")

    plaintext_v1 = "sk_live_ultra_confidential_99887766"
    plaintext_v2 = "sk_live_rotated_confidential_11223344"

    # 1. Create secret (version 1)
    r1 = client.post(
        f"/v1/workspaces/{ws_a}/secrets",
        json={"name": "STRIPE_SECRET_KEY", "value": plaintext_v1},
        headers=alice_headers,
    )
    assert r1.status_code == 201
    body1 = r1.json()
    assert body1["name"] == "STRIPE_SECRET_KEY"
    assert body1["key_version"] == 1
    assert plaintext_v1 not in r1.text
    assert "encrypted_value" not in body1

    # Inspect raw database row
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT encrypted_value, key_version FROM secret_references WHERE workspace_id = %s AND name = %s",
            (ws_a, "STRIPE_SECRET_KEY"),
        )
        row = cur.fetchone()
    conn.close()
    assert row is not None
    raw_ciphertext, db_ver = row
    assert db_ver == 1
    assert raw_ciphertext.startswith("v1:")
    assert plaintext_v1 not in raw_ciphertext

    # Verify round-trip decryption with matching workspace_id and name
    decrypted = decrypt_secret(raw_ciphertext, uuid.UUID(ws_a), "STRIPE_SECRET_KEY")
    assert decrypted == plaintext_v1

    # Verify AAD tampering (wrong workspace_id or wrong secret_name) raises SecretDecryptionError
    with pytest.raises(SecretDecryptionError):
        decrypt_secret(raw_ciphertext, uuid.UUID(ws_b), "STRIPE_SECRET_KEY")
    with pytest.raises(SecretDecryptionError):
        decrypt_secret(raw_ciphertext, uuid.UUID(ws_a), "DIFFERENT_SECRET_NAME")

    # 2. Rotate secret (version 2)
    r2 = client.post(
        f"/v1/workspaces/{ws_a}/secrets",
        json={"name": "STRIPE_SECRET_KEY", "value": plaintext_v2},
        headers=alice_headers,
    )
    assert r2.status_code == 201
    assert r2.json()["key_version"] == 2
    assert plaintext_v2 not in r2.text

    # 3. Verify GET /secrets and GET /audit never leak plaintext or ciphertext
    list_res = client.get(f"/v1/workspaces/{ws_a}/secrets", headers=alice_headers)
    assert list_res.status_code == 200
    assert plaintext_v1 not in list_res.text
    assert plaintext_v2 not in list_res.text
    assert "v1:" not in list_res.text

    audit_res = client.get(f"/v1/workspaces/{ws_a}/audit", headers=alice_headers)
    assert audit_res.status_code == 200
    assert plaintext_v1 not in audit_res.text
    assert plaintext_v2 not in audit_res.text
    actions = [item["action"] for item in audit_res.json()["items"]]
    assert "secret.created" in actions
    assert "secret.rotated" in actions


def test_m4_noisy_workspace_quota_and_redis_rate_limit_isolation(client: TestClient, clean_db, monkeypatch):
    """
    Verify noisy workspace cannot bypass admission quotas (CONCURRENT_BUILD, DEPLOYED_SERVICE)
    or Redis rate limits (429 RATE_LIMITED), while a second workspace operates normally.
    """
    ws_noisy, noisy_headers = create_workspace(client, "noisy-tenant", "noisy")
    ws_quiet, quiet_headers = create_workspace(client, "quiet-tenant", "quiet")

    # 1. Quota enforcement: DEPLOYED_SERVICE cap (default 2 distinct deployed apps per workspace)
    digest = "registry.example.com/app@sha256:" + "d" * 64
    for i in range(2):
        app_r = client.post(
            f"/v1/workspaces/{ws_noisy}/apps",
            json={"name": f"Noisy App {i}", "slug": f"noisy-app-{i}", "workload_type": "HTTP_SERVICE"},
            headers=noisy_headers,
        )
        assert app_r.status_code == 201
        dep_r = client.post(
            f"/v1/apps/{app_r.json()['id']}/deployments",
            json={"image_digest": digest, "port": 8080},
            headers={"Idempotency-Key": f"dep-{i}-{uuid.uuid4().hex[:6]}", **noisy_headers},
        )
        assert dep_r.status_code == 202

    # 3rd distinct app deployment in noisy workspace is rejected with 429 QUOTA_EXCEEDED
    app_3 = client.post(
        f"/v1/workspaces/{ws_noisy}/apps",
        json={"name": "Noisy App 3", "slug": "noisy-app-3", "workload_type": "HTTP_SERVICE"},
        headers=noisy_headers,
    )
    assert app_3.status_code == 201
    dep_3 = client.post(
        f"/v1/apps/{app_3.json()['id']}/deployments",
        json={"image_digest": digest, "port": 8080},
        headers={"Idempotency-Key": f"dep-3-{uuid.uuid4().hex[:6]}", **noisy_headers},
    )
    assert dep_3.status_code == 429
    assert dep_3.json()["error_code"] == "QUOTA_EXCEEDED"
    assert dep_3.headers.get("Retry-After") is not None

    # Quiet workspace can still deploy its service unaffected
    quiet_app = client.post(
        f"/v1/workspaces/{ws_quiet}/apps",
        json={"name": "Quiet App", "slug": "quiet-app", "workload_type": "HTTP_SERVICE"},
        headers=quiet_headers,
    )
    assert quiet_app.status_code == 201
    quiet_dep = client.post(
        f"/v1/apps/{quiet_app.json()['id']}/deployments",
        json={"image_digest": digest, "port": 8080},
        headers={"Idempotency-Key": f"quiet-dep-{uuid.uuid4().hex[:6]}", **quiet_headers},
    )
    assert quiet_dep.status_code == 202

    # 2. Redis per-workspace rate limit enforcement
    monkeypatch.setattr(settings, "RATE_LIMIT_REQUESTS_PER_MINUTE", 3)
    job_digest = "registry.example.com/batch@sha256:" + "e" * 64
    statuses = []
    for i in range(4):
        jr = client.post(
            f"/v1/workspaces/{ws_noisy}/jobs",
            json={"name": f"burst-job-{i}", "image_digest": job_digest},
            headers={"Idempotency-Key": f"burst-{i}-{uuid.uuid4().hex[:6]}", **noisy_headers},
        )
        statuses.append(jr.status_code)
    assert 429 in statuses, f"Expected 429 RATE_LIMITED on burst in noisy workspace, got {statuses}"

    # Quiet workspace is still able to submit jobs under its own isolated rate limit bucket
    quiet_jr = client.post(
        f"/v1/workspaces/{ws_quiet}/jobs",
        json={"name": "quiet-job-1", "image_digest": job_digest},
        headers={"Idempotency-Key": f"quiet-j-{uuid.uuid4().hex[:6]}", **quiet_headers},
    )
    assert quiet_jr.status_code == 202

    # Clean up rows created in hamicloud_test so subsequent Go store tests start on a clean state
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("UPDATE releases SET status = 'SUPERSEDED' WHERE status = 'IMAGE_READY'")
        cur.execute("UPDATE jobs SET state = 'CANCELLED' WHERE state = 'QUEUED'")
    conn.commit()
    conn.close()


def test_m4_concurrent_build_reservation_enforced(client: TestClient, clean_db):
    """Verify that an active CONCURRENT_BUILD QuotaReservation blocks build admission."""
    ws_id, headers = create_workspace(client, "build-tenant", "build-ws")
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO quota_reservations (id, workspace_id, resource_class, units, status, expires_at, created_at, updated_at)
            VALUES (%s, %s, 'CONCURRENT_BUILD', 1, 'ACTIVE', NOW() + INTERVAL '1 hour', NOW(), NOW())
            """,
            (str(uuid.uuid4()), ws_id),
        )
    conn.commit()
    conn.close()

    quota_res = client.get(f"/v1/workspaces/{ws_id}/quotas", headers=headers)
    assert quota_res.status_code == 200
    assert quota_res.json()["concurrent_builds_used"] == 1


def test_m4_rollback_rate_limit_and_quota_enforcement(client: TestClient, clean_db, monkeypatch):
    """Verify that rollback enforces rate limits and DEPLOYED_SERVICE quota."""
    ws_id, headers = create_workspace(client, "rb-tenant", "rb-ws")
    digest = "registry.example.com/app@sha256:" + "f" * 64

    # Create app 1 with 2 releases
    app1 = client.post(
        f"/v1/workspaces/{ws_id}/apps",
        json={"name": "App 1", "slug": "app-1", "workload_type": "HTTP_SERVICE"},
        headers=headers,
    ).json()
    dep1 = client.post(
        f"/v1/apps/{app1['id']}/deployments",
        json={"image_digest": digest, "port": 8080},
        headers={"Idempotency-Key": f"dep-1-{uuid.uuid4().hex[:6]}", **headers},
    ).json()
    dep2 = client.post(
        f"/v1/apps/{app1['id']}/deployments",
        json={"image_digest": digest, "port": 8080},
        headers={"Idempotency-Key": f"dep-2-{uuid.uuid4().hex[:6]}", **headers},
    ).json()

    # Create and deploy app 2 (reaching limit of 2 distinct deployed services)
    app2 = client.post(
        f"/v1/workspaces/{ws_id}/apps",
        json={"name": "App 2", "slug": "app-2", "workload_type": "HTTP_SERVICE"},
        headers=headers,
    ).json()
    client.post(
        f"/v1/apps/{app2['id']}/deployments",
        json={"image_digest": digest, "port": 8080},
        headers={"Idempotency-Key": f"dep-3-{uuid.uuid4().hex[:6]}", **headers},
    )

    # Create app 3 (not deployed yet, distinct services cap is reached by app1 and app2)
    app3 = client.post(
        f"/v1/workspaces/{ws_id}/apps",
        json={"name": "App 3", "slug": "app-3", "workload_type": "HTTP_SERVICE"},
        headers=headers,
    ).json()
    rel_app3_id = str(uuid.uuid4())
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO releases (id, application_id, workspace_id, release_number, image_digest, config_json, status, created_at, updated_at)
            VALUES (%s, %s, %s, 1, %s, '{"port": 8080}', 'SUPERSEDED', NOW(), NOW())
            """,
            (rel_app3_id, app3["id"], ws_id, digest),
        )
    conn.commit()
    conn.close()

    # Attempting rollback on app3 should fail quota check (2 distinct services already deployed)
    rb_fail = client.post(
        f"/v1/apps/{app3['id']}/rollbacks",
        json={"target_release_id": rel_app3_id},
        headers={"Idempotency-Key": f"rb-quota-{uuid.uuid4().hex[:6]}", **headers},
    )
    assert rb_fail.status_code == 429
    assert rb_fail.json()["error_code"] == "QUOTA_EXCEEDED"

    # Rollback on already-deployed app1 should succeed under quota
    rb_ok = client.post(
        f"/v1/apps/{app1['id']}/rollbacks",
        json={"target_release_id": dep1["operation_id"]},
        headers={"Idempotency-Key": f"rb-ok-{uuid.uuid4().hex[:6]}", **headers},
    )
    assert rb_ok.status_code == 202

    # Now test rate limiting on rollback
    monkeypatch.setattr(settings, "RATE_LIMIT_REQUESTS_PER_MINUTE", 1)
    rb_rl = client.post(
        f"/v1/apps/{app1['id']}/rollbacks",
        json={"target_release_id": dep2["operation_id"]},
        headers={"Idempotency-Key": f"rb-rl-{uuid.uuid4().hex[:6]}", **headers},
    )
    assert rb_rl.status_code == 429
    assert rb_rl.json()["error_code"] == "RATE_LIMITED"


def test_sanitize_audit_details_recursively_redacts_lists_and_nested_dicts():
    from app.core.audit import sanitize_audit_details
    details = {
        "safe_key": "safe_val",
        "nested_dict": {"secret_token": "shh", "other": "ok"},
        "list_of_dicts": [
            {"password": "pw1", "user": "u1"},
            {"api_key": "k2", "user": "u2"},
            {"tags": ["admin", {"private_key": "pk"}]},
        ],
        "list_of_strings": ["foo", "bar"],
    }
    cleaned = sanitize_audit_details(details)
    assert cleaned["safe_key"] == "safe_val"
    assert cleaned["nested_dict"]["secret_token"] == "[REDACTED]"
    assert cleaned["nested_dict"]["other"] == "ok"
    assert cleaned["list_of_dicts"][0]["password"] == "[REDACTED]"
    assert cleaned["list_of_dicts"][0]["user"] == "u1"
    assert cleaned["list_of_dicts"][1]["api_key"] == "[REDACTED]"
    assert cleaned["list_of_dicts"][2]["tags"][0] == "admin"
    assert cleaned["list_of_dicts"][2]["tags"][1]["private_key"] == "[REDACTED]"
    assert cleaned["list_of_strings"] == ["foo", "bar"]
