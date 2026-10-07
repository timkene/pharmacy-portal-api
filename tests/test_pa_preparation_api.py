"""Real FastAPI boundary, Phase 1 fake transactional database; sockets blocked."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api import pa_preparation as api
from core import pa_client, pa_preparation as domain
from core.security import encode_session
from test_pa_preparation import flow, Collection, Cursor, DB, matches as old_matches


# The Phase 1 fake has only equality queries. Extend it for actual paginated API queries.
def match(doc, query):
    for key, value in query.items():
        if isinstance(value, dict) and "$gt" in value:
            if doc.get(key) is None or doc[key] <= value["$gt"]: return False
        elif doc.get(key) != value:
            return False
    return True


@pytest.fixture
def team(flow, monkeypatch):
    # pytest-asyncio supplies the existing async transactional fixture.
    async def find_one(self, query, **kwargs):
        return next((deepcopy(r) for r in self.rows if match(r, query)), None)
    def find(self, query, **kwargs):
        return Cursor([r for r in self.rows if match(r, query)])
    def sort(self, key, direction):
        self.rows.sort(key=lambda r: r[key], reverse=direction < 0)
        return self
    def limit(self, count):
        self.rows = self.rows[:count]
        return self
    monkeypatch.setattr(Collection, "find_one", find_one)
    monkeypatch.setattr(Collection, "find", find)
    monkeypatch.setattr(Cursor, "sort", sort, raising=False)
    monkeypatch.setattr(Cursor, "limit", limit, raising=False)
    staff_id = ObjectId()
    flow.db.collections["staff_users"] = Collection()
    flow.db.staff_users.rows.append({"_id": staff_id, "permissions": [api.READ, api.WRITE]})
    monkeypatch.setenv("SESSION_SECRET", "test-preparation-session-secret-" * 3)
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.get_db] = lambda: flow.db
    async def source():
        async def resolve(order): return flow.evidence
        return resolve
    app.dependency_overrides[api.member_validation_source] = source
    with TestClient(app) as client:
        client.cookies.set("staff_session", encode_session({"role": "staff", "userId": str(staff_id), "permissions": [api.READ, api.WRITE]}))
        yield SimpleNamespace(client=client, flow=flow, app=app, staff=flow.db.staff_users.rows[0],
                              base=f'/api/pa-preparation/batches/{flow.batch["_id"]}')


def prepare(team, version=0, action="prepare"):
    return team.client.post(f'{team.base}/orders/{team.flow.order["_id"]}/{action}', json={"expectedVersion": version})


@pytest.mark.parametrize("path,method", [("", "get"), ("", "post"), ("/lines", "get"), ("/close", "post")])
def test_unauthenticated(team, path, method):
    team.client.cookies.clear()
    url = "/api/pa-preparation/batches" if method == "post" and not path else team.base + path
    result = getattr(team.client, method)(url)
    assert result.status_code == 401


@pytest.mark.parametrize("permissions,method,status", [([], "get", 403), ([api.WRITE], "get", 403), ([api.READ], "post", 403)])
def test_permissions_from_database(team, permissions, method, status):
    team.staff["permissions"] = permissions
    result = team.client.get(team.base) if method == "get" else prepare(team)
    assert result.status_code == status


def test_permissions_not_accepted_in_json(team):
    result = team.client.post('/api/pa-preparation/batches', json={"businessDate": "2026-10-07", "canPrepare": True, "permissions": [api.WRITE]})
    assert result.status_code == 422
    result = team.client.post(f'{team.base}/orders/{team.flow.order["_id"]}/prepare', json={"expectedVersion": 0, "memberEvidence": team.flow.evidence.model_dump(mode="json")})
    assert result.status_code == 422


def test_create_list_get_batch(team):
    result = team.client.post('/api/pa-preparation/batches', json={"businessDate": "2026-10-07"})
    assert result.status_code == 200
    batch_id = result.json()["batchId"]
    assert team.client.get(f'/api/pa-preparation/batches/{batch_id}').status_code == 200
    listing = team.client.get('/api/pa-preparation/batches?limit=1').json()
    assert len(listing["items"]) == 1 and listing["nextCursor"]
    assert len(team.client.get('/api/pa-preparation/batches', params={"limit": 1, "cursor": listing["nextCursor"]}).json()["items"]) == 1


def test_prepare_recalculate_close(team):
    first = prepare(team)
    assert first.status_code == 200, first.text
    assert first.json()["items"][0]["preparationStatus"] == "READY"
    second = prepare(team, 1, "recalculate")
    assert second.status_code == 200
    assert first.json()["items"][0]["preparationLineId"] == second.json()["items"][0]["preparationLineId"]
    assert team.client.post(team.base + '/close', json={"expectedVersion": 2}).status_code == 200
    assert team.flow.db.pa_preparation_contexts.rows == []


def test_ownership_conflict(team):
    assert prepare(team).status_code == 200
    other = team.client.post('/api/pa-preparation/batches', json={"businessDate": "2026-10-07"}).json()["batchId"]
    result = team.client.post(f'/api/pa-preparation/batches/{other}/orders/{team.flow.order["_id"]}/prepare', json={"expectedVersion": 0})
    assert result.status_code == 409
    assert result.json()["detail"]["owningBatchId"] == team.flow.batch["_id"]


def test_missing_namespace_review_required(team):
    team.flow.db.orders.rows[0]["medications"][0].pop("sourceNamespaceId")
    item = prepare(team).json()["items"][0]
    assert item["preparationStatus"] == "REVIEW_REQUIRED"
    assert item["mapping"]["sourceNamespaceId"] is None
    assert "SOURCE_NAMESPACE_OR_IDENTITY_REQUIRED" in item["reasonCodes"]


def test_default_member_validation_unavailable(team):
    team.app.dependency_overrides.pop(api.member_validation_source)
    item = prepare(team).json()["items"][0]
    assert item["preparationStatus"] == "BLOCKED" and not item["submissionEligible"]
    assert "MEMBER_VALIDATION_UNAVAILABLE" in item["reasonCodes"]
    assert item["validation"]["memberFreshUntil"] is None


def test_server_evidence_exception_fails_closed(team):
    async def source():
        async def fail(order): raise RuntimeError("secret upstream evidence")
        return fail
    team.app.dependency_overrides[api.member_validation_source] = source
    result = prepare(team)
    assert result.status_code == 200
    assert result.json()["items"][0]["preparationStatus"] == "BLOCKED"
    assert "secret upstream" not in result.text


def test_receipt_freshness_comes_from_current_evidence(team):
    first = prepare(team).json()["items"][0]
    old = team.flow.evidence
    team.flow.evidence = old.model_copy(update={"evidenceId": "new", "validatedAt": old.validatedAt + timedelta(seconds=1),
                                              "expiresAt": old.expiresAt + timedelta(seconds=1)})
    refreshed = prepare(team, 1, "recalculate").json()["items"][0]
    assert refreshed["preparationRevision"] == first["preparationRevision"]
    assert refreshed["validation"]["memberFreshUntil"] != first["validation"]["memberFreshUntil"]
    assert team.flow.db.pa_preparation_lines.rows[0]["memberSnapshot"]["expiresAt"] == old.expiresAt
    team.app.dependency_overrides.pop(api.member_validation_source)
    item = team.client.get(team.base + '/lines').json()["items"][0]
    assert item["validation"]["member"] == "UNAVAILABLE" and not item["submissionEligible"]


@pytest.mark.parametrize("field,value", [("procedureCode", 123), ("procedureCode", {}), ("name", None), ("name", []), ("winnerId", {}), ("winnerId", 7)])
def test_malformed_legacy_rows_block_not_crash(team, field, value):
    order = team.flow.db.orders.rows[0]
    if field == "winnerId": order[field] = value
    else: order["medications"][0][field] = value
    result = prepare(team)
    assert result.status_code == 200, result.text
    assert result.json()["items"][0]["preparationStatus"] == "BLOCKED"


def test_projection_minimized_and_transmission_untouched(team):
    order = team.flow.db.orders.rows[0]
    order["paGeneration"] = {"lines": [{"lineId": "line-1", "status": "generated", "paNumber": "PA-SAFE"}],
        "attempts": [{"response": {"rawBody": "SECRET RESPONSE", "password": "SECRET PASSWORD"}}], "token": "SECRET TOKEN"}
    before = deepcopy(team.flow.db.orders.rows)
    result = prepare(team)
    item = result.json()["items"][0]
    assert item["transmission"]["confirmedPaReference"] == "PA-SAFE"
    for value in ("SECRET", "Must not persist", "inputFingerprint", "memberSnapshot", "enrolleeFingerprint", "groupLegacyId", "phone", "nin"):
        assert value not in result.text
    assert team.flow.db.orders.rows == before
    assert team.flow.db.pa_reference_owners.rows == []
    assert not item["submissionEligible"]


@pytest.mark.parametrize("state", ["verification_required", "submitting", "failed_retryable", "failed_rejected"])
def test_transmission_state_read_only(team, state):
    order = team.flow.db.orders.rows[0]
    order["paGeneration"] = {"lines": [{"lineId": "line-1", "status": state}]}
    before = deepcopy(order)
    item = prepare(team).json()["items"][0]
    assert item["transmission"]["state"] == state
    assert item["transmission"]["confirmedPaReference"] is None
    assert not item["submissionEligible"]
    assert order == before


def test_line_filters_and_pagination(team):
    assert prepare(team).status_code == 200
    row = deepcopy(team.flow.db.pa_preparation_lines.rows[0])
    # A removed historical line remains visible as stale, not READY.
    row.update(_id=str(uuid4()), orderLineId="removed-line", isCurrent=False)
    team.flow.db.pa_preparation_lines.rows.append(row)
    first = team.client.get(team.base + '/lines?limit=1').json()
    assert len(first["items"]) == 1 and first["nextCursor"]
    second = team.client.get(team.base + '/lines', params={"limit": 1, "cursor": first["nextCursor"]}).json()
    assert len(second["items"]) == 1 and not second["nextCursor"]
    assert second["items"][0]["preparationLineId"] != first["items"][0]["preparationLineId"]
    ready = team.client.get(team.base + '/lines?status=READY&mappingStatus=ACTIVE&stale=false').json()
    assert len(ready["items"]) == 1
    assert team.client.get(team.base + '/lines?aggregatorId=unknown').json()["items"] == []
    assert team.client.get(team.base + '/lines?providerId=unknown').json()["items"] == []


def test_bad_request_and_not_found(team):
    assert team.client.get('/api/pa-preparation/batches/' + str(uuid4())).status_code == 404
    assert team.client.get(team.base + '/lines?status=arbitrary').status_code == 422
    assert team.client.get(team.base + '/lines?limit=10000').status_code == 422
    assert prepare(team, version=99).status_code == 409
    assert team.client.post(team.base + '/orders/invalid/prepare', json={"expectedVersion": 0}).status_code == 404


def test_no_privileged_or_transmission_routes(team):
    paths = {route.path for route in api.router.routes}
    assert len(api.router.routes) == 7
    assert not any(word in path for path in paths for word in ('generate', 'submit', 'trust', 'import', 'export', 'retry', 'recover', 'mapping'))


@pytest.mark.parametrize("path,method", [("/lines", "get"), ("/close", "post"), ("/orders/invalid/prepare", "post"), ("/orders/invalid/recalculate", "post")])
def test_every_operation_requires_staff(team, path, method):
    team.client.cookies.clear()
    response = getattr(team.client, method)(team.base + path)
    assert response.status_code == 401


def test_signed_claims_cannot_grant_permissions(team):
    team.staff["permissions"] = []
    # Cookie contains the permissions already; database denial must win.
    assert team.client.get(team.base).status_code == 403
    assert prepare(team).status_code == 403


def test_wrong_role_and_deleted_staff_rejected(team):
    team.client.cookies.set("staff_session", encode_session({"role": "aggregator", "userId": str(team.staff["_id"])}))
    assert team.client.get(team.base).status_code == 401
    team.client.cookies.set("staff_session", encode_session({"role": "staff", "userId": str(ObjectId())}))
    assert team.client.get(team.base).status_code == 401


def test_conflicting_owner_hidden_from_write_only_user(team):
    assert prepare(team).status_code == 200
    other = team.client.post('/api/pa-preparation/batches', json={"businessDate": "2026-10-07"}).json()["batchId"]
    team.staff["permissions"] = [api.WRITE]
    response = team.client.post(f'/api/pa-preparation/batches/{other}/orders/{team.flow.order["_id"]}/prepare', json={"expectedVersion": 0})
    assert response.status_code == 409
    assert response.json()["detail"]["owningBatchId"] is None


def test_invalid_payload_does_not_echo_secrets(team):
    response = team.client.post('/api/pa-preparation/batches', json={"businessDate": "invalid", "password": "MUST-NOT-ECHO"})
    assert response.status_code == 422
    assert "MUST-NOT-ECHO" not in response.text


def test_storage_error_is_safe(team, monkeypatch):
    from pymongo.errors import OperationFailure
    async def fail(*args, **kwargs): raise OperationFailure("SQL/MONGO secret diagnostics")
    monkeypatch.setattr(team.flow.db.pa_preparation_batches, "find_one", fail)
    response = team.client.get(team.base)
    assert response.status_code == 503
    assert "secret" not in response.text


def test_closed_batch_returns_stale_and_cannot_prepare(team):
    assert prepare(team).status_code == 200
    assert team.client.post(team.base + '/close', json={"expectedVersion": 1}).status_code == 200
    item = team.client.get(team.base + '/lines').json()["items"][0]
    assert item["isStale"] and not item["submissionEligible"]
    assert prepare(team, 2).status_code == 409


def test_stale_source_changes_not_reported_ready(team):
    assert prepare(team).status_code == 200
    team.flow.db.orders.rows[0]["medications"][0]["quantity"] += 1
    item = team.client.get(team.base + '/lines').json()["items"][0]
    assert item["isStale"] and item["preparationStatus"] == "REVIEW_REQUIRED"
    assert not item["submissionEligible"]
    assert "PREPARATION_STALE" in item["reasonCodes"]


def test_expired_current_evidence_not_fresh(team):
    assert prepare(team).status_code == 200
    old = team.flow.evidence
    team.flow.evidence = old.model_copy(update={"validatedAt": old.validatedAt - timedelta(hours=2), "expiresAt": old.expiresAt - timedelta(hours=2)})
    item = team.client.get(team.base + '/lines').json()["items"][0]
    assert item["validation"]["member"] == "INVALID"
    assert item["validation"]["memberFreshUntil"] is None
    assert item["preparationStatus"] == "BLOCKED" and not item["submissionEligible"]


def test_read_routes_do_not_write(team):
    assert prepare(team).status_code == 200
    before = {name: deepcopy(col.rows) for name, col in team.flow.db.collections.items()}
    for path in ['/api/pa-preparation/batches', team.base, team.base + '/lines']:
        assert team.client.get(path).status_code == 200
    assert before == {name: col.rows for name, col in team.flow.db.collections.items()}


def test_no_transmission_import_in_router():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(api.__file__).read_text())
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) and 'issue_pa' in ast.unparse(node) for node in ast.walk(tree))


def test_bounded_scan_resumes_when_filter_returns_no_rows(team):
    assert prepare(team).status_code == 200
    original = team.flow.db.pa_preparation_lines.rows[0]
    team.flow.db.pa_preparation_lines.rows = [{**deepcopy(original), "_id": str(uuid4()), "orderLineId": f"old-{n}", "isCurrent": False} for n in range(101)]
    response = team.client.get(team.base + '/lines?status=READY').json()
    assert response["items"] == [] and response["nextCursor"]
    last = team.client.get(team.base + '/lines', params={"status": "READY", "cursor": response["nextCursor"]}).json()
    assert last["items"] == [] and last["nextCursor"] is None


def test_mapping_candidate_state_visible_and_filterable(team):
    head = team.flow.db.pa_mapping_heads.rows[0]
    head["activeVersion"] = None
    head["version"] = 1
    item = prepare(team).json()["items"][0]
    assert item["mapping"]["mappingStatus"] == "PROPOSED"
    assert item["mapping"]["mappedMediCloudCode"] is None
    assert len(team.client.get(team.base + '/lines?mappingStatus=PROPOSED').json()["items"]) == 1
    assert team.client.get(team.base + '/lines?mappingStatus=ACTIVE').json()["items"] == []


def test_nested_response_models_reject_phi():
    from pydantic import ValidationError
    from models.pa_preparation_api import ProviderView
    with pytest.raises(ValidationError):
        ProviderView(referringProviderId=None, referringProviderDisplay=None, aggregatorId=None,
            aggregatorDisplay=None, issuePaProviderId=None, memberName="Forbidden")


def test_batch_counts_are_explicitly_as_of_not_live(team):
    assert prepare(team).status_code == 200
    team.flow.db.orders.rows[0]["version"] += 1
    response = team.client.get(team.base + '/lines').json()
    assert response["countsScope"] == "LAST_CALCULATION"
    assert response["counts"]["ready"] == 1
    assert response["pageCounts"]["stale"] == 1
    assert response["counts"]["submitted"] is None
    assert not response["items"][0]["submissionEligible"]



def test_write_only_mutations_and_no_standalone_reads(team):
    team.staff["permissions"] = [api.WRITE]
    assert team.client.post('/api/pa-preparation/batches', json={"businessDate": "2026-10-07"}).status_code == 200
    assert prepare(team).status_code == 200
    assert prepare(team, 1, "recalculate").status_code == 200
    for path in ['/api/pa-preparation/batches', team.base, team.base + '/lines']:
        assert team.client.get(path).status_code == 403
    version = team.flow.db.pa_preparation_batches.rows[0]["version"]
    assert team.client.post(team.base + '/close', json={"expectedVersion": version}).status_code == 200


def test_mapping_head_does_not_override_failed_line_validation(team):
    assert prepare(team).status_code == 200
    team.flow.db.orders.rows[0]["medications"][0]["name"] = "Changed source identity"
    item = team.client.get(team.base + '/lines').json()["items"][0]
    assert item["mapping"]["mappingStatus"] != "ACTIVE"
    assert item["mapping"]["mappedMediCloudCode"] is None
    assert item["mapping"]["mappedMediCloudDescription"] is None
    assert team.client.get(team.base + '/lines?mappingStatus=REVIEW_REQUIRED').status_code == 422


@pytest.mark.parametrize("ledger", [
    "malformed", {"lines": None}, {"lines": [None]},
    {"lines": [{"lineId": "line-1", "status": "pending"}] * 2},
    {"lines": [{"lineId": "line-1", "status": "unknown"}]},
    {"lines": [{"lineId": "line-1", "status": "generated"}]},
    {"lines": [{"lineId": "line-1", "status": "pending", "paNumber": "unsafe"}]},
])
def test_invalid_ledger_projects_unknown_without_writes(team, ledger):
    team.flow.db.orders.rows[0]["paGeneration"] = deepcopy(ledger)
    before = deepcopy(team.flow.db.orders.rows)
    item = prepare(team).json()["items"][0]
    assert item["transmission"] == {"state": "UNKNOWN", "verificationRequired": True, "confirmedPaReference": None}
    assert item["preparationStatus"] == "BLOCKED"
    assert "PA_LEDGER_INVALID" in item["reasonCodes"]
    assert team.flow.db.orders.rows == before
    assert team.flow.db.pa_reference_owners.rows == []


@pytest.mark.parametrize("corruption", [{"revision": "unsafe"}, {"serviceInput": None}, {"calculatedAt": {"secret": "DO-NOT-EXPOSE"}}])
def test_bad_row_preserves_other_page_rows(team, corruption):
    assert prepare(team).status_code == 200
    original = team.flow.db.pa_preparation_lines.rows[0]
    rows = [{**deepcopy(original), "_id": f"00000000-0000-0000-0000-{i:012d}"} for i in (1, 2, 3)]
    rows[1]["orderLineId"] = "malformed-stored-line"
    rows[1].update(corruption)
    team.flow.db.pa_preparation_lines.rows = rows
    response = team.client.get(team.base + '/lines')
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 3
    assert items[0]["preparationStatus"] == items[2]["preparationStatus"] == "READY"
    assert items[1]["preparationStatus"] == "BLOCKED"
    assert items[1]["reasonCodes"] == ["PREPARATION_ROW_INVALID"]
    assert not items[1]["submissionEligible"]
    assert "DO-NOT-EXPOSE" not in response.text
