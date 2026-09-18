"""Staff identity contract, using synthetic sessions and no live integrations."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from api import auth, aggregator
from core import security


IDENTITY = {"userId": "synthetic-staff", "name": "Test Staff", "email": "staff@example.com"}


@pytest.fixture
def client(monkeypatch):
    import main

    monkeypatch.setenv("SESSION_SECRET", "synthetic-identity-test-secret-" * 2)
    monkeypatch.setattr(main, "connect_db", AsyncMock())
    monkeypatch.setattr(main, "close_db", AsyncMock())

    def forbidden_db():
        raise AssertionError("Identity must not access the database")

    monkeypatch.setattr(auth, "get_db", forbidden_db)
    with TestClient(main.app, base_url="https://testserver") as client:
        yield client


def test_staff_identity_minimum_response(client):
    token = security.encode_session({**IDENTITY, "role": "staff", "internal": "not-public"})
    client.cookies.set("staff_session", token)
    response = client.get("/api/auth/staff/me")
    assert response.status_code == 200
    assert response.json() == IDENTITY
    assert token not in response.text
    assert token.split(".")[1] not in response.text
    assert security.validate_session_secret() not in response.text
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("case", ["missing", "malformed", "tampered", "aggregator", "wrong-role", "expired", "unsigned"])
def test_invalid_staff_identity(client, monkeypatch, case):
    payload = {**IDENTITY, "role": "staff"}
    if case in {"aggregator", "wrong-role"}:
        payload["role"] = "aggregator" if case == "aggregator" else "admin"
    token = security.encode_session(payload)
    if case == "malformed":
        token = "not-a-session"
    elif case == "tampered":
        token = token[:-1] + ("0" if token[-1] != "0" else "1")
    elif case == "unsigned":
        token = token.split(".")[0]
    elif case == "expired":
        expires_at = security.decode_session(token, "staff")["expiresAt"]
        monkeypatch.setattr(security.time, "time", lambda: expires_at)
    if case != "missing":
        client.cookies.set("staff_session", token)
    response = client.get("/api/auth/staff/me")
    assert response.status_code == 401
    assert response.json() == {"detail": "Staff authentication required" if case == "missing" else "Invalid staff session"}
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("field", ["userId", "name", "email"])
@pytest.mark.parametrize("value", [None, 123])
def test_invalid_identity_fields(client, field, value):
    client.cookies.set("staff_session", security.encode_session({**IDENTITY, "role": "staff", field: value}))
    assert client.get("/api/auth/staff/me").status_code == 401


def test_staff_login_to_identity(client, monkeypatch):
    user = {"_id": IDENTITY["userId"], "name": IDENTITY["name"], "email": IDENTITY["email"],
            "password_hash": security.hash_password("synthetic-password")}
    lookup = AsyncMock(return_value=user)
    monkeypatch.setattr(auth, "get_db", lambda: SimpleNamespace(staff_users=SimpleNamespace(find_one=lookup)))
    login = client.post("/api/auth/staff/login", json={"email": user["email"], "password": "synthetic-password"})
    assert login.status_code == 200
    assert security.decode_session(login.json()["session"], "staff")["userId"] == IDENTITY["userId"]
    assert client.get("/api/auth/staff/me").json() == IDENTITY
    lookup.assert_awaited_once_with({"email": user["email"]})


def test_aggregator_auth_unchanged(client):
    token = security.encode_session({**IDENTITY, "role": "aggregator"})
    assert aggregator._require_aggregator(token)["userId"] == IDENTITY["userId"]
    client.cookies.set("aggregator_session", token)
    assert client.get("/api/auth/staff/me").status_code == 401


def test_portal_credentialed_cors(client):
    client.cookies.set("staff_session", security.encode_session({**IDENTITY, "role": "staff"}))
    origin = "https://pharmacy-portal-delta.vercel.app"
    response = client.get("/api/auth/staff/me", headers={"Origin": origin})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
