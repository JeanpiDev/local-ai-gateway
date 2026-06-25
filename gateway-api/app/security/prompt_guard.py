"""Capa anti prompt-injection basada en llm-guard + defensa estructural.

- Escanea SOLO el contenido de los mensajes `user` (no el system, que es nuestro).
- Usa el pipeline de input scanners de llm-guard.
- Aplica política estructural: system prompt fijo en servidor; opcionalmente
  descarta los mensajes `system` que mande el cliente.

llm-guard carga modelos transformer pesados, por eso se importan y construyen
de forma perezosa (solo si GATEWAY_GUARD_ENABLED=true). En dev puedes apagarlo.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status

from ..config import get_settings

logger = logging.getLogger("gateway.guard")

# Scanners que REDACTAN (sanean el texto) en vez de bloquear la petición.
# Cuando devuelven valid=False significa "encontré y redacté", no "rechazar".
REDACTING_SCANNERS = {"Anonymize", "Secrets", "BanSubstrings"}

_scanners: list[Any] | None = None
_vault: Any = None


def _build_scanners() -> list[Any]:
    """Construye (una sola vez) la cadena de scanners de entrada de llm-guard."""
    global _vault
    settings = get_settings()

    # Import perezoso: solo si el guard está habilitado evitamos cargar torch/transformers.
    from llm_guard.input_scanners import (
        Anonymize,
        BanSubstrings,
        PromptInjection,
        Secrets,
        TokenLimit,
    )
    from llm_guard.input_scanners.prompt_injection import MatchType
    from llm_guard.vault import Vault

    _vault = Vault()
    use_onnx = settings.guard_use_onnx

    # Modelo de prompt-injection: por defecto el de llm-guard (inglés); o uno
    # multilingüe si se configura GATEWAY_GUARD_PROMPT_INJECTION_MODEL.
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
        Anonymize(_vault, use_onnx=use_onnx),
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


def warmup() -> None:
    """Pre-carga los scanners en el arranque para evitar latencia en el primer request."""
    global _scanners
    if get_settings().guard_enabled and _scanners is None:
        _scanners = _build_scanners()


@dataclass
class GuardResult:
    sanitized: str
    scores: dict[str, float]


def _scan_text(text: str) -> GuardResult:
    global _scanners
    if _scanners is None:
        _scanners = _build_scanners()

    from llm_guard import scan_prompt

    sanitized, results_valid, results_score = scan_prompt(_scanners, text)
    failed = [name for name, ok in results_valid.items() if not ok]

    # Los scanners de REDACCIÓN no bloquean: cuando "fallan" es que encontraron algo
    # y ya lo redactaron en `sanitized`. Solo bloquean los scanners de GATE.
    blocking = [name for name in failed if name not in REDACTING_SCANNERS]
    redacted = [name for name in failed if name in REDACTING_SCANNERS]
    if redacted:
        logger.info("Input saneado por scanners de redacción %s", redacted)

    if blocking:
        logger.warning("Input bloqueado por scanners %s (scores=%s)", blocking, results_score)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "input_rejected",
                "message": "El contenido fue bloqueado por la capa de seguridad.",
                "blocked_by": blocking,
                "scores": results_score,
            },
        )
    return GuardResult(sanitized=sanitized, scores=results_score)


def _content_to_text(content: Any) -> str | None:
    """Extrae texto escaneable de un `content` (str o partes multimodales)."""
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


def apply(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aplica política estructural + escaneo a una lista de mensajes (dicts).

    Devuelve la lista de mensajes saneada lista para enviar al backend.
    """
    settings = get_settings()
    out: list[dict[str, Any]] = []

    # 1) Defensa estructural: system prompt fijo en servidor al frente.
    if settings.system_prompt:
        out.append({"role": "system", "content": settings.system_prompt})

    for msg in messages:
        role = msg.get("role")

        # 2) Descartar 'system' del cliente si así se configuró.
        if role == "system" and settings.drop_client_system_messages:
            logger.info("Descartado mensaje system del cliente (política)")
            continue

        # 3) Escanear+sanear el contenido de los mensajes de usuario.
        if role == "user" and settings.guard_enabled:
            text = _content_to_text(msg.get("content"))
            if text:
                result = _scan_text(text)
                if isinstance(msg.get("content"), str):
                    msg = {**msg, "content": result.sanitized}
                # Para contenido multimodal no reemplazamos las partes; el escaneo
                # sirve como gate (bloquea si falla) sobre el texto concatenado.

        out.append(msg)

    return out
