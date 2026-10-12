"""Phase 3A boundaries, transactional fakes and blocked sockets; no live services."""
import asyncio
import csv
import io
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from core import pa_mapping_review as domain, pa_mapping
from core.pa_preparation_store import DomainConflict
from models.pa_preparation import Actor
from models.pa_mapping_review import CreateReviewExport
from api import pa_mapping_review as api
from test_pa_preparation import flow, prepare as prepare_domain, STAFF, ADMIN, review
from test_pa_preparation_api import team, prepare

ACTOR = Actor(actorId='reviewer-test', canExportMappings=True, canImportMappings=True)


def amended(data, edit=None):
    rows = list(csv.DictReader(io.StringIO(data.decode())))
    if edit: edit(rows)
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=domain.COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def proposal(rows):
    for row in rows:
        row.update(proposed_medicloud_code='DRG-CANON', proposed_medicloud_description='Suggested medication', mapping_notes='Review needed')


async def setup_export(flow):
    flow.db.pa_mapping_heads.rows[0]['activeVersion'] = None
    await prepare_domain(flow)
    request = CreateReviewExport(expectedVersion=1)
    result = await domain.create_export(flow.db, ACTOR, flow.batch['_id'], request)
    data = await domain.download_export(flow.db, ACTOR, result['exportId'])
    return result, data


@pytest.mark.asyncio
async def test_export_import_proposed_only_and_no_sensitive_data(flow):
    export, data = await setup_export(flow)
    before_orders = deepcopy(flow.db.orders.rows)
    before_heads = deepcopy(flow.db.pa_mapping_heads.rows)
    before_owners = deepcopy(flow.db.pa_reference_owners.rows)
    result = await domain.import_proposals(flow.db, ACTOR, export['exportId'], amended(data, proposal))
    assert result['status'] == 'PROPOSED_ONLY' and result['proposalCount'] == 1
    version = flow.db.pa_mapping_versions.rows[-1]
    assert version['status'] == 'PROPOSED' and version['provenance'] == 'CURSOR_PROPOSAL'
    assert 'medicloudCode' not in version and 'snapshotId' not in version and 'review' not in version
    assert version['proposedMedicloudDescription'] == 'Suggested medication'
    assert flow.db.orders.rows == before_orders
    assert flow.db.pa_mapping_heads.rows == before_heads
    assert flow.db.pa_reference_owners.rows == before_owners
    for forbidden in ('MEM-TEST', 'Must not persist', 'private', 'memberSnapshot', 'inputFingerprint', 'paNumber', 'enrollee', 'diagnosisCode'):
        assert forbidden not in data.decode()
    assert tuple(csv.DictReader(io.StringIO(data.decode())).fieldnames) == domain.COLUMNS
    assert len(flow.db.pa_preparation_events.rows) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize('field', domain.PROTECTED)
async def test_every_protected_cell_rejected_atomically(flow, field):
    export, data = await setup_export(flow)
    before = {name: deepcopy(col.rows) for name, col in flow.db.collections.items()}
    modified = amended(data, lambda rows: rows[0].update({field: 'tampered'}))
    with pytest.raises(domain.ReviewInvalid): await domain.import_proposals(flow.db, ACTOR, export['exportId'], modified)
    assert before == {name: col.rows for name, col in flow.db.collections.items()}


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['duplicate', 'missing', 'added', 'namespace', 'source', 'line'])
async def test_row_set_and_identity_integrity(flow, action):
    export, data = await setup_export(flow)
    def edit(rows):
        if action == 'duplicate': rows.append(deepcopy(rows[0]))
        elif action == 'missing': rows.clear()
        elif action == 'added': rows.append({**rows[0], 'export_row_id': str(uuid4())})
        else: rows[0][{'namespace': 'source_namespace_id', 'source': 'source_code', 'line': 'order_line_id'}[action]] = 'wrong'
    with pytest.raises(domain.ReviewInvalid): await domain.import_proposals(flow.db, ACTOR, export['exportId'], amended(data, edit))
    assert flow.db.pa_mapping_review_imports.rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [b'\xff', b'not,csv\n', b'\x00', b'x' * (domain.MAX_BYTES + 1), b''])
async def test_malformed_and_oversized(flow, bad):
    export, _ = await setup_export(flow)
    with pytest.raises(domain.ReviewInvalid): await domain.import_proposals(flow.db, ACTOR, export['exportId'], bad)


@pytest.mark.asyncio
@pytest.mark.parametrize('code,desc,notes', [(' DRG-CANON', '', ''), ('=cmd', '', ''), ('PRE11', '', ''), ('', 'Without code', ''), ('DRG-CANON', 'DRG-CANON', ''), ('DRG-CANON', '=formula', ''), ('DRG-CANON', '', 'bad\ntext')])
async def test_untrusted_text_and_proposals(flow, code, desc, notes):
    export, data = await setup_export(flow)
    payload = amended(data, lambda rows: rows[0].update(proposed_medicloud_code=code, proposed_medicloud_description=desc, mapping_notes=notes))
    with pytest.raises(domain.ReviewInvalid): await domain.import_proposals(flow.db, ACTOR, export['exportId'], payload)


@pytest.mark.asyncio
async def test_blank_remains_unresolved(flow):
    export, data = await setup_export(flow)
    before = deepcopy(flow.db.pa_mapping_versions.rows)
    result = await domain.import_proposals(flow.db, ACTOR, export['exportId'], data)
    assert result['unresolvedRows'] == 1 and result['proposalCount'] == 0
    assert flow.db.pa_mapping_versions.rows == before


@pytest.mark.asyncio
async def test_idempotent_repeat_and_conflicting_repeat(flow):
    export, data = await setup_export(flow)
    payload = amended(data, proposal)
    first = await domain.import_proposals(flow.db, ACTOR, export['exportId'], payload)
    before = {name: deepcopy(col.rows) for name, col in flow.db.collections.items()}
    assert await domain.import_proposals(flow.db, ACTOR, export['exportId'], payload) == first
    assert before == {name: col.rows for name, col in flow.db.collections.items()}
    with pytest.raises(DomainConflict): await domain.import_proposals(flow.db, ACTOR, export['exportId'], data)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['expire', 'digest', 'binding', 'order', 'line', 'owner', 'head', 'closed'])
async def test_stale_export_fails_closed(flow, change):
    export, data = await setup_export(flow)
    manifest = flow.db.pa_mapping_review_exports.rows[0]
    if change == 'expire': manifest['expiresAt'] -= timedelta(days=2)
    if change == 'digest': manifest['protectedDigest'] = 'wrong'
    if change == 'binding': manifest['bindings'][0]['identity']['sourceNamespaceId'] = 'wrong'
    if change == 'order': flow.db.orders.rows[0]['version'] += 1
    if change == 'line': flow.db.pa_preparation_lines.rows[0]['isCurrent'] = False
    if change == 'owner': flow.db.pa_preparation_contexts.rows[0]['batchId'] = 'other'
    if change == 'head': flow.db.pa_mapping_heads.rows[0]['version'] += 10
    if change == 'closed': flow.db.pa_preparation_batches.rows[0]['status'] = 'CLOSED'
    with pytest.raises((DomainConflict, domain.ReviewInvalid)):
        await domain.import_proposals(flow.db, ACTOR, export['exportId'], amended(data, proposal))
    assert not flow.db.pa_mapping_review_imports.rows


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', ['price', 'ledger', 'namespace', 'provider'])
async def test_unrelated_blocked_or_unbound_scope_excluded(flow, blocked):
    flow.db.pa_mapping_heads.rows[0]['activeVersion'] = None
    order = flow.db.orders.rows[0]
    if blocked == 'price': order['medicationSubtotal'] = 1
    if blocked == 'ledger': order['paGeneration'] = {'lines': None}
    if blocked == 'namespace': order['medications'][0].pop('sourceNamespaceId')
    if blocked == 'provider': flow.db.aggregator_users.rows[0]['providerId'] = None
    await prepare_domain(flow)
    result = await domain.create_export(flow.db, ACTOR, flow.batch['_id'], CreateReviewExport(expectedVersion=1))
    assert result['status'] == 'EMPTY' and result['exportId'] is None
    assert result['skippedRows'] == 1


@pytest.mark.asyncio
async def test_unavailable_member_still_independent_mapping_review(flow):
    flow.db.pa_mapping_heads.rows[0]['activeVersion'] = None
    await prepare_domain(flow, evidence=None)
    result = await domain.create_export(flow.db, ACTOR, flow.batch['_id'], CreateReviewExport(expectedVersion=1))
    assert result['rowCount'] == 1


@pytest.mark.asyncio
async def test_concurrent_import_same_content_no_duplicates(flow):
    export, data = await setup_export(flow)
    payload = amended(data, proposal)
    results = await asyncio.gather(*(domain.import_proposals(flow.db, ACTOR, export['exportId'], payload) for _ in range(2)))
    assert results[0] == results[1]
    assert len(flow.db.pa_mapping_review_imports.rows) == 1


@pytest.mark.asyncio
async def test_concurrent_conflicting_import(flow):
    export, data = await setup_export(flow)
    results = await asyncio.gather(domain.import_proposals(flow.db, ACTOR, export['exportId'], amended(data, proposal)),
        domain.import_proposals(flow.db, ACTOR, export['exportId'], data), return_exceptions=True)
    assert sum(isinstance(item, DomainConflict) for item in results) == 1
    assert len(flow.db.pa_mapping_review_imports.rows) == 1


@pytest.mark.asyncio
async def test_transaction_rollback(flow):
    export, data = await setup_export(flow)
    before = {name: deepcopy(col.rows) for name, col in flow.db.collections.items()}
    flow.db.pa_mapping_review_imports.fail_insert = True
    with pytest.raises(RuntimeError): await domain.import_proposals(flow.db, ACTOR, export['exportId'], amended(data, proposal))
    assert before == {name: col.rows for name, col in flow.db.collections.items()}


@pytest.mark.asyncio
async def test_domain_permissions_required(flow):
    with pytest.raises(PermissionError):
        await domain.create_export(flow.db, STAFF, flow.batch['_id'], CreateReviewExport(expectedVersion=0))
    with pytest.raises(PermissionError): await domain.import_proposals(flow.db, STAFF, str(uuid4()), b'')


def register(team):
    team.app.include_router(api.router, prefix='/api')
    return '/api/pa-mapping-review'


def api_export(team):
    base = register(team)
    team.staff['permissions'] = [api.EXPORT, api.IMPORT, 'PA_PREPARATION_WRITE']
    team.flow.db.pa_mapping_heads.rows[0]['activeVersion'] = None
    assert prepare(team).status_code == 200
    response = team.client.post(f'{base}/batches/{team.flow.batch["_id"]}/exports', json={'expectedVersion': 1})
    assert response.status_code == 200, response.text
    export_id = response.json()['exportId']
    path = f'{base}/exports/{export_id}'
    download = team.client.get(path + '/download')
    assert download.status_code == 200 and download.headers['cache-control'] == 'no-store'
    return path, download.content


def test_api_roundtrip_and_status(team):
    path, data = api_export(team)
    assert team.client.get(path + '/import-status').json()['status'] == 'NOT_IMPORTED'
    result = team.client.post(path + '/imports', content=amended(data, proposal), headers={'content-type': 'text/csv'})
    assert result.status_code == 200 and result.json()['status'] == 'PROPOSED_ONLY'
    assert team.client.get(path + '/import-status').json()['importId'] == result.json()['importId']


@pytest.mark.parametrize('kind', ['create', 'download', 'upload', 'status'])
def test_api_permissions_cannot_be_forged(team, kind):
    path, data = api_export(team)
    team.staff['permissions'] = []
    if kind == 'create': result = team.client.post(f'/api/pa-mapping-review/batches/{team.flow.batch["_id"]}/exports', json={'expectedVersion': 1})
    elif kind == 'download': result = team.client.get(path + '/download')
    elif kind == 'status': result = team.client.get(path + '/import-status')
    else: result = team.client.post(path + '/imports', content=data, headers={'content-type': 'text/csv', 'PA_MAPPING_IMPORT': 'true'})
    assert result.status_code == 403
    team.client.cookies.clear()
    assert team.client.get(path + '/download').status_code == 401


def test_separate_export_import_permissions(team):
    path, data = api_export(team)
    team.staff['permissions'] = [api.EXPORT]
    assert team.client.get(path + '/download').status_code == 200
    assert team.client.post(path + '/imports', content=data, headers={'content-type': 'text/csv'}).status_code == 403
    team.staff['permissions'] = [api.IMPORT]
    assert team.client.get(path + '/download').status_code == 403
    assert team.client.post(path + '/imports', content=data, headers={'content-type': 'text/csv'}).status_code == 200


def test_api_safe_invalid_requests(team):
    path, data = api_export(team)
    assert team.client.post(path + '/imports', content=data, headers={'content-type': 'application/json'}).status_code == 415
    assert team.client.post(path + '/imports', content=b'x' * (domain.MAX_BYTES + 1), headers={'content-type': 'text/csv'}).status_code == 413
    response = team.client.post(f'/api/pa-mapping-review/batches/{team.flow.batch["_id"]}/exports', json={'expectedVersion': 1, 'actor': 'SECRET', 'permissions': [api.EXPORT]})
    assert response.status_code == 422 and 'SECRET' not in response.text


def test_no_issuepa_dependency_or_submission_routes():
    import ast
    from pathlib import Path
    for module in (api, domain):
        tree = ast.parse(Path(module.__file__).read_text())
        assert not any('issue_pa' in ast.unparse(node) for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)))
    assert len(api.router.routes) == 4


@pytest.mark.asyncio
async def test_active_head_remains_unchanged_and_domain_versions_stay_usable(flow):
    # A changed description needs review despite an existing ACTIVE mapping.
    flow.db.orders.rows[0]['medications'][0]['name'] = 'Revised source medication description'
    await prepare_domain(flow)
    result = await domain.create_export(flow.db, ACTOR, flow.batch['_id'], CreateReviewExport(expectedVersion=1))
    data = await domain.download_export(flow.db, ACTOR, result['exportId'])
    before = deepcopy(flow.db.pa_mapping_heads.rows)
    active_before = deepcopy(flow.db.pa_mapping_versions.rows[1])
    await domain.import_proposals(flow.db, ACTOR, result['exportId'], amended(data, proposal))
    assert flow.db.pa_mapping_heads.rows == before
    assert flow.db.pa_mapping_versions.rows[1] == active_before
    assert flow.db.pa_mapping_versions.rows[-1]['version'] == 3
    assert flow.db.pa_mapping_versions.rows[-1]['status'] == 'PROPOSED'
    # Existing explicit lifecycle operations skip immutable pending versions.
    retired = await pa_mapping.revise_mapping(flow.db, ADMIN, flow.mapping['mappingId'], 2, 'RETIRED')
    assert retired['version'] == 4 and retired['activeVersion'] is None


@pytest.mark.asyncio
async def test_cross_export_concurrent_conflicting_imports(flow):
    first, data = await setup_export(flow)
    second = await domain.create_export(flow.db, ACTOR, flow.batch['_id'], CreateReviewExport(expectedVersion=1))
    other_data = await domain.download_export(flow.db, ACTOR, second['exportId'])
    other_payload = amended(other_data, lambda rows: rows[0].update(proposed_medicloud_code='DIFFERENT'))
    results = await asyncio.gather(domain.import_proposals(flow.db, ACTOR, first['exportId'], amended(data, proposal)),
        domain.import_proposals(flow.db, ACTOR, second['exportId'], other_payload), return_exceptions=True)
    assert sum(isinstance(result, DomainConflict) for result in results) == 1
    assert len(flow.db.pa_mapping_review_imports.rows) == 1


@pytest.mark.asyncio
async def test_stored_revision_tampering_rejected(flow):
    result, data = await setup_export(flow)
    flow.db.pa_preparation_lines.rows[0]['revision'] += 1
    with pytest.raises(DomainConflict): await domain.import_proposals(flow.db, ACTOR, result['exportId'], data)


@pytest.mark.asyncio
async def test_multiple_rows_rollback_no_partial_proposals(flow):
    export, data = await setup_export(flow)
    # Failing audit persistence after inserting a proposal must undo all writes.
    before = {name: deepcopy(col.rows) for name, col in flow.db.collections.items()}
    flow.db.pa_preparation_events.fail_insert = True
    with pytest.raises(RuntimeError): await domain.import_proposals(flow.db, ACTOR, export['exportId'], amended(data, proposal))
    assert before == {name: col.rows for name, col in flow.db.collections.items()}


def test_csv_formula_source_escaping():
    assert domain.cell('=formula') == "'=formula"
    assert domain.cell('  +formula') == "'  +formula"
    assert domain.cell('ordinary medication') == 'ordinary medication'


@pytest.mark.asyncio
async def test_export_scope_bounds_and_stale_create(flow):
    flow.db.pa_mapping_heads.rows[0]['activeVersion'] = None
    await prepare_domain(flow)
    flow.db.orders.rows[0]['version'] += 1
    result = await domain.create_export(flow.db, ACTOR, flow.batch['_id'], CreateReviewExport(expectedVersion=1))
    assert result['status'] == 'EMPTY' and result['skipReasons'] == {'PREPARATION_STALE': 1}
    assert not flow.db.pa_mapping_review_exports.rows


@pytest.mark.asyncio
async def test_new_namespace_proposal_uses_existing_mapping_authority(flow):
    flow.db.orders.rows[0]['medications'][0]['sourceNamespaceId'] = 'other-supplier'
    await prepare_domain(flow)
    result = await domain.create_export(flow.db, ACTOR, flow.batch['_id'], CreateReviewExport(expectedVersion=1))
    data = await domain.download_export(flow.db, ACTOR, result['exportId'])
    await domain.import_proposals(flow.db, ACTOR, result['exportId'], amended(data, proposal))
    head = flow.db.pa_mapping_heads.rows[-1]
    assert head['sourceNamespaceId'] == 'other-supplier' and head['activeVersion'] is None
    assert head['version'] == 1
    assert flow.db.pa_mapping_versions.rows[-1]['mappingId'] == head['_id']
    assert flow.db.pa_mapping_heads.rows[0]['activeVersion'] == 2


async def two_lines(flow, same_identity=False):
    flow.db.pa_mapping_heads.rows[0]['activeVersion'] = None
    order = flow.db.orders.rows[0]
    second = {**order['medications'][0], 'lineId': 'line-2'}
    if not same_identity: second['sourceNamespaceId'] = 'supplier-b'
    order['medications'].append(second)
    order['finalProcedurePrices'].append({**order['finalProcedurePrices'][0], 'medicationLineId': 'line-2'})
    order['medicationSubtotal'] = order['overallTotal'] = 2400
    await prepare_domain(flow)
    export = await domain.create_export(flow.db, ACTOR, flow.batch['_id'], CreateReviewExport(expectedVersion=1))
    return export, await domain.download_export(flow.db, ACTOR, export['exportId'])


@pytest.mark.asyncio
async def test_multiple_namespace_proposals_rollback_together(flow, monkeypatch):
    result, data = await two_lines(flow)
    before = {name: deepcopy(col.rows) for name, col in flow.db.collections.items()}
    original = domain.append_proposal
    calls = 0
    async def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2: raise DomainConflict('INJECTED_CONFLICT')
        return await original(*args, **kwargs)
    monkeypatch.setattr(domain, 'append_proposal', fail_second)
    with pytest.raises(DomainConflict): await domain.import_proposals(flow.db, ACTOR, result['exportId'], amended(data, proposal))
    assert calls == 2
    assert before == {name: col.rows for name, col in flow.db.collections.items()}


@pytest.mark.asyncio
async def test_same_identity_rows_coalesce_or_conflict(flow):
    result, data = await two_lines(flow, same_identity=True)
    def disagree(rows):
        proposal(rows)
        rows[1]['proposed_medicloud_code'] = 'OTHER'
    with pytest.raises(domain.ReviewInvalid, match='CONFLICTING_NAMESPACE'):
        await domain.import_proposals(flow.db, ACTOR, result['exportId'], amended(data, disagree))
    response = await domain.import_proposals(flow.db, ACTOR, result['exportId'], amended(data, proposal))
    assert response['proposedRows'] == 2 and response['proposalCount'] == 1
    assert len(flow.db.pa_mapping_versions.rows[-1]['exportRowIds']) == 2


@pytest.mark.asyncio
async def test_paginated_exports_have_exact_nonoverlapping_scope(flow):
    await two_lines(flow)
    first = await domain.create_export(flow.db, ACTOR, flow.batch['_id'], CreateReviewExport(expectedVersion=1, limit=1))
    assert first['rowCount'] == 1 and first['nextCursor']
    second = await domain.create_export(flow.db, ACTOR, flow.batch['_id'], CreateReviewExport(expectedVersion=1, limit=1, after=first['nextCursor']))
    assert second['rowCount'] == 1 and second['nextCursor'] is None
    rows = flow.db.pa_mapping_review_exports.rows[-2:]
    assert rows[0]['rows'][0]['preparation_line_id'] != rows[1]['rows'][0]['preparation_line_id']


@pytest.mark.asyncio
async def test_known_csv_extra_headers_or_fields_rejected(flow):
    export, data = await setup_export(flow)
    for payload in (data.replace(b'mapping_notes', b'mapping_notes,permissions', 1), data + b'extra,line\r\n'):
        with pytest.raises(domain.ReviewInvalid): await domain.import_proposals(flow.db, ACTOR, export['exportId'], payload)


@pytest.mark.asyncio
async def test_missing_description_is_not_invented_or_validated(flow):
    result, data = await setup_export(flow)
    await domain.import_proposals(flow.db, ACTOR, result['exportId'], amended(data, lambda rows: rows[0].update(proposed_medicloud_code='UNKNOWN-CODE')))
    version = flow.db.pa_mapping_versions.rows[-1]
    assert version['proposedMedicloudDescription'] is None
    assert version['status'] == 'PROPOSED' and 'medicloudDescription' not in version


@pytest.mark.asyncio
async def test_transaction_driver_conflict_translated(flow, monkeypatch):
    from pymongo.errors import OperationFailure
    result, data = await setup_export(flow)
    failure = OperationFailure('driver detail must stay private', code=112)
    monkeypatch.setattr(flow.db.pa_mapping_review_imports, 'insert_one', AsyncMock(side_effect=failure))
    before = {name: deepcopy(col.rows) for name, col in flow.db.collections.items()}
    with pytest.raises(DomainConflict, match='TRANSACTION_CONFLICT_REFRESH_REQUIRED'):
        await domain.import_proposals(flow.db, ACTOR, result['exportId'], amended(data, proposal))
    assert before == {name: col.rows for name, col in flow.db.collections.items()}


def test_api_does_not_leak_conflicts_or_upload_text(team):
    path, data = api_export(team)
    invalid = amended(data, lambda rows: rows[0].update(source_namespace_id='SECRET-PAYLOAD'))
    response = team.client.post(path + '/imports', content=invalid, headers={'content-type': 'text/csv'})
    assert response.status_code == 400 and 'SECRET-PAYLOAD' not in response.text
    team.flow.db.orders.rows[0]['version'] += 1
    assert team.client.get(path + '/download').status_code == 409
    assert team.client.post(path + '/imports', content=data, headers={'content-type': 'text/csv'}).status_code == 409


def test_export_limit_is_bounded_at_api(team):
    register(team)
    team.staff['permissions'] = [api.EXPORT]
    response = team.client.post(f'/api/pa-mapping-review/batches/{team.flow.batch["_id"]}/exports', json={'expectedVersion': 0, 'limit': 101})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_nontransactional_database_cannot_import(flow, monkeypatch):
    result, data = await setup_export(flow)
    before = {name: deepcopy(col.rows) for name, col in flow.db.collections.items()}
    monkeypatch.setattr(flow.db.client, 'start_session', AsyncMock(side_effect=RuntimeError('Transactions unavailable')))
    with pytest.raises(RuntimeError): await domain.import_proposals(flow.db, ACTOR, result['exportId'], data)
    assert before == {name: col.rows for name, col in flow.db.collections.items()}


def test_creation_body_size_bounded_without_content_length(team):
    register(team)
    team.staff['permissions'] = [api.EXPORT]
    path = f'/api/pa-mapping-review/batches/{team.flow.batch["_id"]}/exports'
    response = team.client.post(path, content=iter([b' ' * 3000, b' ' * 3000]), headers={'content-type': 'application/json'})
    assert response.status_code == 413
    assert not team.flow.db.pa_mapping_review_exports.rows


def test_streaming_upload_size_bounded_without_content_length(team):
    path, _ = api_export(team)
    response = team.client.post(path + '/imports', content=iter([b'x' * domain.MAX_BYTES, b'x']), headers={'content-type': 'text/csv'})
    assert response.status_code == 413
    assert not team.flow.db.pa_mapping_review_imports.rows


@pytest.mark.asyncio
async def test_empty_bounded_scan_returns_continuation(flow, monkeypatch):
    flow.db.pa_mapping_heads.rows[0]['activeVersion'] = None
    await prepare_domain(flow)
    row = flow.db.pa_preparation_lines.rows[0]
    row['_id'] = '00000000-0000-0000-0000-000000000001'
    other = {**deepcopy(row), '_id': '00000000-0000-0000-0000-000000000002'}
    flow.db.pa_preparation_lines.rows.append(other)
    monkeypatch.setattr(domain, 'SCAN_ROWS', 1)
    monkeypatch.setattr(domain, 'actionable', lambda live: False)
    result = await domain.create_export(flow.db, ACTOR, flow.batch['_id'], CreateReviewExport(expectedVersion=1))
    assert result['status'] == 'EMPTY_SCAN' and result['exportId'] is None
    assert result['nextCursor'] == row['_id'] and result['rowCount'] == 0
    assert not flow.db.pa_mapping_review_exports.rows


@pytest.mark.asyncio
async def test_manifest_bson_roundtrip_preserves_integrity_download_import_and_expiry(flow, monkeypatch):
    from bson import BSON
    from bson.codec_options import CodecOptions
    from datetime import timezone
    clock = domain.now_utc().replace(microsecond=123456)
    monkeypatch.setattr(domain, 'now_utc', lambda: clock)
    export, data = await setup_export(flow)
    original = flow.db.pa_mapping_review_exports.rows[0]
    assert original['createdAt'].microsecond == 123000
    assert original['expiresAt'].microsecond == 123000
    assert original['expiresAt'] - original['createdAt'] == timedelta(hours=24)
    decoded = BSON.encode(original).decode(codec_options=CodecOptions(tz_aware=True, tzinfo=timezone.utc))
    assert domain.manifest_digest(decoded) == original['manifestDigest']
    flow.db.pa_mapping_review_exports.rows[0] = decoded
    domain.check_manifest(decoded)
    assert await domain.download_export(flow.db, ACTOR, export['exportId']) == data
    tampered = amended(data, lambda rows: rows[0].update(source_code='CHANGED'))
    with pytest.raises(domain.ReviewInvalid, match='PROTECTED_CELLS_CHANGED'):
        await domain.import_proposals(flow.db, ACTOR, export['exportId'], tampered)
    result = await domain.import_proposals(flow.db, ACTOR, export['exportId'], amended(data, proposal))
    assert result['status'] == 'PROPOSED_ONLY'
    altered = deepcopy(decoded)
    altered['createdAt'] += timedelta(microseconds=1)
    with pytest.raises(DomainConflict, match='EXPORT_INTEGRITY_FAILURE'): domain.check_manifest(altered)
    altered = deepcopy(decoded)
    altered['expiresAt'] += timedelta(milliseconds=1)
    with pytest.raises(DomainConflict, match='EXPORT_INTEGRITY_FAILURE'): domain.check_manifest(altered)
    monkeypatch.setattr(domain, 'now_utc', lambda: decoded['expiresAt'])
    with pytest.raises(DomainConflict, match='EXPORT_EXPIRED'):
        await domain.download_export(flow.db, ACTOR, export['exportId'])
    with pytest.raises(DomainConflict, match='EXPORT_EXPIRED'):
        await domain.import_proposals(flow.db, ACTOR, export['exportId'], amended(data, proposal))


async def three_review_rows(flow):
    flow.db.pa_mapping_heads.rows[0]['activeVersion'] = None
    order = flow.db.orders.rows[0]
    base = deepcopy(order['medications'][0])
    order['medications'] = [{**base, 'lineId': f'line-{index}', 'sourceNamespaceId': f'supplier-{index}'} for index in (1, 2, 3)]
    order['finalProcedurePrices'] = [{'medicationLineId': f'line-{index}', 'procedureCode': 'LOCAL-1', 'amount': 1200} for index in (1, 2, 3)]
    order['medicationSubtotal'] = order['overallTotal'] = 3600
    await prepare_domain(flow)
    for index, row in enumerate(flow.db.pa_preparation_lines.rows, 1):
        row['_id'] = f'00000000-0000-0000-0000-{index:012d}'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['stale', 'malformed', 'csv', 'namespace'])
async def test_bad_candidate_isolated_with_safe_skip_counts(flow, bad):
    if bad == 'csv':
        # Representable in preparation storage, forbidden in the strict review CSV.
        flow.db.orders.rows[0]['medications'][0]['name'] = 'PRIVATE\nSECRET'
    await three_review_rows(flow)
    if bad == 'csv':
        # Keep the outer two valid; recalculate the middle line's source evidence.
        order = flow.db.orders.rows[0]
        order['medications'][0]['name'] = order['medications'][2]['name'] = 'Medication A'
        await prepare_domain(flow)
        # Remove historical noncurrent revisions; the three current rows remain.
        flow.db.pa_preparation_lines.rows = [row for row in flow.db.pa_preparation_lines.rows if row['isCurrent']]
        flow.db.pa_preparation_lines.rows.sort(key=lambda row: row['orderLineId'])
        for index, row in enumerate(flow.db.pa_preparation_lines.rows, 1):
            row['_id'] = f'00000000-0000-0000-0000-{index:012d}'
    if bad == 'stale': flow.db.orders.rows[0]['medications'][1]['name'] = 'PRIVATE STALE CONTENT'
    if bad == 'malformed': flow.db.pa_preparation_lines.rows[1]['sourceIdentityType'] = {'SECRET': 'PRIVATE'}
    if bad == 'namespace':
        flow.db.orders.rows[0]['medications'][1]['sourceNamespaceId'] = ' supplier-invalid '
        await prepare_domain(flow)
        flow.db.pa_preparation_lines.rows = [row for row in flow.db.pa_preparation_lines.rows if row['isCurrent']]
    before = deepcopy(flow.db.pa_preparation_lines.rows)
    version = flow.db.pa_preparation_batches.rows[0]['version']
    export = await domain.create_export(flow.db, ACTOR, flow.batch['_id'], CreateReviewExport(expectedVersion=version))
    assert export['rowCount'] == 2 and export['skippedRows'] == 1
    expected = {'stale': 'PREPARATION_STALE', 'malformed': 'PREPARATION_ROW_INVALID',
                'csv': 'CSV_ROW_UNREPRESENTABLE', 'namespace': 'PREPARATION_ROW_INVALID'}[bad]
    assert export['skipReasons'] == {expected: 1}
    assert 'PRIVATE' not in str(export) and 'SECRET' not in str(export)
    assert flow.db.pa_preparation_lines.rows == before
    manifest = flow.db.pa_mapping_review_exports.rows[-1]
    assert {row['order_line_id'] for row in manifest['rows']} == {'line-1', 'line-3'}
    assert len(manifest['bindings']) == 2
    domain.check_manifest(manifest)
    data = await domain.download_export(flow.db, ACTOR, export['exportId'])
    def add_skipped(rows): rows.append({**rows[0], 'export_row_id': str(uuid4()), 'order_line_id': 'line-2'})
    with pytest.raises(domain.ReviewInvalid):
        await domain.import_proposals(flow.db, ACTOR, export['exportId'], amended(data, add_skipped))
    with pytest.raises(domain.ReviewInvalid):
        await domain.import_proposals(flow.db, ACTOR, export['exportId'], amended(data, lambda rows: rows[0].update(source_description='changed')))
    assert (await domain.import_proposals(flow.db, ACTOR, export['exportId'], amended(data, proposal)))['proposedRows'] == 2
    event = next(event for event in reversed(flow.db.pa_preparation_events.rows) if event['eventType'] == 'mapping_review_exported')
    assert event['skippedRows'] == 1 and event['skipReasons'] == export['skipReasons']


@pytest.mark.asyncio
async def test_all_invalid_candidates_are_controlled_empty_and_audited(flow):
    await three_review_rows(flow)
    for row in flow.db.pa_preparation_lines.rows: row['revision'] = {'SECRET': 'PRIVATE'}
    before = deepcopy(flow.db.pa_preparation_lines.rows)
    result = await domain.create_export(flow.db, ACTOR, flow.batch['_id'], CreateReviewExport(expectedVersion=1))
    assert result['status'] == 'EMPTY' and result['exportId'] is None and result['rowCount'] == 0
    assert result['skippedRows'] == 3 and result['skipReasons'] == {'PREPARATION_ROW_INVALID': 3}
    assert flow.db.pa_preparation_lines.rows == before
    assert not flow.db.pa_mapping_review_exports.rows
    assert flow.db.pa_preparation_events.rows[-1]['skipReasons'] == result['skipReasons']
    assert 'PRIVATE' not in str(result) and 'SECRET' not in str(result)


@pytest.mark.asyncio
async def test_export_driver_failures_abort_not_skip(flow, monkeypatch):
    from pymongo.errors import OperationFailure
    await three_review_rows(flow)
    before = {name: deepcopy(col.rows) for name, col in flow.db.collections.items()}
    monkeypatch.setattr(domain, 'live_line', AsyncMock(side_effect=OperationFailure('PRIVATE DRIVER ERROR', code=112)))
    with pytest.raises(DomainConflict, match='TRANSACTION_CONFLICT_REFRESH_REQUIRED'):
        await domain.create_export(flow.db, ACTOR, flow.batch['_id'], CreateReviewExport(expectedVersion=1))
    assert before == {name: col.rows for name, col in flow.db.collections.items()}


@pytest.mark.asyncio
async def test_skip_diagnostics_bound_to_manifest_digest(flow):
    await three_review_rows(flow)
    flow.db.orders.rows[0]['medications'][1]['name'] = 'Stale source'
    await domain.create_export(flow.db, ACTOR, flow.batch['_id'], CreateReviewExport(expectedVersion=1))
    manifest = deepcopy(flow.db.pa_mapping_review_exports.rows[-1])
    manifest['skipReasons'] = {'PREPARATION_STALE': 2}
    with pytest.raises(DomainConflict, match='EXPORT_INTEGRITY_FAILURE'): domain.check_manifest(manifest)
