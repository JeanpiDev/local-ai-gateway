"""Orquestador del guard: construye el GuardPipeline y expone la API pública.

Mantiene `apply()` y `warmup()` para no cambiar los llamadores (`routes/chat.py`,
`main.py`). La lógica vive ahora en `pipeline.py` (núcleo) y `stages.py` (etapas).

Pipeline Fase 1: PolicyStructure -> LLMGuard. Etapas futuras (Heurísticas, Output
Guard, modelo multilingüe) se añaden registrándolas aquí. Ver `DESIGN.md`.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, status

from .pipeline import GuardBlocked, GuardContext, GuardPipeline
from .stages import LLMGuardStage, PolicyStructureStage

logger = logging.getLogger("gateway.guard")

# Instancias únicas (la etapa LLM cachea sus scanners).
_llm_stage = LLMGuardStage()
_pipeline = GuardPipeline([PolicyStructureStage(), _llm_stage])


def warmup() -> None:
    """Pre-carga los scanners del guard en el arranque (si está habilitado)."""
    from ..config import get_settings

    if get_settings().guard_enabled:
        _llm_stage.warmup()


def apply(messages: list[dict], user_id: str = "unknown") -> list[dict]:
    """Ejecuta el pipeline sobre los mensajes. Devuelve los mensajes saneados/listos
    para el backend, o lanza HTTP 422 si una etapa bloquea."""
    ctx = GuardContext(messages=messages, user_id=user_id)
    try:
        return _pipeline.run(ctx)
    except GuardBlocked as blocked:
        detail = blocked.result.detail or {
            "error": "input_rejected",
            "message": blocked.result.reason,
        }
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
