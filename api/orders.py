import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator

from bson import ObjectId
from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from core.database import get_db
from core.security import decode_session, generate_code, generate_intake_id
from core.sse import sse_manager
from models.schemas import (
    ApprovalResponse,
    BidOut,
    CreateOrderRequest,
    CreateOrderResponse,
    Enrollee,
    Medication,
    OrderDetail,
    OrderListResponse,
    OrderSummary,
    PlaceBidRequest,
    VerifyCollectionRequest,
)

router = APIRouter(tags=["orders"])

KEEPALIVE_INTERVAL = 15  # seconds


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
    meds = order.get("medications", [])
    diagnosis = meds[0].get("diagnosis", "—") if meds else "—"
    return OrderSummary(
        id=str(order["_id"]),
        intakeId=order["intakeId"],
        enrolleeFullName=order["enrollee"]["fullName"],
        diagnosis=diagnosis,
        status=order["status"],
        biddingEndsAt=order["biddingEndsAt"],
        createdAt=order["createdAt"],
        bidCount=bid_count,
    )


def _order_detail(order: dict, bids: list[BidOut], is_staff: bool) -> OrderDetail:
    return OrderDetail(
        id=str(order["_id"]),
        intakeId=order["intakeId"],
        enrollee=Enrollee(**order["enrollee"]),
        medications=[Medication(**m) for m in order["medications"]],
        biddingEndsAt=order["biddingEndsAt"],
        status=order["status"],
        winnerId=order.get("winnerId"),
        winnerName=order.get("winnerName"),
        winnerTotalPrice=order.get("winnerTotalPrice"),
        collectionCode=order.get("collectionCode") if is_staff else None,
        approvalCode=order.get("approvalCode") if is_staff else None,
        createdAt=order["createdAt"],
        createdBy=order["createdBy"],
        bids=bids,
    )


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
    # Ensure timezone-aware comparison
    if bidding_ends.tzinfo is None:
        bidding_ends = bidding_ends.replace(tzinfo=timezone.utc)

    if now < bidding_ends:
        return order

    # Bidding has expired — find the lowest bid
    bids_cursor = db.bids.find({"orderId": order_id}).sort("totalPrice", 1).limit(1)
    winning_bids = await bids_cursor.to_list(1)

    if not winning_bids:
        # No bids placed — mark as awaiting_fulfillment with no winner
        update = {
            "$set": {
                "status": "awaiting_fulfillment",
                "collectionCode": generate_code(6),
            }
        }
        await db.orders.update_one({"_id": ObjectId(order_id)}, update)
        order = await db.orders.find_one({"_id": ObjectId(order_id)})
        await sse_manager.broadcast(
            order_id,
            "session_closed",
            {"winnerId": None, "winnerName": None, "totalPrice": None},
        )
        return order

    winner = winning_bids[0]
    collection_code = generate_code(6)

    update = {
        "$set": {
            "status": "awaiting_fulfillment",
            "winnerId": winner["aggregatorId"],
            "winnerName": winner["aggregatorName"],
            "winnerTotalPrice": winner["totalPrice"],
            "collectionCode": collection_code,
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
        "medications": [m.model_dump() for m in body.medications],
        "biddingEndsAt": now + timedelta(minutes=10),
        "status": "bidding",
        "winnerId": None,
        "winnerName": None,
        "winnerTotalPrice": None,
        "collectionCode": None,
        "approvalCode": None,
        "createdAt": now,
        "createdBy": staff_user["userId"],
    }

    result = await db.orders.insert_one(doc)
    return CreateOrderResponse(success=True, orderId=str(result.inserted_id))


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

    detail = _order_detail(order, bids_out, is_staff=(role == "staff"))
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
        # Immediately send current bids
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

    # Upsert: update existing bid from this aggregator, or insert new one
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

    # Fetch all bids for this order sorted by totalPrice asc
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
# POST /api/orders/{id}/verify-collection  (aggregator only)
# ---------------------------------------------------------------------------

@router.post("/orders/{order_id}/verify-collection")
async def verify_collection(
    order_id: str,
    body: VerifyCollectionRequest,
    aggregator_session: str | None = Cookie(default=None),
):
    _require_aggregator(aggregator_session)
    db = get_db()

    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.get("status") != "awaiting_fulfillment":
        raise HTTPException(status_code=400, detail="Order is not awaiting fulfillment")

    expected = order.get("collectionCode", "")
    if body.code.strip().upper() != expected:
        raise HTTPException(status_code=400, detail="Invalid collection code")

    await db.orders.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"status": "collection_verified"}},
    )
    await sse_manager.broadcast(order_id, "collection_verified", {})

    return {"success": True}


# ---------------------------------------------------------------------------
# POST /api/orders/{id}/generate-approval  (staff only)
# ---------------------------------------------------------------------------

@router.post("/orders/{order_id}/generate-approval", response_model=ApprovalResponse)
async def generate_approval(
    order_id: str,
    staff_session: str | None = Cookie(default=None),
):
    _require_staff(staff_session)
    db = get_db()

    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.get("status") != "collection_verified":
        raise HTTPException(
            status_code=400,
            detail="Order must be in collection_verified status to generate approval",
        )

    approval_code = generate_code(8)
    await db.orders.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"approvalCode": approval_code, "status": "fulfilled"}},
    )
    await sse_manager.broadcast(
        order_id, "approval_generated", {"approvalCode": approval_code}
    )

    return ApprovalResponse(success=True, approvalCode=approval_code)
