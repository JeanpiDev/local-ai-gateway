"""Autenticación identity-transparent.

El cliente envía su PROPIA API key de Open WebUI (`Authorization: Bearer sk-...`).
La validamos contra el backend (con caché TTL para no golpearlo en cada request)
y, si es válida, dejamos pasar y reenviamos esa misma key aguas abajo.

Se exponen esquemas de seguridad OpenAPI (HTTPBearer y APIKeyHeader) para que
Swagger UI muestre el botón "Authorize" y documente la seguridad de cada ruta.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from . import upstream
from .config import get_settings

# Esquemas de seguridad (auto_error=False para controlar nosotros el 401/403).
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="API key personal de Open WebUI (formato `sk-...`).",
)
admin_scheme = APIKeyHeader(
    name="X-Admin-Key",
    auto_error=False,
    description="Key bootstrap del gateway que protege los endpoints /admin/*.",
)


@dataclass
class AuthContext:
    token: str                 # la key tal cual, para reenviar al backend
    user: dict[str, Any]       # usuario real de Open WebUI

    @property
    def user_id(self) -> str:
        return str(self.user.get("id", "unknown"))


# Caché simple en memoria: token -> (AuthContext, expiry_monotonic)
_cache: dict[str, tuple[AuthContext, float]] = {}


def _purge_expired(now: float) -> None:
    """Elimina las entradas vencidas (evita que la caché crezca sin límite)."""
    expired = [tok for tok, (_, expiry) in list(_cache.items()) if expiry <= now]
    for tok in expired:
        _cache.pop(tok, None)


async def get_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthContext:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta el header Authorization: Bearer <api_key>",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials.strip()
    settings = get_settings()

    now = time.monotonic()
    cached = _cache.get(token)
    if cached and cached[1] > now:
        return cached[0]

    user = await upstream.get_user(token)
    if user is None:
        # No cacheamos los fallos para permitir reintentos inmediatos tras provisión.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key inválida o revocada",
            headers={"WWW-Authenticate": "Bearer"},
        )

    ctx = AuthContext(token=token, user=user)
    _purge_expired(now)   # barrido oportunista en el camino de cache-miss (poco frecuente)
    _cache[token] = (ctx, now + settings.auth_cache_ttl)
    return ctx


def require_admin_bootstrap(api_key: str | None = Depends(admin_scheme)) -> None:
    """Protege los endpoints /admin/* con la key bootstrap del gateway."""
    settings = get_settings()
    if not settings.admin_bootstrap_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GATEWAY_ADMIN_BOOTSTRAP_KEY no configurada; provisión deshabilitada",
        )
    if api_key != settings.admin_bootstrap_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="X-Admin-Key inválida o ausente",
        )


# Alias de dependencia para inyección
AuthDep = Depends(get_auth)
AdminDep = Depends(require_admin_bootstrap)
