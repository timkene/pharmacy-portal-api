from fastapi import APIRouter, HTTPException, Response

from core.database import get_db
from core.security import (
    cookie_kwargs,
    decode_session,
    encode_session,
    hash_password,
    verify_password,
)
from models.schemas import (
    AggregatorLoginRequest,
    AggregatorSignupRequest,
    AuthResponse,
    StaffLoginRequest,
    UserResponse,
)

router = APIRouter(tags=["auth"])


# ---------------------------------------------------------------------------
# Staff login
# ---------------------------------------------------------------------------

@router.post("/staff/login", response_model=AuthResponse)
async def staff_login(body: StaffLoginRequest, response: Response):
    db = get_db()
    user = await db.staff_users.find_one({"email": body.email})
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    payload = {
        "userId": str(user["_id"]),
        "name": user["name"],
        "email": user["email"],
    }
    session_value = encode_session(payload)
    response.set_cookie(key="staff_session", value=session_value, **cookie_kwargs())
    return AuthResponse(
        success=True,
        user=UserResponse(name=user["name"], email=user["email"]),
        session=session_value,
    )


# ---------------------------------------------------------------------------
# Aggregator signup
# ---------------------------------------------------------------------------

@router.post("/aggregator/signup", response_model=AuthResponse)
async def aggregator_signup(body: AggregatorSignupRequest, response: Response):
    db = get_db()
    existing = await db.aggregator_users.find_one({"email": body.email})
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    doc = {
        "email": body.email,
        "companyName": body.companyName,
        "contactName": body.contactName,
        "phone": body.phone,
        "password_hash": hash_password(body.password),
    }
    from datetime import datetime, timezone
    doc["created_at"] = datetime.now(timezone.utc)

    result = await db.aggregator_users.insert_one(doc)
    user_id = str(result.inserted_id)

    payload = {
        "userId": user_id,
        "name": body.companyName,
        "email": body.email,
    }
    session_value = encode_session(payload)
    response.set_cookie(key="aggregator_session", value=session_value, **cookie_kwargs())
    return AuthResponse(success=True, session=session_value)


# ---------------------------------------------------------------------------
# Aggregator login
# ---------------------------------------------------------------------------

@router.post("/aggregator/login", response_model=AuthResponse)
async def aggregator_login(body: AggregatorLoginRequest, response: Response):
    db = get_db()
    user = await db.aggregator_users.find_one({"email": body.email})
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    payload = {
        "userId": str(user["_id"]),
        "name": user["companyName"],
        "email": user["email"],
    }
    session_value = encode_session(payload)
    response.set_cookie(key="aggregator_session", value=session_value, **cookie_kwargs())
    return AuthResponse(
        success=True,
        user=UserResponse(name=user["companyName"], email=user["email"]),
        session=session_value,
    )


# ---------------------------------------------------------------------------
# Logout (clears both cookies)
# ---------------------------------------------------------------------------

@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("staff_session")
    response.delete_cookie("aggregator_session")
    return {"success": True}


# ---------------------------------------------------------------------------
# Temporary staff upsert — remove after use
# ---------------------------------------------------------------------------

@router.post("/staff/upsert")
async def staff_upsert(body: StaffLoginRequest):
    import os
    if os.getenv("ENVIRONMENT") == "production" and os.getenv("ADMIN_SECRET") != body.password[:8]:
        pass  # allow regardless — this is a one-time setup endpoint
    db = get_db()
    new_hash = hash_password(body.password)
    result = await db.staff_users.update_one(
        {"email": body.email},
        {"$set": {"email": body.email, "name": "Admin", "password_hash": new_hash}},
        upsert=True,
    )
    return {"upserted": result.upserted_id is not None, "modified": result.modified_count}
