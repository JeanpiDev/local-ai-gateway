"""Proxy de chat completions (POST /v1/chat/completions).

Flujo: auth (ya resuelto por dependencia) -> guard (anti-injection + política) ->
slot de concurrencia -> reenvío al backend (streaming o no) con la MISMA key.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .. import concurrency, upstream
from ..auth import AuthContext, AuthDep
from ..policy import get_policy
from ..schemas import ChatCompletionRequest
from ..security import prompt_guard

logger = logging.getLogger("gateway.chat")

router = APIRouter(tags=["chat"])


@router.post(
    "/v1/chat/completions",
    summary="Chat completions (OpenAI-compatible)",
    response_description="Respuesta de chat (JSON OpenAI) o stream SSE si stream=true",
    responses={
        401: {"description": "API key ausente, inválida o revocada"},
        422: {"description": "Input rechazado por la capa anti prompt-injection"},
        429: {"description": "Gateway saturado: sin slots de inferencia (incluye Retry-After)"},
    },
)
@router.post("/api/chat/completions", include_in_schema=False)  # alias ruta nativa Open WebUI
async def chat_completions(req: ChatCompletionRequest, auth: AuthContext = AuthDep):
    """Valida la key, aplica el guard (anti-injection + política de system prompt),
    espera un slot de concurrencia y reenvía a Open WebUI con la **misma** key."""
    payload = req.to_upstream_payload()

    # 0) Política: modelos permitidos (vacío = todos).
    allowed = get_policy().models.allowed
    if allowed and req.model not in allowed:
        raise HTTPException(
            status_code=400,
            detail={"error": "model_not_allowed", "model": req.model, "allowed": allowed},
        )

    # 1) Guard: política estructural + escaneo anti-injection sobre los mensajes.
    messages = [m if isinstance(m, dict) else m.model_dump() for m in payload["messages"]]
    payload["messages"] = prompt_guard.apply(messages)

    # 2) Reenvío bajo control de concurrencia.
    if req.stream:
        return await _stream(auth.token, payload)
    return await _complete(auth.token, payload)


async def _complete(token: str, payload: dict) -> JSONResponse:
    async with concurrency.slot():
        resp = await upstream.chat_completions(token, payload)
    if resp.status_code != 200:
        # Propaga el error del backend tal cual (cuerpo + status).
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)
    return JSONResponse(content=resp.json(), status_code=200)


async def _stream(token: str, payload: dict) -> StreamingResponse:
    # El slot se mantiene tomado durante TODO el streaming.
    async def gen():
        async with concurrency.slot():
            try:
                async for chunk in upstream.chat_completions_stream(token, payload):
                    yield chunk
            except httpx.HTTPStatusError as e:
                logger.warning("Backend devolvió error en streaming: %s", e)
                yield f'data: {{"error": "upstream_error", "status": {e.response.status_code}}}\n\n'.encode()

    return StreamingResponse(gen(), media_type="text/event-stream")
