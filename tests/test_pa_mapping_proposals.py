"""Phase 3C explicit approvals with transactional fakes and sockets blocked."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4
from unittest.mock import AsyncMock
import pytest
from core import pa_mapping_proposals as domain, pa_mapping, pa_catalog_provenance as provenance
from core.pa_preparation_store import DomainConflict
from models.pa_preparation import Actor
from models.pa_mapping_proposals import ProposalAction, ActivationAction
from api import pa_mapping_proposals as api
from test_pa_preparation import flow, prepare as recalculate, ADMIN, review
from test_pa_preparation_api import team
from test_pa_mapping_review import setup_export, amended, ACTOR
from core import pa_mapping_review as transfer

REVIEWER=Actor(actorId='review-staff',canReviewMappings=True)
APPROVER=Actor(actorId='approve-staff',canApproveMappings=True)
ACTIVATOR=Actor(actorId='activate-staff',canActivateMappings=True)


def action(flow,*,activate=False,replace=False,**extra):
    head=flow.db.pa_mapping_heads.rows[0]
    values={'requestId':uuid4(),'expectedHeadVersion':head['version'],
        'expectedActiveVersion':head.get('activeVersion'),'reason':'Reviewed against canonical catalog'}|extra
    return ActivationAction(**values,replaceActive=replace) if activate else ProposalAction(**values)


async def imported(flow):
    export,data=await setup_export(flow)
    def edit(rows):
        for row in rows: row.update(proposed_medicloud_code='DRG-CANON',proposed_medicloud_description='Canonical medication A',mapping_notes='Review')
    await transfer.import_proposals(flow.db,ACTOR,export['exportId'],amended(data,edit))
    return flow.db.pa_mapping_versions.rows[-1]


@pytest.mark.asyncio
async def test_explicit_workflow_immutable_evidence_minimized_and_prices_unchanged(flow):
    row=await imported(flow)
    originals=deepcopy(flow.db.pa_mapping_versions.rows)
    orders=deepcopy(flow.db.orders.rows); owners=deepcopy(flow.db.pa_reference_owners.rows)
    with pytest.raises(DomainConflict,match='APPROVED_PROPOSAL_REQUIRED'):
        await domain.activate(flow.db,ACTIVATOR,row['_id'],action(flow,activate=True))
    listing=await domain.list_proposals(flow.db,REVIEWER,25)
    assert len(listing['items'])==1 and listing['items'][0]['workflowStatus']=='PROPOSED'
    assert listing['items'][0]['catalogVerification']=='VERIFIED'
    approve=action(flow)
    result=await domain.decide(flow.db,APPROVER,row['_id'],approve,approve=True)
    assert result['status']=='APPROVED' and flow.db.pa_mapping_heads.rows[0]['activeVersion'] is None
    before=deepcopy(flow.db.pa_preparation_events.rows)
    assert await domain.decide(flow.db,APPROVER,row['_id'],approve,approve=True)==result
    assert flow.db.pa_preparation_events.rows==before
    activation=action(flow,activate=True)
    result=await domain.activate(flow.db,ACTIVATOR,row['_id'],activation)
    assert result['status']=='ACTIVATED'
    assert await domain.activate(flow.db,ACTIVATOR,row['_id'],activation)==result
    assert flow.db.pa_mapping_versions.rows[:len(originals)]==originals
    assert flow.db.orders.rows==orders and flow.db.pa_reference_owners.rows==owners
    rows=await recalculate(flow)
    assert rows[0]['preparationStatus']=='READY' and rows[0]['lineAmount']=='1200.00'
    assert flow.db.orders.rows==orders
    view=await domain.detail(flow.db,REVIEWER,row['_id'])
    assert view['workflowStatus']=='ACTIVE' and view['providerTariffEligibility']=='NOT_VERIFIED'
    for secret in ('memberSnapshot','enrollee','inputFingerprint','paGeneration','signedProvenance','actionDigest'):
        assert secret not in str(view)
    flow.forbidden.assert_not_called()


@pytest.mark.asyncio
async def test_reject_does_not_change_active_head_and_is_idempotent(flow):
    row=await imported(flow); request=action(flow)
    active=flow.db.pa_mapping_heads.rows[0]['activeVersion']
    result=await domain.decide(flow.db,REVIEWER,row['_id'],request,approve=False)
    assert result['status']=='REJECTED'
    assert await domain.decide(flow.db,REVIEWER,row['_id'],request,approve=False)==result
    assert flow.db.pa_mapping_heads.rows[0]['activeVersion']==active
    with pytest.raises(DomainConflict,match='ALREADY_DECIDED'):
        await domain.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    with pytest.raises(DomainConflict,match='APPROVED_PROPOSAL_REQUIRED'):
        await domain.activate(flow.db,ACTIVATOR,row['_id'],action(flow,activate=True))


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['read','reject','approve','activate'])
async def test_domain_permissions_not_import_authority(flow,operation):
    row=await imported(flow)
    with pytest.raises(PermissionError):
        if operation=='read': await domain.detail(flow.db,ACTOR,row['_id'])
        elif operation=='activate': await domain.activate(flow.db,ACTOR,row['_id'],action(flow,activate=True))
        else: await domain.decide(flow.db,ACTOR,row['_id'],action(flow),approve=operation=='approve')


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['approve','activate'])
async def test_importer_cannot_self_approve_or_activate(flow,operation):
    row=await imported(flow)
    if operation=='activate': await domain.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    actor=Actor(actorId=row['createdBy'],canApproveMappings=True,canActivateMappings=True)
    with pytest.raises(PermissionError):
        if operation=='activate': await domain.activate(flow.db,actor,row['_id'],action(flow,activate=True))
        else: await domain.decide(flow.db,actor,row['_id'],action(flow),approve=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['description','namespace','code','revision','owner','closed','head','superseded','manifest','PRE11','suggestion','case'])
async def test_stale_or_altered_proposals_fail_atomically(flow,bad):
    row=await imported(flow); request=action(flow)
    if bad=='description': flow.db.orders.rows[0]['medications'][0]['name']='Changed source'
    elif bad=='namespace': row['sourceNamespaceId']='wrong'
    elif bad=='code': flow.db.orders.rows[0]['medications'][0]['procedureCode']='OTHER'
    elif bad=='revision': flow.db.pa_preparation_lines.rows[0]['revision']+=1
    elif bad=='owner': flow.db.pa_preparation_contexts.rows[0]['batchId']=str(uuid4())
    elif bad=='closed': flow.db.pa_preparation_batches.rows[0]['status']='CLOSED'
    elif bad=='head': flow.db.pa_mapping_heads.rows[0]['version']+=1
    elif bad=='superseded': flow.db.pa_mapping_versions.rows.append({**deepcopy(row),'_id':str(uuid4()),'version':row['version']+1})
    elif bad=='manifest': flow.db.pa_mapping_review_exports.rows[0]['bindings'][0]['sourceDescription']='Altered'
    elif bad=='PRE11': row['proposedMedicloudCode']='PRE11'
    elif bad=='suggestion': row['proposedMedicloudDescription']='Wrong canonical description'
    else: row['proposedMedicloudCode']='drg-canon'
    # row returned by fake collection is the actual stored row here.
    before={name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    with pytest.raises((DomainConflict,ValueError)):
        await domain.decide(flow.db,APPROVER,row['_id'],request,approve=True)
    assert before=={name:col.rows for name,col in flow.db.collections.items()}


@pytest.mark.asyncio
@pytest.mark.parametrize('stage',['approve','activate'])
@pytest.mark.parametrize('bad',['expired','signature','entry','revoked','identity','missing'])
async def test_catalog_cannot_authorize_bad_approval_or_activation(flow,monkeypatch,stage,bad):
    row=await imported(flow)
    if stage=='activate': await domain.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    snapshot=flow.db.pa_procedure_catalog_snapshots.rows[0]
    if bad=='expired': monkeypatch.setattr(provenance,'now_utc',lambda:snapshot['sourceExtractedAt']+timedelta(hours=24))
    elif bad=='signature': snapshot['signedProvenance']['signature']='invalid'
    elif bad=='entry': flow.db.pa_procedure_catalog_entries.rows[0]['procedureDescription']='Tampered'
    elif bad=='revoked': snapshot['revokedAt']=snapshot['sourceExtractedAt']
    elif bad=='identity': snapshot['trustedBy']=snapshot['importedBy']
    else: flow.db.pa_procedure_catalog_entries.rows.clear()
    before={name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    with pytest.raises(DomainConflict):
        if stage=='activate': await domain.activate(flow.db,ACTIVATOR,row['_id'],action(flow,activate=True))
        else: await domain.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    assert before=={name:col.rows for name,col in flow.db.collections.items()}


@pytest.mark.asyncio
async def test_activation_concurrency_and_rollback(flow):
    row=await imported(flow)
    await domain.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    request=action(flow,activate=True)
    before={name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    flow.db.pa_preparation_events.fail_insert=True
    with pytest.raises(RuntimeError): await domain.activate(flow.db,ACTIVATOR,row['_id'],request)
    assert before=={name:col.rows for name,col in flow.db.collections.items()}
    flow.db.pa_preparation_events.fail_insert=False
    results=await asyncio.gather(domain.activate(flow.db,ACTIVATOR,row['_id'],request),domain.activate(flow.db,ACTIVATOR,row['_id'],request))
    assert results[0]==results[1] and len(flow.db.pa_mapping_activations.rows)==1
    with pytest.raises(DomainConflict,match='ALREADY_ACTIVATED'):
        await domain.activate(flow.db,ACTIVATOR,row['_id'],action(flow,activate=True))


def register(team):
    team.app.include_router(api.router,prefix='/api')
    team.client.headers.update({'Origin':'http://localhost:3000','Content-Type':'application/json'})
    return '/api/pa-mapping-proposals'


@pytest.mark.parametrize('permission,path,method',[(api.REVIEW,'','get'),(api.REVIEW,'/reject','post'),(api.APPROVE,'/approve','post'),(api.ACTIVATE,'/activate','post')])
def test_api_unauthorized_and_cross_site(team,permission,path,method):
    base=register(team); target=base+(('/'+str(uuid4())+path) if path else '')
    request=getattr(team.client,method)
    team.staff['permissions']=[]
    response=request(target,**({'json':{}} if method=='post' else {}))
    assert response.status_code==403
    team.staff['permissions']=[permission]
    if method=='post':
        assert request(target,json={},headers={'Origin':'https://attacker.example'}).status_code==403
        assert request(target,content=b'{}',headers={'Content-Type':'text/plain'}).status_code==415
        assert request(target,json={'actorId':'forged'}).status_code==422
    else: assert request(target).status_code==200
    team.client.cookies.clear()
    assert request(target,**({'json':{}} if method=='post' else {})).status_code==401


@pytest.mark.asyncio
async def test_existing_active_replacement_requires_explicit_confirmation(flow):
    from models.pa_mapping_review import CreateReviewExport
    flow.db.orders.rows[0]['medications'][0]['name']='Medication B'
    await recalculate(flow)
    exported=await transfer.create_export(flow.db,ACTOR,flow.batch['_id'],CreateReviewExport(expectedVersion=1))
    data=await transfer.download_export(flow.db,ACTOR,exported['exportId'])
    await transfer.import_proposals(flow.db,ACTOR,exported['exportId'],amended(data,lambda rows:rows[0].update(
        proposed_medicloud_code='DRG-CANON',proposed_medicloud_description='Canonical medication A')))
    row=flow.db.pa_mapping_versions.rows[-1]
    before=deepcopy(flow.db.pa_mapping_versions.rows)
    await domain.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    assert flow.db.pa_mapping_heads.rows[0]['activeVersion']==2
    with pytest.raises(DomainConflict,match='EXPLICIT_ACTIVE_REPLACEMENT_REQUIRED'):
        await domain.activate(flow.db,ACTIVATOR,row['_id'],action(flow,activate=True))
    await domain.activate(flow.db,ACTIVATOR,row['_id'],action(flow,activate=True,replace=True))
    assert flow.db.pa_mapping_versions.rows[:len(before)]==before
    assert flow.db.pa_mapping_heads.rows[0]['activeVersion']!=2
    assert (await recalculate(flow))[0]['preparationStatus']=='READY'


@pytest.mark.asyncio
async def test_multiline_recalculation_preserves_line_totals_and_expired_active_resolution(flow,monkeypatch):
    order=flow.db.orders.rows[0]
    order['medications']=[{**order['medications'][0],'lineId':f'line-{i}'} for i in range(8)]
    order['finalProcedurePrices']=[{'medicationLineId':f'line-{i}','procedureCode':'LOCAL-1','amount':1200} for i in range(8)]
    order['medicationSubtotal']=order['overallTotal']=9600
    row=await imported(flow)
    await domain.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    await domain.activate(flow.db,ACTIVATOR,row['_id'],action(flow,activate=True))
    original=deepcopy(flow.db.orders.rows)
    monkeypatch.setattr(provenance,'now_utc',lambda:flow.snapshot['sourceExtractedAt']+timedelta(hours=25))
    spy=AsyncMock(wraps=provenance.verify_snapshot); monkeypatch.setattr(provenance,'verify_snapshot',spy)
    rows=await recalculate(flow)
    assert len(rows)==8 and all(r['preparationStatus']=='READY' and r['lineAmount']=='1200.00' for r in rows)
    assert spy.await_count==1 and flow.db.orders.rows==original


@pytest.mark.asyncio
async def test_approval_rollback_and_concurrent_distinct_actions(flow):
    row=await imported(flow); before={name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    flow.db.pa_mapping_proposal_decisions.fail_insert=True
    with pytest.raises(RuntimeError): await domain.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    assert before=={name:col.rows for name,col in flow.db.collections.items()}
    flow.db.pa_mapping_proposal_decisions.fail_insert=False
    results=await asyncio.gather(domain.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True),
        domain.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True),return_exceptions=True)
    assert sum(isinstance(r,DomainConflict) for r in results)==1
    assert len(flow.db.pa_mapping_proposal_decisions.rows)==1


@pytest.mark.asyncio
async def test_generic_domain_cannot_bypass_imported_proposal_workflow(flow):
    row=await imported(flow)
    # Pointing a head at a proposal still cannot grant old generic review authority.
    flow.db.pa_mapping_heads.rows[0]['version']=row['version']
    with pytest.raises(DomainConflict,match='EXPLICIT_PROPOSAL_WORKFLOW_REQUIRED'):
        await pa_mapping.revise_mapping(flow.db,ADMIN,row['mappingId'],row['version'],'ACTIVE',
            snapshot_id=flow.snapshot['_id'],review=review())


def test_api_legitimate_complete_flow_permissions_and_pagination(team):
    base=register(team)
    row=asyncio.run(imported(team.flow)); target=base+'/'+row['_id']
    team.staff['permissions']=[api.REVIEW]
    listing=team.client.get(base,params={'limit':1})
    assert listing.status_code==200 and len(listing.json()['items'])==1
    assert team.client.get(base,params={'limit':51}).status_code==422
    assert team.client.get(target).json()['workflowStatus']=='PROPOSED'
    body=action(team.flow).model_dump(mode='json')
    assert team.client.post(target+'/approve',json=body).status_code==403
    team.staff['permissions']=[api.APPROVE]
    assert team.client.post(target+'/approve',json={**body,'snapshotId':team.flow.snapshot['_id']}).status_code==422
    assert team.client.post(target+'/approve',json=body).json()['status']=='APPROVED'
    assert team.client.get(base).status_code==403
    assert team.client.post(target+'/activate',json=action(team.flow,activate=True).model_dump(mode='json')).status_code==403
    team.staff['permissions']=[api.APPROVE,api.ACTIVATE]
    assert team.client.post(target+'/activate',json=action(team.flow,activate=True).model_dump(mode='json')).status_code==403
    from bson import ObjectId
    from core.security import encode_session
    other=ObjectId()
    team.flow.db.staff_users.rows.append({'_id':other,'permissions':[api.ACTIVATE]})
    team.client.cookies.set('staff_session',encode_session({'role':'staff','userId':str(other)}))
    response=team.client.post(target+'/activate',json=action(team.flow,activate=True).model_dump(mode='json'))
    assert response.status_code==200 and response.json()['status']=='ACTIVATED'
    assert len(team.flow.db.pa_mapping_activations.rows)==1


@pytest.mark.parametrize('missing',['Origin','Content-Type'])
def test_api_missing_security_headers_denied(team,missing):
    base=register(team); team.staff['permissions']=[api.APPROVE]
    del team.client.headers[missing]
    response=team.client.post(base+'/'+str(uuid4())+'/approve',content=b'{}')
    assert response.status_code==(403 if missing=='Origin' else 415)


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['approvedActor','decisionActor','namespace','description','head'])
async def test_activation_rechecks_immutable_approval_binding(flow,bad):
    row=await imported(flow)
    await domain.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    approved=flow.db.pa_mapping_versions.rows[-1]
    if bad=='approvedActor': approved['createdBy']='forged'
    elif bad=='decisionActor': flow.db.pa_mapping_proposal_decisions.rows[0]['decidedBy']=row['createdBy']
    elif bad=='namespace': approved['sourceNamespaceId']='wrong'
    elif bad=='description': approved['medicloudDescription']='Wrong canonical description'
    else: flow.db.pa_mapping_heads.rows[0]['version']+=1
    with pytest.raises(DomainConflict):
        await domain.activate(flow.db,ACTIVATOR,row['_id'],action(flow,activate=True))
    assert not flow.db.pa_mapping_activations.rows


@pytest.mark.asyncio
async def test_catalog_page_verification_reused_and_cursor_bounded(flow,monkeypatch):
    row=await imported(flow)
    # Existing proposal records retained for history; paging does not grant activation.
    for _ in range(3):
        flow.db.pa_mapping_versions.rows.append({**deepcopy(row),'_id':str(uuid4())})
    spy=AsyncMock(wraps=provenance.verify_snapshot); monkeypatch.setattr(provenance,'verify_snapshot',spy)
    first=await domain.list_proposals(flow.db,REVIEWER,2)
    assert len(first['items'])==2 and first['nextCursor'] and spy.await_count==1
    second=await domain.list_proposals(flow.db,REVIEWER,2,first['nextCursor'])
    assert len(second['items'])==2 and second['nextCursor'] is None and spy.await_count==2
    assert not ({r['proposalId'] for r in first['items']} & {r['proposalId'] for r in second['items']})


@pytest.mark.asyncio
async def test_three_distinct_people_even_with_both_permissions_and_receipt_replay(flow,monkeypatch):
    row=await imported(flow)
    both=Actor(actorId=APPROVER.actorId,canApproveMappings=True,canActivateMappings=True)
    await domain.decide(flow.db,both,row['_id'],action(flow),approve=True)
    original={name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    with pytest.raises(PermissionError):
        await domain.activate(flow.db,both,row['_id'],action(flow,activate=True))
    assert original=={name:col.rows for name,col in flow.db.collections.items()}
    request=action(flow,activate=True)
    receipt=await domain.activate(flow.db,ACTIVATOR,row['_id'],request)
    original={name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    monkeypatch.setattr(provenance,'now_utc',lambda:flow.snapshot['sourceExtractedAt']+timedelta(hours=25))
    assert await domain.activate(flow.db,ACTIVATOR,row['_id'],request)==receipt
    assert original=={name:col.rows for name,col in flow.db.collections.items()}


@pytest.mark.asyncio
@pytest.mark.parametrize('target',['import','proposal','decision','approved'])
@pytest.mark.parametrize('bad',[None,'',' spaced ',{'actor':'malformed'}])
async def test_activation_actor_evidence_fails_closed(flow,target,bad):
    row=await imported(flow)
    await domain.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    if target=='import': flow.db.pa_mapping_review_imports.rows[0]['importedBy']=bad
    elif target=='proposal': row['createdBy']=bad
    elif target=='decision': flow.db.pa_mapping_proposal_decisions.rows[0]['decidedBy']=bad
    else: flow.db.pa_mapping_versions.rows[-1]['createdBy']=bad
    before={name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    with pytest.raises(DomainConflict):
        await domain.activate(flow.db,ACTIVATOR,row['_id'],action(flow,activate=True))
    assert before=={name:col.rows for name,col in flow.db.collections.items()}


@pytest.mark.asyncio
async def test_completed_activation_rechecks_import_and_approval_receipts(flow):
    row=await imported(flow)
    await domain.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    request=action(flow,activate=True)
    await domain.activate(flow.db,ACTIVATOR,row['_id'],request)
    decision=deepcopy(flow.db.pa_mapping_proposal_decisions.rows[0])
    flow.db.pa_mapping_proposal_decisions.rows.clear()
    with pytest.raises(DomainConflict): await domain.activate(flow.db,ACTIVATOR,row['_id'],request)
    flow.db.pa_mapping_proposal_decisions.rows.append(decision)
    flow.db.pa_mapping_review_imports.rows[0]['importedBy']='different-importer'
    with pytest.raises(DomainConflict): await domain.activate(flow.db,ACTIVATOR,row['_id'],request)


@pytest.mark.asyncio
@pytest.mark.parametrize('stale',['closed','revision','superseded','source','head'])
async def test_reject_stale_proposal_retains_integrity_and_never_touches_head(flow,stale):
    row=await imported(flow); request=action(flow)
    if stale=='closed': flow.db.pa_preparation_batches.rows[0]['status']='CLOSED'
    elif stale=='revision': flow.db.pa_preparation_lines.rows[0]['revision']+=1
    elif stale=='source': flow.db.orders.rows[0]['medications'][0]['name']='Changed current description'
    elif stale=='head': flow.db.pa_mapping_heads.rows[0]['version']+=1
    else: flow.db.pa_mapping_versions.rows.append({**deepcopy(row),'_id':str(uuid4()),'version':row['version']+1})
    heads=deepcopy(flow.db.pa_mapping_heads.rows)
    orders=deepcopy(flow.db.orders.rows)
    originals=deepcopy(flow.db.pa_mapping_versions.rows)
    assert (await domain.decide(flow.db,REVIEWER,row['_id'],request,approve=False))['status']=='REJECTED'
    assert flow.db.pa_mapping_heads.rows==heads and flow.db.orders.rows==orders
    assert flow.db.pa_mapping_versions.rows[:len(originals)]==originals
    audit=deepcopy(flow.db.pa_preparation_events.rows)
    await domain.decide(flow.db,REVIEWER,row['_id'],request,approve=False)
    assert flow.db.pa_preparation_events.rows==audit
    with pytest.raises(DomainConflict,match='ALREADY_DECIDED'):
        await domain.decide(flow.db,REVIEWER,row['_id'],action(flow),approve=False)
    with pytest.raises(DomainConflict,match='ALREADY_DECIDED'):
        await domain.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('state',['APPROVED','ACTIVE'])
async def test_reject_cannot_reverse_approval_or_activation(flow,state):
    row=await imported(flow)
    await domain.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True)
    if state=='ACTIVE': await domain.activate(flow.db,ACTIVATOR,row['_id'],action(flow,activate=True))
    before={name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    with pytest.raises(DomainConflict,match='ALREADY_DECIDED'):
        await domain.decide(flow.db,REVIEWER,row['_id'],action(flow),approve=False)
    assert before=={name:col.rows for name,col in flow.db.collections.items()}


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['namespace','manifest','importer'])
async def test_stale_rejection_does_not_bypass_immutable_identity_or_integrity(flow,bad):
    row=await imported(flow)
    flow.db.pa_preparation_batches.rows[0]['status']='CLOSED'
    if bad=='namespace': row['sourceNamespaceId']='tampered'
    elif bad=='manifest': flow.db.pa_mapping_review_exports.rows[0]['bindings'][0]['sourceDescription']='tampered'
    else: flow.db.pa_mapping_review_imports.rows[0]['importedBy']='tampered'
    with pytest.raises(DomainConflict):
        await domain.decide(flow.db,REVIEWER,row['_id'],action(flow),approve=False)
    assert not flow.db.pa_mapping_proposal_decisions.rows


@pytest.mark.asyncio
async def test_old_rejection_does_not_supersede_newer_approval(flow):
    old=await imported(flow)
    # Simulate a second valid imported proposal revision with the same source scope.
    new={**deepcopy(old),'_id':str(uuid4()),'version':old['version']+1}
    flow.db.pa_mapping_versions.rows.append(new)
    await domain.decide(flow.db,APPROVER,new['_id'],action(flow),approve=True)
    await domain.decide(flow.db,REVIEWER,old['_id'],action(flow),approve=False)
    assert (await domain.activate(flow.db,ACTIVATOR,new['_id'],action(flow,activate=True)))['status']=='ACTIVATED'


@pytest.mark.asyncio
async def test_stale_rejection_rollback_keeps_head_versions_and_audit_unchanged(flow):
    row=await imported(flow)
    flow.db.pa_preparation_batches.rows[0]['status']='CLOSED'
    before={name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    flow.db.pa_preparation_events.fail_insert=True
    with pytest.raises(RuntimeError):
        await domain.decide(flow.db,REVIEWER,row['_id'],action(flow),approve=False)
    assert before=={name:col.rows for name,col in flow.db.collections.items()}


@pytest.mark.asyncio
async def test_concurrent_approval_and_rejection_commit_only_one_decision(flow):
    row=await imported(flow)
    results=await asyncio.gather(
        domain.decide(flow.db,REVIEWER,row['_id'],action(flow),approve=False),
        domain.decide(flow.db,APPROVER,row['_id'],action(flow),approve=True),return_exceptions=True)
    assert sum(isinstance(result,DomainConflict) for result in results)==1
    assert len(flow.db.pa_mapping_proposal_decisions.rows)==1
    assert flow.db.pa_mapping_heads.rows[0]['activeVersion'] is None
