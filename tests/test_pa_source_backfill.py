"""Plan-bound source identity backfill: trust, approval binding, rollback, targeting, paging."""
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

from bson import ObjectId
import pytest
import pytest_asyncio

from core import pa_preparation as prep
from core import pa_source_identity as identity
from core.pa_preparation_store import now_utc
from core.pa_source_namespaces import INTAKE_REVIEW_NAMESPACE, MEDICLOUD_NAMESPACE, UNVERIFIED_METHOD, VERIFIED_METHOD
from scripts import backfill_pa_source_identity as backfill
from scripts.backfill_pa_source_identity import BackfillRefused
from test_pa_preparation import flow, catalog, prepare, Cursor, NOW  # noqa: F401 (fixtures)

ENTRIES = [{"procedureCode": "DRG-CANON", "procedureDescription": "Canonical medication A"},
           {"procedureCode": "ABC1", "procedureDescription": "Other drug"}]


# --- Mongo-subset fake for the orders collection ------------------------------------------

def _values(doc, path):
    current = [doc]
    for part in path.split("."):
        found = []
        for value in current:
            if isinstance(value, dict) and part in value:
                found.append(value[part])
            elif isinstance(value, list) and part.isdigit():
                if int(part) < len(value):
                    found.append(value[int(part)])
            elif isinstance(value, list):
                found.extend(item[part] for item in value if isinstance(item, dict) and part in item)
        current = found
    return current


def mongo_match(doc, query):
    for key, condition in query.items():
        values = _values(doc, key)
        if isinstance(condition, dict) and condition and all(op.startswith("$") for op in condition):
            for op, arg in condition.items():
                if op == "$exists" and bool(values) != arg: return False
                if op == "$nin" and any(v in arg for v in values): return False
                if op == "$in" and not any(v in arg for v in values): return False
                if op == "$gt" and not any(v is not None and type(v) is type(arg) and v > arg for v in values): return False
                if op == "$elemMatch" and not any(isinstance(item, dict) and mongo_match(item, arg)
                                                  for array in values if isinstance(array, list) for item in array):
                    return False
                if op not in ("$exists", "$nin", "$in", "$gt", "$elemMatch"):
                    raise NotImplementedError(op)
        elif condition is None:
            if any(v is not None for v in values): return False
        elif condition not in values:
            return False
    return True


class MongoOrders:
    def __init__(self, rows):
        self.rows = rows
        self.finds = []

    def find(self, query, projection=None, **kwargs):
        self.finds.append((deepcopy(query), projection))
        rows = [r for r in self.rows if mongo_match(r, query)]
        if projection:
            rows = [{k: v for k, v in r.items() if k in projection} for r in rows]
        return Cursor(rows)

    async def find_one(self, query, **kwargs):
        return next((deepcopy(r) for r in self.rows if mongo_match(r, query)), None)

    async def update_one(self, query, update, **kwargs):
        row = next((r for r in self.rows if mongo_match(r, query)), None)
        if row is None:
            return SimpleNamespace(matched_count=0)
        row.update(deepcopy(update.get("$set", {})))
        for key, value in update.get("$push", {}).items():
            row.setdefault(key, []).extend(deepcopy(value["$each"]))
        return SimpleNamespace(matched_count=1)


# --- Helpers ------------------------------------------------------------------------------

def base_med(code="DRG-CANON", name="Canonical medication A", line="line-1"):
    return {"lineId": line, "procedureCode": code, "name": name, "diagnosisCode": "I10", "quantity": 4}


@pytest_asyncio.fixture
async def setup(flow):
    snap = await catalog(flow.db, ENTRIES)
    template = deepcopy(flow.db.orders.rows[0])
    template["medications"] = [base_med()]
    template["finalProcedurePrices"] = [{"medicationLineId": "line-1", "procedureCode": "DRG-CANON", "amount": 1200}]
    flow.db.orders.rows[0].update(deepcopy(template))
    flow.db.collections["orders"] = MongoOrders(flow.db.orders.rows)
    return SimpleNamespace(db=flow.db, flow=flow, snap=snap, template=template)


def add_order(env, **changes):
    order = {**deepcopy(env.template), "_id": ObjectId(), **changes}
    env.db.orders.rows.append(order)
    return order


def stored(env, order):
    return next(r for r in env.db.orders.rows if r["_id"] == order["_id"])


async def plan_and_apply(env, mode="unstamped", **kwargs):
    plan, report = await backfill.build_plan(env.db, mode=mode, **kwargs)
    result = await backfill.apply_plan(env.db, plan, approved_hash=report["planHash"], operator="ops-1", run_id="run-001")
    return plan, report, result


async def outage_stamp(meds):
    """Lines stamped while the catalog was unreachable (reason TRUSTED_CATALOG_UNAVAILABLE)."""
    return await identity.verify_medications(SimpleNamespace(), meds, "staff")


async def stamp(env, order, snapshot_id):
    stored(env, order)["medications"] = await identity.verify_medications(env.db, [base_med()], "staff", snapshot_id=snapshot_id)


def revoke(env, snapshot):
    next(r for r in env.db.pa_procedure_catalog_snapshots.rows if r["_id"] == snapshot["_id"])["revokedAt"] = now_utc()


# --- B1: trusted catalog required; never downgrade ----------------------------------------

@pytest.mark.asyncio
async def test_plan_and_apply_require_a_fully_verified_trusted_snapshot(setup):
    env = setup
    with pytest.raises(BackfillRefused, match="TRUSTED_SNAPSHOT_REQUIRED"):
        await backfill.build_plan(env.db, mode="unstamped", snapshot_id="no-such-snapshot")
    stored_snapshot = next(r for r in env.db.pa_procedure_catalog_snapshots.rows if r["_id"] == env.snap["_id"])
    next(r for r in env.db.pa_procedure_catalog_entries.rows if r["snapshotId"] == env.snap["_id"])["procedureDescription"] = "Tampered"
    with pytest.raises(BackfillRefused, match="TRUSTED_SNAPSHOT_REQUIRED"):
        await backfill.build_plan(env.db, mode="unstamped", snapshot_id=env.snap["_id"])
    stored_snapshot["status"] = "VALIDATED"
    with pytest.raises(BackfillRefused, match="TRUSTED_SNAPSHOT_REQUIRED"):
        await backfill.build_plan(env.db, mode="unstamped", snapshot_id=env.snap["_id"])


@pytest.mark.asyncio
async def test_no_trusted_catalog_at_all_refuses_planning(setup):
    env = setup
    for row in env.db.pa_procedure_catalog_snapshots.rows:
        row["revokedAt"] = now_utc()
    with pytest.raises(BackfillRefused, match="TRUSTED_SNAPSHOT_REQUIRED"):
        await backfill.build_plan(env.db, mode="reverify-unverified")


@pytest.mark.asyncio
async def test_apply_refuses_when_snapshot_revoked_after_planning(setup):
    env = setup
    plan, report = await backfill.build_plan(env.db, mode="unstamped", snapshot_id=env.snap["_id"])
    revoke(env, env.snap)
    before = deepcopy(env.db.orders.rows)
    with pytest.raises(BackfillRefused, match="TRUSTED_SNAPSHOT_REQUIRED"):
        await backfill.apply_plan(env.db, plan, approved_hash=report["planHash"], operator="ops", run_id="run-001")
    assert env.db.orders.rows == before


@pytest.mark.asyncio
async def test_revoked_reverification_never_downgrades(setup):
    env = setup
    order = env.db.orders.rows[0]
    await stamp(env, order, env.snap["_id"])
    revoke(env, env.snap)
    # Newer trusted catalog that no longer contains the code: re-stamping would downgrade.
    await catalog(env.db, [{"procedureCode": "ABC1", "procedureDescription": "Other drug"}], sourceExtractedAt=NOW - timedelta(minutes=5))
    before = deepcopy(env.db.orders.rows)
    plan, report = await backfill.build_plan(env.db, mode="reverify-revoked")
    assert report["skipped"] == {"REVERIFY_WOULD_DOWNGRADE": 1} and plan["orders"] == []
    assert env.db.orders.rows == before


@pytest.mark.asyncio
async def test_revoked_reverification_moves_to_new_snapshot_when_it_still_verifies(setup):
    env = setup
    order = env.db.orders.rows[0]
    await stamp(env, order, env.snap["_id"])
    untouched = add_order(env)
    await stamp(env, untouched, env.snap["_id"])
    revoke(env, env.snap)
    newer = await catalog(env.db, ENTRIES, sourceExtractedAt=NOW - timedelta(minutes=5))
    stored(env, untouched)["medications"][0]["sourceVerification"]["snapshotId"] = newer["_id"]  # Not affected.
    untouched_before = deepcopy(stored(env, untouched))
    _, report, result = await plan_and_apply(env, mode="reverify-revoked")
    assert report["ordersPlanned"] == 1 and result["ordersApplied"] == 1
    assert stored(env, order)["medications"][0]["sourceVerification"]["snapshotId"] == newer["_id"]
    assert stored(env, untouched) == untouched_before


# --- B2: apply bound to the approved plan --------------------------------------------------

@pytest.mark.asyncio
async def test_plan_is_deterministic_hashed_and_complete(setup, tmp_path):
    env = setup
    add_order(env)
    args = backfill.parse_args(["plan", "--mode", "unstamped", "--plan-file", str(tmp_path / "plan.json"), "--snapshot-id", env.snap["_id"]])
    before = deepcopy(env.db.orders.rows)
    report = await backfill.run_command(env.db, args)
    assert env.db.orders.rows == before  # Planning writes nothing.
    written = json.loads((tmp_path / "plan.json").read_text())
    assert backfill.plan_digest(written) == report["planHash"]
    assert backfill.plan_digest(json.loads(backfill.canonical_json(written))) == report["planHash"]
    assert {entry["orderId"] for entry in written["orders"]} == {str(r["_id"]) for r in env.db.orders.rows}
    for entry in written["orders"]:
        assert set(entry) == {"orderId", "version", "hasVersion", "medicationsHash", "plannedMedications", "plannedMedicationsHash"}
        assert entry["plannedMedications"][0]["sourceNamespaceId"] == MEDICLOUD_NAMESPACE
    assert written["snapshotId"] == env.snap["_id"]
    with pytest.raises(FileExistsError):  # A reviewed plan is never overwritten.
        await backfill.run_command(env.db, args)


@pytest.mark.asyncio
async def test_apply_rejects_wrong_hash_and_tampered_plans(setup):
    env = setup
    plan, report = await backfill.build_plan(env.db, mode="unstamped", snapshot_id=env.snap["_id"])
    before = deepcopy(env.db.orders.rows)
    with pytest.raises(BackfillRefused, match="PLAN_HASH_MISMATCH"):
        await backfill.apply_plan(env.db, plan, approved_hash="0" * 64, operator="ops", run_id="run-001")
    tampered = deepcopy(plan)
    tampered["orders"][0]["plannedMedications"][0]["sourceCode"] = "ABC1"
    with pytest.raises(BackfillRefused, match="PLAN_HASH_MISMATCH"):
        await backfill.apply_plan(env.db, tampered, approved_hash=report["planHash"], operator="ops", run_id="run-001")
    # Even a re-hashed tampered plan fails its per-order planned-medication hash.
    with pytest.raises(BackfillRefused, match="PLAN_INVALID"):
        await backfill.apply_plan(env.db, tampered, approved_hash=backfill.plan_digest(tampered), operator="ops", run_id="run-001")
    with pytest.raises(BackfillRefused, match="OPERATOR_AND_RUN_ID_REQUIRED"):
        await backfill.apply_plan(env.db, plan, approved_hash=report["planHash"], operator="", run_id="run-001")
    assert env.db.orders.rows == before


@pytest.mark.asyncio
async def test_apply_touches_only_listed_unchanged_orders(setup):
    env = setup
    listed = env.db.orders.rows[0]
    changed_version = add_order(env)
    changed_meds = add_order(env)
    transmitted = add_order(env)
    plan, report = await backfill.build_plan(env.db, mode="unstamped", snapshot_id=env.snap["_id"])
    assert report["ordersPlanned"] == 4
    late = add_order(env)  # Discovered after planning: never added.
    stored(env, changed_version)["version"] += 1
    stored(env, changed_meds)["medications"][0]["dosage"] = "changed"
    stored(env, transmitted)["paGeneration"] = {"lines": [{"lineId": "line-1", "status": "verification_required"}]}
    result = await backfill.apply_plan(env.db, plan, approved_hash=report["planHash"], operator="ops", run_id="run-001")
    assert result["ordersApplied"] == 1
    assert result["skipped"] == {"ORDER_VERSION_CHANGED": 1, "ORDER_MEDICATIONS_CHANGED": 1, "PA_TRANSMISSION_EVIDENCE": 1}
    assert stored(env, listed)["medications"][0]["sourceNamespaceId"] == MEDICLOUD_NAMESPACE
    assert "sourceNamespaceId" not in stored(env, late)["medications"][0]
    assert "sourceNamespaceId" not in stored(env, changed_meds)["medications"][0]


@pytest.mark.asyncio
async def test_applied_order_records_rollback_data_history_and_stales_preparation(setup):
    env = setup
    order = env.db.orders.rows[0]
    previous = deepcopy(order["medications"])
    version = order["version"]
    await prepare(env.flow)
    _, report, result = await plan_and_apply(env, snapshot_id=env.snap["_id"])
    record = stored(env, order)["sourceIdentityBackfill"]
    assert result["ordersApplied"] == 1 and stored(env, order)["version"] == version + 1
    assert record["previousMedications"] == previous and record["planHash"] == report["planHash"]
    assert record["postVersion"] == version + 1
    assert record["postMedicationsHash"] == backfill.medications_hash(stored(env, order)["medications"])
    assert stored(env, order)["history"][-1]["eventType"] == "pa_source_identity_backfilled"
    lines = await prep.current_lines(env.db, env.flow.batch["_id"], str(order["_id"]), env.flow.evidence)
    assert lines[0]["isStale"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case,reason", [("attempts", "PA_TRANSMISSION_EVIDENCE"), ("owner", "PA_REFERENCE_OWNED"),
    ("cancelled", None), ("malformed", "MALFORMED_MEDICATIONS")])
async def test_ineligible_orders_are_never_planned(setup, case, reason):
    env = setup
    order = env.db.orders.rows[0]
    if case == "attempts": order["paGeneration"] = {"attempts": [{"groupId": "g"}], "lines": []}
    if case == "owner": await env.db.pa_reference_owners.insert_one({"_id": "PA-1", "orderId": str(order["_id"])})
    if case == "cancelled": order["status"] = "cancelled"
    if case == "malformed": order["medications"] = [{"name": 7}]
    plan, report = await backfill.build_plan(env.db, mode="unstamped", snapshot_id=env.snap["_id"])
    assert plan["orders"] == []
    # Transmitted and cancelled orders are excluded by the database filter itself.
    assert report["skipped"] == ({reason: 1} if reason in ("PA_REFERENCE_OWNED", "MALFORMED_MEDICATIONS") else {})


# --- B3: rollback ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rollback_restores_exactly_and_is_not_repeatable(setup):
    env = setup
    order = env.db.orders.rows[0]
    previous = deepcopy(order["medications"])
    await plan_and_apply(env, snapshot_id=env.snap["_id"])
    post_version = stored(env, order)["version"]
    report = await backfill.rollback_run(env.db, run_id="run-001", operator="ops-2")
    assert report["ordersRestored"] == 1
    assert stored(env, order)["medications"] == previous
    assert stored(env, order)["version"] == post_version + 1
    assert stored(env, order)["sourceIdentityBackfill"]["rolledBackBy"] == "ops-2"
    assert stored(env, order)["history"][-1]["eventType"] == "pa_source_identity_backfill_rolled_back"
    again = await backfill.rollback_run(env.db, run_id="run-001", operator="ops-2")
    assert again["ordersRestored"] == 0 and again["skipped"] == {"ALREADY_ROLLED_BACK": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [("version", "CHANGED_AFTER_BACKFILL"), ("medications", "CHANGED_AFTER_BACKFILL"),
    ("transmitted", "PA_TRANSMISSION_EVIDENCE")])
async def test_rollback_never_overwrites_later_changes(setup, change, reason):
    env = setup
    order = env.db.orders.rows[0]
    await plan_and_apply(env, snapshot_id=env.snap["_id"])
    row = stored(env, order)
    if change == "version": row["version"] += 1
    if change == "medications": row["medications"][0]["dosage"] = "edited later"
    if change == "transmitted": row["paGeneration"] = {"attempts": [{"groupId": "g"}], "lines": []}
    before = deepcopy(env.db.orders.rows)
    report = await backfill.rollback_run(env.db, run_id="run-001", operator="ops")
    assert report["ordersRestored"] == 0 and report["skipped"] == {reason: 1}
    assert env.db.orders.rows == before


@pytest.mark.asyncio
async def test_rollback_only_targets_the_named_run_and_requires_confirmation(setup):
    env = setup
    await plan_and_apply(env, snapshot_id=env.snap["_id"])
    report = await backfill.rollback_run(env.db, run_id="run-other", operator="ops")
    assert report["ordersRestored"] == 0 and report["skipped"] == {}
    with pytest.raises(SystemExit, match="confirm-run-id"):
        backfill.main(["rollback", "--run-id", "run-001", "--confirm-run-id", "run-002", "--operator", "ops"])


# --- B4: targeted re-verification --------------------------------------------------------

@pytest.mark.asyncio
async def test_reverify_unverified_only_changes_lines_that_now_verify(setup):
    env = setup
    unverified = env.db.orders.rows[0]
    stored(env, unverified)["medications"] = await outage_stamp([base_med()])
    assert stored(env, unverified)["medications"][0]["sourceVerification"]["reason"] == "TRUSTED_CATALOG_UNAVAILABLE"
    verified = add_order(env)
    await stamp(env, verified, env.snap["_id"])
    still_unknown = add_order(env, medications=await identity.verify_medications(env.db, [base_med("NOPE", "Unknown")], "staff", snapshot_id=env.snap["_id"]))
    verified_before, unknown_before = deepcopy(stored(env, verified)), deepcopy(stored(env, still_unknown))
    _, report, result = await plan_and_apply(env, mode="reverify-unverified")
    assert result["ordersApplied"] == 1 and report["skipped"] == {"NO_CHANGE": 1}
    assert stored(env, unverified)["medications"][0]["sourceNamespaceId"] == MEDICLOUD_NAMESPACE
    assert stored(env, verified) == verified_before and stored(env, still_unknown) == unknown_before


@pytest.mark.asyncio
async def test_reason_filter_and_mixed_orders_keep_verified_lines_identical(setup):
    env = setup
    order = env.db.orders.rows[0]
    [good] = await identity.verify_medications(env.db, [base_med()], "staff", snapshot_id=env.snap["_id"])
    [pending] = await outage_stamp([base_med("ABC1", "Other drug", "line-2")])
    order["medications"] = [good, pending]
    plan, report = await backfill.build_plan(env.db, mode="reverify-unverified", reasons=["DESCRIPTION_MISMATCH"],
                                             snapshot_id=env.snap["_id"])
    assert plan["orders"] == []
    plan, report = await backfill.build_plan(env.db, mode="reverify-unverified", reasons=["TRUSTED_CATALOG_UNAVAILABLE"],
                                             snapshot_id=env.snap["_id"])
    [entry] = plan["orders"]
    assert backfill.medications_hash([good]) == backfill.medications_hash(backfill._restore_medications(entry["plannedMedications"][:1]))
    assert entry["plannedMedications"][1]["sourceVerification"]["method"] == VERIFIED_METHOD
    with pytest.raises(BackfillRefused, match="REASON_FILTER_REQUIRES_REVERIFY_UNVERIFIED"):
        await backfill.build_plan(env.db, mode="unstamped", reasons=["X"])


@pytest.mark.asyncio
async def test_reverify_excludes_transmitted_orders(setup):
    env = setup
    order = env.db.orders.rows[0]
    order["medications"] = await outage_stamp([base_med()])
    order["paGeneration"] = {"lines": [{"lineId": "line-1", "status": "generated", "paNumber": "PA-1"}]}
    plan, report = await backfill.build_plan(env.db, mode="reverify-unverified")
    assert plan["orders"] == [] and report["skipped"] == {"PA_TRANSMISSION_EVIDENCE": 1}


# --- B5: bounded deterministic paging ------------------------------------------------------

@pytest.mark.asyncio
async def test_paging_is_deterministic_bounded_and_projected(setup):
    env = setup
    for _ in range(6):
        add_order(env)
    expected = sorted(r["_id"] for r in env.db.orders.rows)
    seen = [o["_id"] async for o in backfill.iterate_orders(env.db, {}, batch_size=3)]
    assert seen == expected
    limited = [o["_id"] async for o in backfill.iterate_orders(env.db, {}, batch_size=3, limit=5)]
    assert limited == expected[:5]
    exact = [o["_id"] async for o in backfill.iterate_orders(env.db, {}, batch_size=7)]
    assert exact == expected
    page = [o async for o in backfill.iterate_orders(env.db, {}, batch_size=2, limit=1)][0]
    assert set(page) <= set(backfill.PROJECTION) and "enrollee" not in page
    assert all(projection == backfill.PROJECTION and len(query) <= 1 for query, projection in env.db.orders.finds)
    for bad in ({"batch_size": 0}, {"batch_size": 5000}, {"limit": 0}):
        with pytest.raises(BackfillRefused):
            [o async for o in backfill.iterate_orders(env.db, {}, **bad)]


@pytest.mark.asyncio
async def test_plan_limit_bounds_the_scan(setup):
    env = setup
    for _ in range(4):
        add_order(env)
    plan, report = await backfill.build_plan(env.db, mode="unstamped", snapshot_id=env.snap["_id"], limit=2, batch_size=1)
    assert report["ordersScanned"] == 2 and len(plan["orders"]) == 2


def test_selection_filters_use_database_side_eligibility():
    unstamped = backfill.selection_filter("unstamped", (), [])
    assert unstamped["status"] == {"$nin": ["cancelled", "rejected", "recalled"]}
    assert unstamped["paGeneration.attempts.0"] == {"$exists": False}
    assert unstamped["medications.sourceNamespaceId"] == {"$exists": False}
    targeted = backfill.selection_filter("reverify-unverified", ("TRUSTED_CATALOG_UNAVAILABLE",), [])
    assert targeted["medications"]["$elemMatch"] == {"sourceVerification.method": UNVERIFIED_METHOD,
                                                      "sourceVerification.reason": {"$in": ["TRUSTED_CATALOG_UNAVAILABLE"]}}
    revoked = backfill.selection_filter("reverify-revoked", (), ["snap-a"])
    assert revoked["medications"]["$elemMatch"]["sourceVerification.snapshotId"] == {"$in": ["snap-a"]}


def test_transmission_evidence_detection():
    assert not backfill.transmission_evidence({})
    assert not backfill.transmission_evidence({"paGeneration": {"lines": [{"lineId": "a", "status": "pending"}]}})
    for ledger in ("bad", {"active": True}, {"lines": "bad"}, {"lines": [{"paNumber": "PA-1"}]}, {"lines": [{"groupPaNumber": "G"}]}):
        assert backfill.transmission_evidence({"paGeneration": ledger})
