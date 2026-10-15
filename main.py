from core.security import STAFF_BROWSER_ORIGINS
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from api import aggregator, auth, orders, pa, pa_preparation, pa_mapping_review, pa_catalog, pa_mapping_proposals, search
from core.database import close_db, connect_db
from core.security import validate_session_secret


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_session_secret()
    await connect_db()
    yield
    await close_db()


app = FastAPI(title="Pharmacy Dispatch API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(STAFF_BROWSER_ORIGINS),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/auth")
app.include_router(orders.router, prefix="/api")
app.include_router(pa.router, prefix="/api")
app.include_router(pa_preparation.router, prefix="/api")
app.include_router(pa_mapping_review.router, prefix="/api")
app.include_router(pa_catalog.router, prefix="/api")
app.include_router(pa_mapping_proposals.router, prefix="/api")
app.include_router(aggregator.router, prefix="/api")
app.include_router(search.router, prefix="/api")

@app.get("/health")
async def health():
    try:
        validate_session_secret()
    except RuntimeError:
        raise HTTPException(503, "Authentication configuration is invalid") from None
    return {"status": "ok"}
