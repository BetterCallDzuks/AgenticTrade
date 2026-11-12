import asyncio
from typing import Optional

import aiohttp


async def fetch_json_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params=None,
    timeout: float = 8,
    retries: int = 3,
    backoff: float = 0.5,
) -> Optional[dict]:
    last_exc = None
    for attempt in range(max(1, retries)):
        try:
            async with session.get(url, params=params, timeout=timeout) as resp:
                if resp.status == 429 or resp.status >= 500:
                    await asyncio.sleep(backoff * (attempt + 1))
                    continue
                if resp.status != 200:
                    return None
                return await resp.json()
        except (asyncio.TimeoutError, aiohttp.ClientError, Exception) as e:
            last_exc = e
            await asyncio.sleep(backoff * (attempt + 1))
    if last_exc:
        print(f"⚠️ HTTP retry exhausted for {url}: {last_exc}")
    return None


async def post_json_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict,
    *,
    timeout: float = 5,
    retries: int = 2,
    backoff: float = 0.5,
) -> Optional[dict]:
    last_exc = None
    for attempt in range(max(1, retries)):
        try:
            async with session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status == 429 or resp.status >= 500:
                    await asyncio.sleep(backoff * (attempt + 1))
                    continue
                if resp.status != 200:
                    return None
                return await resp.json()
        except (asyncio.TimeoutError, aiohttp.ClientError, Exception) as e:
            last_exc = e
            await asyncio.sleep(backoff * (attempt + 1))
    if last_exc:
        print(f"⚠️ HTTP POST retry exhausted for {url}: {last_exc}")
    return None