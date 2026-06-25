# Documentación

Documentación del proyecto en un solo lugar.

| Documento | De qué trata |
|---|---|
| [DESIGN.md](DESIGN.md) | Diseño del pipeline anti prompt-injection por etapas (harness / Loop Engineering, política, etapas). |
| [CONCURRENCY.md](CONCURRENCY.md) | Concurrencia, colas y `429`; por qué un LLM no escala como una API normal; prueba de estrés y cómo leerla. |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Guía de despliegue en producción (checklist, comandos, secretos, TLS, backups). |

Ver también:
- [README del proyecto](../README.md) — visión general del stack.
- [gateway-api/README.md](../gateway-api/README.md) — la API FastAPI (endpoints, política, build, tests).
- [CLAUDE.md](../CLAUDE.md) — guía de arquitectura para trabajar en el repo.

Pruebas: `gateway-api/tests/` (pytest, lógica del guard) y `scripts/verify-prod.sh` /
`scripts/loadtest.py` (E2E y carga contra el stack levantado).
