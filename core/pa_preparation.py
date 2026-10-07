"""Preparation engine: read orders, append snapshots, never transmit or alter orders."""
import hashlib
import json
import os
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from bson import ObjectId
from fastapi import HTTPException

from core.pa_client import dependant_number, member_flag, _DEPENDANT_NUMBER_ALIASES
from core.pa_mapping import DomainConflict, canonical_procedure, normalized_code, require_permission
from core.pa_preparation_store import event, now_utc, transaction
from core.pricing import money, price_lines, subtotal
from models.pa_preparation import BatchRecord, MemberEvidence, PreparationLineEvidence, MEMBER_EVIDENCE_MAX_AGE


def fingerprint(value):
    def encode(item):
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, ObjectId):
            return str(item)
        raise TypeError("Unsupported fingerprint input")
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    default=encode, allow_nan=False).encode()).hexdigest()


def enrollee_identifier(order):
    enrollee = order.get("enrollee")
    value = enrollee.get("enrolleeId") if isinstance(enrollee, dict) else None
    return value.strip().casefold() if isinstance(value, str) else ""


def canonical_order_id(value):
    if not ObjectId.is_valid(value):
        raise DomainConflict("ORDER_ID_INVALID")
    return str(ObjectId(value))


def material_fields(fields):
    """Evidence receipts/freshness are audited separately from material PA identity."""
    result = dict(fields)
    member = result.pop("memberSnapshot", None)
    if member:
        result["memberMaterial"] = {k: member[k] for k in
            ("memberId", "groupId", "groupLegacyId", "isPrincipal", "dependantId", "isTerminated", "isDeleted", "enrolleeFingerprint")}
        result["memberMaterial"]["dependantId"] = "0" if member["isPrincipal"] else dependant_number(member["dependantId"])
    else:
        result["memberMaterial"] = None
    return result


def member_evidence(lookup, *, order, evidence_id, validated_at, expires_at):
    """Normalize an already obtained authoritative lookup; does not perform a lookup.

    This adapter must only be called by trusted server validation code. Uploaded
    rows must never be accepted as MemberEvidence.
    """
    rows = lookup.get("results")
    if (lookup.get("found") is not True or lookup.get("count") != 1 or lookup.get("truncated")
            or not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)):
        raise ValueError("MEMBER_AMBIGUOUS")
    row = rows[0]
    enrollee = enrollee_identifier(order)
    if not enrollee or str(row.get("legacycode", "")).strip().casefold() != enrollee:
        raise ValueError("MEMBER_IDENTITY_MISMATCH")
    principal = member_flag(row.get("isprincipal"))
    terminated, deleted = member_flag(row.get("isterminated")), member_flag(row.get("isdeleted"))
    raw = row.get("dependantid")
    dependant = dependant_number(raw) if raw is not None else None
    if (principal is None or terminated is not False or deleted is not False
            or (raw is not None and dependant is None)
            or (principal and dependant not in (None, "0"))
            or (principal is False and (dependant is None or int(dependant) <= 0))):
        raise ValueError("MEMBER_INELIGIBLE_OR_AMBIGUOUS")
    expected = "0" if principal else dependant
    for alias in _DEPENDANT_NUMBER_ALIASES:
        if row.get(alias) not in (None, "") and dependant_number(row[alias]) != expected:
            raise ValueError("MEMBER_IDENTITY_CONFLICT")
    member_id = row.get("memberid")
    legacy = row.get("group_legacy_id")
    if (isinstance(member_id, bool) or not str(member_id or "").isdigit() or int(member_id) <= 0
            or not isinstance(legacy, str) or not legacy.strip()):
        raise ValueError("MEMBER_IDENTITY_MISSING")
    return MemberEvidence(evidenceId=evidence_id, validatedAt=validated_at, expiresAt=expires_at,
        orderId=str(order["_id"]), orderVersion=order.get("version", 0),
        enrolleeFingerprint=fingerprint(enrollee), memberId=str(member_id),
        groupId=str(row["groupid"]) if row.get("groupid") is not None else None,
        groupLegacyId=legacy.strip(), isPrincipal=principal, dependantId=dependant,
        isTerminated=False, isDeleted=False)


def valid_member(evidence, order, now):
    if evidence is None:
        return False
    if (not enrollee_identifier(order)
            or evidence.orderId != str(order["_id"]) or evidence.orderVersion != order.get("version", 0)
            or evidence.enrolleeFingerprint != fingerprint(enrollee_identifier(order))
            or not evidence.validatedAt <= now < evidence.expiresAt
            or not evidence.validatedAt < evidence.expiresAt <= evidence.validatedAt + MEMBER_EVIDENCE_MAX_AGE
            or evidence.isTerminated or evidence.isDeleted
            or not evidence.groupLegacyId.strip() or not evidence.memberId.isdigit() or int(evidence.memberId) <= 0):
        return False
    dependant = dependant_number(evidence.dependantId) if evidence.dependantId is not None else None
    if evidence.dependantId is not None and dependant is None:
        return False
    return ((evidence.isPrincipal and dependant in (None, "0"))
            or (not evidence.isPrincipal and dependant is not None and int(dependant) > 0))


async def create_batch(db, actor, business_date):
    require_permission(actor, "canPrepare")
    from datetime import date
    date.fromisoformat(business_date)
    record = BatchRecord(businessDate=business_date, version=0, createdAt=now_utc(), createdBy=actor.actorId)
    doc = {"_id": str(uuid4()), **record.model_dump(mode="python")}
    async with transaction(db) as session:
        await db.pa_preparation_batches.insert_one(doc, session=session)
        await event(db, session, actor, "batch_created", str(uuid4()), batchId=doc["_id"])
    return doc


def price_evidence(order):
    meds = order.get("medications", [])
    # Quotes and legacy aggregate amounts are not approved price evidence.
    source = "finalProcedurePrices" if order.get("finalProcedurePrices") else "approvedProcedurePrices"
    prices = order.get(source)
    try:
        if source == "approvedProcedurePrices" and not order.get("priceApprovedAt"):
            raise ValueError("Price approval absent")
        valid = price_lines(meds, prices)
        if money(subtotal(valid)) != money(order.get("medicationSubtotal")):
            raise ValueError("Subtotal mismatch")
        if source == "finalProcedurePrices":
            fee = order.get("deliveryFee")
            if order.get("fulfillmentType") == "delivered":
                fee = money(fee)
            elif fee is None and (order.get("fulfillmentType") == "picked_up"
                    or (order.get("fulfillmentType") is None and order.get("status") != "completed")):
                fee = 0
            else:
                raise ValueError("Fulfilment mismatch")
            if money(order.get("overallTotal")) != money(subtotal(valid) + float(fee)):
                raise ValueError("Overall total mismatch")
        return {r["medicationLineId"]: format(money(r["amount"]), ".2f") for r in valid}, source, True
    except (HTTPException, ValueError, TypeError, KeyError, AttributeError):
        return {}, source, False


async def calculate(db, order, aggregator, evidence, session, now, catalog_snapshot_id=None):
    """Pure with respect to orders/ledger; only reads mapping/catalog collections."""
    amounts, price_source, prices_valid = price_evidence(order)
    meds = order.get("medications") or []
    if not meds:
        raise DomainConflict("ORDER_MEDICATIONS_REQUIRED")
    member_valid = valid_member(evidence, order, now)
    raw_provider = aggregator.get("providerId") if aggregator else None
    provider = raw_provider.strip() if isinstance(raw_provider, str) else None
    provider_valid = (aggregator is not None and str(aggregator["_id"]) == order.get("winnerId")
                      and isinstance(provider, str) and bool(provider.strip()))
    raw_ledger = order.get("paGeneration")
    ledger = raw_ledger if isinstance(raw_ledger, dict) else {}
    attempts = ledger.get("attempts") or []
    raw_lines = ledger.get("lines", [])
    ledger_invalid = (raw_ledger is not None and not isinstance(raw_ledger, dict)) or not isinstance(raw_lines, list)
    ledger_lines = raw_lines if isinstance(raw_lines, list) else []
    ledger_invalid = ledger_invalid or any(not isinstance(r, dict) for r in ledger_lines)
    ledger_ids = set()
    for entry in ledger_lines:
        if not isinstance(entry, dict):
            continue
        identity, state, reference = entry.get("lineId"), entry.get("status"), entry.get("paNumber")
        if (not isinstance(identity, str) or not identity.strip() or identity in ledger_ids
                or state not in ("pending", "submitting", "generated", "verification_required", "failed_rejected", "failed_retryable")
                or (state == "generated" and (not isinstance(reference, str) or not reference.strip()))
                or (state != "generated" and reference not in (None, ""))):
            ledger_invalid = True
        if isinstance(identity, str): ledger_ids.add(identity)
    transmitted = ledger_invalid or bool(attempts) or any(r.get("submittedAt") or r.get("submissionGroupId")
        or r.get("status") not in (None, "pending") for r in ledger_lines if isinstance(r, dict))
    meds = [m if isinstance(m, dict) else {} for m in meds]
    duplicate_ids = len({m.get("lineId") for m in meds if isinstance(m.get("lineId"), str)}) != len(meds)
    result = []
    for position, med in enumerate(meds):
        blocked, review = [], []
        malformed = any(med.get(key) is not None and not isinstance(med.get(key), str)
                        for key in ("procedureCode", "sourceCode", "name", "sourceDescription", "sourceNamespaceId", "lineId"))
        if malformed or not isinstance(med.get("name"), str) or not med.get("name", "").strip():
            blocked.append("LEGACY_MEDICATION_FIELDS_INVALID")
        med = dict(med)
        for key in ("procedureCode", "sourceCode", "name", "sourceDescription", "sourceNamespaceId", "lineId"):
            if key in med and not isinstance(med[key], str):
                med[key] = None

        if not member_valid:
            blocked.append("MEMBER_EVIDENCE_INVALID_OR_STALE")
        if not ObjectId.is_valid(order.get("winnerId")):
            blocked.append("ASSIGNMENT_IDENTITY_INVALID")
        if not provider_valid:
            blocked.append("ISSUEPA_PROVIDER_UNCONFIGURED")
        if not prices_valid:
            blocked.append("APPROVED_PRICE_EVIDENCE_INVALID")
        if (not med.get("lineId") or duplicate_ids or len(meds) > 10
                or not isinstance(med.get("quantity"), int) or isinstance(med.get("quantity"), bool)
                or med["quantity"] <= 0 or not isinstance(med.get("diagnosisCode"), str)
                or not med["diagnosisCode"].strip() or med.get("procedureCode") == "PRE11"):
            blocked.append("MEDICATION_IDENTITY_INVALID")
        # Existing procedureCode remains the priced source code. Never mutate it.
        source_code = med.get("sourceCode", med.get("procedureCode"))
        description = med.get("sourceDescription") or med.get("name") or ""
        namespace = med.get("sourceNamespaceId")
        identity_type = "CODE" if source_code else "DESCRIPTION"
        identity_key = normalized_code(source_code) if isinstance(source_code, str) and source_code.strip() else str(description).strip().casefold()
        mapping = None
        if not isinstance(namespace, str) or not namespace.strip() or not identity_key:
            review.append("SOURCE_NAMESPACE_OR_IDENTITY_REQUIRED")
        else:
            head = await db.pa_mapping_heads.find_one({"sourceNamespaceId": namespace,
                "sourceIdentityType": identity_type, "sourceIdentityKey": identity_key}, session=session)
            if head and head.get("activeVersion"):
                mapping = await db.pa_mapping_versions.find_one(
                    {"mappingId": head["_id"], "version": head["activeVersion"]}, session=session)
            if not mapping or mapping.get("status") != "ACTIVE" or not mapping.get("review"):
                mapping = None
                review.append("ACTIVE_MAPPING_REQUIRED")
            elif mapping.get("sourceDescription") != description:
                mapping = None
                review.append("SOURCE_DESCRIPTION_CHANGED")
            else:
                try:
                    entry = await canonical_procedure(db, mapping["snapshotId"], mapping["medicloudCode"], session)
                    if (entry["procedureCode"] != mapping["medicloudCode"]
                            or entry["procedureDescription"] != mapping["medicloudDescription"]):
                        raise DomainConflict("CATALOG_EVIDENCE_MISMATCH")
                    if entry["procedureCode"] == "PRE11":
                        raise DomainConflict("PRE11_RESERVED_FOR_DELIVERY")
                except DomainConflict:
                    blocked.append("MAPPING_CATALOG_EVIDENCE_INVALID")
        if ledger_invalid:
            blocked.append("PA_LEDGER_INVALID")
        referring = order.get("provider")
        if not isinstance(referring, dict) or not isinstance(referring.get("providerId"), str) or not referring["providerId"].strip():
            blocked.append("REFERRING_PROVIDER_IDENTITY_INVALID")
        if transmitted:
            blocked.append("EXISTING_PA_LEDGER_REQUIRES_CONTROLLED_SUBMISSION_OR_RECOVERY")
        if order.get("status") in {"cancelled", "rejected", "recalled"}:
            blocked.append("ORDER_NOT_PREPARABLE")
        fields = {"orderId": str(order["_id"]), "orderLineId": med.get("lineId") or f"invalid-{position}",
            "orderVersion": order.get("version", 0), "assignmentVersion": order.get("assignmentVersion", 0),
            "referringProviderId": referring["providerId"].strip() if isinstance(referring, dict) and isinstance(referring.get("providerId"), str) else None,
            "aggregatorId": order.get("winnerId") if isinstance(order.get("winnerId"), str) else None, "issuePaProviderId": provider,
            "sourceNamespaceId": namespace, "sourceIdentityType": identity_type,
            "sourceIdentityKey": identity_key, "sourceCode": source_code, "sourceDescription": description,
            "diagnosisCode": med.get("diagnosisCode").strip() if isinstance(med.get("diagnosisCode"), str) else None, "quantity": med.get("quantity") if type(med.get("quantity")) is int else None,
            "serviceInput": {key: med.get(key) for key in ("procedureCode", "dosage", "tablets", "frequency", "durationDays", "quantity")},
            "lineAmount": amounts.get(med.get("lineId")), "currency": "NGN", "priceSource": price_source,
            "memberSnapshot": evidence.model_dump() if evidence else None,
            "mappingId": mapping["mappingId"] if mapping else None,
            "mappingVersion": mapping["version"] if mapping else None,
            "mappingSource": mapping.get("mappingSource") if mapping else None,
            "candidateConfidence": mapping.get("candidateConfidence") if mapping else None,
            "catalogSnapshotId": mapping.get("snapshotId") if mapping else None,
            "medicloudCode": mapping.get("medicloudCode") if mapping else None,
            "medicloudDescription": mapping.get("medicloudDescription") if mapping else None,
            "memberValidation": "VALID" if member_valid else "INVALID",
            "pricingValidation": "VALID" if prices_valid else "INVALID",
            "providerValidation": "VALID" if provider_valid else "INVALID",
            "mappingValidation": "VALID" if mapping and not review and "MAPPING_CATALOG_EVIDENCE_INVALID" not in blocked else "REVIEW_REQUIRED",
            "providerTariffValidation": "NOT_IMPLEMENTED", "tariffPriceValidation": "NOT_IMPLEMENTED",
            "preparationStatus": "BLOCKED" if blocked else "REVIEW_REQUIRED" if review else "READY",
            "blockingReasons": sorted(blocked), "reviewReasons": sorted(review),
            "submissionEligible": False, "submissionEligibilityReasons": []}
        fields["inputFingerprint"] = fingerprint({**material_fields(fields), "rawMedicationFingerprint": fingerprint(meds[position]), "rawAssignmentFingerprint": fingerprint(order.get("winnerId")), "orderStatus": order.get("status"),
            "pricingEvidence": {key: order.get(key) for key in ("approvedProcedurePrices", "finalProcedurePrices", "medicationSubtotal")},
            "priceApprovedAt": order.get("priceApprovedAt"), "fulfillmentType": order.get("fulfillmentType"),
            "deliveryFee": order.get("deliveryFee"), "overallTotal": order.get("overallTotal"),
            "paLedgerFingerprint": fingerprint(ledger)})
        result.append(fields)
    if order.get("fulfillmentType") == "delivered":
        # Reserved delivery contract from the reviewed sender; no medication mapping
        # may target PRE11. Its canonical description still requires trusted evidence.
        snapshots = {r["catalogSnapshotId"] for r in result if r["catalogSnapshotId"]}
        delivery_snapshot = catalog_snapshot_id or (next(iter(snapshots)) if len(snapshots) == 1 else None)
        delivery = deepcopy(result[0])
        delivery.pop("inputFingerprint")
        shared = {"MEMBER_EVIDENCE_INVALID_OR_STALE", "ISSUEPA_PROVIDER_UNCONFIGURED",
                  "APPROVED_PRICE_EVIDENCE_INVALID", "ORDER_NOT_PREPARABLE", "PA_LEDGER_INVALID", "REFERRING_PROVIDER_IDENTITY_INVALID",
                  "EXISTING_PA_LEDGER_REQUIRES_CONTROLLED_SUBMISSION_OR_RECOVERY"}
        blocked = [r for r in delivery["blockingReasons"] if r in shared]
        code = os.getenv("PA_DELIVERY_DIAGNOSIS_CODE", "").strip()
        if not code:
            blocked.append("DELIVERY_DIAGNOSIS_UNCONFIGURED")
        try:
            amount = format(money(order.get("deliveryFee")), ".2f")
        except HTTPException:
            amount = None
            blocked.append("DELIVERY_PRICE_INVALID")
        entry = None
        try:
            entry = await canonical_procedure(db, delivery_snapshot, "PRE11", session)
        except DomainConflict:
            blocked.append("DELIVERY_CATALOG_EVIDENCE_REQUIRED")
        delivery.update(orderLineId="delivery-PRE11", sourceNamespaceId="SYSTEM_DELIVERY",
            sourceIdentityType="CODE", sourceIdentityKey="PRE11", sourceCode="PRE11",
            sourceDescription="Pharmacy Delivery", serviceInput={"procedureCode": "PRE11"},
            diagnosisCode=code or None, quantity=1, lineAmount=amount, priceSource="deliveryFee",
            mappingId=None, mappingVersion=None, mappingSource="RESERVED_DELIVERY_CONTRACT",
            candidateConfidence=None, catalogSnapshotId=delivery_snapshot,
            medicloudCode=entry["procedureCode"] if entry else None,
            medicloudDescription=entry["procedureDescription"] if entry else None,
            mappingValidation="VALID" if entry else "REVIEW_REQUIRED",
            preparationStatus="BLOCKED" if blocked else "READY", blockingReasons=sorted(blocked),
            reviewReasons=[], submissionEligible=False, submissionEligibilityReasons=[])
        delivery["inputFingerprint"] = fingerprint({**material_fields(delivery), "paLedgerFingerprint": fingerprint(ledger)})
        result.append(delivery)
    group_ready = all(row["preparationStatus"] == "READY" for row in result)
    for row in result:
        row["submissionEligible"] = group_ready and order.get("status") == "completed" and not transmitted
        row["submissionEligibilityReasons"] = (["ORDER_NOT_COMPLETED"] if order.get("status") != "completed" else []) + ([] if group_ready else ["ORDER_PREPARATION_NOT_READY"])
        # Include the final group eligibility in deterministic evidence.
        row["inputFingerprint"] = fingerprint({"lineFingerprint": row["inputFingerprint"],
            "submissionEligible": row["submissionEligible"], "submissionEligibilityReasons": row["submissionEligibilityReasons"]})
    return result


async def prepare_order(db, actor, batch_id, order_id, expected_batch_version, evidence=None, catalog_snapshot_id=None):
    """Serialize publication through batch CAS; immutable line content, mutable current pointer.

    READY is evidence as-of calculation, not a submission gate. Source updates
    after calculation are checked by current_lines and again by future submission.
    No retries of transactions or external operations are performed here.
    """
    require_permission(actor, "canPrepare")
    order_id = canonical_order_id(order_id)
    if evidence is not None and not isinstance(evidence, MemberEvidence):
        raise TypeError("Internal normalized MemberEvidence required")
    async with transaction(db) as session:
        batch = await db.pa_preparation_batches.find_one({"_id": batch_id}, session=session)
        if not batch or batch["status"] not in {"OPEN", "AUDITING"} or batch["version"] != expected_batch_version:
            raise DomainConflict("BATCH_VERSION_CONFLICT")
        owner = await db.pa_preparation_contexts.find_one({"_id": order_id}, session=session)
        if owner and owner["batchId"] != batch_id:
            raise DomainConflict("ORDER_ALREADY_IN_ACTIVE_BATCH")
        if not owner:
            await db.pa_preparation_contexts.insert_one({"_id": order_id, "batchId": batch_id}, session=session)
        order = await db.orders.find_one({"_id": ObjectId(order_id)}, session=session)
        if not order:
            raise DomainConflict("ORDER_NOT_FOUND")
        aggregator = await db.aggregator_users.find_one({"_id": ObjectId(order["winnerId"])}, session=session) if ObjectId.is_valid(order.get("winnerId", "")) else None
        now = now_utc()
        calculated = await calculate(db, order, aggregator, evidence, session, now, catalog_snapshot_id)
        prior = await db.pa_preparation_lines.find({"batchId": batch_id, "orderId": order_id}, session=session).to_list(None)
        result = []
        correlation = str(uuid4())
        await db.pa_preparation_lines.update_many({"batchId": batch_id, "orderId": order_id, "isCurrent": True},
            {"$set": {"isCurrent": False, "staleReason": "RECALCULATED"}}, session=session)
        for fields in calculated:
            # Validate the bounded persistence contract before publishing any revision.
            PreparationLineEvidence.model_validate(fields)
            versions = [p for p in prior if p["orderLineId"] == fields["orderLineId"]]
            latest = max(versions, key=lambda p: p["revision"]) if versions else None
            if latest and latest["inputFingerprint"] == fields["inputFingerprint"]:
                await db.pa_preparation_lines.update_one({"_id": latest["_id"]},
                    {"$set": {"isCurrent": True}, "$unset": {"staleReason": ""}}, session=session)
                result.append({**latest, "isCurrent": True})
                continue
            revision = latest["revision"] + 1 if latest else 1
            doc = {"_id": str(uuid4()), "batchId": batch_id, **fields, "revision": revision,
                   "isCurrent": True, "calculatedAt": now, "createdBy": actor.actorId,
                   "supersedesRevision": latest["revision"] if latest else None}
            await db.pa_preparation_lines.insert_one(doc, session=session)
            await event(db, session, actor, "line_prepared", correlation, batchId=batch_id,
                        preparationLineId=doc["_id"], preparationRevision=revision)
            result.append(doc)
        current = await db.pa_preparation_lines.find({"batchId": batch_id, "isCurrent": True}, session=session).to_list(None)
        counts = {"totalOrders": len({r["orderId"] for r in current}), "totalLines": len(current)}
        for status, name in (("READY", "readyLines"), ("REVIEW_REQUIRED", "reviewRequiredLines"), ("BLOCKED", "blockedLines")):
            counts[name] = sum(r["preparationStatus"] == status for r in current)
        updated = await db.pa_preparation_batches.update_one({"_id": batch_id, "version": expected_batch_version},
            {"$set": {**counts, "lastCalculatedAt": now}, "$inc": {"version": 1}}, session=session)
        if updated.matched_count != 1:
            raise DomainConflict("BATCH_VERSION_CONFLICT")
        await event(db, session, actor, "batch_recalculated", correlation, batchId=batch_id,
                    previousVersion=expected_batch_version, newVersion=expected_batch_version + 1,
                    memberEvidence={k: getattr(evidence, k) for k in ("evidenceId", "validatedAt", "expiresAt")} if evidence else None)
    return deepcopy(result)


async def current_lines(db, batch_id, order_id, evidence=None, catalog_snapshot_id=None):
    """Read contract for future team API: detect stale inputs without rewriting history."""
    order_id = canonical_order_id(order_id)
    async with transaction(db) as session:
        order = await db.orders.find_one({"_id": ObjectId(order_id)}, session=session)
        if not order:
            return []
        aggregator = await db.aggregator_users.find_one({"_id": ObjectId(order["winnerId"])}, session=session) if ObjectId.is_valid(order.get("winnerId", "")) else None
        live = {r["orderLineId"]: r for r in await calculate(db, order, aggregator, evidence, session, now_utc(), catalog_snapshot_id)}
        rows = await db.pa_preparation_lines.find({"batchId": batch_id, "orderId": order_id}, session=session).to_list(None)
        latest = {}
        for row in rows:
            if row["orderLineId"] not in latest or latest[row["orderLineId"]]["revision"] < row["revision"]:
                latest[row["orderLineId"]] = row
        owner = await db.pa_preparation_contexts.find_one({"_id": order_id}, session=session)
        for row in latest.values():
            row["isStale"] = (not owner or owner["batchId"] != batch_id or not row["isCurrent"] or row["inputFingerprint"] != live.get(row["orderLineId"], {}).get("inputFingerprint"))
            if row["isStale"]:
                row["submissionEligible"] = False
        return list(latest.values())


async def close_batch(db, actor, batch_id, expected_version):
    """Close history and release order ownership together, never mutate line evidence."""
    require_permission(actor, "canPrepare")
    async with transaction(db) as session:
        batch = await db.pa_preparation_batches.find_one({"_id": batch_id}, session=session)
        if not batch or batch["status"] not in {"OPEN", "AUDITING"} or batch["version"] != expected_version:
            raise DomainConflict("BATCH_VERSION_CONFLICT")
        updated = await db.pa_preparation_batches.update_one({"_id": batch_id, "version": expected_version},
            {"$set": {"status": "CLOSED", "closedAt": now_utc(), "closedBy": actor.actorId}, "$inc": {"version": 1}}, session=session)
        if updated.matched_count != 1:
            raise DomainConflict("BATCH_VERSION_CONFLICT")
        await db.pa_preparation_contexts.delete_many({"batchId": batch_id}, session=session)
        await event(db, session, actor, "batch_closed", str(uuid4()), batchId=batch_id,
                    previousVersion=expected_version, newVersion=expected_version + 1)
