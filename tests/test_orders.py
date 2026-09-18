from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from api import orders


def matches(row, query):
    return all(row.get(k) in v["$in"] if isinstance(v, dict) and "$in" in v else row.get(k) == v
               for k, v in query.items())


class Cursor:
    def __init__(self, rows):
        self.rows = deepcopy(rows)

    def sort(self, key, direction):
        self.rows.sort(key=lambda r: r.get(key, 0), reverse=direction < 0)
        return self

    def limit(self, limit):
        self.rows = self.rows[:limit]
        return self

    async def to_list(self, limit):
        return deepcopy(self.rows if limit is None else self.rows[:limit])

    def __aiter__(self):
        async def items():
            for row in self.rows:
                yield deepcopy(row)
        return items()


class Collection:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.writes = 0

    async def find_one(self, query):
        return next((deepcopy(r) for r in self.rows if matches(r, query)), None)

    async def insert_one(self, doc):
        self.writes += 1
        doc = deepcopy(doc)
        doc["_id"] = ObjectId()
        self.rows.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def update_one(self, query, update, upsert=False):
        for row in self.rows:
            if matches(row, query):
                self.writes += 1
                row.update(deepcopy(update.get("$set", {})))
                for key, value in update.get("$push", {}).items():
                    row.setdefault(key, []).extend(deepcopy(value["$each"]))
                for key in update.get("$unset", {}):
                    row.pop(key, None)
                return SimpleNamespace(matched_count=1)
        if upsert:
            await self.insert_one({**query, **update.get("$set", {})})
        return SimpleNamespace(matched_count=0)

    def find(self, query=None, *args):
        return Cursor([r for r in self.rows if matches(r, query or {})])

    async def to_list(self, limit):
        return deepcopy(self.rows)


@pytest.fixture
def api(monkeypatch):
    doc = {"_id": ObjectId(), "status": "pending_review", "intakeId": "test", "createdBy": "staff",
           "createdAt": datetime.now(timezone.utc), "enrollee": {"enrolleeId": "E1", "fullName": "Test", "phone": "000"}, "medications": []}
    aggregator = {"_id": ObjectId(), "companyName": "Pharmacy", "contactName": "Contact", "email": "test@example.com", "password_hash": "hidden"}
    db = SimpleNamespace(orders=Collection([doc]), aggregator_users=Collection([aggregator]), bids=Collection())
    monkeypatch.setattr(orders, "get_db", lambda: db)
    monkeypatch.setattr(orders, "decode_session", lambda token, role=None: {"userId": "staff", "name": "Reviewer"} if token == "valid" else None)
    notify = AsyncMock()
    monkeypatch.setattr(orders, "notify_order_created", notify)
    monkeypatch.setattr(orders, "notify_order_fulfilled", AsyncMock())
    monkeypatch.setattr(orders, "notify_order_accepted", AsyncMock())
    monkeypatch.setattr(orders, "notify_order_picked_up", AsyncMock())
    monkeypatch.setattr(orders.sse_manager, "broadcast", AsyncMock())
    app = FastAPI()
    app.include_router(orders.router, prefix="/api")
    with TestClient(app) as client:
        client.cookies.set("staff_session", "valid")
        yield client, db, doc, aggregator, notify


@pytest.mark.parametrize("body", [None, {}, {"comment": ""}, {"comment": "   "}])
def test_reject_requires_comment(api, body):
    client, db, doc, _, _ = api
    assert client.post(f'/api/orders/{doc["_id"]}/reject', json=body).status_code == 422
    assert db.orders.writes == 0


def test_reject_comment(api):
    client, db, doc, _, _ = api
    assert client.post(f'/api/orders/{doc["_id"]}/reject', json={"comment": "  Reason  "}).status_code == 200
    assert doc["status"] == "rejected"
    assert doc["denialComment"] == "Reason"
    assert doc["deniedBy"] == {"userId": "staff", "name": "Reviewer"}
    assert doc["deniedAt"].tzinfo is not None


def test_approve(api):
    client, _, doc, _, notify = api
    assert client.post(f'/api/orders/{doc["_id"]}/approve').status_code == 200
    assert doc["status"] == "bidding"
    assert doc["biddingEndsAt"] > doc["createdAt"]
    notify.assert_called_once()


def test_assign(api):
    client, db, doc, aggregator, notify = api
    assert client.post(f'/api/orders/{doc["_id"]}/assign', json={"aggregatorId": str(aggregator["_id"])}).status_code == 200
    assert doc["status"] == "direct_quote_requested"
    assert doc["winnerId"] == str(aggregator["_id"])
    assert doc["winnerName"] == "Pharmacy"
    assert doc["assignmentType"] == "direct"
    assert doc["biddingEndsAt"] is None
    assert db.bids.writes == 0 and db.bids.rows == []
    notify.assert_called_once()
    assert client.post(f'/api/orders/{doc["_id"]}/assign', json={"aggregatorId": str(aggregator["_id"])}).status_code == 400


@pytest.mark.parametrize("agg_id", ["invalid", str(ObjectId())])
def test_unknown_aggregator(api, agg_id):
    client, db, doc, _, _ = api
    assert client.post(f'/api/orders/{doc["_id"]}/assign', json={"aggregatorId": agg_id}).status_code == 404
    assert db.orders.writes == 0


def test_aggregators_staff_only(api):
    client, _, doc, aggregator, _ = api
    assert client.get('/api/aggregators').json() == [{"id": str(aggregator["_id"]), "companyName": "Pharmacy", "contactName": "Contact", "email": "test@example.com"}]
    client.cookies.clear()
    assert client.get('/api/aggregators').status_code == 401
    assert client.post(f'/api/orders/{doc["_id"]}/assign', json={"aggregatorId": str(aggregator["_id"])}).status_code == 401


def test_create_persists_failure_snapshot_and_exposes_it(api, monkeypatch):
    client, db, _, _, _ = api
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(lambda req: httpx.Response(503)), **kw))
    response = client.post('/api/orders', json={"enrollee": {"enrolleeId": "E1", "fullName": "Test"}, "provider": {"providerId": "P1", "providerName": "Clinic"}, "medications": []})
    assert response.status_code == 201
    doc = db.orders.rows[-1]
    snapshot = deepcopy(doc["reviewFlags"])
    assert doc["status"] == "pending_review"
    assert snapshot["request_date"] == doc["createdAt"].date().isoformat()
    assert "error" in snapshot["medication_benefit"] and "error" in snapshot["recent_medication"]
    assert "REVIEW_CHECKS_INCOMPLETE" in snapshot["codes"]
    assert snapshot["enrollee_id"] == "E1"
    assert orders._order_summary(doc).reviewFlags == snapshot
    assert orders._order_detail(doc, [], is_staff=True).reviewFlags == snapshot
    assert orders._order_detail(doc, [], is_staff=False).reviewFlags is None
    assert client.put(f'/api/orders/{doc["_id"]}', json={"enrollee": {"enrolleeId": "E2", "fullName": "Changed"}}).status_code == 200
    assert doc["reviewFlags"]["enrollee_id"] == "E2"
    assert doc["enrollee"]["enrolleeId"] == "E2"


def test_reject_edit_clears_denial(api):
    client, _, doc, _, _ = api
    assert client.post(f'/api/orders/{doc["_id"]}/reject', json={"comment": "Wrong dose"}).status_code == 200
    assert client.put(f'/api/orders/{doc["_id"]}', json={"enrollee": {"enrolleeId": "E1", "fullName": "Test"}}).status_code == 200
    assert doc["status"] == "pending_review"
    assert "denialComment" not in doc and "deniedBy" not in doc and "deniedAt" not in doc


def test_direct_deliver_does_not_invent_price(api, monkeypatch):
    client, db, doc, aggregator, _ = api
    assert client.post(f'/api/orders/{doc["_id"]}/assign', json={"aggregatorId": str(aggregator["_id"])}).status_code == 200
    doc["status"] = "accepted"
    doc.pop("assignmentVersion")  # legacy accepted direct order, no price
    monkeypatch.setattr(orders, "_require_aggregator", lambda *_a, **_k: {"userId": str(aggregator["_id"]), "name": "Pharmacy"})
    monkeypatch.setattr(orders.sse_manager, "broadcast", AsyncMock())
    resp = client.post(f'/api/orders/{doc["_id"]}/fulfill', json={"fulfillmentType": "delivered", "deliveryFee": 500, "expectedVersion": doc["version"]})
    assert resp.status_code == 200
    assert doc.get("winnerTotalPrice") is None
    assert doc["deliveryFee"] == 500


def test_aggregator_serialize_strips_review_snapshot():
    from api.aggregator import _serialize_order
    out = _serialize_order({
        "_id": ObjectId(),
        "status": "bidding",
        "reviewFlags": {"codes": ["ENROLLEE_TERMINATED"]},
        "denialComment": "secret",
        "deniedBy": {"userId": "staff"},
        "deniedAt": datetime.now(timezone.utc),
        "collectionCode": "x",
        "enrollee": {"enrolleeId": "E1", "fullName": "Ada", "phone": "0800", "email": "a@b.c", "address": "Lagos"},
    })
    assert "reviewFlags" not in out
    assert "denialComment" not in out
    assert "deniedBy" not in out
    assert "deniedAt" not in out
    assert "collectionCode" not in out
    assert out["enrollee"] == {"fullName": "Ada", "address": "Lagos"}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "assign", "reject"])
async def test_enrollee_edit_cannot_overwrite_concurrent_review_decision(api, monkeypatch, action):
    import asyncio
    from fastapi import HTTPException
    from models.schemas import AssignOrderRequest, RejectOrderRequest, UpdateOrderRequest

    _, db, doc, aggregator, _ = api
    doc["reviewFlags"] = {"enrollee_id": "E1", "codes": ["ENROLLEE_TERMINATED"]}
    started, resume = asyncio.Event(), asyncio.Event()

    async def review(*args):
        started.set()
        await resume.wait()
        return {"enrollee_id": "E2", "codes": []}

    monkeypatch.setattr(orders, "compute_review_flags", review)
    order_id = str(doc["_id"])
    edit = asyncio.create_task(orders.update_order(
        order_id, UpdateOrderRequest(enrollee={"enrolleeId": "E2", "fullName": "Changed"}),
        staff_session="valid", x_service_key="",
    ))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        auth = {"staff_session": "valid", "x_service_key": ""}
        if action == "approve":
            await orders.approve_order(order_id, **auth)
        elif action == "assign":
            await orders.assign_order(order_id, AssignOrderRequest(aggregatorId=str(aggregator["_id"])), **auth)
        else:
            await orders.reject_order(order_id, RejectOrderRequest(comment="Denied"), **auth)
        after_decision = deepcopy(doc)
        resume.set()
        with pytest.raises(HTTPException) as exc:
            await edit
        assert exc.value.status_code == 400
        assert doc == after_decision
        assert db.orders.writes == 1
    finally:
        resume.set()
        if not edit.done():
            edit.cancel()
        await asyncio.gather(edit, return_exceptions=True)
