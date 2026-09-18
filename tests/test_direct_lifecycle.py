"""Lifecycle integration tests against an isolated, conditional-update Mongo fake."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId
from fastapi import HTTPException

from api import orders, aggregator as aggregator_api
from core.security import encode_session, decode_session
from models.schemas import ReasonRequest, AssignOrderRequest
from test_orders import api, Collection


@pytest.fixture
def flow(api, monkeypatch):
    client, db, doc, agg, _ = api
    monkeypatch.setenv("SESSION_SECRET", "local-tests-only-" * 4)
    monkeypatch.setattr(orders, "decode_session", decode_session)
    monkeypatch.setattr(aggregator_api, "get_db", lambda: db)
    client.app.include_router(aggregator_api.router, prefix="/api")
    doc["enrollee"].pop("phone")
    other = {"_id": ObjectId(), "companyName": "Second Pharmacy"}
    db.aggregator_users.rows.append(other)

    class Flow:
        def auth(self, role="staff", target=None):
            client.cookies.clear()
            uid = "staff" if role == "staff" else str((target or agg)["_id"])
            client.cookies.set(f"{role}_session", encode_session({"userId": uid, "name": role, "role": role}))

        def post(self, action, data=None, role="staff", target=None, version=None):
            self.auth(role, target)
            payload = {"expectedVersion": doc.get("version", 0), **(data or {})}
            if version is not None:
                payload["expectedVersion"] = version
            return client.post(f'/api/orders/{doc["_id"]}/{action}', json=payload)

        def advance(self, stage="completed"):
            assert self.post("assign", {"aggregatorId": str(agg["_id"])}).status_code == 200
            if stage == "direct_quote_requested": return
            assert self.post("direct-quote", {"totalPrice": 1000}, "aggregator").status_code == 200
            if stage == "direct_price_review": return
            assert self.post("direct-approve").status_code == 200
            if stage == "awaiting_fulfillment": return
            assert self.post("accept", role="aggregator").status_code == 200
            if stage == "accepted": return
            kind = "delivered" if stage == "awaiting_confirmation" else "picked_up"
            assert self.post("fulfill", {"fulfillmentType": kind, "deliveryFee": 50}, "aggregator").status_code == 200

    f = Flow()
    f.client, f.db, f.doc, f.agg, f.other = client, db, doc, agg, other
    return f


def test_direct_complete_and_audit(flow):
    f = flow
    f.advance()
    assert f.doc["status"] == "completed"
    assert f.doc["winnerTotalPrice"] == 1000
    assert f.doc["directQuote"]["totalPrice"] == 1000
    assert f.doc["completedAt"] == f.doc["fulfilledAt"]
    assert [e["eventType"] for e in f.doc["history"]] == [
        "direct_assigned", "direct_quote_submitted", "direct_quote_approved", "aggregator_accepted", "fulfilled"]
    assert f.doc["version"] == 5 and f.doc["assignmentVersion"] == 1
    for event in f.doc["history"]:
        assert event["timestamp"].tzinfo == timezone.utc
        assert event["actorId"] and event["actorName"]
        assert event["actorRole"] in {"staff", "aggregator"}
        assert event["aggregatorId"] == str(f.agg["_id"])
        assert event["assignmentVersion"] == 1
    f.auth()
    detail = f.client.get(f'/api/orders/{f.doc["_id"]}').json()
    assert len(detail["history"]) == 5
    assert detail["paGeneration"] == {"available": False, "status": "not_configured"}


@pytest.mark.parametrize("stage", ["direct_quote_requested", "direct_price_review"])
def test_no_accept_or_fulfil_before_approval(flow, stage):
    flow.advance(stage)
    assert flow.post("accept", role="aggregator").status_code == 400
    assert flow.post("fulfill", {"fulfillmentType": "picked_up"}, "aggregator").status_code == 400


def test_quote_visibility_ownership_and_bidding_isolation(flow):
    f = flow
    f.advance("direct_quote_requested")
    assert f.post("direct-quote", {"totalPrice": 1200}, "aggregator", f.other).status_code == 403
    f.auth("aggregator", f.other)
    assert f.client.get(f'/api/orders/{f.doc["_id"]}').status_code == 403
    assert f.client.get(f'/api/orders/{f.doc["_id"]}/stream').status_code == 403
    assert f.client.get('/api/aggregator/orders').json()["active"] == []
    f.auth("aggregator")
    result = f.client.get('/api/aggregator/orders').json()
    assert result["active"][0]["status"] == "direct_quote_requested"
    assert "history" not in result["active"][0]
    assert f.post("bids", {"unitPrice": 1, "totalPrice": 100}, "aggregator").status_code == 400
    f.doc.update(status="bidding", assignmentType=None, biddingEndsAt=datetime.now(timezone.utc) + timedelta(hours=1))
    assert f.post("direct-quote", {"totalPrice": 100}, "aggregator").status_code == 400


def test_adjust_and_approve_preserves_quote(flow):
    flow.advance("direct_price_review")
    assert flow.post("direct-approve", {"adjusted_price": 900}).status_code == 422
    assert flow.post("direct-approve", {"adjusted_price": 900, "reason": "  Agreed discount  "}).status_code == 200
    assert flow.doc["winnerTotalPrice"] == 900 and flow.doc["directQuote"]["totalPrice"] == 1000
    event = flow.doc["history"][-2]
    assert event["eventType"] == "price_adjusted" and event["reason"] == "Agreed discount"
    assert event["oldValues"]["winnerTotalPrice"] == 1000
    assert event["newValues"]["winnerTotalPrice"] == 900


@pytest.mark.parametrize("stage", ["direct_quote_requested", "direct_price_review", "awaiting_fulfillment", "accepted"])
def test_recall_all_stages_and_reassign(flow, stage):
    f = flow
    f.advance(stage)
    history = deepcopy(f.doc["history"])
    assert f.post("recall", {"reason": "Supplier unavailable"}).status_code == 200
    assert f.doc["status"] == "direct_reassignment" and f.doc["winnerId"] is None
    assert f.doc["history"][:-1] == history
    assert f.post("direct-quote", {"totalPrice": 1000}, "aggregator").status_code == 403
    assert f.post("accept", role="aggregator").status_code == 400
    assert f.post("fulfill", {"fulfillmentType": "picked_up"}, "aggregator").status_code == 400
    assert f.post("assign", {"aggregatorId": str(f.other["_id"])}).status_code == 200
    assert f.doc["assignmentVersion"] == 2
    assert f.doc["directQuote"] is None and f.doc["winnerTotalPrice"] is None
    assert f.doc["history"][-1]["eventType"] == "direct_reassigned"
    assert f.post("direct-quote", {"totalPrice": 1000}, "aggregator").status_code == 403
    assert f.post("direct-quote", {"totalPrice": 1100}, "aggregator", f.other).status_code == 200


def test_deny_and_new_aggregator_complete(flow):
    f = flow
    f.advance("direct_price_review")
    assert f.post("direct-deny", {"reason": "Too expensive"}).status_code == 200
    assert f.doc["history"][-1]["eventType"] == "direct_quote_denied"
    assert f.doc["directQuote"]["totalPrice"] == 1000
    assert f.post("assign", {"aggregatorId": str(f.other["_id"])}).status_code == 200
    assert f.post("direct-quote", {"totalPrice": 800}, "aggregator", f.other).status_code == 200
    assert f.post("direct-approve").status_code == 200
    assert f.post("accept", role="aggregator").status_code == 403
    assert f.post("accept", role="aggregator", target=f.other).status_code == 200
    assert f.post("fulfill", {"fulfillmentType": "picked_up"}, "aggregator").status_code == 403
    assert f.post("fulfill", {"fulfillmentType": "picked_up"}, "aggregator", f.other).status_code == 200


@pytest.mark.parametrize("action,stage", [("direct-deny", "direct_price_review"), ("recall", "accepted"),
                                         ("adjust-price", "completed"), ("cancel", "completed"), ("recall", "completed")])
@pytest.mark.parametrize("reason", [None, "", " \n "])
def test_mandatory_reasons(flow, action, stage, reason):
    flow.advance(stage)
    before = deepcopy(flow.doc)
    payload = {"totalPrice": 900}
    if reason is not None: payload["reason"] = reason
    assert flow.post(action, payload).status_code == 422
    assert flow.doc == before


@pytest.mark.parametrize("stage", ["awaiting_confirmation", "completed"])
@pytest.mark.parametrize("action,state,event", [("adjust-price", None, "price_adjusted"),
                                               ("cancel", "cancelled", "cancelled"),
                                               ("recall", "post_fulfilment_recalled", "post_fulfilment_recalled")])
def test_post_fulfilment_controls(flow, stage, action, state, event):
    f = flow
    f.advance(stage)
    before = deepcopy(f.doc)
    assert f.post(action, {"reason": "Administrative correction", "totalPrice": 850}).status_code == 200
    assert f.doc["status"] == (state or stage)
    for key in ["fulfilledAt", "completedAt", "fulfillmentType", "deliveryFee", "acceptedAt"]:
        assert f.doc.get(key) == before.get(key)
    audit = f.doc["history"][-1]
    assert audit["eventType"] == event
    if action == "adjust-price":
        assert audit["oldValues"]["winnerTotalPrice"] == before["winnerTotalPrice"]
        assert audit["newValues"]["winnerTotalPrice"] == 850
    else:
        assert f.post("staff-confirm").status_code == 400
        assert f.post("klaire-callback", {"received": True}).status_code == 200
        assert f.doc["status"] == state
        assert f.post("accept", role="aggregator").status_code == 400
        assert f.post("fulfill", {"fulfillmentType": "picked_up"}, "aggregator").status_code == 400


@pytest.mark.parametrize("action", ["assign", "direct-approve", "direct-deny", "recall", "adjust-price", "cancel"])
def test_staff_actions_reject_aggregators(flow, action):
    f = flow
    data = {"aggregatorId": str(f.agg["_id"]), "reason": "test", "totalPrice": 100}
    assert f.post(action, data, "aggregator").status_code == 401
    # Moving a valid aggregator token into the staff cookie cannot elevate it.
    token = f.client.cookies.get("aggregator_session")
    f.client.cookies.clear()
    f.client.cookies.set("staff_session", token)
    assert f.client.post(f'/api/orders/{f.doc["_id"]}/{action}', json={**data, "expectedVersion": 0}).status_code == 401


@pytest.mark.parametrize("price", [0, -1, "NaN", "Infinity", "-Infinity"])
def test_quote_invalid_prices(flow, price):
    flow.advance("direct_quote_requested")
    assert flow.post("direct-quote", {"totalPrice": price}, "aggregator").status_code == 422


def test_legacy_completed_serializes_and_adjusts(flow):
    f = flow
    f.doc.update(status="completed", winnerTotalPrice=700, assignmentType="direct", winnerId=str(f.agg["_id"]), completedAt=datetime.now(timezone.utc))
    f.auth()
    data = f.client.get(f'/api/orders/{f.doc["_id"]}').json()
    assert data["version"] == 0 and data["assignmentVersion"] == 0 and data["history"] == []
    assert orders._order_summary(f.doc).version == 0
    assert f.post("adjust-price", {"totalPrice": 800, "reason": "Corrected"}).status_code == 200
    assert f.doc["history"][0]["oldValues"]["winnerTotalPrice"] == 700


def test_legacy_unapproved_direct_cannot_accept(flow):
    flow.doc.update(status="awaiting_fulfillment", assignmentType="direct", winnerId=str(flow.agg["_id"]))
    assert flow.post("accept", role="aggregator").status_code == 400
    assert flow.post("recall", {"reason": "Request a quote"}).status_code == 200


def test_stale_request_after_same_aggregator_reassignment(flow):
    f = flow
    f.advance("direct_quote_requested")
    stale = f.doc["version"]
    assert f.post("recall", {"reason": "Retry"}).status_code == 200
    assert f.post("assign", {"aggregatorId": str(f.agg["_id"])}).status_code == 200
    before = deepcopy(f.doc)
    assert f.post("direct-quote", {"totalPrice": 100}, "aggregator", version=stale).status_code == 409
    assert f.doc == before


@pytest.mark.parametrize("stage,action,data,role", [
    ("direct_quote_requested", "direct-quote", {"totalPrice": 900}, "aggregator"),
    ("direct_price_review", "direct-approve", {}, "staff"),
    ("direct_price_review", "direct-deny", {"reason": "deny"}, "staff"),
    ("awaiting_fulfillment", "accept", {}, "aggregator"),
    ("accepted", "fulfill", {"fulfillmentType": "delivered", "deliveryFee": 50}, "aggregator"),
    ("accepted", "recall", {"reason": "recall"}, "staff"),
    ("completed", "adjust-price", {"reason": "edit", "totalPrice": 900}, "staff"),
    ("completed", "cancel", {"reason": "cancel"}, "staff"),
    ("awaiting_confirmation", "staff-confirm", {}, "staff"),
    ("awaiting_confirmation", "klaire-callback", {"received": True}, "staff"),
])
def test_concurrent_recall_wins_without_stale_write_or_audit(flow, monkeypatch, stage, action, data, role):
    f = flow
    f.advance(stage)
    original = f.db.orders.update_one
    expected = None
    async def race(query, update, **kwargs):
        nonlocal expected
        monkeypatch.setattr(f.db.orders, "update_one", original)
        await orders.recall_order(str(f.doc["_id"]), ReasonRequest(expectedVersion=f.doc["version"], reason="Concurrent recall"),
                                  staff_session=encode_session({"userId": "staff", "name": "Staff", "role": "staff"}), x_service_key="")
        expected = deepcopy(f.doc)
        return await original(query, update, **kwargs)
    monkeypatch.setattr(f.db.orders, "update_one", race)
    assert f.post(action, data, role).status_code == 409
    assert f.doc == expected


def test_competitive_bidding_end_to_end(flow):
    f = flow
    assert f.post("approve").status_code == 200
    assert f.post("bids", {"unitPrice": 400, "totalPrice": 1200}, "aggregator").status_code == 200
    assert f.post("bids", {"unitPrice": 300, "totalPrice": 900}, "aggregator", f.other).status_code == 200
    assert f.post("close-bidding").status_code == 200
    assert f.doc["status"] == "clearline_price_review" and f.doc["winnerId"] == str(f.other["_id"])
    assert f.post("clearline-approve", {"adjusted_price": 850}).status_code == 200
    assert f.post("accept", role="aggregator", target=f.other).status_code == 200
    assert f.post("fulfill", {"fulfillmentType": "delivered", "deliveryFee": 50}, "aggregator", f.other).status_code == 200
    assert f.doc["winnerTotalPrice"] == 900
    assert f.post("staff-confirm").status_code == 200
    assert f.doc["status"] == "completed"


def test_delete_cannot_erase_history(flow):
    flow.advance()
    flow.auth()
    before = deepcopy(flow.doc)
    assert flow.client.delete(f'/api/orders/{flow.doc["_id"]}').status_code == 405
    assert flow.doc == before


def test_session_signature_role_expiry_and_legacy_rejection(monkeypatch):
    import base64
    import json
    from core import security
    monkeypatch.setenv("SESSION_SECRET", "test-only-secret-" * 4)
    token = encode_session({"userId": "a", "name": "A", "role": "aggregator"})
    assert decode_session(token, "aggregator")["userId"] == "a"
    assert decode_session(token, "staff") is None
    assert decode_session(token + "x") is None
    assert decode_session(base64.b64encode(json.dumps({"userId": "staff"}).encode()).decode()) is None
    monkeypatch.setattr(security.time, "time", lambda: datetime.now(timezone.utc).timestamp() + 86401)
    assert decode_session(token) is None


@pytest.mark.parametrize("action,stage,data,role", [
    ("direct-quote", "direct_quote_requested", {"totalPrice": 500}, "aggregator"),
    ("direct-approve", "direct_price_review", {}, "staff"),
    ("direct-deny", "direct_price_review", {"reason": "No"}, "staff"),
    ("accept", "awaiting_fulfillment", {}, "aggregator"),
    ("fulfill", "accepted", {"fulfillmentType": "picked_up"}, "aggregator"),
    ("assign", "direct_reassignment", {}, "staff"),
    ("recall", "accepted", {"reason": "No"}, "staff"),
    ("adjust-price", "completed", {"reason": "No", "totalPrice": 500}, "staff"),
    ("cancel", "completed", {"reason": "No"}, "staff"),
])
def test_version_required(flow, action, stage, data, role):
    f = flow
    f.advance("direct_quote_requested" if stage == "direct_reassignment" else stage)
    if stage == "direct_reassignment":
        assert f.post("recall", {"reason": "Retry"}).status_code == 200
        data = {"aggregatorId": str(f.other["_id"])}
    f.auth(role)
    before = deepcopy(f.doc)
    assert f.client.post(f'/api/orders/{f.doc["_id"]}/{action}', json=data).status_code == 422
    assert f.doc == before


@pytest.mark.parametrize("action", ["direct-approve", "direct-deny", "adjust-price", "cancel", "recall"])
def test_invalid_states_are_rejected(flow, action):
    before = deepcopy(flow.doc)
    assert flow.post(action, {"reason": "Test", "totalPrice": 500}).status_code == 400
    assert flow.doc == before


@pytest.mark.asyncio
async def test_stream_redacts_competitors_and_revokes_existing_subscription(flow, monkeypatch):
    import asyncio
    from core.sse import SSEManager
    f = flow
    f.advance("direct_quote_requested")
    manager = SSEManager()
    monkeypatch.setattr(orders, "sse_manager", manager)
    token = encode_session({"userId": str(f.agg["_id"]), "name": "A", "role": "aggregator"})
    f.db.bids.rows = [{"_id": ObjectId(), "orderId": str(f.doc["_id"]), "aggregatorId": str(f.other["_id"]),
                       "aggregatorName": "Secret rival", "unitPrice": 1, "totalPrice": 10,
                       "submittedAt": datetime.now(timezone.utc)}]
    response = await orders.order_stream(str(f.doc["_id"]), staff_session=None, aggregator_session=token, x_service_key="")
    stream = response.body_iterator
    first = await anext(stream)
    assert "Secret rival" not in first and '"bids": []' in first
    next_event = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await manager.broadcast(str(f.doc["_id"]), "bid_update", {"secret": "competitor"})
    assert await asyncio.wait_for(next_event, 1) == 'event: order_changed\ndata: {"refresh": true}\n\n'
    f.doc["winnerId"] = None
    next_event = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await manager.broadcast(str(f.doc["_id"]), "order_changed", {"refresh": True})
    assert "access_revoked" in await asyncio.wait_for(next_event, 1)
    await stream.aclose()
    assert not manager._subscribers[str(f.doc["_id"])]


@pytest.mark.parametrize("role", ["staff", "aggregator"])
def test_login_emits_role_bound_session(flow, monkeypatch, role):
    from api import auth
    f = flow
    f.db.staff_users = Collection([{"_id": ObjectId(), "email": "staff@example.com", "name": "Staff", "password_hash": "fake"}])
    f.agg.update(email="aggregator@example.com", password_hash="fake")
    monkeypatch.setattr(auth, "get_db", lambda: f.db)
    monkeypatch.setattr(auth, "verify_password", lambda plain, hashed: plain == "correct")
    f.client.app.include_router(auth.router, prefix="/api/auth")
    response = f.client.post(f'/api/auth/{role}/login', json={"email": f'{role}@example.com', "password": "correct"})
    assert response.status_code == 200
    assert decode_session(response.json()["session"], role)["role"] == role
    assert f.client.post(f'/api/auth/{role}/login', json={"email": f'{role}@example.com', "password": "wrong"}).status_code == 401


def test_session_missing_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    assert decode_session("anything") is None
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        encode_session({"userId": "a", "role": "staff"})


def test_invalid_id_new_endpoint(flow):
    flow.auth()
    assert flow.client.post('/api/orders/not-an-id/recall', json={"expectedVersion": 0, "reason": "test"}).status_code == 404


def test_competing_final_price_edit_is_rejected(flow, monkeypatch):
    from models.schemas import PriceAdjustmentRequest
    f = flow
    f.advance()
    original = f.db.orders.update_one
    async def race(query, update, **kwargs):
        monkeypatch.setattr(f.db.orders, "update_one", original)
        await orders.adjust_final_price(str(f.doc["_id"]), PriceAdjustmentRequest(expectedVersion=f.doc["version"], reason="First edit", totalPrice=700),
                                        staff_session=encode_session({"userId": "s", "name": "S", "role": "staff"}), x_service_key="")
        return await original(query, update, **kwargs)
    monkeypatch.setattr(f.db.orders, "update_one", race)
    assert f.post("adjust-price", {"reason": "Second edit", "totalPrice": 800}).status_code == 409
    assert f.doc["winnerTotalPrice"] == 700
    assert len([e for e in f.doc["history"] if e["eventType"] == "price_adjusted"]) == 1
