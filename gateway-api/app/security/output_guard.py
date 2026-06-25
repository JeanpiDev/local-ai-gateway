"""Output Guard (etapa 5): revisa la RESPUESTA del modelo antes de entregarla.

Checks deterministas (sin modelo → corren también en dev):
- `system_prompt_leak`: detecta si la salida reproduce un fragmento significativo del
  system prompt del servidor → BLOCK (no se entrega).
- `secrets`: redacta secretos que se hayan colado en la salida (API keys, tokens,
  claves privadas) → SANITIZE.
- `ban_substrings`: subcadenas prohibidas en la salida → SANITIZE (redacción).

No es un `Stage` del pipeline de entrada (opera sobre texto de salida, no sobre
`messages`), pero se declara en la política como la etapa "OutputGuard".
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger("gateway.guard")


class OutputAction(str, Enum):
    ALLOW = "allow"
    SANITIZE = "sanitize"
    BLOCK = "block"


@dataclass
class OutputResult:
    action: OutputAction = OutputAction.ALLOW
    text: str = ""
    reason: str = ""


# Patrones de secretos de alta precisión (redactan, no bloquean).
SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED_SLACK_TOKEN]"),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        "[REDACTED_PRIVATE_KEY]",
    ),
]


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


class OutputGuard:
    def __init__(self, system_prompt: str = "", params: dict[str, Any] | None = None) -> None:
        params = params or {}
        self.checks: list[str] = params.get("checks", ["system_prompt_leak", "secrets"])
        self.system_prompt = system_prompt
        self.ban_substrings: list[str] = params.get("ban_substrings", []) or []
        # Para streaming: True = bufferizar y revisar (seguro, pierde streaming incremental);
        # False = dejar pasar el stream sin guard de salida (preserva UX). Ver DESIGN §8.
        self.guard_streaming: bool = params.get("guard_streaming", False)

    def _leaks_system_prompt(self, out_text: str) -> bool:
        sp = _normalize(self.system_prompt)
        if len(sp) < 30:   # demasiado corto para detectar de forma fiable
            return False
        ot = _normalize(out_text)
        win, step = 40, 10
        for i in range(0, len(sp) - win + 1, step):
            if sp[i:i + win] in ot:
                return True
        return False

    def _redact(self, text: str) -> tuple[str, int]:
        total = 0
        for rx, repl in SECRET_PATTERNS:
            text, n = rx.subn(repl, text)
            total += n
        for sub in self.ban_substrings:
            if sub:
                text, n = re.subn(re.escape(sub), "[REDACTED]", text, flags=re.IGNORECASE)
                total += n
        return text, total

    def check(self, text: str) -> OutputResult:
        if not text:
            return OutputResult(action=OutputAction.ALLOW, text=text)

        if "system_prompt_leak" in self.checks and self._leaks_system_prompt(text):
            logger.warning("OutputGuard: posible fuga del system prompt en la respuesta")
            return OutputResult(
                action=OutputAction.BLOCK,
                text="[Respuesta bloqueada por la política de seguridad de salida.]",
                reason="system_prompt_leak",
            )

        new_text = text
        sanitized = False
        if "secrets" in self.checks or self.ban_substrings:
            new_text, n = self._redact(new_text)
            if n:
                logger.info("OutputGuard: %d secreto(s)/subcadena(s) redactada(s) en la salida", n)
                sanitized = True

        if sanitized:
            return OutputResult(action=OutputAction.SANITIZE, text=new_text, reason="secrets_redacted")
        return OutputResult(action=OutputAction.ALLOW, text=text)
