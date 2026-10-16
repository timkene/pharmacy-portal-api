"""Allowlisted read projections. No writes or authorization side effects."""
from datetime import datetime, timezone
import re
from uuid import UUID
from core import pa_catalog_provenance as provenance
from core.pa_preparation_store import DomainConflict


def safe_id(value):
    return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,199}',value) else None


def timestamp(value):
    try:
        return value.astimezone(timezone.utc) if isinstance(value,datetime) and value.tzinfo is not None else None
    except (ValueError,OverflowError): return None


def actor_step(identity=None,at=None,*,expected=False):
    identity,at=safe_id(identity),timestamp(at)
    if at and at>provenance.now_utc(): at=None
    return {'state':'COMPLETED' if identity and at else 'INVALID' if expected else 'PENDING',
            'staffId':identity if identity and at else None,'at':at if identity and at else None}


def snapshot_id(value):
    try: return str(UUID(value)) if isinstance(value,str) and str(UUID(value))==value else None
    except (ValueError,TypeError,AttributeError): return None


def freshness(extracted):
    now=provenance.now_utc()
    try: expires=extracted+provenance.CATALOG_MAX_AGE if extracted else None
    except OverflowError: extracted=expires=None
    return {'evaluatedAt':now,'expiresAt':expires,'maximumAgeHours':24,
        'state':'UNKNOWN' if not extracted else 'FUTURE' if extracted>now else 'EXPIRED' if now>=expires else 'FRESH'}


async def catalog_metadata(db,identity,session=None,*,verify=True):
    identity=snapshot_id(identity)
    raw=await db.pa_procedure_catalog_snapshots.find_one({'_id':identity},session=session) if identity else None
    result={'snapshotId':identity,'status':'MISSING','source':None,'sourceExtractedAt':None,'importedAt':None,
        'validatedAt':None,'trustedAt':None,'recordCount':None,'schemaVersion':None,'revokedAt':None,
        'revoked':False,'integrityStatus':'NOT_EVALUATED','trustStatus':'UNTRUSTED',
        'actors':{'uploader':actor_step(),'validator':actor_step(),'truster':actor_step()},
        'freshness':freshness(None),'usableForNewAuthorization':False,'usableForExistingResolution':False,
        'providerTariffEligibility':'NOT_VERIFIED','coverageEligibility':'NOT_VERIFIED','priceApplicability':'NOT_VERIFIED'}
    if not raw: return result
    status=raw.get('status')
    result['status']=status if status in ('INGESTING','VALIDATED','TRUSTED','INVALID') else 'INVALID'
    result.update(source='MEDICLOUD_PROCEDUREDATA' if raw.get('source')=='MEDICLOUD_PROCEDUREDATA' else None,
        sourceExtractedAt=timestamp(raw.get('sourceExtractedAt')),importedAt=timestamp(raw.get('importedAt')),
        trustedAt=timestamp(raw.get('trustedAt')),revokedAt=timestamp(raw.get('revokedAt')),
        revoked=raw.get('revokedAt') is not None,
        recordCount=raw['recordCount'] if type(raw.get('recordCount')) is int and 0<raw['recordCount']<=100000 else None,
        schemaVersion=1 if type(raw.get('schemaVersion')) is int and raw['schemaVersion']==1 else None)
    result['freshness']=freshness(result['sourceExtractedAt'])
    # Bounded receipt lookup also detects ambiguous legacy validation evidence.
    receipts=await db.pa_catalog_validation_evidence.find({'snapshotId':identity},session=session).limit(2).to_list(2)
    evidence=receipts[0] if len(receipts)==1 else None
    result['validatedAt']=timestamp(evidence.get('validatedAt')) if evidence else None
    result['revoked']=result['revoked'] or bool(evidence and evidence.get('revokedAt') is not None)
    result['actors']={'uploader':actor_step(raw.get('importedBy'),raw.get('importedAt'),expected=True),
        'validator':actor_step(evidence.get('validatedBy') if evidence else None,result['validatedAt'],expected=bool(receipts) or status=='TRUSTED'),
        'truster':actor_step(raw.get('trustedBy'),raw.get('trustedAt'),expected=status=='TRUSTED' or 'trustedBy' in raw)}
    if result['revoked']: result['trustStatus']='REVOKED'
    elif status=='TRUSTED': result['trustStatus']='NOT_EVALUATED'
    if not verify: return result
    try:
        if result['status']=='INVALID' or not all(result[k] is not None for k in ('source','sourceExtractedAt','recordCount','schemaVersion')):
            raise DomainConflict('CATALOG_STORED_EVIDENCE_INVALID')
        verified=await provenance.verified_lookup_context(db,raw,evidence or {},session,authorization=False)
        result['integrityStatus']='VERIFIED'
        if status=='TRUSTED':
            actors=[step['staffId'] for step in result['actors'].values()]
            if (not evidence or len(receipts)!=1 or raw.get('validationEvidenceId')!=evidence.get('_id')
                    or any(step['state']!='COMPLETED' for step in result['actors'].values()) or len(set(actors))!=3
                    or evidence.get('validationMethod')!='SERVER_CONTROLLED_MEDICLOUD_EXTRACTION'
                    or evidence.get('manifestDigest')!=verified['provenance']['manifestDigest']
                    or evidence.get('signingKeyFingerprint')!=verified['provenance']['signingKeyFingerprint']
                    or any(raw.get(k)!=evidence.get(k) for k in ('source','sourceExtractedAt','contentHash','recordCount','schemaVersion'))):
                raise DomainConflict('CATALOG_TRUST_EVIDENCE_REQUIRED')
            result['trustStatus']='REVOKED' if result['revoked'] else 'TRUSTED'
            result['usableForExistingResolution']=not result['revoked']
            result['usableForNewAuthorization']=not result['revoked'] and result['freshness']['state']=='FRESH'
    except provenance.CatalogConfigurationError:
        result.update(integrityStatus='UNAVAILABLE',trustStatus='REVOKED' if result['revoked'] else 'UNAVAILABLE')
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
        result.update(integrityStatus='INVALID',trustStatus='REVOKED' if result['revoked'] else 'INVALID',
                      usableForNewAuthorization=False,usableForExistingResolution=False)
    return result


async def proposal_actors(db,row,decision,activation,session,*,active_head=None):
    receipt=await db.pa_mapping_review_imports.find_one({'_id':row.get('exportId')},session=session)
    imported=bool(receipt and receipt.get('status')=='PROPOSED_ONLY'
        and receipt.get('exportId')==row.get('exportId') and receipt.get('importedBy')==row.get('createdBy'))
    importer=actor_step(receipt.get('importedBy') if imported else None,
                        receipt.get('importedAt') if imported else None,expected=True)
    approved=decision and decision.get('status')=='APPROVED'
    decided=bool(decision and decision.get('proposalId')==row['_id'] and decision.get('mappingId')==row['mappingId']
        and decision.get('status') in ('APPROVED','REJECTED')
        and type(decision.get('lifecycleVersion')) is int and decision['lifecycleVersion']>0)
    revision=await db.pa_mapping_versions.find_one({'mappingId':row['mappingId'],
        'version':decision.get('lifecycleVersion')},session=session) if decided else None
    if (not revision or revision.get('proposalId')!=row['_id'] or revision.get('createdBy')!=decision.get('decidedBy')
            or revision.get('status')!=('VALIDATED' if approved else 'REJECTED')
            or any(revision.get(k)!=row.get(k) for k in ('sourceNamespaceId','sourceIdentityType','sourceIdentityKey','sourceCode','sourceDescription','proposedMedicloudCode'))
            or revision.get('createdAt')!=decision.get('decidedAt')
            or (approved and (not snapshot_id(decision.get('snapshotId'))
                or revision.get('snapshotId')!=decision.get('snapshotId')
                or revision.get('medicloudCode')!=decision.get('medicloudCode')
                or revision.get('medicloudCode')!=row.get('proposedMedicloudCode')
                or revision.get('medicloudDescription')!=decision.get('medicloudDescription')
                or not isinstance(decision.get('medicloudDescription'),str)
                or not decision['medicloudDescription'].strip()
                or decision['medicloudDescription'].strip().upper()==row.get('proposedMedicloudCode','').upper()))):
        decided=False
    decision_view=actor_step(decision.get('decidedBy') if decided else None,
        decision.get('decidedAt') if decided else None,expected=bool(decision))
    decision_view['outcome']=decision['status'] if decided else None
    approver=actor_step(decision.get('decidedBy') if approved and decided else None,
        decision.get('decidedAt') if approved and decided else None,expected=bool(approved))
    active=await db.pa_mapping_versions.find_one({'mappingId':row['mappingId'],
        'version':activation['lifecycleVersion']},session=session) if (activation
            and type(activation.get('lifecycleVersion')) is int and activation['lifecycleVersion']>0) else None
    activated=bool(activation and active and approved and decided and activation.get('proposalId')==row['_id']
        and activation.get('mappingId')==row['mappingId'] and activation.get('status')=='ACTIVATED'
        and active.get('status')=='ACTIVE' and active.get('proposalId')==row['_id']
        and active.get('activatedBy')==activation.get('activatedBy') and active.get('createdBy')==activation.get('activatedBy')
        and active.get('createdAt')==activation.get('activatedAt')
        and activation.get('snapshotId')==decision.get('snapshotId')
        and all(active.get(k)==revision.get(k) for k in ('snapshotId','medicloudCode','medicloudDescription',
            'sourceNamespaceId','sourceIdentityType','sourceIdentityKey','sourceCode','sourceDescription','proposedMedicloudCode'))
        and isinstance(active.get('review'),dict)
        and active['review'].get('reviewedBy')==decision.get('decidedBy')
        and active['review'].get('reviewedAt')==decision.get('decidedAt')
        and active['review'].get('evidenceReference')==row['_id'])
    activator=actor_step(activation.get('activatedBy') if activated else None,
        activation.get('activatedAt') if activated else None,expected=bool(activation))
    steps={'importer':importer,'approver':approver,'activator':activator}
    completed=[step['staffId'] for step in steps.values() if step['state']=='COMPLETED']
    invalid=any(step['state']=='INVALID' for step in steps.values()) or len(completed)!=len(set(completed))
    if decision and decision_view['state']=='INVALID': invalid=True
    if not decision:
        missing_decision=await db.pa_mapping_versions.find_one({'mappingId':row['mappingId'],
            'proposalId':row['_id'],'status':'VALIDATED'},session=session)
        if missing_decision: invalid=True
    if not activation:
        missing_activation=await db.pa_mapping_versions.find_one({'mappingId':row['mappingId'],
            'proposalId':row['_id'],'status':'ACTIVE'},session=session)
        if missing_activation: invalid=True
    if active_head and active_head.get('proposalId')==row['_id']:
        if not activation or active_head.get('version')!=activation.get('lifecycleVersion'):
            invalid=True
    return {'actors':steps,'decision':decision_view,
        'threePersonStatus':'INVALID' if invalid else 'VALID' if len(completed)==3 else 'PENDING'}
