"""Proxy de chat completions (POST /v1/chat/completions).

Flujo: auth -> política (modelos) -> guard de entrada (anti-injection) -> slot de
concurrencia -> backend -> guard de salida (OutputGuard) -> respuesta.
"""
from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .. import concurrency, telemetry, upstream
from ..auth import AuthContext, AuthDep
from ..policy import get_policy
from ..schemas import ChatCompletionRequest
from ..security import prompt_guard
from ..security.output_guard import OutputAction, OutputGuard

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
    """Valida la key, aplica el guard de entrada y de salida, controla la concurrencia
    y reenvía a Open WebUI con la **misma** key."""
    payload = req.to_upstream_payload()

    # 0) Política: modelos permitidos (vacío = todos).
    allowed = get_policy().models.allowed
    if allowed and req.model not in allowed:
        raise HTTPException(
            status_code=400,
            detail={"error": "model_not_allowed", "model": req.model, "allowed": allowed},
        )

    # 1) Guard de entrada: política estructural + heurísticas + escaneo anti-injection.
    messages = [m if isinstance(m, dict) else m.model_dump() for m in payload["messages"]]
    payload["messages"] = prompt_guard.apply(messages, user_id=auth.user_id)

    # 2) Reenvío bajo control de concurrencia (+ guard de salida).
    og = prompt_guard.get_output_guard()
    if req.stream:
        return await _stream(auth.token, payload, og)
    return await _complete(auth.token, payload, og, auth.user_id)


async def _complete(token: str, payload: dict, og: OutputGuard | None, user_id: str = "unknown") -> JSONResponse:
    async with concurrency.slot():
        resp = await upstream.chat_completions(token, payload)
    if resp.status_code != 200:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)

    data = resp.json()

    # Guard de salida sobre el contenido del asistente.
    if og is not None:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = None
        if isinstance(content, str):
            res = og.check(content)
            if res.action is not OutputAction.ALLOW:
                data["choices"][0]["message"]["content"] = res.text
                if res.action is OutputAction.BLOCK:
                    data["choices"][0]["finish_reason"] = "content_filter"
                telemetry.record_output(user_id, res.action.value, res.reason)

    return JSONResponse(content=data, status_code=200)


async def _stream(token: str, payload: dict, og: OutputGuard | None) -> StreamingResponse:
    # Modo buffer: si el guard de salida debe revisar el streaming, bufferiza, revisa
    # y emite una respuesta segura (pierde el streaming incremental; ver DESIGN §8).
    if og is not None and og.guard_streaming:
        return await _stream_buffered(token, payload, og)

    # Passthrough: reenvía el stream tal cual (guard de salida NO aplica al streaming).
    async def gen():
        async with concurrency.slot():
            try:
                async for chunk in upstream.chat_completions_stream(token, payload):
                    yield chunk
            except httpx.HTTPStatusError as e:
                logger.warning("Backend devolvió error en streaming: %s", e)
                yield f'data: {{"error": "upstream_error", "status": {e.response.status_code}}}\n\n'.encode()

    return StreamingResponse(gen(), media_type="text/event-stream")


def _assemble_stream_text(raw: bytes) -> str:
    """Reensambla el texto del asistente a partir de los chunks SSE de OpenAI."""
    parts: list[str] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            obj = json.loads(body)
            delta = obj["choices"][0].get("delta", {})
            piece = delta.get("content")
            if isinstance(piece, str):
                parts.append(piece)
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            continue
    return "".join(parts)


def _sse_single(text: str, finish: str) -> bytes:
    obj = {"choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": finish}]}
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


async def _stream_buffered(token: str, payload: dict, og: OutputGuard) -> StreamingResponse:
    async with concurrency.slot():
        raw = b""
        try:
            async for chunk in upstream.chat_completions_stream(token, payload):
                raw += chunk
        except httpx.HTTPStatusError as e:
            logger.warning("Backend devolvió error en streaming (buffer): %s", e)
            raw = b""

    text = _assemble_stream_text(raw)
    res = og.check(text)
    safe_text = res.text if res.action is not OutputAction.ALLOW else text
    finish = "content_filter" if res.action is OutputAction.BLOCK else "stop"

    async def gen():
        yield _sse_single(safe_text, finish)
        yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
