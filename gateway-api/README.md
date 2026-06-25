# Gateway-API

Capa **FastAPI** delante de Open WebUI que aporta:

- 🔐 **Auth identity-transparent**: el cliente usa su **propia API key de Open WebUI**
  (`Authorization: Bearer sk-...`). El gateway la valida contra Open WebUI (con caché),
  aplica seguridad y la **reenvía tal cual** → Open WebUI atribuye todo al usuario real.
- 🛡️ **Anti prompt-injection** con [llm-guard](https://github.com/protectai/llm-guard):
  escanea los mensajes `user`, fuerza un system prompt de servidor y descarta los
  `system` del cliente.
- 🚦 **Control de concurrencia**: semáforo + cola con timeout → `429` si se satura
  (el throughput real lo limita el CPU de Ollama).
- 🧩 **Compatible OpenAI**: `/v1/chat/completions` (streaming incluido) y `/v1/models`.
- 👤 **Provisión de usuarios** reales en Open WebUI vía `/admin/*`.

## Política del guard (`policy.yaml`)

El guard se configura de forma **declarativa**. Copia [policy.example.yaml](policy.example.yaml)
a `policy.yaml`, descoméntalo en el `volumes` del servicio `gateway-api` y ajústalo
(system prompt, roles, límites, modelos permitidos, etapas y su `fail_mode`/params).
Si **no** montas el archivo, el guard deriva la política de las variables `GATEWAY_*`
(comportamiento por defecto, sin cambios). Ruta configurable con `GATEWAY_POLICY_FILE`.

## Documentos relacionados

- [docs/CONCURRENCY.md](docs/CONCURRENCY.md) — concurrencia, colas, 429 y por qué un LLM no escala como una API normal.
- [DESIGN.md](DESIGN.md) — diseño del pipeline anti prompt-injection por etapas (propuesta).

## Documentación interactiva (Swagger / OpenAPI)

Con el servicio levantado:

- **Swagger UI**: http://localhost:8090/docs — explora y prueba endpoints. Usa el botón
  **Authorize** para meter tu `Bearer sk-...` (consumo) y/o tu `X-Admin-Key` (administración).
- **ReDoc**: http://localhost:8090/redoc
- **OpenAPI JSON**: http://localhost:8090/openapi.json (útil para generar clientes/SDKs).

## Endpoints

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| GET | `/health` | — | Liveness |
| GET | `/v1/models` | Bearer (key OWUI) | Lista modelos |
| POST | `/v1/chat/completions` | Bearer (key OWUI) | Chat (OpenAI-compatible, `stream` opc.) |
| POST | `/admin/setup` | `X-Admin-Key` | Habilita permiso `features.api_keys` en OWUI |
| POST | `/admin/users` | `X-Admin-Key` | Crea usuario real + devuelve su `sk-...` |
| DELETE | `/admin/users/{id}` | `X-Admin-Key` | Borra usuario |

## Variables de entorno (`GATEWAY_*`)

| Variable | Default | Descripción |
|---|---|---|
| `GATEWAY_OPENWEBUI_BASE_URL` | `http://open-webui:8080` | Backend |
| `GATEWAY_OPENWEBUI_ADMIN_KEY` | — | Key admin OWUI (solo provisión) |
| `GATEWAY_ADMIN_BOOTSTRAP_KEY` | — | Protege `/admin/*` (`X-Admin-Key`) |
| `GATEWAY_GUARD_ENABLED` | `true` | Activa llm-guard |
| `GATEWAY_GUARD_USE_ONNX` | `false` | Acelera scanners en CPU |
| `GATEWAY_GUARD_PROMPT_INJECTION_THRESHOLD` | `0.9` | Umbral de bloqueo |
| `GATEWAY_MAX_CONCURRENCY` | `2` | Peticiones simultáneas al backend |
| `GATEWAY_QUEUE_TIMEOUT` | `30` | Seg. esperando slot antes de 429 |
| `GATEWAY_SYSTEM_PROMPT` | `""` | System prompt fijo del servidor |

## Build dev vs prod

```bash
# DEV (imagen ligera, sin torch): construye con INSTALL_GUARD=false y GUARD_ENABLED=false
# PROD (con llm-guard):
docker compose build --build-arg INSTALL_GUARD=true gateway-api
```

## Onboarding de un usuario (una vez por persona)

```bash
# 0) (una sola vez) habilitar que los usuarios puedan generar API keys
curl -X POST http://localhost:8090/admin/setup -H "X-Admin-Key: $ADMIN_BOOTSTRAP_KEY"

# 1) crear la persona -> devuelve su sk-...
curl -X POST http://localhost:8090/admin/users \
  -H "X-Admin-Key: $ADMIN_BOOTSTRAP_KEY" -H "Content-Type: application/json" \
  -d '{"name":"Ana","email":"ana@empresa.com","password":"...","role":"user"}'
```

## Consumo (cliente final)

```bash
curl -X POST http://localhost:8090/v1/chat/completions \
  -H "Authorization: Bearer sk-<la-key-de-la-persona>" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5:3b-instruct","messages":[{"role":"user","content":"Hola"}]}'
```
