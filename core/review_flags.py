"""Frozen, advisory review checks using the existing MediCloud HTTP endpoints."""
import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def is_medication(code):
    return str(code or "").strip().upper().startswith(("DRG", "MED", "PRE", "BRG", "NHIA-17-"))


def medication_benefit(payload):
    rows = payload["benefits"]
    names = {"drg", "drug", "drugs", "medication", "medications", "pharmacy"}
    row = next((r for r in rows if str(r.get("benefit_name", "")).casefold() == "drg"), None)
    if row is None:
        row = next((r for r in rows if str(r.get("benefit_name", "")).casefold() in names), None)
    if row is None:
        raise ValueError("Medication benefit missing")
    used = row.get("total_used")
    if used is None:
        used = float(row.get("claims_used") or 0) + float(row.get("unclaimed_pa_used") or 0)
    remaining = row.get("amount_left")
    return {"limit_amount": row.get("limit_amount"), "utilized_amount": used,
            "remaining_amount": remaining, "benefit_name": row.get("benefit_name"),
            "flagged": remaining is not None and float(remaining) < 15000}


class IncompleteClaimsError(ValueError):
    def __init__(self, items):
        super().__init__("Claims contain invalid rows or medication encounter dates")
        self.items = items


def recent_medication(payload, start, end):
    if isinstance(payload, list):
        rows = payload
    elif not isinstance(payload, dict):
        raise ValueError("Claims payload missing")
    elif "results" in payload:
        rows = payload.get("results")
    elif "claims" in payload:
        rows = payload.get("claims")
    else:
        raise ValueError("Claims payload missing")
    if not isinstance(rows, list):
        raise ValueError("Claims collection must be a list")
    items = []
    incomplete = False
    for row in rows:
        if not isinstance(row, dict):
            incomplete = True
            continue
        if not is_medication(row.get("procedure_code")):
            continue
        day = parse_date(row.get("encounter_date_from"))
        if day is None:
            incomplete = True
            continue
        if start <= day <= end:
            amount = row.get("approved_amount")
            items.append({"date": day.isoformat(), "procedure_code": row.get("procedure_code"),
                          "description": row.get("description") or row.get("procedure_description"),
                          "provider": row.get("provider") or row.get("provider_name"),
                          "amount": amount if amount is not None else row.get("charge_amount")})
    if incomplete:
        raise IncompleteClaimsError(items)
    return items


def _medicloud_error(exc):
    status = getattr(getattr(exc, "response", None), "status_code", None)
    logger.warning("MediCloud review check failed (%s%s)", type(exc).__name__,
                   f" HTTP {status}" if status is not None else "")
    return f"MediCloud check failed ({type(exc).__name__})"


async def compute_review_flags(enrollee, created_at):
    now = created_at.astimezone(timezone.utc)
    day = now.date()
    # Inclusive lookback: request_date minus 21 days through request_date.
    # Example: 2026-09-14 includes 2026-08-24 (exactly 21 days ago) and excludes 2026-08-23.
    start = day - timedelta(days=21)
    termination = parse_date(enrollee.get("terminationDate"))
    terminated = enrollee.get("isterminated") is True or (termination is not None and termination < day)
    snapshot = {
        "checked_at": now.isoformat(), "request_date": day.isoformat(), "codes": [],
        "enrollee_id": enrollee.get("enrolleeId"),
        "enrollee_status": {"isterminated": enrollee.get("isterminated"),
                            "terminationDate": enrollee.get("terminationDate"), "flagged": terminated},
        "medication_benefit": {"limit_amount": None, "utilized_amount": None,
                               "remaining_amount": None, "benefit_name": None, "flagged": False,
                               "checked_at": now.isoformat()},
        "recent_medication": {"from_date": start.isoformat(), "to_date": day.isoformat(),
                              "flagged": False, "items": []},
    }
    base = os.getenv("MEDICLOUD_API_URL", "https://api.clearlinehmo.com").rstrip("/")
    headers = {"X-API-Key": os.getenv("MEDICLOUD_API_KEY", "")}

    async def fetch_benefit(client):
        response = await client.post(f"{base}/enrollee-utilization/by-benefit", json={
            "enrollee_id": enrollee["enrolleeId"], "start_date": date(day.year, 1, 1).isoformat(),
            "end_date": day.isoformat()})
        response.raise_for_status()
        snapshot["medication_benefit"].update(medication_benefit(response.json()))

    async def fetch_claims(client):
        response = await client.get(f"{base}/claims-lookup", params={
            "enrollee_id": enrollee["enrolleeId"], "from_date": start.isoformat(), "to_date": day.isoformat()})
        response.raise_for_status()
        try:
            items = recent_medication(response.json(), start, day)
        except IncompleteClaimsError as exc:
            snapshot["recent_medication"].update(items=exc.items, flagged=bool(exc.items))
            raise
        snapshot["recent_medication"].update(items=items, flagged=bool(items))

    async def run(section, coro):
        try:
            await coro
        except Exception as exc:
            snapshot[section]["error"] = _medicloud_error(exc)

    async with httpx.AsyncClient(timeout=5, headers=headers) as client:
        await asyncio.gather(
            run("medication_benefit", fetch_benefit(client)),
            run("recent_medication", fetch_claims(client)),
        )
    for section, code in (("enrollee_status", "ENROLLEE_TERMINATED"),
                          ("medication_benefit", "LOW_MEDICATION_BENEFIT"),
                          ("recent_medication", "MEDICATION_USED_WITHIN_LAST_21_DAYS")):
        if snapshot[section]["flagged"]:
            snapshot["codes"].append(code)
    if snapshot["medication_benefit"].get("error") or snapshot["recent_medication"].get("error"):
        snapshot["codes"].append("REVIEW_CHECKS_INCOMPLETE")
    return snapshot
