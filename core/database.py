import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

_client: AsyncIOMotorClient | None = None
_db = None


async def connect_db():
    global _client, _db
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    db_name = os.getenv("DB_NAME", "pharmacy_dispatch")
    _client = AsyncIOMotorClient(mongo_uri)
    _db = _client[db_name]

    # Ensure indexes
    await _db.staff_users.create_index("email", unique=True)
    await _db.aggregator_users.create_index("email", unique=True)
    await _db.orders.create_index("intakeId", unique=True)
    await _db.bids.create_index([("orderId", 1), ("aggregatorId", 1)])


async def close_db():
    global _client
    if _client:
        _client.close()


def get_db():
    if _db is None:
        raise RuntimeError("Database not initialised — call connect_db() first")
    return _db
