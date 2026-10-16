"""Phase 4A minimized read contracts; transaction fakes and blocked sockets."""
import asyncio
from copy import deepcopy
from datetime import timedelta, datetime, timezone
from uuid import uuid4
from unittest.mock import AsyncMock
import pytest
from core import pa_catalog, pa_catalog_provenance as provenance, pa_mapping_proposals as proposals
from core.pa_read_views import catalog_metadata
from core.pa_preparation_store import DomainConflict
from core.pa_staff import PA_PERMISSIONS
from core.security import encode_session
from models.pa_preparation import Actor
from api import auth, pa_catalog as catalog_api, pa_mapping_proposals as proposal_api
from test_pa_preparation import flow, catalog as fixture_catalog
from test_pa_preparation_api import team
from test_pa_mapping_proposals import imported, action, APPROVER, ACTIVATOR, REVIEWER

READ=Actor(actorId='catalog-reader',canReadCatalog=True)


def routes(team,monkeypatch):
    team.app.include_router(auth.router,prefix='/api/auth')
    team.app.include_router(catalog_api.router,prefix='/api')
    team.app.include_router(proposal_api.router,prefix='/api')
    monkeypatch.setattr(auth,'get_db',lambda:team.flow.db)
    team.client.cookies.set('staff_session',encode_session({'role':'staff','userId':str(team.staff['_id']),
        'name':'Existing Staff','email':'staff@example.test','permissions':list(PA_PERMISSIONS)}))


def test_identity_current_allowlisted_permissions_and_backward_compatibility(team,monkeypatch):
    routes(team,monkeypatch)
    team.staff['permissions']=['PA_MAPPING_REVIEW','PA_CATALOG_READ','SUPERUSER','PA_MAPPING_REVIEW',{},123]
    result=team.client.get('/api/auth/staff/me')
    assert result.status_code==200 and result.headers['cache-control']=='no-store'
    assert result.json()=={'userId':str(team.staff['_id']),'name':'Existing Staff','email':'staff@example.test',
        'permissions':['PA_CATALOG_READ','PA_MAPPING_REVIEW']}
    assert 'set-cookie' not in result.headers
    team.staff['permissions']=[]
    assert team.client.get('/api/auth/staff/me').json()['permissions']==[]
    assert team.client.get('/api/pa-catalog/snapshots').status_code==403


@pytest.mark.parametrize('value',[None,{},'PA_CATALOG_READ',123])
def test_bad_permission_lists_grant_nothing(team,monkeypatch,value):
    routes(team,monkeypatch); team.staff['permissions']=value
    assert team.client.get('/api/auth/staff/me').json()['permissions']==[]
    assert team.client.get('/api/pa-catalog/snapshots').status_code==403


@pytest.mark.parametrize('flag,value',[('disabled',True),('disabled',None),('disabled','false'),
    ('isActive',False),('isActive',None),('isActive',1)])
def test_disabled_or_invalid_staff_flags_deny_permissions_and_reads(team,monkeypatch,flag,value):
    routes(team,monkeypatch); team.staff['permissions']=list(PA_PERMISSIONS); team.staff[flag]=value
    for path in ('/api/auth/staff/me','/api/pa-catalog/snapshots','/api/pa-mapping-proposals'):
        response=team.client.get(path)
        assert response.status_code==401 and 'permissions' not in response.json()


def test_missing_staff_and_unauthenticated_identity(team,monkeypatch):
    routes(team,monkeypatch); team.flow.db.staff_users.rows.clear()
    assert team.client.get('/api/auth/staff/me').status_code==401
    team.client.cookies.clear()
    assert team.client.get('/api/auth/staff/me').status_code==401


def test_identity_storage_failure_is_sanitized(team,monkeypatch):
    routes(team,monkeypatch)
    from pymongo.errors import OperationFailure
    monkeypatch.setattr(team.flow.db.staff_users,'find_one',AsyncMock(side_effect=OperationFailure('SECRET SQL MONGO')))
    response=team.client.get('/api/auth/staff/me')
    assert response.status_code==503 and 'SECRET' not in response.text


@pytest.mark.parametrize('permission',[[],['PA_CATALOG_INGEST'],['PA_CATALOG_VALIDATE'],['PA_CATALOG_TRUST'],['PA_MAPPING_REVIEW']])
def test_catalog_read_permission_explicit_no_inheritance(team,monkeypatch,permission):
    routes(team,monkeypatch); team.staff['permissions']=permission
    assert team.client.get('/api/pa-catalog/snapshots').status_code==403
    assert team.client.get('/api/pa-catalog/snapshots/'+team.flow.snapshot['_id']).status_code==403


def test_catalog_read_api_minimization_and_no_write_permission(team,monkeypatch):
    routes(team,monkeypatch); team.staff['permissions']=['PA_CATALOG_READ']
    protected={name:deepcopy(col.rows) for name,col in team.flow.db.collections.items()}
    snapshots=team.client.get('/api/pa-catalog/snapshots').json()
    assert snapshots['integrityScope']=='METADATA_ONLY'
    assert snapshots['items'][0]['integrityStatus']=='NOT_EVALUATED'
    assert snapshots['items'][0]['usableForNewAuthorization'] is False
    result=team.client.get('/api/pa-catalog/snapshots/'+team.flow.snapshot['_id'])
    assert result.status_code==200 and result.headers['cache-control']=='no-store'
    data=result.json()
    assert data['trustStatus']=='TRUSTED' and data['usableForNewAuthorization'] is True
    assert data['usableForExistingResolution'] is True
    assert len({v['staffId'] for v in data['actors'].values()})==3
    for forbidden in ('signedProvenance','manifestDigest','contentHash','signingKeyFingerprint','validationEvidenceId','procedureCode','password'):
        assert forbidden not in result.text
    assert team.client.post('/api/pa-catalog/snapshots/'+team.flow.snapshot['_id']+'/trust',json={}).status_code==403
    assert team.client.get('/api/pa-catalog/snapshots/'+str(uuid4())).status_code==404
    assert team.client.get('/api/pa-catalog/snapshots?limit=51').status_code==422
    assert protected=={name:col.rows for name,col in team.flow.db.collections.items()}


@pytest.mark.asyncio
async def test_catalog_pagination_bounds_and_never_reads_catalog_entries_for_list(flow,monkeypatch):
    for _ in range(4):
        flow.db.pa_procedure_catalog_snapshots.rows.append({**deepcopy(flow.snapshot),'_id':str(uuid4())})
    def forbidden(*args,**kwargs): raise AssertionError('List must not read catalog contents')
    monkeypatch.setattr(flow.db.pa_procedure_catalog_entries,'find',forbidden)
    first=await pa_catalog.list_snapshots(flow.db,READ,2)
    second=await pa_catalog.list_snapshots(flow.db,READ,2,first['nextCursor'])
    assert len(first['items'])==len(second['items'])==2
    assert not ({r['snapshotId'] for r in first['items']} & {r['snapshotId'] for r in second['items']})


@pytest.mark.asyncio
@pytest.mark.parametrize('age,state,new,existing',[(timedelta(hours=23,minutes=59),'FRESH',True,True),
    (timedelta(hours=24),'EXPIRED',False,True),(timedelta(hours=25),'EXPIRED',False,True)])
async def test_catalog_freshness_separate_from_existing_resolution(flow,monkeypatch,age,state,new,existing):
    monkeypatch.setattr(provenance,'now_utc',lambda:flow.snapshot['sourceExtractedAt']+age)
    result=await pa_catalog.read_snapshot(flow.db,READ,flow.snapshot['_id'])
    assert result['freshness']['state']==state
    assert result['usableForNewAuthorization'] is new
    assert result['usableForExistingResolution'] is existing
    assert result['integrityStatus']=='VERIFIED' and result['trustStatus']=='TRUSTED'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['entry','signature','revoked','receiptRevoked','receiptMissing','receiptDuplicate','sameActor','untrusted','unsigned','future','authority'])
async def test_bad_catalog_evidence_is_visible_but_never_usable(flow,monkeypatch,bad):
    raw=flow.db.pa_procedure_catalog_snapshots.rows[0]
    if bad=='entry': flow.db.pa_procedure_catalog_entries.rows[0]['procedureDescription']='Tampered'
    elif bad=='signature': raw['signedProvenance']['signature']='Invalid'
    elif bad=='revoked': raw['revokedAt']=datetime.now(timezone.utc)
    elif bad=='receiptRevoked': flow.db.pa_catalog_validation_evidence.rows[0]['revokedAt']=datetime.now(timezone.utc)
    elif bad=='receiptMissing': flow.db.pa_catalog_validation_evidence.rows.clear()
    elif bad=='receiptDuplicate': flow.db.pa_catalog_validation_evidence.rows.append({**deepcopy(flow.db.pa_catalog_validation_evidence.rows[0]),'_id':'duplicate'})
    elif bad=='sameActor': raw['trustedBy']=raw['importedBy']
    elif bad=='untrusted': raw['status']='VALIDATED'
    elif bad=='unsigned': raw.pop('signedProvenance')
    elif bad=='future': monkeypatch.setattr(provenance,'now_utc',lambda:raw['sourceExtractedAt']-timedelta(seconds=1))
    else: monkeypatch.delenv('PA_CATALOG_PUBLIC_KEY_PATH')
    result=await pa_catalog.read_snapshot(flow.db,READ,raw['_id'])
    assert result['usableForNewAuthorization'] is False and result['usableForExistingResolution'] is False
    if bad in ('revoked','receiptRevoked'): assert result['trustStatus']=='REVOKED'
    if bad=='authority': assert result['integrityStatus']=='UNAVAILABLE'


@pytest.mark.asyncio
@pytest.mark.parametrize('key,bad',[('sourceExtractedAt',{}),('sourceExtractedAt',datetime.max.replace(tzinfo=timezone.utc)),
    ('status',{'private':'hidden'}),('recordCount',True),('schemaVersion','secret'),('importedBy',{'secret':'private'}),
    ('trustedAt',None),('trustedAt',datetime.now(timezone.utc)+timedelta(days=1))])
async def test_malformed_legacy_catalog_safe_projection(flow,key,bad):
    raw=flow.db.pa_procedure_catalog_snapshots.rows[0]; raw[key]=bad
    result=await pa_catalog.read_snapshot(flow.db,READ,raw['_id'])
    assert result['usableForNewAuthorization'] is False
    assert result['usableForExistingResolution'] is False
    assert 'private' not in str(result) and 'hidden' not in str(result) and 'secret' not in str(result)


@pytest.mark.asyncio
async def test_proposal_actor_steps_and_approval_bound_catalog_never_substituted(flow,monkeypatch):
    row=await imported(flow)
    original=deepcopy(flow.db.pa_mapping_versions.rows)
    view=await proposals.detail(flow.db,REVIEWER,row['_id'])
    assert view['actors']['importer']['state']=='COMPLETED'
    assert view['actors']['approver']['state']==view['actors']['activator']['state']=='PENDING'
    assert view['catalog']['binding']=='LATEST_TRUSTED'
    await proposals.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    bound=flow.db.pa_mapping_proposal_decisions.rows[0]['snapshotId']
    # A newer genuine catalog has a different canonical description. Approval stays bound.
    newer=await fixture_catalog(flow.db,[{'procedureCode':'DRG-CANON','procedureDescription':'Different canonical description'}],
        sourceExtractedAt=datetime.now(timezone.utc)-timedelta(minutes=1))
    assert newer['_id']!=bound
    view=await proposals.detail(flow.db,REVIEWER,row['_id'])
    assert view['catalog']['snapshotId']==bound and view['catalog']['binding']=='APPROVAL_BOUND'
    assert view['canonicalDescription']=='Canonical medication A'
    assert view['actors']['approver']['staffId']==APPROVER.actorId and view['actors']['activator']['state']=='PENDING'
    await proposals.activate(flow.db,ACTIVATOR,row['_id'],action(flow,activate=True))
    protected={name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    monkeypatch.setattr(provenance,'now_utc',lambda:flow.snapshot['sourceExtractedAt']+timedelta(hours=25))
    view=await proposals.detail(flow.db,REVIEWER,row['_id'])
    assert view['workflowStatus']=='ACTIVE' and view['threePersonStatus']=='VALID'
    assert [view['actors'][k]['staffId'] for k in ('importer','approver','activator')]==[row['createdBy'],APPROVER.actorId,ACTIVATOR.actorId]
    assert view['catalog']['freshness']['state']=='EXPIRED'
    assert view['catalog']['usableForExistingResolution'] is True
    assert view['catalog']['usableForNewAuthorization'] is False
    assert view['catalogVerification']=='VERIFIED_FOR_EXISTING_ACTIVE'
    assert protected=={name:col.rows for name,col in flow.db.collections.items()}
    assert flow.db.pa_mapping_versions.rows[:len(original)]==original
    assert 'memberSnapshot' not in str(view) and 'actionDigest' not in str(view)


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['missing','revoked','tampered','noBoundId'])
async def test_approved_catalog_metadata_fails_closed_without_latest_fallback(flow,bad):
    row=await imported(flow)
    await proposals.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    bound=flow.db.pa_mapping_proposal_decisions.rows[0]['snapshotId']
    await fixture_catalog(flow.db,sourceExtractedAt=datetime.now(timezone.utc)-timedelta(minutes=1))
    if bad=='missing': flow.db.pa_procedure_catalog_snapshots.rows=[r for r in flow.db.pa_procedure_catalog_snapshots.rows if r['_id']!=bound]
    elif bad=='revoked': flow.db.pa_procedure_catalog_snapshots.rows[0]['revokedAt']=datetime.now(timezone.utc)
    elif bad=='tampered': flow.db.pa_procedure_catalog_entries.rows[0]['procedureDescription']='tampered'
    else: flow.db.pa_mapping_proposal_decisions.rows[0]['snapshotId']=None
    view=await proposals.detail(flow.db,REVIEWER,row['_id'])
    assert view['catalog']['binding']=='APPROVAL_BOUND'
    assert view['catalog']['snapshotId']==(None if bad=='noBoundId' else bound)
    assert view['catalog']['usableForNewAuthorization'] is False
    assert view['catalogVerification']=='UNAVAILABLE_OR_INVALID'


@pytest.mark.asyncio
async def test_rejection_is_a_completed_decision_not_a_completed_approval(flow):
    row=await imported(flow)
    await proposals.decide(flow.db,REVIEWER,row['_id'],action(flow),approve=False)
    view=await proposals.detail(flow.db,REVIEWER,row['_id'])
    assert view['decision']['state']=='COMPLETED' and view['decision']['outcome']=='REJECTED'
    assert view['decision']['staffId']==REVIEWER.actorId
    assert view['actors']['approver']['state']=='PENDING'
    assert view['actors']['activator']['state']=='PENDING'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['importer','approver','activator'])
async def test_malformed_actor_receipts_do_not_leak_and_are_not_completed(flow,bad):
    row=await imported(flow)
    await proposals.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    await proposals.activate(flow.db,ACTIVATOR,row['_id'],action(flow,activate=True))
    if bad=='importer': flow.db.pa_mapping_review_imports.rows[0]['importedBy']={'secret':'private'}
    elif bad=='approver': flow.db.pa_mapping_proposal_decisions.rows[0]['decidedBy']={'secret':'private'}
    else: flow.db.pa_mapping_activations.rows[0]['activatedBy']={'secret':'private'}
    view=await proposals.detail(flow.db,REVIEWER,row['_id'])
    assert view['actors'][bad]['state']=='INVALID' and view['actors'][bad]['staffId'] is None
    assert view['threePersonStatus']=='INVALID' and 'private' not in str(view)


@pytest.mark.asyncio
async def test_catalog_unauthorized_domain_actor_and_no_live_authority_fallback(flow):
    with pytest.raises(PermissionError):
        await pa_catalog.read_snapshot(flow.db,Actor(actorId='staff'),flow.snapshot['_id'])
    with pytest.raises(PermissionError):
        await pa_catalog.list_snapshots(flow.db,Actor(actorId='staff',canTrustCatalog=True),10)


@pytest.mark.asyncio
async def test_proposal_list_shares_verified_catalog_scope_with_reads(flow,monkeypatch):
    row=await imported(flow)
    # Safe display fixtures retained independently of mutation authorization.
    for _ in range(3): flow.db.pa_mapping_versions.rows.append({**deepcopy(row),'_id':str(uuid4())})
    verify=AsyncMock(wraps=provenance.verify_snapshot); monkeypatch.setattr(provenance,'verify_snapshot',verify)
    before={name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    result=await proposals.list_proposals(flow.db,REVIEWER,4)
    assert len(result['items'])==4 and verify.await_count==0
    assert before=={name:col.rows for name,col in flow.db.collections.items()}


@pytest.mark.asyncio
async def test_missing_or_malformed_catalog_ids_do_not_leak_or_crash_list(flow):
    flow.db.pa_procedure_catalog_snapshots.rows.append({'_id':'zz-private-legacy-id','status':{'private':'hidden'}})
    result=await pa_catalog.list_snapshots(flow.db,READ,50)
    assert result['skippedInvalidRecords']==1 and len(result['items'])==1
    assert 'private' not in str(result)


@pytest.mark.asyncio
async def test_missing_approval_bound_id_never_falls_back_even_if_receipt_status_malformed(flow):
    row=await imported(flow)
    await proposals.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    bound=flow.db.pa_mapping_proposal_decisions.rows[0]['snapshotId']
    flow.db.pa_mapping_proposal_decisions.rows[0]['status']='INVALID'
    view=await proposals.detail(flow.db,REVIEWER,row['_id'])
    assert view['catalog']['binding']=='APPROVAL_BOUND' and view['catalog']['snapshotId']==bound
    assert view['decision']['state']=='INVALID'


@pytest.mark.asyncio
async def test_fifty_distinct_approval_bound_catalogs_list_is_metadata_only(flow,monkeypatch):
    row=await imported(flow)
    await proposals.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    decision=deepcopy(flow.db.pa_mapping_proposal_decisions.rows[0])
    revision=deepcopy(flow.db.pa_mapping_versions.rows[-1])
    head=deepcopy(flow.db.pa_mapping_heads.rows[0])
    catalog_ids={flow.snapshot['_id']}
    for i in range(49):
        mapping_id,proposal_id,catalog_id=str(uuid4()),str(uuid4()),str(uuid4())
        identity={'sourceNamespaceId':f'supplier-{i+100}'}
        flow.db.pa_procedure_catalog_snapshots.rows.append({**deepcopy(flow.snapshot),'_id':catalog_id})
        flow.db.pa_mapping_heads.rows.append({**head,'_id':mapping_id,**identity})
        flow.db.pa_mapping_versions.rows.append({**deepcopy(row),'_id':proposal_id,'mappingId':mapping_id,**identity})
        flow.db.pa_mapping_versions.rows.append({**revision,'_id':str(uuid4()),'mappingId':mapping_id,
            'proposalId':proposal_id,'snapshotId':catalog_id,**identity})
        flow.db.pa_mapping_proposal_decisions.rows.append({**decision,'_id':proposal_id,'proposalId':proposal_id,
            'mappingId':mapping_id,'snapshotId':catalog_id})
        catalog_ids.add(catalog_id)
    verify=AsyncMock(side_effect=AssertionError('List must not verify catalogs'))
    monkeypatch.setattr(provenance,'verify_snapshot',verify)
    monkeypatch.setattr(provenance,'verified_lookup_context',verify)
    def no_entries(*args,**kwargs): raise AssertionError('List must not fetch procedure entries')
    monkeypatch.setattr(flow.db.pa_procedure_catalog_entries,'find',no_entries)
    monkeypatch.setattr(flow.db.pa_procedure_catalog_entries,'find_one',AsyncMock(side_effect=AssertionError('No entry lookup')))
    before={name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    page=await proposals.list_proposals(flow.db,REVIEWER,50)
    assert len(page['items'])==50 and page['nextCursor'] is None
    assert {r['catalog']['snapshotId'] for r in page['items']}==catalog_ids
    for item in page['items']:
        assert item['evidenceScope']==item['catalog']['verificationScope']=='METADATA_ONLY'
        assert item['catalog']['binding']=='APPROVAL_BOUND'
        assert item['catalog']['status']=='NOT_EVALUATED' and item['catalog']['recordedStatus']=='TRUSTED'
        assert item['catalogVerification']==item['catalog']['integrityStatus']=='NOT_EVALUATED'
        assert item['catalog']['trustStatus']=='NOT_EVALUATED'
        assert item['catalog']['usableForNewAuthorization'] is False
        assert item['catalog']['usableForExistingResolution'] is False
        assert item['workflowStatus']=='UNVERIFIED' and item['recordedWorkflowStatus']=='APPROVED'
        assert item['workflowVerification']=='NOT_EVALUATED'
        assert item['canonicalCode'] is None and item['canonicalDescription'] is None
    assert verify.await_count==0
    assert before=={name:col.rows for name,col in flow.db.collections.items()}


@pytest.mark.asyncio
@pytest.mark.parametrize('hours',[24,25])
async def test_expired_active_detail_verified_for_resolution_but_not_new_authorization(flow,monkeypatch,hours):
    row=await imported(flow)
    await proposals.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    await proposals.activate(flow.db,ACTIVATOR,row['_id'],action(flow,activate=True))
    monkeypatch.setattr(provenance,'now_utc',lambda:flow.snapshot['sourceExtractedAt']+timedelta(hours=hours))
    verify=AsyncMock(wraps=provenance.verify_snapshot); monkeypatch.setattr(provenance,'verify_snapshot',verify)
    view=await proposals.detail(flow.db,REVIEWER,row['_id'])
    assert view['workflowStatus']=='ACTIVE' and view['workflowVerification']=='VERIFIED'
    assert view['recordedWorkflowStatus']=='ACTIVE' and view['evidenceScope']=='FULL_VERIFICATION'
    assert view['catalogVerification']=='VERIFIED_FOR_EXISTING_ACTIVE'
    assert view['catalogVerificationPurpose']=='EXISTING_ACTIVE_RESOLUTION'
    assert view['canonicalCode']=='DRG-CANON' and view['canonicalDescription']=='Canonical medication A'
    assert view['catalog']['usableForNewAuthorization'] is False and view['catalog']['usableForExistingResolution'] is True
    assert verify.await_count==1
    # New lifecycle authorization still rejects evidence at or beyond 24 hours.
    with pytest.raises(DomainConflict,match='CATALOG_EXPIRED'):
        await proposals.catalog_entry(flow.db,row,None,flow.snapshot['_id'])


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['revoked','entry','signature','activeCode'])
async def test_expired_active_projection_keeps_integrity_checks(flow,monkeypatch,bad):
    row=await imported(flow)
    await proposals.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    await proposals.activate(flow.db,ACTIVATOR,row['_id'],action(flow,activate=True))
    monkeypatch.setattr(provenance,'now_utc',lambda:flow.snapshot['sourceExtractedAt']+timedelta(hours=25))
    if bad=='revoked': flow.db.pa_procedure_catalog_snapshots.rows[0]['revokedAt']=datetime.now(timezone.utc)
    elif bad=='entry': flow.db.pa_procedure_catalog_entries.rows[0]['procedureDescription']='Tampered'
    elif bad=='signature': flow.db.pa_procedure_catalog_snapshots.rows[0]['signedProvenance']['signature']='Invalid'
    else: flow.db.pa_mapping_versions.rows[-1]['medicloudCode']='ARBITRARY'
    view=await proposals.detail(flow.db,REVIEWER,row['_id'])
    assert view['workflowStatus']=='UNVERIFIED' and view['recordedWorkflowStatus']=='ACTIVE'
    assert view['workflowVerification']=='INVALID'
    assert view['catalogVerification']=='UNAVAILABLE_OR_INVALID' and view['canonicalCode'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('stage',['APPROVED','ACTIVE'])
@pytest.mark.parametrize('bad',['importMissing','actorMismatch','decisionMissing','decisionMalformed','decisionVersion',
    'decisionCode','approvalRevision','activationMissing','activationMalformed'])
async def test_workflow_presentation_never_qualifies_corrupt_evidence(flow,stage,bad):
    row=await imported(flow)
    await proposals.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    if stage=='ACTIVE': await proposals.activate(flow.db,ACTIVATOR,row['_id'],action(flow,activate=True))
    if bad=='importMissing': flow.db.pa_mapping_review_imports.rows.clear()
    elif bad=='actorMismatch': flow.db.pa_mapping_proposal_decisions.rows[0]['decidedBy']='unexpected-actor'
    elif bad=='decisionMissing': flow.db.pa_mapping_proposal_decisions.rows.clear()
    elif bad=='decisionMalformed': flow.db.pa_mapping_proposal_decisions.rows[0].pop('status')
    elif bad=='decisionVersion': flow.db.pa_mapping_proposal_decisions.rows[0]['lifecycleVersion']={'private':'never echo'}
    elif bad=='decisionCode': flow.db.pa_mapping_proposal_decisions.rows[0]['medicloudCode']='ARBITRARY'
    elif bad=='approvalRevision':
        next(v for v in flow.db.pa_mapping_versions.rows if v.get('proposalId')==row['_id'] and v['status']=='VALIDATED')['sourceDescription']='Altered'
    elif bad=='activationMissing':
        if stage=='APPROVED': flow.db.pa_mapping_proposal_decisions.rows[0]['decidedBy']=row['createdBy']
        else: flow.db.pa_mapping_activations.rows.clear()
    else:
        if stage=='APPROVED': flow.db.pa_mapping_proposal_decisions.rows[0]['status']={'private':'never echo'}
        else: flow.db.pa_mapping_activations.rows[0].pop('lifecycleVersion')
    before={name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    for view in [await proposals.detail(flow.db,REVIEWER,row['_id']),
                 (await proposals.list_proposals(flow.db,REVIEWER,50))['items'][0]]:
        assert view['workflowStatus']=='UNVERIFIED' and view['workflowVerification']=='INVALID'
        assert view['threePersonStatus']=='INVALID' or view['recordedWorkflowStatus']=='UNKNOWN'
        assert 'private' not in str(view) and 'never echo' not in str(view)
        assert view['catalogVerification'] not in ('VERIFIED','VERIFIED_FOR_EXISTING_ACTIVE')
    assert before=={name:col.rows for name,col in flow.db.collections.items()}


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['missingDescription','consistentButWrongDescription'])
async def test_malformed_canonical_approval_never_becomes_verified_workflow(flow,bad):
    row=await imported(flow)
    await proposals.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    description=None if bad=='missingDescription' else 'Wrong canonical description'
    flow.db.pa_mapping_proposal_decisions.rows[0]['medicloudDescription']=description
    flow.db.pa_mapping_versions.rows[-1]['medicloudDescription']=description
    result=await proposals.detail(flow.db,REVIEWER,row['_id'])
    assert result['workflowStatus']=='UNVERIFIED' and result['recordedWorkflowStatus']=='APPROVED'
    assert result['workflowVerification']=='INVALID' and result['canonicalCode'] is None
