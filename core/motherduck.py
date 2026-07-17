import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

MOTHERDUCK_TOKEN = os.getenv("MOTHERDUCK_TOKEN")
_executor = ThreadPoolExecutor(max_workers=3)


def _query_sync(sql: str, params: list | None = None) -> list[dict]:
    if not MOTHERDUCK_TOKEN:
        raise RuntimeError("MOTHERDUCK_TOKEN not configured")
    # Lazy import — keeps duckdb out of the startup critical path
    import duckdb  # noqa: PLC0415
    conn = duckdb.connect(f"md:ai_driven_data?motherduck_token={MOTHERDUCK_TOKEN}")
    try:
        cursor = conn.execute(sql, params or [])
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


async def md_query(sql: str, params: list | None = None) -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, lambda: _query_sync(sql, params))
