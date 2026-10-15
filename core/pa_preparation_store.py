"""Mongo persistence primitives for preparation only; no order/PA writes."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4
import logging
from pymongo.errors import PyMongoError, DuplicateKeyError


class DomainConflict(ValueError):
    """Refresh domain state; this exception never grants transmission permission."""


logger = logging.getLogger(__name__)


COLLECTIONS = (
    "pa_preparation_batches", "pa_preparation_lines", "pa_mapping_versions",
    "pa_mapping_heads", "pa_procedure_catalog_snapshots",
    "pa_procedure_catalog_entries", "pa_preparation_events",
    "pa_catalog_validation_evidence", "pa_preparation_contexts",
    "pa_mapping_review_exports", "pa_mapping_review_imports",
    "pa_mapping_proposal_decisions", "pa_mapping_activations",
)


async def ensure_preparation_indexes(db):
    indexes = {
        "pa_mapping_proposal_decisions": [([("proposalId", 1)], {"unique": True})],
        "pa_mapping_activations": [([("proposalId", 1)], {"unique": True})],
        "pa_mapping_review_exports": [([("batchId", 1), ("createdAt", -1)], {}), ([("expiresAt", 1)], {})],
        "pa_mapping_review_imports": [([("importId", 1)], {"unique": True})],
        "pa_preparation_contexts": [([("batchId", 1)], {})],
        "pa_catalog_validation_evidence": [([("snapshotId", 1), ("validationEvidenceId", 1)], {"unique": True})],
        "pa_preparation_batches": [([("businessDate", 1), ("status", 1)], {}),
                                   ([("createdBy", 1), ("createdAt", -1)], {})],
        "pa_preparation_lines": [
            ([("batchId", 1), ("_id", 1)], {}),
            ([("batchId", 1), ("orderId", 1), ("orderLineId", 1), ("revision", 1)], {"unique": True}),
            ([("batchId", 1), ("orderId", 1), ("orderLineId", 1)],
             {"unique": True, "partialFilterExpression": {"isCurrent": True}}),
            ([("batchId", 1), ("isCurrent", 1), ("preparationStatus", 1)], {}),
            ([("orderId", 1), ("isCurrent", 1)], {}),
            ([("mappingId", 1), ("isCurrent", 1)], {}),
        ],
        "pa_mapping_versions": [([("provenance", 1), ("status", 1), ("_id", 1)], {}),([("mappingId", 1), ("version", 1)], {"unique": True}),
            ([("sourceNamespaceId", 1), ("sourceIdentityType", 1), ("sourceIdentityKey", 1), ("version", 1)], {"unique": True})],
        "pa_mapping_heads": [([("sourceNamespaceId", 1), ("sourceIdentityType", 1), ("sourceIdentityKey", 1)], {"unique": True})],
        "pa_procedure_catalog_snapshots": [([("status", 1), ("sourceExtractedAt", -1)], {})],
        "pa_procedure_catalog_entries": [([("snapshotId", 1), ("normalizedCode", 1)], {"unique": True})],
        "pa_preparation_events": [([("correlationId", 1), ("timestamp", 1)], {}),
                                  ([("batchId", 1), ("timestamp", 1)], {}),
                                  ([("mappingId", 1), ("timestamp", 1)], {})],
    }
    for name, specs in indexes.items():
        for keys, options in specs:
            try:
                await db[name].create_index(keys, **options)
            except PyMongoError:
                logger.error("Required preparation index failed: collection=%s keys=%s", name, keys)
                raise RuntimeError(f"Required preparation index failed: {name}") from None


@asynccontextmanager
async def transaction(db):
    # No nontransactional fallback. A replica set / transaction-capable Mongo is required.
    try:
        from core.pa_catalog_provenance import catalog_verification_scope
        async with catalog_verification_scope():
            async with await db.client.start_session() as session:
                async with session.start_transaction():
                    yield session
    except PyMongoError as exc:
        if (isinstance(exc, DuplicateKeyError) or exc.has_error_label("TransientTransactionError")
                or exc.has_error_label("UnknownTransactionCommitResult") or getattr(exc, "code", None) in {112, 244, 251}):
            raise DomainConflict("TRANSACTION_CONFLICT_REFRESH_REQUIRED") from None
        raise


def now_utc():
    return datetime.now(timezone.utc)


async def event(db, session, actor, kind, correlation_id, **refs):
    allowed = {"batchId", "preparationLineId", "preparationRevision", "mappingId",
               "previousVersion", "newVersion", "reason", "snapshotId", "memberEvidence",
               "exportId", "importId", "exportRowIds", "rowCount", "skippedRows", "skipReasons"}
    if set(refs) - allowed:
        raise ValueError("Unexpected audit evidence")
    doc = {"_id": str(uuid4()), "eventType": kind, "timestamp": now_utc(),
           "actorId": actor.actorId, "correlationId": correlation_id, **refs}
    await db.pa_preparation_events.insert_one(doc, session=session)
    return doc
