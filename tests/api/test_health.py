"""Health endpoints."""


async def test_health_reports_ok(app_client):
    response = await app_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_openapi_schema_is_generated(app_client):
    response = await app_client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"]
    # Every resource router must be mounted.
    paths = " ".join(schema["paths"])
    for resource in ("auth", "users", "programs", "plans", "ai", "admin"):
        assert resource in paths
