"""Bounded, transactional review manifests and untrusted PROPOSED versions only."""
import csv
import hashlib
import io
import json
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from bson import ObjectId
from core import pa_preparation as preparation
from core.pa_mapping import normalized_code, require_permission, next_mapping_version
from core.pa_preparation_store import DomainConflict, event, now_utc, transaction
from core.pa_source_namespaces import is_reserved_namespace
from models.pa_preparation import MappingIdentity, MappingProposal, MemberEvidence, PreparationLineEvidence

MAX_ROWS = 100
SCAN_ROWS = 200
MAX_BYTES = 512 * 1024
EXPORT_LIFETIME = timedelta(hours=24)
EDITABLE = ('proposed_medicloud_code', 'proposed_medicloud_description', 'mapping_notes')
PROTECTED = ('schema_version', 'export_id', 'export_row_id', 'expires_at', 'batch_id',
    'preparation_line_id', 'preparation_revision', 'order_id', 'order_line_id',
    'referring_provider_id', 'aggregator_id', 'issue_pa_provider_id', 'source_namespace_id',
    'source_identity_type', 'source_identity_key', 'source_code', 'source_description',
    'existing_mapping_id', 'existing_mapping_version', 'existing_mapping_status')
COLUMNS = PROTECTED + EDITABLE
IDENTITY = ('sourceNamespaceId', 'sourceIdentityType', 'sourceIdentityKey')


class ReviewInvalid(ValueError):
    """Safe, static error codes only; never attach uploaded content."""


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def text(value, limit=1000):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ReviewInvalid('INVALID_REVIEW_TEXT')
    # Prevent spreadsheet formula injection. Stored source cells are escaped separately.
    return value


def cell(value):
    value = '' if value is None else str(value)
    text(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value


def csv_bytes(rows):
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator='\r\n')
    writer.writeheader()
    writer.writerows({**row, **{key: '' for key in EDITABLE}} for row in rows)
    result = output.getvalue().encode('utf-8')
    if len(result) > MAX_BYTES:
        raise ReviewInvalid('EXPORT_TOO_LARGE')
    return result


async def live_line(db, row, session):
    """Check against current sources, with stored receipt at its calculation time.

    No claim of current member freshness is made. Missing/expired member evidence
    may coexist with an independently actionable mapping review.
    """
    PreparationLineEvidence.model_validate({k: row[k] for k in PreparationLineEvidence.model_fields})
    if str(UUID(row['_id'])) != row['_id'] or str(UUID(row['batchId'])) != row['batchId']:
        raise DomainConflict('PREPARATION_ROW_INVALID')
    if not isinstance(row.get('calculatedAt'), datetime) or row['calculatedAt'].tzinfo is None:
        raise DomainConflict('PREPARATION_ROW_INVALID')
    if type(row.get('revision')) is not int or row['revision'] < 1:
        raise DomainConflict('PREPARATION_ROW_INVALID')
    if not row.get('isCurrent'):
        raise DomainConflict('PREPARATION_STALE')
    owner = await db.pa_preparation_contexts.find_one({'_id': row['orderId']}, session=session)
    if not owner or owner['batchId'] != row['batchId']:
        raise DomainConflict('PREPARATION_STALE')
    order = await db.orders.find_one({'_id': ObjectId(row['orderId'])}, session=session)
    if not order:
        raise DomainConflict('PREPARATION_STALE')
    aggregator = await db.aggregator_users.find_one({'_id': ObjectId(order['winnerId'])}, session=session) if ObjectId.is_valid(order.get('winnerId')) else None
    evidence = MemberEvidence.model_validate(row['memberSnapshot']) if row.get('memberSnapshot') else None
    calculated = await preparation.calculate(db, order, aggregator, evidence, session, row['calculatedAt'])
    live = next((item for item in calculated if item['orderLineId'] == row['orderLineId']), None)
    if not live or live['inputFingerprint'] != row['inputFingerprint']:
        raise DomainConflict('PREPARATION_STALE')
    return live


def actionable(live):
    # System namespaces (verified MediCloud codes, delivery) never enter manual review.
    return (live['mappingValidation'] == 'REVIEW_REQUIRED'
        and bool(live.get('sourceNamespaceId')) and not is_reserved_namespace(live.get('sourceNamespaceId'))
        and bool(live.get('sourceIdentityKey'))
        and bool(live.get('sourceDescription', '').strip())
        and not (set(live['blockingReasons']) - {'MEMBER_EVIDENCE_INVALID_OR_STALE', 'MAPPING_CATALOG_EVIDENCE_INVALID'})
        and 'SOURCE_NAMESPACE_OR_IDENTITY_REQUIRED' not in live['reviewReasons'])


async def mapping_binding(db, row, session):
    identity = MappingIdentity(**{key: row[key] for key in IDENTITY}).model_dump()
    head = await db.pa_mapping_heads.find_one(identity, session=session)
    if not head:
        return {'mappingId': None, 'version': None, 'activeVersion': None, 'status': 'UNMAPPED', 'latestVersion': None}
    version = await db.pa_mapping_versions.find_one({'mappingId': head['_id'], 'version': head['version']}, session=session)
    if not version:
        raise DomainConflict('MAPPING_VERSION_CONFLICT')
    latest = await next_mapping_version(db, head['_id'], session) - 1
    return {'mappingId': head['_id'], 'version': head['version'], 'activeVersion': head.get('activeVersion'), 'status': version['status'], 'latestVersion': latest}


async def batch_guard(db, session, batch_id, version=None):
    batch = await db.pa_preparation_batches.find_one({'_id': batch_id}, session=session)
    if not batch or batch.get('status') not in {'OPEN', 'AUDITING'}:
        raise DomainConflict('ACTIVE_BATCH_REQUIRED')
    if version is not None and batch['version'] != version:
        raise DomainConflict('BATCH_VERSION_CONFLICT')
    # Serialize against prepare/recalculate/close without changing preparation version.
    result = await db.pa_preparation_batches.update_one({'_id': batch_id, 'version': batch['version'], 'status': batch['status']},
        {'$inc': {'mappingReviewGuard': 1}}, session=session)
    if result.matched_count != 1:
        raise DomainConflict('BATCH_VERSION_CONFLICT')
    return batch


def utc_milliseconds(value):
    return value.astimezone(timezone.utc).replace(microsecond=(value.microsecond // 1000) * 1000)


def canonical_timestamp(value):
    # Exact millisecond timestamps only; verification never rounds altered data.
    if not isinstance(value, datetime) or value.tzinfo is None or value.microsecond % 1000:
        raise DomainConflict('EXPORT_INTEGRITY_FAILURE')
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds')


def skip_reason(exc):
    if isinstance(exc, DomainConflict):
        return {'PREPARATION_STALE': 'PREPARATION_STALE',
                'MAPPING_VERSION_CONFLICT': 'MAPPING_VERSION_INVALID'}.get(str(exc), 'PREPARATION_ROW_INVALID')
    if isinstance(exc, ReviewInvalid):
        return 'CSV_SIZE_LIMIT_EXCEEDED' if str(exc) == 'EXPORT_TOO_LARGE' else 'CSV_ROW_UNREPRESENTABLE'
    return 'PREPARATION_ROW_INVALID'


def export_view(manifest):
    return {'exportId': manifest['_id'], 'batchId': manifest['batchId'], 'status': manifest['status'],
        'createdAt': manifest['createdAt'], 'expiresAt': manifest['expiresAt'], 'rowCount': len(manifest['rows']),
        'contentDigest': manifest['fileDigest'], 'nextCursor': manifest.get('nextCursor'),
        'skippedRows': manifest['skippedRows'], 'skipReasons': manifest['skipReasons']}


async def create_export(db, actor, batch_id, request):
    require_permission(actor, 'canExportMappings')
    async with transaction(db) as session:
        await batch_guard(db, session, batch_id, request.expectedVersion)
        query = {'batchId': batch_id, 'isCurrent': True}
        if request.after: query['_id'] = {'$gt': str(request.after)}
        candidates = await db.pa_preparation_lines.find(query, session=session).sort('_id', 1).limit(SCAN_ROWS + 1).to_list(SCAN_ROWS + 1)
        export_id, created = str(uuid4()), utc_milliseconds(now_utc())
        expiry = utc_milliseconds(created + EXPORT_LIFETIME)
        rows, bindings, scanned, last = [], [], 0, None
        skips = {}
        for row in candidates[:SCAN_ROWS]:
            scanned += 1
            try:
                # Cursor diagnostics must never echo malformed stored identifiers.
                if str(UUID(row['_id'])) == row['_id']: last = row['_id']
                live = await live_line(db, row, session)
                if not actionable(live):
                    skips['NOT_ACTIONABLE_MAPPING_REVIEW'] = skips.get('NOT_ACTIONABLE_MAPPING_REVIEW', 0) + 1
                    continue
                # Validate namespace/type/key independently of whether a head exists.
                MappingIdentity(**{key: live[key] for key in IDENTITY})
                binding = await mapping_binding(db, live, session)
                values = ('1', export_id, str(uuid4()), canonical_timestamp(expiry), batch_id, row['_id'], row['revision'],
                    row['orderId'], row['orderLineId'], live['referringProviderId'], live['aggregatorId'], live['issuePaProviderId'],
                    live['sourceNamespaceId'], live['sourceIdentityType'], live['sourceIdentityKey'], live['sourceCode'], live['sourceDescription'],
                    binding['mappingId'], binding['version'], binding['status'])
                protected = dict(zip(PROTECTED, map(cell, values)))
                # Keep the existing file cap; an oversized candidate does not poison others.
                csv_bytes(rows + [protected])
                row_binding = {'rowId': protected['export_row_id'], 'lineId': row['_id'], 'fingerprint': row['inputFingerprint'], 'revision': row['revision'],
                    'identity': {key: live[key] for key in IDENTITY}, 'sourceCode': live['sourceCode'], 'sourceDescription': live['sourceDescription'],
                    'mappingBinding': binding}
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
                # Do not catch Mongo driver failures: the whole transaction must abort.
                reason = skip_reason(exc)
                skips[reason] = skips.get(reason, 0) + 1
                continue
            rows.append(protected)
            bindings.append(row_binding)
            if len(rows) == request.limit: break
        diagnostics = {'skippedRows': sum(skips.values()), 'skipReasons': dict(sorted(skips.items()))}
        if not rows:
            more = scanned < len(candidates)
            await event(db, session, actor, 'mapping_review_empty_scan', export_id, batchId=batch_id, rowCount=0, **diagnostics)
            return {'exportId': None, 'batchId': batch_id, 'status': 'EMPTY_SCAN' if more else 'EMPTY',
                'rowCount': 0, 'nextCursor': last if more else None, **diagnostics}
        data = csv_bytes(rows)
        manifest = {'_id': export_id, 'schemaVersion': 1, 'batchId': batch_id, 'createdAt': created,
            'createdBy': actor.actorId, 'expiresAt': expiry, 'status': 'OPEN', 'rows': rows, 'bindings': bindings,
            'protectedDigest': digest(rows), 'fileDigest': hashlib.sha256(data).hexdigest(),
            'nextCursor': last if scanned < len(candidates) else None, **diagnostics}
        manifest['manifestDigest'] = manifest_digest(manifest)
        await db.pa_mapping_review_exports.insert_one(manifest, session=session)
        await event(db, session, actor, 'mapping_review_exported', export_id, batchId=batch_id, exportId=export_id, rowCount=len(rows), **diagnostics)
    return export_view(manifest)


def manifest_digest(manifest):
    return digest({**{key: manifest[key] for key in ('rows', 'bindings', 'batchId', 'schemaVersion', 'skippedRows', 'skipReasons')},
        'createdAt': canonical_timestamp(manifest['createdAt']), 'expiresAt': canonical_timestamp(manifest['expiresAt'])})


def check_manifest(manifest):
    if manifest_digest(manifest) != manifest['manifestDigest']:
        raise DomainConflict('EXPORT_INTEGRITY_FAILURE')
    if digest(manifest['rows']) != manifest['protectedDigest'] or hashlib.sha256(csv_bytes(manifest['rows'])).hexdigest() != manifest['fileDigest']:
        raise DomainConflict('EXPORT_INTEGRITY_FAILURE')
    if now_utc() >= manifest['expiresAt']:
        raise DomainConflict('EXPORT_EXPIRED')


async def download_export(db, actor, export_id):
    require_permission(actor, 'canExportMappings')
    async with transaction(db) as session:
        manifest = await db.pa_mapping_review_exports.find_one({'_id': export_id}, session=session)
        if not manifest: raise ReviewInvalid('EXPORT_NOT_FOUND')
        check_manifest(manifest)
        await batch_guard(db, session, manifest['batchId'])
        await verify_scope(db, manifest, session)
        await event(db, session, actor, 'mapping_review_downloaded', export_id, batchId=manifest['batchId'], exportId=export_id, rowCount=len(manifest['rows']))
        return csv_bytes(manifest['rows'])


def parse_import(data, manifest):
    if not data or len(data) > MAX_BYTES:
        raise ReviewInvalid('UPLOAD_SIZE_INVALID')
    try:
        decoded = data.decode('utf-8-sig', errors='strict')
        # Strict CSV only. Embedded control/newline text is not part of this contract.
        if '\x00' in decoded: raise ValueError()
        reader = csv.DictReader(io.StringIO(decoded, newline=''), strict=True)
        if tuple(reader.fieldnames or ()) != COLUMNS: raise ValueError()
        rows = []
        for row in reader:
            if len(rows) >= MAX_ROWS or None in row or any(value is None for value in row.values()): raise ValueError()
            rows.append(row)
    except (UnicodeError, csv.Error, ValueError):
        raise ReviewInvalid('INVALID_CSV') from None
    original = {row['export_row_id']: row for row in manifest['rows']}
    if len(rows) != len(original) or len({row['export_row_id'] for row in rows}) != len(rows):
        raise ReviewInvalid('EXPORT_ROW_SET_MISMATCH')
    proposals, protected_rows = {}, {}
    for row in rows:
        identity = row['export_row_id']
        protected = {key: row[key] for key in PROTECTED}
        if identity not in original or protected != original[identity]:
            raise ReviewInvalid('PROTECTED_CELLS_CHANGED')
        protected_rows[identity] = protected
        code, desc, notes = (text(row[key], 200 if key == EDITABLE[0] else 1000) for key in EDITABLE)
        if code:
            if (code != code.strip() or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', code)
                    or normalized_code(code) == 'PRE11'):
                raise ReviewInvalid('INVALID_PROPOSED_CODE')
            if desc and (desc.lstrip().startswith(('=', '+', '-', '@')) or desc.strip().casefold() == code.casefold()):
                raise ReviewInvalid('INVALID_PROPOSED_DESCRIPTION')
        elif desc:
            raise ReviewInvalid('DESCRIPTION_WITHOUT_PROPOSED_CODE')
        if notes.lstrip().startswith(('=', '+', '-', '@')): raise ReviewInvalid('INVALID_MAPPING_NOTES')
        proposals[identity] = {'code': code, 'description': desc, 'notes': notes}
    ordered = [protected_rows[row['export_row_id']] for row in manifest['rows']]
    if digest(ordered) != manifest['protectedDigest']:
        raise ReviewInvalid('PROTECTED_DIGEST_MISMATCH')
    return proposals, digest(proposals)


async def verify_scope(db, manifest, session):
    for binding in manifest['bindings']:
        row = await db.pa_preparation_lines.find_one({'_id': binding['lineId'], 'batchId': manifest['batchId']}, session=session)
        if not row or row['inputFingerprint'] != binding['fingerprint'] or row.get('revision') != binding['revision']:
            raise DomainConflict('PREPARATION_STALE')
        live = await live_line(db, row, session)
        if not actionable(live) or await mapping_binding(db, live, session) != binding['mappingBinding']:
            raise DomainConflict('EXPORT_SCOPE_CHANGED')
        if {key: live[key] for key in IDENTITY} != binding['identity']:
            raise DomainConflict('EXPORT_SCOPE_CHANGED')


async def append_proposal(db, actor, binding, suggestion, export_id, session, row_ids):
    """Reuse immutable versions/namespace authority. Existing heads are untouched.

    A later explicit review workflow must select this pending version; importing
    never changes the current/active pointer or invalidates preparation.
    """
    require_permission(actor, 'canImportMappings')
    if is_reserved_namespace(binding['identity'].get('sourceNamespaceId')):
        raise DomainConflict('RESERVED_SOURCE_NAMESPACE')
    proposal = MappingProposal(identity=MappingIdentity(**binding['identity']), sourceCode=binding['sourceCode'],
        sourceDescription=binding['sourceDescription'], proposedMedicloudCode=suggestion['code'], mappingSource='CURSOR')
    head = await db.pa_mapping_heads.find_one(binding['identity'], session=session)
    mapping_id = head['_id'] if head else str(uuid4())
    version = await next_mapping_version(db, mapping_id, session)
    doc = {'_id': str(uuid4()), 'mappingId': mapping_id, 'version': version, **binding['identity'],
        **proposal.model_dump(exclude={'identity'}), 'status': 'PROPOSED', 'createdAt': now_utc(), 'createdBy': actor.actorId,
        'proposedMedicloudDescription': suggestion['description'] or None, 'mappingNotes': suggestion['notes'],
        'provenance': 'CURSOR_PROPOSAL', 'exportId': export_id, 'exportRowIds': row_ids}
    await db.pa_mapping_versions.insert_one(doc, session=session)
    if not head:
        await db.pa_mapping_heads.insert_one({'_id': mapping_id, **binding['identity'], 'version': version, 'activeVersion': None}, session=session)
    await event(db, session, actor, 'mapping_proposal_imported', export_id, mappingId=mapping_id, newVersion=version,
        exportId=export_id, exportRowIds=row_ids)
    return {'mappingId': mapping_id, 'version': version, 'status': 'PROPOSED'}


def import_view(receipt):
    return {key: receipt[key] for key in ('importId', 'exportId', 'status', 'importedAt', 'proposedRows', 'unresolvedRows', 'proposalCount')}


async def import_proposals(db, actor, export_id, data):
    require_permission(actor, 'canImportMappings')
    async with transaction(db) as session:
        manifest = await db.pa_mapping_review_exports.find_one({'_id': export_id}, session=session)
        if not manifest: raise ReviewInvalid('EXPORT_NOT_FOUND')
        check_manifest(manifest)
        proposals, upload_digest = parse_import(data, manifest)
        existing = await db.pa_mapping_review_imports.find_one({'_id': export_id}, session=session)
        if existing:
            if existing['proposalDigest'] != upload_digest: raise DomainConflict('EXPORT_ALREADY_IMPORTED_DIFFERENT_CONTENT')
            return import_view(existing)
        if manifest['status'] != 'OPEN': raise DomainConflict('EXPORT_NOT_OPEN')
        await batch_guard(db, session, manifest['batchId'])
        await verify_scope(db, manifest, session)
        grouped = {}
        for binding in manifest['bindings']:
            suggestion = proposals[binding['rowId']]
            if not suggestion['code']: continue
            key = tuple(binding['identity'][field] for field in IDENTITY)
            if key in grouped:
                prior = grouped[key]
                if prior['suggestion'] != suggestion or prior['binding']['sourceDescription'] != binding['sourceDescription'] or prior['binding']['sourceCode'] != binding['sourceCode']:
                    raise ReviewInvalid('CONFLICTING_NAMESPACE_PROPOSALS')
                prior['rowIds'].append(binding['rowId'])
            else:
                grouped[key] = {'binding': binding, 'suggestion': suggestion, 'rowIds': [binding['rowId']]}
        result = await db.pa_mapping_review_exports.update_one({'_id': export_id, 'status': 'OPEN'},
            {'$set': {'status': 'IMPORTED'}}, session=session)
        if result.matched_count != 1: raise DomainConflict('EXPORT_IMPORT_CONFLICT')
        for group in grouped.values():
            await append_proposal(db, actor, group['binding'], group['suggestion'], export_id, session, group['rowIds'])
        proposed_count = sum(bool(item['code']) for item in proposals.values())
        receipt = {'_id': export_id, 'importId': str(uuid4()), 'exportId': export_id, 'status': 'PROPOSED_ONLY',
            'importedAt': now_utc(), 'importedBy': actor.actorId, 'proposalDigest': upload_digest,
            'proposedRows': proposed_count, 'unresolvedRows': len(proposals) - proposed_count, 'proposalCount': len(grouped)}
        await db.pa_mapping_review_imports.insert_one(receipt, session=session)
        await event(db, session, actor, 'mapping_review_import_completed', export_id, batchId=manifest['batchId'],
            exportId=export_id, importId=receipt['importId'], rowCount=len(proposals))
    return import_view(receipt)
