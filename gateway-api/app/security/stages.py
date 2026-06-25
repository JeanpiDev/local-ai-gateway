"""Etapas concretas del guard.

Fase 1 migra la lógica del antiguo `prompt_guard.apply()` monolítico a dos etapas,
SIN cambiar el comportamiento observable:

- `PolicyStructureStage` (etapa 1): defensa estructural — system prompt fijo del
  servidor y descarte de los mensajes `system` del cliente. Determinista, fail-closed.
- `LLMGuardStage` (etapas 3+4): escaneo llm-guard sobre los mensajes `user`. Los
  scanners de redacción sanean; los de gate (PromptInjection/TokenLimit) bloquean.
  Etapa pesada → fail-open (si el modelo se cae, se degrada en vez de tumbar).

Etapas futuras (Heurísticas, Output Guard, división fina 3/4, modelo multilingüe):
ver `gateway-api/DESIGN.md`.
"""
from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings
from .pipeline import GuardContext, Stage, StageAction, StageResult

logger = logging.getLogger("gateway.guard")

# Scanners que REDACTAN (sanean el texto) en vez de bloquear la petición.
REDACTING_SCANNERS = {"Anonymize", "Secrets", "BanSubstrings"}


class PolicyStructureStage(Stage):
    name = "PolicyStructure"
    fail_mode = "closed"

    def run(self, ctx: GuardContext) -> StageResult:
        settings = get_settings()
        out: list[dict] = []

        # System prompt fijo del servidor al frente (defensa estructural).
        if settings.system_prompt:
            out.append({"role": "system", "content": settings.system_prompt})

        for msg in ctx.messages:
            # Descartar 'system' del cliente si así se configuró.
            if msg.get("role") == "system" and settings.drop_client_system_messages:
                logger.info("Descartado mensaje system del cliente (política)")
                continue
            out.append(msg)

        ctx.messages = out
        return StageResult(action=StageAction.ALLOW)


class LLMGuardStage(Stage):
    name = "LLMGuard"
    fail_mode = "open"   # etapa pesada: degradar en vez de tumbar el servicio

    def __init__(self) -> None:
        self._scanners: list[Any] | None = None
        self._vault: Any = None

    # ── Carga perezosa de scanners (solo si el guard está habilitado) ─────────
    def _build_scanners(self) -> list[Any]:
        settings = get_settings()

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
        use_onnx = settings.guard_use_onnx

        pi_kwargs: dict[str, Any] = dict(
            threshold=settings.guard_prompt_injection_threshold,
            match_type=MatchType.FULL,
            use_onnx=use_onnx,
        )
        if settings.guard_prompt_injection_model:
            from llm_guard.model_selection import Model

            pi_kwargs["model"] = Model(
                path=settings.guard_prompt_injection_model,
                pipeline_kwargs={
                    "return_token_type_ids": False,
                    "max_length": 512,
                    "truncation": True,
                },
            )
            logger.info("PromptInjection usando modelo custom: %s", settings.guard_prompt_injection_model)

        scanners: list[Any] = [
            PromptInjection(**pi_kwargs),
            TokenLimit(limit=settings.guard_token_limit),
            Secrets(redact_mode="all"),
            Anonymize(self._vault, use_onnx=use_onnx),
        ]
        if settings.ban_substrings_list:
            scanners.append(
                BanSubstrings(
                    substrings=settings.ban_substrings_list,
                    match_type="word",
                    case_sensitive=False,
                    redact=True,
                )
            )
        logger.info("llm-guard inicializado con %d scanners (onnx=%s)", len(scanners), use_onnx)
        return scanners

    def warmup(self) -> None:
        if self._scanners is None:
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
        settings = get_settings()
        if not settings.guard_enabled:
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

            # Reemplaza el contenido por el saneado (solo para contenido de texto).
            if isinstance(msg.get("content"), str) and sanitized != text:
                ctx.messages[i] = {**msg, "content": sanitized}
                sanitized_any = True

        return StageResult(action=StageAction.SANITIZE if sanitized_any else StageAction.ALLOW)
