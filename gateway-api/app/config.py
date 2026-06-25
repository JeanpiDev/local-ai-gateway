"""Configuración central del gateway (pydantic-settings, leída de variables de entorno)."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_", env_file=".env", extra="ignore")

    # ── Backend Open WebUI ───────────────────────────────────────────────────
    # Dentro de docker-compose se resuelve por DNS de la red interna (ai_net).
    openwebui_base_url: str = "http://open-webui:8080"
    # Key de servicio (admin) usada SOLO para provisión de usuarios. NO se usa
    # para reenviar chats: cada persona usa su propia key (identity-transparent).
    openwebui_admin_key: str = ""
    # Timeout (s) para llamadas al backend. La inferencia en CPU es lenta.
    upstream_timeout: float = 600.0

    # ── Autenticación ────────────────────────────────────────────────────────
    # TTL (s) de la caché de validación de keys contra Open WebUI.
    auth_cache_ttl: float = 60.0
    # Key bootstrap que protege los endpoints /admin/* de provisión.
    admin_bootstrap_key: str = ""

    # ── Anti prompt-injection (llm-guard) ────────────────────────────────────
    guard_enabled: bool = True
    guard_use_onnx: bool = False          # ONNX acelera en CPU si está disponible
    guard_prompt_injection_threshold: float = 0.95
    # Modelo de detección de prompt-injection. Vacío = modelo por defecto de
    # llm-guard (protectai, SOLO INGLÉS: da falsos positivos con órdenes en
    # español). Para producción en español usar un modelo multilingüe (mDeBERTa),
    # p.ej. "meta-llama/Llama-Prompt-Guard-2-86M" (requiere licencia/token HF).
    guard_prompt_injection_model: str = ""
    guard_token_limit: int = 4096
    # Subcadenas prohibidas extra (separadas por coma) — lista negra propia.
    guard_ban_substrings: str = ""

    # ── Política del system prompt (defensa estructural) ─────────────────────
    # Si hay system prompt fijo, se antepone y se descartan los 'system' del cliente.
    system_prompt: str = ""
    drop_client_system_messages: bool = True

    # ── Concurrencia ─────────────────────────────────────────────────────────
    # Peticiones simultáneas permitidas hacia el backend (techo = CPU/NUM_PARALLEL).
    max_concurrency: int = 2
    # Tiempo máx (s) esperando un slot libre antes de devolver 429.
    queue_timeout: float = 30.0

    @property
    def ban_substrings_list(self) -> list[str]:
        return [s.strip() for s in self.guard_ban_substrings.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
