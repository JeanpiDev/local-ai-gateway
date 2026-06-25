"""Proxy de listado de modelos (GET /v1/models)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from .. import upstream
from ..auth import AuthContext, AuthDep

router = APIRouter(tags=["models"])


@router.get(
    "/v1/models",
    summary="Listar modelos disponibles",
    response_description="Lista de modelos visibles para el usuario autenticado",
    responses={401: {"description": "API key ausente, inválida o revocada"}},
)
@router.get("/api/models", include_in_schema=False)  # alias ruta nativa Open WebUI
async def list_models(auth: AuthContext = AuthDep) -> JSONResponse:
    """Proxy de `GET /api/models` de Open WebUI con la key del usuario."""
    resp = await upstream.list_models(auth.token)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Error listando modelos")
    return JSONResponse(content=resp.json(), status_code=200)
