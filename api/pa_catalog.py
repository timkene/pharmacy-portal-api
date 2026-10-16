"""Catalog provenance administration only; no mapping review/activation routes."""
import json
from uuid import UUID
from core.security import STAFF_BROWSER_ORIGINS
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from api.pa_preparation import SafePreparationRoute, staff_context
from core.database import get_db
from core import pa_catalog as domain, pa_mapping
from core.pa_catalog_provenance import CatalogConfigurationError
from core.pa_preparation_store import DomainConflict
from models.pa_catalog import MAX_ARTIFACT_BYTES
from models.pa_preparation import Actor

READ = 'PA_CATALOG_READ'
INGEST = 'PA_CATALOG_INGEST'
VALIDATE = 'PA_CATALOG_VALIDATE'
TRUST = 'PA_CATALOG_TRUST'
router = APIRouter(prefix='/pa-catalog', tags=['pa-catalog'], route_class=SafePreparationRoute)


def mutation_security(request):
    types = request.headers.getlist('content-type')
    if len(types) != 1 or types[0].strip().lower() not in ('application/json', 'application/json; charset=utf-8'):
        raise HTTPException(415, 'Strict JSON required')
    origins = request.headers.getlist('origin')
    own_origin = str(request.base_url).rstrip('/')
    if origins:
        if len(origins) != 1 or origins[0] not in (*STAFF_BROWSER_ORIGINS, own_origin):
            raise HTTPException(403, 'Invalid mutation origin')
    elif (request.headers.get('x-pharmacy-csrf') != '1'
          or request.headers.get('sec-fetch-site') == 'cross-site'):
        raise HTTPException(403, 'Mutation origin or CSRF header required')


async def empty_json(request):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 4096:
            raise HTTPException(413, 'Request too large')
        body.extend(chunk)
    try:
        if json.loads(body) != {}:
            raise ValueError()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'Empty JSON object required') from None


def require(permission):
    async def dependency(request: Request, context=Depends(staff_context)):
        if permission not in context['permissions']: raise HTTPException(403, 'Catalog permission required')
        if request.method == 'POST': mutation_security(request)
        return Actor(actorId=context['actorId'], canReadCatalog=permission == READ, canIngestCatalog=permission == INGEST,
            canValidateCatalog=permission == VALIDATE, canTrustCatalog=permission == TRUST)
    return dependency


async def safe(operation):
    try:
        return await operation
    except CatalogConfigurationError:
        raise HTTPException(503, 'Catalog authority is not configured') from None
    except PermissionError:
        raise HTTPException(403, 'Independent catalog staff identities required') from None
    except DomainConflict as exc:
        raise HTTPException(404 if str(exc) == 'CATALOG_NOT_FOUND' else 409, str(exc)) from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(400, 'Invalid catalog evidence') from None


@router.post('/snapshots')
async def ingest(request: Request, actor=Depends(require(INGEST)), db=Depends(get_db)):
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
        raise HTTPException(415, 'Catalog JSON artifact required')
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_ARTIFACT_BYTES: raise HTTPException(413, 'Catalog artifact too large')
        body.extend(chunk)
    return domain.snapshot_view(await safe(domain.ingest_signed_catalog(db, actor, bytes(body))))


@router.post('/snapshots/{snapshot_id}/validate')
async def validate(snapshot_id: UUID, request: Request, actor=Depends(require(VALIDATE)), db=Depends(get_db)):
    await empty_json(request)
    evidence_id = await safe(domain.validate_signed_catalog(db, actor, str(snapshot_id)))
    return {'snapshotId': str(snapshot_id), 'validationEvidenceId': evidence_id, 'status': 'EVIDENCE_VALIDATED'}


@router.post('/snapshots/{snapshot_id}/trust')
async def trust(snapshot_id: UUID, request: Request, actor=Depends(require(TRUST)), db=Depends(get_db)):
    await empty_json(request)
    # One server-selected validation receipt per snapshot; callers cannot nominate authority.
    snapshot = await safe(pa_mapping.promote_catalog_trust(db, actor, str(snapshot_id), str(snapshot_id)))
    return domain.snapshot_view(snapshot)


@router.get('/snapshots')
async def snapshots(limit: int = Query(default=25, ge=1, le=50), cursor: UUID | None = None,
                    actor=Depends(require(READ)), db=Depends(get_db)):
    return await safe(domain.list_snapshots(db, actor, limit, str(cursor) if cursor else None))


@router.get('/snapshots/{snapshot_id}')
async def snapshot(snapshot_id: UUID, actor=Depends(require(READ)), db=Depends(get_db)):
    return await safe(domain.read_snapshot(db, actor, str(snapshot_id)))
