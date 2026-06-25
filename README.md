# Local AI Gateway

Servicio de **IA local autohospedada**, genérico y reutilizable. Levanta un
modelo open-source con [Ollama](https://ollama.com) y expone una **API compatible
con OpenAI** detrás de un gateway protegido por API key, para que **cualquier
proyecto** pueda consumirlo desde su propio dominio. Incluye además
[Open WebUI](https://github.com/open-webui/open-webui) como interfaz de chat.

```
                         ┌─────────────────────────────────────────┐
   otros proyectos  ───► │ gateway (nginx)  :GATEWAY_PORT           │
   (Authorization:       │   valida Bearer <LLM_API_KEY>           │
    Bearer <key>)        │   proxy ─────────────►  ollama :11434   │
                         │                          (modelos)      │
   humanos  ──────────►  │ open-webui  :WEBUI_PORT ─►  ollama       │
                         └─────────────────────────────────────────┘
```

- **ollama**: motor de inferencia (no expuesto al host; solo red interna).
- **ollama-init**: descarga el modelo (`OLLAMA_MODEL`) al levantar.
- **open-webui**: chat web para personas.
- **gateway** (nginx, legacy): entrada simple a la API con una API key única; proxy directo a Ollama.
- **gateway-api** (FastAPI): capa de seguridad sobre Open WebUI — multiusuario, anti
  prompt-injection (pipeline por etapas), control de concurrencia y observabilidad.
  Es la entrada recomendada. Ver [gateway-api/README.md](gateway-api/README.md).

## Documentación

Toda la documentación está en **[docs/](docs/)**:
- [docs/DESIGN.md](docs/DESIGN.md) — diseño del pipeline anti prompt-injection.
- [docs/CONCURRENCY.md](docs/CONCURRENCY.md) — concurrencia, colas y rendimiento.
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — despliegue en producción.

## Requisitos

- Docker + Docker Compose v2.
- ~8 GB de RAM libres para un modelo 7B en CPU (más rápido con GPU NVIDIA).

## Puesta en marcha

```bash
cp .env.example .env
# Edita .env: genera una LLM_API_KEY fuerte
#   python -c "import secrets; print(secrets.token_hex(24))"

docker compose up -d
```

La primera vez `ollama-init` descarga el modelo (varios GB) — puede tardar.
Sigue el progreso con `docker compose logs -f ollama-init`.

- **Chat (humanos)**: http://localhost:3000  (Open WebUI)
- **API (proyectos)**: http://localhost:8080/v1  (requiere API key)

### GPU (opcional)

Si el host tiene GPU NVIDIA + `nvidia-container-toolkit`, descomenta el bloque
`deploy.resources` del servicio `ollama` en `docker-compose.yml` y recrea.

## Consumir la API desde otro proyecto

Es **compatible con OpenAI**, así que sirve cualquier cliente OpenAI apuntando
el `base_url` al gateway y usando la `LLM_API_KEY` como api key.

```bash
curl http://<dominio>:8080/v1/chat/completions \
  -H "Authorization: Bearer <LLM_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5:3b-instruct",
    "messages": [{"role": "user", "content": "Hola"}]
  }'
```

Python (cliente OpenAI):

```python
from openai import OpenAI
client = OpenAI(base_url="http://<dominio>:8080/v1", api_key="<LLM_API_KEY>")
resp = client.chat.completions.create(
    model="qwen2.5:3b-instruct",
    messages=[{"role": "user", "content": "Hola"}],
)
```

### Ejemplo: usarlo como proveedor/fallback en un proyecto

Cualquier proyecto que ya hable con la API de OpenAI puede apuntar al gateway
cambiando el `base_url` y la api key. Por ejemplo, en un backend con fallback LLM:

```
FALLBACK_LLM_PROVIDER=openai
FALLBACK_LLM_MODEL=qwen2.5:3b-instruct
FALLBACK_LLM_API_KEY=<LLM_API_KEY>
FALLBACK_LLM_BASE_URL=http://<dominio>:8080/v1
```

> El proyecto consumidor solo necesita que su cliente OpenAI acepte un `base_url`
> personalizado apuntando a este gateway.

## Seguridad

- La API solo responde con `Authorization: Bearer <LLM_API_KEY>` válido; cualquier
  otra cosa recibe `401`. `/healthz` queda abierto para health checks.
- `LLM_API_KEY` vive en `.env` (gitignored). Rota la key si se filtra.
- Ollama no se expone al host; el único acceso externo es vía el gateway.
- Para producción, pon TLS delante del gateway (nginx/traefik/Caddy con tu dominio).

## Modelos

Cambia `OLLAMA_MODEL` en `.env` y recrea. Recomendado: `qwen2.5:3b-instruct` en
desarrollo (CPU, ágil) y `qwen2.5:14b-instruct` en producción (servidor con RAM
holgada). Para descargar modelos adicionales bajo demanda:
`docker compose exec ollama ollama pull <modelo>`.
