# Concurrencia, colas y rendimiento

Documenta **cómo se comporta el gateway bajo carga** y **por qué** un LLM local no
escala como una API normal. Operativo y de diseño.

## Por qué un LLM hace "esperar" y una API normal no

| | API "normal" (CRUD/web) | Inferencia LLM |
|---|---|---|
| Naturaleza del trabajo | **I/O-bound**: la mayor parte del tiempo espera a BD/disco/red | **compute-bound**: miles de millones de operaciones por token |
| Uso de CPU por petición | milisegundos; la CPU queda libre enseguida | **CPU al 100% durante segundos** (sin GPU, peor) |
| Concurrencia que aguanta 1 nodo | miles (no se estorban) | **unas pocas** (pelean por la misma CPU) |
| Qué pasa al saturar | también devuelve 429/503 | igual, pero llega al límite con **muy pocas** peticiones |

Una API normal atiende miles a la vez porque cada petición ocupa la CPU un instante
y la suelta (mientras espera I/O, atiende a otros). Una petición a un LLM **satura
todos los núcleos durante segundos**: si entran muchas a la vez, todas van lentísimas
o se agota la memoria. Por eso hay que **limitar y encolar**.

> Esto **no es un defecto del gateway**: es la naturaleza del cómputo de LLM en
> hardware limitado. Incluso OpenAI/Anthropic encolan y limitan — solo que tienen
> flotas enormes de GPU y la fila es invisible en uso normal. Aquí hay **una máquina
> con CPU**. El gateway no crea la espera: la **administra** ordenadamente.

## El modelo de slots + cola

Dos parámetros, que conviene mantener **alineados**:

- **`OLLAMA_NUM_PARALLEL`** (servicio `ollama`): nº de inferencias concurrentes que
  Ollama atiende = "slots". En CPU los slots comparten núcleos → más slots dan mejor
  **reparto/latencia**, NO más throughput, y gastan más RAM (cada slot tiene su KV-cache).
- **`MAX_CONCURRENCY`** (gateway-api): cuántas peticiones deja pasar el gateway a la vez.
  Si supera a `NUM_PARALLEL`, el exceso solo esperaría en Ollama; por eso se igualan.
- **`QUEUE_TIMEOUT`** (gateway-api): segundos que una petición espera un slot libre
  antes de recibir **`429`** (con `Retry-After`).

```
peticiones que corren YA   = NUM_PARALLEL (≈ MAX_CONCURRENCY)
peticiones que esperan     = el resto, hasta QUEUE_TIMEOUT segundos
las que superan ese tiempo = 429 (reintentar luego)
```

## Ejemplo: 5 usuarios llaman a la vez

Con `NUM_PARALLEL=2`, `MAX_CONCURRENCY=2`, `QUEUE_TIMEOUT=30s` y ~10s por respuesta:

| Momento | Slot 1 | Slot 2 | En cola |
|---|---|---|---|
| t=0s | 👤1 | 👤2 | 3, 4, 5 esperan |
| t≈10s | 👤3 | 👤4 | 5 espera (~10s) |
| t≈20s | 👤5 | — | — |

El usuario 5 espera ~20s antes de empezar; como es **< 30s**, se atiende. Con más
usuarios o un modelo más lento, los del final recibirían **`429`** (no se cuelgan:
reintentan). No es "uno por uno" ni "todos a la vez", sino **"N a la vez (N=slots) y
el resto en cola con un límite de paciencia"**.

## Cómo ajustarlo

- Subir **`QUEUE_TIMEOUT`** (60-120s): menos 429, pero esperas más largas.
- Subir **`MAX_CONCURRENCY`/`NUM_PARALLEL`**: más en paralelo, pero en CPU cada uno
  va más lento y gasta más RAM. No es gratis.
- **Throughput real** para muchos usuarios → no se logra con config:
  - **GPU** (acelera cada inferencia), o
  - **réplicas de Ollama** detrás de un balanceador (escalado horizontal).

| Entorno | Sugerencia |
|---|---|
| Dev (3b, 16 GB) | `OLLAMA_NUM_PARALLEL=2`, `MAX_CONCURRENCY=2` |
| Prod (14b, 90 GB) | `OLLAMA_NUM_PARALLEL=4`, `MAX_CONCURRENCY=4` (la RAM lo permite) |

## Medición

El cuello de botella es el CPU: medido en este stack, el throughput se mantiene plano
(~0.11 req/s) al subir la concurrencia, mientras la latencia de los últimos crece de
forma lineal. Conclusión: **abrir más usuarios no aumenta la capacidad**; las palancas
reales son GPU o réplicas. Implementación: semáforo + cola en `app/concurrency.py`.
