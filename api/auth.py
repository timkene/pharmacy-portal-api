import hashlib
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from bson import ObjectId
from fastapi import APIRouter, Cookie, HTTPException, Request, Response

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
    AggregatorIdentityResponse, AggregatorEmailRequest, ForgotPasswordRequest,
    PasswordChangeRequest, ResetPasswordRequest, TokenRequest,
    AuthResponse,
    StaffLoginRequest,
    StaffIdentityResponse,
    UserResponse,
)

router = APIRouter(tags=["auth"])

_attempts: dict[tuple[str, str], list[float]] = {}


def normalize_username(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _limit(request: Request, action: str, identifier: str, maximum: int = 10) -> None:
    now = time.monotonic()
    key = (action, f"{request.client.host if request.client else 'unknown'}:{normalize_username(identifier)[:160]}")
    values = [stamp for stamp in _attempts.get(key, []) if now - stamp < 60]
    if len(values) >= maximum:
        raise HTTPException(429, detail="Too many requests")
    values.append(now)
    _attempts[key] = values


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _identity(user: dict) -> AggregatorIdentityResponse:
    email = user.get("email")
    return AggregatorIdentityResponse(
        userId=str(user["_id"]), name=user.get("companyName") or "Aggregator", email=email,
        companyName=user.get("companyName"), mustChangePassword=bool(user.get("mustChangePassword", False)),
        emailVerified=bool(user.get("emailVerified", bool(email))), hasEmail=bool(email),
        medicloudProviderId=None if user.get("isTestAggregator", False) else user.get("medicloudProviderId"),
    )


async def _find_aggregator(db, identifier: str):
    user = await db.aggregator_users.find_one({"usernameNormalized": normalize_username(identifier)})
    return user or await db.aggregator_users.find_one({"email": identifier})


def _check_password(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(422, detail="Password must be at least 8 characters")


async def _send_link(user: dict, kind: str, raw: str) -> None:
    from core.mailer import send_email
    base = os.getenv("FRONTEND_BASE_URL", "").rstrip("/")
    path = "verify-email" if kind == "verify" else "reset-password"
    await send_email(user["email"], f"Clearline {kind} link", f"Use this link: {base}/aggregator/{path}?token={raw}")


@router.get("/staff/me", response_model=StaffIdentityResponse)
async def staff_me(response: Response, staff_session: str | None = Cookie(default=None)):
    if not staff_session:
        raise HTTPException(status_code=401, detail="Staff authentication required")
    user = decode_session(staff_session, "staff")
    if not user or user.get("role") != "staff" or any(
        not isinstance(user.get(field), str) for field in ("userId", "name", "email")
    ):
        raise HTTPException(status_code=401, detail="Invalid staff session")
    response.headers["Cache-Control"] = "no-store"
    return StaffIdentityResponse(userId=user["userId"], name=user["name"], email=user["email"])


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
        "role": "staff",
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
    _check_password(body.password)
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
        "username": normalize_username(body.companyName),
        "usernameNormalized": normalize_username(body.companyName),
        "mustChangePassword": False,
        "emailVerified": False,
    }
    from datetime import datetime, timezone
    doc["created_at"] = datetime.now(timezone.utc)

    result = await db.aggregator_users.insert_one(doc)
    user_id = str(result.inserted_id)

    payload = {
        "role": "aggregator",
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
async def aggregator_login(body: AggregatorLoginRequest, response: Response, request: Request):
    # Do not expose whether either identifier exists.
    identifier = body.username or str(body.email)
    _limit(request, "login", identifier)
    db = get_db()
    user = await _find_aggregator(db, identifier)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    payload = {
        "role": "aggregator",
        "userId": str(user["_id"]),
        "name": user["companyName"],
        "email": user.get("email"),
        "limited": bool(user.get("mustChangePassword", False)),
    }
    session_value = encode_session(payload)
    response.set_cookie(key="aggregator_session", value=session_value, **cookie_kwargs())
    return AuthResponse(
        success=True,
        user=_identity(user),
        session=session_value,
    )


def _current_aggregator(aggregator_session: str | None) -> dict:
    if not aggregator_session:
        raise HTTPException(401, "Aggregator authentication required")
    user = decode_session(aggregator_session, "aggregator")
    if not user:
        raise HTTPException(401, "Invalid aggregator session")
    return user


async def _load_current(db, session: str | None) -> dict:
    actor = _current_aggregator(session)
    try:
        lookup = ObjectId(actor["userId"]) if ObjectId.is_valid(actor["userId"]) else actor["userId"]
        user = await db.aggregator_users.find_one({"_id": lookup})
    except Exception:
        user = None
    if not user:
        raise HTTPException(401, "Invalid aggregator session")
    return user


@router.get("/aggregator/me", response_model=AggregatorIdentityResponse)
async def aggregator_me(aggregator_session: str | None = Cookie(default=None)):
    return _identity(await _load_current(get_db(), aggregator_session))


@router.post("/aggregator/change-password")
async def aggregator_change_password(body: PasswordChangeRequest, response: Response,
                                     aggregator_session: str | None = Cookie(default=None)):
    user = await _load_current(get_db(), aggregator_session)
    _check_password(body.newPassword)
    if not verify_password(body.currentPassword, user["password_hash"]):
        raise HTTPException(401, "Invalid current password")
    if verify_password(body.newPassword, user["password_hash"]):
        raise HTTPException(422, "New password must differ from current password")
    await get_db().aggregator_users.update_one({"_id": user["_id"]}, {"$set": {
        "password_hash": hash_password(body.newPassword), "mustChangePassword": False,
    }, "$unset": {"passwordResetTokenHash": "", "passwordResetExpiresAt": ""}})
    refreshed = encode_session({"role": "aggregator", "userId": str(user["_id"]),
                                "name": user.get("companyName", "Aggregator"),
                                "email": user.get("email"), "limited": False})
    response.set_cookie(key="aggregator_session", value=refreshed, **cookie_kwargs())
    return {"success": True}


@router.post("/aggregator/email")
async def aggregator_email(body: AggregatorEmailRequest, request: Request,
                           aggregator_session: str | None = Cookie(default=None)):
    if _current_aggregator(aggregator_session).get("limited"):
        raise HTTPException(403, detail={"code": "password_required", "message": "Password change required"})
    user = await _load_current(get_db(), aggregator_session)
    _limit(request, "verify", str(body.email), 5)
    raw = _new_token()
    await get_db().aggregator_users.update_one({"_id": user["_id"]}, {"$set": {
        "email": str(body.email), "emailVerified": False, "emailVerifyTokenHash": _sha(raw),
        "emailVerifyExpiresAt": datetime.now(timezone.utc) + timedelta(minutes=30),
    }})
    try:
        await _send_link({**user, "email": str(body.email)}, "verify", raw)
    except RuntimeError:
        raise HTTPException(503, "Email delivery is unavailable") from None
    return {"success": True}


@router.post("/aggregator/email/verify")
async def aggregator_verify_email(body: TokenRequest, request: Request,
                                 aggregator_session: str | None = Cookie(default=None)):
    actor = _current_aggregator(aggregator_session) if aggregator_session else None
    raw = body.token or body.code or ""
    query = {"emailVerifyTokenHash": _sha(raw)}
    if actor:
        _limit(request, "verify", actor["userId"], 10)
        if not ObjectId.is_valid(actor["userId"]):
            raise HTTPException(400, "Invalid or expired verification token")
        query["_id"] = ObjectId(actor["userId"])
    else:
        _limit(request, "verify", raw, 10)
    user = await get_db().aggregator_users.find_one(query)
    if not user or not user.get("emailVerifyExpiresAt") or user["emailVerifyExpiresAt"] < datetime.now(timezone.utc):
        if user:
            await get_db().aggregator_users.update_one({"_id": user["_id"]}, {"$unset": {"emailVerifyTokenHash": "", "emailVerifyExpiresAt": ""}})
        raise HTTPException(400, "Invalid or expired verification token")
    await get_db().aggregator_users.update_one({"_id": user["_id"]}, {"$set": {"emailVerified": True}, "$unset": {"emailVerifyTokenHash": "", "emailVerifyExpiresAt": ""}})
    return {"success": True}


@router.post("/aggregator/forgot-password")
async def aggregator_forgot_password(body: ForgotPasswordRequest, request: Request):
    _limit(request, "forgot", body.identifier, 5)
    user = await _find_aggregator(get_db(), body.identifier)
    if user and user.get("email") and user.get("emailVerified") is True:
        raw = _new_token()
        await get_db().aggregator_users.update_one({"_id": user["_id"]}, {"$set": {
            "passwordResetTokenHash": _sha(raw), "passwordResetExpiresAt": datetime.now(timezone.utc) + timedelta(minutes=30),
        }})
        try:
            await _send_link(user, "reset", raw)
        except RuntimeError:
            pass
    return {"success": True}


@router.post("/aggregator/reset-password")
async def aggregator_reset_password(body: ResetPasswordRequest, request: Request):
    _limit(request, "reset", body.token, 10)
    _check_password(body.newPassword)
    user = await get_db().aggregator_users.find_one({"passwordResetTokenHash": _sha(body.token)})
    if not user or not user.get("passwordResetExpiresAt") or user["passwordResetExpiresAt"] < datetime.now(timezone.utc):
        if user:
            await get_db().aggregator_users.update_one({"_id": user["_id"]}, {"$unset": {"passwordResetTokenHash": "", "passwordResetExpiresAt": ""}})
        raise HTTPException(400, "Invalid or expired reset token")
    if verify_password(body.newPassword, user["password_hash"]):
        raise HTTPException(422, "New password must differ from current password")
    await get_db().aggregator_users.update_one({"_id": user["_id"]}, {"$set": {"password_hash": hash_password(body.newPassword), "mustChangePassword": False}, "$unset": {"passwordResetTokenHash": "", "passwordResetExpiresAt": ""}})
    return {"success": True}


# ---------------------------------------------------------------------------
# Logout (clears both cookies)
# ---------------------------------------------------------------------------

@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("staff_session")
    response.delete_cookie("aggregator_session")
    return {"success": True}
