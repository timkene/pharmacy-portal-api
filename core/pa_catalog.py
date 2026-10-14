"""Signed ingestion and independent catalog validation/trust, no mapping actions."""
from core import pa_mapping
from core.pa_catalog_provenance import parse_artifact, verify_artifact
from core.pa_preparation_store import now_utc
from models.pa_preparation import CatalogEvidence, CatalogValidationEvidence


async def ingest_signed_catalog(db, actor, data):
    pa_mapping.require_permission(actor, 'canIngestCatalog')
    artifact = parse_artifact(data)
    provenance, extracted = verify_artifact(artifact)
    evidence = CatalogEvidence(sourceExtractedAt=extracted, recordCount=artifact.manifest.recordCount,
        contentHash=artifact.manifest.contentHash, evidenceReference=provenance['manifestDigest'])
    return await pa_mapping.persist_catalog_snapshot(db, actor, evidence,
        [row.model_dump() for row in artifact.entries], signed_provenance=provenance)


async def validate_signed_catalog(db, actor, snapshot_id):
    pa_mapping.require_permission(actor, 'canValidateCatalog')
    snapshot = await db.pa_procedure_catalog_snapshots.find_one({'_id': snapshot_id})
    if not snapshot: raise pa_mapping.DomainConflict('CATALOG_NOT_FOUND')
    provenance = snapshot.get('signedProvenance') or {}
    evidence = CatalogValidationEvidence(**{key: snapshot[key] for key in CatalogEvidence.model_fields},
        snapshotId=snapshot_id, validationMethod='SERVER_CONTROLLED_MEDICLOUD_EXTRACTION',
        validatedAt=now_utc(), validatedBy=actor.actorId,
        validationEvidenceId=snapshot_id, manifestDigest=provenance.get('manifestDigest'),
        signingKeyFingerprint=provenance.get('signingKeyFingerprint'))
    return await pa_mapping.register_catalog_validation(db, actor, evidence)


def snapshot_view(snapshot):
    return {key: snapshot.get(key) for key in ('source', 'sourceExtractedAt', 'importedAt', 'recordCount', 'status', 'schemaVersion')} | {
        'snapshotId': snapshot['_id'], 'providerTariffEligibility': 'NOT_VERIFIED', 'priceApplicability': 'NOT_VERIFIED'}
