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

## Medición (prueba de estrés)

Una "prueba de estrés" manda muchas peticiones a la vez para ver **hasta dónde aguanta**
el sistema. Usamos **`scripts/loadtest.py`** (no necesita instalar nada extra; corre
dentro del contenedor del gateway):

```bash
docker exec local-ai-gateway-gateway-api-1 python /tmp/loadtest.py \
  --admin-key "$ADMIN_BOOTSTRAP_KEY" --model qwen2.5:14b-instruct \
  --levels 1,2,4,8 --total 16 --max-tokens 64
```

`--levels 1,2,4,8` = prueba con 1 petición a la vez, luego 2, luego 4, luego 8
(esto es el "barrido de concurrencia": **concurrencia** = peticiones simultáneas).

### Glosario rápido de la tabla
- **conc** (concurrencia): cuántas peticiones llegan al mismo tiempo.
- **req/s** (throughput): cuántas peticiones **completa por segundo**. Es la capacidad. Más = mejor.
- **p50 / p95 / p99** (latencia, en segundos): cuánto tarda **una** petición.
  - p50 = la mitad tardó menos que eso (la típica). p95 = solo 1 de cada 20 fue más lenta.
  - p99 = solo 1 de cada 100 fue más lenta (el peor caso habitual).
- **200 / 429 / err**: respuestas. 200 = OK. 429 = "saturado, reintenta". err = falló.
- **slots**: cuántas peticiones procesa Ollama a la vez (`OLLAMA_NUM_PARALLEL`).

### Resultado en dev (16 GB, qwen2.5:3b-instruct, respuestas cortas, 2 slots)

| peticiones a la vez | req/s (capacidad) | p50 (lo que tarda la típica) |
|---|---|---|
| 1 | 0.11 | 8.9s |
| 2 | 0.17 | 12.9s |
| 4 | 0.19 | 20.6s |
| 8 | 0.21 | 24.0s |

**Cómo leerlo:** la capacidad (req/s) es **muy baja y apenas sube** al meter más peticiones
a la vez — está limitada por el CPU. Lo que sí crece, y mucho, es la **latencia**: la típica
pasa de ~9s a ~24s. Es decir, pasado el primer par de slots, más concurrencia **no da más
capacidad, solo más espera** (cada quien hace cola por el CPU).

> ⚠️ Estas cifras de dev son **muy ruidosas**: el portátil (16 GB, CPU) estaba en uso, así
> que los tiempos absolutos varían bastante entre corridas. **No las tomes como referencia**;
> sirven solo para ver la *forma* (capacidad plana, latencia creciente). Los números reales
> se miden en el servidor con `scripts/loadtest.py`.

**Conclusión para el negocio:** la capacidad máxima la pone el CPU, **sin importar cuántas
personas usen el sistema**. **Abrir más usuarios NO da más capacidad** — solo hace que todos
esperen más. Para subir el tope de verdad: usar **GPU** (acelera la IA) o **réplicas** (varias
copias de Ollama repartiendo la carga).

> En producción (servidor con RAM holgada, modelo 14B) conviene correr `loadtest.py` para
> obtener la curva real con tu hardware y dimensionar la concurrencia.

El mecanismo que reparte el acceso (deja pasar N a la vez y encola al resto) está en
`gateway-api/app/concurrency.py`.
