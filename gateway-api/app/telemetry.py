"""Telemetría en memoria del guard (Fase 6: observabilidad).

Mantiene contadores agregados y un buffer circular de eventos recientes para que
los endpoints /admin/metrics y /admin/audit muestren qué está bloqueando el guard.
Es estado en memoria del proceso (se reinicia al recrear el contenedor) — suficiente
para diagnóstico/operación; para métricas persistentes se exportaría a Prometheus.
"""
from __future__ import annotations

import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Any

_lock = threading.Lock()
_counters: Counter = Counter()
_recent: deque[dict[str, Any]] = deque(maxlen=200)
_started_at = time.time()


def record_input(user_id: str, outcome: str, blocked_stage: str | None,
                 audit: list[dict], latency_ms: float) -> None:
    """Registra el resultado del pipeline de ENTRADA para una petición."""
    with _lock:
        _counters["requests_total"] += 1
        _counters[f"outcome:{outcome}"] += 1
        if blocked_stage:
            _counters[f"block:{blocked_stage}"] += 1
        for a in audit:
            if a.get("action") == "sanitize":
                _counters[f"sanitize:{a['stage']}"] += 1
            if "fail-open" in (a.get("reason") or ""):
                _counters[f"degraded:{a['stage']}"] += 1
        _recent.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "phase": "input",
            "user_id": user_id,
            "outcome": outcome,
            "blocked_stage": blocked_stage,
            "latency_ms": round(latency_ms, 1),
            "stages": audit,
        })


def record_output(user_id: str, action: str, reason: str) -> None:
    """Registra una decisión del OutputGuard (block/sanitize)."""
    with _lock:
        _counters[f"output:{action}"] += 1
        _recent.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "phase": "output",
            "user_id": user_id,
            "outcome": action,
            "reason": reason,
        })


def metrics() -> dict[str, Any]:
    with _lock:
        return {
            "uptime_s": round(time.time() - _started_at, 1),
            "counters": dict(_counters),
        }


def recent(limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        items = list(_recent)
    return items[-limit:][::-1]   # más recientes primero
