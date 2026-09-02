"""OTP flow and access control."""


async def test_otp_request_requires_exactly_one_identifier(app_client):
    response = await app_client.post(
        "/api/v1/auth/otp/request",
        json={"phone": "+998901234567", "email": "a@b.com"},
    )
    assert response.status_code == 422


async def test_otp_request_validates_phone_format(app_client):
    response = await app_client.post("/api/v1/auth/otp/request", json={"phone": "12345"})
    assert response.status_code == 422


async def test_protected_route_rejects_anonymous_request(app_client):
    response = await app_client.get("/api/v1/users/me")
    assert response.status_code == 401


async def test_protected_route_rejects_a_malformed_token(app_client):
    response = await app_client.get(
        "/api/v1/users/me", headers={"Authorization": "Bearer not-a-token"}
    )
    assert response.status_code == 401


async def test_admin_route_rejects_a_plain_user(app_client, auth_headers):
    response = await app_client.get("/api/v1/admin/audit", headers=auth_headers)
    assert response.status_code == 403
