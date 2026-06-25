"""Etapas concretas del guard, configuradas desde la política (Fase 2).

- `PolicyStructureStage` (etapa 1): defensa estructural dirigida por la política —
  system prompt fijo, descarte de `system` del cliente, whitelist de roles y límites
  de nº de mensajes / tamaño. Determinista, fail-closed.
- `LLMGuardStage` (etapas 3+4): escaneo llm-guard con parámetros de la política.
  Redacción sanea; gate (PromptInjection/TokenLimit) bloquea. Fail-open (etapa pesada).

Etapas futuras (Heurísticas, Output Guard): ver `gateway-api/DESIGN.md`.
"""
from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings
from ..policy import Policy
from .pipeline import GuardContext, Stage, StageAction, StageResult

logger = logging.getLogger("gateway.guard")

# Scanners que REDACTAN (sanean el texto) en vez de bloquear la petición.
REDACTING_SCANNERS = {"Anonymize", "Secrets", "BanSubstrings"}


class PolicyStructureStage(Stage):
    name = "PolicyStructure"
    fail_mode = "closed"

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def run(self, ctx: GuardContext) -> StageResult:
        p = self.policy
        out: list[dict] = []

        # System prompt fijo del servidor al frente (defensa estructural).
        if p.system_prompt:
            out.append({"role": "system", "content": p.system_prompt})

        for msg in ctx.messages:
            role = msg.get("role")

            # Descartar 'system' del cliente si así se configuró.
            if role == "system" and p.roles.drop_client_system:
                logger.info("Descartado mensaje system del cliente (política)")
                continue

            # Whitelist de roles (por defecto incluye todos los comunes → inerte).
            if role not in p.roles.allowed:
                return StageResult(
                    action=StageAction.BLOCK,
                    reason=f"rol '{role}' no permitido",
                    detail={"error": "role_not_allowed", "role": role, "allowed": p.roles.allowed},
                )

            # Límite de tamaño por mensaje (None = sin límite → inerte).
            if p.limits.max_chars_per_message is not None:
                text = msg.get("content")
                if isinstance(text, str) and len(text) > p.limits.max_chars_per_message:
                    return StageResult(
                        action=StageAction.BLOCK,
                        reason="mensaje excede el tamaño máximo",
                        detail={"error": "message_too_large", "limit": p.limits.max_chars_per_message},
                    )

            out.append(msg)

        # Límite de nº de mensajes (cuenta tras el saneo estructural).
        if p.limits.max_messages is not None and len(out) > p.limits.max_messages:
            return StageResult(
                action=StageAction.BLOCK,
                reason="demasiados mensajes",
                detail={"error": "too_many_messages", "limit": p.limits.max_messages},
            )

        ctx.messages = out
        return StageResult(action=StageAction.ALLOW)


class LLMGuardStage(Stage):
    name = "LLMGuard"
    fail_mode = "open"   # etapa pesada: degradar en vez de tumbar el servicio

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        params = params or {}
        self.use_onnx: bool = params.get("use_onnx", False)
        self.threshold: float = params.get("threshold", 0.95)
        self.model: str = params.get("model", "") or ""
        self.token_limit: int = params.get("token_limit", 4096)
        self.ban_substrings: list[str] = params.get("ban_substrings", []) or []
        self._scanners: list[Any] | None = None
        self._vault: Any = None

    # ── Carga perezosa de scanners (solo si el guard está habilitado) ─────────
    def _build_scanners(self) -> list[Any]:
        from llm_guard.input_scanners import (
            Anonymize,
            BanSubstrings,
            PromptInjection,
            Secrets,
            TokenLimit,
        )
        from llm_guard.input_scanners.prompt_injection import MatchType
        from llm_guard.vault import Vault

        self._vault = Vault()

        pi_kwargs: dict[str, Any] = dict(
            threshold=self.threshold,
            match_type=MatchType.FULL,
            use_onnx=self.use_onnx,
        )
        if self.model:
            from llm_guard.model_selection import Model

            pi_kwargs["model"] = Model(
                path=self.model,
                pipeline_kwargs={
                    "return_token_type_ids": False,
                    "max_length": 512,
                    "truncation": True,
                },
            )
            logger.info("PromptInjection usando modelo custom: %s", self.model)

        scanners: list[Any] = [
            PromptInjection(**pi_kwargs),
            TokenLimit(limit=self.token_limit),
            Secrets(redact_mode="all"),
            Anonymize(self._vault, use_onnx=self.use_onnx),
        ]
        if self.ban_substrings:
            scanners.append(
                BanSubstrings(
                    substrings=self.ban_substrings,
                    match_type="word",
                    case_sensitive=False,
                    redact=True,
                )
            )
        logger.info("llm-guard inicializado con %d scanners (onnx=%s)", len(scanners), self.use_onnx)
        return scanners

    def warmup(self) -> None:
        if get_settings().guard_enabled and self._scanners is None:
            self._scanners = self._build_scanners()

    @staticmethod
    def _content_to_text(content: Any) -> str | None:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            return "\n".join(parts) if parts else None
        return None

    def run(self, ctx: GuardContext) -> StageResult:
        # GUARD_ENABLED es el interruptor maestro (en dev se apaga para imagen ligera).
        if not get_settings().guard_enabled:
            return StageResult(action=StageAction.ALLOW)

        if self._scanners is None:
            self._scanners = self._build_scanners()

        from llm_guard import scan_prompt

        sanitized_any = False
        for i, msg in enumerate(ctx.messages):
            if msg.get("role") != "user":
                continue
            text = self._content_to_text(msg.get("content"))
            if not text:
                continue

            sanitized, results_valid, results_score = scan_prompt(self._scanners, text)
            failed = [name for name, ok in results_valid.items() if not ok]
            blocking = [n for n in failed if n not in REDACTING_SCANNERS]
            redacted = [n for n in failed if n in REDACTING_SCANNERS]
            if redacted:
                logger.info("Input saneado por scanners de redacción %s", redacted)

            if blocking:
                logger.warning("Input bloqueado por %s (scores=%s)", blocking, results_score)
                return StageResult(
                    action=StageAction.BLOCK,
                    score=max((results_score.get(n, 0.0) for n in blocking), default=1.0),
                    reason=f"bloqueado por {', '.join(blocking)}",
                    detail={
                        "error": "input_rejected",
                        "message": "El contenido fue bloqueado por la capa de seguridad.",
                        "blocked_by": blocking,
                        "scores": results_score,
                    },
                )

            if isinstance(msg.get("content"), str) and sanitized != text:
                ctx.messages[i] = {**msg, "content": sanitized}
                sanitized_any = True

        return StageResult(action=StageAction.SANITIZE if sanitized_any else StageAction.ALLOW)
