# syntax=docker/dockerfile:1
FROM python:3.11-slim

# INSTALL_GUARD=true  -> instala llm-guard (torch/transformers), imagen grande (prod)
# INSTALL_GUARD=false -> imagen ligera sin guard (dev). Usa GATEWAY_GUARD_ENABLED=false.
ARG INSTALL_GUARD=true

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencias del sistema mínimas (algunos wheels de llm-guard las requieren).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-guard.txt ./
RUN pip install -r requirements.txt \
 && if [ "$INSTALL_GUARD" = "true" ]; then pip install -r requirements-guard.txt; fi

# Assets de Swagger UI y ReDoc auto-hospedados (para que /docs y /redoc funcionen
# sin salida a internet / CDN). Se descargan en build y quedan dentro de la imagen.
RUN mkdir -p static && python - <<'PY'
import urllib.request as u
assets = {
    "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui-bundle.js": "static/swagger-ui-bundle.js",
    "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui.css": "static/swagger-ui.css",
    "https://cdn.jsdelivr.net/npm/redoc@2.1.5/bundles/redoc.standalone.js": "static/redoc.standalone.js",
}
for src, dst in assets.items():
    u.urlretrieve(src, dst)
    print("descargado", dst)
PY

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
