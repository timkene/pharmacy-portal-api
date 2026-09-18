import hmac
import asyncio
import os
from datetime import datetime, timezone
from typing import AsyncGenerator

from bson import ObjectId
from fastapi import APIRouter, Cookie, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from core.database import get_db
from core.review_flags import compute_review_flags
from core.order_lifecycle import (
    POST_FULFILMENT, DIRECT_ACTIVE, load_order, check_version, require_direct,
    lifecycle_fields, transition,
)
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
    AssignOrderRequest,
    AcceptOrderRequest, ReasonRequest, DirectQuoteRequest, DirectApproveRequest, PriceAdjustmentRequest,
    RejectOrderRequest,
    BidOut,
    ClearlineApproveRequest,
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
    if _PHARMACY_SERVICE_KEY and hmac.compare_digest(x_service_key.encode(), _PHARMACY_SERVICE_KEY.encode()):
        return {"userId": "service", "name": "Clearline Analytics"}
    if not staff_session:
        raise HTTPException(status_code=401, detail="Staff authentication required")
    user = decode_session(staff_session, "staff")
    if not user:
        raise HTTPException(status_code=401, detail="Invalid staff session")
    return user


def _require_aggregator(aggregator_session: str | None) -> dict:
    if not aggregator_session:
        raise HTTPException(status_code=401, detail="Aggregator authentication required")
    user = decode_session(aggregator_session, "aggregator")
    if not user:
        raise HTTPException(status_code=401, detail="Invalid aggregator session")
    return user


def _require_any(
    staff_session: str | None,
    aggregator_session: str | None,
    x_service_key: str = "",
) -> tuple[dict, str]:
    """Return (user_dict, role) where role is 'staff' or 'aggregator'."""
    if _PHARMACY_SERVICE_KEY and hmac.compare_digest(x_service_key.encode(), _PHARMACY_SERVICE_KEY.encode()):
        return {"userId": "service", "name": "Clearline Analytics"}, "staff"
    if staff_session:
        user = decode_session(staff_session, "staff")
        if user:
            return user, "staff"
    if aggregator_session:
        user = decode_session(aggregator_session, "aggregator")
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
        **lifecycle_fields(order),
        reviewFlags=order.get("reviewFlags"),
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
        assignmentType=order.get("assignmentType"),
        denialComment=order.get("denialComment"),
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
        **lifecycle_fields(order),
        history=order.get("history", []) if is_staff else [],
        completedAt=order.get("completedAt"),
        reviewFlags=order.get("reviewFlags") if is_staff else None,
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
        assignmentType=order.get("assignmentType"),
        denialComment=order.get("denialComment") if is_staff else None,
        deniedBy=order.get("deniedBy") if is_staff else None,
        deniedAt=order.get("deniedAt") if is_staff else None,
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
    order = await load_order(db, order_id)

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
        update = {"$set": {"status": "clearline_price_review"}}
        result = await db.orders.update_one({"_id": ObjectId(order_id), "status": "bidding"}, update)
        if not result.matched_count:
            return await load_order(db, order_id)
        order = await load_order(db, order_id)
        await sse_manager.broadcast(
            order_id,
            "session_closed",
            {"winnerId": None, "winnerName": None, "totalPrice": None},
        )
        return order

    winner = winning_bids[0]
    update = {
        "$set": {
            "status": "clearline_price_review",
            "winnerId": winner["aggregatorId"],
            "winnerName": winner["aggregatorName"],
            "winnerTotalPrice": winner["totalPrice"],
        }
    }
    result = await db.orders.update_one({"_id": ObjectId(order_id), "status": "bidding"}, update)
    if not result.matched_count:
        return await load_order(db, order_id)
    order = await load_order(db, order_id)

    # Broadcast bidding-closed so the live table stops; winner info is withheld
    # from aggregators until Clearline approves via /clearline-approve.
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
        "reviewFlags": await compute_review_flags(body.enrollee.model_dump(), now),
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

    try:
        from ..core.push_manager import send_push
        enrollee_name = getattr(body.enrollee, "fullName", None) or "Enrollee"
        await send_push(
            title="New Pharmacy Order",
            body=f"{enrollee_name} — {len(body.medications)} medication(s) pending review",
            url=f"/pharmacy/orders/{order_id}",
        )
    except Exception as _pe:
        pass

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
    await load_order(db, order_id)
    raise HTTPException(405, "Orders cannot be deleted; use cancel or recall with a reason")


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

    order = await load_order(db, order_id)

    if order.get("status") != "pending_review":
        raise HTTPException(status_code=400, detail="Order is not pending review")

    now = datetime.now(timezone.utc)
    result = await db.orders.update_one(
        {"_id": ObjectId(order_id), "status": "pending_review"},
        {"$set": {
            "status": "bidding",
            "biddingEndsAt": now + timedelta(minutes=BIDDING_WINDOW_MINUTES),
        }},
    )
    if not result.matched_count:
        raise HTTPException(status_code=400, detail="Order is not pending review")

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
    body: RejectOrderRequest,
    staff_session: str | None = Cookie(default=None),
    x_service_key: str = Header(default=""),
):
    staff_user = _require_staff(staff_session, x_service_key)
    db = get_db()

    order = await load_order(db, order_id)

    if order.get("status") != "pending_review":
        raise HTTPException(status_code=400, detail="Order is not pending review")

    result = await db.orders.update_one(
        {"_id": ObjectId(order_id), "status": "pending_review"},
        {"$set": {"status": "rejected", "denialComment": body.comment,
                  "deniedBy": {"userId": staff_user["userId"], "name": staff_user.get("name")},
                  "deniedAt": datetime.now(timezone.utc)}},
    )
    if not result.matched_count:
        raise HTTPException(status_code=400, detail="Order is not pending review")
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

    order = await load_order(db, order_id)

    if order.get("status") not in ("pending_review", "rejected"):
        raise HTTPException(status_code=400, detail="Only pending or rejected orders can be edited")

    patch: dict = {}
    if body.enrollee is not None:
        patch["enrollee"] = body.enrollee.model_dump()
        previous_id = (order.get("enrollee") or {}).get("enrolleeId")
        if body.enrollee.enrolleeId != previous_id:
            patch["reviewFlags"] = await compute_review_flags(
                body.enrollee.model_dump(), datetime.now(timezone.utc)
            )
    if body.provider is not None:
        patch["provider"] = body.provider.model_dump()
    if body.medications is not None:
        patch["medications"] = [m.model_dump() for m in body.medications]

    # Editing a rejected order moves it back to pending_review for re-review
    unset = None
    if order.get("status") == "rejected":
        patch["status"] = "pending_review"
        unset = {"denialComment": "", "deniedBy": "", "deniedAt": ""}

    if patch:
        update = {"$set": patch}
        if unset:
            update["$unset"] = unset
        result = await db.orders.update_one(
            {"_id": ObjectId(order_id), "status": order["status"]}, update
        )
        if not result.matched_count:
            raise HTTPException(status_code=400, detail="Order status changed while editing")

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

    order = await load_order(db, order_id)

    if order.get("status") != "bidding":
        raise HTTPException(status_code=400, detail="Order is not in bidding status")

    # Move biddingEndsAt into the past so check_and_close_bidding triggers immediately
    now = datetime.now(timezone.utc)
    await db.orders.update_one(
        {"_id": ObjectId(order_id), "status": "bidding"},
        {"$set": {"biddingEndsAt": now - timedelta(seconds=1)}},
    )
    await check_and_close_bidding(order_id, db)
    return {"success": True}


# ---------------------------------------------------------------------------
# POST /api/orders/{id}/clearline-approve  (staff only)
# Clearline pharmacy team reviews & optionally adjusts the winning price,
# then releases the order to the aggregator for acceptance.
# ---------------------------------------------------------------------------

@router.post("/orders/{order_id}/clearline-approve")
async def clearline_approve(
    order_id: str,
    body: ClearlineApproveRequest | None = None,
    staff_session: str | None = Cookie(default=None),
    x_service_key: str = Header(default=""),
):
    staff = _require_staff(staff_session, x_service_key)
    db = get_db()

    order = await load_order(db, order_id)

    if order.get("status") != "clearline_price_review":
        raise HTTPException(status_code=400, detail="Order is not in Clearline price review")

    patch: dict = {"status": "awaiting_fulfillment"}
    if body and body.adjusted_price is not None:
        patch["winnerTotalPrice"] = body.adjusted_price

    await transition(db, order, patch, "price_approved", staff, "staff")
    order = await db.orders.find_one({"_id": ObjectId(order_id)})

    await sse_manager.broadcast(
        order_id,
        "price_approved",
        {
            "winnerId": order.get("winnerId"),
            "winnerName": order.get("winnerName"),
            "totalPrice": order.get("winnerTotalPrice"),
        },
    )
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
    if role == "aggregator" and order.get("assignmentType") == "direct":
        require_direct(order, user)

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
    user, role = _require_any(staff_session, aggregator_session, x_service_key)
    db = get_db()

    initial_order = await load_order(db, order_id)
    if role == "aggregator" and initial_order.get("assignmentType") == "direct":
        require_direct(initial_order, user)

    async def event_generator() -> AsyncGenerator[str, None]:
        order = await check_and_close_bidding(order_id, db)
        bids_cursor = db.bids.find({"orderId": order_id}).sort("totalPrice", 1)
        raw_bids = await bids_cursor.to_list(None)
        if role == "aggregator":
            raw_bids = [b for b in raw_bids if b["aggregatorId"] == user["userId"]]
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
                except asyncio.TimeoutError:
                    msg = ": keepalive\n\n"
                if role == "aggregator":
                    current = await load_order(db, order_id)
                    if current.get("assignmentType") == "direct" and current.get("winnerId") != user["userId"]:
                        yield 'event: access_revoked\ndata: {"refresh": true}\n\n'
                        return
                    # Shared channel contains competitor bids and staff pricing.
                    # Tell aggregators to refetch their authorized detail instead.
                    yield 'event: order_changed\ndata: {"refresh": true}\n\n'
                else:
                    yield msg
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
    body: AcceptOrderRequest | None = None,
    aggregator_session: str | None = Cookie(default=None),
):
    agg_user = _require_aggregator(aggregator_session)
    db = get_db()

    order = await load_order(db, order_id)

    if order.get("status") != "awaiting_fulfillment":
        raise HTTPException(status_code=400, detail="Order is not awaiting fulfillment")

    if order.get("winnerId") != agg_user["userId"]:
        raise HTTPException(status_code=403, detail="Only the winning aggregator can accept this order")

    if order.get("assignmentType") == "direct":
        check_version(order, body.expectedVersion if body else None)
        if not order.get("priceApprovedAt") or order.get("winnerTotalPrice") is None:
            raise HTTPException(400, "Direct price must be approved before acceptance; recall legacy assignments to requote")
    result = await transition(db, order, {"status": "accepted", "acceptedAt": datetime.now(timezone.utc)},
                             "aggregator_accepted", agg_user, "aggregator")
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

    return result


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

    order = await load_order(db, order_id)

    if order.get("status") != "accepted":
        raise HTTPException(status_code=400, detail="Order must be in accepted status to mark as fulfilled")

    if order.get("winnerId") != agg_user["userId"]:
        raise HTTPException(status_code=403, detail="Only the winning aggregator can fulfill this order")

    if order.get("assignmentType") == "direct":
        check_version(order, body.expectedVersion if body else None)
        if order.get("assignmentVersion", 0) and not order.get("priceApprovedAt"):
            raise HTTPException(400, "Direct price must be approved before fulfilment")

    fulfillment_type = (body.fulfillmentType if body else None) or "picked_up"
    delivery_fee = body.deliveryFee if body else None

    now = datetime.now(timezone.utc)
    enrollee = order.get("enrollee", {})
    phone = enrollee.get("phone")

    if fulfillment_type == "delivered":
        delivered_set = {
            "status": "awaiting_confirmation",
            "fulfillmentType": "delivered",
            "deliveryFee": delivery_fee,
            "fulfilledAt": now,
        }
        current_total = order.get("winnerTotalPrice")
        if current_total is not None:
            delivered_set["winnerTotalPrice"] = current_total + (delivery_fee or 0)
        result = await transition(db, order, delivered_set, "fulfilled", agg_user, "aggregator")
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
        result = await transition(db, order, {
            "status": "completed", "fulfillmentType": "picked_up", "completedAt": now, "fulfilledAt": now,
        }, "fulfilled", agg_user, "aggregator")
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

    return result


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

    order = await load_order(db, order_id)

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

    await transition(db, order, {"status": new_status, "completedAt": now},
                     "receipt_confirmed" if body.received else "receipt_disputed",
                     {"userId": "klaire", "name": "Klaire"}, "system")
    await sse_manager.broadcast(order_id, event, {"received": body.received})

    return {"success": True}


# ---------------------------------------------------------------------------
# POST /api/orders/{id}/staff-confirm  (staff manually confirms receipt)
# ---------------------------------------------------------------------------

@router.post("/orders/{order_id}/staff-confirm")
async def staff_confirm_receipt(
    order_id: str,
    staff_session: str | None = Cookie(default=None),
    x_service_key: str = Header(default=""),
):
    staff = _require_staff(staff_session, x_service_key)
    db = get_db()

    order = await load_order(db, order_id)

    if order.get("status") != "awaiting_confirmation":
        raise HTTPException(
            status_code=400,
            detail="Order must be awaiting confirmation to manually confirm receipt",
        )

    now = datetime.now(timezone.utc)
    await transition(db, order, {"status": "completed", "completedAt": now},
                     "receipt_confirmed", staff, "staff")
    await sse_manager.broadcast(order_id, "order_completed", {"received": True})

    return {"success": True}


# ---------------------------------------------------------------------------
# Push notification subscribe / unsubscribe (staff only)
# ---------------------------------------------------------------------------

from pydantic import BaseModel as _BaseModel

class _PushSubBody(_BaseModel):
    endpoint: str
    keys: dict
    expirationTime: float | None = None


@router.post("/push/subscribe")
async def push_subscribe(
    body: _PushSubBody,
    staff_session: str | None = Cookie(default=None),
    x_service_key: str = Header(default=""),
):
    staff_user = _require_staff(staff_session, x_service_key)
    from core.push_manager import save_subscription, VAPID_PUBLIC_KEY
    await save_subscription({"endpoint": body.endpoint, "keys": body.keys}, staff_user.get("userId", "unknown"))
    return {"status": "subscribed", "vapid_public_key": VAPID_PUBLIC_KEY}


@router.post("/push/unsubscribe")
async def push_unsubscribe(
    body: _PushSubBody,
    staff_session: str | None = Cookie(default=None),
    x_service_key: str = Header(default=""),
):
    _require_staff(staff_session, x_service_key)
    from core.push_manager import remove_subscription
    await remove_subscription(body.endpoint)
    return {"status": "unsubscribed"}


@router.get("/push/vapid-public-key")
async def pharmacy_vapid_public_key():
    from core.push_manager import VAPID_PUBLIC_KEY
    return {"vapid_public_key": VAPID_PUBLIC_KEY}


@router.get("/aggregators")
async def list_aggregators(
    staff_session: str | None = Cookie(default=None),
    x_service_key: str = Header(default=""),
):
    _require_staff(staff_session, x_service_key)
    rows = await get_db().aggregator_users.find(
        {}, {"companyName": 1, "contactName": 1, "email": 1}
    ).to_list(None)
    return [{"id": str(row["_id"]), "companyName": row.get("companyName"),
             "contactName": row.get("contactName"), "email": row.get("email")} for row in rows]


@router.post("/orders/{order_id}/assign")
async def assign_order(
    order_id: str,
    body: AssignOrderRequest,
    staff_session: str | None = Cookie(default=None),
    x_service_key: str = Header(default=""),
):
    staff = _require_staff(staff_session)
    db = get_db()
    if not ObjectId.is_valid(order_id):
        raise HTTPException(status_code=404, detail="Order not found")
    order = await load_order(db, order_id)
    if order.get("status") not in {"pending_review", "direct_reassignment"}:
        raise HTTPException(400, "Order must be pending review or awaiting direct reassignment")
    if order["status"] == "direct_reassignment" or body.expectedVersion is not None:
        check_version(order, body.expectedVersion)
    if not ObjectId.is_valid(body.aggregatorId):
        raise HTTPException(status_code=404, detail="Aggregator not found")
    aggregator = await db.aggregator_users.find_one({"_id": ObjectId(body.aggregatorId)})
    if not aggregator:
        raise HTTPException(status_code=404, detail="Aggregator not found")
    result = await transition(db, order, {
        "status": "direct_quote_requested", "assignmentType": "direct",
        "winnerId": str(aggregator["_id"]), "winnerName": aggregator.get("companyName") or "Aggregator",
        "winnerTotalPrice": None, "biddingEndsAt": None, "directQuote": None,
        "priceApprovedAt": None, "acceptedAt": None,
        "denialComment": None, "deniedBy": None, "deniedAt": None,
        "assignmentVersion": order.get("assignmentVersion", 0) + 1,
    }, "direct_reassigned" if order["status"] == "direct_reassignment" else "direct_assigned", staff, "staff")
    enrollee = order.get("enrollee", {})
    if enrollee.get("phone"):
        asyncio.create_task(notify_order_created(
            phone=enrollee["phone"], enrollee_id=enrollee.get("enrolleeId", ""),
            enrollee_name=enrollee.get("fullName", ""), medications=_med_names(order), order_id=order_id,
        ))
    return result


@router.post("/orders/{order_id}/direct-quote")
async def submit_direct_quote(
    order_id: str, body: DirectQuoteRequest,
    aggregator_session: str | None = Cookie(default=None),
):
    actor = _require_aggregator(aggregator_session)
    db = get_db()
    order = await load_order(db, order_id)
    require_direct(order, actor)
    check_version(order, body.expectedVersion)
    if order["status"] != "direct_quote_requested":
        raise HTTPException(400, "Order is not waiting for a direct quote")
    quote = {"totalPrice": body.totalPrice, "submittedAt": datetime.now(timezone.utc),
             "aggregatorId": actor["userId"], "assignmentVersion": order.get("assignmentVersion", 0)}
    return await transition(db, order, {"status": "direct_price_review", "directQuote": quote},
                            "direct_quote_submitted", actor, "aggregator")


@router.post("/orders/{order_id}/direct-approve")
async def approve_direct_quote(
    order_id: str, body: DirectApproveRequest,
    staff_session: str | None = Cookie(default=None), x_service_key: str = Header(default=""),
):
    actor = _require_staff(staff_session)
    db = get_db()
    order = await load_order(db, order_id)
    require_direct(order)
    check_version(order, body.expectedVersion)
    if order["status"] != "direct_price_review" or not order.get("directQuote"):
        raise HTTPException(400, "Order is not in direct price review")
    quoted_price = order["directQuote"]["totalPrice"]
    price = body.adjusted_price if body.adjusted_price is not None else quoted_price
    adjusted = price != quoted_price
    if adjusted and not body.reason:
        raise HTTPException(422, "A reason is required when adjusting the quote")
    return await transition(db, order, {
        "status": "awaiting_fulfillment", "winnerTotalPrice": price,
        "priceApprovedAt": datetime.now(timezone.utc),
    }, "direct_quote_approved", actor, "staff", body.reason,
        extra_events=("price_adjusted",) if adjusted else ())


@router.post("/orders/{order_id}/direct-deny")
async def deny_direct_quote(
    order_id: str, body: ReasonRequest,
    staff_session: str | None = Cookie(default=None), x_service_key: str = Header(default=""),
):
    actor = _require_staff(staff_session)
    db = get_db()
    order = await load_order(db, order_id)
    require_direct(order)
    check_version(order, body.expectedVersion)
    if order["status"] != "direct_price_review":
        raise HTTPException(400, "Order is not in direct price review")
    return await transition(db, order, {
        "status": "direct_reassignment", "winnerId": None, "winnerName": None,
        "winnerTotalPrice": None, "priceApprovedAt": None,
        "denialComment": body.reason,
        "deniedBy": {"userId": actor["userId"], "name": actor.get("name")},
        "deniedAt": datetime.now(timezone.utc),
    }, "direct_quote_denied", actor, "staff", body.reason)


@router.post("/orders/{order_id}/recall")
async def recall_order(
    order_id: str, body: ReasonRequest,
    staff_session: str | None = Cookie(default=None), x_service_key: str = Header(default=""),
):
    actor = _require_staff(staff_session)
    db = get_db()
    order = await load_order(db, order_id)
    if order["status"] in {"cancelled", "post_fulfilment_recalled"}:
        raise HTTPException(409, f"Order is already terminal ({order['status']}); action unavailable")
    check_version(order, body.expectedVersion)
    if order["status"] in POST_FULFILMENT:
        patch = {"status": "post_fulfilment_recalled", "recalledAt": datetime.now(timezone.utc)}
        event = "post_fulfilment_recalled"
    else:
        require_direct(order)
        if order["status"] not in DIRECT_ACTIVE:
            raise HTTPException(400, "Order cannot be recalled in its current state")
        patch = {"status": "direct_reassignment", "winnerId": None, "winnerName": None,
                 "winnerTotalPrice": None, "priceApprovedAt": None}
        event = "direct_recalled"
    return await transition(db, order, patch, event, actor, "staff", body.reason)


@router.post("/orders/{order_id}/adjust-price")
async def adjust_final_price(
    order_id: str, body: PriceAdjustmentRequest,
    staff_session: str | None = Cookie(default=None), x_service_key: str = Header(default=""),
):
    actor = _require_staff(staff_session)
    db = get_db()
    order = await load_order(db, order_id)
    check_version(order, body.expectedVersion)
    if order["status"] not in POST_FULFILMENT:
        raise HTTPException(400, "Final price can only be adjusted after fulfilment")
    return await transition(db, order, {"winnerTotalPrice": body.totalPrice},
                            "price_adjusted", actor, "staff", body.reason)


@router.post("/orders/{order_id}/cancel")
async def cancel_order(
    order_id: str, body: ReasonRequest,
    staff_session: str | None = Cookie(default=None), x_service_key: str = Header(default=""),
):
    actor = _require_staff(staff_session)
    db = get_db()
    order = await load_order(db, order_id)
    if order["status"] in {"cancelled", "post_fulfilment_recalled"}:
        raise HTTPException(409, f"Order is already terminal ({order['status']}); action unavailable")
    check_version(order, body.expectedVersion)
    if order["status"] not in POST_FULFILMENT:
        raise HTTPException(400, "Administrative cancellation is available after fulfilment")
    return await transition(db, order, {"status": "cancelled", "cancelledAt": datetime.now(timezone.utc)},
                            "cancelled", actor, "staff", body.reason)
