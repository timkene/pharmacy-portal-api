"""Authenticated explicit decisions; no generic lifecycle or transmission routes."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from api.pa_preparation import SafePreparationRoute, staff_context
from api.pa_catalog import mutation_security
from core.database import get_db
from core import pa_mapping_proposals as domain
from core.pa_catalog_provenance import CatalogConfigurationError, no_duplicate_keys
from core.pa_preparation_store import DomainConflict
from models.pa_preparation import Actor
from models.pa_mapping_proposals import ProposalAction, ActivationAction
import json

REVIEW='PA_MAPPING_REVIEW'
APPROVE='PA_MAPPING_APPROVE'
ACTIVATE='PA_MAPPING_ACTIVATE'
router=APIRouter(prefix='/pa-mapping-proposals',tags=['pa-mapping-proposals'],route_class=SafePreparationRoute)


def require(permission):
    async def dependency(request: Request,context=Depends(staff_context)):
        if permission not in context['permissions']: raise HTTPException(403,'Mapping permission required')
        if request.method=='POST': mutation_security(request)
        return Actor(actorId=context['actorId'],canReviewMappings=permission==REVIEW,
            canApproveMappings=permission==APPROVE,canActivateMappings=permission==ACTIVATE)
    return dependency


async def body(request,model):
    data=bytearray()
    async for chunk in request.stream():
        if len(data)+len(chunk)>4096: raise HTTPException(413,'Action request too large')
        data.extend(chunk)
    try:
        return model.model_validate(json.loads(data.decode('utf-8'),object_pairs_hook=no_duplicate_keys))
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise HTTPException(422,'Invalid mapping action') from None


async def safe(operation):
    try: return await operation
    except CatalogConfigurationError: raise HTTPException(503,'Catalog authority unavailable') from None
    except PermissionError: raise HTTPException(403,'Independent authorized staff action required') from None
    except DomainConflict as exc:
        raise HTTPException(404 if str(exc)=='PROPOSAL_NOT_FOUND' else 409,str(exc)) from None
    except (ValueError,TypeError,KeyError,AttributeError):
        raise HTTPException(409,'Mapping evidence invalid or stale') from None


@router.get('')
async def listing(limit:int=Query(default=25,ge=1,le=50),after:UUID|None=None,
                  actor=Depends(require(REVIEW)),db=Depends(get_db)):
    return await safe(domain.list_proposals(db,actor,limit,str(after) if after else None))


@router.get('/{proposal_id}')
async def detail(proposal_id:UUID,actor=Depends(require(REVIEW)),db=Depends(get_db)):
    return await safe(domain.detail(db,actor,str(proposal_id)))


@router.post('/{proposal_id}/reject')
async def reject(proposal_id:UUID,request:Request,actor=Depends(require(REVIEW)),db=Depends(get_db)):
    action=await body(request,ProposalAction)
    return await safe(domain.decide(db,actor,str(proposal_id),action,approve=False))


@router.post('/{proposal_id}/approve')
async def approve(proposal_id:UUID,request:Request,actor=Depends(require(APPROVE)),db=Depends(get_db)):
    action=await body(request,ProposalAction)
    return await safe(domain.decide(db,actor,str(proposal_id),action,approve=True))


@router.post('/{proposal_id}/activate')
async def activate(proposal_id:UUID,request:Request,actor=Depends(require(ACTIVATE)),db=Depends(get_db)):
    action=await body(request,ActivationAction)
    return await safe(domain.activate(db,actor,str(proposal_id),action))
