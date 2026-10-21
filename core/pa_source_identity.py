"""Server-side source identity for order medications.

A medication receives the verified MediCloud namespace only when its exact
procedure code exists in a trusted, unrevoked catalog snapshot and its intake
label matches the canonical description. Everything else is recorded as an
unverified intake code for manual mapping review. Client-supplied source fields
are always discarded. Verification never authorizes or transmits a PA.
"""
import logging

from core.pa_catalog_provenance import CatalogConfigurationError, catalog_verification_scope
from core.pa_mapping import canonical_procedure, normalized_code
from core.pa_preparation_store import DomainConflict, now_utc
from core.pa_source_namespaces import (INTAKE_REVIEW_NAMESPACE, MEDICLOUD_NAMESPACE, SOURCE_FIELDS,
    UNVERIFIED_METHOD, VERIFIED_METHOD)

logger = logging.getLogger(__name__)

_DOMAIN_REASONS = {
    "PROCEDURE_CODE_NOT_FOUND": "CODE_NOT_IN_TRUSTED_CATALOG",
    "CANONICAL_DESCRIPTION_REQUIRED": "CATALOG_DESCRIPTION_INVALID",
    # Same code ignoring case/form, but not the exact catalog code.
    "VERIFIED_SOURCE_BINDING_INVALID": "CODE_FORM_MISMATCH",
}


async def latest_trusted_snapshot_id(db, session=None):
    """Newest trusted, unrevoked snapshot; full trust is re-checked on every lookup."""
    rows = await db.pa_procedure_catalog_snapshots.find({"status": "TRUSTED", "revokedAt": None},
        session=session).sort("sourceExtractedAt", -1).limit(1).to_list(1)
    return rows[0]["_id"] if rows else None


def _labels_match(name, description):
    return (isinstance(name, str) and isinstance(description, str)
            and name.strip().casefold() == description.strip().casefold())


def _without_source_fields(med):
    return {key: value for key, value in med.items() if key not in SOURCE_FIELDS}


def _unverified(base, reason, snapshot_id, actor_id, now):
    code = base.get("procedureCode")
    return {**base, "sourceNamespaceId": INTAKE_REVIEW_NAMESPACE,
        "sourceCode": code if isinstance(code, str) and code.strip() else None,
        "sourceDescription": base.get("name") if isinstance(base.get("name"), str) else "",
        "sourceVerification": {"method": UNVERIFIED_METHOD, "reason": reason, "snapshotId": snapshot_id,
            "verifiedAt": now, "verifiedBy": actor_id}}


async def verify_medication(db, med, snapshot_id, actor_id, now, session=None):
    """Return a new medication dict with server-assigned source identity."""
    base = _without_source_fields(med)
    code = base.get("procedureCode")
    if not isinstance(code, str) or not code.strip() or code != code.strip():
        return _unverified(base, "SOURCE_CODE_INVALID", snapshot_id, actor_id, now)
    if normalized_code(code) == "PRE11":
        return _unverified(base, "RESERVED_PROCEDURE", snapshot_id, actor_id, now)
    if not snapshot_id:
        return _unverified(base, "TRUSTED_CATALOG_UNAVAILABLE", snapshot_id, actor_id, now)
    try:
        entry = await canonical_procedure(db, snapshot_id, code, session,
                                          verified_source={"snapshotId": snapshot_id, "code": code})
    except DomainConflict as exc:
        return _unverified(base, _DOMAIN_REASONS.get(str(exc), "CATALOG_EVIDENCE_INVALID"), snapshot_id, actor_id, now)
    except CatalogConfigurationError:
        return _unverified(base, "CATALOG_AUTHORITY_UNAVAILABLE", snapshot_id, actor_id, now)
    if entry["procedureCode"] != code:
        return _unverified(base, "CODE_FORM_MISMATCH", snapshot_id, actor_id, now)
    if not _labels_match(base.get("name"), entry["procedureDescription"]):
        # Guards against a stale intake mirror or a reused code for a different drug.
        return _unverified(base, "DESCRIPTION_MISMATCH", snapshot_id, actor_id, now)
    return {**base, "sourceNamespaceId": MEDICLOUD_NAMESPACE, "sourceCode": entry["procedureCode"],
        "sourceDescription": entry["procedureDescription"],
        "sourceVerification": {"method": VERIFIED_METHOD, "snapshotId": snapshot_id,
            "catalogCode": entry["procedureCode"], "catalogDescription": entry["procedureDescription"],
            "verifiedAt": now, "verifiedBy": actor_id, "intakeLabelMatched": True}}


async def verify_medications(db, medications, actor_id, *, snapshot_id=None, session=None):
    """Stamp every medication. Catalog problems fail closed to unverified, never block intake."""
    now = now_utc()
    now = now.replace(microsecond=(now.microsecond // 1000) * 1000)  # BSON millisecond precision
    async with catalog_verification_scope():
        if snapshot_id is None:
            try:
                snapshot_id = await latest_trusted_snapshot_id(db, session)
            except Exception as exc:  # Catalog outage must not stop order intake.
                logger.warning("PA source identity: trusted catalog lookup failed (%s)", type(exc).__name__)
                snapshot_id = None
        result = []
        for med in medications:
            try:
                result.append(await verify_medication(db, med, snapshot_id, actor_id, now, session))
            except Exception as exc:  # Fail closed to manual review; logged without payload.
                logger.warning("PA source identity: verification failed (%s)", type(exc).__name__)
                result.append(_unverified(_without_source_fields(med), "CATALOG_UNAVAILABLE", snapshot_id, actor_id, now))
        return result
