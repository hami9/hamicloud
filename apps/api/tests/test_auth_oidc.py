from datetime import datetime, timedelta, timezone
import os
import time
import uuid
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
import httpx
import jwt
import pytest

from app.core.auth import set_jwks_client
from app.core.config import settings
from app.main import app

pytestmark = pytest.mark.usefixtures("clean_db")


class MockSigningKey:
    """Mock PyJWK that exposes the RSA public key for PyJWT decode."""

    def __init__(self, key: bytes):
        self.key = key


class MockJWKClient:
    """Mock PyJWKClient that provides a signing key from a local RSA public key."""

    def __init__(self, public_key_pem: bytes):
        self.public_key_pem = public_key_pem

    def get_signing_key_from_jwt(self, token: str) -> MockSigningKey:
        return MockSigningKey(self.public_key_pem)


@pytest.fixture
def mock_rsa_keys():
    """Generate ephemeral RSA keypair for offline cryptographic JWT testing."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def test_oidc_bearer_token_validation_with_crypto_keys(client: TestClient, mock_rsa_keys):
    """Verify that get_caller strictly validates RSA-signed OIDC JWT Bearer tokens."""
    private_pem, public_pem = mock_rsa_keys

    # Inject mock JWKS client
    mock_client = MockJWKClient(public_pem)
    set_jwks_client(mock_client)  # type: ignore[arg-type]

    try:
        sub = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        claims = {
            "sub": sub,
            "iss": settings.OIDC_ISSUER_URL,
            "exp": int((now + timedelta(hours=1)).timestamp()),
            "iat": int(now.timestamp()),
            "preferred_username": "rsa-test-user",
            "email": "test@example.com",
        }
        token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "mock-key-1"})

        # Authenticated workspace creation with valid Bearer token
        resp = client.post(
            "/v1/workspaces",
            json={"name": "OIDC WS", "slug": f"ws-{uuid.uuid4().hex[:8]}"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
    finally:
        set_jwks_client(None)


def test_oidc_expired_token_rejected(client: TestClient, mock_rsa_keys):
    """Verify that expired tokens return 401 with standard ErrorResponse and WWW-Authenticate."""
    private_pem, public_pem = mock_rsa_keys
    mock_client = MockJWKClient(public_pem)
    set_jwks_client(mock_client)  # type: ignore[arg-type]

    try:
        sub = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        claims = {
            "sub": sub,
            "iss": settings.OIDC_ISSUER_URL,
            "exp": int((now - timedelta(seconds=10)).timestamp()),  # expired 10s ago
            "iat": int((now - timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "mock-key-1"})

        target_url = f"/v1/workspaces/{uuid.uuid4()}/jobs"
        resp = client.get(target_url, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "UNAUTHORIZED"
        assert resp.json()["message"] == "Token has expired"
        assert "Bearer" in resp.headers.get("www-authenticate", "")
    finally:
        set_jwks_client(None)


def test_oidc_invalid_issuer_rejected(client: TestClient, mock_rsa_keys):
    """Verify that tokens from an unauthorized issuer are rejected with 401."""
    private_pem, public_pem = mock_rsa_keys
    mock_client = MockJWKClient(public_pem)
    set_jwks_client(mock_client)  # type: ignore[arg-type]

    try:
        claims = {
            "sub": str(uuid.uuid4()),
            "iss": "http://evil-idp.com/realms/fake",
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "mock-key-1"})

        target_url = f"/v1/workspaces/{uuid.uuid4()}/jobs"
        resp = client.get(target_url, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "UNAUTHORIZED"
        assert resp.json()["message"] == "Invalid token issuer"
    finally:
        set_jwks_client(None)


def test_oidc_tampered_signature_rejected(client: TestClient, mock_rsa_keys):
    """Verify that tampered tokens fail signature verification and return 401."""
    private_pem, public_pem = mock_rsa_keys
    mock_client = MockJWKClient(public_pem)
    set_jwks_client(mock_client)  # type: ignore[arg-type]

    try:
        claims = {
            "sub": str(uuid.uuid4()),
            "iss": settings.OIDC_ISSUER_URL,
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "mock-key-1"})
        tampered_token = token[:-10] + "0000000000"

        target_url = f"/v1/workspaces/{uuid.uuid4()}/jobs"
        resp = client.get(target_url, headers={"Authorization": f"Bearer {tampered_token}"})
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "UNAUTHORIZED"
    finally:
        set_jwks_client(None)


def test_oidc_missing_sub_claim_rejected(client: TestClient, mock_rsa_keys):
    """Verify that a token without sub claim is rejected with 401."""
    private_pem, public_pem = mock_rsa_keys
    mock_client = MockJWKClient(public_pem)
    set_jwks_client(mock_client)  # type: ignore[arg-type]

    try:
        claims = {
            "iss": settings.OIDC_ISSUER_URL,
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "mock-key-1"})

        target_url = f"/v1/workspaces/{uuid.uuid4()}/jobs"
        resp = client.get(target_url, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "UNAUTHORIZED"
        assert resp.json()["message"] == "Token missing subject claim"
    finally:
        set_jwks_client(None)


def test_invalid_auth_header_formats(client: TestClient):
    """Verify malformed authorization headers return 401."""
    target_url = f"/v1/workspaces/{uuid.uuid4()}/jobs"

    # Basic auth instead of Bearer
    r1 = client.get(target_url, headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert r1.status_code == 401
    assert r1.json()["error_code"] == "UNAUTHORIZED"

    # Bearer with empty token
    r2 = client.get(target_url, headers={"Authorization": "Bearer  "})
    assert r2.status_code == 401

    # Completely garbage format
    r3 = client.get(target_url, headers={"Authorization": "NotEvenBearer"})
    assert r3.status_code == 401


def test_live_keycloak_tokens_and_two_workspace_isolation(client: TestClient):
    """Live end-to-end integration test against running Keycloak container.

    1. Polls Keycloak discovery and acquires tokens for 'alice' and 'bob'.
    2. Alice creates workspace 1 using her real Keycloak token.
    3. Bob attempts to list jobs in workspace 1 using his real Keycloak token.
    4. Bob receives 404 'Workspace not found' (anti-enumeration isolation).
    5. Bob creates workspace 2 and lists it successfully.
    """
    require_live = os.getenv("REQUIRE_LIVE_KEYCLOAK") == "1"
    discovery_url = f"{settings.OIDC_ISSUER_URL}/.well-known/openid-configuration"
    keycloak_token_url = f"{settings.OIDC_ISSUER_URL}/protocol/openid-connect/token"

    # Poll Keycloak discovery up to 60s with backoff
    discovery_ok = False
    start_time = time.monotonic()
    while time.monotonic() - start_time < 60.0:
        try:
            resp = httpx.get(discovery_url, timeout=5.0)
            if resp.status_code == 200:
                discovery_ok = True
                break
        except Exception:
            pass
        time.sleep(1.0)

    if not discovery_ok:
        if require_live:
            pytest.fail("Keycloak server discovery failed within 60s but REQUIRE_LIVE_KEYCLOAK=1 is set")
        else:
            pytest.skip("Keycloak server is not running or reachable at OIDC_ISSUER_URL")

    # Reset any cached jwks client to ensure live Keycloak JWKS is used
    set_jwks_client(None)

    # 1. Acquire Alice token with polling up to 60s
    alice_token = None
    last_err = None
    start_time = time.monotonic()
    while time.monotonic() - start_time < 60.0:
        try:
            alice_res = httpx.post(
                keycloak_token_url,
                data={
                    "client_id": "hamicloud-api",
                    "grant_type": "password",
                    "username": "alice",
                    "password": "alice123",
                },
                timeout=30.0,
            )
            if alice_res.status_code == 200:
                alice_token = alice_res.json()["access_token"]
                break
            last_err = f"Status {alice_res.status_code}: {alice_res.text}"
        except Exception as exc:
            last_err = str(exc)
        time.sleep(1.0)

    if not alice_token:
        if require_live:
            pytest.fail(f"Keycloak Alice token request failed within 60s ({last_err}) but REQUIRE_LIVE_KEYCLOAK=1 is set")
        else:
            pytest.skip(f"Keycloak Alice token acquisition failed ({last_err})")

    alice_headers = {"Authorization": f"Bearer {alice_token}"}

    # 2. Acquire Bob token
    try:
        bob_res = httpx.post(
            keycloak_token_url,
            data={
                "client_id": "hamicloud-api",
                "grant_type": "password",
                "username": "bob",
                "password": "bob123",
            },
            timeout=30.0,
        )
        assert bob_res.status_code == 200, f"Bob auth failed: {bob_res.text}"
        bob_token = bob_res.json()["access_token"]
        bob_headers = {"Authorization": f"Bearer {bob_token}"}
    except Exception as exc:
        if require_live:
            pytest.fail(f"Keycloak Bob token acquisition failed: {exc}")
        else:
            pytest.skip("Keycloak Bob token acquisition failed")

    # 3. Alice creates Workspace 1
    ws1_slug = f"ws-alice-{uuid.uuid4().hex[:6]}"
    ws1_resp = client.post(
        "/v1/workspaces",
        json={"name": "Alice Keycloak WS", "slug": ws1_slug},
        headers=alice_headers,
    )
    assert ws1_resp.status_code == 201, ws1_resp.text
    ws1_id = ws1_resp.json()["id"]

    # 4. Bob attempts to list jobs in Alice's Workspace 1 -> 404 Workspace not found
    bob_access_ws1 = client.get(f"/v1/workspaces/{ws1_id}/jobs", headers=bob_headers)
    assert bob_access_ws1.status_code == 404
    assert bob_access_ws1.json()["error_code"] == "NOT_FOUND"
    assert bob_access_ws1.json()["message"] == "Workspace not found"

    # 5. Bob creates Workspace 2
    ws2_slug = f"ws-bob-{uuid.uuid4().hex[:6]}"
    ws2_resp = client.post(
        "/v1/workspaces",
        json={"name": "Bob Keycloak WS", "slug": ws2_slug},
        headers=bob_headers,
    )
    assert ws2_resp.status_code == 201, ws2_resp.text
    ws2_id = ws2_resp.json()["id"]

    # 6. Bob can list jobs in his own workspace -> 200
    bob_access_ws2 = client.get(f"/v1/workspaces/{ws2_id}/jobs", headers=bob_headers)
    assert bob_access_ws2.status_code == 200
    assert bob_access_ws2.json()["items"] == []


def test_oidc_wrong_audience_rejected(client: TestClient, mock_rsa_keys, monkeypatch):
    """Verify that tokens with wrong audience are rejected with 401 Unauthorized."""
    private_pem, public_pem = mock_rsa_keys
    mock_client = MockJWKClient(public_pem)
    set_jwks_client(mock_client)  # type: ignore[arg-type]

    monkeypatch.setattr(settings, "OIDC_AUDIENCE", "hamicloud-api")

    try:
        claims = {
            "sub": str(uuid.uuid4()),
            "iss": settings.OIDC_ISSUER_URL,
            "aud": "wrong-audience",
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "mock-key-1"})

        target_url = f"/v1/workspaces/{uuid.uuid4()}/jobs"
        resp = client.get(target_url, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "UNAUTHORIZED"
        assert resp.json()["message"] == "Invalid token audience"
    finally:
        set_jwks_client(None)


def test_oidc_correct_audience_accepted(client: TestClient, mock_rsa_keys, monkeypatch):
    """Verify that tokens with matching audience are successfully validated."""
    private_pem, public_pem = mock_rsa_keys
    mock_client = MockJWKClient(public_pem)
    set_jwks_client(mock_client)  # type: ignore[arg-type]

    monkeypatch.setattr(settings, "OIDC_AUDIENCE", "hamicloud-api")

    try:
        claims = {
            "sub": str(uuid.uuid4()),
            "iss": settings.OIDC_ISSUER_URL,
            "aud": "hamicloud-api",
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "mock-key-1"})

        resp = client.post(
            "/v1/workspaces",
            json={"name": "Audience WS", "slug": f"ws-aud-{uuid.uuid4().hex[:8]}"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201
    finally:
        set_jwks_client(None)


def test_production_missing_oidc_audience_raises_startup_error(monkeypatch):
    """Verify that missing OIDC_AUDIENCE in non-development environment causes a startup error."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "OIDC_AUDIENCE", None)
    monkeypatch.setattr(settings, "ARTIFACTS_DIR", "/var/artifacts")

    with pytest.raises(RuntimeError, match="OIDC_AUDIENCE is required"):
        settings.validate_runtime_environment()

    with pytest.raises(RuntimeError, match="OIDC_AUDIENCE is required"):
        with TestClient(app):
            pass


def test_production_missing_artifacts_dir_raises_startup_error(monkeypatch):
    """Verify that missing ARTIFACTS_DIR in non-development environment causes a startup error."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "OIDC_AUDIENCE", "hamicloud-api")
    monkeypatch.setattr(settings, "ARTIFACTS_DIR", None)

    with pytest.raises(RuntimeError, match="ARTIFACTS_DIR is required"):
        settings.validate_runtime_environment()

    with pytest.raises(RuntimeError, match="ARTIFACTS_DIR is required"):
        with TestClient(app):
            pass


def test_non_development_missing_oidc_audience_returns_401(mock_rsa_keys, monkeypatch):
    """Verify that in non-development environment, missing OIDC_AUDIENCE at request-time raises 401."""
    from fastapi import HTTPException
    from app.core.auth import decode_oidc_token

    private_pem, public_pem = mock_rsa_keys
    mock_client = MockJWKClient(public_pem)
    set_jwks_client(mock_client)  # type: ignore[arg-type]

    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    monkeypatch.setattr(settings, "OIDC_AUDIENCE", None)

    try:
        claims = {
            "sub": str(uuid.uuid4()),
            "iss": settings.OIDC_ISSUER_URL,
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "mock-key-1"})

        with pytest.raises(HTTPException) as exc_info:
            decode_oidc_token(token)
        assert exc_info.value.status_code == 401
        assert "OIDC_AUDIENCE is required in non-development environment" in exc_info.value.detail
    finally:
        set_jwks_client(None)


def test_non_development_missing_oidc_audience_endpoint_returns_401(client: TestClient, mock_rsa_keys, monkeypatch):
    """Verify that incoming requests in non-development environment without OIDC_AUDIENCE receive 401."""
    private_pem, public_pem = mock_rsa_keys
    mock_client = MockJWKClient(public_pem)
    set_jwks_client(mock_client)  # type: ignore[arg-type]

    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    monkeypatch.setattr(settings, "OIDC_AUDIENCE", None)

    try:
        claims = {
            "sub": str(uuid.uuid4()),
            "iss": settings.OIDC_ISSUER_URL,
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "mock-key-1"})

        resp = client.post(
            "/v1/workspaces",
            json={"name": "Staging WS", "slug": f"ws-stg-{uuid.uuid4().hex[:8]}"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401
        assert "OIDC_AUDIENCE is required in non-development environment" in resp.json()["message"]
    finally:
        set_jwks_client(None)
