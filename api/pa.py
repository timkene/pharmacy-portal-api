"""Staff-only, locally deduplicated Pharmacy PA generation."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from uuid import uuid4
import os
import re
import logging

import httpx
from bson import ObjectId
from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel, Field
from typing import Literal

from core.database import get_db
from core.order_lifecycle import load_order
from core.pa_client import build_payload, get_member_info, issue_pa
from core.pricing import money, price_lines, subtotal
from core.security import decode_session

router = APIRouter(tags=["pharmacy-pa"])
logger = logging.getLogger(__name__)


def _utc(value):
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class VerificationResolution(BaseModel):
    resolution: Literal["existing_pa", "confirmed_no_pa"]
    evidence: str = Field(min_length=10, max_length=2000)
    paNumber: str | None = None
    confirmNoPaCreated: bool = False
    verificationMethod: str | None = None
    checkedWith: str | None = None
    verifiedAt: datetime | None = None


class GenerationRequest(BaseModel):
    expectedVersion: int = Field(ge=0, strict=True)


def _staff(session):
    user = decode_session(session, "staff") if session else None
    if not user:
        raise HTTPException(401, "Staff authentication required")
    return user


def _summary(lines):
    statuses = {line["status"] for line in lines}
    if not statuses:
        return "not_generated"
    if "verification_required" in statuses:
        return "verification_required"
    if statuses == {"generated"}:
        return "generated"
    if "submitting" in statuses:
        return "in_progress"
    if "generated" in statuses or "failed_retryable" in statuses:
        return "partial_failure"
    return "not_generated"


def public_pa_state(order):
    """Only return operational PA state needed by staff, never claim ownership data."""
    state = order.get("paGeneration") or {}
    if not state.get("available"):
        return {"available": False, "status": state.get("status", "not_configured")}
    now = datetime.now(timezone.utc)
    lease = state.get("leaseUntil")
    interrupted = bool(state.get("active") and _utc(lease) and _utc(lease) <= now)
    return {"available": True, "status": "interrupted" if interrupted else state.get("status"),
            "active": bool(state.get("active") and not interrupted), "interrupted": interrupted,
            "lines": [{key: line.get(key) for key in ("lineId", "procedureCode", "description", "quantity", "amount", "status", "paNumber", "failureKind")}
                      for line in state.get("lines") or []]}


def _lines(order, provider_id):
    medications = order.get("medications") or []
    if len(medications) > 10:
        raise HTTPException(422, "PA generation supports at most 10 medication lines")
    prices = order.get("finalProcedurePrices")
    if not prices:
        raise HTTPException(422, "This order lacks explicit procedure prices; no PA amount can be inferred")
    valid = price_lines(medications, prices)
    if subtotal(valid) != order.get("medicationSubtotal"):
        raise HTTPException(422, "Medication subtotal does not match procedure prices")
    result = []
    for med, price in zip(medications, valid):
        if med["procedureCode"] == "PRE11":
            raise HTTPException(422, "PRE11 is reserved for delivery")
        if not med.get("diagnosisCode") or not isinstance(med.get("quantity"), int) or med["quantity"] <= 0:
            raise HTTPException(422, "Medication needs diagnosis code and positive quantity")
        result.append({"lineId": med["lineId"], "procedureCode": med["procedureCode"],
                       "description": med.get("name", ""), "diagnosisCode": med["diagnosisCode"],
                       "quantity": med["quantity"], "amount": price["amount"],
                       "providerId": provider_id, "status": "pending", "paNumber": None})
    method = order.get("fulfillmentType")
    if method == "delivered":
        fee = float(money(order.get("deliveryFee"), "Delivery fee"))
        delivery_diagnosis = os.getenv("PA_DELIVERY_DIAGNOSIS_CODE", "").strip()
        if not delivery_diagnosis:
            raise HTTPException(422, "Delivery PA diagnosis code is not configured")
        result.append({"lineId": "delivery-PRE11", "procedureCode": "PRE11",
                       "description": "Pharmacy Delivery", "diagnosisCode": delivery_diagnosis,
                       "quantity": 1, "amount": fee, "providerId": provider_id,
                       "status": "pending", "paNumber": None})
    elif method != "picked_up" or order.get("deliveryFee") is not None:
        raise HTTPException(422, "Fulfilment method or delivery fee is invalid")
    expected_total = subtotal(valid) + (result[-1]["amount"] if method == "delivered" else 0)
    if round(expected_total, 2) != order.get("overallTotal"):
        raise HTTPException(422, "Overall total does not match procedure prices and delivery fee")
    return result


def _event(kind, actor, line, now):
    return {"eventType": kind, "timestamp": now, "actorId": actor["userId"],
            "actorName": actor.get("name"), "actorRole": "staff", "lineId": line["lineId"],
            "procedureCode": line["procedureCode"], "description": line["description"],
            "amount": line["amount"], "providerId": line["providerId"],
            "paNumber": line.get("paNumber"), "paStatus": line["status"]}


async def _save(db, order_id, token, state, actor, line, event_type):
    now = datetime.now(timezone.utc)
    state["status"] = _summary(state["lines"])
    state["updatedAt"] = now
    state["leaseUntil"] = now + timedelta(minutes=10)
    result = await db.orders.update_one(
        {"_id": ObjectId(order_id), "paGeneration.token": token},
        {"$set": {"paGeneration": state}, "$inc": {"version": 1},
         "$push": {"history": _event(event_type, actor, line, now)}})
    if not result.matched_count:
        raise HTTPException(409, "PA generation ownership changed; verify upstream before retrying")


@router.post("/orders/{order_id}/generate-pa")
async def generate_pharmacy_pa(order_id: str, body: GenerationRequest, staff_session: str | None = Cookie(default=None)):
    actor = _staff(staff_session)
    db = get_db()
    order = await load_order(db, order_id)
    if order.get("version") != body.expectedVersion:
        raise HTTPException(409, "Order changed; refresh and confirm PA amounts again")
    if order.get("status") != "completed":
        raise HTTPException(409, "PA is available only after order completion")
    completed_at = order.get("completedAt")
    if not isinstance(completed_at, datetime):
        raise HTTPException(422, "Order completion date is missing")
    if not order.get("winnerId") or not ObjectId.is_valid(order["winnerId"]):
        raise HTTPException(422, "Winning aggregator is missing")
    aggregator = await db.aggregator_users.find_one({"_id": ObjectId(order["winnerId"])})
    provider_id = str((aggregator or {}).get("providerId") or "").strip()
    if not provider_id:
        raise HTTPException(422, "Winning aggregator has no providerId configured")
    enrollee_id = (order.get("enrollee") or {}).get("enrolleeId")
    if not enrollee_id:
        raise HTTPException(422, "Order has no enrollee ID")
    expected = _lines(order, provider_id)
    existing = order.get("paGeneration") or {}
    old = existing.get("lines") or []
    if old:
        if [(x["lineId"], x["procedureCode"], x["amount"], x["providerId"]) for x in old] != [
            (x["lineId"], x["procedureCode"], x["amount"], x["providerId"]) for x in expected]:
            raise HTTPException(409, "PA inputs changed after generation began; manual review required")
        lines = old
    else:
        lines = expected
    if any(x["status"] == "verification_required" for x in lines):
        raise HTTPException(409, "An uncertain PA result requires verification before another submission")
    if all(x["status"] == "generated" for x in lines):
        raise HTTPException(409, "All PAs have already been generated")
    now = datetime.now(timezone.utc)
    if existing.get("active") and _utc(existing.get("leaseUntil")) and _utc(existing["leaseUntil"]) > now:
        raise HTTPException(409, "PA generation is already in progress")
    # An abandoned submitting line may have reached IssuePa. Never claim it again.
    if any(x["status"] == "submitting" for x in lines):
        raise HTTPException(409, "An interrupted PA requires verification before another submission")
    try:
        member = await get_member_info(enrollee_id)
    except Exception:
        raise HTTPException(502, "Member data for PA could not be verified; no PA was submitted") from None
    token = str(uuid4())
    state = {"available": True, "status": _summary(lines), "active": True, "token": token,
             "leaseUntil": now + timedelta(minutes=10), "updatedAt": now, "lines": lines}
    claimed = await db.orders.update_one(
        {"_id": order["_id"], "status": "completed", "version": body.expectedVersion,
         "$or": [{"paGeneration.active": {"$ne": True}}, {"paGeneration.leaseUntil": {"$lte": now}}]},
        {"$set": {"paGeneration": state}, "$inc": {"version": 1}})
    if not claimed.matched_count:
        raise HTTPException(409, "PA generation is already in progress; refresh the order")
    generation_date = datetime.now(ZoneInfo("Africa/Lagos")).date()
    for line in lines:
        if line["status"] not in ("pending", "failed_retryable"):
            continue
        line["status"] = "submitting"
        line["submittedAt"] = datetime.now(timezone.utc)
        await _save(db, order_id, token, state, actor, line, "pa_submitting")
        payload = build_payload(enrollee_id, member, line["procedureCode"], line["diagnosisCode"],
                                line["quantity"], line["amount"], provider_id, generation_date)
        try:
            line["paNumber"] = await issue_pa(payload)
            logger.info("PA upstream returned reference orderId=%s medicationLineId=%s procedureCode=%s paReference=%s requestedAt=%s", order_id, line["lineId"], line["procedureCode"], line["paNumber"], line["submittedAt"].isoformat())
            line["status"] = "generated"
            line["generatedAt"] = datetime.now(timezone.utc)
        except httpx.ConnectError:
            # Connection establishment failed before sending the request.
            line["status"] = "failed_retryable"
            line["failureKind"] = "connect_error"
        except Exception as exc:
            # Timeouts, HTTP errors and unparseable responses can follow a successful create.
            line["status"] = "verification_required"
            line["failureKind"] = ("http_error" if isinstance(exc, httpx.HTTPStatusError) else
                                   "timeout" if isinstance(exc, httpx.TimeoutException) else
                                   "invalid_response" if isinstance(exc, ValueError) else "uncertain_upstream_result")
            logger.warning("PA uncertain response orderId=%s medicationLineId=%s procedureCode=%s requestedAt=%s failureClass=%s httpStatus=%s responseClass=%s",
                           order_id, line["lineId"], line["procedureCode"], line["submittedAt"].isoformat(),
                           type(exc).__name__, exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None,
                           line["failureKind"])
        if line["status"] != "generated":
            logger.warning("PA request outcome orderId=%s medicationLineId=%s procedureCode=%s requestedAt=%s failureClass=%s", order_id, line["lineId"], line["procedureCode"], line["submittedAt"].isoformat(), line.get("failureKind"))
        line["updatedAt"] = datetime.now(timezone.utc)
        await _save(db, order_id, token, state, actor, line, f"pa_{line['status']}")
        if line["status"] == "verification_required":
            break
    state["active"] = False
    state["status"] = _summary(lines)
    await db.orders.update_one({"_id": order["_id"], "paGeneration.token": token},
                               {"$set": {"paGeneration": state}, "$inc": {"version": 1}})
    return {"status": state["status"], "lines": public_pa_state({"paGeneration": state})["lines"]}


@router.post("/orders/{order_id}/pa-interruption/mark-verification-required")
async def mark_interrupted_pa(order_id: str, staff_session: str | None = Cookie(default=None)):
    actor = _staff(staff_session)
    permitted = {item.strip() for item in os.getenv("PA_RECOVERY_STAFF_IDS", "").split(",") if item.strip()}
    if actor["userId"] not in permitted:
        raise HTTPException(403, "PA recovery requires an authorized staff account")
    db = get_db()
    order = await load_order(db, order_id)
    state = order.get("paGeneration") or {}
    now = datetime.now(timezone.utc)
    if not state.get("active") or not _utc(state.get("leaseUntil")) or _utc(state["leaseUntil"]) > now:
        raise HTTPException(409, "No expired PA submission is available")
    lines = state.get("lines") or []
    interrupted = [line for line in lines if line.get("status") == "submitting"]
    if not interrupted:
        raise HTTPException(409, "No interrupted PA line requires verification")
    for line in interrupted:
        line["status"] = "verification_required"
        line["failureKind"] = "interrupted_submission"
        line["updatedAt"] = now
    old_token = state.get("token")
    state.update(active=False, token=str(uuid4()), leaseUntil=None,
                 status="verification_required", updatedAt=now)
    events = [{**_event("pa_interrupted_requires_verification", actor, line, now),
               "previousStatus": "submitting"} for line in interrupted]
    result = await db.orders.update_one(
        {"_id": order["_id"], "version": order.get("version"), "paGeneration.token": old_token,
         "paGeneration.active": True, "paGeneration.leaseUntil": {"$lte": now}},
        {"$set": {"paGeneration": state}, "$inc": {"version": 1}, "$push": {"history": {"$each": events}}})
    if not result.matched_count:
        raise HTTPException(409, "PA state changed; refresh before recovery")
    return {"status": state["status"], "lines": public_pa_state({"paGeneration": state})["lines"]}


@router.post("/orders/{order_id}/pa-lines/{line_id}/verify")
async def verify_uncertain_pa(order_id: str, line_id: str, body: VerificationResolution,
                              staff_session: str | None = Cookie(default=None)):
    actor = _staff(staff_session)
    permitted = {item.strip() for item in os.getenv("PA_RECOVERY_STAFF_IDS", "").split(",") if item.strip()}
    if actor["userId"] not in permitted:
        raise HTTPException(403, "PA recovery requires an authorized staff account")
    evidence = body.evidence.strip()
    if len(evidence) < 10:
        raise HTTPException(422, "Verification evidence is required")
    if body.resolution == "existing_pa" and not (body.paNumber or "").strip():
        raise HTTPException(422, "Verified PA number is required")
    if body.resolution == "confirmed_no_pa" and body.paNumber is not None:
        raise HTTPException(422, "Do not attach a PA number to a confirmed rejection")
    if body.resolution == "confirmed_no_pa" and not (body.confirmNoPaCreated is True and (body.verificationMethod or "").strip()
                                                      and (body.checkedWith or "").strip() and body.verifiedAt and body.verifiedAt.tzinfo):
        raise HTTPException(422, "Explicit no-PA confirmation, method, checked source and timestamp are required")
    if body.verifiedAt and body.verifiedAt.tzinfo and body.verifiedAt.astimezone(timezone.utc) > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise HTTPException(422, "Verification timestamp cannot be in the future")
    if body.resolution == "existing_pa" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/-]{2,63}", (body.paNumber or "").strip()):
        raise HTTPException(422, "Invalid PA reference format")
    db = get_db()
    order = await load_order(db, order_id)
    state = order.get("paGeneration") or {}
    if state.get("active"):
        raise HTTPException(409, "PA generation is in progress")
    lines = state.get("lines") or []
    matching = [line for line in lines if line["lineId"] == line_id]
    if len(matching) != 1 or matching[0]["status"] != "verification_required":
        raise HTTPException(409, "This PA line is not awaiting verification")
    line = matching[0]
    if body.resolution == "existing_pa" and any(other is not line and other.get("paNumber") == body.paNumber.strip() for other in lines):
        raise HTTPException(422, "PA reference is already used by another line")
    now = datetime.now(timezone.utc)
    previous_status = line["status"]
    if body.resolution == "existing_pa":
        line["status"] = "generated"
        line["paNumber"] = body.paNumber.strip()
        line["generatedAt"] = now
    else:
        line["status"] = "failed_retryable"
    line["updatedAt"] = now
    state["status"] = _summary(lines)
    state["updatedAt"] = now
    event = {**_event("pa_manually_verified", actor, line, now),
             "resolution": body.resolution, "evidence": evidence, "previousStatus": previous_status,
             "verificationMethod": body.verificationMethod, "checkedWith": body.checkedWith,
             "verifiedAt": body.verifiedAt, "confirmNoPaCreated": body.confirmNoPaCreated}
    result = await db.orders.update_one(
        {"_id": order["_id"], "version": order.get("version"), "paGeneration.active": False},
        {"$set": {"paGeneration": state}, "$inc": {"version": 1}, "$push": {"history": event}})
    if not result.matched_count:
        raise HTTPException(409, "Order changed; verify again before recovery")
    return {"status": state["status"], "line": line}
