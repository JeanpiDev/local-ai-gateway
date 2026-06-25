"""Aplicación FastAPI: gateway de seguridad/estandarización delante de Open WebUI."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import concurrency, upstream
from .config import get_settings
from .routes import admin, chat, models
from .security import prompt_guard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    upstream.init_client()
    concurrency.init_semaphore()
    logger.info("Backend Open WebUI: %s", settings.openwebui_base_url)
    logger.info("Concurrencia máx: %d | guard: %s", settings.max_concurrency, settings.guard_enabled)
    if settings.guard_enabled:
        logger.info("Pre-cargando llm-guard (puede tardar la primera vez)...")
        try:
            prompt_guard.warmup()
        except Exception as e:  # no tumbar el arranque si el guard falla al cargar
            logger.exception("Fallo al pre-cargar llm-guard: %s", e)
    yield
    await upstream.close_client()


DESCRIPTION = """
Capa **FastAPI** delante de Open WebUI que aporta seguridad y estandarización.

* 🔐 **Auth identity-transparent** — el cliente usa su **propia API key de Open WebUI**
  (`Authorization: Bearer sk-...`); el gateway la valida y la reenvía aguas abajo.
* 🛡️ **Anti prompt-injection** con llm-guard (escaneo de mensajes `user` + system prompt fijo).
* 🚦 **Control de concurrencia** — semáforo + cola con timeout (`429` si se satura).
* 🧩 **Compatible OpenAI** — `/v1/chat/completions` (streaming) y `/v1/models`.
* 👤 **Provisión de usuarios** reales en Open WebUI vía `/admin/*`.

Autoriza con el botón **Authorize** (Bearer para consumo, `X-Admin-Key` para administración).
"""

TAGS_METADATA = [
    {"name": "chat", "description": "Inferencia compatible con OpenAI (con guard + concurrencia)."},
    {"name": "models", "description": "Listado de modelos disponibles para el usuario."},
    {"name": "admin", "description": "Provisión de usuarios reales en Open WebUI. Requiere `X-Admin-Key`."},
    {"name": "meta", "description": "Endpoints de salud y operativos."},
]

app = FastAPI(
    title="Local AI Gateway API",
    description=DESCRIPTION,
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=TAGS_METADATA,
    contact={"name": "Local AI Gateway", "url": "http://localhost:8090/docs"},
    license_info={"name": "Interno"},
    docs_url="/docs",
    redoc_url="/redoc",
)

app.include_router(models.router)
app.include_router(chat.router)
app.include_router(admin.router)


@app.get(
    "/health",
    tags=["meta"],
    summary="Liveness probe",
    response_description="Estado del servicio",
)
async def health() -> dict[str, str]:
    """Devuelve `{\"status\": \"ok\"}` si el proceso está vivo."""
    return {"status": "ok"}
