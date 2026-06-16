#!/bin/sh
# Inyecta la API key en la config de nginx y arranca el servidor.
# Se usa sed (no envsubst) para no tocar las variables propias de nginx ($host, etc.).
set -e

if [ -z "$LLM_API_KEY" ]; then
    echo "ERROR: LLM_API_KEY no está definida. El gateway no arrancará sin una API key." >&2
    exit 1
fi

sed "s|__LLM_API_KEY__|${LLM_API_KEY}|g" \
    /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf

echo "Gateway listo — API protegida por Authorization: Bearer <LLM_API_KEY>"
exec nginx -g 'daemon off;'
