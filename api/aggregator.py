from fastapi import APIRouter, Cookie, HTTPException

from core.database import get_db
from core.security import decode_session

router = APIRouter(tags=["aggregator"])


def _require_aggregator(aggregator_session: str | None) -> dict:
    if not aggregator_session:
        raise HTTPException(status_code=401, detail="Aggregator authentication required")
    user = decode_session(aggregator_session)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid aggregator session")
    return user


def _serialize_order(order: dict) -> dict:
    """Convert a MongoDB order document to a JSON-serialisable dict."""
    out = {k: v for k, v in order.items()}
    out["id"] = str(out.pop("_id"))
    # Remove fields aggregators should not see
    out.pop("collectionCode", None)
    out.pop("approvalCode", None)
    # Serialise datetimes
    for field in ("biddingEndsAt", "createdAt"):
        if field in out and hasattr(out[field], "isoformat"):
            out[field] = out[field].isoformat()
    return out


@router.get("/aggregator/orders")
async def aggregator_orders_list(
    aggregator_session: str | None = Cookie(default=None),
):
    """All orders relevant to this aggregator — open sessions, active won orders, fulfilled."""
    agg_user = _require_aggregator(aggregator_session)
    agg_id = agg_user["userId"]
    db = get_db()

    open_cursor = db.orders.find({"status": "bidding"}).sort("biddingEndsAt", 1)
    open_orders = [_serialize_order(o) async for o in open_cursor]

    active_statuses = ["awaiting_fulfillment", "accepted", "awaiting_confirmation"]
    active_cursor = db.orders.find(
        {"winnerId": agg_id, "status": {"$in": active_statuses}}
    ).sort("createdAt", -1)
    active_orders = [_serialize_order(o) async for o in active_cursor]

    fulfilled_cursor = db.orders.find(
        {"winnerId": agg_id, "status": "completed"}
    ).sort("createdAt", -1)
    fulfilled_orders = [_serialize_order(o) async for o in fulfilled_cursor]

    return {
        "open": open_orders,
        "active": active_orders,
        "fulfilled": fulfilled_orders,
        "counts": {
            "open": len(open_orders),
            "active": len(active_orders),
            "fulfilled": len(fulfilled_orders),
        },
    }


@router.get("/aggregator/dashboard")
async def aggregator_dashboard(
    aggregator_session: str | None = Cookie(default=None),
):
    agg_user = _require_aggregator(aggregator_session)
    agg_id = agg_user["userId"]
    db = get_db()

    # Open bidding sessions (any aggregator can see these to place bids)
    open_cursor = db.orders.find({"status": "bidding"}).sort("biddingEndsAt", 1)
    open_orders = [_serialize_order(o) async for o in open_cursor]

    # Orders this aggregator won that are still active
    won_statuses = ["awaiting_fulfillment", "collection_verified"]
    won_cursor = db.orders.find(
        {"winnerId": agg_id, "status": {"$in": won_statuses}}
    ).sort("createdAt", -1)
    won_orders = [_serialize_order(o) async for o in won_cursor]

    # Orders this aggregator won that are fully fulfilled
    completed_cursor = db.orders.find(
        {"winnerId": agg_id, "status": "fulfilled"}
    ).sort("createdAt", -1)
    completed_orders = [_serialize_order(o) async for o in completed_cursor]

    return {
        "openSessions": open_orders,
        "wonOrders": won_orders,
        "completedOrders": completed_orders,
    }
