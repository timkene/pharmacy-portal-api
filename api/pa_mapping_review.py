"""Authenticated CSV review transfer only; no catalog or activation capability."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from api.pa_preparation import SafePreparationRoute, staff_context
from core.database import get_db
from core import pa_mapping_review as domain
from core.pa_preparation_store import DomainConflict
from models.pa_preparation import Actor
from models.pa_mapping_review import CreateReviewExport

EXPORT = 'PA_MAPPING_EXPORT'
IMPORT = 'PA_MAPPING_IMPORT'
CREATE_MAX_BYTES = 4096


class BoundedReviewRoute(SafePreparationRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def bounded(request):
            if request.method == 'POST' and request.url.path.endswith('/exports'):
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > CREATE_MAX_BYTES:
                        raise HTTPException(413, 'Export request too large')
                    body.extend(chunk)
                async def receive():
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                request = Request(request.scope, receive=receive)
            return await handler(request)
        return bounded


router = APIRouter(prefix='/pa-mapping-review', tags=['pa-mapping-review'], route_class=BoundedReviewRoute)


def require(permission):
    async def dependency(context=Depends(staff_context)):
        if permission not in context['permissions']:
            raise HTTPException(403, 'Mapping review permission required')
        return Actor(actorId=context['actorId'], canExportMappings=permission == EXPORT, canImportMappings=permission == IMPORT)
    return dependency


export_actor = require(EXPORT)
import_actor = require(IMPORT)


async def safe(operation):
    try:
        return await operation
    except domain.ReviewInvalid as exc:
        if str(exc) == 'EXPORT_NOT_FOUND': raise HTTPException(404, 'Mapping review export not found') from None
        raise HTTPException(400, str(exc)) from None
    except DomainConflict as exc:
        # Domain messages are server-defined reason codes, never uploaded data.
        raise HTTPException(409, str(exc)) from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(409, 'Mapping review source invalid or stale') from None


@router.post('/batches/{batch_id}/exports')
async def create(batch_id: UUID, body: CreateReviewExport, actor=Depends(export_actor), db=Depends(get_db)):
    return await safe(domain.create_export(db, actor, str(batch_id), body))


@router.get('/exports/{export_id}/download')
async def download(export_id: UUID, actor=Depends(export_actor), db=Depends(get_db)):
    data = await safe(domain.download_export(db, actor, str(export_id)))
    return Response(data, media_type='text/csv; charset=utf-8', headers={
        'Cache-Control': 'no-store', 'Content-Disposition': f'attachment; filename="mapping-review-{export_id}.csv"',
        'X-Content-Type-Options': 'nosniff'})


@router.post('/exports/{export_id}/imports')
async def upload(export_id: UUID, request: Request, actor=Depends(import_actor), db=Depends(get_db)):
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'text/csv':
        raise HTTPException(415, 'UTF-8 text/csv required')
    length = request.headers.get('content-length')
    if length:
        try:
            if int(length) < 0 or int(length) > domain.MAX_BYTES: raise ValueError()
        except ValueError:
            raise HTTPException(413, 'Upload size invalid') from None
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > domain.MAX_BYTES: raise HTTPException(413, 'Upload too large')
        data.extend(chunk)
    return await safe(domain.import_proposals(db, actor, str(export_id), bytes(data)))


@router.get('/exports/{export_id}/import-status')
async def status(export_id: UUID, actor=Depends(import_actor), db=Depends(get_db)):
    receipt = await db.pa_mapping_review_imports.find_one({'_id': str(export_id)})
    if receipt: return domain.import_view(receipt)
    if not await db.pa_mapping_review_exports.find_one({'_id': str(export_id)}):
        raise HTTPException(404, 'Mapping review export not found')
    return {'exportId': str(export_id), 'status': 'NOT_IMPORTED'}
