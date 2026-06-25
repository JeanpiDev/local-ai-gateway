"""Provisión de usuarios reales en Open WebUI (endpoints /admin/*).

Automatiza el flujo verificado:
  1. (setup) habilita el permiso default features.api_keys
  2. crea el usuario real           POST /api/v1/auths/add
  3. signin como el usuario          -> JWT
  4. genera su API key               POST /api/v1/auths/api_key
  5. (baja) borra el usuario         DELETE /api/v1/users/{id}

Protegido por la key bootstrap del gateway (header X-Admin-Key). Usa la
GATEWAY_OPENWEBUI_ADMIN_KEY (key de servicio admin) para hablar con Open WebUI.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from .. import upstream
from ..auth import AdminDep
from ..config import get_settings
from ..schemas import ProvisionUserRequest, ProvisionUserResponse

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[AdminDep],
    responses={
        403: {"description": "X-Admin-Key inválida o ausente"},
        503: {"description": "Key admin del gateway no configurada"},
    },
)


def _admin_key() -> str:
    key = get_settings().openwebui_admin_key
    if not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GATEWAY_OPENWEBUI_ADMIN_KEY no configurada",
        )
    return key


@router.post("/setup", summary="Habilitar generación de API keys en Open WebUI")
async def setup_permissions():
    """Habilita el permiso default `features.api_keys` para que los usuarios puedan generar key.
    Ejecútalo **una sola vez** antes de provisionar usuarios."""
    admin_key = _admin_key()
    r = await upstream.admin_get_default_permissions(admin_key)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail="No se pudieron leer permisos")
    perms = r.json()
    perms.setdefault("features", {})["api_keys"] = True
    r2 = await upstream.admin_set_default_permissions(admin_key, perms)
    if r2.status_code != 200:
        raise HTTPException(status_code=r2.status_code, detail="No se pudieron actualizar permisos")
    return {"ok": True, "features.api_keys": r2.json().get("features", {}).get("api_keys")}


@router.post(
    "/users",
    status_code=201,
    response_model=ProvisionUserResponse,
    summary="Provisionar un usuario y emitir su API key",
)
async def provision_user(body: ProvisionUserRequest):
    """Crea un usuario real en Open WebUI (add → signin → api_key) y devuelve su `sk-...`."""
    admin_key = _admin_key()

    # 1) Crear usuario
    r = await upstream.admin_add_user(admin_key, body.name, body.email, body.password, body.role)
    if r.status_code != 200:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=detail)
    user = r.json()
    user_id = user.get("id")

    # 2) Signin como el usuario para obtener su JWT
    s = await upstream.signin(body.email, body.password)
    if s.status_code != 200:
        raise HTTPException(status_code=502, detail="Usuario creado pero falló el signin")
    user_token = s.json().get("token")

    # 3) Generar su API key
    k = await upstream.create_api_key(user_token)
    if k.status_code != 200:
        try:
            detail = k.json()
        except Exception:
            detail = k.text
        raise HTTPException(
            status_code=502,
            detail={"message": "Usuario creado pero no se pudo generar la API key. "
                              "¿Ejecutaste POST /admin/setup?", "upstream": detail},
        )
    api_key = k.json().get("api_key")

    return {
        "id": user_id,
        "name": body.name,
        "email": body.email,
        "role": body.role,
        "api_key": api_key,
    }


@router.delete("/users/{user_id}", summary="Borrar un usuario de Open WebUI")
async def delete_user(user_id: str):
    admin_key = _admin_key()
    r = await upstream.admin_delete_user(admin_key, user_id)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail="No se pudo borrar el usuario")
    return {"ok": True, "deleted": user_id}
