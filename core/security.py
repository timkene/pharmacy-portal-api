import base64
import json
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

def encode_session(payload: dict) -> str:
    """Encode a dict as base64(JSON) for use as a cookie value."""
    return base64.b64encode(json.dumps(payload).encode()).decode()


def decode_session(value: str) -> dict | None:
    """Decode a base64(JSON) cookie value; return None on any error."""
    try:
        return json.loads(base64.b64decode(value.encode()).decode())
    except Exception:
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
