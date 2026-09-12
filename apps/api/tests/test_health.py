from fastapi.testclient import TestClient


def test_healthz_endpoint(client: TestClient):
    response = client.get("/healthz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "version" in data
    assert "timestamp" in data
    assert "x-correlation-id" in response.headers


def test_correlation_id_propagation(client: TestClient):
    custom_cid = "req_custom_correlation_id_12345"
    response = client.get("/healthz", headers={"X-Correlation-ID": custom_cid})
    assert response.status_code == 200
    assert response.headers["x-correlation-id"] == custom_cid
