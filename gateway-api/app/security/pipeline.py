"""Núcleo del pipeline de guard por etapas.

Cada etapa (`Stage`) inspecciona/transforma los mensajes y devuelve un `StageResult`.
El `GuardPipeline` las ejecuta en orden, corta en el primer `BLOCK` (short-circuit) y
aplica el `fail_mode` de cada etapa si lanza una excepción (circuit breaker / degradación).

Ver el diseño completo en `docs/DESIGN.md`.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("gateway.guard")


class StageAction(str, Enum):
    ALLOW = "allow"        # no toca el contenido
    SANITIZE = "sanitize"  # modificó el contenido (redacción) — sigue el pipeline
    BLOCK = "block"        # rechaza la petición — corta el pipeline (-> 422)


@dataclass
class StageResult:
    action: StageAction = StageAction.ALLOW
    score: float = 0.0
    reason: str = ""
    detail: dict[str, Any] | None = None   # cuerpo del 422 cuando action == BLOCK


@dataclass
class GuardContext:
    """Estado mutable que recorre el pipeline."""
    messages: list[dict]
    user_id: str = "unknown"
    audit: list[dict] = field(default_factory=list)   # traza por etapa (para logging)


class GuardBlocked(Exception):
    """Una etapa bloqueó la petición. El borde HTTP lo traduce a 422."""
    def __init__(self, stage: str, result: StageResult):
        self.stage = stage
        self.result = result
        super().__init__(f"bloqueado por {stage}: {result.reason}")


class Stage(ABC):
    name: str = "stage"
    # "closed" => si la etapa falla, bloquea (seguridad). "open" => deja pasar (disponibilidad).
    fail_mode: str = "closed"
    timeout_s: float | None = None   # reservado (enforcement best-effort; ver DESIGN §8)

    @abstractmethod
    def run(self, ctx: GuardContext) -> StageResult:
        ...


class GuardPipeline:
    def __init__(self, stages: list[Stage]):
        self.stages = stages

    def run(self, ctx: GuardContext) -> list[dict]:
        for stage in self.stages:
            res = self._run_stage(stage, ctx)
            ctx.audit.append(
                {"stage": stage.name, "action": res.action.value, "score": res.score, "reason": res.reason}
            )
            if res.action is StageAction.BLOCK:
                raise GuardBlocked(stage.name, res)
        return ctx.messages

    def _run_stage(self, stage: Stage, ctx: GuardContext) -> StageResult:
        """Ejecuta una etapa aplicando su fail_mode ante excepciones (circuit breaker)."""
        try:
            return stage.run(ctx)
        except GuardBlocked:
            raise
        except Exception as e:  # noqa: BLE001 — degradación controlada
            logger.exception("Etapa %s falló: %s", stage.name, e)
            if stage.fail_mode == "open":
                logger.warning("Etapa %s degradada (fail-open): se deja pasar", stage.name)
                return StageResult(action=StageAction.ALLOW, reason=f"{stage.name} degradado (fail-open)")
            return StageResult(
                action=StageAction.BLOCK,
                reason=f"{stage.name} falló (fail-closed)",
                detail={"error": "guard_stage_failed", "stage": stage.name},
            )
