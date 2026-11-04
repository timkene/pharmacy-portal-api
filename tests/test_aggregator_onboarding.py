import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import aggregator, auth, orders
from core.security import hash_password, verify_password
from test_orders import Collection


@pytest.fixture
def onboarding(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "aggregator-onboarding-tests-" * 2)
    staff = {"_id": ObjectId(), "name": "Staff", "email": "staff@example.com",
             "password_hash": hash_password("staff-password")}
    first = {"_id": ObjectId(), "companyName": "Alpha Pharmacy", "username": "alpha pharmacy",
             "usernameNormalized": "alpha pharmacy", "email": "alpha@example.com",
             "emailVerified": True, "password_hash": hash_password("alpha-password")}
    second = {"_id": ObjectId(), "companyName": "Beta Pharmacy", "usernameNormalized": "beta pharmacy",
              "email": "beta@example.com", "emailVerified": True,
              "password_hash": hash_password("beta-password")}
    assigned = {"_id": ObjectId(), "status": "accepted", "winnerId": str(second["_id"]),
                "winnerName": second["companyName"], "intakeId": "I1", "createdBy": "staff",
                "createdAt": datetime.now(timezone.utc), "enrollee": {"fullName": "Patient"},
                "medications": [], "bids": []}
    bidding = {"_id": ObjectId(), "status": "bidding", "biddingEndsAt": datetime.now(timezone.utc) + timedelta(hours=1),
               "intakeId": "I2", "createdBy": "staff", "createdAt": datetime.now(timezone.utc),
               "enrollee": {"fullName": "Open Patient"}, "medications": [], "bids": []}
    db = SimpleNamespace(
        staff_users=Collection([staff]), aggregator_users=Collection([first, second]),
        orders=Collection([assigned, bidding]), bids=Collection(),
    )
    monkeypatch.setattr(auth, "get_db", lambda: db)
    monkeypatch.setattr(aggregator, "get_db", lambda: db)
    monkeypatch.setattr(orders, "get_db", lambda: db)
    sent = []

    async def fake_send(to, subject, body):
        sent.append((to, subject, body))

    monkeypatch.setattr("core.mailer.send_email", fake_send)
    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(orders.router, prefix="/api")
    app.include_router(aggregator.router, prefix="/api")
    with TestClient(app) as client:
        yield client, db, first, second, staff, sent


def token_from(sent):
    return re.search(r"token=([^\s]+)", sent[-1][2]).group(1)


def login(client, username, password):
    return client.post("/api/auth/aggregator/login", json={"username": username, "password": password})


def test_username_login_normalizes_whitespace_and_case(onboarding):
    client, _, _, _, _, _ = onboarding
    response = login(client, "  ALPHA   PHARMACY ", "alpha-password")
    assert response.status_code == 200
    assert response.json()["user"]["companyName"] == "Alpha Pharmacy"


def test_auth_rate_limit_normalizes_whitespace_variants(monkeypatch):
    from fastapi import HTTPException

    auth._attempts.clear()
    request = SimpleNamespace(client=SimpleNamespace(host="test"))
    for _ in range(10):
        auth._limit(request, "login", "alpha pharmacy")
    with pytest.raises(HTTPException) as exc:
        auth._limit(request, "login", "  ALPHA   PHARMACY ")
    assert exc.value.status_code == 429


def test_must_change_password_gates_aggregator_orders(onboarding):
    client, db, first, _, _, _ = onboarding
    first["mustChangePassword"] = True
    assert login(client, "alpha pharmacy", "alpha-password").status_code == 200
    response = client.get("/api/aggregator/orders")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "password_required"
    first.pop("mustChangePassword")


def test_email_register_and_verify_without_session(onboarding):
    client, db, first, _, _, sent = onboarding
    assert login(client, "alpha pharmacy", "alpha-password").status_code == 200
    assert client.post("/api/auth/aggregator/email", json={"email": "new@example.com"}).status_code == 200
    assert "/aggregator/verify-email?token=" in sent[-1][2]
    token = token_from(sent)
    client.cookies.clear()
    assert client.post("/api/auth/aggregator/email/verify", json={"token": token}).status_code == 200
    assert db.aggregator_users.rows[0]["emailVerified"] is True
    assert "emailVerifyTokenHash" not in db.aggregator_users.rows[0]


def test_unverified_forgot_password_is_generic_and_does_not_send(onboarding):
    client, db, first, _, _, sent = onboarding
    first["emailVerified"] = False
    assert client.post("/api/auth/aggregator/forgot-password", json={"email": "alpha@example.com"}).status_code == 200
    assert sent == []
    assert not any("passwordResetTokenHash" in row for row in db.aggregator_users.rows)


def test_forgot_password_is_generic_for_unknown_and_known_identifiers(onboarding):
    client, _, _, _, _, sent = onboarding
    assert client.post("/api/auth/aggregator/forgot-password", json={"username": "missing"}).status_code == 200
    assert client.post("/api/auth/aggregator/forgot-password", json={"identifier": "alpha pharmacy"}).status_code == 200
    assert len(sent) == 1
    assert "/aggregator/reset-password?token=" in sent[-1][2]


def test_reset_expiry_and_reuse_are_rejected(onboarding):
    client, db, first, _, _, sent = onboarding
    assert client.post("/api/auth/aggregator/forgot-password", json={"email": first["email"]}).status_code == 200
    token = token_from(sent)
    first["passwordResetExpiresAt"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert client.post("/api/auth/aggregator/reset-password", json={"token": token, "newPassword": "new-alpha-password"}).status_code == 400
    assert client.post("/api/auth/aggregator/forgot-password", json={"email": first["email"]}).status_code == 200
    token = token_from(sent)
    assert client.post("/api/auth/aggregator/reset-password", json={"token": token, "newPassword": "new-alpha-password"}).status_code == 200
    assert client.post("/api/auth/aggregator/reset-password", json={"token": token, "newPassword": "another-password"}).status_code == 400


def test_reset_and_change_password_are_scoped_to_aggregator(onboarding):
    client, _, first, second, staff, sent = onboarding
    assert client.post("/api/auth/aggregator/forgot-password", json={"email": first["email"]}).status_code == 200
    token = token_from(sent)
    assert client.post("/api/auth/aggregator/reset-password", json={"token": token, "newPassword": "new-alpha-password"}).status_code == 200
    assert verify_password("new-alpha-password", first["password_hash"])
    assert verify_password("beta-password", second["password_hash"])
    assert verify_password("staff-password", staff["password_hash"])

    assert login(client, "alpha pharmacy", "new-alpha-password").status_code == 200
    assert client.post("/api/auth/aggregator/change-password", json={"currentPassword": "new-alpha-password", "newPassword": "final-alpha-password"}).status_code == 200
    assert verify_password("final-alpha-password", first["password_hash"])
    assert verify_password("beta-password", second["password_hash"])
    assert verify_password("staff-password", staff["password_hash"])


def test_aggregator_cannot_list_or_get_another_assigned_order(onboarding):
    client, db, first, second, _, _ = onboarding
    assert login(client, "alpha pharmacy", "alpha-password").status_code == 200
    listed = client.get("/api/aggregator/orders")
    assert listed.status_code == 200 and listed.json()["active"] == []
    order_id = str(db.orders.rows[0]["_id"])
    assert client.get(f"/api/orders/{order_id}").status_code == 403


def test_bidding_orders_are_visible_and_readable_but_assigned_orders_are_isolated(onboarding):
    client, db, first, second, _, _ = onboarding
    assert login(client, "alpha pharmacy", "alpha-password").status_code == 200

    listed = client.get("/api/aggregator/orders").json()
    assert listed["open"] and listed["open"][0]["status"] == "bidding"
    dashboard = client.get("/api/aggregator/dashboard").json()
    assert dashboard["openSessions"] and dashboard["openSessions"][0]["status"] == "bidding"

    bidding_id = str(db.orders.rows[1]["_id"])
    assert client.get(f"/api/orders/{bidding_id}").status_code == 200
    assert client.post(f"/api/orders/{bidding_id}/bids", json={"totalPrice": 100}).status_code == 200

    assigned_id = str(db.orders.rows[0]["_id"])
    assert client.get(f"/api/orders/{assigned_id}").status_code == 403


class _SyncCollection:
    def __init__(self, rows=()):
        self.rows = [dict(row) for row in rows]

    def find_one(self, query):
        for row in self.rows:
            if "usernameNormalized" in query and row.get("usernameNormalized") == query["usernameNormalized"]:
                return row
            if "companyName" in query and re.fullmatch(query["companyName"]["$regex"].strip("^$"), row.get("companyName", ""), re.IGNORECASE):
                return row
        return None

    def update_one(self, query, update):
        row = next(row for row in self.rows if row["_id"] == query["_id"])
        row.update(update["$set"])

    def insert_one(self, doc):
        row = dict(doc)
        row["_id"] = ObjectId()
        self.rows.append(row)


class _SyncClient:
    def __init__(self, collection):
        self.collection = collection
        self.closed = False

    def __getitem__(self, _name):
        return SimpleNamespace(aggregator_users=self.collection)

    def close(self):
        self.closed = True


def test_onboarding_seed_is_idempotent_without_password_reset(monkeypatch, capsys):
    from scripts import upsert_onboarding_aggregators as seed

    collection = _SyncCollection()
    client = _SyncClient(collection)
    monkeypatch.setenv("AGGREGATOR_TEMP_PASSWORD", "temporary-secret")
    monkeypatch.setattr(seed, "MongoClient", lambda *args, **kwargs: client)
    seed.run()
    original_hash = collection.rows[0]["password_hash"]
    seed.run()
    assert collection.rows[0]["password_hash"] == original_hash
    assert verify_password("temporary-secret", collection.rows[0]["password_hash"])
    assert "temporary-secret" not in capsys.readouterr().out


def test_onboarding_seed_refuses_username_company_collision(monkeypatch):
    from scripts import upsert_onboarding_aggregators as seed

    collection = _SyncCollection([
        {"_id": ObjectId(), "usernameNormalized": "benson pharmacy", "companyName": "Other Owner"},
        {"_id": ObjectId(), "usernameNormalized": "other", "companyName": "Benson Pharmacy"},
    ])
    client = _SyncClient(collection)
    monkeypatch.setenv("AGGREGATOR_TEMP_PASSWORD", "temporary-secret")
    monkeypatch.setattr(seed, "MongoClient", lambda *args, **kwargs: client)
    with pytest.raises(RuntimeError, match="Refusing Benson Pharmacy"):
        seed.run()


def test_staff_identity_does_not_expose_aggregator_fields(onboarding):
    client, _, _, _, staff, _ = onboarding
    response = client.post("/api/auth/staff/login", json={"email": staff["email"], "password": "staff-password"})
    assert response.status_code == 200
    assert set(response.json()["user"]) == {"name", "email"}
    assert set(client.get("/api/auth/staff/me").json()) == {"name", "email", "userId"}


def test_legacy_email_aggregator_login_still_works(onboarding):
    client, _, first, _, _, _ = onboarding
    response = client.post("/api/auth/aggregator/login", json={"email": first["email"], "password": "alpha-password"})
    assert response.status_code == 200


def test_staff_aggregator_listing_includes_medicloud_provider_id(onboarding):
    client, _, first, _, staff, _ = onboarding
    first["medicloudProviderId"] = "22342"
    response = client.post("/api/auth/staff/login", json={"email": staff["email"], "password": "staff-password"})
    assert response.status_code == 200
    assert client.get("/api/aggregators").json()[0]["medicloudProviderId"] == "22342"


def test_mailer_uses_gmail_defaults_and_fails_closed(monkeypatch):
    import core.mailer as mailer

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            self.args = (host, port, timeout)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def starttls(self):
            pass
        def login(self, user, password):
            pass
        def send_message(self, message):
            pass

    for key in ("SMTP_HOST", "SMTP", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLEARLINE_APP_USER", "mailer@example.com")
    monkeypatch.setenv("CLEARLINE_APP_PASS", "mailer-password")
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    mailer._send_sync("recipient@example.com", "subject", "body")

    monkeypatch.delenv("CLEARLINE_APP_USER")
    with pytest.raises(RuntimeError, match="not configured"):
        mailer._send_sync("recipient@example.com", "subject", "body")


def test_onboarding_seed_accounts_have_stable_normalized_usernames():
    from scripts.upsert_onboarding_aggregators import ACCOUNTS, normalize

    assert [normalize(username) for _, username, _ in ACCOUNTS] == [username for _, username, _ in ACCOUNTS]
    assert len({normalize(username) for _, username, _ in ACCOUNTS}) == len(ACCOUNTS)
