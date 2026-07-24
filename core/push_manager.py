"""Web push notification manager for the pharmacy backend.

Environment variables required:
  VAPID_PUBLIC_KEY   — base64url-encoded uncompressed EC public key
  VAPID_PRIVATE_PEM  — PEM-encoded EC private key (newlines as literal \\n)
  VAPID_SUBJECT      — mailto: contact for push services
"""
import json
import logging
import os

from .database import get_db

logger = logging.getLogger(__name__)

VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
_VAPID_PRIVATE_PEM_RAW = os.getenv("VAPID_PRIVATE_PEM", "")
VAPID_PRIVATE_PEM = _VAPID_PRIVATE_PEM_RAW.replace("\\n", "\n") if _VAPID_PRIVATE_PEM_RAW else ""
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:ops@clearline.ng")


async def save_subscription(subscription: dict, user_name: str) -> None:
    db = get_db()
    endpoint = subscription.get("endpoint", "")
    if not endpoint:
        return
    await db.push_subscriptions.update_one(
        {"endpoint": endpoint},
        {"$set": {
            "endpoint": endpoint,
            "keys": subscription.get("keys", {}),
            "user_name": user_name,
        }},
        upsert=True,
    )


async def remove_subscription(endpoint: str) -> None:
    db = get_db()
    await db.push_subscriptions.delete_one({"endpoint": endpoint})


async def send_push(title: str, body: str, url: str = "/") -> int:
    """Send push to all stored subscriptions. Returns number of successful sends."""
    if not VAPID_PRIVATE_PEM or not VAPID_PUBLIC_KEY:
        logger.warning("push_manager: VAPID keys not configured, skipping push")
        return 0

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.warning("push_manager: pywebpush not installed")
        return 0

    db = get_db()
    payload = json.dumps({"title": title, "body": body, "url": url})
    subs = await db.push_subscriptions.find({}, {"endpoint": 1, "keys": 1, "_id": 0}).to_list(None)
    sent = 0
    stale = []

    import asyncio
    loop = asyncio.get_event_loop()

    for sub in subs:
        try:
            await loop.run_in_executor(None, lambda s=sub: webpush(
                subscription_info={"endpoint": s["endpoint"], "keys": s.get("keys", {})},
                data=payload,
                vapid_private_key=VAPID_PRIVATE_PEM,
                vapid_claims={"sub": VAPID_SUBJECT},
            ))
            sent += 1
        except WebPushException as e:
            if e.response and e.response.status_code in (404, 410):
                stale.append(sub["endpoint"])
            else:
                logger.warning("push_manager: push failed: %s", e)
        except Exception as e:
            logger.warning("push_manager: unexpected error: %s", e)

    for ep in stale:
        await remove_subscription(ep)

    logger.info("push_manager: sent %d/%d pushes", sent, len(subs))
    return sent
