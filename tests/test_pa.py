"""PA tests forbid sockets via conftest and mock both intermediary requests."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx

from api import pa
from core import pa_client
from core.security import encode_session
from core.pricing import medication_docs, price_lines, subtotal, totals
from test_orders import Collection, api


def nested(row, key):
    for part in key.split("."):
        row = row.get(part) if isinstance(row, dict) else None
    return row


def match(row, query):
    for key, wanted in query.items():
        if key == "$or":
            if not any(match(row, condition) for condition in wanted): return False
        elif isinstance(wanted, dict) and "$ne" in wanted:
            if nested(row, key) == wanted["$ne"]: return False
        elif isinstance(wanted, dict) and "$lte" in wanted:
            if nested(row, key) is None or nested(row, key) > wanted["$lte"]: return False
        elif nested(row, key) != wanted:
            return False
    return True


class PACollection(Collection):
    async def update_one(self, query, update, upsert=False):
        for row in self.rows:
            if match(row, query):
                self.writes += 1
                row.update(deepcopy(update.get("$set", {})))
                row["version"] = row.get("version", 0) + update.get("$inc", {}).get("version", 0)
                for key, value in update.get("$push", {}).items():
                    row.setdefault(key, []).extend(deepcopy(value["$each"] if isinstance(value, dict) and "$each" in value else [value]))
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)


@pytest.fixture
def pa_flow(api, monkeypatch):
    client, db, doc, aggregator, _ = api
    meds = [
        {"lineId": "a", "procedureCode": "DRG-A", "diagnosisCode": "I10", "name": "A", "diagnosis": "Hypertension", "dosage": "daily", "quantity": 1},
        {"lineId": "b", "procedureCode": "DRG-B", "diagnosisCode": "I10", "name": "B", "diagnosis": "Hypertension", "dosage": "daily", "quantity": 2},
    ]
    prices = [{"medicationLineId": "a", "procedureCode": "DRG-A", "amount": 3000},
              {"medicationLineId": "b", "procedureCode": "DRG-B", "amount": 4000}]
    doc.update(status="completed", winnerId=str(aggregator["_id"]), medications=meds,
               finalProcedurePrices=prices, medicationSubtotal=7000, overallTotal=7000,
               fulfillmentType="picked_up", version=0, history=[], completedAt=datetime.now(timezone.utc))
    aggregator["providerId"] = "4304"
    db.orders = PACollection([doc])
    monkeypatch.setenv("SESSION_SECRET", "tests-only-session-secret-" * 3)
    monkeypatch.setenv("PA_DELIVERY_DIAGNOSIS_CODE", "Z76.9")
    monkeypatch.setattr(pa, "get_db", lambda: db)
    monkeypatch.setattr(pa, "get_member_info", AsyncMock(return_value={"group_id": "G", "division_id": "D", "dependant_number": "0"}))
    calls = []
    async def issue(payload):
        calls.append(payload)
        return f"PA-{len(calls)}"
    monkeypatch.setattr(pa, "issue_pa", issue)
    client.app.include_router(pa.router, prefix="/api")
    client.cookies.clear()
    client.cookies.set("staff_session", encode_session({"role": "staff", "userId": "staff", "name": "Reviewer"}))
    return client, db, doc, aggregator, calls


def generate(flow):
    client, _, doc, _, _ = flow
    return client.post(f'/api/orders/{doc["_id"]}/generate-pa', json={"expectedVersion": doc["version"]})


@pytest.mark.parametrize("count,delivery", [(1, False), (2, False), (1, True), (2, True)])
def test_pickup_and_delivery_lines(pa_flow, count, delivery):
    client, _, doc, _, calls = pa_flow
    doc["medications"] = doc["medications"][:count]
    doc["finalProcedurePrices"] = doc["finalProcedurePrices"][:count]
    doc["medicationSubtotal"] = sum(x["amount"] for x in doc["finalProcedurePrices"])
    doc["fulfillmentType"] = "delivered" if delivery else "picked_up"
    if delivery: doc["deliveryFee"] = 1250; doc["overallTotal"] = doc["medicationSubtotal"] + 1250
    else: doc["overallTotal"] = doc["medicationSubtotal"]
    result = generate(pa_flow)
    assert result.status_code == 200, result.text
    assert len(calls) == count + int(delivery)
    assert [x["ProcedureCode"] for x in calls] == [m["procedureCode"] for m in doc["medications"]] + (["PRE11"] if delivery else [])
    assert [x["AmountRequested"] for x in calls] == [p["amount"] for p in doc["finalProcedurePrices"]] + ([1250] if delivery else [])
    assert all(x["ProviderId"] == x["ProviderTIN"] == "4304" and x["AdditionalServices"] == [] for x in calls)
    assert all(x["CompletionDate"] == datetime.now(ZoneInfo("Africa/Lagos")).date().isoformat() for x in calls)
    assert all("UserID" in x and "UserId" in x and "IsNewBorn" in x and "IsNewborn" in x for x in calls)
    assert result.json()["status"] == "generated"
    assert generate(pa_flow).status_code == 409
    assert len(calls) == count + int(delivery)
    assert all("payload" not in str(event) and "Authorization" not in str(event) for event in doc["history"])


def test_eligibility_and_provider(pa_flow):
    _, _, doc, aggregator, calls = pa_flow
    doc["status"] = "accepted"
    assert generate(pa_flow).status_code == 409
    doc["status"] = "completed"
    aggregator.pop("providerId")
    assert generate(pa_flow).status_code == 422
    aggregator["providerId"] = "4304"
    doc.pop("finalProcedurePrices")
    assert generate(pa_flow).status_code == 422
    assert not calls


def test_partial_success_never_resubmits_generated(pa_flow, monkeypatch):
    _, _, doc, _, calls = pa_flow
    from httpx import ConnectError
    attempts = 0
    async def issue(payload):
        nonlocal attempts
        attempts += 1
        calls.append(payload)
        if attempts == 2: raise ConnectError("connection not established")
        return f"PA-{attempts}"
    monkeypatch.setattr(pa, "issue_pa", issue)
    assert generate(pa_flow).json()["status"] == "partial_failure"
    assert [line["status"] for line in doc["paGeneration"]["lines"]] == ["generated", "failed_retryable"]
    assert generate(pa_flow).json()["status"] == "generated"
    assert [x["ProcedureCode"] for x in calls] == ["DRG-A", "DRG-B", "DRG-B"]


def test_timeout_requires_verification(pa_flow, monkeypatch):
    import httpx
    _, _, doc, _, calls = pa_flow
    async def issue(payload):
        calls.append(payload)
        raise httpx.ReadTimeout("unknown result")
    monkeypatch.setattr(pa, "issue_pa", issue)
    assert generate(pa_flow).json()["status"] == "verification_required"
    assert generate(pa_flow).status_code == 409
    assert len(calls) == 1
    assert doc["paGeneration"]["lines"][0]["status"] == "verification_required"


def test_uncertain_recovery_requires_allowlisted_staff_and_evidence(pa_flow, monkeypatch):
    import httpx
    client, _, doc, _, calls = pa_flow
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(side_effect=httpx.ReadTimeout("unknown")))
    assert generate(pa_flow).json()["status"] == "verification_required"
    endpoint = f'/api/orders/{doc["_id"]}/pa-lines/a/verify'
    body = {"resolution": "confirmed_no_pa", "evidence": "Checked intermediary PA register; no matching PA exists",
            "confirmNoPaCreated": True, "verificationMethod": "PA register search", "checkedWith": "Intermediary PA register",
            "verifiedAt": datetime.now(timezone.utc).isoformat()}
    assert client.post(endpoint, json=body).status_code == 403
    monkeypatch.setenv("PA_RECOVERY_STAFF_IDS", "staff")
    assert client.post(endpoint, json={**body, "evidence": "short"}).status_code == 422
    assert client.post(endpoint, json={**body, "confirmNoPaCreated": False}).status_code == 422
    assert doc["paGeneration"]["lines"][0]["status"] == "verification_required"
    assert client.post(endpoint, json=body).status_code == 200
    assert doc["paGeneration"]["lines"][0]["status"] == "failed_retryable"
    assert doc["history"][-1]["resolution"] == "confirmed_no_pa"


def test_staff_session_required(pa_flow):
    client, _, doc, _, calls = pa_flow
    client.cookies.clear()
    assert generate(pa_flow).status_code == 401
    assert client.post(f'/api/orders/{doc["_id"]}/generate-pa',
                       json={"expectedVersion": doc["version"]},
                       headers={"X-Service-Key": "test-service-key"}).status_code == 401
    assert not calls


def test_decimal_prices_and_totals():
    meds = [{"lineId": "x", "procedureCode": "X"}]
    for value in (0, -1, "NaN", "Infinity", "abc", None, "1.001", "1e309"):
        with pytest.raises(Exception):
            price_lines(meds, [{"medicationLineId": "x", "procedureCode": "X", "amount": value}])
    lines = price_lines(meds, [{"medicationLineId": "x", "procedureCode": "X", "amount": "3.25"}])
    assert subtotal(lines) == 3.25
    assert totals(lines, "1.25")["overallTotal"] == 4.5


def test_new_order_missing_pa_fields_fails_at_intake(api):
    client, db, _, _, _ = api
    client.cookies.clear()
    client.cookies.set("staff_session", "valid")
    base = {"name": "Drug", "dosage": "daily", "quantity": 3, "diagnosis": "Hypertension",
            "procedureCode": "DRG-A", "diagnosisCode": "I10"}
    payload = {"enrollee": {"enrolleeId": "E1", "fullName": "Test"},
               "provider": {"providerId": "P1", "providerName": "Clinic"}, "medications": [base]}
    before = db.orders.writes
    for changes in ({"procedureCode": None}, {"procedureCode": "PRE11"}, {"diagnosisCode": None}, {"quantity": 0}):
        response = client.post('/api/orders', json={**payload, "medications": [{**base, **changes}]})
        assert response.status_code == 422
    assert db.orders.writes == before


def test_stable_line_ids_disambiguate_duplicate_procedure_codes():
    meds = medication_docs([{"procedureCode": "DRG-A"}, {"procedureCode": "DRG-A"}])
    assert meds[0]["lineId"] != meds[1]["lineId"]
    lines = price_lines(meds, [
        {"medicationLineId": meds[0]["lineId"], "procedureCode": "DRG-A", "amount": 3000},
        {"medicationLineId": meds[1]["lineId"], "procedureCode": "DRG-A", "amount": 4000},
    ])
    assert subtotal(lines) == 7000


def test_duplicate_procedure_codes_generate_distinct_pa_lines(pa_flow):
    _, _, doc, _, calls = pa_flow
    doc["medications"][1]["procedureCode"] = "DRG-A"
    doc["finalProcedurePrices"][1]["procedureCode"] = "DRG-A"
    assert generate(pa_flow).status_code == 200
    assert [item["ProcedureCode"] for item in calls] == ["DRG-A", "DRG-A"]
    assert [item["AmountRequested"] for item in calls] == [3000, 4000]
    assert [line["lineId"] for line in doc["paGeneration"]["lines"]] == ["a", "b"]


def test_provider_migration_is_dry_run_by_default():
    from bson import ObjectId
    from scripts.set_test_pharmacy_provider import update_record
    class Cursor:
        def __init__(self, rows): self.rows = rows
        def limit(self, n): return self.rows[:n]
    class Records:
        def __init__(self): self.row = {"_id": ObjectId(), "companyName": "Test pharmacy 1"}; self.writes = 0
        def find(self, query, projection): return Cursor([self.row] if query["_id"] == self.row["_id"] else [])
        def update_one(self, query, update):
            self.writes += 1
            assert query["_id"] == self.row["_id"] and query["providerId"] is None
            self.row.update(update["$set"])
            return SimpleNamespace(matched_count=1)
    records = Records()
    assert update_record(records, str(records.row["_id"]))["providerId"] == "4304"
    assert records.writes == 0
    assert update_record(records, str(records.row["_id"]), apply=True)["changed"]
    assert records.row["providerId"] == "4304"


def test_stale_page_and_stale_retry_never_call_issue_pa(pa_flow, monkeypatch):
    client, _, doc, _, calls = pa_flow
    page_version = doc["version"]
    doc["version"] += 1
    doc["finalProcedurePrices"][0]["amount"] = 3100
    doc["medicationSubtotal"] = doc["overallTotal"] = 7100
    response = client.post(f'/api/orders/{doc["_id"]}/generate-pa', json={"expectedVersion": page_version})
    assert response.status_code == 409
    assert calls == []
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(side_effect=httpx.ConnectError("not sent")))
    assert generate(pa_flow).status_code == 200
    retry_version = doc["version"]
    doc["version"] += 1
    assert client.post(f'/api/orders/{doc["_id"]}/generate-pa', json={"expectedVersion": retry_version}).status_code == 409
    assert not calls


def test_missing_expected_version_rejected(pa_flow):
    client, _, doc, _, calls = pa_flow
    assert client.post(f'/api/orders/{doc["_id"]}/generate-pa').status_code == 422
    assert not calls


def test_expired_submitting_requires_explicit_recovery(pa_flow, monkeypatch):
    from datetime import timedelta
    client, _, doc, _, calls = pa_flow
    doc["paGeneration"] = {"available": True, "active": True, "token": "old-owner",
                            "leaseUntil": datetime.now(timezone.utc) - timedelta(seconds=1),
                            "status": "in_progress", "lines": [{"lineId": "a", "procedureCode": "DRG-A", "description": "A",
                            "quantity": 1, "diagnosisCode": "I10", "amount": 3000, "providerId": "4304", "status": "submitting", "paNumber": None},
                            {"lineId": "b", "procedureCode": "DRG-B", "description": "B", "quantity": 2,
                            "diagnosisCode": "I10", "amount": 4000, "providerId": "4304", "status": "pending", "paNumber": None}]}
    assert pa.public_pa_state(doc)["interrupted"] is True
    assert generate(pa_flow).status_code == 409
    assert not calls
    monkeypatch.setenv("PA_RECOVERY_STAFF_IDS", "staff")
    response = client.post(f'/api/orders/{doc["_id"]}/pa-interruption/mark-verification-required')
    assert response.status_code == 200, response.text
    assert doc["paGeneration"]["token"] != "old-owner"
    assert doc["paGeneration"]["lines"][0]["status"] == "verification_required"
    assert doc["history"][-1]["previousStatus"] == "submitting"
    assert generate(pa_flow).status_code == 409
    assert not calls


def test_existing_pa_recovery_rejects_duplicate_reference(pa_flow, monkeypatch):
    client, _, doc, _, calls = pa_flow
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(side_effect=httpx.ReadTimeout("unknown")))
    assert generate(pa_flow).status_code == 200
    monkeypatch.setenv("PA_RECOVERY_STAFF_IDS", "staff")
    doc["paGeneration"]["lines"][1]["paNumber"] = "PA-USED"
    endpoint = f'/api/orders/{doc["_id"]}/pa-lines/a/verify'
    assert client.post(endpoint, json={"resolution": "existing_pa", "evidence": "Verified in PA register", "paNumber": "PA-USED"}).status_code == 422
    response = client.post(endpoint, json={"resolution": "existing_pa", "evidence": "Verified in PA register", "paNumber": "PA-NEW"})
    assert response.status_code == 200, response.text
    assert doc["history"][-1]["previousStatus"] == "verification_required"


def test_retry_rejects_provider_or_price_drift_before_issue_pa(pa_flow, monkeypatch):
    client, _, doc, aggregator, calls = pa_flow
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(side_effect=httpx.ConnectError("not sent")))
    assert generate(pa_flow).status_code == 200
    aggregator["providerId"] = "9999"
    assert generate(pa_flow).status_code == 409
    aggregator["providerId"] = "4304"
    doc["finalProcedurePrices"][0]["amount"] = 3500
    doc["medicationSubtotal"] = doc["overallTotal"] = 7500
    assert generate(pa_flow).status_code == 409
    assert not calls


def test_delivery_diagnosis_must_be_configured(pa_flow, monkeypatch):
    _, _, doc, _, calls = pa_flow
    doc.update(fulfillmentType="delivered", deliveryFee=500, overallTotal=7500)
    monkeypatch.delenv("PA_DELIVERY_DIAGNOSIS_CODE", raising=False)
    assert generate(pa_flow).status_code == 422
    assert not calls


def test_delivery_pre11_uncertain_result_retains_exact_fee_and_medication_success(pa_flow, monkeypatch):
    _, _, doc, _, calls = pa_flow
    doc.update(fulfillmentType="delivered", deliveryFee=1250.25, overallTotal=8250.25)
    async def issue(payload):
        calls.append(payload)
        if payload["ProcedureCode"] == "PRE11": raise httpx.ReadTimeout("unknown")
        return f"PA-{len(calls)}"
    monkeypatch.setattr(pa, "issue_pa", issue)
    assert generate(pa_flow).json()["status"] == "verification_required"
    assert calls[-1]["ProcedureCode"] == "PRE11"
    assert calls[-1]["AmountRequested"] == 1250.25
    assert [line["status"] for line in doc["paGeneration"]["lines"]] == ["generated", "generated", "verification_required"]


def test_historical_zero_delivery_fee_remains_readable(pa_flow, monkeypatch):
    from api import orders
    from core.security import decode_session
    monkeypatch.setattr(orders, "decode_session", decode_session)
    client, _, doc, _, _ = pa_flow
    doc.update(fulfillmentType="delivered", deliveryFee=0, overallTotal=7000)
    response = client.get(f'/api/orders/{doc["_id"]}')
    assert response.status_code == 200
    assert response.json()["deliveryFee"] == 0


def test_contract_date_is_lagos_generation_date_not_completion_date(pa_flow):
    _, _, doc, _, calls = pa_flow
    doc["completedAt"] = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert generate(pa_flow).status_code == 200
    today = datetime.now(ZoneInfo("Africa/Lagos")).date().isoformat()
    assert all(x["CompletionDate"] == x["PAStatusDate"] == x["Proceduredate"] == today for x in calls)


def test_price_recall_and_cancel_blocked_after_pa_starts(pa_flow, monkeypatch):
    from api import orders
    from core.security import decode_session
    monkeypatch.setattr(orders, "decode_session", decode_session)
    client, _, doc, _, _ = pa_flow
    assert generate(pa_flow).status_code == 200
    base = f'/api/orders/{doc["_id"]}'
    for action, body in [
        ("adjust-price", {"procedurePrices": doc["finalProcedurePrices"], "reason": "Correction"}),
        ("recall", {"reason": "Reversal"}),
        ("cancel", {"reason": "Reversal"}),
    ]:
        response = client.post(f"{base}/{action}", json={"expectedVersion": doc["version"], **body})
        assert response.status_code == 409, (action, response.text)


@pytest.mark.asyncio
async def test_pa_client_contract_and_fail_closed(monkeypatch):
    monkeypatch.delenv("MEDICLOUD_LEGACY_URL", raising=False)
    with pytest.raises(RuntimeError): pa_client._base()
    monkeypatch.setenv("MEDICLOUD_LEGACY_URL", "https://fake.example/intermediary")
    monkeypatch.delenv("MEDICLOUD_LEGACY_USER", raising=False)
    monkeypatch.delenv("MEDICLOUD_LEGACY_PASS", raising=False)
    monkeypatch.delenv("CLEARLINE_APP_USER", raising=False)
    monkeypatch.delenv("CLEARLINE_APP_PASS", raising=False)
    with pytest.raises(RuntimeError): pa_client._credentials()
    monkeypatch.setenv("MEDICLOUD_LEGACY_USER", "fake-user")
    monkeypatch.setenv("MEDICLOUD_LEGACY_PASS", "fake-pass")
    requests = []
    async def handler(request):
        requests.append(request)
        if request.url.path.endswith("/member"):
            return httpx.Response(200, json=[{"GroupId": "G", "DivisionID": "D", "DependantNumber": 0}])
        return httpx.Response(200, json={"PANumber": "PA-123"})
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(pa_client.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    member = await pa_client.get_member_info("M1")
    assert member == {"group_id": "G", "division_id": "D", "dependant_number": "0"}
    payload = pa_client.build_payload("M1", member, "DRG-A", "I10", 3, 4200, "4304", datetime.now(ZoneInfo("Africa/Lagos")).date())
    assert payload["Quantity"] == 3 and payload["AmountRequested"] == 4200
    assert payload["AdditionalServices"] == []
    assert await pa_client.issue_pa(payload) == "PA-123"
    assert requests[0].headers["username"] == "fake-user"
    assert requests[1].url.path.endswith("/IssuePa")


@pytest.mark.asyncio
async def test_pa_client_rejects_missing_member_fields_and_missing_pa_reference(monkeypatch):
    monkeypatch.setenv("MEDICLOUD_LEGACY_URL", "https://fake.example/intermediary")
    monkeypatch.setenv("MEDICLOUD_LEGACY_USER", "fake-user")
    monkeypatch.setenv("MEDICLOUD_LEGACY_PASS", "fake-pass")
    async def handler(request):
        return httpx.Response(200, json={"GroupId": "G"} if request.method == "GET" else {"AdmissionCode": "0"})
    original = httpx.AsyncClient
    monkeypatch.setattr(pa_client.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(ValueError): await pa_client.get_member_info("M1")
    with pytest.raises(ValueError): await pa_client.issue_pa({"ProcedureCode": "DRG-A"})


@pytest.mark.asyncio
async def test_concurrent_claim_blocks_second_request(pa_flow, monkeypatch):
    _, _, doc, _, calls = pa_flow
    entered = asyncio.Event()
    release = asyncio.Event()
    async def issue(payload):
        calls.append(payload)
        entered.set()
        await release.wait()
        return "PA-1"
    monkeypatch.setattr(pa, "issue_pa", issue)
    session = encode_session({"role": "staff", "userId": "staff", "name": "Reviewer"})
    first = asyncio.create_task(pa.generate_pharmacy_pa(str(doc["_id"]), pa.GenerationRequest(expectedVersion=0), staff_session=session))
    await entered.wait()
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await pa.generate_pharmacy_pa(str(doc["_id"]), pa.GenerationRequest(expectedVersion=0), staff_session=session)
    assert exc.value.status_code == 409
    release.set()
    await first
    assert [x["ProcedureCode"] for x in calls] == ["DRG-A", "DRG-B"]


@pytest.mark.asyncio
async def test_both_requests_pass_precheck_only_one_atomic_claim_succeeds(pa_flow, monkeypatch):
    _, _, doc, _, calls = pa_flow
    waiting = 0
    both_ready = asyncio.Event()
    async def member(_):
        nonlocal waiting
        waiting += 1
        if waiting == 2: both_ready.set()
        await both_ready.wait()
        return {"group_id": "G", "division_id": "D", "dependant_number": "0"}
    monkeypatch.setattr(pa, "get_member_info", member)
    session = encode_session({"role": "staff", "userId": "staff", "name": "Reviewer"})
    results = await asyncio.gather(
        pa.generate_pharmacy_pa(str(doc["_id"]), pa.GenerationRequest(expectedVersion=0), staff_session=session),
        pa.generate_pharmacy_pa(str(doc["_id"]), pa.GenerationRequest(expectedVersion=0), staff_session=session),
        return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(getattr(result, "status_code", None) == 409 for result in results) == 1
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_crash_after_upstream_success_keeps_write_ahead_and_reference(pa_flow, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger=pa.__name__)
    _, _, doc, _, calls = pa_flow
    original_save = pa._save
    saves = 0
    async def crash_after_post(*args):
        nonlocal saves
        saves += 1
        if saves == 2: raise RuntimeError("simulated persistence failure")
        return await original_save(*args)
    monkeypatch.setattr(pa, "_save", crash_after_post)
    session = encode_session({"role": "staff", "userId": "staff", "name": "Reviewer"})
    with pytest.raises(RuntimeError):
        await pa.generate_pharmacy_pa(str(doc["_id"]), pa.GenerationRequest(expectedVersion=0), staff_session=session)
    assert [line["status"] for line in doc["paGeneration"]["lines"]] == ["submitting", "pending"]
    assert len(calls) == 1
    assert "PA-1" in caplog.text
