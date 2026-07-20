import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator

from bson import ObjectId
from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from core.database import get_db
from core.klaire_client import (
    notify_order_accepted,
    notify_order_created,
    notify_order_fulfilled,
)
from core.security import decode_session, generate_intake_id
from core.sse import sse_manager
from models.schemas import (
    BidOut,
    CreateOrderRequest,
    CreateOrderResponse,
    Enrollee,
    KlaireCallbackRequest,
    Medication,
    OrderDetail,
    OrderListResponse,
    OrderSummary,
    PlaceBidRequest,
    Provider,
)

router = APIRouter(tags=["orders"])

KEEPALIVE_INTERVAL = 15  # seconds
BIDDING_WINDOW_MINUTES = 15


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _require_staff(staff_session: str | None) -> dict:
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


def _require_any(staff_session: str | None, aggregator_session: str | None) -> tuple[dict, str]:
    """Return (user_dict, role) where role is 'staff' or 'aggregator'."""
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

def _bid_to_out(bid: dict) -> BidOut:
    return BidOut(
        id=str(bid["_id"]),
        orderId=bid["orderId"],
        aggregatorId=bid["aggregatorId"],
        aggregatorName=bid["aggregatorName"],
        unitPrice=bid["unitPrice"],
        totalPrice=bid["totalPrice"],
        submittedAt=bid["submittedAt"],
    )


def _order_summary(order: dict, bid_count: int) -> OrderSummary:
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
        status=order.get("status", "bidding"),
        biddingEndsAt=order.get("biddingEndsAt", datetime.now(timezone.utc)),
        createdAt=order.get("createdAt", datetime.now(timezone.utc)),
        bidCount=bid_count,
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
        biddingEndsAt=order["biddingEndsAt"],
        status=order["status"],
        winnerId=winner_id,
        winnerName=order.get("winnerName"),
        winnerTotalPrice=order.get("winnerTotalPrice"),
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
):
    _require_staff(staff_session)
    db = get_db()

    skip = (page - 1) * limit
    total = await db.orders.count_documents({})
    cursor = db.orders.find({}).sort("createdAt", -1).skip(skip).limit(limit)
    raw_orders = await cursor.to_list(limit)

    summaries = []
    for order in raw_orders:
        order_id_str = str(order["_id"])
        bid_count = await db.bids.count_documents({"orderId": order_id_str})
        summaries.append(_order_summary(order, bid_count))

    return OrderListResponse(orders=summaries, total=total, page=page)


# ---------------------------------------------------------------------------
# POST /api/orders  (staff only)
# ---------------------------------------------------------------------------

@router.post("/orders", response_model=CreateOrderResponse, status_code=201)
async def create_order(
    body: CreateOrderRequest,
    staff_session: str | None = Cookie(default=None),
):
    staff_user = _require_staff(staff_session)
    db = get_db()

    from datetime import timedelta

    now = datetime.now(timezone.utc)
    doc = {
        "intakeId": generate_intake_id(),
        "enrollee": body.enrollee.model_dump(),
        "provider": body.provider.model_dump(),
        "medications": [m.model_dump() for m in body.medications],
        "biddingEndsAt": now + timedelta(minutes=BIDDING_WINDOW_MINUTES),
        "status": "bidding",
        "winnerId": None,
        "winnerName": None,
        "winnerTotalPrice": None,
        "createdAt": now,
        "createdBy": staff_user["userId"],
    }

    result = await db.orders.insert_one(doc)
    order_id = str(result.inserted_id)

    # Notify enrollee via Klaire if they have a phone number
    phone = body.enrollee.phone
    if phone:
        med_names = [m.name for m in body.medications if m.name]
        asyncio.create_task(notify_order_created(
            phone=phone,
            enrollee_id=body.enrollee.enrolleeId,
            enrollee_name=body.enrollee.fullName,
            medications=med_names,
            order_id=order_id,
        ))

    return CreateOrderResponse(success=True, orderId=order_id)


# ---------------------------------------------------------------------------
# GET /api/orders/{id}  (staff or aggregator)
# ---------------------------------------------------------------------------

@router.get("/orders/{order_id}")
async def get_order(
    order_id: str,
    staff_session: str | None = Cookie(default=None),
    aggregator_session: str | None = Cookie(default=None),
):
    user, role = _require_any(staff_session, aggregator_session)
    db = get_db()

    order = await check_and_close_bidding(order_id, db)

    bids_cursor = db.bids.find({"orderId": order_id}).sort("totalPrice", 1)
    raw_bids = await bids_cursor.to_list(None)
    bids_out = [_bid_to_out(b) for b in raw_bids]

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
):
    _require_any(staff_session, aggregator_session)
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
    import json
    bids_payload = [
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

    await db.orders.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"status": "awaiting_confirmation"}},
    )
    await sse_manager.broadcast(order_id, "order_fulfilled", {})

    # Ask enrollee via Klaire if they received their medication
    enrollee = order.get("enrollee", {})
    phone = enrollee.get("phone")
    if phone:
        asyncio.create_task(notify_order_fulfilled(
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

    if body.received:
        new_status = "completed"
        event = "order_completed"
    else:
        new_status = "not_received"
        event = "order_not_received"

    await db.orders.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"status": new_status}},
    )
    await sse_manager.broadcast(order_id, event, {"received": body.received})

    return {"success": True}
