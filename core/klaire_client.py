"""HTTP client for notifying the Klaire WhatsApp service about pharmacy order events."""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_KLAIRE_URL = os.getenv("KLAIRE_BASE_URL", "").rstrip("/")
_PHARMACY_API_URL = os.getenv("PHARMACY_API_URL", "").rstrip("/")


async def _post(event_type: str, payload: dict) -> None:
    if not _KLAIRE_URL:
        logger.warning("KLAIRE_BASE_URL not set — skipping pharmacy notify: %s", event_type)
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_KLAIRE_URL}/pharmacy/notify",
                json={**payload, "event_type": event_type, "callback_url": _PHARMACY_API_URL},
            )
            if resp.status_code not in (200, 201):
                logger.warning("Klaire notify failed event=%s status=%d", event_type, resp.status_code)
    except Exception as exc:
        logger.warning("Klaire notify exception event=%s: %s", event_type, exc)


async def notify_order_created(
    phone: str,
    enrollee_id: str,
    enrollee_name: str,
    medications: list[str],
    order_id: str,
) -> None:
    await _post("order_created", {
        "phone": phone,
        "enrollee_id": enrollee_id,
        "enrollee_name": enrollee_name,
        "medications": medications,
        "order_id": order_id,
    })


async def notify_order_accepted(
    phone: str,
    enrollee_id: str,
    enrollee_name: str,
    pharmacy_name: str,
    order_id: str,
) -> None:
    await _post("order_accepted", {
        "phone": phone,
        "enrollee_id": enrollee_id,
        "enrollee_name": enrollee_name,
        "pharmacy_name": pharmacy_name,
        "order_id": order_id,
    })


async def notify_order_fulfilled(
    phone: str,
    enrollee_id: str,
    enrollee_name: str,
    pharmacy_name: str,
    medications: list[str],
    order_id: str,
) -> None:
    await _post("order_fulfilled", {
        "phone": phone,
        "enrollee_id": enrollee_id,
        "enrollee_name": enrollee_name,
        "pharmacy_name": pharmacy_name,
        "medications": medications,
        "order_id": order_id,
    })
