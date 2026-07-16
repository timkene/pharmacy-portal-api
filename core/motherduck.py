import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import duckdb

MOTHERDUCK_TOKEN = os.getenv("MOTHERDUCK_TOKEN")
_executor = ThreadPoolExecutor(max_workers=3)


def _query_sync(sql: str, params: list | None = None) -> list[dict]:
    if not MOTHERDUCK_TOKEN:
        raise RuntimeError("MOTHERDUCK_TOKEN not configured")
    conn = duckdb.connect(f"md:ai_driven_data?motherduck_token={MOTHERDUCK_TOKEN}")
    try:
        result = conn.execute(sql, params or []).fetchdf()
        return result.to_dict("records")
    finally:
        conn.close()


async def md_query(sql: str, params: list | None = None) -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: _query_sync(sql, params))
