import base64
import json
import hashlib
import hmac
import time
import os
import random
import string

import bcrypt

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

def validate_session_secret() -> str:
    """Fail closed without including configuration values in diagnostics."""
    secret = os.getenv("SESSION_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters")
    return secret


def encode_session(payload: dict) -> str:
    """Sign role-bound sessions; never accept the legacy unsigned cookie."""
    secret = validate_session_secret()
    data = {**payload, "expiresAt": int(time.time()) + 86400}
    encoded = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def decode_session(value: str, expected_role: str | None = None) -> dict | None:
    try:
        secret = os.getenv("SESSION_SECRET", "")
        if len(secret) < 32:
            return None
        encoded, signature = value.split(".")
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode())
        if payload.get("role") not in {"staff", "aggregator"} or not payload.get("userId"):
            return None
        if expected_role and payload["role"] != expected_role:
            return None
        if payload.get("expiresAt", 0) <= time.time():
            return None
        return payload
    except (ValueError, TypeError, AttributeError):
        return None


def cookie_kwargs() -> dict:
    """Return keyword arguments for Response.set_cookie based on environment."""
    if ENVIRONMENT == "production":
        return {"httponly": True, "secure": True, "samesite": "none"}
    return {"httponly": True, "secure": False, "samesite": "lax"}


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------

def generate_intake_id() -> str:
    from datetime import datetime
    date_part = datetime.now().strftime("%Y%m%d")
    rand_part = "".join(random.choices(string.ascii_uppercase, k=4))
    return f"RX-{date_part}-{rand_part}"


def generate_code(length: int) -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=length))
