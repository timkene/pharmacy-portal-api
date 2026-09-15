import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from core import review_flags as rf

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)


@pytest.fixture
def medicloud(monkeypatch):
    state = {"benefits": [{"benefit_name": "DRG", "limit_amount": 50000,
                           "total_used": 30000, "amount_left": 20000}], "claims": [], "calls": []}
    original = httpx.AsyncClient
    def handle(request):
        state["calls"].append(request)
        assert request.headers["X-API-Key"] == "test-key"
        if request.url.path == "/enrollee-utilization/by-benefit":
            assert request.method == "POST"
            assert json.loads(request.content) == {"enrollee_id": "E1", "start_date": "2026-01-01", "end_date": "2026-09-14"}
            return httpx.Response(state.get("status", 200), json={"benefits": state["benefits"]})
        assert request.url.path == "/claims-lookup"
        assert dict(request.url.params) == {"enrollee_id": "E1", "from_date": "2026-08-24", "to_date": "2026-09-14"}
        return httpx.Response(200, json=state.get("claims_payload", {state.get("claims_envelope", "results"): state["claims"]}))
    monkeypatch.setenv("MEDICLOUD_API_KEY", "test-key")
    monkeypatch.setenv("MEDICLOUD_API_URL", "https://medicloud.test")
    monkeypatch.setattr(rf.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("terminated,remaining,age,code,expected", [
    (False, 20000, None, "DRG1084", []),
    (True, 20000, None, "DRG1084", ["ENROLLEE_TERMINATED"]),
    (False, 14999, None, "DRG1084", ["LOW_MEDICATION_BENEFIT"]),
    (False, 15000, None, "DRG1084", []),
    (False, 20000, 20, "DRG1084", ["MEDICATION_USED_WITHIN_LAST_21_DAYS"]),
    (False, 20000, 21, "DRG1084", ["MEDICATION_USED_WITHIN_LAST_21_DAYS"]),
    (False, 20000, 22, "DRG1084", []),
    (False, 20000, 20, "CON001", []),
    (True, 14999, 20, "DRG1084", ["ENROLLEE_TERMINATED", "LOW_MEDICATION_BENEFIT", "MEDICATION_USED_WITHIN_LAST_21_DAYS"]),
    (False, None, None, "DRG1084", []),
])
async def test_flags(medicloud, terminated, remaining, age, code, expected):
    medicloud["benefits"][0]["amount_left"] = remaining
    if age is not None:
        medicloud["claims"] = [{"encounter_date_from": (NOW - timedelta(days=age)).isoformat(),
                                "procedure_code": code, "approved_amount": 0, "charge_amount": 100,
                                "description": "Drug", "provider": "Clinic"}]
    result = await rf.compute_review_flags({"enrolleeId": "E1", "isterminated": terminated}, NOW)
    assert result["codes"] == expected
    assert len(medicloud["calls"]) == 2
    if result["recent_medication"]["flagged"]:
        assert result["recent_medication"]["items"][0]["amount"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("termination,flagged", [("2026-09-13", True), ("2026-09-14", False), ("2027-01-01", False), (None, False), ("", False), ("invalid", False)])
async def test_termination_date(medicloud, termination, flagged):
    result = await rf.compute_review_flags({"enrolleeId": "E1", "terminationDate": termination}, NOW)
    assert result["enrollee_status"]["flagged"] is flagged


@pytest.mark.asyncio
async def test_failure_is_independent(medicloud):
    medicloud["status"] = 503
    result = await rf.compute_review_flags({"enrolleeId": "E1", "isterminated": True}, NOW)
    assert "error" in result["medication_benefit"]
    assert "error" not in result["recent_medication"]
    assert result["codes"] == ["ENROLLEE_TERMINATED", "REVIEW_CHECKS_INCOMPLETE"]


@pytest.mark.asyncio
async def test_unknown_claims_shape_marks_incomplete(medicloud):
    medicloud["claims_envelope"] = "data"
    result = await rf.compute_review_flags({"enrolleeId": "E1", "isterminated": False}, NOW)
    assert result["codes"] == ["REVIEW_CHECKS_INCOMPLETE"]
    assert "error" in result["recent_medication"]
    assert not result["recent_medication"]["flagged"]


def test_benefit_preference_and_utilization():
    result = rf.medication_benefit({"benefits": [
        {"benefit_name": "pharmacy", "amount_left": 1},
        {"benefit_name": "drg", "amount_left": 20000, "claims_used": 10, "unclaimed_pa_used": 20}]})
    assert result["benefit_name"] == "drg"
    assert result["utilized_amount"] == 30
    assert not result["flagged"]


@pytest.mark.parametrize("code", [" drg1084 ", "MED1", "PRE1", "BRG1", "NHIA-17-1"])
def test_drug_families(code):
    assert rf.is_medication(code)


def test_recent_medication_reads_medicloud_results_key():
    start = NOW.date() - timedelta(days=21)
    items = rf.recent_medication(
        {"results": [{"encounter_date_from": NOW.isoformat(), "procedure_code": "DRG1",
                      "approved_amount": 10, "provider_name": "Clinic"}]},
        start,
        NOW.date(),
    )
    assert items[0]["provider"] == "Clinic"
    assert items[0]["amount"] == 10


def test_unknown_claims_envelope_is_incomplete():
    start = NOW.date() - timedelta(days=21)
    try:
        rf.recent_medication({"data": []}, start, NOW.date())
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert rf.recent_medication({"results": []}, start, NOW.date()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, "", 0, {}, {"results": {}}, {"results": ""},
                                          {"results": None}, {"claims": {}}, {"claims": ""},
                                          {"results": [None]}, {"results": ["invalid"]}])
async def test_malformed_claims_preserve_other_flags(medicloud, payload):
    medicloud["claims_payload"] = payload
    medicloud["benefits"][0]["amount_left"] = 100
    result = await rf.compute_review_flags({"enrolleeId": "E1", "isterminated": True}, NOW)
    assert result["codes"] == ["ENROLLEE_TERMINATED", "LOW_MEDICATION_BENEFIT", "REVIEW_CHECKS_INCOMPLETE"]
    assert "error" in result["recent_medication"]
    assert result["medication_benefit"]["remaining_amount"] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_date", [None, "", "invalid", "2026-02-30", {}, 123])
@pytest.mark.parametrize("include_valid", [False, True])
async def test_invalid_medication_date_preserves_valid_matches(medicloud, invalid_date, include_valid):
    medicloud["claims"] = [{"procedure_code": "DRG1", "encounter_date_from": invalid_date}]
    if include_valid:
        medicloud["claims"].append({"procedure_code": "DRG2", "encounter_date_from": NOW.isoformat()})
    result = await rf.compute_review_flags({"enrolleeId": "E1"}, NOW)
    expected = ["MEDICATION_USED_WITHIN_LAST_21_DAYS"] if include_valid else []
    assert result["codes"] == expected + ["REVIEW_CHECKS_INCOMPLETE"]
    assert "error" in result["recent_medication"]
    assert result["recent_medication"]["flagged"] is include_valid
    assert len(result["recent_medication"]["items"]) == int(include_valid)
