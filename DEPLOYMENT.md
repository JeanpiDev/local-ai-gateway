# Despliegue en producción

Guía para desplegar el stack (Ollama + Open WebUI + gateway-api con el guard
anti prompt-injection) en el servidor de producción **sin GPU, CPU potente, ~90 GB RAM**.

> Contexto de rendimiento: el CPU es el techo de throughput (ver
> [gateway-api/docs/CONCURRENCY.md](gateway-api/docs/CONCURRENCY.md)). La RAM permite
> modelos grandes y varios slots, no más velocidad por petición.

## 0. Requisitos del servidor
- Docker + Docker Compose v2.
- ~30 GB de disco libres (modelo 14B ~9 GB + imagen del guard ~9 GB + cachés).
- Salida a internet para la primera descarga de modelos (Ollama y HuggingFace).

## 1. Secretos y `.env` (CRÍTICO — no reusar los de dev)

```bash
cp .env.example .env
```

Edita `.env`:

| Variable | Valor en prod |
|---|---|
| `OLLAMA_MODEL` | `qwen2.5:14b-instruct` |
| `OLLAMA_NUM_PARALLEL` | `4` (hay RAM) |
| `MAX_CONCURRENCY` | `4` (alineado con NUM_PARALLEL) |
| `INSTALL_GUARD` | `true` |
| `GUARD_ENABLED` | `true` |
| `LLM_API_KEY` | **generar**: `python -c "import secrets;print(secrets.token_hex(24))"` |
| `ADMIN_BOOTSTRAP_KEY` | **generar fuerte** (protege `/admin/*`) |
| `HF_TOKEN` | token de HuggingFace con la licencia Llama 4 aceptada |
| `OPENWEBUI_ADMIN_KEY` | se rellena en el paso 3 (key del admin de la OWUI de prod) |

`.env` está en `.gitignore` — nunca se commitea.

## 2. Levantar Ollama + Open WebUI y descargar el modelo

```bash
docker compose up -d ollama open-webui
docker compose logs -f ollama-init      # espera "Modelo listo" (descarga ~9 GB)
```

## 3. Crear el admin de Open WebUI y su API key de servicio

1. Abre `http://<host>:3000` → el **primer registro se vuelve admin**. Créalo.
2. Admin Panel → Settings → habilita API Keys (o se hará en el paso 5 con `/admin/setup`).
3. Settings → Account → **Generate New API Key** → cópiala (empieza por `sk-`).
4. Pégala en `.env` como `OPENWEBUI_ADMIN_KEY=sk-...` (es solo para provisión).

## 4. Construir y levantar el gateway-api (con guard)

```bash
docker compose build --build-arg INSTALL_GUARD=true gateway-api
docker compose up -d gateway-api
# Primer arranque: descarga el modelo del guard (gated, ~min). Sigue el progreso:
docker compose logs -f gateway-api       # espera "Application startup complete"
```

> El modelo gated `meta-llama/Llama-Prompt-Guard-2-86M` requiere haber **aceptado la
> licencia** en su página de HuggingFace y un `HF_TOKEN` válido. Queda cacheado en el
> volumen `gateway_api_cache` (no se re-descarga).

## 5. `policy.yaml` de producción

Edita [gateway-api/policy.yaml](gateway-api/policy.yaml) (está montado en el contenedor):

```yaml
system_prompt: "<el system prompt de tu empresa>"
models:
  allowed: [qwen2.5:14b-instruct]    # rechaza otros modelos
stages:
  - { name: PromptGuard, enabled: true, ... }   # activar la capa multilingüe
```

Recrea para aplicar: `docker compose up -d gateway-api`.

Habilita la generación de API keys de usuario (una vez):
```bash
curl -X POST http://localhost:8090/admin/setup -H "X-Admin-Key: $ADMIN_BOOTSTRAP_KEY"
```

## 6. Verificación automática

```bash
GATEWAY_URL=http://localhost:8090 \
ADMIN_KEY=<tu ADMIN_BOOTSTRAP_KEY> \
MODEL=qwen2.5:14b-instruct \
bash scripts/verify-prod.sh
```
Valida health, setup, provisión, modelos, chat benigno, ataque ES (422) y métricas.
Además, prueba un **ataque sutil en español** que solo el modelo multilingüe atrape, para
confirmar que `PromptGuard` está activo y funcionando.

## 7. Onboarding de usuarios

```bash
curl -X POST http://localhost:8090/admin/users \
  -H "X-Admin-Key: $ADMIN_BOOTSTRAP_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"Ana","email":"ana@empresa.com","password":"...","role":"user"}'
# -> devuelve la api_key (sk-...) que se le entrega a la persona
```

## 8. Exposición externa (TLS + dominio)
- Pon un reverse proxy con **TLS** (Caddy/Traefik/nginx) delante.
- Expón **solo** lo necesario: `gateway-api` (8090, la API) y, si aplica, Open WebUI (3000).
- **Ollama nunca** al exterior (ya está en red interna `ai_net`).
- Decide si retiras el gateway nginx legacy (`:8080`) — el FastAPI lo reemplaza.

## 9. Operación
- **Salud:** `docker compose ps` (el gateway-api tiene healthcheck con start_period amplio).
- **Observabilidad:** `GET /admin/metrics` y `GET /admin/audit` (con `X-Admin-Key`).
  Es estado en memoria; para métricas persistentes, exportar a Prometheus/logs.
- **Backups:** respalda el volumen `open_webui_data` (usuarios, chats y **API keys**).
  `ollama_data` y `gateway_api_cache` son regenerables.
- **Rotación:** rota `LLM_API_KEY`, `ADMIN_BOOTSTRAP_KEY` y `HF_TOKEN` si se filtran.

## Checklist rápido
- [ ] `.env` con secretos nuevos y `INSTALL_GUARD=true`, `GUARD_ENABLED=true`
- [ ] Modelo `qwen2.5:14b-instruct` descargado
- [ ] Admin de OWUI creado + `OPENWEBUI_ADMIN_KEY` puesta
- [ ] Licencia Llama 4 aceptada + `HF_TOKEN`
- [ ] `policy.yaml`: `models.allowed`, `system_prompt`, `PromptGuard` enabled
- [ ] `POST /admin/setup` ejecutado
- [ ] `scripts/verify-prod.sh` en verde
- [ ] TLS + reverse proxy + dominio
- [ ] Backups de `open_webui_data` programados
