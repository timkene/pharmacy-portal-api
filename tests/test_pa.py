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


class ReferenceOwners(Collection):
    async def insert_one(self, doc):
        from pymongo.errors import DuplicateKeyError
        if any(x["_id"] == doc["_id"] for x in self.rows):
            raise DuplicateKeyError("duplicate reference")
        self.rows.append(deepcopy(doc))
        return SimpleNamespace(inserted_id=doc["_id"])


class PACollection(Collection):
    async def find_one(self, query):
        if isinstance(query.get("_id"), dict) and "$ne" in query["_id"]:
            number = query["$or"][0]["paGeneration.lines.paNumber"]
            for row in self.rows:
                if row["_id"] == query["_id"]["$ne"]:
                    continue
                state = row.get("paGeneration", {})
                if any(x.get("paNumber") == number or x.get("groupPaNumber") == number for x in state.get("lines", [])) or any(x.get("paNumber") == number for x in state.get("attempts", [])):
                    return deepcopy(row)
            return None
        return await super().find_one(query)

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


def accepted(payload):
    services = [payload] + payload.get("AdditionalServices", [])
    return {"data": {"PANumber": "PA-1", "Services": [
        {"ProcedureCode": x["ProcedureCode"], "Status": "ACCEPTED"} for x in services]}, "evidence": {}}


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
    db.pa_reference_owners = ReferenceOwners()
    monkeypatch.setenv("SESSION_SECRET", "tests-only-session-secret-" * 3)
    monkeypatch.setenv("PA_DELIVERY_DIAGNOSIS_CODE", "Z76.9")
    monkeypatch.setenv("MEDICLOUD_LEGACY_USER", "configured-test-user")
    monkeypatch.setenv("MEDICLOUD_LEGACY_PASS", "test-only-pass")
    monkeypatch.setattr(pa, "get_db", lambda: db)
    monkeypatch.setattr(pa, "get_member_info", AsyncMock(return_value={"group_legacy_id": "AVO", "dependant_number": "0", "member_id": "42"}))
    calls = []
    async def issue(payload):
        calls.append(payload)
        return accepted(payload)
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
    assert len(calls) == 1
    services = [calls[0]] + calls[0].get("AdditionalServices", [])
    assert [x["ProcedureCode"] for x in services] == [m["procedureCode"] for m in doc["medications"]] + (["PRE11"] if delivery else [])
    assert [x["AmountRequested"] for x in services] == [p["amount"] for p in doc["finalProcedurePrices"]] + ([1250] if delivery else [])
    assert all(x["AmountGranted"] == x["AmountRequested"] for x in services[1:])
    assert ("AdditionalServices" in calls[0]) == (len(services) > 1)
    assert calls[0]["GroupId"] == calls[0]["DivisionID"] == "AVO"
    assert calls[0]["UserID"] == calls[0]["UserId"] == "admin"
    assert calls[0]["Isbooked"] is False
    assert "isbooked" not in calls[0]
    assert result.json()["status"] == "generated"
    assert generate(pa_flow).status_code == 409
    assert len(calls) == 1
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
    async def issue(payload):
        calls.append(payload)
        return {"data": {"PANumber": "PA-1", "Services": [
            {"ProcedureCode": "DRG-A", "Status": "ACCEPTED"},
            {"ProcedureCode": "DRG-B", "Status": "REJECTED"}]}, "evidence": {}}
    monkeypatch.setattr(pa, "issue_pa", issue)
    assert generate(pa_flow).json()["status"] == "partial_failure"
    assert [x["status"] for x in doc["paGeneration"]["lines"]] == ["generated", "failed_rejected"]
    assert [x["paNumber"] for x in doc["paGeneration"]["lines"]] == ["PA-1", None]
    assert generate(pa_flow).status_code == 409
    assert len(calls) == 1


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
            "verifiedAt": datetime.now(timezone.utc).isoformat(),
            "confirmAuthoritativeAbsence": True, "reconciliationReference": "Authoritative register investigation TEST"}
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
    assert calls[0]["ProcedureCode"] == calls[0]["AdditionalServices"][0]["ProcedureCode"] == "DRG-A"
    assert [calls[0]["AmountRequested"], calls[0]["AdditionalServices"][0]["AmountRequested"]] == [3000, 4000]
    assert [line["lineId"] for line in doc["paGeneration"]["lines"]] == ["a", "b"]
    assert all(x["status"] == "verification_required" for x in doc["paGeneration"]["lines"])


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
    doc["paGeneration"]["lines"][1].update(paNumber="PA-USED", submissionGroupId="different-group")
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
        return {"data": {"PANumber": "PA-1", "Services": [
            {"ProcedureCode": "DRG-A", "Status": "ACCEPTED"},
            {"ProcedureCode": "DRG-B", "Status": "ACCEPTED"}]}, "evidence": {}}
    monkeypatch.setattr(pa, "issue_pa", issue)
    assert generate(pa_flow).json()["status"] == "verification_required"
    assert calls[0]["AdditionalServices"][-1]["ProcedureCode"] == "PRE11"
    assert calls[0]["AdditionalServices"][-1]["AmountRequested"] == 1250.25
    assert [x["status"] for x in doc["paGeneration"]["lines"]] == ["generated", "generated", "verification_required"]


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
    monkeypatch.setenv("MEDICLOUD_API_URL", "https://lookup.test")
    monkeypatch.setenv("MEDICLOUD_API_KEY", "test-key")
    monkeypatch.setenv("MEDICLOUD_LEGACY_URL", "https://medicloud.test/interm/")
    monkeypatch.setenv("MEDICLOUD_LEGACY_USER", "different-staging-user")
    monkeypatch.setenv("MEDICLOUD_LEGACY_PASS", "test-pass")
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"found": True, "count": 1, "truncated": False,
                "results": [{"legacycode": "M1", "groupid": 123, "divisionid": 456, "group_legacy_id": "AVO", "memberid": 42, "isprincipal": True, "isterminated": 0, "isdeleted": 0}]})
        return httpx.Response(200, json={"PANumber": "PA-123"})
    original = httpx.AsyncClient
    monkeypatch.setattr(pa_client.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    member = await pa_client.get_member_info("M1")
    assert member == {"group_legacy_id": "AVO", "dependant_number": "0", "member_id": "42"}
    payload = pa_client.build_payload("M1", member, "DRG-A", "I10", 3, 4200, "4304", datetime.now(ZoneInfo("Africa/Lagos")).date())
    assert "AdditionalServices" not in payload
    result = await pa_client.issue_pa(payload)
    assert result["data"]["PANumber"] == "PA-123"
    assert requests[0].url.path == "/enrollees/lookup"
    assert requests[0].url.params["enrollee_id"] == "M1"
    assert requests[0].headers["X-API-Key"] == "test-key"
    assert requests[1].url.path == "/interm/IssuePa"
    assert requests[1].headers["username"] == "different-staging-user"
    assert requests[1].headers["password"] == "test-pass"
    assert "authorization" not in requests[1].headers
    import json
    outbound = json.loads(requests[1].content)
    assert outbound["UserID"] == outbound["UserId"] == "admin"
    assert "AmountGranted" not in payload


def test_office_issue_pa_sample_shape(monkeypatch):
    """Office STN sample: primary consult + two additional drugs, staging host default."""
    monkeypatch.setenv("MEDICLOUD_LEGACY_USER", "shape-test-user")
    monkeypatch.setenv("MEDICLOUD_LEGACY_PASS", "shape-test-pass")
    monkeypatch.delenv("MEDICLOUD_LEGACY_URL", raising=False)
    member = {"group_legacy_id": "STN", "dependant_number": "0"}
    extra = [
        {
            "procedureCode": "Z11047",
            "diagnosisCode": "R0789",
            "description": "PARACETAMOL - 500 (Oral) - Three times daily (TDS/TID) for 3",
            "diagnosisDescription": "Other chest pain",
            "quantity": 18,
            "amount": 10,
        },
        {
            "procedureCode": "Z15283",
            "diagnosisCode": "R0789",
            "description": "LORATIDINE TAB 10MG - 10 (Oral) - Once daily (OD) for 7",
            "diagnosisDescription": "Other chest pain",
            "quantity": 7,
            "amount": 50,
        },
    ]
    payload = pa_client.build_payload(
        "151027856", member, "7330", "R0789", 1, 5000, "18928",
        datetime(2025, 12, 1).date(), additional_lines=extra,
    )
    assert pa_client._base() == "https://medicloud.dev/interm"
    assert payload["GroupId"] == payload["DivisionID"] == "STN"
    assert payload["IID"] == payload["InsureID"] == "151027856"
    assert payload["ProcedureCode"] == "7330"
    assert payload["UserID"] == payload["UserId"] == "admin"
    assert payload["Isbooked"] is False
    assert payload["PAStatus"] == "AUTHORIZED"
    assert [x["ProcedureCode"] for x in payload["AdditionalServices"]] == ["Z11047", "Z15283"]
    assert payload["AdditionalServices"][0]["AmountGranted"] == 10
    assert payload["AdditionalServices"][1]["Quantity"] == 7
    assert "isbooked" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [None, "", 123])
async def test_member_no_numeric_fallback(monkeypatch, legacy):
    monkeypatch.setenv("MEDICLOUD_API_URL", "https://lookup.test")
    monkeypatch.setenv("MEDICLOUD_API_KEY", "test-key")
    original = httpx.AsyncClient
    def handler(request):
        return httpx.Response(200, json={"found": True, "count": 1, "results": [
            {"legacycode": "M1", "groupid": 123, "divisionid": 456, "group_legacy_id": legacy}]})
    monkeypatch.setattr(pa_client.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(ValueError):
        await pa_client.get_member_info("M1")


def test_missing_legacy_blocks_before_claim(pa_flow, monkeypatch):
    _, db, doc, _, calls = pa_flow
    monkeypatch.setattr(pa, "get_member_info", AsyncMock(return_value={"groupid": 123}))
    assert generate(pa_flow).status_code == 422
    assert db.orders.writes == 0 and not calls


@pytest.mark.asyncio
async def test_concurrent_claim_blocks_second_request(pa_flow, monkeypatch):
    _, _, doc, _, calls = pa_flow
    entered = asyncio.Event()
    release = asyncio.Event()
    async def issue(payload):
        calls.append(payload)
        entered.set()
        await release.wait()
        return accepted(payload)
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
    assert len(calls) == 1


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
        return {"group_legacy_id": "AVO", "dependant_number": "0", "member_id": "42"}
    monkeypatch.setattr(pa, "get_member_info", member)
    session = encode_session({"role": "staff", "userId": "staff", "name": "Reviewer"})
    results = await asyncio.gather(
        pa.generate_pharmacy_pa(str(doc["_id"]), pa.GenerationRequest(expectedVersion=0), staff_session=session),
        pa.generate_pharmacy_pa(str(doc["_id"]), pa.GenerationRequest(expectedVersion=0), staff_session=session),
        return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(getattr(result, "status_code", None) == 409 for result in results) == 1
    assert len(calls) == 1


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
        if saves == 1: raise RuntimeError("simulated persistence failure")
        return await original_save(*args)
    monkeypatch.setattr(pa, "_save", crash_after_post)
    session = encode_session({"role": "staff", "userId": "staff", "name": "Reviewer"})
    with pytest.raises(RuntimeError):
        await pa.generate_pharmacy_pa(str(doc["_id"]), pa.GenerationRequest(expectedVersion=0), staff_session=session)
    assert [line["status"] for line in doc["paGeneration"]["lines"]] == ["submitting", "submitting"]
    assert len(calls) == 1
    assert "PA-1" in caplog.text


@pytest.mark.parametrize("data", [{"PANumber": "PA-1"}, {"PANumber": "PA-1", "PAStatus": "AUTHORIZED"}, {"unknown": True}])
def test_reference_alone_never_accepts_services(pa_flow, monkeypatch, data):
    _, _, doc, _, _ = pa_flow
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(return_value={"data": data, "evidence": {}}))
    assert generate(pa_flow).json()["status"] == "verification_required"
    assert all(x["status"] == "verification_required" for x in doc["paGeneration"]["lines"])
    assert generate(pa_flow).status_code == 409


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("unknown"), httpx.ConnectError("unknown"),
    httpx.HTTPStatusError("500", request=httpx.Request("POST", "https://fake.test"), response=httpx.Response(500))])
def test_ambiguous_group_blocks_all_lines_and_retry(pa_flow, monkeypatch, exc):
    _, _, doc, _, _ = pa_flow
    mock = AsyncMock(side_effect=exc)
    monkeypatch.setattr(pa, "issue_pa", mock)
    assert generate(pa_flow).json()["status"] == "verification_required"
    assert all(x["status"] == "verification_required" for x in doc["paGeneration"]["lines"])
    assert generate(pa_flow).status_code == 409
    assert mock.await_count == 1


def test_recovery_allows_shared_reference_only_in_same_group(pa_flow, monkeypatch):
    client, _, doc, _, _ = pa_flow
    monkeypatch.setenv("PA_RECOVERY_STAFF_IDS", "staff")
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(return_value={"data": {"PANumber": "PA-1"}, "evidence": {}}))
    generate(pa_flow)
    for line_id in ("a", "b"):
        r = client.post(f'/api/orders/{doc["_id"]}/pa-lines/{line_id}/verify', json={
            "resolution": "existing_pa", "paNumber": "PA-1", "evidence": "Verified individual service in upstream PA"})
        assert r.status_code == 200, r.text
    assert all(x["status"] == "generated" for x in doc["paGeneration"]["lines"])


@pytest.mark.parametrize('data', [
    {"PANumber": "PA-1", "Services": [{"ProcedureCode": "DRG-A", "Status": "ACCEPTED", "PAStatus": "REJECTED"}]},
    {"PANumber": "PA-1", "Services": [{"ProcedureCode": "DRG-A", "Status": "ACCEPTED", "DiagnosisCode": "WRONG"}]},
    {"PANumber": "PA-1", "Services": [{"ProcedureCode": "DRG-A", "Status": "ACCEPTED"}, {"ProcedureCode": "DRG-A", "Status": "ACCEPTED"}]},
    {"Services": [{"ProcedureCode": "DRG-A", "Status": "ACCEPTED"}]},
])
def test_conflicting_or_unreferenced_evidence_is_uncertain(pa_flow, monkeypatch, data):
    _, _, doc, _, _ = pa_flow
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(return_value={"data": data, "evidence": {}}))
    generate(pa_flow)
    assert all(x["status"] == "verification_required" for x in doc["paGeneration"]["lines"])


def test_grouped_http_post_and_private_response_evidence(pa_flow, monkeypatch):
    _, _, doc, _, _ = pa_flow
    doc.update(fulfillmentType="delivered", deliveryFee=500, overallTotal=7500)
    requests = []
    monkeypatch.setenv("MEDICLOUD_LEGACY_URL", "https://fake.test/interm")
    def handler(request):
        import json
        requests.append(request)
        payload = json.loads(request.content)
        data = accepted(payload)["data"]
        data["echo"] = "configured-test-user:test-only-pass"
        return httpx.Response(200, json=data)
    original = httpx.AsyncClient
    monkeypatch.setattr(pa_client.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(pa, "issue_pa", pa_client.issue_pa)
    assert generate(pa_flow).json()["status"] == "generated"
    assert len(requests) == 1 and requests[0].method == "POST"
    attempt = doc["paGeneration"]["attempts"][0]
    assert attempt["lineIds"] == ["a", "b", "delivery-PRE11"]
    assert attempt["response"]["httpStatus"] == 200
    assert "test-only-pass" not in attempt["response"]["body"]
    assert "configured-test-user" not in attempt["response"]["body"]
    assert "body" not in str(pa.public_pa_state(doc))


def test_http500_reference_is_preserved_without_success(pa_flow, monkeypatch):
    _, _, doc, _, _ = pa_flow
    monkeypatch.setenv("MEDICLOUD_LEGACY_URL", "https://fake.test/interm")
    original = httpx.AsyncClient
    monkeypatch.setattr(pa_client.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(500, json={"PANumber": "PA-1"})), **kwargs))
    monkeypatch.setattr(pa, "issue_pa", pa_client.issue_pa)
    assert generate(pa_flow).json()["status"] == "verification_required"
    assert all(x["groupPaNumber"] == "PA-1" and x["paNumber"] is None for x in doc["paGeneration"]["lines"])
    assert doc["paGeneration"]["attempts"][0]["response"]["httpStatus"] == 500
    assert generate(pa_flow).status_code == 409



def test_request_echo_is_not_primary_acceptance(pa_flow, monkeypatch):
    _, _, doc, _, _ = pa_flow
    async def echo(payload):
        return {"data": {**payload, "PANumber": "PA-1"}, "evidence": {}}
    monkeypatch.setattr(pa, "issue_pa", echo)
    generate(pa_flow)
    assert all(x["status"] == "verification_required" for x in doc["paGeneration"]["lines"])


@pytest.mark.parametrize("field,value", [("quantity", 9), ("diagnosisCode", "J069")])
def test_retry_blocks_medication_input_drift(pa_flow, monkeypatch, field, value):
    _, _, doc, _, _ = pa_flow
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(side_effect=httpx.ReadTimeout("unknown")))
    generate(pa_flow)
    # Simulate explicitly reconciled absence, without any production action.
    for line in doc["paGeneration"]["lines"]:
        line["status"] = "failed_retryable"
    doc["medications"][0][field] = value
    assert generate(pa_flow).status_code == 409


@pytest.mark.parametrize("fields,valid,expected", [
    ({"isprincipal": True}, True, "0"),
    ({"isprincipal": True, "dependantid": 0}, True, "0"),
    ({"isprincipal": False, "dependantid": 1}, True, "1"),
    ({"isprincipal": False, "dependantid": 3}, True, "3"),
    ({"isprincipal": False, "dependantid": True}, False, None),
    ({"isprincipal": False, "dependantnumber": 1}, False, None),
    ({"isprincipal": False, "dependantid": 1, "dependantnumber": 3}, False, None),
    ({"isprincipal": "0", "dependantid": "03"}, True, "3"),
    ({}, False, None),
    ({"dependantid": 0}, False, None),
    ({"dependantid": 2}, False, None),
    ({"isprincipal": None, "dependantid": 0}, False, None),
    ({"isprincipal": "unknown", "dependantid": 0}, False, None),
    ({"isprincipal": False, "dependantid": -1}, False, None),
    ({"isprincipal": False}, False, None),
    ({"isprincipal": True, "dependantid": 2}, False, None),
    ({"isprincipal": False, "dependantid": 0}, False, None),
    ({"dependantid": 2, "dependentNumber": 3}, False, None),
    ({"dependantid": "unknown"}, False, None),
    ({"isprincipal": False, "dependantid": 2, "isterminated": 1}, False, None),
    ({"isprincipal": False, "dependantid": 2, "isdeleted": "true"}, False, None),
    ({"isprincipal": False, "dependantid": 2, "isterminated": None}, False, None),
    ({"isprincipal": False, "dependantid": 2, "isdeleted": "unknown"}, False, None),
    ({"isprincipal": False, "dependantid": 2, "memberid": None}, False, None),
])
@pytest.mark.asyncio
async def test_authoritative_member_identity(monkeypatch, fields, valid, expected):
    monkeypatch.setenv("MEDICLOUD_API_URL", "https://lookup.test")
    monkeypatch.setenv("MEDICLOUD_API_KEY", "test-key")
    row = {"legacycode": "M1", "memberid": 42, "group_legacy_id": "AVO", "isterminated": 0, "isdeleted": 0, **fields}
    original = httpx.AsyncClient
    monkeypatch.setattr(pa_client.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"found": True, "count": 1, "results": [row]})), **kwargs))
    if valid:
        assert (await pa_client.get_member_info("M1"))["dependant_number"] == expected
    else:
        with pytest.raises(ValueError):
            await pa_client.get_member_info("M1")


def test_b2_top_level_status_echo_never_accepts(pa_flow, monkeypatch):
    _, _, doc, _, _ = pa_flow
    async def echo(payload):
        return {"data": {**payload, "PANumber": "PA-1", "Status": "AUTHORIZED"}, "evidence": {"httpStatus": 200}}
    monkeypatch.setattr(pa, "issue_pa", echo)
    assert generate(pa_flow).json()["status"] == "verification_required"
    assert all(x["status"] == "verification_required" for x in doc["paGeneration"]["lines"])


@pytest.mark.parametrize("key", ["ID", "Id", "AdmissionCode", "PAReference"])
def test_unconfirmed_reference_keys_never_trusted(pa_flow, monkeypatch, key):
    client, _, doc, _, _ = pa_flow
    monkeypatch.setenv("PA_RECOVERY_STAFF_IDS", "staff")
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(return_value={"data": {key: "PA-1"}, "evidence": {"httpStatus": 200}}))
    generate(pa_flow)
    assert all(x["paNumber"] is None and x["groupPaNumber"] is None for x in doc["paGeneration"]["lines"])
    body = {"resolution": "confirmed_no_pa", "evidence": "Parser did not recognize a reference", "confirmNoPaCreated": True,
            "verificationMethod": "parser", "checkedWith": "response", "verifiedAt": datetime.now(timezone.utc).isoformat()}
    response = client.post(f'/api/orders/{doc["_id"]}/pa-lines/a/verify', json=body)
    assert response.status_code == 422
    assert generate(pa_flow).status_code == 409


@pytest.mark.parametrize("granted,status", [(4000, "generated"), ("4000.00", "generated"), (0, "verification_required"), (3500, "verification_required"), ("NaN", "verification_required"), (True, "verification_required")])
def test_amount_granted_must_match(pa_flow, monkeypatch, granted, status):
    _, _, doc, _, _ = pa_flow
    data = {"PANumber": "PA-1", "Services": [{"ProcedureCode": "DRG-B", "Status": "ACCEPTED", "AmountGranted": granted}]}
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(return_value={"data": data, "evidence": {}}))
    generate(pa_flow)
    assert doc["paGeneration"]["lines"][1]["status"] == status


def test_cross_order_reference_blocked(pa_flow, monkeypatch):
    from bson import ObjectId
    client, db, doc, _, _ = pa_flow
    monkeypatch.setenv("PA_RECOVERY_STAFF_IDS", "staff")
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(side_effect=httpx.ReadTimeout("unknown")))
    generate(pa_flow)
    db.orders.rows.append({"_id": ObjectId(), "paGeneration": {"lines": [{"paNumber": "PA-OTHER"}]}})
    result = client.post(f'/api/orders/{doc["_id"]}/pa-lines/a/verify', json={"resolution": "existing_pa", "paNumber": "PA-OTHER", "evidence": "Verified individual service"})
    assert result.status_code == 409


@pytest.mark.parametrize("field,value", [("group_legacy_id", "OTHER"), ("dependant_number", "2"), ("member_id", "43")])
def test_material_member_drift_blocks_retry(pa_flow, monkeypatch, field, value):
    _, _, doc, _, _ = pa_flow
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(side_effect=httpx.ReadTimeout("unknown")))
    generate(pa_flow)
    for line in doc["paGeneration"]["lines"]:
        line["status"] = "failed_retryable"
    identity = {"group_legacy_id": "AVO", "dependant_number": "0", "member_id": "42", field: value}
    monkeypatch.setattr(pa, "get_member_info", AsyncMock(return_value=identity))
    assert generate(pa_flow).status_code == 409


def test_rejected_and_delivery_resolution_are_audited(pa_flow, monkeypatch):
    client, _, doc, _, _ = pa_flow
    doc.update(fulfillmentType="delivered", deliveryFee=500, overallTotal=7500)
    monkeypatch.setenv("PA_RECOVERY_STAFF_IDS", "staff")
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(return_value={"data": {"PANumber": "PA-1", "Services": [
        {"ProcedureCode": "DRG-A", "Status": "ACCEPTED"}, {"ProcedureCode": "DRG-B", "Status": "REJECTED"}]}, "evidence": {"httpStatus": 200}}))
    generate(pa_flow)
    base = f'/api/orders/{doc["_id"]}/pa-lines'
    result = client.post(base + '/b/verify', json={"resolution": "existing_pa", "paNumber": "PA-1", "evidence": "Service accepted after authoritative review"})
    assert result.status_code == 200
    assert "submissionGroupId" not in result.json()["line"]
    result = client.post(base + '/delivery-PRE11/verify', json={"resolution": "confirmed_rejected", "evidence": "Authoritative delivery rejection", "verificationMethod": "register", "checkedWith": "MediCloud", "verifiedAt": datetime.now(timezone.utc).isoformat()})
    assert result.status_code == 200
    assert result.json()["line"]["paNumber"] is None
    assert doc["history"][-1]["resolution"] == "confirmed_rejected"
    assert generate(pa_flow).status_code == 409


def test_authoritative_absence_requires_explicit_reconciliation(pa_flow, monkeypatch):
    client, _, doc, _, _ = pa_flow
    monkeypatch.setenv("PA_RECOVERY_STAFF_IDS", "staff")
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(return_value={"data": {"unknown": True}, "evidence": {"httpStatus": 200}}))
    generate(pa_flow)
    body = {"resolution": "confirmed_no_pa", "evidence": "Authoritative register confirmed this service was not created", "confirmNoPaCreated": True,
            "verificationMethod": "authoritative register", "checkedWith": "MediCloud operator", "verifiedAt": datetime.now(timezone.utc).isoformat(),
            "confirmAuthoritativeAbsence": True, "reconciliationReference": "Operator investigation ticket TEST-ONLY"}
    result = client.post(f'/api/orders/{doc["_id"]}/pa-lines/a/verify', json=body)
    assert result.status_code == 200
    assert doc["history"][-1]["confirmAuthoritativeAbsence"] is True
    assert generate(pa_flow).status_code == 409  # other group member remains unresolved



def test_reference_reserved_by_other_group_blocks_attachment(pa_flow, monkeypatch):
    client, db, doc, _, _ = pa_flow
    monkeypatch.setenv("PA_RECOVERY_STAFF_IDS", "staff")
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(side_effect=httpx.ReadTimeout("unknown")))
    generate(pa_flow)
    db.pa_reference_owners.rows.append({"_id": "PA-NEW", "orderId": "another-order", "submissionGroupId": "another-group"})
    result = client.post(f'/api/orders/{doc["_id"]}/pa-lines/a/verify', json={"resolution": "existing_pa", "paNumber": "PA-NEW", "evidence": "Verified individual service"})
    assert result.status_code == 409


def test_enrollee_identifier_drift_blocks_retry(pa_flow, monkeypatch):
    _, _, doc, _, _ = pa_flow
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(side_effect=httpx.ReadTimeout("unknown")))
    generate(pa_flow)
    for line in doc["paGeneration"]["lines"]:
        line["status"] = "failed_retryable"
    doc["enrollee"]["enrolleeId"] = "OTHER-MEMBER"
    assert generate(pa_flow).status_code == 409


def test_submission_identity_drift_blocks_retry(pa_flow, monkeypatch):
    _, _, doc, _, _ = pa_flow
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(side_effect=httpx.ReadTimeout("unknown")))
    generate(pa_flow)
    for line in doc["paGeneration"]["lines"]:
        line["status"] = "failed_retryable"
    doc["paGeneration"]["lines"][0]["submissionGroupId"] = "wrong-group"
    assert generate(pa_flow).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("count,truncated,rows", [(2, False, 2), (1, True, 1)])
async def test_ambiguous_lookup_member_blocks(monkeypatch, count, truncated, rows):
    monkeypatch.setenv("MEDICLOUD_API_URL", "https://lookup.test")
    monkeypatch.setenv("MEDICLOUD_API_KEY", "test-key")
    row = {"legacycode": "M1", "memberid": 42, "group_legacy_id": "AVO", "isterminated": 0, "isdeleted": 0, "isprincipal": True}
    original = httpx.AsyncClient
    monkeypatch.setattr(pa_client.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"found": True, "count": count, "truncated": truncated, "results": [row] * rows})), **kwargs))
    with pytest.raises(ValueError):
        await pa_client.get_member_info("M1")


@pytest.mark.parametrize("failure", ["timeout", "500", "400", "connection", "malformed", "unknown", "2xx"])
def test_every_transmitted_attempt_requires_current_authoritative_absence(pa_flow, monkeypatch, failure):
    from datetime import timedelta
    client, _, doc, _, calls = pa_flow
    monkeypatch.setenv("PA_RECOVERY_STAFF_IDS", "staff")
    async def issue(payload):
        calls.append(payload)
        if failure == "timeout":
            raise httpx.ReadTimeout("unknown")
        if failure in {"500", "400"}:
            raise httpx.HTTPStatusError("uncertain", request=httpx.Request("POST", "https://mock.test"), response=httpx.Response(int(failure)))
        if failure == "connection":
            raise httpx.ReadError("interrupted")
        if failure == "malformed":
            raise ValueError("unparseable")
        if failure == "unknown":
            raise RuntimeError("unknown outcome")
        return {"data": {"unknown": True}, "evidence": {"httpStatus": 200}}
    monkeypatch.setattr(pa, "issue_pa", issue)
    assert generate(pa_flow).json()["status"] == "verification_required"
    assert len(calls) == 1
    submitted = doc["paGeneration"]["lines"][0]["submittedAt"]
    body = {"resolution": "confirmed_no_pa", "evidence": "Authoritative investigation confirms all services were not created", "confirmNoPaCreated": True,
            "verificationMethod": "authoritative investigation", "checkedWith": "MediCloud register operator",
            "verifiedAt": datetime.now(timezone.utc).isoformat()}
    endpoint = f'/api/orders/{doc["_id"]}/pa-lines'
    for line_id in ("a", "b"):
        assert client.post(endpoint + '/' + line_id + '/verify', json=body).status_code == 422
    assert generate(pa_flow).status_code == 409 and len(calls) == 1
    strong = {**body, "confirmAuthoritativeAbsence": True, "reconciliationReference": "Authoritative investigation TEST-ONLY"}
    stale = {**strong, "verifiedAt": (submitted - timedelta(seconds=1)).isoformat()}
    for line_id in ("a", "b"):
        assert client.post(endpoint + '/' + line_id + '/verify', json=stale).status_code == 422
    assert generate(pa_flow).status_code == 409 and len(calls) == 1
    for line_id in ("a", "b"):
        assert client.post(endpoint + '/' + line_id + '/verify', json=strong).status_code == 200
    assert len(calls) == 1  # recording absence never submits
    assert generate(pa_flow).status_code == 200
    assert len(calls) == 2  # only explicit new generation after BOTH reconciliations


@pytest.mark.parametrize("missing_time", [False, True])
def test_interrupted_legacy_attempt_cannot_unlock_with_stale_evidence(pa_flow, monkeypatch, missing_time):
    from datetime import timedelta
    client, _, doc, _, calls = pa_flow
    monkeypatch.setenv("PA_RECOVERY_STAFF_IDS", "staff")
    monkeypatch.setattr(pa, "issue_pa", AsyncMock(side_effect=httpx.ReadTimeout("unknown")))
    generate(pa_flow)
    submitted = doc["paGeneration"]["lines"][0]["submittedAt"]
    for line in doc["paGeneration"]["lines"]:
        line.update(status="submitting", failureKind="interrupted_submission")
        if missing_time:
            line.pop("submittedAt", None)
    doc["paGeneration"].update(active=True, leaseUntil=datetime.now(timezone.utc) - timedelta(minutes=1))
    assert client.post(f'/api/orders/{doc["_id"]}/pa-interruption/mark-verification-required').status_code == 200
    body = {"resolution": "confirmed_no_pa", "evidence": "Authoritative investigation TEST", "confirmNoPaCreated": True,
            "verificationMethod": "register", "checkedWith": "authoritative source", "confirmAuthoritativeAbsence": True,
            "reconciliationReference": "TEST investigation", "verifiedAt": (submitted - timedelta(seconds=1)).isoformat()}
    result = client.post(f'/api/orders/{doc["_id"]}/pa-lines/a/verify', json=body)
    assert result.status_code == (409 if missing_time else 422)
    if missing_time:
        body["verifiedAt"] = datetime.now(timezone.utc).isoformat()
        assert client.post(f'/api/orders/{doc["_id"]}/pa-lines/a/verify', json=body).status_code == 409
    assert generate(pa_flow).status_code == 409
    assert not calls


@pytest.mark.parametrize("body", [
    {"status": "Success"},
    {"status": "SUCCESS", "PANumber": "PA-123"},
    {"status": "success", "response": {"unknown": "PA-123"}},
])
def test_group_success_never_implies_service_acceptance(pa_flow, monkeypatch, body):
    _, _, doc, _, calls = pa_flow
    async def upstream(payload):
        calls.append(payload)
        return {"data": body, "evidence": {"httpStatus": 200}}
    monkeypatch.setattr(pa, "issue_pa", upstream)
    assert generate(pa_flow).json()["status"] == "verification_required"
    assert len(calls) == 1
    assert all(line["status"] == "verification_required" for line in doc["paGeneration"]["lines"])
    assert generate(pa_flow).status_code == 409
    assert len(calls) == 1


def test_header_credentials_redacted_from_attempt_and_public_state(pa_flow, monkeypatch):
    import json
    _, _, doc, _, _ = pa_flow
    username, password = pa_client._credentials()
    async def upstream(payload):
        response = httpx.Response(200, json={"status": "Success", "username": username, "password": password})
        return {"data": response.json(), "evidence": pa_client.response_evidence(response)}
    monkeypatch.setattr(pa, "issue_pa", upstream)
    result = generate(pa_flow)
    private = json.dumps(doc["paGeneration"], default=str)
    assert username not in private and password not in private
    assert username not in result.text and password not in result.text
    assert "<redacted>" in private
