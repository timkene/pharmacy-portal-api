"""Phase 1 contracts with transaction-aware in-memory Mongo; sockets forbidden."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bson import ObjectId
from pymongo.errors import DuplicateKeyError
import pytest
import pytest_asyncio

from core import pa_client, pa_mapping, pa_preparation as prep
from core.pa_preparation_store import COLLECTIONS, ensure_preparation_indexes
from models.pa_preparation import (Actor, CatalogEvidence, MappingIdentity, MappingProposal,
                                   MemberEvidence, ReviewEvidence, CatalogValidationEvidence)


def matches(doc, query):
    return all((doc.get(k) is not None and type(doc[k]) is type(v["$gt"]) and doc[k] > v["$gt"])
               if isinstance(v, dict) and "$gt" in v else doc.get(k) == v for k, v in query.items())


class Cursor:
    def __init__(self, rows):
        self.rows = deepcopy(rows)

    def sort(self, key, direction):
        self.rows.sort(key=lambda row: row[key], reverse=direction < 0)
        return self

    def limit(self, count):
        self.rows = self.rows[:count]
        return self

    async def to_list(self, length):
        return deepcopy(self.rows if length is None else self.rows[:length])


class Collection:
    def __init__(self):
        self.rows, self.indexes = [], []
        self.fail_insert = False

    async def create_index(self, keys, **options):
        if (keys, options) not in self.indexes:
            self.indexes.append((keys, options))

    async def find_one(self, query, **kwargs):
        return next((deepcopy(r) for r in self.rows if matches(r, query)), None)

    def find(self, query, **kwargs):
        return Cursor([r for r in self.rows if matches(r, query)])

    async def insert_one(self, doc, **kwargs):
        if self.fail_insert:
            raise RuntimeError("injected persistence failure")
        if any(r["_id"] == doc["_id"] for r in self.rows):
            raise DuplicateKeyError("duplicate id")
        for keys, options in self.indexes:
            partial = options.get("partialFilterExpression", {})
            if options.get("unique") and matches(doc, partial):
                if any(matches(r, partial) and all(r.get(k) == doc.get(k) for k, _ in keys) for r in self.rows):
                    raise DuplicateKeyError("duplicate indexed identity")
        self.rows.append(deepcopy(doc))
        return SimpleNamespace(inserted_id=doc["_id"])

    async def insert_many(self, docs, **kwargs):
        for doc in docs:
            await self.insert_one(doc, **kwargs)

    async def update_many(self, query, update, **kwargs):
        count = 0
        for row in self.rows:
            if matches(row, query):
                row.update(deepcopy(update.get("$set", {})))
                for k, v in update.get("$inc", {}).items():
                    row[k] = row.get(k, 0) + v
                for k in update.get("$unset", {}):
                    row.pop(k, None)
                count += 1
        return SimpleNamespace(matched_count=count)

    async def delete_many(self, query, **kwargs):
        self.rows = [r for r in self.rows if not matches(r, query)]

    async def update_one(self, query, update, **kwargs):
        row = next((r for r in self.rows if matches(r, query)), None)
        if row is None:
            return SimpleNamespace(matched_count=0)
        return await self.update_many({"_id": row["_id"]}, update)


class Transaction:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        await self.db.lock.acquire()
        self.before = {name: deepcopy(collection.rows) for name, collection in self.db.collections.items()}

    async def __aexit__(self, kind, exc, tb):
        if exc:
            for name, rows in self.before.items():
                self.db[name].rows = rows
        self.db.lock.release()


class Session:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def start_transaction(self):
        return Transaction(self.db)


class DB:
    def __init__(self):
        self.collections = {name: Collection() for name in (*COLLECTIONS, "orders", "aggregator_users", "pa_reference_owners")}
        self.lock = asyncio.Lock()
        self.client = self

    async def start_session(self):
        return Session(self)

    def __getitem__(self, key):
        return self.collections[key]

    def __getattr__(self, key):
        return self.collections[key]


NOW = datetime.now(timezone.utc)
ADMIN = Actor(actorId="admin-test", canReviewMappings=True, canPrepare=True, canProposeMappings=True,
              canIngestCatalog=True, canValidateCatalog=True, canTrustCatalog=True)
STAFF = Actor(actorId="staff-test", canPrepare=True, canProposeMappings=True)


def review(actor=ADMIN):
    return ReviewEvidence(reviewedBy=actor.actorId, reviewedAt=NOW,
                          evidenceReference="review-1", reason="Human-reviewed exact drug identity")


def proposal(namespace="supplier-a", target="DRG-CANON", source="LOCAL-1", description="Medication A"):
    return MappingProposal(identity=MappingIdentity(sourceNamespaceId=namespace, sourceIdentityType="CODE",
        sourceIdentityKey=source), sourceCode=source, sourceDescription=description,
        proposedMedicloudCode=target, mappingSource="CURSOR", candidateConfidence=1)


async def catalog(db, entries=None, **overrides):
    entries = entries if entries is not None else [{"procedureCode": "DRG-CANON", "procedureDescription": "Canonical medication A"}]
    evidence = CatalogEvidence(sourceExtractedAt=NOW - timedelta(hours=1), recordCount=max(1, len(entries)),
        contentHash=pa_mapping.catalog_hash(entries), evidenceReference="controlled-extraction")
    if overrides:
        evidence = CatalogEvidence.model_validate({**evidence.model_dump(), **overrides})
    from catalog_fixtures import signed_artifact
    from core.pa_catalog_provenance import canonical_json, key_fingerprint
    import hashlib
    artifact = signed_artifact(entries, extracted=evidence.sourceExtractedAt)
    provenance = {'manifest': artifact['manifest'], 'signature': artifact['signature'],
        'manifestDigest': hashlib.sha256(canonical_json(artifact['manifest'])).hexdigest(),
        'signingKeyFingerprint': artifact['manifest']['signingKeyFingerprint']}
    uploader = Actor(actorId="catalog-upload-test", canIngestCatalog=True)
    snapshot = await pa_mapping.persist_catalog_snapshot(db, uploader, evidence, entries, signed_provenance=provenance)
    if snapshot["status"] == "VALIDATED":
        # Clearly privileged fixture: simulates server-controlled extraction validator.
        validation = CatalogValidationEvidence(**evidence.model_dump(), snapshotId=snapshot["_id"],
            validationMethod="SERVER_CONTROLLED_MEDICLOUD_EXTRACTION", validatedAt=NOW,
            validatedBy=ADMIN.actorId, validationEvidenceId=str(ObjectId()),
            manifestDigest=provenance['manifestDigest'], signingKeyFingerprint=provenance['signingKeyFingerprint'])
        ref = await pa_mapping.register_catalog_validation(db, ADMIN, validation)
        trustee = Actor(actorId="independent-catalog-trust-test", canTrustCatalog=True)
        snapshot = await pa_mapping.promote_catalog_trust(db, trustee, snapshot["_id"], ref)
    return snapshot


@pytest_asyncio.fixture
async def flow(monkeypatch, tmp_path):
    from catalog_fixtures import configure
    configure(monkeypatch, tmp_path)
    from api import pa
    forbidden = AsyncMock(side_effect=AssertionError("IssuePa forbidden during preparation"))
    monkeypatch.setattr(pa_client, "issue_pa", forbidden)
    monkeypatch.setattr(pa, "issue_pa", forbidden)
    db = DB()
    await ensure_preparation_indexes(db)
    order_id, aggregator_id = ObjectId(), ObjectId()
    order = {"_id": order_id, "version": 1, "assignmentVersion": 2, "status": "completed",
        "winnerId": str(aggregator_id), "provider": {"providerId": "referring-1"},
        "enrollee": {"enrolleeId": "MEM-TEST", "fullName": "Must not persist", "phone": "private"},
        "medications": [{"lineId": "line-1", "procedureCode": "LOCAL-1", "sourceNamespaceId": "supplier-a",
                         "name": "Medication A", "diagnosisCode": "I10", "quantity": 4}],
        "finalProcedurePrices": [{"medicationLineId": "line-1", "procedureCode": "LOCAL-1", "amount": 1200}],
        "medicationSubtotal": 1200, "overallTotal": 1200, "fulfillmentType": "picked_up"}
    await db.orders.insert_one(order)
    await db.aggregator_users.insert_one({"_id": aggregator_id, "providerId": "issue-provider-9"})
    evidence = prep.member_evidence({"found": True, "count": 1, "results": [{"legacycode": "MEM-TEST",
        "memberid": 42, "groupid": 2, "group_legacy_id": "GROUP-LEGACY", "isprincipal": True,
        "dependantid": 0, "isterminated": False, "isdeleted": False, "firstname": "Secret", "nin": "Secret"}]},
        order=order, evidence_id="evidence-1", validated_at=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(minutes=30))
    snap = await catalog(db)
    mapping = await pa_mapping.propose_mapping(db, STAFF, proposal())
    active = await pa_mapping.revise_mapping(db, ADMIN, mapping["mappingId"], 1, "ACTIVE", snapshot_id=snap["_id"], review=review())
    batch = await prep.create_batch(db, STAFF, "2026-10-06")
    yield SimpleNamespace(db=db, order=order, evidence=evidence, snapshot=snap, mapping=active, batch=batch, forbidden=forbidden)
    forbidden.assert_not_called()


async def prepare(f, version=None, evidence="default"):
    current = await f.db.pa_preparation_batches.find_one({"_id": f.batch["_id"]})
    return await prep.prepare_order(f.db, STAFF, f.batch["_id"], str(f.order["_id"]),
        current["version"] if version is None else version, f.evidence if evidence == "default" else evidence)


@pytest.mark.asyncio
async def test_ready_no_transmission_no_order_mutation_line_total(flow):
    before = deepcopy(flow.db.orders.rows)
    rows = await prepare(flow)
    assert rows[0]["preparationStatus"] == "READY"
    assert rows[0]["submissionEligible"] is True
    assert rows[0]["quantity"] == 4 and rows[0]["lineAmount"] == "1200.00"
    assert rows[0]["referringProviderId"] == "referring-1"
    assert rows[0]["issuePaProviderId"] == "issue-provider-9"
    assert rows[0]["aggregatorId"] == flow.order["winnerId"]
    assert flow.db.orders.rows == before
    assert flow.db.pa_reference_owners.rows == []
    assert rows[0]["providerTariffValidation"] == rows[0]["tariffPriceValidation"] == "NOT_IMPLEMENTED"


@pytest.mark.asyncio
async def test_namespace_scoping_and_uniqueness(flow):
    other = await pa_mapping.propose_mapping(flow.db, STAFF, proposal(namespace="supplier-b"))
    assert other["mappingId"] != flow.mapping["mappingId"]
    with pytest.raises(pa_mapping.DomainConflict):
        await pa_mapping.propose_mapping(flow.db, STAFF, proposal())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["untrusted", "missing-code", "missing-desc", "code-desc", "no-review", "wrong-reviewer", "no-permission"])
async def test_activation_fails_closed(flow, failure):
    snap = flow.snapshot["_id"]
    actor, evidence = ADMIN, review()
    if failure == "untrusted": snap = "unknown"
    if failure == "missing-code":
        flow.db.pa_procedure_catalog_entries.rows.clear()
    if failure == "missing-desc": flow.db.pa_procedure_catalog_entries.rows[0]["procedureDescription"] = ""
    if failure == "code-desc": flow.db.pa_procedure_catalog_entries.rows[0]["procedureDescription"] = "DRG-CANON"
    if failure == "no-review": evidence = None
    if failure == "wrong-reviewer": evidence = review(STAFF)
    if failure == "no-permission": actor = STAFF
    before = deepcopy(flow.db.pa_mapping_heads.rows)
    with pytest.raises((pa_mapping.DomainConflict, PermissionError)):
        await pa_mapping.revise_mapping(flow.db, actor, flow.mapping["mappingId"], 2, "ACTIVE", snapshot_id=snap, review=evidence)
    assert flow.db.pa_mapping_heads.rows == before


@pytest.mark.asyncio
@pytest.mark.parametrize("entries", [
    [{"procedureCode": "X", "procedureDescription": ""}],
    [{"procedureCode": "X", "procedureDescription": " x "}],
    [{"procedureCode": "X", "procedureDescription": "One"}, {"procedureCode": "x", "procedureDescription": "Two"}],
    [{"procedureCode": "X", "procedureDescription": "One"}, {"procedureCode": "X", "procedureDescription": "One"}],
])
async def test_invalid_snapshot_preserves_trusted(flow, entries):
    bad = await catalog(flow.db, entries)
    assert bad["status"] == "INVALID"
    assert await pa_mapping.canonical_procedure(flow.db, flow.snapshot["_id"], " drg-canon ")
    assert not [r for r in flow.db.pa_procedure_catalog_entries.rows if r["snapshotId"] == bad["_id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [{"contentHash": "0" * 64}, {"recordCount": 2}, {"sourceExtractedAt": NOW + timedelta(days=1)}])
async def test_snapshot_evidence_checked(flow, overrides):
    assert (await catalog(flow.db, **overrides))["status"] == "INVALID"


@pytest.mark.asyncio
async def test_confidence_cannot_activate(flow):
    candidate = await pa_mapping.propose_mapping(flow.db, STAFF, proposal(namespace="another"))
    assert candidate["status"] == "PROPOSED" and candidate["candidateConfidence"] == 1
    validated = await pa_mapping.revise_mapping(flow.db, ADMIN, candidate["mappingId"], 1, "VALIDATED", snapshot_id=flow.snapshot["_id"])
    assert validated["status"] == "VALIDATED"
    head = await flow.db.pa_mapping_heads.find_one({"_id": candidate["mappingId"]})
    assert head["activeVersion"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["line", "version", "assignment", "provider", "mapping", "quantity", "diagnosis", "price", "member"])
async def test_drift_stales_and_preserves_history(flow, change):
    before = (await prepare(flow))[0]
    saved = deepcopy(before)
    order = flow.db.orders.rows[0]
    if change == "line":
        order["medications"][0]["lineId"] = "new-line"
        order["finalProcedurePrices"][0]["medicationLineId"] = "new-line"
    if change == "version": order["version"] += 1
    if change == "assignment": order["assignmentVersion"] += 1
    if change == "provider": flow.db.aggregator_users.rows[0]["providerId"] = "changed"
    if change == "mapping":
        await pa_mapping.revise_mapping(flow.db, ADMIN, flow.mapping["mappingId"], 2, "ACTIVE", snapshot_id=flow.snapshot["_id"], review=review())
    if change == "quantity": order["medications"][0]["quantity"] += 1
    if change == "diagnosis": order["medications"][0]["diagnosisCode"] = "I11"
    if change == "price":
        order["finalProcedurePrices"][0]["amount"] = 1300
        order["medicationSubtotal"] = order["overallTotal"] = 1300
    if change == "member": flow.evidence = flow.evidence.model_copy(update={"memberId": "43"})
    old = await prep.current_lines(flow.db, flow.batch["_id"], str(order["_id"]), flow.evidence)
    assert old[0]["isStale"] and not old[0]["submissionEligible"]
    new = (await prepare(flow))[0]
    assert new["inputFingerprint"] != before["inputFingerprint"]
    persisted = next(r for r in flow.db.pa_preparation_lines.rows if r["_id"] == saved["_id"])
    for key in saved:
        if key != "isCurrent": assert persisted[key] == saved[key]
    assert persisted["isCurrent"] is False


@pytest.mark.asyncio
async def test_deterministic_idempotent_revision(flow):
    first = (await prepare(flow))[0]
    second = (await prepare(flow))[0]
    assert first["inputFingerprint"] == second["inputFingerprint"]
    assert first["_id"] == second["_id"] and second["revision"] == 1
    assert len(flow.db.pa_preparation_lines.rows) == 1
    assert prep.fingerprint({"b": 1, "a": 2}) == prep.fingerprint({"a": 2, "b": 1})


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["submitting", "generated", "verification_required", "failed_rejected", "failed_retryable"])
async def test_transmission_ledger_and_owners_untouched(flow, status):
    ledger = {"lines": [{"lineId": "line-1", "status": status, "submittedAt": NOW, "paNumber": "PA-TEST"}],
              "attempts": [{"submissionGroupId": "attempt-1", "submittedAt": NOW}], "inputIdentity": {"memberId": "42"}}
    flow.db.orders.rows[0]["paGeneration"] = deepcopy(ledger)
    await flow.db.pa_reference_owners.insert_one({"_id": "PA-TEST", "orderId": "another-order"})
    before = deepcopy(flow.db.pa_reference_owners.rows)
    row = (await prepare(flow))[0]
    assert row["preparationStatus"] == "BLOCKED" and not row["submissionEligible"]
    assert flow.db.orders.rows[0]["paGeneration"] == ledger
    assert flow.db.pa_reference_owners.rows == before


@pytest.mark.asyncio
async def test_member_snapshot_minimized(flow):
    row = (await prepare(flow))[0]
    member = row["memberSnapshot"]
    assert set(member) == set(MemberEvidence.model_fields)
    assert not {"fullName", "firstname", "phone", "nin", "email", "dob", "address", "legacycode", "enrolleeId"} & set(member)
    assert "Must not persist" not in str(flow.db.pa_preparation_lines.rows)
    assert "Secret" not in str(flow.db.pa_preparation_lines.rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "expired", "future", "deleted", "terminated", "dependant", "principal-conflict", "invalid-dependant", "wrong-order"])
async def test_member_fail_closed(flow, failure):
    ev = flow.evidence
    updates = {"expired": {"expiresAt": NOW - timedelta(seconds=1)}, "future": {"validatedAt": NOW + timedelta(days=1), "expiresAt": NOW + timedelta(days=1, minutes=30)},
        "deleted": {"isDeleted": True}, "terminated": {"isTerminated": True},
        "dependant": {"isPrincipal": False, "dependantId": "0"},
        "principal-conflict": {"dependantId": "3"}, "invalid-dependant": {"dependantId": "invalid"}, "wrong-order": {"orderId": "wrong"}}
    ev = None if failure == "none" else ev.model_copy(update=updates[failure])
    row = (await prepare(flow, evidence=ev))[0]
    assert row["preparationStatus"] == "BLOCKED"
    assert "MEMBER_EVIDENCE_INVALID_OR_STALE" in row["blockingReasons"]


@pytest.mark.asyncio
async def test_concurrent_publication_cas(flow):
    outcomes = await asyncio.gather(prepare(flow, version=0), prepare(flow, version=0), return_exceptions=True)
    assert sum(isinstance(x, list) for x in outcomes) == 1
    assert sum(isinstance(x, pa_mapping.DomainConflict) for x in outcomes) == 1
    assert len(flow.db.pa_preparation_lines.rows) == 1
    assert flow.db.pa_preparation_batches.rows[0]["version"] == 1


@pytest.mark.asyncio
async def test_event_failure_rolls_back_publication(flow):
    before = deepcopy(flow.db.pa_preparation_batches.rows)
    flow.db.pa_preparation_events.fail_insert = True
    with pytest.raises(RuntimeError): await prepare(flow)
    assert flow.db.pa_preparation_batches.rows == before
    assert flow.db.pa_preparation_lines.rows == []


@pytest.mark.asyncio
async def test_mapping_event_failure_rolls_back_head(flow):
    before = deepcopy(flow.db.pa_mapping_heads.rows)
    flow.db.pa_preparation_events.fail_insert = True
    with pytest.raises(RuntimeError):
        await pa_mapping.revise_mapping(flow.db, ADMIN, flow.mapping["mappingId"], 2, "RETIRED")
    assert flow.db.pa_mapping_heads.rows == before
    assert len(flow.db.pa_mapping_versions.rows) == 2


@pytest.mark.asyncio
async def test_indexes_idempotent_and_only_phase1(flow):
    before = {k: deepcopy(flow.db[k].indexes) for k in COLLECTIONS}
    await ensure_preparation_indexes(flow.db)
    assert before == {k: flow.db[k].indexes for k in COLLECTIONS}
    assert flow.db.orders.indexes == flow.db.pa_reference_owners.indexes == []
    assert any(opts.get("partialFilterExpression") == {"isCurrent": True} for _, opts in flow.db.pa_preparation_lines.indexes)


@pytest.mark.asyncio
async def test_approved_daytime_ready_but_not_submittable(flow):
    order = flow.db.orders.rows[0]
    order["status"] = "awaiting_fulfillment"
    order["approvedProcedurePrices"] = order.pop("finalProcedurePrices")
    order["priceApprovedAt"] = NOW
    row = (await prepare(flow))[0]
    assert row["preparationStatus"] == "READY" and not row["submissionEligible"]
    assert row["priceSource"] == "approvedProcedurePrices"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["legacy", "subtotal", "mismatch", "quantity-bool", "missing-approval"])
async def test_invalid_price_or_service_blocks(flow, failure):
    order = flow.db.orders.rows[0]
    if failure == "legacy": order.pop("finalProcedurePrices"); order["unitPrice"] = 1200
    if failure == "subtotal": order["medicationSubtotal"] = 1
    if failure == "mismatch": order["finalProcedurePrices"][0]["procedureCode"] = "changed"
    if failure == "quantity-bool": order["medications"][0]["quantity"] = True
    if failure == "missing-approval": order["approvedProcedurePrices"] = order.pop("finalProcedurePrices")
    assert (await prepare(flow))[0]["preparationStatus"] == "BLOCKED"


@pytest.mark.asyncio
async def test_no_mapping_review_required(flow):
    flow.db.orders.rows[0]["medications"][0]["sourceNamespaceId"] = "unmapped"
    assert (await prepare(flow))[0]["preparationStatus"] == "REVIEW_REQUIRED"


@pytest.mark.asyncio
async def test_append_only_audit_minimal(flow):
    await prepare(flow)
    for row in flow.db.pa_preparation_events.rows:
        assert set(row) <= {"_id", "eventType", "timestamp", "actorId", "correlationId", "batchId",
            "preparationLineId", "preparationRevision", "mappingId", "previousVersion", "newVersion", "reason", "snapshotId", "memberEvidence"}
    assert any(r["eventType"] == "line_prepared" for r in flow.db.pa_preparation_events.rows)


@pytest.mark.asyncio
async def test_final_prices_before_fulfilment_can_prepare(flow):
    order = flow.db.orders.rows[0]
    order["status"] = "awaiting_fulfillment"
    order.pop("fulfillmentType")
    row = (await prepare(flow))[0]
    assert row["preparationStatus"] == "READY" and not row["submissionEligible"]


@pytest.mark.asyncio
async def test_delivery_reserved_contract_line_total(flow, monkeypatch):
    monkeypatch.setenv("PA_DELIVERY_DIAGNOSIS_CODE", "Z76.9")
    snapshot = await catalog(flow.db, [{"procedureCode": "DRG-CANON", "procedureDescription": "Canonical medication A"},
                                    {"procedureCode": "PRE11", "procedureDescription": "Delivery service"}])
    await pa_mapping.revise_mapping(flow.db, ADMIN, flow.mapping["mappingId"], 2, "ACTIVE", snapshot_id=snapshot["_id"], review=review())
    order = flow.db.orders.rows[0]
    order.update(fulfillmentType="delivered", deliveryFee=500, overallTotal=1700)
    before = deepcopy(order)
    rows = await prepare(flow)
    assert [r["lineAmount"] for r in rows] == ["1200.00", "500.00"]
    assert rows[1]["orderLineId"] == "delivery-PRE11" and rows[1]["medicloudCode"] == "PRE11"
    assert rows[1]["mappingId"] is None
    assert all(r["preparationStatus"] == "READY" and r["submissionEligible"] for r in rows)
    assert order == before


@pytest.mark.asyncio
async def test_delivery_missing_evidence_blocks_group(flow, monkeypatch):
    monkeypatch.delenv("PA_DELIVERY_DIAGNOSIS_CODE", raising=False)
    flow.db.orders.rows[0].update(fulfillmentType="delivered", deliveryFee=500, overallTotal=1700)
    rows = await prepare(flow)
    assert rows[1]["preparationStatus"] == "BLOCKED"
    assert "DELIVERY_DIAGNOSIS_UNCONFIGURED" in rows[1]["blockingReasons"]
    assert "DELIVERY_CATALOG_EVIDENCE_REQUIRED" in rows[1]["blockingReasons"]
    assert not any(r["submissionEligible"] for r in rows)


@pytest.mark.asyncio
async def test_medication_mapping_cannot_use_reserved_delivery_code(flow):
    snapshot = await catalog(flow.db, [{"procedureCode": "PRE11", "procedureDescription": "Delivery service"}])
    replacement = proposal(target="PRE11")
    await pa_mapping.revise_mapping(flow.db, ADMIN, flow.mapping["mappingId"], 2, "ACTIVE", snapshot_id=snapshot["_id"], review=review(), replacement=replacement)
    assert (await prepare(flow))[0]["preparationStatus"] == "BLOCKED"


@pytest.mark.parametrize("principal,dependant", [(True, 0), (True, None), (False, 1), (False, 3)])
def test_member_adapter_principal_and_dependants(principal, dependant):
    order = {"_id": ObjectId(), "version": 0, "enrollee": {"enrolleeId": "E"}}
    data = {"found": True, "count": 1, "results": [{"legacycode": " e ", "memberid": 42,
        "group_legacy_id": "G", "isprincipal": principal, "dependantid": dependant,
        "isterminated": False, "isdeleted": False}]}
    ev = prep.member_evidence(data, order=order, evidence_id="evidence", validated_at=NOW,
                              expires_at=NOW + timedelta(hours=1))
    assert prep.valid_member(ev, order, NOW)


@pytest.mark.parametrize("patch", [{"isterminated": None}, {"isdeleted": True}, {"isprincipal": None},
    {"dependantid": 3}, {"dependantnumber": 2}, {"memberid": True}, {"group_legacy_id": 2},
    {"legacycode": "OTHER"}, {"isprincipal": False, "dependantid": 0}, {"dependantid": "invalid"}])
def test_member_adapter_fail_closed(patch):
    order = {"_id": ObjectId(), "version": 0, "enrollee": {"enrolleeId": "E"}}
    row = {"legacycode": "E", "memberid": 42, "group_legacy_id": "G", "isprincipal": True,
           "dependantid": 0, "isterminated": False, "isdeleted": False, **patch}
    with pytest.raises(ValueError):
        prep.member_evidence({"found": True, "count": 1, "results": [row]}, order=order,
            evidence_id="evidence", validated_at=NOW, expires_at=NOW + timedelta(hours=1))


@pytest.mark.asyncio
async def test_transaction_unavailable_fails_without_writes(flow, monkeypatch):
    async def unavailable():
        raise RuntimeError("transactions unavailable")
    monkeypatch.setattr(flow.db, "start_session", unavailable)
    with pytest.raises(RuntimeError): await prepare(flow)
    assert flow.db.pa_preparation_lines.rows == []


@pytest.mark.asyncio
async def test_retirement_invalidates_rows_not_order(flow):
    await prepare(flow)
    before = deepcopy(flow.db.orders.rows)
    await pa_mapping.revise_mapping(flow.db, ADMIN, flow.mapping["mappingId"], 2, "RETIRED")
    assert not flow.db.pa_preparation_lines.rows[0]["isCurrent"]
    assert flow.db.orders.rows == before
    assert (await prepare(flow))[0]["preparationStatus"] == "REVIEW_REQUIRED"


@pytest.mark.asyncio
async def test_mapping_concurrent_revision_cas(flow):
    async def activate():
        return await pa_mapping.revise_mapping(flow.db, ADMIN, flow.mapping["mappingId"], 2, "ACTIVE",
            snapshot_id=flow.snapshot["_id"], review=review())
    results = await asyncio.gather(activate(), activate(), return_exceptions=True)
    assert sum(isinstance(x, dict) for x in results) == 1
    assert sum(isinstance(x, pa_mapping.DomainConflict) for x in results) == 1
    assert len(flow.db.pa_mapping_versions.rows) == 3


@pytest.mark.asyncio
async def test_description_identity_requires_exact_reviewed_identity(flow):
    p = MappingProposal(identity=MappingIdentity(sourceNamespaceId="no-code-supplier", sourceIdentityType="DESCRIPTION",
        sourceIdentityKey="medication a 10mg tablet"), sourceDescription="Medication A 10mg tablet",
        proposedMedicloudCode="DRG-CANON", mappingSource="FUZZY", candidateConfidence=1)
    doc = await pa_mapping.propose_mapping(flow.db, STAFF, p)
    assert doc["status"] == "PROPOSED"
    with pytest.raises(ValueError):
        await pa_mapping.propose_mapping(flow.db, STAFF, p.model_copy(update={"sourceDescription": "Different strength"}))


@pytest.mark.asyncio
async def test_invalid_price_change_also_creates_revision(flow):
    flow.db.orders.rows[0]["finalProcedurePrices"][0]["amount"] = 0
    before = (await prepare(flow))[0]
    flow.db.orders.rows[0]["finalProcedurePrices"][0]["amount"] = -1
    after = (await prepare(flow))[0]
    assert after["revision"] == 2
    assert before["inputFingerprint"] != after["inputFingerprint"]
    assert before["preparationStatus"] == after["preparationStatus"] == "BLOCKED"


@pytest.mark.asyncio
async def test_no_medications_cannot_publish_empty_ready_order(flow):
    flow.db.orders.rows[0]["medications"] = []
    with pytest.raises(pa_mapping.DomainConflict): await prepare(flow)
    assert flow.db.pa_preparation_batches.rows[0]["version"] == 0


@pytest.mark.asyncio
async def test_new_snapshot_failure_rolls_back_atomically(flow):
    before = deepcopy(flow.db.pa_procedure_catalog_snapshots.rows)
    flow.db.pa_procedure_catalog_entries.fail_insert = True
    with pytest.raises(RuntimeError): await catalog(flow.db)
    assert flow.db.pa_procedure_catalog_snapshots.rows[:len(before)] == before
    assert flow.db.pa_procedure_catalog_snapshots.rows[-1]["status"] == "INGESTING"
    assert await pa_mapping.canonical_procedure(flow.db, flow.snapshot["_id"], "DRG-CANON")


@pytest.mark.asyncio
async def test_rejected_candidate_keeps_active_version(flow):
    previous = deepcopy(flow.mapping)
    await pa_mapping.revise_mapping(flow.db, ADMIN, flow.mapping["mappingId"], 2, "PROPOSED", replacement=proposal(target="OTHER"))
    rejected = await pa_mapping.revise_mapping(flow.db, ADMIN, flow.mapping["mappingId"], 3, "REJECTED")
    assert rejected["version"] == 4
    assert rejected["activeVersion"] == 2 and not rejected["isActive"]
    assert "medicloudCode" not in rejected and "snapshotId" not in rejected
    head = await flow.db.pa_mapping_heads.find_one({"_id": flow.mapping["mappingId"]})
    assert head["activeVersion"] == 2
    assert await flow.db.pa_mapping_versions.find_one({"_id": previous["_id"]}) == {k: v for k, v in previous.items() if k not in {"activeVersion", "isActive"}}
    assert (await prepare(flow))[0]["preparationStatus"] == "READY"


@pytest.mark.asyncio
async def test_changed_description_does_not_fuzzy_approve(flow):
    flow.db.orders.rows[0]["medications"][0]["name"] = "Medication A other strength"
    row = (await prepare(flow))[0]
    assert row["preparationStatus"] == "REVIEW_REQUIRED"
    assert "SOURCE_DESCRIPTION_CHANGED" in row["reviewReasons"]


@pytest.mark.asyncio
async def test_sensitive_member_fields_rejected_by_schema(flow):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        MemberEvidence.model_validate({**flow.evidence.model_dump(), "phone": "private"})
    with pytest.raises(ValidationError):
        MemberEvidence.model_validate({**flow.evidence.model_dump(), "isDeleted": "false"})


@pytest.mark.asyncio
async def test_missing_source_namespace_is_review_not_global_match(flow):
    flow.db.orders.rows[0]["medications"][0].pop("sourceNamespaceId")
    row = (await prepare(flow))[0]
    assert row["preparationStatus"] == "REVIEW_REQUIRED"
    assert row["mappingId"] is None


@pytest.mark.asyncio
async def test_fresh_receipt_same_material_not_stale(flow):
    first = (await prepare(flow))[0]
    fresh = flow.evidence.model_copy(update={"evidenceId": "new-retrieval", "validatedAt": NOW,
                                            "expiresAt": NOW + timedelta(minutes=45)})
    view = await prep.current_lines(flow.db, flow.batch["_id"], str(flow.order["_id"]), fresh)
    assert not view[0]["isStale"]
    second = (await prepare(flow, evidence=fresh))[0]
    assert second["_id"] == first["_id"] and second["inputFingerprint"] == first["inputFingerprint"]
    assert flow.db.pa_preparation_events.rows[-1]["memberEvidence"]["evidenceId"] == "new-retrieval"
    assert flow.db.pa_preparation_lines.rows[0]["memberSnapshot"]["evidenceId"] == "evidence-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"memberId": "43"}, {"groupId": "9"}, {"groupLegacyId": "NEW"},
    {"isPrincipal": False, "dependantId": "1"}, {"isDeleted": True}, {"isTerminated": True}])
async def test_material_member_change_stales(flow, change):
    await prepare(flow)
    changed = flow.evidence.model_copy(update=change)
    assert (await prep.current_lines(flow.db, flow.batch["_id"], str(flow.order["_id"]), changed))[0]["isStale"]


@pytest.mark.asyncio
async def test_active_reject_forbidden_without_mutation(flow):
    before = deepcopy(flow.db.pa_mapping_heads.rows)
    with pytest.raises(pa_mapping.DomainConflict, match="RETIREMENT"):
        await pa_mapping.revise_mapping(flow.db, ADMIN, flow.mapping["mappingId"], 2, "REJECTED")
    assert flow.db.pa_mapping_heads.rows == before
    assert len(flow.db.pa_mapping_versions.rows) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("validated", [False, True])
async def test_never_activated_candidate_can_be_rejected(flow, validated):
    candidate = await pa_mapping.propose_mapping(flow.db, STAFF, proposal(namespace="new-candidate"))
    version = 1
    if validated:
        await pa_mapping.revise_mapping(flow.db, ADMIN, candidate["mappingId"], 1, "VALIDATED", snapshot_id=flow.snapshot["_id"])
        version = 2
    result = await pa_mapping.revise_mapping(flow.db, ADMIN, candidate["mappingId"], version, "REJECTED")
    assert result["activeVersion"] is None and not result["isActive"]
    assert "medicloudCode" not in result


@pytest.mark.asyncio
async def test_missing_previous_mapping_has_domain_error(flow):
    flow.db.pa_mapping_versions.rows = []
    with pytest.raises(pa_mapping.DomainConflict, match="PREVIOUS_MAPPING"):
        await pa_mapping.revise_mapping(flow.db, ADMIN, flow.mapping["mappingId"], 2, "RETIRED")


@pytest.mark.asyncio
async def test_ingest_does_not_grant_trust(flow):
    entries = [{"procedureCode": "NEW", "procedureDescription": "New canonical procedure"}]
    evidence = CatalogEvidence(sourceExtractedAt=NOW, recordCount=1, contentHash=pa_mapping.catalog_hash(entries), evidenceReference="caller-claims")
    snapshot = await pa_mapping.persist_catalog_snapshot(flow.db, ADMIN, evidence, entries)
    assert snapshot["status"] == "VALIDATED"
    with pytest.raises(pa_mapping.DomainConflict):
        await pa_mapping.canonical_procedure(flow.db, snapshot["_id"], "NEW")
    with pytest.raises(pa_mapping.DomainConflict):
        await pa_mapping.promote_catalog_trust(flow.db, ADMIN, snapshot["_id"], "not-recorded")
    with pytest.raises(pa_mapping.DomainConflict):
        await pa_mapping.revise_mapping(flow.db, ADMIN, flow.mapping["mappingId"], 2, "ACTIVE", snapshot_id=snapshot["_id"], review=review())


@pytest.mark.asyncio
async def test_mapping_review_permission_does_not_imply_catalog_trust(flow):
    reviewer = Actor(actorId="only-mapping-review", canReviewMappings=True)
    with pytest.raises(PermissionError):
        await pa_mapping.promote_catalog_trust(flow.db, reviewer, flow.snapshot["_id"], "anything")
    with pytest.raises(PermissionError):
        await pa_mapping.persist_catalog_snapshot(flow.db, reviewer,
            CatalogEvidence(sourceExtractedAt=NOW, recordCount=1, contentHash="0" * 64, evidenceReference="e"), [])


@pytest.mark.asyncio
@pytest.mark.parametrize("code,label", [(112, None), (251, None), (1, "TransientTransactionError"), (1, "UnknownTransactionCommitResult")])
async def test_mongo_conflict_translated_no_retry(flow, monkeypatch, code, label):
    from pymongo.errors import OperationFailure
    calls = []
    async def fail():
        calls.append(1)
        raise OperationFailure("driver details must not leak", code=code,
                               details={"errorLabels": [label] if label else []})
    monkeypatch.setattr(flow.db, "start_session", fail)
    with pytest.raises(pa_mapping.DomainConflict, match="TRANSACTION_CONFLICT_REFRESH_REQUIRED"):
        await prepare(flow)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_order_second_active_batch_conflicts(flow):
    await prepare(flow)
    other = await prep.create_batch(flow.db, STAFF, "2026-10-07")
    with pytest.raises(pa_mapping.DomainConflict, match="ORDER_ALREADY"):
        await prep.prepare_order(flow.db, STAFF, other["_id"], str(flow.order["_id"]), 0, flow.evidence)
    assert len(flow.db.pa_preparation_contexts.rows) == 1


@pytest.mark.asyncio
async def test_concurrent_batches_only_one_claims_order(flow):
    other = await prep.create_batch(flow.db, STAFF, "2026-10-07")
    results = await asyncio.gather(prepare(flow, version=0),
        prep.prepare_order(flow.db, STAFF, other["_id"], str(flow.order["_id"]), 0, flow.evidence), return_exceptions=True)
    assert sum(isinstance(r, list) for r in results) == 1
    assert sum(isinstance(r, pa_mapping.DomainConflict) for r in results) == 1
    assert len(flow.db.pa_preparation_contexts.rows) == 1


@pytest.mark.asyncio
async def test_close_releases_ownership_preserves_history(flow):
    await prepare(flow)
    history = deepcopy(flow.db.pa_preparation_lines.rows)
    await prep.close_batch(flow.db, STAFF, flow.batch["_id"], 1)
    assert flow.db.pa_preparation_contexts.rows == []
    assert flow.db.pa_preparation_lines.rows == history
    assert (await prep.current_lines(flow.db, flow.batch["_id"], str(flow.order["_id"]), flow.evidence))[0]["isStale"]
    other = await prep.create_batch(flow.db, STAFF, "2026-10-07")
    assert await prep.prepare_order(flow.db, STAFF, other["_id"], str(flow.order["_id"]), 0, flow.evidence)


@pytest.mark.asyncio
async def test_close_audit_failure_rolls_back_release(flow):
    await prepare(flow)
    flow.db.pa_preparation_events.fail_insert = True
    with pytest.raises(RuntimeError): await prep.close_batch(flow.db, STAFF, flow.batch["_id"], 1)
    assert flow.db.pa_preparation_batches.rows[0]["status"] == "OPEN"
    assert len(flow.db.pa_preparation_contexts.rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [7, {}, None, " "])
async def test_bad_legacy_provider_blocked(flow, bad):
    flow.db.aggregator_users.rows[0]["providerId"] = bad
    row = (await prepare(flow))[0]
    assert row["preparationStatus"] == "BLOCKED"
    assert "ISSUEPA_PROVIDER_UNCONFIGURED" in row["blockingReasons"]


@pytest.mark.asyncio
async def test_provider_whitespace_normalized(flow):
    flow.db.aggregator_users.rows[0]["providerId"] = " issue-provider-9 "
    assert (await prepare(flow))[0]["issuePaProviderId"] == "issue-provider-9"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["provider", "enrollee", "ledger-lines", "diagnosis"])
async def test_none_legacy_fields_fail_closed_without_crash(flow, field):
    order = flow.db.orders.rows[0]
    if field == "provider": order["provider"] = None
    if field == "enrollee": order["enrollee"] = None
    if field == "ledger-lines": order["paGeneration"] = {"lines": None}
    if field == "diagnosis": order["medications"][0]["diagnosisCode"] = 123
    row = (await prepare(flow))[0]
    if field == "provider":
        assert row["referringProviderId"] is None
    assert row["preparationStatus"] == "BLOCKED"


@pytest.mark.asyncio
async def test_domain_permission_required(flow):
    unprivileged = Actor(actorId="no-permissions")
    with pytest.raises(PermissionError): await prep.create_batch(flow.db, unprivileged, "2026-10-07")
    with pytest.raises(PermissionError): await pa_mapping.propose_mapping(flow.db, unprivileged, proposal(namespace="new"))


def test_member_validity_cap():
    from pydantic import ValidationError
    order = {"_id": ObjectId(), "version": 0, "enrollee": {"enrolleeId": "E"}}
    with pytest.raises(ValidationError, match="maximum validity"):
        prep.member_evidence({"found": True, "count": 1, "results": [{"legacycode": "E", "memberid": 1,
            "group_legacy_id": "G", "isprincipal": True, "isterminated": False, "isdeleted": False}]},
            order=order, evidence_id="e", validated_at=NOW, expires_at=NOW + timedelta(days=1))


@pytest.mark.asyncio
async def test_safety_index_failure_fails_closed(flow, monkeypatch):
    from pymongo.errors import OperationFailure
    async def fail(*args, **kwargs):
        raise OperationFailure("secret-like driver detail", code=1)
    monkeypatch.setattr(flow.db.pa_preparation_lines, "create_index", fail)
    with pytest.raises(RuntimeError, match="Required preparation index failed"):
        await ensure_preparation_indexes(flow.db)


@pytest.mark.asyncio
async def test_order_id_case_cannot_bypass_ownership(flow):
    await prepare(flow)
    other = await prep.create_batch(flow.db, STAFF, "2026-10-07")
    with pytest.raises(pa_mapping.DomainConflict, match="ORDER_ALREADY"):
        await prep.prepare_order(flow.db, STAFF, other["_id"], str(flow.order["_id"]).upper(), 0, flow.evidence)


@pytest.mark.asyncio
async def test_partial_chunk_ingest_is_never_trusted(flow, monkeypatch):
    entries = [{"procedureCode": f"DRG-{n}", "procedureDescription": f"Medicine {n}"} for n in range(1001)]
    evidence = CatalogEvidence(sourceExtractedAt=NOW, recordCount=len(entries), contentHash=pa_mapping.catalog_hash(entries), evidenceReference="e")
    original = flow.db.pa_procedure_catalog_entries.insert_many
    chunks = []
    async def insert(docs, **kwargs):
        chunks.append(len(docs))
        if len(chunks) == 2:
            raise RuntimeError("second chunk unavailable")
        await original(docs, **kwargs)
    monkeypatch.setattr(flow.db.pa_procedure_catalog_entries, "insert_many", insert)
    with pytest.raises(RuntimeError):
        await pa_mapping.persist_catalog_snapshot(flow.db, ADMIN, evidence, entries)
    snapshot = flow.db.pa_procedure_catalog_snapshots.rows[-1]
    assert snapshot["status"] == "INGESTING" and snapshot["ingestedCount"] == 1000
    assert chunks == [1000, 1]
    with pytest.raises(pa_mapping.DomainConflict):
        await pa_mapping.canonical_procedure(flow.db, snapshot["_id"], "DRG-1")
    assert await pa_mapping.canonical_procedure(flow.db, flow.snapshot["_id"], "DRG-CANON")


@pytest.mark.asyncio
async def test_auditing_batch_retains_exclusive_context(flow):
    await prepare(flow)
    flow.db.pa_preparation_batches.rows[0]["status"] = "AUDITING"
    other = await prep.create_batch(flow.db, STAFF, "2026-10-07")
    with pytest.raises(pa_mapping.DomainConflict):
        await prep.prepare_order(flow.db, STAFF, other["_id"], str(flow.order["_id"]), 0, flow.evidence)
    assert await prepare(flow)


@pytest.mark.asyncio
async def test_concurrent_close_and_claim_safe(flow):
    await prepare(flow)
    other = await prep.create_batch(flow.db, STAFF, "2026-10-07")
    await asyncio.gather(prep.close_batch(flow.db, STAFF, flow.batch["_id"], 1),
        prep.prepare_order(flow.db, STAFF, other["_id"], str(flow.order["_id"]), 0, flow.evidence), return_exceptions=True)
    assert len(flow.db.pa_preparation_contexts.rows) <= 1
    assert flow.db.pa_preparation_batches.rows[0]["status"] == "CLOSED"
    if not flow.db.pa_preparation_contexts.rows:
        await prep.prepare_order(flow.db, STAFF, other["_id"], str(flow.order["_id"]), 0, flow.evidence)
    assert flow.db.pa_preparation_contexts.rows[0]["batchId"] == other["_id"]


@pytest.mark.asyncio
async def test_replacement_activation_retains_history_and_explicit_head(flow):
    old = await flow.db.pa_mapping_versions.find_one({"mappingId": flow.mapping["mappingId"], "version": 2})
    new = await pa_mapping.revise_mapping(flow.db, ADMIN, flow.mapping["mappingId"], 2, "ACTIVE", snapshot_id=flow.snapshot["_id"], review=review())
    assert new["activeVersion"] == 3 and new["isActive"]
    assert await flow.db.pa_mapping_versions.find_one({"_id": old["_id"]}) == old


@pytest.mark.asyncio
async def test_catalog_validation_method_and_identity_explicit(flow):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        CatalogValidationEvidence(sourceExtractedAt=NOW, recordCount=1, contentHash="0" * 64,
            evidenceReference="e", snapshotId="s", validationMethod="HASH_MATCH_ONLY",
            validatedAt=NOW, validatedBy="validator", validationEvidenceId="v")


@pytest.mark.asyncio
async def test_mongo_body_conflict_rolls_back_and_translates(flow, monkeypatch):
    from pymongo.errors import OperationFailure
    async def fail(*args, **kwargs):
        raise OperationFailure("write conflict", code=112)
    monkeypatch.setattr(flow.db.pa_preparation_events, "insert_one", fail)
    with pytest.raises(pa_mapping.DomainConflict): await prepare(flow)
    assert flow.db.pa_preparation_contexts.rows == []
    assert flow.db.pa_preparation_lines.rows == []
    assert flow.db.pa_preparation_batches.rows[0]["version"] == 0
