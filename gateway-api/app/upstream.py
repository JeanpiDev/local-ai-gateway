"""Cliente HTTP asíncrono hacia Open WebUI.

Concentra todas las llamadas al backend. El cliente httpx se crea una sola vez
(en el lifespan de la app) y se reutiliza.
"""
from __future__ import annotations

from typing import Any, AsyncIterator

import httpx

from .config import get_settings

_client: httpx.AsyncClient | None = None


def init_client() -> httpx.AsyncClient:
    global _client
    settings = get_settings()
    _client = httpx.AsyncClient(
        base_url=settings.openwebui_base_url.rstrip("/"),
        timeout=httpx.Timeout(settings.upstream_timeout, connect=10.0),
    )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("Cliente upstream no inicializado")
    return _client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def get_user(token: str) -> dict[str, Any] | None:
    """Valida una API key/JWT y devuelve el usuario, o None si es inválida."""
    try:
        r = await client().get("/api/v1/auths/", headers=_bearer(token))
    except httpx.HTTPError:
        return None
    if r.status_code == 200:
        return r.json()
    return None


async def list_models(token: str) -> httpx.Response:
    return await client().get("/api/models", headers=_bearer(token))


async def chat_completions(token: str, payload: dict[str, Any]) -> httpx.Response:
    """Llamada NO-streaming. Devuelve la Response completa."""
    return await client().post(
        "/api/chat/completions", headers=_bearer(token), json=payload
    )


async def chat_completions_stream(
    token: str, payload: dict[str, Any]
) -> AsyncIterator[bytes]:
    """Llamada streaming (SSE). Itera los bytes del cuerpo tal cual los emite el backend."""
    headers = _bearer(token)
    async with client().stream(
        "POST", "/api/chat/completions", headers=headers, json=payload
    ) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes():
            yield chunk


# ── Endpoints de administración (provisión de usuarios) ──────────────────────

async def admin_get_default_permissions(admin_key: str) -> httpx.Response:
    return await client().get(
        "/api/v1/users/default/permissions", headers=_bearer(admin_key)
    )


async def admin_set_default_permissions(
    admin_key: str, permissions: dict[str, Any]
) -> httpx.Response:
    # Open WebUI espera el objeto de permisos plano en el cuerpo.
    return await client().post(
        "/api/v1/users/default/permissions",
        headers=_bearer(admin_key),
        json=permissions,
    )


async def admin_add_user(
    admin_key: str, name: str, email: str, password: str, role: str
) -> httpx.Response:
    return await client().post(
        "/api/v1/auths/add",
        headers=_bearer(admin_key),
        json={"name": name, "email": email, "password": password, "role": role},
    )


async def signin(email: str, password: str) -> httpx.Response:
    return await client().post(
        "/api/v1/auths/signin", json={"email": email, "password": password}
    )


async def create_api_key(token: str) -> httpx.Response:
    return await client().post("/api/v1/auths/api_key", headers=_bearer(token))


async def admin_delete_user(admin_key: str, user_id: str) -> httpx.Response:
    return await client().delete(
        f"/api/v1/users/{user_id}", headers=_bearer(admin_key)
    )
