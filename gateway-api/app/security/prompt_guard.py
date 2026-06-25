"""Orquestador del guard: construye el GuardPipeline DESDE LA POLÍTICA.

Mantiene la API pública `apply()` y `warmup()` (no cambian `routes/chat.py` ni
`main.py`). El pipeline se arma a partir de `policy.stages` mediante un registro de
etapas; añadir una etapa nueva = registrarla aquí y declararla en `policy.yaml`.
"""
from __future__ import annotations

import logging
from typing import Callable

from fastapi import HTTPException, status

from ..policy import Policy, StageConfig, get_policy
from .pipeline import GuardBlocked, GuardContext, GuardPipeline, Stage
from .stages import LLMGuardStage, PolicyStructureStage

logger = logging.getLogger("gateway.guard")

# Registro de etapas: nombre -> constructor(policy, stage_config) -> Stage
STAGE_REGISTRY: dict[str, Callable[[Policy, StageConfig], Stage]] = {
    "PolicyStructure": lambda policy, cfg: PolicyStructureStage(policy),
    "LLMGuard": lambda policy, cfg: LLMGuardStage(params=cfg.params),
}


def _build_pipeline(policy: Policy) -> GuardPipeline:
    stages: list[Stage] = []
    for cfg in policy.stages:
        if not cfg.enabled:
            logger.info("Etapa %s deshabilitada por política", cfg.name)
            continue
        builder = STAGE_REGISTRY.get(cfg.name)
        if builder is None:
            logger.warning("Etapa desconocida en la política: %s (ignorada)", cfg.name)
            continue
        stage = builder(policy, cfg)
        if cfg.fail_mode:                      # override explícito de la política
            stage.fail_mode = cfg.fail_mode
        stages.append(stage)
    logger.info("Pipeline de guard: %s", [s.name for s in stages])
    return GuardPipeline(stages)


_policy = get_policy()
_pipeline = _build_pipeline(_policy)


def warmup() -> None:
    """Pre-carga scanners de las etapas que lo soporten (si el guard está habilitado)."""
    for stage in _pipeline.stages:
        warm = getattr(stage, "warmup", None)
        if callable(warm):
            warm()


def apply(messages: list[dict], user_id: str = "unknown") -> list[dict]:
    """Ejecuta el pipeline. Devuelve los mensajes listos para el backend o lanza 422."""
    ctx = GuardContext(messages=messages, user_id=user_id)
    try:
        return _pipeline.run(ctx)
    except GuardBlocked as blocked:
        detail = blocked.result.detail or {
            "error": "input_rejected",
            "message": blocked.result.reason,
        }
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
