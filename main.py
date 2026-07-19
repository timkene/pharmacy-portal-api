from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import aggregator, auth, orders, search
from core.database import close_db, connect_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    yield
    await close_db()


app = FastAPI(title="Pharmacy Dispatch API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://pharmacy-portal-delta.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/auth")
app.include_router(orders.router, prefix="/api")
app.include_router(aggregator.router, prefix="/api")
app.include_router(search.router, prefix="/api")

@app.get("/health")
async def health():
    return {"status": "ok"}
