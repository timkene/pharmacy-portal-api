"""Authenticated preparation only. No transmission, catalog or mapping mutations."""
from uuid import UUID
from typing import Literal

from bson import ObjectId
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Response
from pymongo.errors import PyMongoError

from core.database import get_db
from core.security import decode_session
from core import pa_preparation as domain
from core.pa_preparation_store import DomainConflict, now_utc
from models.pa_preparation import Actor, MemberEvidence
from models.pa_preparation_api import BatchVersion, CreateBatch, TeamLine

class SafePreparationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe(request):
            try:
                return await handler(request)
            except PyMongoError:
                raise HTTPException(503, "Preparation storage unavailable") from None
            except RequestValidationError:
                # Never echo unknown uploaded fields, including credential/evidence values.
                raise HTTPException(422, "Invalid preparation request") from None
        return safe


router = APIRouter(route_class=SafePreparationRoute, prefix="/pa-preparation", tags=["pa-preparation"])
READ = "PA_PREPARATION_READ"
WRITE = "PA_PREPARATION_WRITE"
SCAN_LIMIT = 100


async def staff_context(response: Response, staff_session: str | None = Cookie(default=None), db=Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    user = decode_session(staff_session, "staff") if staff_session else None
    if not user or not ObjectId.is_valid(user.get("userId")):
        raise HTTPException(401, "Staff authentication required")
    record = await db.staff_users.find_one({"_id": ObjectId(user["userId"])})
    if not record:
        raise HTTPException(401, "Staff authentication required")
    permissions = record.get("permissions")
    return {"actorId": str(record["_id"]), "permissions": permissions if isinstance(permissions, list) else []}


def require(permission):
    async def dependency(context=Depends(staff_context)):
        if permission not in context["permissions"]:
            raise HTTPException(403, "Preparation permission required")
        return Actor(actorId=context["actorId"], canPrepare=permission == WRITE)
    return dependency


read_actor = require(READ)
write_actor = require(WRITE)


async def member_validation_source():
    """Dependency seam for a trusted full-evidence adapter, never a request body.

    Existing get_member_info returns only member/group/dependant identity, not
    the full MemberEvidence interface. Do not fabricate omitted eligibility
    evidence or make a Mac service a runtime dependency.
    """
    async def unavailable(order):
        return None
    return unavailable


async def evidence_for(source, order):
    try:
        evidence = await source(order)
        return MemberEvidence.model_validate(evidence.model_dump()) if isinstance(evidence, MemberEvidence) else None
    except Exception:
        # Fail closed; do not include external errors or member data in responses.
        return None


async def batch_record(db, batch_id):
    record = await db.pa_preparation_batches.find_one({"_id": str(batch_id)})
    if not record:
        raise HTTPException(404, "Preparation batch not found")
    return record


def batch_view(record):
    return {"batchId": record["_id"], "businessDate": record["businessDate"],
        "status": record["status"], "version": record["version"], "createdAt": record["createdAt"],
        "counts": {"orders": record.get("totalOrders", 0), "lines": record.get("totalLines", 0),
            "ready": record.get("readyLines", 0), "reviewRequired": record.get("reviewRequiredLines", 0),
            "blocked": record.get("blockedLines", 0), "submitted": None, "verificationRequired": None, "stale": None},
        "countsScope": "LAST_CALCULATION", "countsAsOf": record.get("lastCalculatedAt")}


def transmission_view(order, line_id):
    unknown = {"state": "UNKNOWN", "verificationRequired": True, "confirmedPaReference": None}
    empty = {"state": "not_generated", "verificationRequired": False, "confirmedPaReference": None}
    if not isinstance(order, dict):
        return unknown
    ledger = order.get("paGeneration")
    if ledger is None:
        return empty
    if not isinstance(ledger, dict):
        return unknown
    rows = ledger.get("lines", [])
    if not isinstance(rows, list):
        return unknown
    allowed = {"pending", "submitting", "generated", "verification_required", "failed_rejected", "failed_retryable"}
    identifiers = set()
    for row in rows:
        if not isinstance(row, dict):
            return unknown
        identity, state = row.get("lineId"), row.get("status")
        if (not isinstance(identity, str) or not identity.strip() or identity in identifiers
                or not isinstance(state, str) or state not in allowed):
            return unknown
        identifiers.add(identity)
        reference = row.get("paNumber")
        if state == "generated":
            if not isinstance(reference, str) or not reference.strip():
                return unknown
        elif reference not in (None, ""):
            return unknown
    matches = [r for r in rows if r["lineId"] == line_id]
    if not matches:
        # Existing transmission evidence with no interpretable relevant line is uncertain.
        return unknown if rows or ledger.get("attempts") or ledger.get("active") else empty
    line = matches[0]
    state = line["status"]
    return {"state": state, "verificationRequired": state in {"verification_required", "submitting"},
            "confirmedPaReference": line["paNumber"] if state == "generated" else None}


async def line_view(db, row, source, batch, contexts=None):
    contexts = contexts if contexts is not None else {}
    if row["orderId"] not in contexts:
        order = await db.orders.find_one({"_id": ObjectId(row["orderId"])})
        evidence = await evidence_for(source, order) if order else None
        live_rows, aggregator = {}, None
        if order:
            winner = order.get("winnerId")
            aggregator = await db.aggregator_users.find_one({"_id": ObjectId(winner)}) if ObjectId.is_valid(winner) else None
            try:
                live_rows = {r["orderLineId"]: r for r in await domain.calculate(db, order, aggregator, evidence, None, now_utc())}
            except (DomainConflict, ValueError, TypeError, AttributeError, KeyError):
                live_rows = {}
        owner = await db.pa_preparation_contexts.find_one({"_id": row["orderId"]})
        contexts[row["orderId"]] = order, evidence, aggregator, live_rows, owner
    order, evidence, aggregator, live_rows, owner = contexts[row["orderId"]]
    live = live_rows.get(row["orderLineId"])
    stale = (not live or row["inputFingerprint"] != live["inputFingerprint"] or not row["isCurrent"]
             or not owner or owner["batchId"] != row["batchId"] or batch["status"] == "CLOSED")
    reasons = sorted(set((live or row).get("blockingReasons", []) + (live or row).get("reviewReasons", [])))
    if not live:
        reasons.append("ORDER_LINE_NOT_CURRENT_OR_STRUCTURE_INVALID")
    if not evidence:
        reasons.append("MEMBER_VALIDATION_UNAVAILABLE")
    if stale:
        reasons.append("PREPARATION_STALE")
    status = live["preparationStatus"] if live else "BLOCKED"
    if not evidence:
        status = "BLOCKED"
    if stale and status == "READY":
        status = "REVIEW_REQUIRED"
    mapped = live or row
    valid = bool(evidence and order and domain.valid_member(evidence, order, now_utc()))
    if evidence and not valid:
        status = "BLOCKED"
        reasons.append("MEMBER_EVIDENCE_INVALID_OR_STALE")
    referring = order.get("provider") if order else None
    referring = referring if isinstance(referring, dict) else {}
    effective_mapping = bool(live and live.get("mappingValidation") == "VALID"
        and isinstance(live.get("medicloudCode"), str) and live["medicloudCode"].strip()
        and isinstance(live.get("medicloudDescription"), str) and live["medicloudDescription"].strip())
    mapping_status = "UNMAPPED"
    if effective_mapping:
        mapping_status = "VALIDATED" if live.get("mappingSource") == "RESERVED_DELIVERY_CONTRACT" else "ACTIVE"
    elif live and mapped.get("sourceNamespaceId"):
        head = await db.pa_mapping_heads.find_one({"sourceNamespaceId": mapped["sourceNamespaceId"],
            "sourceIdentityType": mapped["sourceIdentityType"], "sourceIdentityKey": mapped["sourceIdentityKey"]})
        # An active head is not evidence that this particular line can use it.
        if head and not head.get("activeVersion"):
            candidate = await db.pa_mapping_versions.find_one({"mappingId": head["_id"], "version": head["version"]})
            if (candidate and candidate.get("sourceDescription") == live.get("sourceDescription")
                    and candidate.get("status") in {"PROPOSED", "VALIDATED", "REJECTED", "RETIRED"}):
                mapping_status = candidate["status"]
    transmission = transmission_view(order, row["orderLineId"])
    if transmission["state"] == "UNKNOWN":
        status = "BLOCKED"
        reasons.append("PA_LEDGER_INVALID")
    return TeamLine(batchId=row["batchId"], preparationLineId=row["_id"], preparationRevision=row["revision"],
        orderId=row["orderId"], orderLineId=row["orderLineId"], calculatedAt=row["calculatedAt"],
        provider={"referringProviderId": mapped.get("referringProviderId"),
            "referringProviderDisplay": referring.get("providerName") if isinstance(referring.get("providerName"), str) else None,
            "aggregatorId": mapped.get("aggregatorId"), "aggregatorDisplay": aggregator.get("companyName") if aggregator and isinstance(aggregator.get("companyName"), str) else None,
            "issuePaProviderId": mapped.get("issuePaProviderId")},
        medication={"sourceCode": row.get("sourceCode"), "sourceDescription": row.get("sourceDescription"),
            "quantity": row.get("quantity"), "dosage": row.get("serviceInput", {}).get("dosage") if isinstance(row.get("serviceInput", {}).get("dosage"), str) else None},
        pricing={"approvedLineAmount": row.get("lineAmount"), "currency": row.get("currency"), "priceSource": row.get("priceSource")},
        mapping={"sourceNamespaceId": mapped.get("sourceNamespaceId"),
            "mappingStatus": mapping_status,
            "mappedMediCloudCode": live.get("medicloudCode") if effective_mapping else None,
            "mappedMediCloudDescription": live.get("medicloudDescription") if effective_mapping else None},
        validation={"member": "VALID" if valid else "UNAVAILABLE" if not evidence else "INVALID",
            "memberFreshAsOf": evidence.validatedAt if valid else None,
            "memberFreshUntil": evidence.expiresAt if valid else None,
            "price": mapped.get("pricingValidation"), "provider": mapped.get("providerValidation"), "mapping": mapped.get("mappingValidation")},
        preparationStatus=status, reasonCodes=sorted(set(reasons)), isStale=stale,
        submissionEligible=bool(status == "READY" and not stale and valid and live and live["submissionEligible"]),
        transmission=transmission).model_dump()


def safe_row_identifier(value, kind):
    if kind == "order":
        return str(ObjectId(value)) if ObjectId.is_valid(value) else None
    if kind == "uuid":
        try:
            return str(UUID(str(value))) if isinstance(value, (str, UUID)) else None
        except (ValueError, TypeError, AttributeError):
            return None
    return None


def invalid_row(row, batch_id):
    # No display/source content is copied from a malformed row. IDs only.
    row = row if isinstance(row, dict) else {}
    return {"batchId": str(batch_id), "preparationLineId": safe_row_identifier(row.get("_id"), "uuid"),
        "orderId": safe_row_identifier(row.get("orderId"), "order"),
        "preparationStatus": "BLOCKED", "reasonCodes": ["PREPARATION_ROW_INVALID"],
        "isStale": True, "submissionEligible": False,
        "provider": {}, "medication": {}, "pricing": {},
        "mapping": {"mappingStatus": "UNMAPPED", "mappedMediCloudCode": None, "mappedMediCloudDescription": None},
        "validation": {"member": "UNAVAILABLE"},
        "transmission": {"state": "UNKNOWN", "verificationRequired": True, "confirmedPaReference": None}}


async def projected_row(db, row, source, batch, contexts):
    try:
        item = await line_view(db, row, source, batch, contexts)
        # Ensure values are JSON-safe before FastAPI serializes the entire page.
        return TeamLine.model_validate(item).model_dump(mode="json")
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
        return invalid_row(row, batch["_id"])


@router.post("/batches")
async def create(body: CreateBatch, actor=Depends(write_actor), db=Depends(get_db)):
    try:
        return batch_view(await domain.create_batch(db, actor, body.businessDate.isoformat()))
    except DomainConflict:
        raise HTTPException(409, "Preparation concurrency conflict") from None
    except PyMongoError:
        raise HTTPException(503, "Preparation storage unavailable") from None


@router.get("/batches")
async def batches(limit: int = Query(25, ge=1, le=100), cursor: UUID | None = None,
                  actor=Depends(read_actor), db=Depends(get_db)):
    query = {"_id": {"$gt": str(cursor)}} if cursor else {}
    rows = await db.pa_preparation_batches.find(query).sort("_id", 1).limit(limit + 1).to_list(limit + 1)
    return {"items": [batch_view(r) for r in rows[:limit]], "nextCursor": rows[limit - 1]["_id"] if len(rows) > limit else None}


@router.get("/batches/{batch_id}")
async def batch(batch_id: UUID, actor=Depends(read_actor), db=Depends(get_db)):
    return batch_view(await batch_record(db, batch_id))


@router.get("/batches/{batch_id}/lines")
async def lines(batch_id: UUID, limit: int = Query(25, ge=1, le=50), cursor: UUID | None = None,
                status: Literal["READY", "REVIEW_REQUIRED", "BLOCKED"] | None = None,
                mappingStatus: Literal["ACTIVE", "PROPOSED", "VALIDATED", "REJECTED", "RETIRED", "UNMAPPED"] | None = None,
                aggregatorId: str | None = Query(None, max_length=100), providerId: str | None = Query(None, max_length=100),
                stale: bool | None = None, actor=Depends(read_actor), db=Depends(get_db), source=Depends(member_validation_source)):
    record = await batch_record(db, batch_id)
    query = {"batchId": str(batch_id)}
    if cursor: query["_id"] = {"$gt": str(cursor)}
    candidates = await db.pa_preparation_lines.find(query).sort("_id", 1).limit(SCAN_LIMIT + 1).to_list(SCAN_LIMIT + 1)
    items, last, scanned, contexts = [], None, 0, {}
    for row in candidates[:SCAN_LIMIT]:
        scanned += 1
        safe_id = safe_row_identifier(row.get("_id"), "uuid") if isinstance(row, dict) else None
        if safe_id: last = safe_id
        try:
            if (not isinstance(row.get("orderId"), str) or not ObjectId.is_valid(row["orderId"])
                    or not isinstance(row.get("orderLineId"), str) or not row["orderLineId"].strip()
                    or type(row.get("revision")) is not int or row["revision"] < 1):
                raise ValueError("Invalid row identity")
            newer = await db.pa_preparation_lines.find_one({"batchId": str(batch_id), "orderId": row["orderId"],
                "orderLineId": row["orderLineId"], "revision": {"$gt": row["revision"]}})
            if newer: continue
            item = await projected_row(db, row, source, record, contexts)
        except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
            item = invalid_row(row, batch_id)
        if status and item["preparationStatus"] != status: continue
        if mappingStatus and item["mapping"]["mappingStatus"] != mappingStatus: continue
        if aggregatorId and item["provider"].get("aggregatorId") != aggregatorId: continue
        if providerId and item["provider"].get("issuePaProviderId") != providerId: continue
        if stale is not None and item["isStale"] != stale: continue
        items.append(item)
        if len(items) == limit: break
    more = scanned < len(candidates)
    return {"items": items, "nextCursor": last if more else None, "counts": batch_view(record)["counts"],
        "countsScope": "LAST_CALCULATION", "countsAsOf": record.get("lastCalculatedAt"),
        "pageCounts": {"lines": len(items), "stale": sum(i["isStale"] for i in items),
            "submitted": sum(i["transmission"]["state"] == "generated" for i in items),
            "verificationRequired": sum(i["transmission"]["verificationRequired"] for i in items)}}


@router.post("/batches/{batch_id}/orders/{order_id}/prepare")
@router.post("/batches/{batch_id}/orders/{order_id}/recalculate")
async def prepare(batch_id: UUID, order_id: str, body: BatchVersion, actor=Depends(write_actor),
                  db=Depends(get_db), source=Depends(member_validation_source), context=Depends(staff_context)):
    record = await batch_record(db, batch_id)
    if not ObjectId.is_valid(order_id): raise HTTPException(404, "Order not found")
    order_id = str(ObjectId(order_id))
    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order: raise HTTPException(404, "Order not found")
    evidence = await evidence_for(source, order)
    try:
        rows = await domain.prepare_order(db, actor, str(batch_id), order_id, body.expectedVersion, evidence)
    except DomainConflict as exc:
        if str(exc) == "ORDER_ALREADY_IN_ACTIVE_BATCH":
            owner = await db.pa_preparation_contexts.find_one({"_id": order_id})
            raise HTTPException(409, {"code": "ORDER_ALREADY_IN_ACTIVE_BATCH", "owningBatchId": owner.get("batchId") if owner and READ in context["permissions"] else None}) from None
        raise HTTPException(409, {"code": "PREPARATION_DOMAIN_CONFLICT"}) from None
    except PyMongoError:
        raise HTTPException(503, "Preparation storage unavailable") from None
    except (ValueError, TypeError, AttributeError, KeyError):
        raise HTTPException(409, {"code": "LEGACY_ORDER_STRUCTURE_INVALID"}) from None
    updated = await batch_record(db, batch_id)
    contexts = {}
    return {"batch": batch_view(updated), "items": [await projected_row(db, row, lambda_source(evidence), updated, contexts) for row in rows]}


def lambda_source(evidence):
    async def known(order): return evidence
    return known


@router.post("/batches/{batch_id}/close")
async def close(batch_id: UUID, body: BatchVersion, actor=Depends(write_actor), db=Depends(get_db)):
    await batch_record(db, batch_id)
    try:
        await domain.close_batch(db, actor, str(batch_id), body.expectedVersion)
    except DomainConflict:
        raise HTTPException(409, "Preparation concurrency conflict") from None
    except PyMongoError:
        raise HTTPException(503, "Preparation storage unavailable") from None
    return batch_view(await batch_record(db, batch_id))
