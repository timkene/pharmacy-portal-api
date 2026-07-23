import asyncio
import os
from datetime import datetime, timezone
from typing import AsyncGenerator

from bson import ObjectId
from fastapi import APIRouter, Cookie, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from core.database import get_db
from core.klaire_client import (
    notify_order_accepted,
    notify_order_created,
    notify_order_fulfilled,
    notify_order_picked_up,
)
from core.security import decode_session, generate_intake_id
from core.sse import sse_manager

_PHARMACY_SERVICE_KEY = os.getenv("PHARMACY_SERVICE_KEY", "")
from models.schemas import (
    BidOut,
    CreateOrderRequest,
    CreateOrderResponse,
    Enrollee,
    FulfillOrderRequest,
    KlaireCallbackRequest,
    Medication,
    OrderDetail,
    OrderListResponse,
    OrderSummary,
    PlaceBidRequest,
    Provider,
    UpdateOrderRequest,
)

router = APIRouter(tags=["orders"])

KEEPALIVE_INTERVAL = 15  # seconds
BIDDING_WINDOW_MINUTES = 60


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _require_staff(staff_session: str | None, x_service_key: str = "") -> dict:
    if _PHARMACY_SERVICE_KEY and x_service_key == _PHARMACY_SERVICE_KEY:
        return {"userId": "service", "name": "Clearline Analytics"}
    if not staff_session:
        raise HTTPException(status_code=401, detail="Staff authentication required")
    user = decode_session(staff_session)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid staff session")
    return user


def _require_aggregator(aggregator_session: str | None) -> dict:
    if not aggregator_session:
        raise HTTPException(status_code=401, detail="Aggregator authentication required")
    user = decode_session(aggregator_session)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid aggregator session")
    return user


def _require_any(
    staff_session: str | None,
    aggregator_session: str | None,
    x_service_key: str = "",
) -> tuple[dict, str]:
    """Return (user_dict, role) where role is 'staff' or 'aggregator'."""
    if _PHARMACY_SERVICE_KEY and x_service_key == _PHARMACY_SERVICE_KEY:
        return {"userId": "service", "name": "Clearline Analytics"}, "staff"
    if staff_session:
        user = decode_session(staff_session)
        if user:
            return user, "staff"
    if aggregator_session:
        user = decode_session(aggregator_session)
        if user:
            return user, "aggregator"
    raise HTTPException(status_code=401, detail="Authentication required")


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _bid_to_out(bid: dict, *, is_cheapest: bool = False) -> BidOut:
    return BidOut(
        id=str(bid["_id"]),
        orderId=bid["orderId"],
        aggregatorId=bid["aggregatorId"],
        aggregatorName=bid["aggregatorName"],
        unitPrice=bid["unitPrice"],
        totalPrice=bid["totalPrice"],
        isCheapest=is_cheapest,
        submittedAt=bid["submittedAt"],
    )


def _order_summary(order: dict, bid_count: int = 0) -> OrderSummary:
    meds_raw = order.get("medications", [])
    enrollee_raw = order.get("enrollee", {})
    full_name = enrollee_raw.get("fullName") or (
        f"{enrollee_raw.get('firstname', '')} {enrollee_raw.get('lastname', '')}".strip()
    ) or "—"
    enrollee = Enrollee(
        enrolleeId=enrollee_raw.get("enrolleeId", ""),
        fullName=full_name,
        phone=enrollee_raw.get("phone"),
        address=enrollee_raw.get("address"),
        title=enrollee_raw.get("title"),
        gender=enrollee_raw.get("gender"),
        dateOfBirth=enrollee_raw.get("dateOfBirth"),
        planType=enrollee_raw.get("planType"),
        groupName=enrollee_raw.get("groupName"),
        email=enrollee_raw.get("email"),
        effectiveDate=enrollee_raw.get("effectiveDate"),
        terminationDate=enrollee_raw.get("terminationDate"),
        isterminated=enrollee_raw.get("isterminated"),
    )
    medications = []
    for m in meds_raw:
        try:
            medications.append(Medication(**m))
        except Exception:
            pass
    return OrderSummary(
        id=str(order["_id"]),
        intakeId=order.get("intakeId", ""),
        enrollee=enrollee,
        medications=medications,
        diagnosis=meds_raw[0].get("diagnosis") if meds_raw else None,
        status=order.get("status", "pending_review"),
        biddingEndsAt=order.get("biddingEndsAt"),
        createdAt=order.get("createdAt", datetime.now(timezone.utc)),
        completedAt=order.get("completedAt"),
        bidCount=order.get("bidCount", bid_count),
        winnerName=order.get("winnerName"),
        winnerTotalPrice=order.get("winnerTotalPrice"),
        fulfillmentType=order.get("fulfillmentType"),
        deliveryFee=order.get("deliveryFee"),
    )


def _order_detail(
    order: dict,
    bids: list[BidOut],
    *,
    is_staff: bool = False,
    viewer_aggregator_id: str | None = None,
) -> OrderDetail:
    enrollee_raw = order.get("enrollee", {})
    winner_id = order.get("winnerId")
    is_winner = viewer_aggregator_id and viewer_aggregator_id == winner_id

    # Hide enrolleeId and phone from aggregators who are not the winner
    if is_staff or is_winner:
        enrollee = Enrollee(
            enrolleeId=enrollee_raw.get("enrolleeId", ""),
            fullName=enrollee_raw.get("fullName", ""),
            phone=enrollee_raw.get("phone"),
            address=enrollee_raw.get("address"),
            title=enrollee_raw.get("title"),
            gender=enrollee_raw.get("gender"),
            dateOfBirth=enrollee_raw.get("dateOfBirth"),
            planType=enrollee_raw.get("planType"),
            groupName=enrollee_raw.get("groupName"),
            email=enrollee_raw.get("email"),
            effectiveDate=enrollee_raw.get("effectiveDate"),
            terminationDate=enrollee_raw.get("terminationDate"),
            isterminated=enrollee_raw.get("isterminated"),
        )
    else:
        # Non-winner aggregators see name and address only — no ID or phone
        enrollee = Enrollee(
            enrolleeId="",
            fullName=enrollee_raw.get("fullName", ""),
            address=enrollee_raw.get("address"),
        )

    provider_raw = order.get("provider")
    provider = Provider(**provider_raw) if provider_raw else None
    return OrderDetail(
        id=str(order["_id"]),
        intakeId=order["intakeId"],
        enrollee=enrollee,
        provider=provider,
        medications=[Medication(**m) for m in order["medications"]],
        biddingEndsAt=order.get("biddingEndsAt"),
        status=order["status"],
        winnerId=winner_id if is_staff else None,
        winnerName=order.get("winnerName") if (is_staff or is_winner) else None,
        winnerTotalPrice=order.get("winnerTotalPrice") if (is_staff or is_winner) else None,
        fulfillmentType=order.get("fulfillmentType"),
        deliveryFee=order.get("deliveryFee") if is_staff else None,
        createdAt=order["createdAt"],
        createdBy=order["createdBy"],
        bids=bids,
    )


def _med_names(order: dict) -> list[str]:
    return [m.get("name", "") for m in order.get("medications", []) if m.get("name")]


# ---------------------------------------------------------------------------
# Bidding auto-close logic
# ---------------------------------------------------------------------------

async def check_and_close_bidding(order_id: str, db) -> dict:
    """
    If the order is still in 'bidding' status and biddingEndsAt has passed,
    close the bidding session, pick a winner, and broadcast SSE events.
    Returns the (potentially updated) order document.
    """
    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] != "bidding":
        return order

    now = datetime.now(timezone.utc)
    bidding_ends = order["biddingEndsAt"]
    if bidding_ends.tzinfo is None:
        bidding_ends = bidding_ends.replace(tzinfo=timezone.utc)

    if now < bidding_ends:
        return order

    # Bidding has expired — find the lowest total price bid
    bids_cursor = db.bids.find({"orderId": order_id}).sort("totalPrice", 1).limit(1)
    winning_bids = await bids_cursor.to_list(1)

    if not winning_bids:
        update = {"$set": {"status": "awaiting_fulfillment"}}
        await db.orders.update_one({"_id": ObjectId(order_id)}, update)
        order = await db.orders.find_one({"_id": ObjectId(order_id)})
        await sse_manager.broadcast(
            order_id,
            "session_closed",
            {"winnerId": None, "winnerName": None, "totalPrice": None},
        )
        return order

    winner = winning_bids[0]
    update = {
        "$set": {
            "status": "awaiting_fulfillment",
            "winnerId": winner["aggregatorId"],
            "winnerName": winner["aggregatorName"],
            "winnerTotalPrice": winner["totalPrice"],
        }
    }
    await db.orders.update_one({"_id": ObjectId(order_id)}, update)
    order = await db.orders.find_one({"_id": ObjectId(order_id)})

    await sse_manager.broadcast(
        order_id,
        "session_closed",
        {
            "winnerId": winner["aggregatorId"],
            "winnerName": winner["aggregatorName"],
            "totalPrice": winner["totalPrice"],
        },
    )
    return order


# ---------------------------------------------------------------------------
# GET /api/orders  (staff only)
# ---------------------------------------------------------------------------

@router.get("/orders", response_model=OrderListResponse)
async def list_orders(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    staff_session: str | None = Cookie(default=None),
    x_service_key: str = Header(default=""),
):
    _require_staff(staff_session, x_service_key)
    db = get_db()

    skip = (page - 1) * limit
    total = await db.orders.count_documents({})

    # Single aggregation — no N+1 bid count queries
    pipeline = [
        {"$sort": {"createdAt": -1}},
        {"$skip": skip},
        {"$limit": limit},
        {"$addFields": {"_id_str": {"$toString": "$_id"}}},
        {
            "$lookup": {
                "from": "bids",
                "localField": "_id_str",
                "foreignField": "orderId",
                "as": "_bid_agg",
                "pipeline": [{"$count": "n"}],
            }
        },
        {"$addFields": {"bidCount": {"$ifNull": [{"$first": "$_bid_agg.n"}, 0]}}},
        {"$unset": ["_bid_agg", "_id_str"]},
    ]
    raw_orders = await db.orders.aggregate(pipeline).to_list(limit)
    summaries = [_order_summary(order) for order in raw_orders]

    return OrderListResponse(orders=summaries, total=total, page=page)


# ---------------------------------------------------------------------------
# POST /api/orders  (staff only)
# ---------------------------------------------------------------------------

@router.post("/orders", response_model=CreateOrderResponse, status_code=201)
async def create_order(
    body: CreateOrderRequest,
    staff_session: str | None = Cookie(default=None),
    x_service_key: str = Header(default=""),
):
    staff_user = _require_staff(staff_session, x_service_key)
    db = get_db()

    now = datetime.now(timezone.utc)
    doc = {
        "intakeId": generate_intake_id(),
        "enrollee": body.enrollee.model_dump(),
        "provider": body.provider.model_dump(),
        "medications": [m.model_dump() for m in body.medications],
        "status": "pending_review",
        "winnerId": None,
        "winnerName": None,
        "winnerTotalPrice": None,
        "createdAt": now,
        "createdBy": staff_user["userId"],
    }

    result = await db.orders.insert_one(doc)
    order_id = str(result.inserted_id)

    return CreateOrderResponse(success=True, orderId=order_id)


# ---------------------------------------------------------------------------
# DELETE /api/orders/{id}  (staff only)
# ---------------------------------------------------------------------------

@router.delete("/orders/{order_id}", status_code=200)
async def delete_order(
    order_id: str,
    staff_session: str | None = Cookie(default=None),
    x_service_key: str = Header(default=""),
):
    _require_staff(staff_session, x_service_key)
    db = get_db()
    result = await db.orders.delete_one({"_id": ObjectId(order_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Order not found")
    await db.bids.delete_many({"orderId": order_id})
    return {"success": True}


# ---------------------------------------------------------------------------
# POST /api/orders/{id}/approve  (staff only)
# ---------------------------------------------------------------------------

@router.post("/orders/{order_id}/approve")
async def approve_order(
    order_id: str,
    staff_session: str | None = Cookie(default=None),
    x_service_key: str = Header(default=""),
):
    _require_staff(staff_session, x_service_key)
    db = get_db()

    from datetime import timedelta

    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.get("status") != "pending_review":
        raise HTTPException(status_code=400, detail="Order is not pending review")

    now = datetime.now(timezone.utc)
    await db.orders.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {
            "status": "bidding",
            "biddingEndsAt": now + timedelta(minutes=BIDDING_WINDOW_MINUTES),
        }},
    )

    # Notify enrollee via Klaire that their order has been received
    enrollee = order.get("enrollee", {})
    phone = enrollee.get("phone")
    if phone:
        asyncio.create_task(notify_order_created(
            phone=phone,
            enrollee_id=enrollee.get("enrolleeId", ""),
            enrollee_name=enrollee.get("fullName", ""),
            medications=_med_names(order),
            order_id=order_id,
        ))

    return {"success": True}


# ---------------------------------------------------------------------------
# POST /api/orders/{id}/reject  (staff only)
# ---------------------------------------------------------------------------

@router.post("/orders/{order_id}/reject")
async def reject_order(
    order_id: str,
    staff_session: str | None = Cookie(default=None),
    x_service_key: str = Header(default=""),
):
    _require_staff(staff_session, x_service_key)
    db = get_db()

    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.get("status") != "pending_review":
        raise HTTPException(status_code=400, detail="Order is not pending review")

    await db.orders.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"status": "rejected"}},
    )
    return {"success": True}


# ---------------------------------------------------------------------------
# PUT /api/orders/{id}  (staff only — edit pending_review or rejected orders)
# ---------------------------------------------------------------------------

@router.put("/orders/{order_id}")
async def update_order(
    order_id: str,
    body: UpdateOrderRequest,
    staff_session: str | None = Cookie(default=None),
    x_service_key: str = Header(default=""),
):
    _require_staff(staff_session, x_service_key)
    db = get_db()

    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.get("status") not in ("pending_review", "rejected"):
        raise HTTPException(status_code=400, detail="Only pending or rejected orders can be edited")

    patch: dict = {}
    if body.enrollee is not None:
        patch["enrollee"] = body.enrollee.model_dump()
    if body.provider is not None:
        patch["provider"] = body.provider.model_dump()
    if body.medications is not None:
        patch["medications"] = [m.model_dump() for m in body.medications]

    # Editing a rejected order moves it back to pending_review for re-review
    if order.get("status") == "rejected":
        patch["status"] = "pending_review"

    if patch:
        await db.orders.update_one({"_id": ObjectId(order_id)}, {"$set": patch})

    return {"success": True}


# ---------------------------------------------------------------------------
# POST /api/orders/{id}/close-bidding  (staff only — force-close early)
# ---------------------------------------------------------------------------

@router.post("/orders/{order_id}/close-bidding")
async def close_bidding_early(
    order_id: str,
    staff_session: str | None = Cookie(default=None),
    x_service_key: str = Header(default=""),
):
    _require_staff(staff_session, x_service_key)
    db = get_db()

    from datetime import timedelta

    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.get("status") != "bidding":
        raise HTTPException(status_code=400, detail="Order is not in bidding status")

    # Move biddingEndsAt into the past so check_and_close_bidding triggers immediately
    now = datetime.now(timezone.utc)
    await db.orders.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"biddingEndsAt": now - timedelta(seconds=1)}},
    )
    await check_and_close_bidding(order_id, db)
    return {"success": True}


# ---------------------------------------------------------------------------
# GET /api/orders/{id}  (staff or aggregator)
# ---------------------------------------------------------------------------

@router.get("/orders/{order_id}")
async def get_order(
    order_id: str,
    staff_session: str | None = Cookie(default=None),
    aggregator_session: str | None = Cookie(default=None),
    x_service_key: str = Header(default=""),
):
    user, role = _require_any(staff_session, aggregator_session, x_service_key)
    db = get_db()

    order = await check_and_close_bidding(order_id, db)

    bids_cursor = db.bids.find({"orderId": order_id}).sort("totalPrice", 1)
    raw_bids = await bids_cursor.to_list(None)
    cheapest_id = str(raw_bids[0]["_id"]) if raw_bids else None
    bids_out = [_bid_to_out(b, is_cheapest=(str(b["_id"]) == cheapest_id)) for b in raw_bids]

    # Aggregators only see their own bid — never competitor bids or isCheapest flag
    if role == "aggregator":
        agg_id = user["userId"]
        bids_out = [
            BidOut(**{**b.model_dump(), "isCheapest": False})
            for b in bids_out if b.aggregatorId == agg_id
        ]

    detail = _order_detail(
        order,
        bids_out,
        is_staff=(role == "staff"),
        viewer_aggregator_id=user["userId"] if role == "aggregator" else None,
    )
    return detail


# ---------------------------------------------------------------------------
# GET /api/orders/{id}/stream  (SSE — staff or aggregator)
# ---------------------------------------------------------------------------

@router.get("/orders/{order_id}/stream")
async def order_stream(
    order_id: str,
    staff_session: str | None = Cookie(default=None),
    aggregator_session: str | None = Cookie(default=None),
    x_service_key: str = Header(default=""),
):
    _require_any(staff_session, aggregator_session, x_service_key)
    db = get_db()

    async def event_generator() -> AsyncGenerator[str, None]:
        order = await check_and_close_bidding(order_id, db)
        bids_cursor = db.bids.find({"orderId": order_id}).sort("totalPrice", 1)
        raw_bids = await bids_cursor.to_list(None)
        import json
        initial_bids = [
            {
                "id": str(b["_id"]),
                "orderId": b["orderId"],
                "aggregatorId": b["aggregatorId"],
                "aggregatorName": b["aggregatorName"],
                "unitPrice": b["unitPrice"],
                "totalPrice": b["totalPrice"],
                "submittedAt": b["submittedAt"].isoformat(),
            }
            for b in raw_bids
        ]
        yield f"event: bid_update\ndata: {json.dumps({'bids': initial_bids})}\n\n"

        queue = sse_manager.subscribe(order_id)
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_INTERVAL)
                    yield msg
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            sse_manager.unsubscribe(order_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# POST /api/orders/{id}/bids  (aggregator only)
# ---------------------------------------------------------------------------

@router.post("/orders/{order_id}/bids")
async def place_bid(
    order_id: str,
    body: PlaceBidRequest,
    aggregator_session: str | None = Cookie(default=None),
):
    agg_user = _require_aggregator(aggregator_session)
    db = get_db()

    order = await check_and_close_bidding(order_id, db)

    if order["status"] != "bidding":
        raise HTTPException(status_code=400, detail="Bidding session is closed")

    now = datetime.now(timezone.utc)
    bidding_ends = order["biddingEndsAt"]
    if bidding_ends.tzinfo is None:
        bidding_ends = bidding_ends.replace(tzinfo=timezone.utc)
    if now >= bidding_ends:
        raise HTTPException(status_code=400, detail="Bidding session has expired")

    bid_doc = {
        "orderId": order_id,
        "aggregatorId": agg_user["userId"],
        "aggregatorName": agg_user["name"],
        "unitPrice": body.unitPrice,
        "totalPrice": body.totalPrice,
        "submittedAt": now,
    }
    await db.bids.update_one(
        {"orderId": order_id, "aggregatorId": agg_user["userId"]},
        {"$set": bid_doc},
        upsert=True,
    )

    bids_cursor = db.bids.find({"orderId": order_id}).sort("totalPrice", 1)
    raw_bids = await bids_cursor.to_list(None)
    cheapest_id = str(raw_bids[0]["_id"]) if raw_bids else None
    bids_payload = [
        {
            "id": str(b["_id"]),
            "orderId": b["orderId"],
            "aggregatorId": b["aggregatorId"],
            "aggregatorName": b["aggregatorName"],
            "unitPrice": b["unitPrice"],
            "totalPrice": b["totalPrice"],
            "isCheapest": str(b["_id"]) == cheapest_id,
            "submittedAt": b["submittedAt"].isoformat(),
        }
        for b in raw_bids
    ]
    await sse_manager.broadcast(order_id, "bid_update", {"bids": bids_payload})

    return {"success": True}


# ---------------------------------------------------------------------------
# POST /api/orders/{id}/accept  (winner aggregator only)
# ---------------------------------------------------------------------------

@router.post("/orders/{order_id}/accept")
async def accept_order(
    order_id: str,
    aggregator_session: str | None = Cookie(default=None),
):
    agg_user = _require_aggregator(aggregator_session)
    db = get_db()

    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.get("status") != "awaiting_fulfillment":
        raise HTTPException(status_code=400, detail="Order is not awaiting fulfillment")

    if order.get("winnerId") != agg_user["userId"]:
        raise HTTPException(status_code=403, detail="Only the winning aggregator can accept this order")

    await db.orders.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"status": "accepted"}},
    )
    await sse_manager.broadcast(order_id, "order_accepted", {"aggregatorName": agg_user["name"]})

    # Notify enrollee via Klaire
    enrollee = order.get("enrollee", {})
    phone = enrollee.get("phone")
    if phone:
        asyncio.create_task(notify_order_accepted(
            phone=phone,
            enrollee_id=enrollee.get("enrolleeId", ""),
            enrollee_name=enrollee.get("fullName", ""),
            pharmacy_name=agg_user["name"],
            order_id=order_id,
        ))

    return {"success": True}


# ---------------------------------------------------------------------------
# POST /api/orders/{id}/fulfill  (winner aggregator only)
# ---------------------------------------------------------------------------

@router.post("/orders/{order_id}/fulfill")
async def fulfill_order(
    order_id: str,
    body: FulfillOrderRequest | None = None,
    aggregator_session: str | None = Cookie(default=None),
):
    agg_user = _require_aggregator(aggregator_session)
    db = get_db()

    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.get("status") != "accepted":
        raise HTTPException(status_code=400, detail="Order must be in accepted status to mark as fulfilled")

    if order.get("winnerId") != agg_user["userId"]:
        raise HTTPException(status_code=403, detail="Only the winning aggregator can fulfill this order")

    fulfillment_type = (body.fulfillmentType if body else None) or "picked_up"
    delivery_fee = body.deliveryFee if body else None

    now = datetime.now(timezone.utc)
    enrollee = order.get("enrollee", {})
    phone = enrollee.get("phone")

    if fulfillment_type == "delivered":
        current_total = order.get("winnerTotalPrice") or 0
        new_total = current_total + (delivery_fee or 0)
        await db.orders.update_one(
            {"_id": ObjectId(order_id)},
            {"$set": {
                "status": "awaiting_confirmation",
                "fulfillmentType": "delivered",
                "deliveryFee": delivery_fee,
                "winnerTotalPrice": new_total,
            }},
        )
        await sse_manager.broadcast(order_id, "order_fulfilled", {})
        if phone:
            asyncio.create_task(notify_order_fulfilled(
                phone=phone,
                enrollee_id=enrollee.get("enrolleeId", ""),
                enrollee_name=enrollee.get("fullName", ""),
                pharmacy_name=agg_user["name"],
                medications=_med_names(order),
                order_id=order_id,
            ))
    else:
        # picked_up — closes immediately; Klaire notifies enrollee as a receipt
        await db.orders.update_one(
            {"_id": ObjectId(order_id)},
            {"$set": {
                "status": "completed",
                "fulfillmentType": "picked_up",
                "completedAt": now,
            }},
        )
        await sse_manager.broadcast(order_id, "order_completed", {"received": True})
        if phone:
            asyncio.create_task(notify_order_picked_up(
                phone=phone,
                enrollee_id=enrollee.get("enrolleeId", ""),
                enrollee_name=enrollee.get("fullName", ""),
                pharmacy_name=agg_user["name"],
                medications=_med_names(order),
                order_id=order_id,
            ))

    return {"success": True}


# ---------------------------------------------------------------------------
# POST /api/orders/{id}/klaire-callback  (called by Klaire WhatsApp service)
# ---------------------------------------------------------------------------

@router.post("/orders/{order_id}/klaire-callback")
async def klaire_callback(
    order_id: str,
    body: KlaireCallbackRequest,
    request: Request,
):
    db = get_db()

    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.get("status") != "awaiting_confirmation":
        # Idempotent — if already resolved, just return ok
        return {"success": True, "note": "order already resolved"}

    now = datetime.now(timezone.utc)
    if body.received:
        new_status = "completed"
        event = "order_completed"
    else:
        new_status = "not_received"
        event = "order_not_received"

    await db.orders.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"status": new_status, "completedAt": now}},
    )
    await sse_manager.broadcast(order_id, event, {"received": body.received})

    return {"success": True}
