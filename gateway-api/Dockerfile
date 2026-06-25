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

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
