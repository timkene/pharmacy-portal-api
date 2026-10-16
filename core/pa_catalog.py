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


async def list_snapshots(db, actor, limit, cursor=None):
    from core.pa_read_views import catalog_metadata, snapshot_id
    from core.pa_preparation_store import transaction
    pa_mapping.require_permission(actor, 'canReadCatalog')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Invalid catalog page size')
    query={'_id': {'$gt': cursor}} if cursor else {}
    async with transaction(db) as session:
        rows=await db.pa_procedure_catalog_snapshots.find(query, session=session).sort('_id', 1).limit(101).to_list(101)
        items=[]; last=None; scanned=0; invalid=0
        for row in rows[:100]:
            scanned+=1
            identity=snapshot_id(row.get('_id'))
            if not identity:
                invalid+=1
                continue
            last=identity
            items.append(await catalog_metadata(db, identity, session, verify=False))
            if len(items)==limit: break
        return {'items':items, 'nextCursor':last if scanned<len(rows) else None,
                'skippedInvalidRecords':invalid, 'integrityScope':'METADATA_ONLY'}


async def read_snapshot(db, actor, snapshot_id):
    from core.pa_read_views import catalog_metadata
    from core.pa_preparation_store import transaction
    pa_mapping.require_permission(actor, 'canReadCatalog')
    async with transaction(db) as session:
        result=await catalog_metadata(db, snapshot_id, session)
        if result['status']=='MISSING': raise pa_mapping.DomainConflict('CATALOG_NOT_FOUND')
        return result
