# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Documentación detallada en **[docs/](docs/)**: `DESIGN.md` (pipeline del guard),
`CONCURRENCY.md` (rendimiento/colas), `DEPLOYMENT.md` (producción).

## Qué es esto

Stack Docker Compose de **IA local autohospedada** sobre Ollama. Hay **dos puertas de
entrada distintas** a los mismos modelos, y entender la diferencia es clave:

1. **`gateway` (nginx, puerto `GATEWAY_PORT`/8080)** — gateway *legacy*, simple. Valida un
   único `Authorization: Bearer <LLM_API_KEY>` (vía `map` de nginx) y hace proxy **directo a
   Ollama** (`/v1/...` OpenAI-compatible). Una sola key compartida. Ver `gateway/nginx.conf.template`
   + `gateway/entrypoint.sh` (sustituye `__LLM_API_KEY__` con `sed`, no envsubst).

2. **`gateway-api` (FastAPI, puerto `GATEWAY_API_PORT`/8090)** — gateway *nuevo* con seguridad.
   Apunta a **Open WebUI** (no a Ollama directo) y añade auth multiusuario, anti
   prompt-injection (llm-guard) y control de concurrencia. **Es donde se desarrolla activamente.**

Flujo del gateway-api: cliente → valida su **propia API key de Open WebUI** (identity-transparent,
con caché TTL) → llm-guard sobre los mensajes `user` → semáforo de concurrencia → reenvía a
Open WebUI **con la misma key** (así Open WebUI atribuye todo al usuario real).

```
proyectos ─Bearer LLM_API_KEY─►  gateway (nginx)   ──► ollama          (legacy, key única)
clientes  ─Bearer sk-...──────►  gateway-api (FastAPI) ──► open-webui ──► ollama  (seguro)
humanos   ──────────────────►   open-webui :3000     ──► ollama
```

## Comandos

No hay sistema de build del repo ni suite de tests automatizada; la verificación se hace
end-to-end con `curl`/PowerShell contra los contenedores. Todo es Docker Compose.

```bash
docker compose up -d                       # levanta todo el stack
docker compose logs -f ollama-init         # progreso de la descarga del modelo
docker compose exec ollama ollama pull <m> # descargar un modelo adicional

# gateway-api (imagen): DEV ligera sin torch / PROD con llm-guard
docker compose build --build-arg INSTALL_GUARD=false gateway-api   # dev (~684 MB)
docker compose build --build-arg INSTALL_GUARD=true  gateway-api   # prod (~9 GB, torch)
docker compose up -d --force-recreate gateway-api

# gateway-api en local sin Docker (desde gateway-api/):
#   apunta GATEWAY_OPENWEBUI_BASE_URL al Open WebUI publicado (http://localhost:3000)
pip install -r requirements.txt            # + requirements-guard.txt si quieres el guard
uvicorn app.main:app --reload --port 8000
```

Docs interactivas del gateway-api: **`/docs`** (Swagger), `/redoc`, `/openapi.json`.

## Configuración (`.env`)

Variables del gateway-api llevan prefijo `GATEWAY_` dentro del contenedor, pero el
`docker-compose.yml` las mapea desde nombres cortos en `.env` (p.ej. `GUARD_ENABLED`,
`MAX_CONCURRENCY`, `GUARD_PI_MODEL`). El código las lee con `pydantic-settings` en
`gateway-api/app/config.py` — esa es la fuente de verdad de todas las opciones y sus defaults.

**DEV vs PROD** (se cambia por `.env`, no por código):
- **DEV** (PC 16 GB, sin GPU): `OLLAMA_MODEL=qwen2.5:3b-instruct`, `INSTALL_GUARD=false`,
  `GUARD_ENABLED=false`. El guard (torch, imagen 9 GB) **no cabe** junto a Ollama+Open WebUI
  en 16 GB — provoca OOM. Mantenerlo apagado en dev.
- **PROD** (servidor 90 GB, sin GPU): `OLLAMA_MODEL=qwen2.5:14b-instruct`, `INSTALL_GUARD=true`,
  `GUARD_ENABLED=true`, y `ADMIN_BOOTSTRAP_KEY` real.

## Restricciones y gotchas (descubiertos en runtime)

- **Throughput**: Ollama corre en **CPU** con `OLLAMA_NUM_PARALLEL=1` → serializa la inferencia.
  Abrir más usuarios NO aumenta capacidad; solo crece la cola. La RAM permite modelos más
  grandes/contexto, no más velocidad. Por eso `gateway-api` limita con `MAX_CONCURRENCY` y
  devuelve `429` cuando se satura (`app/concurrency.py`).
- **Acceso a modelos**: Open WebUI oculta los modelos a usuarios normales por su control de
  acceso. El stack usa `BYPASS_MODEL_ACCESS_CONTROL=true` en el servicio `open-webui` para que
  cualquier usuario con cuenta válida los use (el control de *quién entra* lo hace el gateway).
- **Provisión de usuarios** (`/admin/*`, ver `app/routes/admin.py`): el flujo es habilitar el
  permiso default `features.api_keys` (POST `/api/v1/users/default/permissions`) → `POST
  /api/v1/auths/add` → signin → `POST /api/v1/auths/api_key`. Sin el permiso, los no-admin no
  pueden generar key ("API key creation is not allowed in the environment").
- **llm-guard es inglés-céntrico**: el modelo por defecto (protectai) da **falsos positivos
  1.0 con órdenes en español** ("Resume este texto…"). Subir el umbral no ayuda. Para español
  hay que usar un modelo multilingüe (mDeBERTa) vía `GUARD_PI_MODEL` (configurable, sin tocar
  código). Los scanners de redacción (`Anonymize`/`Secrets`/`BanSubstrings`) **sanean**, no
  bloquean; solo `PromptInjection`/`TokenLimit` bloquean (`REDACTING_SCANNERS` en
  `app/security/prompt_guard.py`).
- **Imports de llm-guard son perezosos**: solo se cargan si `GUARD_ENABLED=true`. Permite correr
  la imagen ligera sin torch. La caché de modelos persiste en el volumen `gateway_api_cache`.

## El guard: pipeline por etapas declarado por política

El guard NO es monolítico: es un **pipeline de etapas** (`security/pipeline.py`) que se
construye **desde la política** (`policy.py` carga `policy.yaml`, o la deriva de env si no
existe). `security/prompt_guard.py` arma el pipeline vía `STAGE_REGISTRY` y expone
`apply()`/`get_output_guard()`. Cada etapa implementa `Stage.run(ctx) -> StageResult`
(allow/sanitize/block), corta en el primer `block` y aplica su `fail_mode` (closed=bloquea,
open=degrada) si lanza. Etapas en `security/stages.py`:
- `PolicyStructure` (system prompt fijo, roles, límites) — fail-closed.
- `Heuristics` (regex ES/EN de injection, sin modelo) — fail-closed.
- `LLMGuard` (llm-guard: PromptInjection inglés + redacción Secrets/PII) — fail-open.
- `PromptGuard` (modelo multilingüe Meta Prompt Guard 2, gated) — fail-open.
- `OutputGuard` (`security/output_guard.py`, revisa la RESPUESTA: fuga de system prompt,
  secretos) — se aplica en `routes/chat.py`, no en el pipeline de entrada.

Añadir una etapa = implementar `Stage`, registrarla en `STAGE_REGISTRY` y declararla en
`policy.yaml`. Ver [docs/DESIGN.md](docs/DESIGN.md).

## Estructura de gateway-api/

`app/main.py` (app + lifespan + OpenAPI) · `config.py` (settings) · `policy.py` (modelos
pydantic + carga de `policy.yaml`) · `auth.py` (HTTPBearer + APIKeyHeader, caché) ·
`upstream.py` (cliente httpx a Open WebUI, único punto de llamadas al backend) ·
`concurrency.py` (semáforo/429) · `telemetry.py` (contadores + audit en memoria) ·
`security/` (`pipeline.py`, `stages.py`, `output_guard.py`, `prompt_guard.py`) ·
`routes/{chat,models,admin}.py`. Las rutas exponen `/v1/*` (OpenAI) y alias `/api/*`
(nativos de Open WebUI, ocultos del schema). Observabilidad: `/admin/metrics`, `/admin/audit`.
