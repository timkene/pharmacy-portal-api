"""Server-side source identity: intake verification, direct resolution and mapping isolation.

Backfill coverage lives in test_pa_source_backfill.py.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api import orders as orders_api
from core import pa_catalog_provenance as provenance, pa_mapping, pa_mapping_proposals, pa_mapping_review, pa_preparation as prep
from core import pa_source_identity as identity
from core.pa_preparation_store import DomainConflict, now_utc
from core.pa_source_namespaces import (INTAKE_REVIEW_NAMESPACE, MEDICLOUD_NAMESPACE, UNVERIFIED_METHOD,
    VERIFIED_MAPPING_SOURCE, VERIFIED_METHOD, is_reserved_namespace)
from models.pa_preparation import Actor, MappingIdentity, MappingProposal
from test_pa_preparation import flow, catalog, prepare, NOW, STAFF, ADMIN  # noqa: F401 (fixtures)
from test_orders import Collection as OrdersCollection

ENTRIES = [{"procedureCode": "DRG-CANON", "procedureDescription": "Canonical medication A"},
           {"procedureCode": "PRE11", "procedureDescription": "Pharmacy delivery service"},
           {"procedureCode": "ABC1", "procedureDescription": "Other drug"}]


def med(code="DRG-CANON", name="Canonical medication A", **extra):
    return {"lineId": "line-1", "procedureCode": code, "name": name, "diagnosisCode": "I10", "quantity": 4, **extra}


async def trusted(flow, entries=ENTRIES, **overrides):
    return await catalog(flow.db, entries, **overrides)


async def stamp_order(flow, snapshot_id, code="DRG-CANON", name="Canonical medication A"):
    """Replace the fixture medication with a server-stamped one and matching price line."""
    order = flow.db.orders.rows[0]
    order["medications"] = await identity.verify_medications(flow.db, [med(code, name)], "staff", snapshot_id=snapshot_id)
    order["finalProcedurePrices"] = [{"medicationLineId": "line-1", "procedureCode": code, "amount": 1200}]
    return order


def calculate(flow, evidence="default"):
    order = flow.db.orders.rows[0]
    return prep.calculate(flow.db, order, flow.db.aggregator_users.rows[0],
                          flow.evidence if evidence == "default" else evidence, None, now_utc())


# --- Verification -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exact_code_and_label_verifies_with_recorded_provenance(flow):
    snap = await trusted(flow)
    [result] = await identity.verify_medications(flow.db, [med(name="  canonical MEDICATION a ")], "staff-1", snapshot_id=snap["_id"])
    assert result["sourceNamespaceId"] == MEDICLOUD_NAMESPACE
    assert result["sourceCode"] == "DRG-CANON" and result["sourceDescription"] == "Canonical medication A"
    verification = result["sourceVerification"]
    assert verification["method"] == VERIFIED_METHOD and verification["snapshotId"] == snap["_id"]
    assert verification["catalogCode"] == "DRG-CANON" and verification["verifiedBy"] == "staff-1"
    assert verification["verifiedAt"].microsecond % 1000 == 0
    assert result["name"] == "  canonical MEDICATION a " and result["procedureCode"] == "DRG-CANON"


@pytest.mark.asyncio
async def test_client_supplied_source_fields_are_discarded(flow):
    snap = await trusted(flow)
    forged = med("UNKNOWN", "Anything", sourceNamespaceId=MEDICLOUD_NAMESPACE, sourceCode="DRG-CANON",
                 sourceDescription="Canonical medication A",
                 sourceVerification={"method": VERIFIED_METHOD, "snapshotId": snap["_id"], "catalogCode": "DRG-CANON"})
    [result] = await identity.verify_medications(flow.db, [forged], "staff", snapshot_id=snap["_id"])
    assert result["sourceNamespaceId"] == INTAKE_REVIEW_NAMESPACE
    assert result["sourceCode"] == "UNKNOWN" and result["sourceDescription"] == "Anything"
    assert result["sourceVerification"]["method"] == UNVERIFIED_METHOD


@pytest.mark.asyncio
@pytest.mark.parametrize("code,name,reason", [
    ("drg-canon", "Canonical medication A", "CODE_FORM_MISMATCH"),
    ("UNKNOWN", "Canonical medication A", "CODE_NOT_IN_TRUSTED_CATALOG"),
    ("DRG-CANON", "Different medication", "DESCRIPTION_MISMATCH"),
    ("PRE11", "Pharmacy delivery service", "RESERVED_PROCEDURE"),
    (" DRG-CANON", "Canonical medication A", "SOURCE_CODE_INVALID"),
])
async def test_unverifiable_codes_fall_back_to_manual_review(flow, code, name, reason):
    snap = await trusted(flow)
    [result] = await identity.verify_medications(flow.db, [med(code, name)], "staff", snapshot_id=snap["_id"])
    assert result["sourceNamespaceId"] == INTAKE_REVIEW_NAMESPACE
    assert result["sourceVerification"] == {"method": UNVERIFIED_METHOD, "reason": reason, "snapshotId": snap["_id"],
        "verifiedAt": result["sourceVerification"]["verifiedAt"], "verifiedBy": "staff"}


@pytest.mark.asyncio
async def test_revoked_or_missing_catalog_never_verifies(flow):
    snap = await trusted(flow)
    next(r for r in flow.db.pa_procedure_catalog_snapshots.rows if r["_id"] == snap["_id"])["revokedAt"] = now_utc()
    [revoked] = await identity.verify_medications(flow.db, [med()], "staff", snapshot_id=snap["_id"])
    assert revoked["sourceVerification"]["reason"] == "CATALOG_EVIDENCE_INVALID"
    [missing] = await identity.verify_medications(flow.db, [med()], "staff", snapshot_id="no-such-snapshot")
    assert missing["sourceNamespaceId"] == INTAKE_REVIEW_NAMESPACE


@pytest.mark.asyncio
async def test_latest_trusted_snapshot_skips_revoked_and_chooses_newest(flow):
    older = await trusted(flow, sourceExtractedAt=NOW - timedelta(hours=3))
    newer = await trusted(flow, sourceExtractedAt=NOW - timedelta(minutes=30))
    assert await identity.latest_trusted_snapshot_id(flow.db) == newer["_id"]
    next(r for r in flow.db.pa_procedure_catalog_snapshots.rows if r["_id"] == newer["_id"])["revokedAt"] = now_utc()
    assert await identity.latest_trusted_snapshot_id(flow.db) in {older["_id"], flow.snapshot["_id"]}


@pytest.mark.asyncio
async def test_catalog_outage_fails_closed_without_blocking_intake():
    broken = SimpleNamespace()  # No catalog collections at all.
    [result] = await identity.verify_medications(broken, [med()], "staff")
    assert result["sourceNamespaceId"] == INTAKE_REVIEW_NAMESPACE
    assert result["sourceVerification"]["reason"] in {"TRUSTED_CATALOG_UNAVAILABLE", "CATALOG_UNAVAILABLE"}


@pytest.mark.asyncio
async def test_verified_source_binding_cannot_be_misused(flow):
    snap = await trusted(flow)
    for kwargs in ({"verified_source": {"snapshotId": "other", "code": "DRG-CANON"}},
                   {"verified_source": {"snapshotId": snap["_id"], "code": "ABC1"}},
                   {"verified_source": {"snapshotId": snap["_id"], "code": "DRG-CANON"}, "existing_mapping": ("m", 1)}):
        with pytest.raises(DomainConflict):
            await pa_mapping.canonical_procedure(flow.db, snap["_id"], "DRG-CANON", **kwargs)
    with pytest.raises(DomainConflict):
        await pa_mapping.canonical_procedure(flow.db, snap["_id"], "PRE11", verified_source={"snapshotId": snap["_id"], "code": "PRE11"})


# --- Preparation --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verified_code_resolves_directly_without_mapping_record(flow):
    snap = await trusted(flow)
    await stamp_order(flow, snap["_id"])
    flow.db.pa_mapping_heads.rows.clear(); flow.db.pa_mapping_versions.rows.clear()
    [row] = await calculate(flow)
    assert row["preparationStatus"] == "READY" and row["mappingValidation"] == "VALID"
    assert row["mappingSource"] == VERIFIED_MAPPING_SOURCE and row["mappingId"] is None
    assert row["catalogSnapshotId"] == snap["_id"] and row["medicloudCode"] == "DRG-CANON"
    assert row["lineAmount"] == "1200.00" and row["quantity"] == 4


@pytest.mark.asyncio
async def test_catalog_alone_never_bypasses_other_readiness_checks(flow):
    snap = await trusted(flow)
    await stamp_order(flow, snap["_id"])
    [no_member] = await calculate(flow, evidence=None)
    assert no_member["preparationStatus"] == "BLOCKED" and "MEMBER_EVIDENCE_INVALID_OR_STALE" in no_member["blockingReasons"]
    flow.db.aggregator_users.rows[0]["providerId"] = ""
    [no_provider] = await calculate(flow)
    assert "ISSUEPA_PROVIDER_UNCONFIGURED" in no_provider["blockingReasons"]
    assert no_provider["mappingValidation"] == "VALID"


@pytest.mark.asyncio
async def test_resolution_uses_existing_resolution_freshness(flow, monkeypatch):
    snap = await trusted(flow)
    await stamp_order(flow, snap["_id"])
    real = provenance.now_utc
    monkeypatch.setattr(provenance, "now_utc", lambda: real() + timedelta(hours=25))
    [row] = await calculate(flow)
    assert row["mappingValidation"] == "VALID" and "MAPPING_CATALOG_EVIDENCE_INVALID" not in row["blockingReasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["revoked", "untrusted", "tampered"])
async def test_revocation_or_integrity_failure_blocks_resolution(flow, damage):
    snap = await trusted(flow)
    await stamp_order(flow, snap["_id"])
    stored = next(r for r in flow.db.pa_procedure_catalog_snapshots.rows if r["_id"] == snap["_id"])
    if damage == "revoked": stored["revokedAt"] = now_utc()
    if damage == "untrusted": stored["status"] = "VALIDATED"
    if damage == "tampered":
        next(r for r in flow.db.pa_procedure_catalog_entries.rows
             if r["snapshotId"] == snap["_id"] and r["normalizedCode"] == "DRG-CANON")["procedureDescription"] = "Changed"
    [row] = await calculate(flow)
    assert row["preparationStatus"] == "BLOCKED" and "MAPPING_CATALOG_EVIDENCE_INVALID" in row["blockingReasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize("forgery", ["method", "catalogCode", "procedureCode", "missing"])
async def test_forged_or_inconsistent_verification_is_blocked(flow, forgery):
    snap = await trusted(flow)
    order = await stamp_order(flow, snap["_id"])
    line = order["medications"][0]
    if forgery == "method": line["sourceVerification"]["method"] = UNVERIFIED_METHOD
    if forgery == "catalogCode": line["sourceVerification"]["catalogCode"] = "ABC1"
    if forgery == "procedureCode": line["procedureCode"] = "ABC1"; order["finalProcedurePrices"][0]["procedureCode"] = "ABC1"
    if forgery == "missing": line.pop("sourceVerification")
    [row] = await calculate(flow)
    assert row["preparationStatus"] == "BLOCKED" and "SOURCE_VERIFICATION_INVALID" in row["blockingReasons"]


@pytest.mark.asyncio
async def test_unverified_intake_code_stays_review_required(flow):
    snap = await trusted(flow)
    await stamp_order(flow, snap["_id"], code="UNKNOWN", name="Unknown drug")
    [row] = await calculate(flow)
    assert row["sourceNamespaceId"] == INTAKE_REVIEW_NAMESPACE
    assert row["preparationStatus"] == "REVIEW_REQUIRED" and "ACTIVE_MAPPING_REQUIRED" in row["reviewReasons"]
    assert row["submissionEligible"] is False


@pytest.mark.asyncio
async def test_other_system_namespace_on_a_medication_is_blocked(flow):
    flow.db.orders.rows[0]["medications"][0]["sourceNamespaceId"] = "SYSTEM_DELIVERY"
    [row] = await calculate(flow)
    assert "SOURCE_NAMESPACE_RESERVED" in row["blockingReasons"]


@pytest.mark.asyncio
async def test_reverification_against_a_new_snapshot_makes_preparation_stale(flow):
    first = await trusted(flow)
    await stamp_order(flow, first["_id"])
    await prepare(flow)
    second = await trusted(flow)
    await stamp_order(flow, second["_id"])
    lines = await prep.current_lines(flow.db, flow.batch["_id"], str(flow.order["_id"]), flow.evidence)
    assert lines[0]["isStale"]


@pytest.mark.asyncio
async def test_delivery_line_uses_the_verified_snapshot(flow, monkeypatch):
    monkeypatch.setenv("PA_DELIVERY_DIAGNOSIS_CODE", "Z00")
    snap = await trusted(flow)
    order = await stamp_order(flow, snap["_id"])
    order.update(fulfillmentType="delivered", deliveryFee=500, overallTotal=1700)
    rows = await calculate(flow)
    delivery = next(r for r in rows if r["orderLineId"] == "delivery-PRE11")
    assert delivery["catalogSnapshotId"] == snap["_id"] and delivery["preparationStatus"] == "READY"


# --- Mapping workflow isolation ----------------------------------------------------------

def test_reserved_namespace_detection():
    assert is_reserved_namespace(MEDICLOUD_NAMESPACE) and is_reserved_namespace("SYSTEM_DELIVERY")
    assert not is_reserved_namespace(INTAKE_REVIEW_NAMESPACE) and not is_reserved_namespace("supplier-a")


@pytest.mark.asyncio
async def test_reserved_namespace_cannot_be_proposed_imported_or_revised(flow):
    proposer = Actor(actorId="proposer", canProposeMappings=True)
    reserved = MappingProposal(identity=MappingIdentity(sourceNamespaceId=MEDICLOUD_NAMESPACE, sourceIdentityType="CODE",
        sourceIdentityKey="DRG-CANON"), sourceCode="DRG-CANON", sourceDescription="Canonical medication A",
        proposedMedicloudCode="DRG-CANON", mappingSource="MANUAL")
    with pytest.raises(DomainConflict, match="RESERVED_SOURCE_NAMESPACE"):
        await pa_mapping.propose_mapping(flow.db, proposer, reserved)
    binding = {"identity": reserved.identity.model_dump(), "sourceCode": "DRG-CANON", "sourceDescription": "Canonical medication A"}
    with pytest.raises(DomainConflict, match="RESERVED_SOURCE_NAMESPACE"):
        await pa_mapping_review.append_proposal(flow.db, Actor(actorId="importer", canImportMappings=True), binding,
            {"code": "DRG-CANON", "description": "", "notes": ""}, "export", None, ["row"])
    with pytest.raises(DomainConflict, match="RESERVED_SOURCE_NAMESPACE"):
        await pa_mapping_proposals.immutable_source(flow.db, {**reserved.identity.model_dump()}, None)
    head = flow.db.pa_mapping_heads.rows[0]
    head["sourceNamespaceId"] = MEDICLOUD_NAMESPACE
    with pytest.raises(DomainConflict, match="RESERVED_SOURCE_NAMESPACE"):
        await pa_mapping.revise_mapping(flow.db, ADMIN, head["_id"], head["version"], "PROPOSED")


def test_reserved_namespace_lines_are_never_exportable():
    live = {"mappingValidation": "REVIEW_REQUIRED", "sourceNamespaceId": MEDICLOUD_NAMESPACE, "sourceIdentityKey": "DRG-CANON",
            "sourceDescription": "Canonical medication A", "blockingReasons": ["MAPPING_CATALOG_EVIDENCE_INVALID"], "reviewReasons": []}
    assert not pa_mapping_review.actionable(live)
    assert pa_mapping_review.actionable({**live, "sourceNamespaceId": INTAKE_REVIEW_NAMESPACE})


# --- Intake API --------------------------------------------------------------------------

@pytest.fixture
def intake(flow, monkeypatch):
    db = SimpleNamespace(orders=OrdersCollection(), **{name: flow.db[name] for name in (
        "pa_procedure_catalog_snapshots", "pa_procedure_catalog_entries", "pa_catalog_validation_evidence")})
    monkeypatch.setattr(orders_api, "get_db", lambda: db)
    monkeypatch.setattr(orders_api, "decode_session", lambda token, role=None: {"userId": "staff-7", "name": "Reviewer"} if token == "valid" else None)
    monkeypatch.setattr(orders_api, "compute_review_flags", AsyncMock(return_value={}))
    monkeypatch.setattr(orders_api, "notify_order_created", AsyncMock())
    monkeypatch.setattr(orders_api.sse_manager, "broadcast", AsyncMock())
    monkeypatch.setattr(orders_api, "_PHARMACY_SERVICE_KEY", "service-key-for-tests")
    app = FastAPI(); app.include_router(orders_api.router, prefix="/api")
    with TestClient(app) as client:
        yield client, db


def body(code="DRG-CANON", name="Canonical medication A", **extra):
    return {"enrollee": {"enrolleeId": "E1", "fullName": "Test"}, "provider": {"providerId": "P1", "providerName": "Clinic"},
            "medications": [{"procedureCode": code, "name": name, "dosage": "1 tab", "quantity": 4, "diagnosisCode": "I10",
                             "diagnosis": "Hypertension", **extra}]}


@pytest.mark.asyncio
async def test_staff_intake_stamps_verified_identity_and_ignores_client_fields(flow, intake):
    snap = await trusted(flow, sourceExtractedAt=NOW - timedelta(minutes=10))
    client, db = intake
    client.cookies.set("staff_session", "valid")
    response = client.post("/api/orders", json=body(sourceNamespaceId=MEDICLOUD_NAMESPACE, sourceVerification={"method": VERIFIED_METHOD}))
    assert response.status_code == 201
    [stored] = db.orders.rows[-1]["medications"]
    assert stored["sourceNamespaceId"] == MEDICLOUD_NAMESPACE
    assert stored["sourceVerification"]["snapshotId"] == snap["_id"] and stored["sourceVerification"]["verifiedBy"] == "staff-7"
    assert "sourceVerification" not in response.text


@pytest.mark.asyncio
async def test_service_key_intake_and_unverified_code(flow, intake):
    await trusted(flow, sourceExtractedAt=NOW - timedelta(minutes=10))
    client, db = intake
    response = client.post("/api/orders", json=body("UNKNOWN", "Unknown drug", sourceNamespaceId=MEDICLOUD_NAMESPACE),
                           headers={"X-Service-Key": "service-key-for-tests"})
    assert response.status_code == 201
    [stored] = db.orders.rows[-1]["medications"]
    assert stored["sourceNamespaceId"] == INTAKE_REVIEW_NAMESPACE
    assert stored["sourceVerification"]["verifiedBy"] == "service"


@pytest.mark.asyncio
async def test_medication_update_reverifies(flow, intake):
    await trusted(flow, sourceExtractedAt=NOW - timedelta(minutes=10))
    client, db = intake
    client.cookies.set("staff_session", "valid")
    assert client.post("/api/orders", json=body()).status_code == 201
    order = db.orders.rows[-1]
    assert order["medications"][0]["sourceNamespaceId"] == MEDICLOUD_NAMESPACE
    assert client.put(f'/api/orders/{order["_id"]}', json={"medications": body("ABC1", "Not the catalog label")["medications"]}).status_code == 200
    [updated] = order["medications"]
    assert updated["sourceNamespaceId"] == INTAKE_REVIEW_NAMESPACE
    assert updated["sourceVerification"]["reason"] == "DESCRIPTION_MISMATCH"
