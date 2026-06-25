"""Etapas concretas del guard, configuradas desde la política.

- `PolicyStructureStage` (etapa 1): defensa estructural — system prompt fijo, descarte
  de `system` del cliente, whitelist de roles y límites. Determinista, fail-closed.
- `HeuristicsStage` (etapa 2): regex multi-idioma (ES/EN) de injection/jailbreak.
  Corta ataques obvios SIN cargar el modelo pesado. Barata, fail-closed.
- `LLMGuardStage` (etapas 3+4): escaneo llm-guard. Redacción sanea; gate bloquea.
  Fail-open (etapa pesada).

Etapa futura (Output Guard): ver `docs/DESIGN.md`.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from ..config import get_settings
from ..policy import Policy
from .pipeline import GuardContext, Stage, StageAction, StageResult

logger = logging.getLogger("gateway.guard")

# Scanners que REDACTAN (sanean el texto) en vez de bloquear la petición.
REDACTING_SCANNERS = {"Anonymize", "Secrets", "BanSubstrings"}


def content_to_text(content: Any) -> str | None:
    """Extrae texto escaneable de un `content` (str o partes multimodales OpenAI)."""
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


class PolicyStructureStage(Stage):
    name = "PolicyStructure"
    fail_mode = "closed"

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def run(self, ctx: GuardContext) -> StageResult:
        p = self.policy
        out: list[dict] = []

        if p.system_prompt:
            out.append({"role": "system", "content": p.system_prompt})

        for msg in ctx.messages:
            role = msg.get("role")

            if role == "system" and p.roles.drop_client_system:
                logger.info("Descartado mensaje system del cliente (política)")
                continue

            if role not in p.roles.allowed:
                return StageResult(
                    action=StageAction.BLOCK,
                    reason=f"rol '{role}' no permitido",
                    detail={"error": "role_not_allowed", "role": role, "allowed": p.roles.allowed},
                )

            if p.limits.max_chars_per_message is not None:
                text = msg.get("content")
                if isinstance(text, str) and len(text) > p.limits.max_chars_per_message:
                    return StageResult(
                        action=StageAction.BLOCK,
                        reason="mensaje excede el tamaño máximo",
                        detail={"error": "message_too_large", "limit": p.limits.max_chars_per_message},
                    )

            out.append(msg)

        if p.limits.max_messages is not None and len(out) > p.limits.max_messages:
            return StageResult(
                action=StageAction.BLOCK,
                reason="demasiados mensajes",
                detail={"error": "too_many_messages", "limit": p.limits.max_messages},
            )

        ctx.messages = out
        return StageResult(action=StageAction.ALLOW)


# ── Patrones de injection/jailbreak (ES + EN). Alta precisión: frases explícitas de
#    ataque, para minimizar falsos positivos sobre texto legítimo. Ampliables por política.
DEFAULT_INJECTION_PATTERNS: list[str] = [
    # — Ignorar/olvidar instrucciones previas —
    r"ignore\s+(all\s+|any\s+)?(of\s+)?(the\s+)?(previous|prior|above|preceding)\s+(instructions?|prompts?|messages?|rules?)",
    r"disregard\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|your)\s+(instructions?|rules?|guidelines?|prompt)",
    r"forget\s+(all\s+|everything\s+|your\s+)?(previous\s+)?(instructions?|rules?|context)",
    r"ignor(a|es|en|e|ar)\s+(que\s+)?(todas?\s+)?(las\s+|tus\s+)?(instrucciones|reglas|[oó]rdenes|indicaciones|normas|restricciones|filtros|pol[ií]tica\s+de\s+contenido)(\s+(previas|anteriores|de\s+arriba))?",
    r"(olv[ií]da(te)?|haz\s+caso\s+omiso)\s+(de\s+)?(todas?\s+)?(las\s+|tus\s+)?(instrucciones|reglas|indicaciones)",
    r"no\s+sigas\s+(las\s+|tus\s+)?(instrucciones|reglas|normas)",
    # — Revelar/mostrar el system prompt —
    r"(reveal|show|print|repeat|display|expose|tell\s+me)\s+(me\s+)?(your\s+|the\s+)?(system\s+|initial\s+|original\s+)?(prompt|instructions?)",
    r"(revela|mu[eé]strame?|imprime|repite|dime|ens[eé][ñn]ame)\s+(tu|el|las)\s+(system\s+)?(prompt|instrucciones\s+(del\s+sistema|iniciales)|indicaciones\s+del\s+sistema)",
    # — Jailbreak / personas sin restricciones —
    r"jailbreak",
    r"do\s+anything\s+now",
    r"(developer|dev|god|admin|root)\s+mode",
    r"modo\s+(desarrollador|dios|administrador|sin\s+restricciones)",
    r"(you\s+are\s+now|act\s+as|pretend\s+to\s+be|you\s+must\s+act\s+as)\s+(an?\s+)?(dan|unrestricted|jailbroken|uncensored)",
    r"(act[uú]a|comp[oó]rtate|finge)\s+(como|que\s+eres)\s+(un[ao]?\s+)?(dan|asistente\s+sin\s+(restricciones|l[ií]mites|filtros)|ia\s+sin\s+restricciones)",
    # — Anular restricciones / bypass —
    r"(with\s+no|without\s+any|no)\s+(restrictions|limitations|rules|filters|content\s+policy|guardrails)",
    r"bypass\s+(your\s+|the\s+|all\s+)?(safety|security|guardrails|filters|restrictions|rules)",
    r"(sin|ignorando)\s+(ninguna\s+)?(restricci[oó]n|restricciones|l[ií]mites|reglas|filtros|pol[ií]tica\s+de\s+contenido)",
    r"(evita|s[aá]ltate|omite|esquiva)\s+(las\s+|tus\s+|los\s+)?(restricciones|reglas|filtros|medidas\s+de\s+seguridad|controles)",
    # — Tokens de plantilla de chat (inyección de rol) —
    r"<\|?im_start\|?>|<\|?im_end\|?>|<<sys>>|\[/?inst\]",
]


class HeuristicsStage(Stage):
    name = "Heuristics"
    fail_mode = "closed"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        params = params or {}
        patterns = [] if params.get("disable_defaults") else list(DEFAULT_INJECTION_PATTERNS)
        patterns += params.get("extra_patterns", []) or []
        self._compiled = [(p, re.compile(p, re.IGNORECASE | re.UNICODE)) for p in patterns]
        logger.info("Heuristics: %d patrones cargados", len(self._compiled))

    def run(self, ctx: GuardContext) -> StageResult:
        for msg in ctx.messages:
            if msg.get("role") != "user":
                continue
            text = content_to_text(msg.get("content"))
            if not text:
                continue
            for pattern, rx in self._compiled:
                if rx.search(text):
                    logger.warning("Heuristics bloqueó: patrón %r", pattern)
                    return StageResult(
                        action=StageAction.BLOCK,
                        score=1.0,
                        reason="patrón de injection detectado por heurísticas",
                        detail={
                            "error": "input_rejected",
                            "message": "El contenido fue bloqueado por la capa de seguridad.",
                            "blocked_by": ["Heuristics"],
                            "matched": pattern,
                        },
                    )
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

    def run(self, ctx: GuardContext) -> StageResult:
        if not get_settings().guard_enabled:
            return StageResult(action=StageAction.ALLOW)

        if self._scanners is None:
            self._scanners = self._build_scanners()

        from llm_guard import scan_prompt

        sanitized_any = False
        for i, msg in enumerate(ctx.messages):
            if msg.get("role") != "user":
                continue
            text = content_to_text(msg.get("content"))
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


class PromptGuardStage(Stage):
    """Detección de injection con un modelo de clasificación MULTILINGÜE (transformers).

    Alternativa multilingüe al PromptInjection (inglés) de llm-guard. Pensada para
    Meta Llama Prompt Guard 2 (mDeBERTa, gated → requiere token HF), pero sirve con
    cualquier clasificador binario de texto vía mapeo de etiquetas. Fail-open.
    """
    name = "PromptGuard"
    fail_mode = "open"

    DEFAULT_MODEL = "meta-llama/Llama-Prompt-Guard-2-86M"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        params = params or {}
        self.model = params.get("model") or self.DEFAULT_MODEL
        self.threshold: float = params.get("threshold", 0.5)
        self.max_length: int = params.get("max_length", 512)
        self.malicious_labels = {
            str(label).lower()
            for label in params.get("malicious_labels", ["label_1", "malicious", "injection", "jailbreak"])
        }
        self.hf_token: str = (
            params.get("hf_token")
            or get_settings().hf_token
            or os.environ.get("HF_TOKEN", "")
        )
        self._pipe: Any = None

    def _build(self) -> None:
        from transformers import pipeline

        self._pipe = pipeline(
            "text-classification",
            model=self.model,
            top_k=None,                 # devuelve todas las etiquetas con score
            truncation=True,
            max_length=self.max_length,
            token=self.hf_token or None,
        )
        logger.info("PromptGuard cargado: %s", self.model)

    def warmup(self) -> None:
        if get_settings().guard_enabled and self._pipe is None:
            self._build()

    def _malicious_score(self, text: str) -> float:
        results = self._pipe(text)
        # `top_k=None` puede devolver list[dict] o list[list[dict]] según versión.
        scores = results[0] if results and isinstance(results[0], list) else results
        for entry in scores:
            if str(entry.get("label", "")).lower() in self.malicious_labels:
                return float(entry.get("score", 0.0))
        return 0.0

    def run(self, ctx: GuardContext) -> StageResult:
        if not get_settings().guard_enabled:
            return StageResult(action=StageAction.ALLOW)
        if self._pipe is None:
            self._build()

        for msg in ctx.messages:
            if msg.get("role") != "user":
                continue
            text = content_to_text(msg.get("content"))
            if not text:
                continue
            score = self._malicious_score(text)
            if score >= self.threshold:
                logger.warning("PromptGuard bloqueó (score=%.3f, modelo=%s)", score, self.model)
                return StageResult(
                    action=StageAction.BLOCK,
                    score=score,
                    reason="injection detectado por modelo multilingüe",
                    detail={
                        "error": "input_rejected",
                        "message": "El contenido fue bloqueado por la capa de seguridad.",
                        "blocked_by": ["PromptGuard"],
                        "score": round(score, 4),
                        "model": self.model,
                    },
                )
        return StageResult(action=StageAction.ALLOW)
