"""Trusted procedure snapshots and immutable mapping versions.

Inputs are internal authenticated/attested domain inputs, not upload contracts.
No refresh, HTTP, SQL, tariff comparison or IssuePa capability exists here.
"""
import hashlib
import json
from uuid import uuid4

from core.pa_preparation_store import DomainConflict, event, now_utc, transaction
from core.pa_catalog_provenance import verify_snapshot, verified_lookup_context
from core.pa_source_namespaces import is_reserved_namespace
from models.pa_preparation import Actor, CatalogEntry, CatalogEvidence, CatalogValidationEvidence, MappingProposal, ReviewEvidence


def normalized_code(code):
    return code.strip().upper()


def catalog_hash(entries):
    """Documented wire hash: sorted canonical code/description pairs, compact UTF-8 JSON."""
    pairs = sorted((r["procedureCode"], r["procedureDescription"]) for r in entries)
    return hashlib.sha256(json.dumps(pairs, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def require_permission(actor, permission):
    if not isinstance(actor, Actor) or not getattr(actor, permission, False):
        raise PermissionError("Authenticated domain permission required")


def require_reviewer(actor):
    if not isinstance(actor, Actor) or not actor.canReviewMappings:
        raise PermissionError("Mapping administrator permission required")


async def persist_catalog_snapshot(db, actor, evidence: CatalogEvidence, entries, *, signed_provenance=None):
    """Bounded ingestion only; matching caller hash/count never grants trust."""
    require_permission(actor, "canIngestCatalog")
    snapshot_id, correlation = str(uuid4()), str(uuid4())
    imported = now_utc()
    reasons, clean, seen = set(), [], set()
    for raw in entries:
        try:
            row = CatalogEntry.model_validate(raw).model_dump()
            code, desc = row["procedureCode"], row["procedureDescription"]
            key = normalized_code(code)
            if not key or code != code.strip():
                reasons.add("INVALID_CODE")
            if not desc.strip() or normalized_code(desc) == key:
                reasons.add("MISSING_CANONICAL_DESCRIPTION")
            if key in seen:
                reasons.add("DUPLICATE_NORMALIZED_CODE")
            seen.add(key)
            clean.append(row)
        except (ValueError, TypeError):
            reasons.add("INVALID_CATALOG_ENTRY")
    if len(clean) != evidence.recordCount:
        reasons.add("RECORD_COUNT_MISMATCH")
    if catalog_hash(clean) != evidence.contentHash:
        reasons.add("CONTENT_HASH_MISMATCH")
    if evidence.sourceExtractedAt > imported or not evidence.evidenceReference.strip():
        reasons.add("INVALID_EXTRACTION_EVIDENCE")
    doc = {"_id": snapshot_id, **evidence.model_dump(), "importedAt": imported,
           "importedBy": actor.actorId, "status": "INVALID" if reasons else "INGESTING", "ingestedCount": 0,
           "validationReasons": sorted(reasons)}
    if signed_provenance is not None:
        doc["signedProvenance"] = signed_provenance
    async with transaction(db) as session:
        await db.pa_procedure_catalog_snapshots.insert_one(doc, session=session)
        await event(db, session, actor, "catalog_ingested" if not reasons else "catalog_invalid",
                    correlation, snapshotId=snapshot_id)
    if reasons:
        return doc
    # Each chunk commits independently. INGESTING/partial snapshots are unusable.
    for offset in range(0, len(clean), 1000):
        chunk = clean[offset:offset + 1000]
        async with transaction(db) as session:
            await db.pa_procedure_catalog_entries.insert_many([
                {"_id": str(uuid4()), "snapshotId": snapshot_id, **row,
                 "normalizedCode": normalized_code(row["procedureCode"])}
                for row in chunk], session=session, ordered=True)
            updated = await db.pa_procedure_catalog_snapshots.update_one(
                {"_id": snapshot_id, "status": "INGESTING", "ingestedCount": offset},
                {"$inc": {"ingestedCount": len(chunk)}}, session=session)
            if updated.matched_count != 1:
                raise DomainConflict("CATALOG_INGEST_CONFLICT")
    async with transaction(db) as session:
        updated = await db.pa_procedure_catalog_snapshots.update_one(
            {"_id": snapshot_id, "status": "INGESTING", "ingestedCount": len(clean)},
            {"$set": {"status": "VALIDATED"}}, session=session)
        if updated.matched_count != 1:
            raise DomainConflict("CATALOG_INCOMPLETE")
        await event(db, session, actor, "catalog_structurally_validated", correlation, snapshotId=snapshot_id)
    return {**doc, "status": "VALIDATED", "ingestedCount": len(clean)}


async def register_catalog_validation(db, actor, evidence: CatalogValidationEvidence):
    """Privileged validator context only, never generic mapping review/upload input.

    Signed source provenance is reverified from persisted entries and configured
    server authority, never inferred from a caller-supplied method label.
    """
    require_permission(actor, "canValidateCatalog")
    if not isinstance(evidence, CatalogValidationEvidence) or evidence.validatedBy != actor.actorId:
        raise PermissionError("Explicit catalog validator identity required")
    if evidence.validatedAt > now_utc() or evidence.validatedAt < evidence.sourceExtractedAt:
        raise DomainConflict("CATALOG_VALIDATION_TIME_INVALID")
    async with transaction(db) as session:
        snapshot = await db.pa_procedure_catalog_snapshots.find_one({"_id": evidence.snapshotId}, session=session)
        if not snapshot or snapshot["status"] != "VALIDATED":
            raise DomainConflict("COMPLETE_VALIDATED_CATALOG_REQUIRED")
        if not snapshot.get('importedBy') or actor.actorId == snapshot['importedBy']:
            raise PermissionError("Independent catalog validator staff identity required")
        provenance = await verify_snapshot(db, snapshot, session)
        if (evidence.manifestDigest != provenance['manifestDigest']
                or evidence.signingKeyFingerprint != provenance['signingKeyFingerprint']):
            raise DomainConflict("CATALOG_VALIDATION_EVIDENCE_MISMATCH")
        fields = ("source", "sourceExtractedAt", "contentHash", "recordCount", "schemaVersion")
        if any(snapshot[k] != getattr(evidence, k) for k in fields):
            raise DomainConflict("CATALOG_VALIDATION_EVIDENCE_MISMATCH")
        await db.pa_catalog_validation_evidence.insert_one(
            {"_id": evidence.validationEvidenceId, **evidence.model_dump()}, session=session)
        await event(db, session, actor, "catalog_validation_recorded", str(uuid4()), snapshotId=evidence.snapshotId)
    return evidence.validationEvidenceId


async def promote_catalog_trust(db, actor, snapshot_id, validation_evidence_id):
    require_permission(actor, "canTrustCatalog")
    async with transaction(db) as session:
        snapshot = await db.pa_procedure_catalog_snapshots.find_one({"_id": snapshot_id}, session=session)
        evidence = await db.pa_catalog_validation_evidence.find_one(
            {"_id": validation_evidence_id, "snapshotId": snapshot_id}, session=session)
        if (not snapshot or snapshot["status"] != "VALIDATED" or not evidence
                or snapshot.get("revokedAt") or evidence.get("revokedAt")):
            raise DomainConflict("CATALOG_TRUST_EVIDENCE_REQUIRED")
        if (not snapshot.get('importedBy') or not evidence.get('validatedBy')
                or snapshot['importedBy'] == evidence['validatedBy']
                or actor.actorId in {snapshot.get('importedBy'), evidence.get('validatedBy')}):
            raise PermissionError("Independent catalog trust staff identity required")
        provenance = await verify_snapshot(db, snapshot, session)
        if (evidence.get('manifestDigest') != provenance['manifestDigest']
                or evidence.get('signingKeyFingerprint') != provenance['signingKeyFingerprint']):
            raise DomainConflict("CATALOG_VALIDATION_EVIDENCE_MISMATCH")
        if evidence.get("validationMethod") != "SERVER_CONTROLLED_MEDICLOUD_EXTRACTION":
            raise DomainConflict("CATALOG_TRUST_METHOD_INVALID")
        if any(snapshot[k] != evidence.get(k) for k in
               ("source", "sourceExtractedAt", "contentHash", "recordCount", "schemaVersion")):
            raise DomainConflict("CATALOG_VALIDATION_EVIDENCE_MISMATCH")
        result = await db.pa_procedure_catalog_snapshots.update_one(
            {"_id": snapshot_id, "status": "VALIDATED"},
            {"$set": {"status": "TRUSTED", "validationEvidenceId": validation_evidence_id,
                      "trustedAt": now_utc(), "trustedBy": actor.actorId}}, session=session)
        if result.matched_count != 1:
            raise DomainConflict("CATALOG_TRUST_CONFLICT")
        await event(db, session, actor, "catalog_trusted", str(uuid4()), snapshotId=snapshot_id)
    return await db.pa_procedure_catalog_snapshots.find_one({"_id": snapshot_id})


async def canonical_procedure(db, snapshot_id, code, session=None, *, existing_mapping=None, reserved_delivery=False,
                              verified_source=None):
    # Resolution only for the fixed reserved delivery contract, never arbitrary codes
    # or authorization of a new mapping/catalog lifecycle transition.
    if reserved_delivery and (code != "PRE11" or existing_mapping is not None or verified_source is not None):
        raise DomainConflict("RESERVED_DELIVERY_PROCEDURE_REQUIRED")
    # Verified source binding: an exact MediCloud code recorded against this snapshot.
    # Like existing mappings it resolves without the 24-hour new-authorization limit,
    # but every trust, revocation, signature and integrity check below still applies.
    if verified_source is not None and (existing_mapping is not None or not isinstance(verified_source, dict)
            or verified_source.get("snapshotId") != snapshot_id or verified_source.get("code") != code
            or not isinstance(code, str) or normalized_code(code) == "PRE11"):
        raise DomainConflict("VERIFIED_SOURCE_BINDING_INVALID")
    snapshot = await db.pa_procedure_catalog_snapshots.find_one({"_id": snapshot_id}, session=session)
    if not snapshot or snapshot["status"] != "TRUSTED" or snapshot.get('revokedAt'):
        raise DomainConflict("TRUSTED_PROCEDURE_SNAPSHOT_REQUIRED")
    validation = await db.pa_catalog_validation_evidence.find_one(
        {"_id": snapshot.get("validationEvidenceId"), "snapshotId": snapshot_id}, session=session)
    if (not validation or validation.get('revokedAt')
            or validation.get("validationMethod") != "SERVER_CONTROLLED_MEDICLOUD_EXTRACTION"):
        raise DomainConflict("CATALOG_TRUST_EVIDENCE_REQUIRED")
    if any(snapshot.get(field) != validation.get(field) for field in
           ("source", "sourceExtractedAt", "contentHash", "recordCount", "schemaVersion")):
        raise DomainConflict("CATALOG_VALIDATION_EVIDENCE_MISMATCH")
    actors = (snapshot.get('importedBy'), validation.get('validatedBy'), snapshot.get('trustedBy'))
    if any(not isinstance(actor, str) or not actor for actor in actors) or len(set(actors)) != 3:
        raise DomainConflict("CATALOG_VALIDATION_EVIDENCE_MISMATCH")
    if existing_mapping is not None:
        mapping_id, version = existing_mapping
        head = await db.pa_mapping_heads.find_one({'_id': mapping_id}, session=session)
        active = await db.pa_mapping_versions.find_one({'mappingId': mapping_id, 'version': version}, session=session)
        if (not head or head.get('activeVersion') != version or not active or active.get('status') != 'ACTIVE'
                or active.get('snapshotId') != snapshot_id or active.get('medicloudCode') != code):
            raise DomainConflict('EXISTING_ACTIVE_MAPPING_REQUIRED')
    verified = await verified_lookup_context(db, snapshot, validation, session,
        authorization=existing_mapping is None and not reserved_delivery and verified_source is None)
    provenance = verified['provenance']
    if (validation.get('manifestDigest') != provenance['manifestDigest']
            or validation.get('signingKeyFingerprint') != provenance['signingKeyFingerprint']):
        raise DomainConflict("CATALOG_VALIDATION_EVIDENCE_MISMATCH")
    entry = await db.pa_procedure_catalog_entries.find_one(
        {"snapshotId": snapshot_id, "normalizedCode": normalized_code(code)}, session=session)
    if not entry:
        raise DomainConflict("PROCEDURE_CODE_NOT_FOUND")
    if (entry.get('procedureCode'), entry.get('procedureDescription')) != verified['entries'].get(normalized_code(code)):
        raise DomainConflict('CATALOG_ENTRY_INTEGRITY_INVALID')
    if verified_source is not None and entry.get('procedureCode') != code:
        raise DomainConflict('VERIFIED_SOURCE_BINDING_INVALID')
    desc = entry.get("procedureDescription", "")
    if not desc.strip() or normalized_code(desc) == entry["normalizedCode"]:
        raise DomainConflict("CANONICAL_DESCRIPTION_REQUIRED")
    return entry


async def propose_mapping(db, actor, proposal: MappingProposal):
    require_permission(actor, "canProposeMappings")
    if is_reserved_namespace(proposal.identity.sourceNamespaceId):
        raise DomainConflict("RESERVED_SOURCE_NAMESPACE")
    identity = proposal.identity.model_dump()
    if proposal.identity.sourceIdentityType == "CODE":
        if not proposal.sourceCode or normalized_code(proposal.sourceCode) != proposal.identity.sourceIdentityKey:
            raise ValueError("Source code identity does not match")
    else:
        # Conservative description identity; no fuzzy normalization or equivalence.
        if proposal.sourceDescription.strip().casefold() != proposal.identity.sourceIdentityKey:
            raise ValueError("Source description identity does not match")
    if not proposal.sourceDescription.strip() or not proposal.proposedMedicloudCode.strip():
        raise ValueError("Nonblank source description and target required")
    async with transaction(db) as session:
        if await db.pa_mapping_heads.find_one(identity, session=session):
            raise DomainConflict("Use explicit expected-version mapping revision")
        mapping_id = str(uuid4())
        doc = {"_id": str(uuid4()), "mappingId": mapping_id, "version": 1,
               **identity, **proposal.model_dump(exclude={"identity"}),
               "status": "PROPOSED", "createdAt": now_utc(), "createdBy": actor.actorId}
        await db.pa_mapping_versions.insert_one(doc, session=session)
        await db.pa_mapping_heads.insert_one({"_id": mapping_id, **identity,
            "version": 1, "activeVersion": None}, session=session)
        await event(db, session, actor, "mapping_proposed", str(uuid4()), mappingId=mapping_id, newVersion=1)
    return doc


async def next_mapping_version(db, mapping_id, session):
    # Pending imported proposals may be newer than the active/current head.
    versions = await db.pa_mapping_versions.find({'mappingId': mapping_id}, session=session).sort('version', -1).limit(1).to_list(1)
    return versions[0]['version'] + 1 if versions else 1


async def revise_mapping(db, actor, mapping_id, expected_version, status, *, snapshot_id=None,
                         review: ReviewEvidence | None = None, replacement: MappingProposal | None = None):
    """Append a version and CAS the head. Review is required even for AI confidence=1."""
    require_reviewer(actor)
    if status not in {"PROPOSED", "VALIDATED", "ACTIVE", "REJECTED", "RETIRED"}:
        raise ValueError("Unknown mapping status")
    async with transaction(db) as session:
        head = await db.pa_mapping_heads.find_one({"_id": mapping_id}, session=session)
        if not head or head["version"] != expected_version:
            raise DomainConflict("MAPPING_VERSION_CONFLICT")
        if is_reserved_namespace(head.get("sourceNamespaceId")) and status != "RETIRED":
            raise DomainConflict("RESERVED_SOURCE_NAMESPACE")
        previous = await db.pa_mapping_versions.find_one(
            {"mappingId": mapping_id, "version": expected_version}, session=session)
        if not previous:
            raise DomainConflict("PREVIOUS_MAPPING_VERSION_MISSING")
        if previous.get("provenance") == "CURSOR_PROPOSAL" and status != "RETIRED":
            raise DomainConflict("EXPLICIT_PROPOSAL_WORKFLOW_REQUIRED")
        if status == "REJECTED" and previous["status"] not in {"PROPOSED", "VALIDATED"}:
            raise DomainConflict("ACTIVE_MAPPING_REQUIRES_RETIREMENT_OR_REPLACEMENT")
        if status in {"PROPOSED", "VALIDATED"} and previous["status"] == "ACTIVE" and not replacement:
            raise DomainConflict("EXPLICIT_REPLACEMENT_PROPOSAL_REQUIRED")
        doc = {**previous, "_id": str(uuid4()), "version": await next_mapping_version(db, mapping_id, session),
               "status": status, "createdAt": now_utc(), "createdBy": actor.actorId}
        if replacement:
            if replacement.identity.model_dump() != {k: head[k] for k in replacement.identity.model_dump()}:
                raise DomainConflict("MAPPING_IDENTITY_IMMUTABLE")
            if replacement.sourceCode != previous.get("sourceCode") or replacement.sourceDescription != previous["sourceDescription"]:
                raise DomainConflict("SOURCE_IDENTITY_IMMUTABLE")
            doc.update(replacement.model_dump(exclude={"identity"}))
        if status not in {"VALIDATED", "ACTIVE", "RETIRED"}:
            for key in ("snapshotId", "medicloudCode", "medicloudDescription", "review"):
                doc.pop(key, None)
        if status in {"VALIDATED", "ACTIVE"}:
            if not snapshot_id:
                raise DomainConflict("TRUSTED_PROCEDURE_SNAPSHOT_REQUIRED")
            entry = await canonical_procedure(db, snapshot_id, doc["proposedMedicloudCode"], session)
            doc.update(snapshotId=snapshot_id, medicloudCode=entry["procedureCode"],
                       medicloudDescription=entry["procedureDescription"])
        if status == "ACTIVE":
            if (not review or review.reviewedBy != actor.actorId
                    or review.reviewedAt > now_utc()):
                raise PermissionError("Explicit current administrator review evidence required")
            doc["review"] = review.model_dump()
        else:
            doc.pop("review", None)
        active = doc["version"] if status == "ACTIVE" else head.get("activeVersion")
        if status == "RETIRED":
            active = None
        result = await db.pa_mapping_heads.update_one({"_id": mapping_id, "version": expected_version},
            {"$set": {"version": doc["version"], "activeVersion": active}}, session=session)
        if result.matched_count != 1:
            raise DomainConflict("MAPPING_VERSION_CONFLICT")
        await db.pa_mapping_versions.insert_one(doc, session=session)
        if active != head.get("activeVersion"):
            await db.pa_preparation_lines.update_many({"mappingId": mapping_id, "isCurrent": True},
                {"$set": {"isCurrent": False, "staleReason": "MAPPING_CHANGED"}}, session=session)
        await event(db, session, actor, "mapping_" + status.lower(), str(uuid4()), mappingId=mapping_id,
                    previousVersion=expected_version, newVersion=doc["version"])
    return {**doc, "activeVersion": active, "isActive": active == doc["version"]}
