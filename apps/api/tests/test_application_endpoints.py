import uuid
from fastapi.testclient import TestClient
import psycopg2
import pytest
from datetime import datetime, timezone

from tests.conftest import TEST_DATABASE_URL_SYNC

pytestmark = pytest.mark.usefixtures("clean_db")


def test_list_workspace_applications_cursor_pagination(client: TestClient):
    auth_headers = {"X-Dev-Subject": "app-owner"}

    # 1. Create a Workspace
    ws_resp = client.post(
        "/v1/workspaces",
        json={"name": "App Listing WS", "slug": f"ws-{uuid.uuid4().hex[:8]}"},
        headers=auth_headers,
    )
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    # 2. Empty list initially
    empty_resp = client.get(f"/v1/workspaces/{ws_id}/apps", headers=auth_headers)
    assert empty_resp.status_code == 200
    empty_data = empty_resp.json()
    assert empty_data["items"] == []
    assert empty_data["next_cursor"] is None

    # 3. Create 5 Applications
    created_app_ids = []
    for i in range(5):
        create_resp = client.post(
            f"/v1/workspaces/{ws_id}/apps",
            json={
                "name": f"App {i}",
                "slug": f"app-{i}-{uuid.uuid4().hex[:6]}",
                "workload_type": "HTTP_SERVICE",
            },
            headers=auth_headers,
        )
        assert create_resp.status_code == 201
        created_app_ids.append(create_resp.json()["id"])

    # 4. Paginate with limit=2
    # Page 1:
    p1 = client.get(f"/v1/workspaces/{ws_id}/apps?limit=2", headers=auth_headers)
    assert p1.status_code == 200
    p1_data = p1.json()
    assert len(p1_data["items"]) == 2
    assert p1_data["next_cursor"] is not None

    # Page 2:
    p2 = client.get(
        f"/v1/workspaces/{ws_id}/apps?limit=2&cursor={p1_data['next_cursor']}",
        headers=auth_headers,
    )
    assert p2.status_code == 200
    p2_data = p2.json()
    assert len(p2_data["items"]) == 2
    assert p2_data["next_cursor"] is not None

    # Page 3:
    p3 = client.get(
        f"/v1/workspaces/{ws_id}/apps?limit=2&cursor={p2_data['next_cursor']}",
        headers=auth_headers,
    )
    assert p3.status_code == 200
    p3_data = p3.json()
    assert len(p3_data["items"]) == 1
    assert p3_data["next_cursor"] is None

    traversed_ids = [app["id"] for app in p1_data["items"] + p2_data["items"] + p3_data["items"]]
    assert len(traversed_ids) == 5
    assert set(traversed_ids) == set(created_app_ids)

    # 5. Invalid pagination limit boundary checks
    assert client.get(f"/v1/workspaces/{ws_id}/apps?limit=0", headers=auth_headers).status_code == 422
    assert client.get(f"/v1/workspaces/{ws_id}/apps?limit=101", headers=auth_headers).status_code == 422
    assert client.get(f"/v1/workspaces/{ws_id}/apps?limit=-1", headers=auth_headers).status_code == 422

    # 6. Malformed cursor returns 400
    bad_cursor_resp = client.get(f"/v1/workspaces/{ws_id}/apps?cursor=not-a-valid-cursor", headers=auth_headers)
    assert bad_cursor_resp.status_code == 400
    assert bad_cursor_resp.json()["error_code"] == "BAD_REQUEST"
    assert bad_cursor_resp.json()["message"] == "Invalid pagination cursor"


def test_list_applications_multi_workspace_isolation(client: TestClient):
    headers_alice = {"X-Dev-Subject": "alice-owner"}
    headers_bob = {"X-Dev-Subject": "bob-owner"}

    # Workspace 1 (Alice)
    ws1_resp = client.post(
        "/v1/workspaces",
        json={"name": "Alice WS", "slug": f"alice-{uuid.uuid4().hex[:8]}"},
        headers=headers_alice,
    )
    ws1_id = ws1_resp.json()["id"]

    # Workspace 2 (Bob)
    ws2_resp = client.post(
        "/v1/workspaces",
        json={"name": "Bob WS", "slug": f"bob-{uuid.uuid4().hex[:8]}"},
        headers=headers_bob,
    )
    ws2_id = ws2_resp.json()["id"]

    # Create App in WS 1
    app1_resp = client.post(
        f"/v1/workspaces/{ws1_id}/apps",
        json={"name": "Alice App", "slug": f"alice-app-{uuid.uuid4().hex[:6]}", "workload_type": "HTTP_SERVICE"},
        headers=headers_alice,
    )
    app1_id = app1_resp.json()["id"]

    # Create App in WS 2
    app2_resp = client.post(
        f"/v1/workspaces/{ws2_id}/apps",
        json={"name": "Bob App", "slug": f"bob-app-{uuid.uuid4().hex[:6]}", "workload_type": "HTTP_SERVICE"},
        headers=headers_bob,
    )
    app2_id = app2_resp.json()["id"]

    # Alice lists WS 1: sees only app1
    alice_list = client.get(f"/v1/workspaces/{ws1_id}/apps", headers=headers_alice).json()
    assert len(alice_list["items"]) == 1
    assert alice_list["items"][0]["id"] == app1_id

    # Bob lists WS 2: sees only app2
    bob_list = client.get(f"/v1/workspaces/{ws2_id}/apps", headers=headers_bob).json()
    assert len(bob_list["items"]) == 1
    assert bob_list["items"][0]["id"] == app2_id

    # Bob attempts to list WS 1 -> 404 Workspace not found (anti-enumeration)
    bob_ws1 = client.get(f"/v1/workspaces/{ws1_id}/apps", headers=headers_bob)
    assert bob_ws1.status_code == 404
    assert bob_ws1.json()["message"] == "Workspace not found"


def test_get_application_details_and_roles(client: TestClient):
    headers_owner = {"X-Dev-Subject": "owner-user"}
    headers_viewer = {"X-Dev-Subject": "viewer-user"}
    headers_outsider = {"X-Dev-Subject": "outsider-user"}

    # 1. Create Workspace
    ws_resp = client.post(
        "/v1/workspaces",
        json={"name": "Details WS", "slug": f"ws-{uuid.uuid4().hex[:8]}"},
        headers=headers_owner,
    )
    ws_id = ws_resp.json()["id"]

    # Add viewer role
    now = datetime.now(timezone.utc)
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workspace_memberships (id, workspace_id, user_subject, role, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), ws_id, "viewer-user", "VIEWER", now, now),
        )
    conn.commit()
    conn.close()

    # 2. Create Application
    app_slug = f"details-app-{uuid.uuid4().hex[:6]}"
    create_resp = client.post(
        f"/v1/workspaces/{ws_id}/apps",
        json={"name": "Inspectable App", "slug": app_slug, "workload_type": "HTTP_SERVICE"},
        headers=headers_owner,
    )
    app_id = create_resp.json()["id"]

    # 3. Owner inspects App -> 200
    owner_get = client.get(f"/v1/apps/{app_id}", headers=headers_owner)
    assert owner_get.status_code == 200
    app_data = owner_get.json()
    assert app_data["id"] == app_id
    assert app_data["workspace_id"] == ws_id
    assert app_data["name"] == "Inspectable App"
    assert app_data["slug"] == app_slug
    assert app_data["workload_type"] == "HTTP_SERVICE"
    assert app_data["desired_generation"] == 1
    assert app_data["current_release_id"] is None
    assert "created_at" in app_data

    # 4. Viewer inspects App -> 200
    viewer_get = client.get(f"/v1/apps/{app_id}", headers=headers_viewer)
    assert viewer_get.status_code == 200
    assert viewer_get.json()["id"] == app_id

    # 5. Outsider inspects App -> 404 Application not found
    outsider_get = client.get(f"/v1/apps/{app_id}", headers=headers_outsider)
    assert outsider_get.status_code == 404
    assert outsider_get.json()["message"] == "Application not found"

    # 6. Non-existent app UUID -> 404 Application not found
    missing_get = client.get(f"/v1/apps/{uuid.uuid4()}", headers=headers_owner)
    assert missing_get.status_code == 404
    assert missing_get.json()["message"] == "Application not found"

    # 7. Unauthenticated request -> 401
    unauth_get = client.get(f"/v1/apps/{app_id}")
    assert unauth_get.status_code == 401
