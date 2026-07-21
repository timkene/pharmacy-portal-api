import httpx
from fastapi import APIRouter, Cookie, HTTPException

from core.security import decode_session

router = APIRouter(tags=["search"])

NHIA_BASE = "https://clearline-nhia-api.onrender.com"


def _require_staff(staff_session: str | None) -> dict:
    if not staff_session:
        raise HTTPException(status_code=401, detail="Staff authentication required")
    user = decode_session(staff_session)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid session")
    return user


@router.get("/members/{enrollee_id:path}")
async def get_member_detail(
    enrollee_id: str,
    staff_session: str | None = Cookie(default=None),
):
    _require_staff(staff_session)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{NHIA_BASE}/api/members/{enrollee_id}")
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return {"phone": None, "address": None}
