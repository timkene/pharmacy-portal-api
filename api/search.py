from fastapi import APIRouter, Cookie, HTTPException, Query

from core.motherduck import MOTHERDUCK_TOKEN, md_query
from core.security import decode_session

router = APIRouter(tags=["search"])

SEARCH_LIMIT = 20


def _require_staff(staff_session: str | None) -> dict:
    if not staff_session:
        raise HTTPException(status_code=401, detail="Staff authentication required")
    user = decode_session(staff_session)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid session")
    return user


def _check_md():
    if not MOTHERDUCK_TOKEN:
        raise HTTPException(status_code=503, detail="Search service not configured — set MOTHERDUCK_TOKEN")


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

@router.get("/search/members")
async def search_members(
    q: str = Query(default=""),
    staff_session: str | None = Cookie(default=None),
):
    _require_staff(staff_session)
    _check_md()
    if not q.strip():
        return {"results": []}

    rows = await md_query(
        f"""
        SELECT legacycode, firstname, lastname
        FROM "AI DRIVEN DATA"."MEMBER"
        WHERE legacycode ILIKE ?
           OR firstname  ILIKE ?
           OR lastname   ILIKE ?
           OR (firstname || ' ' || lastname) ILIKE ?
        LIMIT {SEARCH_LIMIT}
        """,
        [f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"],
    )
    return {
        "results": [
            {
                "code": r["legacycode"],
                "label": f"{r.get('firstname', '')} {r.get('lastname', '')}".strip(),
            }
            for r in rows
            if r.get("legacycode")
        ]
    }


@router.get("/members/{enrollee_id}")
async def get_member_detail(
    enrollee_id: str,
    staff_session: str | None = Cookie(default=None),
):
    """Return phone and address for an enrollee by ID (for pharmacy intake form auto-fill)."""
    _require_staff(staff_session)
    _check_md()
    # Try to fetch phone1 and address1; fall back gracefully if address1 column doesn't exist.
    try:
        rows = await md_query(
            'SELECT phone1, address1 FROM "AI DRIVEN DATA"."MEMBER" WHERE legacycode = ? LIMIT 1',
            [enrollee_id],
        )
        phone = rows[0].get("phone1") if rows else None
        address = rows[0].get("address1") if rows else None
    except Exception:
        try:
            rows = await md_query(
                'SELECT phone1 FROM "AI DRIVEN DATA"."MEMBER" WHERE legacycode = ? LIMIT 1',
                [enrollee_id],
            )
            phone = rows[0].get("phone1") if rows else None
            address = None
        except Exception:
            phone = None
            address = None
    return {"phone": phone or None, "address": address or None}


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

@router.get("/search/providers")
async def search_providers(
    q: str = Query(default=""),
    staff_session: str | None = Cookie(default=None),
):
    _require_staff(staff_session)
    _check_md()
    if not q.strip():
        return {"results": []}

    rows = await md_query(
        f"""
        SELECT providerid, providername
        FROM "AI DRIVEN DATA"."PROVIDERS"
        WHERE (providername ILIKE ? OR CAST(providerid AS VARCHAR) ILIKE ?)
          AND isvisible = true
        LIMIT {SEARCH_LIMIT}
        """,
        [f"%{q}%", f"%{q}%"],
    )
    return {
        "results": [
            {"code": str(r["providerid"]), "label": str(r["providername"])}
            for r in rows
            if r.get("providername")
        ]
    }


# ---------------------------------------------------------------------------
# Procedures
# ---------------------------------------------------------------------------

@router.get("/search/procedures")
async def search_procedures(
    q: str = Query(default=""),
    staff_session: str | None = Cookie(default=None),
):
    _require_staff(staff_session)
    _check_md()
    if not q.strip():
        return {"results": []}

    rows = await md_query(
        f"""
        SELECT procedurecode, proceduredesc
        FROM "AI DRIVEN DATA"."PROCEDURE DATA"
        WHERE proceduredesc ILIKE ? OR procedurecode ILIKE ?
        LIMIT {SEARCH_LIMIT}
        """,
        [f"%{q}%", f"%{q}%"],
    )
    return {
        "results": [
            {"code": r["procedurecode"], "label": r["proceduredesc"]}
            for r in rows
            if r.get("procedurecode")
        ]
    }


# ---------------------------------------------------------------------------
# Diagnoses
# ---------------------------------------------------------------------------

@router.get("/search/diagnoses")
async def search_diagnoses(
    q: str = Query(default=""),
    staff_session: str | None = Cookie(default=None),
):
    _require_staff(staff_session)
    _check_md()
    if not q.strip():
        return {"results": []}

    rows = await md_query(
        f"""
        SELECT diagnosiscode, diagnosisdesc
        FROM "AI DRIVEN DATA"."DIAGNOSIS"
        WHERE diagnosisdesc ILIKE ? OR diagnosiscode ILIKE ?
        LIMIT {SEARCH_LIMIT}
        """,
        [f"%{q}%", f"%{q}%"],
    )
    return {
        "results": [
            {"code": r["diagnosiscode"], "label": r["diagnosisdesc"]}
            for r in rows
            if r.get("diagnosiscode")
        ]
    }
