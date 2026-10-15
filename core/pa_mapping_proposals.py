"""Explicit proposal decisions and activation; no transmission capability."""
from copy import deepcopy
from uuid import uuid4
from core import pa_mapping as mappings, pa_mapping_review as transfer
from core.pa_preparation_store import DomainConflict, transaction, event, now_utc
from models.pa_preparation import MappingIdentity, MappingProposal

IDENTITY = transfer.IDENTITY


def action_digest(actor, action):
    return transfer.digest({'actorId':actor.actorId, **action.model_dump(mode='json')})


async def proposal(db, proposal_id, session=None):
    row=await db.pa_mapping_versions.find_one({'_id':proposal_id},session=session)
    if not row or row.get('status')!='PROPOSED' or row.get('provenance')!='CURSOR_PROPOSAL':
        raise DomainConflict('PROPOSAL_NOT_FOUND')
    return row


async def head_for(db, row, session):
    head=await db.pa_mapping_heads.find_one({'_id':row['mappingId']},session=session)
    if not head or any(head.get(k)!=row.get(k) for k in IDENTITY):
        raise DomainConflict('MAPPING_IDENTITY_CONFLICT')
    return head


def staff_identity(value):
    if (not isinstance(value,str) or not value or value!=value.strip() or len(value)>200
            or any(ord(c)<32 or ord(c)==127 for c in value)):
        raise DomainConflict('PROPOSAL_ACTOR_EVIDENCE_INVALID')
    return value


async def import_receipt(db,row,session):
    receipt=await db.pa_mapping_review_imports.find_one({'_id':row['exportId']},session=session)
    importer=staff_identity(row.get('createdBy'))
    if (not receipt or receipt.get('status')!='PROPOSED_ONLY'
            or receipt.get('exportId')!=row['exportId']
            or staff_identity(receipt.get('importedBy'))!=importer):
        raise DomainConflict('PROPOSAL_PROVENANCE_INVALID')
    return receipt


async def immutable_source(db,row,session):
    # Stale rejection needs original integrity, never current source eligibility.
    MappingProposal.model_validate({k:row[k] for k in MappingProposal.model_fields if k!='identity'} |
        {'identity':MappingIdentity(**{k:row[k] for k in IDENTITY})})
    if (row['sourceIdentityType']=='CODE' and mappings.normalized_code(row['sourceCode'])!=row['sourceIdentityKey']
            or row['sourceIdentityType']=='DESCRIPTION' and row['sourceDescription'].strip().casefold()!=row['sourceIdentityKey']):
        raise DomainConflict('SOURCE_IDENTITY_CHANGED')
    if mappings.normalized_code(row['proposedMedicloudCode'])=='PRE11':
        raise DomainConflict('RESERVED_PROCEDURE_FORBIDDEN')
    await import_receipt(db,row,session)
    manifest=await db.pa_mapping_review_exports.find_one({'_id':row['exportId']},session=session)
    if (not manifest or manifest.get('status')!='IMPORTED'
            or transfer.manifest_digest(manifest)!=manifest['manifestDigest']
            or transfer.digest(manifest['rows'])!=manifest['protectedDigest']):
        raise DomainConflict('PROPOSAL_PROVENANCE_INVALID')
    bindings=[b for b in manifest['bindings'] if b['rowId'] in row['exportRowIds']]
    if not bindings or len(bindings)!=len(set(row['exportRowIds'])):
        raise DomainConflict('PROPOSAL_PROVENANCE_INVALID')
    for binding in bindings:
        if (binding['identity']!={k:row[k] for k in IDENTITY}
                or binding['sourceCode']!=row['sourceCode'] or binding['sourceDescription']!=row['sourceDescription']):
            raise DomainConflict('SOURCE_IDENTITY_CHANGED')
    return manifest,bindings


async def source_scope(db, row, head, session, latest_version):
    manifest,bindings=await immutable_source(db,row,session)
    # Rejection of an older proposal appends history but must not supersede a
    # different pending proposal/approval. Only actionable lifecycle revisions count.
    latest=0
    for status in ('PROPOSED','VALIDATED','ACTIVE','RETIRED'):
        rows=await db.pa_mapping_versions.find({'mappingId':row['mappingId'],'status':status},session=session).sort('version',-1).limit(1).to_list(1)
        if rows: latest=max(latest,rows[0]['version'])
    if latest!=latest_version:
        raise DomainConflict('PROPOSAL_SUPERSEDED')
    await transfer.batch_guard(db,session,manifest['batchId'])
    for binding in bindings:
        original=binding['mappingBinding']
        if (original['version'] is not None and original['version']!=head['version']
                or original['activeVersion']!=head.get('activeVersion')):
            raise DomainConflict('MAPPING_HEAD_CHANGED')
        stored=await db.pa_preparation_lines.find_one({'_id':binding['lineId'],'batchId':manifest['batchId']},session=session)
        if not stored or stored.get('revision')!=binding['revision'] or stored.get('inputFingerprint')!=binding['fingerprint']:
            raise DomainConflict('PREPARATION_STALE')
        live=await transfer.live_line(db,stored,session)
        if not transfer.actionable(live): raise DomainConflict('SOURCE_NO_LONGER_REVIEWABLE')


async def catalog_entry(db,row,session,snapshot_id=None):
    if snapshot_id is None:
        snapshots=await db.pa_procedure_catalog_snapshots.find({'status':'TRUSTED'},session=session).sort('sourceExtractedAt',-1).limit(1).to_list(1)
        if not snapshots: raise DomainConflict('TRUSTED_PROCEDURE_SNAPSHOT_REQUIRED')
        snapshot_id=snapshots[0]['_id']
    entry=await mappings.canonical_procedure(db,snapshot_id,row['proposedMedicloudCode'],session)
    if entry['procedureCode']!=row['proposedMedicloudCode']:
        raise DomainConflict('CANONICAL_CODE_MISMATCH')
    suggested=row.get('proposedMedicloudDescription')
    if suggested and suggested!=entry['procedureDescription']:
        raise DomainConflict('CANONICAL_DESCRIPTION_MISMATCH')
    return snapshot_id,entry


def expect(head,action):
    if head['version']!=action.expectedHeadVersion or head.get('activeVersion')!=action.expectedActiveVersion:
        raise DomainConflict('MAPPING_HEAD_CHANGED')


async def guard_head(db,head,session):
    result=await db.pa_mapping_heads.update_one({'_id':head['_id'],'version':head['version'],
        'activeVersion':head.get('activeVersion')},{'$inc':{'proposalReviewGuard':1}},session=session)
    if result.matched_count!=1: raise DomainConflict('MAPPING_HEAD_CHANGED')


def decision_view(receipt):
    return {k:receipt.get(k) for k in ('proposalId','mappingId','status','lifecycleVersion','decidedAt','activatedAt','snapshotId')}


async def decide(db,actor,proposal_id,action,*,approve):
    mappings.require_permission(actor,'canApproveMappings' if approve else 'canReviewMappings')
    staff_identity(actor.actorId)
    async with transaction(db) as session:
        row=await proposal(db,proposal_id,session)
        prior=await db.pa_mapping_proposal_decisions.find_one({'_id':proposal_id},session=session)
        fingerprint=action_digest(actor,action)
        if prior:
            if prior['actionDigest']==fingerprint and prior['status']==('APPROVED' if approve else 'REJECTED'):
                return decision_view(prior)
            raise DomainConflict('PROPOSAL_ALREADY_DECIDED')
        await import_receipt(db,row,session)
        if approve and actor.actorId==row['createdBy']:
            raise PermissionError('Independent proposal approver required')
        head=await head_for(db,row,session)
        if approve:
            expect(head,action)
            await source_scope(db,row,head,session,row['version'])
            await guard_head(db,head,session)
        else:
            await immutable_source(db,row,session)
            if await db.pa_mapping_activations.find_one({'_id':proposal_id},session=session):
                raise DomainConflict('PROPOSAL_ALREADY_ACTIVATED')
        snapshot_id,entry=await catalog_entry(db,row,session) if approve else (None,None)
        version=await mappings.next_mapping_version(db,row['mappingId'],session)
        now=now_utc()
        revision={**deepcopy(row),'_id':str(uuid4()),'version':version,'status':'VALIDATED' if approve else 'REJECTED',
            'proposalId':proposal_id,'createdAt':now,'createdBy':actor.actorId}
        if approve:
            revision.update(snapshotId=snapshot_id,medicloudCode=entry['procedureCode'],medicloudDescription=entry['procedureDescription'])
        decision={'_id':proposal_id,'proposalId':proposal_id,'mappingId':row['mappingId'],
            'status':'APPROVED' if approve else 'REJECTED','lifecycleVersion':version,
            'decidedAt':now,'decidedBy':actor.actorId,'reason':action.reason,'actionDigest':fingerprint,
            'expectedHeadVersion':head['version'],'expectedActiveVersion':head.get('activeVersion'),
            'snapshotId':snapshot_id,'medicloudCode':entry['procedureCode'] if entry else None,
            'medicloudDescription':entry['procedureDescription'] if entry else None}
        await db.pa_mapping_versions.insert_one(revision,session=session)
        await db.pa_mapping_proposal_decisions.insert_one(decision,session=session)
        await event(db,session,actor,'mapping_proposal_approved' if approve else 'mapping_proposal_rejected',str(action.requestId),
            mappingId=row['mappingId'],previousVersion=row['version'],newVersion=version,reason=action.reason,snapshotId=snapshot_id)
    return decision_view(decision)


async def activate(db,actor,proposal_id,action):
    mappings.require_permission(actor,'canActivateMappings')
    staff_identity(actor.actorId)
    async with transaction(db) as session:
        row=await proposal(db,proposal_id,session)
        fingerprint=action_digest(actor,action)
        await import_receipt(db,row,session)
        decision=await db.pa_mapping_proposal_decisions.find_one({'_id':proposal_id},session=session)
        if not decision or decision['status']!='APPROVED': raise DomainConflict('APPROVED_PROPOSAL_REQUIRED')
        approver=staff_identity(decision.get('decidedBy'))
        if (decision.get('proposalId')!=proposal_id or decision.get('mappingId')!=row['mappingId']
                or approver==row['createdBy']):
            raise DomainConflict('APPROVAL_EVIDENCE_MISMATCH')
        approved=await db.pa_mapping_versions.find_one({'mappingId':row['mappingId'],'version':decision['lifecycleVersion']},session=session)
        if (not approved or approved.get('status')!='VALIDATED' or approved.get('proposalId')!=proposal_id
                or staff_identity(approved.get('createdBy'))!=approver):
            raise DomainConflict('APPROVAL_EVIDENCE_MISMATCH')
        if actor.actorId in (row['createdBy'],approver):
            raise PermissionError('Three distinct proposal staff identities required')
        existing=await db.pa_mapping_activations.find_one({'_id':proposal_id},session=session)
        if existing:
            if (existing['actionDigest']==fingerprint
                    and staff_identity(existing.get('activatedBy'))==actor.actorId):
                return decision_view(existing)
            raise DomainConflict('PROPOSAL_ALREADY_ACTIVATED')
        head=await head_for(db,row,session); expect(head,action)
        if head['version']!=decision['expectedHeadVersion'] or head.get('activeVersion')!=decision['expectedActiveVersion']:
            raise DomainConflict('APPROVAL_HEAD_CHANGED')
        if head.get('activeVersion') is not None and not action.replaceActive:
            raise DomainConflict('EXPLICIT_ACTIVE_REPLACEMENT_REQUIRED')
        await source_scope(db,row,head,session,decision['lifecycleVersion'])
        _,entry=await catalog_entry(db,row,session,decision['snapshotId'])
        approved=await db.pa_mapping_versions.find_one({'mappingId':row['mappingId'],'version':decision['lifecycleVersion']},session=session)
        if (not approved or approved.get('status')!='VALIDATED' or approved.get('proposalId')!=proposal_id
                or approved.get('createdBy')!=decision['decidedBy']
                or any(approved.get(k)!=row.get(k) for k in (*IDENTITY,'sourceCode','sourceDescription','proposedMedicloudCode'))
                or approved.get('snapshotId')!=decision['snapshotId']
                or approved.get('medicloudCode')!=entry['procedureCode'] or approved.get('medicloudDescription')!=entry['procedureDescription']
                or decision.get('medicloudCode')!=entry['procedureCode'] or decision.get('medicloudDescription')!=entry['procedureDescription']):
            raise DomainConflict('APPROVAL_EVIDENCE_MISMATCH')
        version=await mappings.next_mapping_version(db,row['mappingId'],session)
        now=now_utc()
        active={**deepcopy(approved),'_id':str(uuid4()),'version':version,'status':'ACTIVE','createdAt':now,'createdBy':actor.actorId,
            'review':{'reviewedBy':decision['decidedBy'],'reviewedAt':decision['decidedAt'],
                      'evidenceReference':proposal_id,'reason':decision['reason']},'activatedBy':actor.actorId}
        result=await db.pa_mapping_heads.update_one({'_id':head['_id'],'version':head['version'],'activeVersion':head.get('activeVersion')},
            {'$set':{'version':version,'activeVersion':version}},session=session)
        if result.matched_count!=1: raise DomainConflict('MAPPING_HEAD_CHANGED')
        await db.pa_mapping_versions.insert_one(active,session=session)
        # Only preparation metadata becomes stale. No order/price/PA ledger writes.
        await db.pa_preparation_lines.update_many({**{k:row[k] for k in IDENTITY},'isCurrent':True},
            {'$set':{'isCurrent':False,'staleReason':'MAPPING_CHANGED'}},session=session)
        receipt={'_id':proposal_id,'proposalId':proposal_id,'mappingId':row['mappingId'],'status':'ACTIVATED',
            'lifecycleVersion':version,'snapshotId':decision['snapshotId'],'activatedAt':now,
            'activatedBy':actor.actorId,'actionDigest':fingerprint}
        await db.pa_mapping_activations.insert_one(receipt,session=session)
        await event(db,session,actor,'mapping_proposal_activated',str(action.requestId),mappingId=row['mappingId'],
            previousVersion=head.get('activeVersion'),newVersion=version,reason=action.reason,snapshotId=decision['snapshotId'])
    return decision_view(receipt)


async def detail(db,actor,proposal_id):
    mappings.require_permission(actor,'canReviewMappings')
    async with transaction(db) as session:
        return await _detail(db,proposal_id,session)


async def _detail(db,proposal_id,session):
    row=await proposal(db,proposal_id,session)
    decision=await db.pa_mapping_proposal_decisions.find_one({'_id':proposal_id},session=session)
    activation=await db.pa_mapping_activations.find_one({'_id':proposal_id},session=session)
    head=await head_for(db,row,session)
    state='PROPOSED' if not decision else decision['status']
    if activation: state='ACTIVE' if head.get('activeVersion')==activation['lifecycleVersion'] else 'REPLACED'
    verification='NOT_VERIFIED'; code=description=None
    try:
        _,entry=await catalog_entry(db,row,session)
        verification='VERIFIED'; code=entry['procedureCode']; description=entry['procedureDescription']
    except DomainConflict: verification='UNAVAILABLE_OR_INVALID'
    return {**{k:row.get(k) for k in ('mappingId','version','sourceNamespaceId','sourceIdentityType','sourceIdentityKey',
        'sourceCode','sourceDescription','proposedMedicloudCode','proposedMedicloudDescription','mappingNotes','provenance')},
        'proposalId':proposal_id,'workflowStatus':state,'expectedHeadVersion':head['version'],
        'expectedActiveVersion':head.get('activeVersion'),'catalogVerification':verification,
        'canonicalCode':code,'canonicalDescription':description,'providerTariffEligibility':'NOT_VERIFIED',
        'priceApplicability':'NOT_VERIFIED'}


async def list_proposals(db,actor,limit,after=None):
    mappings.require_permission(actor,'canReviewMappings')
    if type(limit) is not int or not 1<=limit<=50: raise ValueError('Invalid bounded page')
    query={'status':'PROPOSED','provenance':'CURSOR_PROPOSAL'}
    if after: query['_id']={'$gt':after}
    async with transaction(db) as session:
        rows=await db.pa_mapping_versions.find(query,session=session).sort('_id',1).limit(limit+1).to_list(limit+1)
        return {'items':[await _detail(db,row['_id'],session) for row in rows[:limit]],
            'nextCursor':rows[limit-1]['_id'] if len(rows)>limit else None}
