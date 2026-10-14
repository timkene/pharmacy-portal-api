"""Offline signing fixtures; fake SELECT-only SQL/Mongo and blocked sockets."""
import base64
from copy import deepcopy
from datetime import timedelta, timezone
from unittest.mock import AsyncMock
from bson import BSON, ObjectId
from bson.codec_options import CodecOptions
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
from api import pa_catalog as api
from core import pa_catalog as catalog, pa_catalog_provenance as prov, pa_mapping
from core.pa_preparation_store import DomainConflict, now_utc
from models.pa_preparation import Actor, CatalogEvidence, CatalogValidationEvidence
from scripts import extract_pa_catalog as extractor
from catalog_fixtures import TEST_KEY, signed_artifact
from test_pa_preparation import flow, STAFF
from test_pa_preparation_api import team

INGEST = Actor(actorId='uploader', canIngestCatalog=True)
VALIDATE = Actor(actorId='validator', canValidateCatalog=True)
TRUST = Actor(actorId='trustee', canTrustCatalog=True)
def wire(value): return prov.canonical_json(value)
async def ingest(flow, value=None): return await catalog.ingest_signed_catalog(flow.db, INGEST, wire(value or signed_artifact()))


@pytest.mark.asyncio
async def test_signature_workflow_immutable_audit_and_no_mapping_ledger_writes(flow):
    protected = {name: deepcopy(flow.db[name].rows) for name in ('orders','pa_reference_owners','pa_mapping_heads','pa_mapping_versions')}
    snapshot = await ingest(flow)
    assert snapshot['status'] == 'VALIDATED' and 'trustedBy' not in snapshot
    ref = await catalog.validate_signed_catalog(flow.db, VALIDATE, snapshot['_id'])
    before = deepcopy(flow.db.pa_preparation_events.rows)
    result = await pa_mapping.promote_catalog_trust(flow.db, TRUST, snapshot['_id'], ref)
    assert result['status'] == 'TRUSTED'
    assert (await pa_mapping.canonical_procedure(flow.db, snapshot['_id'],'DRG-CANON'))['procedureDescription'] == 'Canonical medication A'
    assert before == flow.db.pa_preparation_events.rows[:len(before)]
    assert protected == {name: flow.db[name].rows for name in protected}
    assert catalog.snapshot_view(result)['providerTariffEligibility'] == 'NOT_VERIFIED'
    assert catalog.snapshot_view(result)['priceApplicability'] == 'NOT_VERIFIED'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['signature','catalog','manifest','key','source','table','columns','clientkey','trustflag','duplicate','description','fallback','incomplete','future','expired','count'])
async def test_artifacts_reject_forged_or_incomplete_authority(flow,bad):
    artifact = signed_artifact()
    if bad == 'signature': artifact['signature'] = base64.b64encode(b'0'*64).decode()
    if bad == 'catalog': artifact['entries'][0]['procedureDescription'] = 'Altered'
    if bad == 'manifest': artifact['manifest']['recordCount'] += 1
    if bad == 'key': artifact = signed_artifact(key=Ed25519PrivateKey.generate())
    if bad == 'source': artifact = signed_artifact(sqlServerIdentity='another-sql')
    if bad == 'table': artifact = signed_artifact(sourceTable='dbo.benefitcode_procedure')
    if bad == 'columns': artifact = signed_artifact(sourceColumns='*')
    if bad == 'clientkey': artifact['publicKey'] = 'caller-key'
    if bad == 'trustflag': artifact['status'] = 'TRUSTED'
    if bad == 'duplicate': artifact = signed_artifact(entries=[artifact['entries'][0],{**artifact['entries'][0],'procedureCode':'drg-canon'}])
    if bad == 'description': artifact = signed_artifact(entries=[{'procedureCode':'A','procedureDescription':''}])
    if bad == 'fallback': artifact = signed_artifact(entries=[{'procedureCode':'A','procedureDescription':'A'}])
    if bad == 'incomplete': artifact = signed_artifact(extractionComplete=False)
    if bad == 'future': artifact = signed_artifact(extracted=now_utc()+timedelta(minutes=1))
    if bad == 'expired': artifact = signed_artifact(extracted=now_utc()-timedelta(hours=24))
    if bad == 'count': artifact = signed_artifact(expectedRowCount=2,finalRowCount=2)
    before = {name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    with pytest.raises(DomainConflict): await ingest(flow,artifact)
    assert before == {name:col.rows for name,col in flow.db.collections.items()}


@pytest.mark.asyncio
@pytest.mark.parametrize('actor_id',['uploader','validator'])
async def test_server_persisted_separation_of_duties(flow,actor_id):
    snapshot = await ingest(flow)
    ref = await catalog.validate_signed_catalog(flow.db,VALIDATE,snapshot['_id'])
    before = deepcopy(flow.db.pa_procedure_catalog_snapshots.rows)
    with pytest.raises(PermissionError): await pa_mapping.promote_catalog_trust(flow.db,Actor(actorId=actor_id,canTrustCatalog=True),snapshot['_id'],ref)
    assert before == flow.db.pa_procedure_catalog_snapshots.rows


@pytest.mark.asyncio
async def test_method_label_cannot_replace_signature(flow):
    entries=[{'procedureCode':'OTHER','procedureDescription':'Other procedure'}]
    evidence=CatalogEvidence(sourceExtractedAt=now_utc()-timedelta(minutes=1),recordCount=1,contentHash=pa_mapping.catalog_hash(entries),evidenceReference='caller-label')
    snapshot=await pa_mapping.persist_catalog_snapshot(flow.db,INGEST,evidence,entries)
    evidence=CatalogValidationEvidence(**evidence.model_dump(),snapshotId=snapshot['_id'],validatedAt=now_utc(),validatedBy='validator',validationEvidenceId='forged',validationMethod='SERVER_CONTROLLED_MEDICLOUD_EXTRACTION')
    with pytest.raises(DomainConflict,match='SIGNED_CATALOG_PROVENANCE_REQUIRED'): await pa_mapping.register_catalog_validation(flow.db,VALIDATE,evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize('stage',['validate','trust','lookup'])
async def test_freshness_rechecked_old_snapshots_retained(flow,monkeypatch,stage):
    snapshot=await ingest(flow)
    ref=await catalog.validate_signed_catalog(flow.db,VALIDATE,snapshot['_id'])
    if stage=='lookup': await pa_mapping.promote_catalog_trust(flow.db,TRUST,snapshot['_id'],ref)
    before=deepcopy(flow.db.pa_procedure_catalog_snapshots.rows)
    monkeypatch.setattr(prov,'now_utc',lambda:snapshot['sourceExtractedAt']+timedelta(hours=24))
    with pytest.raises(DomainConflict,match='CATALOG_EXPIRED'):
        if stage=='validate': await catalog.validate_signed_catalog(flow.db,VALIDATE,snapshot['_id'])
        elif stage=='trust': await pa_mapping.promote_catalog_trust(flow.db,TRUST,snapshot['_id'],ref)
        else: await pa_mapping.canonical_procedure(flow.db,snapshot['_id'],'DRG-CANON')
    assert before==flow.db.pa_procedure_catalog_snapshots.rows


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['description','manifest','signature','normalized','count'])
async def test_stored_evidence_tampering(flow,bad):
    snapshot=await ingest(flow)
    stored=flow.db.pa_procedure_catalog_snapshots.rows[-1]
    row=next(row for row in flow.db.pa_procedure_catalog_entries.rows if row['snapshotId']==snapshot['_id'])
    if bad=='description': row['procedureDescription']='Altered'
    if bad=='manifest': stored['signedProvenance']['manifest']['authorityId']='forged'
    if bad=='signature': stored['signedProvenance']['signature']=base64.b64encode(b'0'*64).decode()
    if bad=='normalized': row['normalizedCode']='OTHER'
    if bad=='count': stored['ingestedCount']+=1
    with pytest.raises(DomainConflict): await catalog.validate_signed_catalog(flow.db,VALIDATE,snapshot['_id'])


@pytest.mark.asyncio
async def test_validation_and_trust_rollback(flow):
    snapshot=await ingest(flow)
    before={name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    flow.db.pa_preparation_events.fail_insert=True
    with pytest.raises(RuntimeError): await catalog.validate_signed_catalog(flow.db,VALIDATE,snapshot['_id'])
    assert before=={name:col.rows for name,col in flow.db.collections.items()}
    flow.db.pa_preparation_events.fail_insert=False
    ref=await catalog.validate_signed_catalog(flow.db,VALIDATE,snapshot['_id'])
    before={name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    flow.db.pa_preparation_events.fail_insert=True
    with pytest.raises(RuntimeError): await pa_mapping.promote_catalog_trust(flow.db,TRUST,snapshot['_id'],ref)
    assert before=={name:col.rows for name,col in flow.db.collections.items()}


@pytest.mark.asyncio
async def test_partial_ingestion_never_trusted(flow):
    old=deepcopy(flow.snapshot)
    flow.db.pa_procedure_catalog_entries.fail_insert=True
    with pytest.raises(RuntimeError): await ingest(flow)
    partial=flow.db.pa_procedure_catalog_snapshots.rows[-1]
    assert partial['status']=='INGESTING'
    with pytest.raises(DomainConflict): await catalog.validate_signed_catalog(flow.db,VALIDATE,partial['_id'])
    assert flow.db.pa_procedure_catalog_snapshots.rows[0]==old


@pytest.mark.asyncio
async def test_duplicate_validation_and_missing_receipt(flow):
    snapshot=await ingest(flow)
    with pytest.raises(DomainConflict): await pa_mapping.promote_catalog_trust(flow.db,TRUST,snapshot['_id'],snapshot['_id'])
    await catalog.validate_signed_catalog(flow.db,VALIDATE,snapshot['_id'])
    before=deepcopy(flow.db.pa_catalog_validation_evidence.rows)
    with pytest.raises(DomainConflict): await catalog.validate_signed_catalog(flow.db,VALIDATE,snapshot['_id'])
    assert before==flow.db.pa_catalog_validation_evidence.rows


@pytest.mark.asyncio
async def test_bson_precision_signed_manifest_stable(flow):
    snapshot=await ingest(flow)
    flow.db.pa_procedure_catalog_snapshots.rows[-1]=BSON.encode(snapshot).decode(codec_options=CodecOptions(tz_aware=True,tzinfo=timezone.utc))
    ref=await catalog.validate_signed_catalog(flow.db,VALIDATE,snapshot['_id'])
    assert (await pa_mapping.promote_catalog_trust(flow.db,TRUST,snapshot['_id'],ref))['status']=='TRUSTED'


@pytest.mark.asyncio
async def test_domain_permissions_and_no_config_fail_closed(flow,monkeypatch):
    with pytest.raises(PermissionError): await catalog.ingest_signed_catalog(flow.db,STAFF,wire(signed_artifact()))
    with pytest.raises(PermissionError): await catalog.validate_signed_catalog(flow.db,STAFF,flow.snapshot['_id'])
    with pytest.raises(PermissionError): await pa_mapping.promote_catalog_trust(flow.db,STAFF,flow.snapshot['_id'],'x')
    monkeypatch.delenv('PA_CATALOG_PUBLIC_KEY_PATH')
    with pytest.raises(prov.CatalogConfigurationError): await ingest(flow)


def test_deterministic_and_strict_json():
    extracted=now_utc()
    assert wire(signed_artifact(extracted=extracted))==wire(signed_artifact(extracted=extracted))
    for raw in (b'{"entries":[],"entries":[]}',b'{"entries":NaN}'):
        with pytest.raises(DomainConflict): prov.parse_artifact(raw)


class SQLCursor:
    def __init__(self,conn): self.conn,self.position,self.query=conn,0,None
    def execute(self,query):
        assert query.startswith('SELECT ')
        self.conn.queries.append(query); self.query=query; return self
    def fetchone(self): return self.conn.identity if self.query==extractor.IDENTITY_SQL else (self.conn.count,)
    def fetchmany(self,limit):
        assert limit==1000
        if self.position: return []
        self.position+=1; return self.conn.rows
    def close(self): self.conn.cursor_closed=True
class SQLConnection:
    autocommit=False
    identity=('test-sql','test-db',123)
    rows=[('DRG-CANON','Canonical medication A')]
    count=1
    def __init__(self): self.queries,self.rolled_back,self.cursor_closed=[],False,False
    def cursor(self): return SQLCursor(self)
    def rollback(self): self.rolled_back=True


def test_offline_extractor_verified_source_complete_select_only(flow):
    conn=SQLConnection()
    data=extractor.extract_catalog(conn,prov.configured_authority(),TEST_KEY)
    prov.verify_artifact(prov.parse_artifact(data))
    assert conn.queries==[extractor.IDENTITY_SQL,extractor.COUNT_SQL,extractor.CATALOG_SQL,extractor.COUNT_SQL]
    assert conn.rolled_back and conn.cursor_closed
    assert b'password' not in data and b'privateKey' not in data


@pytest.mark.parametrize('bad',['server','database','table','incomplete','duplicate','description','transaction','key'])
def test_extractor_fail_closed(flow,bad):
    conn,key=SQLConnection(),TEST_KEY
    if bad=='server': conn.identity=('other','test-db',123)
    if bad=='database': conn.identity=('test-sql','other',123)
    if bad=='table': conn.identity=('test-sql','test-db',None)
    if bad=='incomplete': conn.count=2
    if bad=='duplicate': conn.rows=[('A','Canonical A'),('a','Duplicate A')]; conn.count=2
    if bad=='description': conn.rows=[('A','')]
    if bad=='transaction': conn.autocommit=True
    if bad=='key': key=Ed25519PrivateKey.generate()
    with pytest.raises(DomainConflict): extractor.extract_catalog(conn,prov.configured_authority(),key)


def register(team):
    team.app.include_router(api.router,prefix='/api')
    team.client.headers.update({'Origin':'http://localhost:3000','Content-Type':'application/json'})
    return '/api/pa-catalog'

def test_api_permissions_and_independent_identity(team):
    base=register(team); team.staff['permissions']=[api.INGEST]
    result=team.client.post(base+'/snapshots',content=wire(signed_artifact()),headers={'content-type':'application/json'})
    assert result.status_code==200
    path=base+'/snapshots/'+result.json()['snapshotId']
    assert team.client.post(path+'/validate',json={}).status_code==403
    team.staff['permissions']=[api.VALIDATE]
    assert team.client.post(path+'/validate',json={}).status_code==403
    from core.security import encode_session
    validator=ObjectId(); team.flow.db.staff_users.rows.append({'_id':validator,'permissions':[api.VALIDATE,api.TRUST]})
    team.client.cookies.set('staff_session',encode_session({'role':'staff','userId':str(validator)}))
    assert team.client.post(path+'/validate',json={}).status_code==200
    team.staff['permissions']=[api.TRUST]
    assert team.client.post(path+'/trust',json={}).status_code==403
    from core.security import encode_session
    other=ObjectId(); team.flow.db.staff_users.rows.append({'_id':other,'permissions':[api.TRUST]})
    team.client.cookies.set('staff_session',encode_session({'role':'staff','userId':str(other)}))
    trusted=team.client.post(path+'/trust',json={})
    assert trusted.status_code==200 and trusted.json()['status']=='TRUSTED'
    assert trusted.json()['providerTariffEligibility']=='NOT_VERIFIED'


def test_api_forged_authority_permissions_and_unauthenticated(team):
    base=register(team); team.staff['permissions']=[]
    assert team.client.post(base+'/snapshots',content=wire(signed_artifact()),headers={'content-type':'application/json','PA_CATALOG_INGEST':'true'}).status_code==403
    team.staff['permissions']=[api.INGEST]
    artifact=signed_artifact(); artifact['publicKey']='SECRET-CLIENT-KEY'
    response=team.client.post(base+'/snapshots',content=wire(artifact),headers={'content-type':'application/json'})
    assert response.status_code==409 and 'SECRET-CLIENT-KEY' not in response.text
    team.client.cookies.clear()
    assert team.client.post(base+'/snapshots').status_code==401


def test_api_rejects_client_validation_or_trust_body(team):
    base=register(team); team.staff['permissions']=[api.INGEST]
    result=team.client.post(base+'/snapshots',content=wire(signed_artifact()),headers={'content-type':'application/json'}).json()
    path=base+'/snapshots/'+result['snapshotId']; team.staff['permissions']=[api.VALIDATE,api.TRUST]
    assert team.client.post(path+'/validate',json={'validatedBy':'other'}).status_code==400
    assert team.client.post(path+'/trust',json={'publicKey':'other'}).status_code==400


def test_api_missing_authority_503(team,monkeypatch):
    base=register(team); team.staff['permissions']=[api.INGEST]
    monkeypatch.delenv('PA_CATALOG_PUBLIC_KEY_PATH')
    assert team.client.post(base+'/snapshots',content=wire(signed_artifact()),headers={'content-type':'application/json'}).status_code==503


def test_only_catalog_routes():
    assert len(api.router.routes)==3
    assert all('mapping' not in route.path and 'submission' not in route.path for route in api.router.routes)


@pytest.mark.asyncio
async def test_forged_validation_digest_cannot_self_attest(flow):
    snapshot=await ingest(flow)
    evidence=CatalogValidationEvidence(**{key:snapshot[key] for key in CatalogEvidence.model_fields},
        snapshotId=snapshot['_id'],validatedAt=now_utc(),validatedBy='validator',validationEvidenceId='forged',
        validationMethod='SERVER_CONTROLLED_MEDICLOUD_EXTRACTION',manifestDigest='0'*64,signingKeyFingerprint='0'*64)
    with pytest.raises(DomainConflict,match='CATALOG_VALIDATION_EVIDENCE_MISMATCH'):
        await pa_mapping.register_catalog_validation(flow.db,VALIDATE,evidence)


@pytest.mark.asyncio
async def test_validation_receipt_forgery_detected_at_trust(flow):
    snapshot=await ingest(flow)
    ref=await catalog.validate_signed_catalog(flow.db,VALIDATE,snapshot['_id'])
    receipt=next(row for row in flow.db.pa_catalog_validation_evidence.rows if row['_id']==ref)
    receipt['manifestDigest']='0'*64
    with pytest.raises(DomainConflict): await pa_mapping.promote_catalog_trust(flow.db,TRUST,snapshot['_id'],ref)


@pytest.mark.asyncio
async def test_current_config_key_rotation_does_not_silently_trust_old_key(flow,monkeypatch,tmp_path):
    from cryptography.hazmat.primitives import serialization
    new_key=Ed25519PrivateKey.generate()
    path=tmp_path/'rotated-test-public.pem'
    path.write_bytes(new_key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo))
    monkeypatch.setenv('PA_CATALOG_PUBLIC_KEY_PATH',str(path))
    with pytest.raises(DomainConflict,match='CATALOG_AUTHORITY_MISMATCH'):
        await pa_mapping.canonical_procedure(flow.db,flow.snapshot['_id'],'DRG-CANON')


def test_canonical_timestamp_precision_and_exact_age_boundary():
    value=now_utc().replace(microsecond=123000)
    formatted=value.isoformat(timespec='milliseconds').replace('+00:00','Z')
    assert prov.extracted_timestamp(formatted)==value
    with pytest.raises(DomainConflict): prov.extracted_timestamp(formatted.replace('123Z','123456Z'))
    with pytest.raises(DomainConflict): prov.require_fresh(value,value+timedelta(hours=24))
    prov.require_fresh(value,value+timedelta(hours=24)-timedelta(milliseconds=1))


@pytest.mark.asyncio
async def test_catalog_requires_genuine_description_and_known_exact_code(flow):
    with pytest.raises(DomainConflict,match='PROCEDURE_CODE_NOT_FOUND'):
        await pa_mapping.canonical_procedure(flow.db,flow.snapshot['_id'],'NOT-IN-CATALOG')
    # A description is preserved exactly, not invented/overwritten by a proposal.
    assert (await pa_mapping.canonical_procedure(flow.db,flow.snapshot['_id'],'DRG-CANON'))['procedureCode']=='DRG-CANON'


def test_api_upload_size_capped_even_without_length(team,monkeypatch):
    base=register(team); team.staff['permissions']=[api.INGEST]
    monkeypatch.setattr(api,'MAX_ARTIFACT_BYTES',64)
    response=team.client.post(base+'/snapshots',content=iter([b'x'*40,b'x'*40]),headers={'content-type':'application/json'})
    assert response.status_code==413


@pytest.mark.asyncio
async def test_server_validation_write_conflict_translated_without_retry(flow,monkeypatch):
    from pymongo.errors import OperationFailure
    snapshot=await ingest(flow)
    before={name:deepcopy(col.rows) for name,col in flow.db.collections.items()}
    stub=AsyncMock(side_effect=OperationFailure('PRIVATE DRIVER DETAILS',code=112))
    monkeypatch.setattr(flow.db.pa_catalog_validation_evidence,'insert_one',stub)
    with pytest.raises(DomainConflict,match='TRANSACTION_CONFLICT_REFRESH_REQUIRED'):
        await catalog.validate_signed_catalog(flow.db,VALIDATE,snapshot['_id'])
    assert stub.await_count==1
    assert before=={name:col.rows for name,col in flow.db.collections.items()}


def test_schema_rejects_oversized_empty_and_unexpected_fields():
    from models.pa_catalog import MAX_ARTIFACT_BYTES
    for raw in (b'',b'0'*(MAX_ARTIFACT_BYTES+1)):
        with pytest.raises(DomainConflict): prov.parse_artifact(raw)
    artifact=signed_artifact()
    artifact['entries'][0]['memberName']='SECRET'
    with pytest.raises(DomainConflict): prov.parse_artifact(wire(artifact))


def test_sql_query_projection_does_not_use_member_tables_or_writes():
    assert 'SELECT procedurecode, proceduredesc FROM dbo.proceduredata' in extractor.CATALOG_SQL
    assert '*' not in extractor.CATALOG_SQL and 'WHERE' not in extractor.CATALOG_SQL
    for query in (extractor.COUNT_SQL,extractor.CATALOG_SQL,extractor.IDENTITY_SQL):
        assert query.startswith('SELECT ')
        assert not any(word in query.upper() for word in ('INSERT ','UPDATE ','DELETE ','MERGE ','EXEC '))


def test_private_key_exclusion_accounts_for_linked_repository(monkeypatch,tmp_path):
    root=tmp_path/'worktree'; primary=tmp_path/'primary'; external=tmp_path/'keys'/'private.pem'
    def output(args,**kwargs):
        return str(primary/'.git') if 'rev-parse' in args else f'worktree {root}\n\nworktree {primary}\n'
    monkeypatch.setattr(extractor.subprocess,'check_output',output)
    with pytest.raises(ValueError): extractor.require_external_private_key(root/'key.pem',root)
    with pytest.raises(ValueError): extractor.require_external_private_key(primary/'key.pem',root)
    extractor.require_external_private_key(external,root)


@pytest.mark.parametrize('field,value',[('artifactSchemaVersion',True),('recordCount','1'),('extractionComplete',1)])
def test_signed_schema_does_not_coerce_authority_scalar_types(field,value):
    artifact=signed_artifact(**{field:value})
    with pytest.raises(DomainConflict): prov.parse_artifact(wire(artifact))


@pytest.mark.asyncio
async def test_uploader_cannot_validate_domain_or_reuse_forged_identity(flow):
    snapshot = await ingest(flow)
    before = deepcopy(flow.db.pa_preparation_events.rows)
    with pytest.raises(PermissionError):
        await catalog.validate_signed_catalog(flow.db, Actor(actorId=INGEST.actorId, canValidateCatalog=True), snapshot['_id'])
    assert before == flow.db.pa_preparation_events.rows
    ref = await catalog.validate_signed_catalog(flow.db, VALIDATE, snapshot['_id'])
    await pa_mapping.promote_catalog_trust(flow.db, TRUST, snapshot['_id'], ref)
    stored = flow.db.pa_procedure_catalog_snapshots.rows[-1]
    stored['trustedBy'] = VALIDATE.actorId
    with pytest.raises(DomainConflict, match='EVIDENCE_MISMATCH'):
        await pa_mapping.canonical_procedure(flow.db, snapshot['_id'], 'DRG-CANON')


@pytest.mark.parametrize('headers,expected', [
    ({'Origin':'https://attacker.example'},403),
    ({'Origin':'null'},403),
    ({'Content-Type':'null'},415),
    ({'Content-Type':'application/x-www-form-urlencoded'},415),
    ({'Content-Type':'text/plain'},415),
    ({'Content-Type':'application/json; charset=latin1'},415),
    ({'Origin':'http://localhost:3000'},404),
    ({'Origin':'http://testserver'},404),
])
@pytest.mark.parametrize('action',['validate','trust'])
def test_catalog_mutation_origin_and_content_type(team, headers, expected, action):
    from uuid import uuid4
    base = register(team)
    team.staff['permissions'] = [api.VALIDATE, api.TRUST]
    result = team.client.post(base+'/snapshots/'+str(uuid4())+'/'+action, json={}, headers=headers)
    assert result.status_code == (409 if action == "trust" and expected == 404 else expected)


@pytest.mark.parametrize('action',['validate','trust'])
def test_catalog_missing_headers_and_nonbrowser_csrf(team, action):
    from uuid import uuid4
    base = register(team); team.staff['permissions']=[api.VALIDATE,api.TRUST]
    path = base+'/snapshots/'+str(uuid4())+'/'+action
    del team.client.headers['Origin']
    assert team.client.post(path,json={}).status_code == 403
    assert team.client.post(path,json={},headers={'X-Pharmacy-CSRF':'1'}).status_code == (404 if action == 'validate' else 409)
    assert team.client.post(path,json={},headers={'X-Pharmacy-CSRF':'1','Sec-Fetch-Site':'cross-site'}).status_code == 403
    del team.client.headers['Content-Type']
    assert team.client.post(path,content=b'{}',headers={'X-Pharmacy-CSRF':'1'}).status_code == 415
    assert team.client.post(path,json=None,headers={'X-Pharmacy-CSRF':'1'}).status_code == 415


@pytest.mark.asyncio
@pytest.mark.parametrize('age',[timedelta(hours=23,minutes=59),timedelta(hours=24),timedelta(hours=25)])
async def test_existing_active_resolution_vs_new_activation_at_expiry(flow, monkeypatch, age):
    from test_pa_preparation import ADMIN, review
    monkeypatch.setattr(prov,'now_utc',lambda:flow.snapshot['sourceExtractedAt']+age)
    result = await pa_mapping.canonical_procedure(flow.db,flow.snapshot['_id'],'DRG-CANON',
        existing_mapping=(flow.mapping['mappingId'],2))
    assert result['procedureCode']=='DRG-CANON'
    if age < timedelta(hours=24):
        assert await pa_mapping.canonical_procedure(flow.db,flow.snapshot['_id'],'DRG-CANON')
    else:
        with pytest.raises(DomainConflict,match='CATALOG_EXPIRED'):
            await pa_mapping.revise_mapping(flow.db,ADMIN,flow.mapping['mappingId'],2,'ACTIVE',
                snapshot_id=flow.snapshot['_id'],review=review())


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['entry','revocation','evidence','key','header'])
async def test_request_cache_does_not_bypass_mutation_or_key_rotation(flow, monkeypatch, tmp_path, change):
    async with prov.catalog_verification_scope():
        await pa_mapping.canonical_procedure(flow.db,flow.snapshot['_id'],'DRG-CANON')
        if change=='entry': flow.db.pa_procedure_catalog_entries.rows[0]['procedureDescription']='tampered'
        elif change=='revocation': flow.db.pa_procedure_catalog_snapshots.rows[0]['revokedAt']=now_utc()
        elif change=='evidence': flow.db.pa_catalog_validation_evidence.rows[0]['revokedAt']=now_utc()
        elif change=='header': flow.db.pa_procedure_catalog_snapshots.rows[0]['contentHash']='0'*64
        else:
            from cryptography.hazmat.primitives import serialization
            path=tmp_path/'rotated-public.pem'
            path.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo))
            monkeypatch.setenv('PA_CATALOG_PUBLIC_KEY_PATH',str(path))
        with pytest.raises(DomainConflict):
            await pa_mapping.canonical_procedure(flow.db,flow.snapshot['_id'],'DRG-CANON')


@pytest.mark.asyncio
async def test_twenty_thousand_catalog_verified_once_for_multiple_medication_lines(flow, monkeypatch):
    from test_pa_preparation import ADMIN, review, prepare
    entries=[{'procedureCode':'DRG-CANON','procedureDescription':'Canonical medication A'}]
    entries += [{'procedureCode':f'PROC-{i:05d}','procedureDescription':f'Canonical procedure {i}'} for i in range(19999)]
    # The fake Mongo unique-index implementation scans every prior insert. Replace only
    # its bulk insert with a set-checked equivalent to keep this realistic fixture bounded.
    collection=flow.db.pa_procedure_catalog_entries
    async def bulk(documents, **kwargs):
        ids={row['_id'] for row in collection.rows}
        identities={(row['snapshotId'],row['normalizedCode']) for row in collection.rows}
        for row in documents:
            assert row['_id'] not in ids and (row['snapshotId'],row['normalizedCode']) not in identities
            ids.add(row['_id']); identities.add((row['snapshotId'],row['normalizedCode']))
        collection.rows.extend(deepcopy(documents))
    monkeypatch.setattr(collection,'insert_many',bulk)
    snapshot=await ingest(flow,signed_artifact(entries=entries))
    ref=await catalog.validate_signed_catalog(flow.db,VALIDATE,snapshot['_id'])
    await pa_mapping.promote_catalog_trust(flow.db,TRUST,snapshot['_id'],ref)
    await pa_mapping.revise_mapping(flow.db,ADMIN,flow.mapping['mappingId'],2,'ACTIVE',snapshot_id=snapshot['_id'],review=review())
    order=flow.db.orders.rows[0]
    order['medications']=[{**order['medications'][0],'lineId':f'line-{i}'} for i in range(8)]
    order['finalProcedurePrices']=[{'medicationLineId':f'line-{i}','procedureCode':'LOCAL-1','amount':1200} for i in range(8)]
    order['medicationSubtotal']=order['overallTotal']=9600
    verify=AsyncMock(wraps=prov.verify_snapshot); monkeypatch.setattr(prov,'verify_snapshot',verify)
    single=AsyncMock(wraps=collection.find_one); monkeypatch.setattr(collection,'find_one',single)
    rows=await prepare(flow)
    assert len(rows)==8 and all(row['mappingVersion']==3 for row in rows)
    assert verify.await_count==1 and single.await_count==8
    # A new operation has a new scope and verifies the complete catalog again.
    await prepare(flow)
    assert verify.await_count==2 and single.await_count==16


@pytest.mark.asyncio
@pytest.mark.parametrize('age',[timedelta(hours=23,minutes=59),timedelta(hours=24),timedelta(hours=25)])
@pytest.mark.parametrize('stage',['validate','trust'])
async def test_new_catalog_authorization_exact_age_boundary(flow, monkeypatch, age, stage):
    snapshot=await ingest(flow)
    if stage=='trust':
        ref=await catalog.validate_signed_catalog(flow.db,VALIDATE,snapshot['_id'])
    monkeypatch.setattr(prov,'now_utc',lambda:snapshot['sourceExtractedAt']+age)
    async def operation():
        if stage=='validate': return await catalog.validate_signed_catalog(flow.db,VALIDATE,snapshot['_id'])
        return await pa_mapping.promote_catalog_trust(flow.db,TRUST,snapshot['_id'],ref)
    if age < timedelta(hours=24): assert await operation()
    else:
        with pytest.raises(DomainConflict,match='CATALOG_EXPIRED'): await operation()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['signature','entry','untrusted','revoked','identity'])
async def test_expired_active_resolution_still_requires_integrity_and_trust(flow, monkeypatch, bad):
    monkeypatch.setattr(prov,'now_utc',lambda:flow.snapshot['sourceExtractedAt']+timedelta(hours=25))
    stored=flow.db.pa_procedure_catalog_snapshots.rows[0]
    if bad=='signature': stored['signedProvenance']['signature']=base64.b64encode(b'0'*64).decode()
    elif bad=='entry': flow.db.pa_procedure_catalog_entries.rows[0]['procedureDescription']='tampered'
    elif bad=='untrusted': stored['status']='VALIDATED'
    elif bad=='revoked': stored['revokedAt']=now_utc()
    else: stored['importedBy']=stored['trustedBy']
    with pytest.raises(DomainConflict):
        await pa_mapping.canonical_procedure(flow.db,stored['_id'],'DRG-CANON',existing_mapping=(flow.mapping['mappingId'],2))


@pytest.mark.parametrize('name,values',[
    ('Origin',['http://localhost:3000','https://attacker.example']),
    ('Content-Type',['application/json','text/plain']),
])
def test_duplicate_security_headers_rejected(team,name,values):
    from uuid import uuid4
    base=register(team); team.staff['permissions']=[api.VALIDATE]
    headers=[('Origin','http://localhost:3000'),('Content-Type','application/json')]
    headers=[(k,v) for k,v in headers if k.lower()!=name.lower()]+[(name,value) for value in values]
    assert team.client.post(base+'/snapshots/'+str(uuid4())+'/validate',content=b'{}',headers=headers).status_code in (403,415)


@pytest.mark.asyncio
@pytest.mark.parametrize('hours',[24,25])
@pytest.mark.parametrize('delivered',[False,True])
async def test_preparation_reserved_delivery_and_active_lines_after_expiry(flow,monkeypatch,hours,delivered):
    from test_pa_preparation import catalog as fixture_catalog, ADMIN, review, prepare
    snapshot=await fixture_catalog(flow.db,[
        {'procedureCode':'DRG-CANON','procedureDescription':'Canonical medication A'},
        {'procedureCode':'PRE11','procedureDescription':'Delivery service'}])
    await pa_mapping.revise_mapping(flow.db,ADMIN,flow.mapping['mappingId'],2,'ACTIVE',snapshot_id=snapshot['_id'],review=review())
    if delivered:
        monkeypatch.setenv('PA_DELIVERY_DIAGNOSIS_CODE','Z76.9')
        flow.db.orders.rows[0].update(fulfillmentType='delivered',deliveryFee=500,overallTotal=1700)
    before=deepcopy(flow.db.orders.rows)
    monkeypatch.setattr(prov,'now_utc',lambda:snapshot['sourceExtractedAt']+timedelta(hours=hours))
    verify=AsyncMock(wraps=prov.verify_snapshot); monkeypatch.setattr(prov,'verify_snapshot',verify)
    rows=await prepare(flow)
    assert len(rows)==(2 if delivered else 1)
    assert all(row['preparationStatus']=='READY' for row in rows)
    assert verify.await_count==1
    if delivered:
        assert rows[1]['medicloudCode']=='PRE11' and rows[1]['lineAmount']=='500.00'
    assert flow.db.orders.rows==before
    with pytest.raises(DomainConflict,match='CATALOG_EXPIRED'):
        await pa_mapping.canonical_procedure(flow.db,snapshot['_id'],'PRE11')


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['revoked','tampered','unsigned','untrusted','missing'])
async def test_reserved_delivery_expiry_exception_requires_intact_trusted_evidence(flow,monkeypatch,bad):
    from test_pa_preparation import catalog as fixture_catalog, ADMIN, review, prepare
    snapshot=await fixture_catalog(flow.db,[
        {'procedureCode':'DRG-CANON','procedureDescription':'Canonical medication A'},
        {'procedureCode':'PRE11','procedureDescription':'Delivery service'}])
    await pa_mapping.revise_mapping(flow.db,ADMIN,flow.mapping['mappingId'],2,'ACTIVE',snapshot_id=snapshot['_id'],review=review())
    monkeypatch.setenv('PA_DELIVERY_DIAGNOSIS_CODE','Z76.9')
    flow.db.orders.rows[0].update(fulfillmentType='delivered',deliveryFee=500,overallTotal=1700)
    monkeypatch.setattr(prov,'now_utc',lambda:snapshot['sourceExtractedAt']+timedelta(hours=25))
    stored=flow.db.pa_procedure_catalog_snapshots.rows[-1]
    if bad=='revoked': stored['revokedAt']=now_utc()
    elif bad=='unsigned': stored.pop('signedProvenance')
    elif bad=='untrusted': stored['status']='VALIDATED'
    elif bad=='missing': flow.db.pa_procedure_catalog_snapshots.rows.pop()
    else:
        next(row for row in flow.db.pa_procedure_catalog_entries.rows
             if row['snapshotId']==snapshot['_id'] and row['procedureCode']=='PRE11')['procedureDescription']='Tampered'
    rows=await prepare(flow)
    assert rows[-1]['preparationStatus']=='BLOCKED'
    assert 'DELIVERY_CATALOG_EVIDENCE_REQUIRED' in rows[-1]['blockingReasons']
    assert rows[-1]['medicloudCode'] is None


@pytest.mark.asyncio
async def test_reserved_exception_cannot_resolve_arbitrary_unmapped_code(flow):
    with pytest.raises(DomainConflict,match='RESERVED_DELIVERY_PROCEDURE_REQUIRED'):
        await pa_mapping.canonical_procedure(flow.db,flow.snapshot['_id'],'DRG-CANON',reserved_delivery=True)
