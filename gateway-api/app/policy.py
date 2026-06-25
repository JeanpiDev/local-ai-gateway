"""Declaración de políticas del guard (Audio 3: "declaración de políticas").

La política es la **fuente de verdad declarativa** del guard: system prompt, roles,
límites, qué etapas se ejecutan, en qué orden, con qué `fail_mode` y parámetros.

Se carga de un `policy.yaml` (ruta en `GATEWAY_POLICY_FILE`, por defecto `policy.yaml`).
Si el archivo no existe, se deriva una política equivalente desde las variables de
entorno → **comportamiento idéntico** al de antes de la Fase 2 (retrocompatibilidad).

Ver `gateway-api/policy.example.yaml` y `docs/DESIGN.md`.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import get_settings

logger = logging.getLogger("gateway.policy")

FailMode = Literal["open", "closed"]


class Roles(BaseModel):
    allowed: list[str] = ["user", "assistant", "system", "tool", "developer"]
    drop_client_system: bool = True


class Limits(BaseModel):
    max_messages: int | None = None
    max_chars_per_message: int | None = None
    max_tokens: int | None = None        # informativo (no se fuerza aún)


class Defaults(BaseModel):
    fail_mode: FailMode = "closed"


class Budget(BaseModel):
    total_timeout_s: float | None = None  # reservado (enforcement futuro, DESIGN §8)


class ModelsPolicy(BaseModel):
    allowed: list[str] = []               # vacío = todos permitidos


class StageConfig(BaseModel):
    name: str
    enabled: bool = True
    fail_mode: FailMode | None = None     # None = usa el default de la clase de etapa
    params: dict[str, Any] = Field(default_factory=dict)


class Policy(BaseModel):
    version: int = 1
    system_prompt: str = ""
    roles: Roles = Field(default_factory=Roles)
    limits: Limits = Field(default_factory=Limits)
    defaults: Defaults = Field(default_factory=Defaults)
    budget: Budget = Field(default_factory=Budget)
    models: ModelsPolicy = Field(default_factory=ModelsPolicy)
    stages: list[StageConfig] = Field(default_factory=list)


def _default_policy_from_env() -> Policy:
    """Política equivalente a la config por entorno (cuando no hay policy.yaml).

    Reproduce el pipeline de la Fase 1: PolicyStructure -> LLMGuard, con los mismos
    parámetros que las variables GATEWAY_* — así el comportamiento no cambia.
    """
    s = get_settings()
    return Policy(
        system_prompt=s.system_prompt,
        roles=Roles(drop_client_system=s.drop_client_system_messages),
        stages=[
            StageConfig(name="PolicyStructure"),
            StageConfig(name="Heuristics"),
            StageConfig(
                name="LLMGuard",
                params={
                    "use_onnx": s.guard_use_onnx,
                    "threshold": s.guard_prompt_injection_threshold,
                    "model": s.guard_prompt_injection_model,
                    "token_limit": s.guard_token_limit,
                    "ban_substrings": s.ban_substrings_list,
                },
            ),
            StageConfig(
                name="OutputGuard",
                params={"checks": ["system_prompt_leak", "secrets"], "guard_streaming": False},
            ),
        ],
    )


@lru_cache
def get_policy() -> Policy:
    s = get_settings()
    path = s.policy_file
    if path and os.path.exists(path):
        import yaml

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        logger.info("Política cargada desde %s", path)
        return Policy(**data)
    logger.info("Sin policy.yaml (%s); usando política derivada de variables de entorno", path)
    return _default_policy_from_env()
