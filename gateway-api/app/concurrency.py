"""Control de concurrencia hacia el backend.

Como Ollama serializa la inferencia (en CPU el throughput tiene un techo duro),
limitamos las peticiones simultáneas con un semáforo. Si no hay slot libre dentro
de `queue_timeout`, devolvemos 429 en vez de dejar al cliente colgado.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import HTTPException, status

from .config import get_settings

_semaphore: asyncio.Semaphore | None = None


def init_semaphore() -> None:
    global _semaphore
    _semaphore = asyncio.Semaphore(get_settings().max_concurrency)


def _sem() -> asyncio.Semaphore:
    if _semaphore is None:
        raise RuntimeError("Semáforo no inicializado")
    return _semaphore


@asynccontextmanager
async def slot():
    """Adquiere un slot de inferencia o lanza 429 si se agota el tiempo de espera."""
    settings = get_settings()
    sem = _sem()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=settings.queue_timeout)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Gateway saturado: no hay slots de inferencia libres. Reintenta.",
            headers={"Retry-After": str(int(settings.queue_timeout))},
        )
    try:
        yield
    finally:
        sem.release()
