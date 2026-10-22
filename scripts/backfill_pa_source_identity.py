"""Audited, plan-bound backfill of server-side PA source identity on existing orders.

Never runs on application startup or automatically. Three explicit commands:

  plan     Read-only. Writes a deterministic plan file and prints its SHA-256 digest.
           --mode unstamped | reverify-unverified [--reason R ...] | reverify-revoked
  apply    Writes only the orders listed in an approved plan, and only within four
           hours of the plan's hashed creation time:
           --plan-file PATH --approved-plan-hash HEX --operator STAFF_ID --run-id RUN_ID
  rollback Restores the pre-backfill medications of one run, only where the order is
           still exactly as the backfill left it:
           --run-id RUN_ID --confirm-run-id RUN_ID --operator STAFF_ID

Every command requires a trusted, unrevoked, fully verified catalog snapshot where it
stamps identity, never downgrades a verified line, and never touches orders with PA
transmission evidence, a PA reference owner, an ineligible status or malformed
medications. Writes are transaction-safe compare-and-sets that bump the order
version (staling existing preparations) and append order-history events.

Run as: python -m scripts.backfill_pa_source_identity <command> [options]
"""
import argparse
import asyncio
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta

from bson import ObjectId

from core.pa_catalog_provenance import CatalogConfigurationError, catalog_verification_scope
from core.pa_mapping import canonical_procedure
from core.pa_preparation_store import DomainConflict, now_utc, transaction
from core.pa_source_identity import latest_trusted_snapshot_id, verify_medications
from core.pa_source_namespaces import SOURCE_FIELDS, UNVERIFIED_METHOD, VERIFIED_METHOD

PLAN_VERSION = 1
MODES = ("unstamped", "reverify-unverified", "reverify-revoked")
INELIGIBLE_STATUSES = ("cancelled", "rejected", "recalled")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
DEFAULT_BATCH_SIZE = 200
MAX_BATCH_SIZE = 1000
MAX_PLAN_ORDERS = 100_000
# A plan may be applied only within this window after it was created (not approved).
MAX_PLAN_AGE = timedelta(hours=4)
# Small allowance for clock differences between the planning and applying hosts.
PLAN_CLOCK_SKEW = timedelta(minutes=5)
PROJECTION = {"_id": 1, "version": 1, "status": 1, "medications": 1, "paGeneration": 1, "sourceIdentityBackfill": 1}


class BackfillRefused(ValueError):
    """The requested operation is unsafe or unapproved; nothing was written."""


# --- Canonical encoding -------------------------------------------------------------------

def _encode(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ObjectId):
        return str(value)
    raise TypeError("Unsupported backfill value")


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_encode, allow_nan=False)


def medications_hash(medications):
    return hashlib.sha256(canonical_json(medications).encode("utf-8")).hexdigest()


def plan_digest(plan):
    return hashlib.sha256(canonical_json(plan).encode("utf-8")).hexdigest()


# --- Eligibility --------------------------------------------------------------------------

def transmission_evidence(order):
    """Any trace that IssuePa may have been attempted or a PA reference exists."""
    ledger = order.get("paGeneration")
    if ledger is None:
        return False
    if not isinstance(ledger, dict):
        return True
    if ledger.get("attempts") or ledger.get("active"):
        return True
    lines = ledger.get("lines", [])
    if not isinstance(lines, list):
        return True
    return any(not isinstance(line, dict) or line.get("submittedAt") or line.get("submissionGroupId")
               or line.get("status") not in (None, "pending") or line.get("paNumber") or line.get("groupPaNumber")
               for line in lines)


def eligibility_reason(order):
    """Mode-independent exclusions, re-checked inside every write transaction."""
    if transmission_evidence(order):
        return "PA_TRANSMISSION_EVIDENCE"
    if order.get("status") in INELIGIBLE_STATUSES:
        return "ORDER_STATUS_INELIGIBLE"
    if "version" in order and (type(order["version"]) is not int or order["version"] < 0):
        return "MALFORMED_VERSION"
    medications = order.get("medications")
    if (not isinstance(medications, list) or not medications
            or any(not isinstance(med, dict) or not isinstance(med.get("lineId"), str)
                   or not isinstance(med.get("procedureCode"), str) or not isinstance(med.get("name"), str)
                   for med in medications)):
        return "MALFORMED_MEDICATIONS"
    return None


async def _owned_reference(db, order_id, session=None):
    return await db.pa_reference_owners.find_one({"orderId": str(order_id)}, session=session) is not None


def _method(med):
    verification = med.get("sourceVerification")
    return verification.get("method") if isinstance(verification, dict) else None


# --- Catalog authority --------------------------------------------------------------------

async def require_trusted_snapshot(db, snapshot_id=None):
    """Return a snapshot ID only if it passes full trust, revocation, signature and integrity checks."""
    snapshot_id = snapshot_id or await latest_trusted_snapshot_id(db)
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise BackfillRefused("TRUSTED_SNAPSHOT_REQUIRED")
    entries = await db.pa_procedure_catalog_entries.find({"snapshotId": snapshot_id}).sort("normalizedCode", 1).limit(2).to_list(2)
    probe = next((entry for entry in entries if entry.get("normalizedCode") != "PRE11"), None)
    if not probe:
        raise BackfillRefused("TRUSTED_SNAPSHOT_REQUIRED")
    try:
        async with catalog_verification_scope():
            await canonical_procedure(db, snapshot_id, probe["procedureCode"],
                                      verified_source={"snapshotId": snapshot_id, "code": probe["procedureCode"]})
    except (DomainConflict, CatalogConfigurationError, KeyError, TypeError):
        raise BackfillRefused("TRUSTED_SNAPSHOT_REQUIRED") from None
    return snapshot_id


async def unusable_snapshot_ids(db):
    """Snapshots whose verified bindings no longer resolve: revoked, untrusted or revoked receipt."""
    snapshots = await db.pa_procedure_catalog_snapshots.find({}).to_list(None)
    receipts = await db.pa_catalog_validation_evidence.find({}).to_list(None)
    revoked_receipts = {row.get("snapshotId") for row in receipts if row.get("revokedAt") is not None}
    return sorted(str(row["_id"]) for row in snapshots
                  if row.get("status") != "TRUSTED" or row.get("revokedAt") is not None or row["_id"] in revoked_receipts)


# --- Selection ----------------------------------------------------------------------------

def selection_filter(mode, reasons, unusable):
    """Database-side candidate selection. Python eligibility checks remain authoritative."""
    base = {"status": {"$nin": list(INELIGIBLE_STATUSES)}, "paGeneration.attempts.0": {"$exists": False}}
    if mode == "unstamped":
        return {**base, "medications.sourceNamespaceId": {"$exists": False}}
    if mode == "reverify-unverified":
        match = {"sourceVerification.method": UNVERIFIED_METHOD}
        if reasons:
            match["sourceVerification.reason"] = {"$in": list(reasons)}
        return {**base, "medications": {"$elemMatch": match}}
    return {**base, "medications": {"$elemMatch": {"sourceVerification.method": VERIFIED_METHOD,
                                                   "sourceVerification.snapshotId": {"$in": list(unusable)}}}}


async def iterate_orders(db, query, *, limit=None, batch_size=DEFAULT_BATCH_SIZE):
    """Deterministic _id-ordered pages with a projection; never loads the whole collection."""
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise BackfillRefused("INVALID_BATCH_SIZE")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise BackfillRefused("INVALID_LIMIT")
    last, yielded = None, 0
    while limit is None or yielded < limit:
        page_query = {**query, **({"_id": {"$gt": last}} if last is not None else {})}
        size = batch_size if limit is None else min(batch_size, limit - yielded)
        page = await db.orders.find(page_query, PROJECTION).sort("_id", 1).limit(size).to_list(size)
        if not page:
            return
        for order in page:
            yielded += 1
            yield order
        last = page[-1]["_id"]
        if len(page) < size:
            return


def merge_line(mode, old, new, reasons, unusable):
    """Choose the line to keep. Returns (line, changed, downgrade)."""
    old_method = _method(old)
    if mode == "unstamped":
        return new, True, False
    if mode == "reverify-unverified":
        reason = (old.get("sourceVerification") or {}).get("reason")
        if old_method != UNVERIFIED_METHOD or (reasons and reason not in reasons) or _method(new) != VERIFIED_METHOD:
            return old, False, False  # Unchanged unless it now verifies; no churn on still-unverified lines.
        return new, True, False
    affected = old_method == VERIFIED_METHOD and (old.get("sourceVerification") or {}).get("snapshotId") in unusable
    if not affected:
        return old, False, False
    return new, True, _method(new) != VERIFIED_METHOD


async def plan_order(db, order, *, mode, reasons, unusable, snapshot_id, actor):
    reason = eligibility_reason(order)
    if not reason and await _owned_reference(db, order["_id"]):
        reason = "PA_REFERENCE_OWNED"
    if reason:
        return reason, None
    old_lines = order["medications"]
    if mode == "unstamped" and any(field in med for med in old_lines for field in SOURCE_FIELDS):
        return "ALREADY_STAMPED", None
    base = [{key: value for key, value in med.items() if key not in SOURCE_FIELDS} for med in old_lines]
    stamped = await verify_medications(db, base, actor, snapshot_id=snapshot_id)
    planned, changed = [], False
    for old, new in zip(old_lines, stamped):
        if old.get("lineId") != new.get("lineId"):
            return "MALFORMED_MEDICATIONS", None
        line, line_changed, downgrade = merge_line(mode, old, new, reasons, unusable)
        if downgrade or (_method(old) == VERIFIED_METHOD and _method(line) != VERIFIED_METHOD):
            return "REVERIFY_WOULD_DOWNGRADE", None
        planned.append(line)
        changed = changed or line_changed
    if not changed:
        return "NO_CHANGE", None
    return None, {"orderId": str(order["_id"]), "version": order.get("version", 0), "hasVersion": "version" in order,
                  "medicationsHash": medications_hash(old_lines), "plannedMedications": planned,
                  "plannedMedicationsHash": medications_hash(planned)}


async def build_plan(db, *, mode, reasons=(), snapshot_id=None, limit=None, batch_size=DEFAULT_BATCH_SIZE, created_by="dry-run"):
    """Read-only. Returns (plan, report); nothing is written."""
    if mode not in MODES:
        raise BackfillRefused("INVALID_MODE")
    reasons = tuple(sorted(set(reasons or ())))
    if reasons and mode != "reverify-unverified":
        raise BackfillRefused("REASON_FILTER_REQUIRES_REVERIFY_UNVERIFIED")
    snapshot_id = await require_trusted_snapshot(db, snapshot_id)
    unusable = await unusable_snapshot_ids(db) if mode == "reverify-revoked" else []
    orders, skipped, verified, unverified = [], Counter(), 0, Counter()
    scanned = 0
    async for order in iterate_orders(db, selection_filter(mode, reasons, unusable), limit=limit, batch_size=batch_size):
        scanned += 1
        reason, entry = await plan_order(db, order, mode=mode, reasons=reasons, unusable=set(unusable),
                                         snapshot_id=snapshot_id, actor=f"BACKFILL:{created_by}")
        if reason:
            skipped[reason] += 1
            continue
        if len(orders) >= MAX_PLAN_ORDERS:
            raise BackfillRefused("PLAN_TOO_LARGE")
        for line in entry["plannedMedications"]:
            if _method(line) == VERIFIED_METHOD:
                verified += 1
            else:
                unverified[(line.get("sourceVerification") or {}).get("reason", "UNKNOWN")] += 1
        orders.append(entry)
    # Creation time is set by this tool, never by an argument, and is covered by the plan hash.
    created = now_utc()
    created = created.replace(microsecond=(created.microsecond // 1000) * 1000)
    plan = json.loads(canonical_json({"planVersion": PLAN_VERSION, "mode": mode, "reasons": list(reasons),
        "snapshotId": snapshot_id, "unusableSnapshotIds": unusable, "createdAt": created,
        "createdBy": created_by, "orders": orders}))
    report = {"command": "plan", "mode": mode, "snapshotId": snapshot_id, "ordersScanned": scanned,
              "ordersPlanned": len(orders), "verifiedLines": verified, "unverifiedLines": dict(unverified),
              "skipped": dict(skipped), "planHash": plan_digest(plan)}
    return plan, report


# --- Apply --------------------------------------------------------------------------------

def _restore_medications(planned):
    """Plan JSON stores datetimes as ISO strings; restore the only datetime field."""
    restored = []
    for med in planned:
        if not isinstance(med, dict):
            raise BackfillRefused("PLAN_INVALID")
        med = dict(med)
        verification = med.get("sourceVerification")
        if isinstance(verification, dict) and "verifiedAt" in verification:
            try:
                stamp = datetime.fromisoformat(verification["verifiedAt"])
            except (TypeError, ValueError):
                raise BackfillRefused("PLAN_INVALID") from None
            if stamp.tzinfo is None:
                raise BackfillRefused("PLAN_INVALID")
            med["sourceVerification"] = {**verification, "verifiedAt": stamp}
        restored.append(med)
    return restored


def plan_created_at(plan):
    """Parse the hashed creation time; only an explicit UTC ISO timestamp is accepted."""
    value = plan.get("createdAt") if isinstance(plan, dict) else None
    try:
        created = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        created = None
    if created is None or created.tzinfo is None or created.utcoffset() != timedelta(0):
        raise BackfillRefused("PLAN_TIMESTAMP_INVALID")
    return created


def require_plan_age(plan, now=None):
    """Reject plans older than MAX_PLAN_AGE or dated beyond the allowed clock skew."""
    now = now or now_utc()
    created = plan_created_at(plan)
    if created - now > PLAN_CLOCK_SKEW:
        raise BackfillRefused("PLAN_FUTURE_DATED")
    if now - created > MAX_PLAN_AGE:
        raise BackfillRefused("PLAN_EXPIRED")


def validate_plan(plan, approved_hash):
    if not isinstance(approved_hash, str) or not HASH_PATTERN.match(approved_hash) or plan_digest(plan) != approved_hash:
        raise BackfillRefused("PLAN_HASH_MISMATCH")
    if (not isinstance(plan, dict) or plan.get("planVersion") != PLAN_VERSION or plan.get("mode") not in MODES
            or not isinstance(plan.get("snapshotId"), str) or not isinstance(plan.get("orders"), list)
            or len(plan["orders"]) > MAX_PLAN_ORDERS):
        raise BackfillRefused("PLAN_INVALID")
    require_plan_age(plan)
    seen = set()
    for entry in plan["orders"]:
        if (not isinstance(entry, dict) or not ObjectId.is_valid(entry.get("orderId")) or entry["orderId"] in seen
                or type(entry.get("version")) is not int or entry["version"] < 0 or type(entry.get("hasVersion")) is not bool
                or not HASH_PATTERN.match(str(entry.get("medicationsHash"))) or not HASH_PATTERN.match(str(entry.get("plannedMedicationsHash")))
                or not isinstance(entry.get("plannedMedications"), list)):
            raise BackfillRefused("PLAN_INVALID")
        if medications_hash(_restore_medications(entry["plannedMedications"])) != entry["plannedMedicationsHash"]:
            raise BackfillRefused("PLAN_INVALID")
        seen.add(entry["orderId"])


async def _apply_entry(db, entry, *, plan_hash, snapshot_id, operator, run_id):
    order_id = ObjectId(entry["orderId"])
    planned = _restore_medications(entry["plannedMedications"])
    async with transaction(db) as session:
        current = await db.orders.find_one({"_id": order_id}, session=session)
        if not current:
            raise DomainConflict("ORDER_NOT_FOUND")
        if ("version" in current) != entry["hasVersion"] or current.get("version", 0) != entry["version"]:
            raise DomainConflict("ORDER_VERSION_CHANGED")
        if medications_hash(current.get("medications")) != entry["medicationsHash"]:
            raise DomainConflict("ORDER_MEDICATIONS_CHANGED")
        reason = eligibility_reason(current) or ("PA_REFERENCE_OWNED" if await _owned_reference(db, order_id, session) else None)
        if reason:
            raise DomainConflict(reason)
        now = now_utc()
        record = {"runId": run_id, "planHash": plan_hash, "appliedAt": now, "appliedBy": operator, "snapshotId": snapshot_id,
                  "previousVersion": entry["version"], "previousMedications": current["medications"],
                  "previousMedicationsHash": entry["medicationsHash"], "postVersion": entry["version"] + 1,
                  "postMedicationsHash": entry["plannedMedicationsHash"]}
        push = {"history": {"$each": [{"eventType": "pa_source_identity_backfilled", "timestamp": now,
            "actorId": operator, "actorRole": "staff", "runId": run_id, "planHash": plan_hash, "snapshotId": snapshot_id}]}}
        if isinstance(current.get("sourceIdentityBackfill"), dict):
            push["sourceIdentityBackfillHistory"] = {"$each": [current["sourceIdentityBackfill"]]}
        version_filter = {"version": entry["version"]} if entry["hasVersion"] else {"version": None}
        result = await db.orders.update_one({"_id": order_id, **version_filter},
            {"$set": {"medications": planned, "version": entry["version"] + 1, "sourceIdentityBackfill": record}, "$push": push},
            session=session)
        if result.matched_count != 1:
            raise DomainConflict("ORDER_VERSION_CHANGED")


async def apply_plan(db, plan, *, approved_hash, operator, run_id):
    """Write only the orders listed in an approved plan; skip anything that changed."""
    if not (isinstance(operator, str) and operator.strip() and isinstance(run_id, str) and RUN_ID_PATTERN.match(run_id)):
        raise BackfillRefused("OPERATOR_AND_RUN_ID_REQUIRED")
    validate_plan(plan, approved_hash)
    # The catalog may have been revoked since planning: re-verify before any write.
    await require_trusted_snapshot(db, plan["snapshotId"])
    report = {"command": "apply", "runId": run_id, "planHash": approved_hash, "snapshotId": plan["snapshotId"],
              "ordersListed": len(plan["orders"]), "ordersApplied": 0, "skipped": Counter(), "applied": []}
    for entry in plan["orders"]:
        try:
            await _apply_entry(db, entry, plan_hash=approved_hash, snapshot_id=plan["snapshotId"], operator=operator, run_id=run_id)
        except DomainConflict as exc:
            report["skipped"][str(exc) if str(exc).isupper() else "ORDER_CHANGED"] += 1
            continue
        report["ordersApplied"] += 1
        report["applied"].append({"orderId": entry["orderId"], "previousVersion": entry["version"], "postVersion": entry["version"] + 1})
    report["skipped"] = dict(report["skipped"])
    return report


# --- Rollback -----------------------------------------------------------------------------

async def _rollback_order(db, order_id, *, run_id, operator):
    async with transaction(db) as session:
        current = await db.orders.find_one({"_id": order_id}, session=session)
        record = current.get("sourceIdentityBackfill") if current else None
        if not isinstance(record, dict) or record.get("runId") != run_id:
            raise DomainConflict("NOT_FROM_THIS_RUN")
        if record.get("rolledBackAt") is not None:
            raise DomainConflict("ALREADY_ROLLED_BACK")
        if (current.get("version", 0) != record.get("postVersion")
                or medications_hash(current.get("medications")) != record.get("postMedicationsHash")):
            raise DomainConflict("CHANGED_AFTER_BACKFILL")
        if not isinstance(record.get("previousMedications"), list):
            raise DomainConflict("ROLLBACK_DATA_MISSING")
        if transmission_evidence(current) or await _owned_reference(db, order_id, session):
            raise DomainConflict("PA_TRANSMISSION_EVIDENCE")
        now = now_utc()
        restored_version = record["postVersion"] + 1
        result = await db.orders.update_one({"_id": order_id, "version": record["postVersion"]}, {
            "$set": {"medications": record["previousMedications"], "version": restored_version,
                     "sourceIdentityBackfill": {**record, "rolledBackAt": now, "rolledBackBy": operator,
                                                "rollbackVersion": restored_version}},
            "$push": {"history": {"$each": [{"eventType": "pa_source_identity_backfill_rolled_back", "timestamp": now,
                "actorId": operator, "actorRole": "staff", "runId": run_id}]}}}, session=session)
        if result.matched_count != 1:
            raise DomainConflict("CHANGED_AFTER_BACKFILL")


async def rollback_run(db, *, run_id, operator, limit=None, batch_size=DEFAULT_BATCH_SIZE):
    """Restore one run's orders only where they are still exactly as the backfill left them."""
    if not (isinstance(operator, str) and operator.strip() and isinstance(run_id, str) and RUN_ID_PATTERN.match(run_id)):
        raise BackfillRefused("OPERATOR_AND_RUN_ID_REQUIRED")
    report = {"command": "rollback", "runId": run_id, "ordersRestored": 0, "skipped": Counter(), "restored": []}
    async for order in iterate_orders(db, {"sourceIdentityBackfill.runId": run_id}, limit=limit, batch_size=batch_size):
        try:
            await _rollback_order(db, order["_id"], run_id=run_id, operator=operator)
        except DomainConflict as exc:
            report["skipped"][str(exc)] += 1
            continue
        report["ordersRestored"] += 1
        report["restored"].append(str(order["_id"]))
    report["skipped"] = dict(report["skipped"])
    return report


# --- Command line -------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Plan-bound PA source identity backfill")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Read-only: write a plan file and print its hash")
    plan.add_argument("--mode", choices=MODES, required=True)
    plan.add_argument("--reason", action="append", default=[])
    plan.add_argument("--snapshot-id")
    plan.add_argument("--limit", type=int)
    plan.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    plan.add_argument("--plan-file", required=True)
    plan.add_argument("--created-by", default="dry-run")
    apply = commands.add_parser("apply", help="Apply an approved plan")
    apply.add_argument("--plan-file", required=True)
    apply.add_argument("--approved-plan-hash", required=True)
    apply.add_argument("--operator", required=True)
    apply.add_argument("--run-id", required=True)
    rollback = commands.add_parser("rollback", help="Roll back one applied run")
    rollback.add_argument("--run-id", required=True)
    rollback.add_argument("--confirm-run-id", required=True)
    rollback.add_argument("--operator", required=True)
    rollback.add_argument("--limit", type=int)
    rollback.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    return parser.parse_args(argv)


async def run_command(db, args):
    if args.command == "plan":
        plan, report = await build_plan(db, mode=args.mode, reasons=args.reason, snapshot_id=args.snapshot_id,
                                        limit=args.limit, batch_size=args.batch_size, created_by=args.created_by)
        with open(args.plan_file, "x", encoding="utf-8") as handle:  # Never overwrite a reviewed plan.
            handle.write(canonical_json(plan))
        return report
    if args.command == "apply":
        with open(args.plan_file, encoding="utf-8") as handle:
            try:
                plan = json.loads(handle.read())
            except ValueError:
                raise BackfillRefused("PLAN_INVALID") from None
        return await apply_plan(db, plan, approved_hash=args.approved_plan_hash, operator=args.operator, run_id=args.run_id)
    if args.confirm_run_id != args.run_id:
        raise BackfillRefused("ROLLBACK_CONFIRMATION_MISMATCH")
    return await rollback_run(db, run_id=args.run_id, operator=args.operator, limit=args.limit, batch_size=args.batch_size)


def main(argv=None):
    args = parse_args(argv)
    if args.command == "rollback" and args.confirm_run_id != args.run_id:
        raise SystemExit("Rollback refused: --confirm-run-id must match --run-id.")

    async def execute():
        from motor.motor_asyncio import AsyncIOMotorClient
        uri = os.environ.get("MONGO_URI")
        if not uri:
            raise SystemExit("MONGO_URI is required; nothing was read or written.")
        client = AsyncIOMotorClient(uri, tz_aware=True)
        try:
            return await run_command(client[os.getenv("DB_NAME", "pharmacy_dispatch")], args)
        except BackfillRefused as exc:
            raise SystemExit(f"Backfill refused: {exc}. Nothing further was written.") from None
        finally:
            client.close()

    print(json.dumps(asyncio.run(execute()), indent=2, default=str))


if __name__ == "__main__":
    main()
