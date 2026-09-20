"""Atomic order transitions and append-only, staff-visible audit events."""
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import HTTPException
from core.sse import sse_manager

POST_FULFILMENT = {"awaiting_confirmation", "completed", "not_received", "fulfilled"}
DIRECT_ACTIVE = {"direct_quote_requested", "direct_price_review", "awaiting_fulfillment", "accepted"}


async def load_order(db, order_id):
    if not ObjectId.is_valid(order_id):
        raise HTTPException(404, "Order not found")
    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        raise HTTPException(404, "Order not found")
    return order


def check_version(order, expected):
    if expected is None:
        raise HTTPException(422, "expectedVersion is required; refresh the order")
    if expected != order.get("version", 0):
        raise HTTPException(409, "Order changed; refresh before retrying")


def require_direct(order, user=None):
    if order.get("assignmentType") != "direct":
        raise HTTPException(400, "Order is not directly assigned")
    if user and order.get("winnerId") != user["userId"]:
        raise HTTPException(403, "Direct assignment is unavailable to this aggregator")


def lifecycle_fields(order):
    return {key: order.get(key, default) for key, default in {
        "version": 0, "assignmentVersion": 0, "directQuote": None,
        "priceApprovedAt": None, "fulfilledAt": None, "acceptedAt": None,
        "cancelledAt": None, "recalledAt": None,
    }.items()}


async def transition(db, order, patch, event, actor, role, reason=None, extra_events=()):
    """State and audit commit together; legacy missing versions match Mongo null."""
    now = datetime.now(timezone.utc)
    patch = {**patch, "version": order.get("version", 0) + 1}
    def entry(kind):
        old_values = {k: order.get(k) for k in patch}
        if kind == "price_adjusted" and order["status"] == "direct_price_review":
            old_values["winnerTotalPrice"] = order["directQuote"]["totalPrice"]
        return {
            "eventType": kind, "timestamp": now,
            "actorId": actor.get("userId"), "actorName": actor.get("name"),
            "actorRole": role, "reason": reason,
            "oldValues": old_values, "newValues": patch.copy(),
            "aggregatorId": patch.get("winnerId") or order.get("winnerId"),
            "aggregatorName": patch.get("winnerName") or order.get("winnerName"),
            "assignmentVersion": patch.get("assignmentVersion", order.get("assignmentVersion", 0)),
        }
    result = await db.orders.update_one(
        {"_id": order["_id"], "status": order["status"], "version": order.get("version"),
         "winnerId": order.get("winnerId"), "assignmentVersion": order.get("assignmentVersion")},
        {"$set": patch, "$push": {"history": {"$each": [entry(e) for e in (*extra_events, event)]}}},
    )
    if not result.matched_count:
        raise HTTPException(409, "Order changed; refresh before retrying")
    await sse_manager.broadcast(str(order["_id"]), "order_changed", {"refresh": True})
    return {"success": True, "status": patch.get("status", order["status"]),
            "version": patch["version"],
            "assignmentVersion": patch.get("assignmentVersion", order.get("assignmentVersion", 0))}
