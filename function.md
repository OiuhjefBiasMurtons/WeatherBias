# WeatherSniper — Cómo funciona todo

## El concepto en una oración

Polymarket resuelve sus mercados de temperatura usando datos de Weather Underground, que a su vez lee reportes METAR de aeropuertos. Hay un lag de 5-30 minutos entre que aparece el dato real y que el mercado ajusta sus precios. WeatherSniper detecta ese lag y apuesta antes de que el mercado corrija.

---

## Flujo general del sistema

```
Cada 30 minutos:

1. Leer temperatura actual del aeropuerto (METAR)
        ↓
2. Leer precios actuales del mercado (Polymarket)
        ↓
3. Comparar: ¿el mercado refleja la realidad?
        ↓
4. Si no: generar señal → guardar en Supabase → alertar por Telegram
        ↓
5. Operador aprueba (botón en Telegram o endpoint REST)
        ↓
6. Ejecutar orden en Polymarket
```

---

## Mapa de archivos

```
weathersniper/
├── main.py                    ← Punto de entrada. Arranca todo.
├── config.py                  ← Configuración centralizada. Ciudades, thresholds.
│
├── data/                      ← Capa de datos externos
│   ├── metar.py               ← Lee temperatura del aeropuerto
│   ├── forecast.py            ← Lee forecast del modelo meteorológico GFS
│   └── polymarket.py          ← Lee mercados y precios de Polymarket
│
├── signals/                   ← Lógica de detección de oportunidades
│   ├── models.py              ← Definición de tipos de datos
│   ├── engine.py              ← Orquestador: une todo y corre el ciclo
│   ├── end_of_day.py          ← Estrategia 1: temperatura ya no puede subir más
│   └── forecast_divergence.py ← Estrategia 2: forecast dice algo diferente al mercado
│
├── execution/                 ← Ejecución de órdenes
│   ├── position_sizer.py      ← Calcula cuánto dinero arriesgar
│   └── polymarket_exec.py     ← Coloca la orden en Polymarket
│
├── alerts/
│   └── telegram.py            ← Bot de Telegram: envía alertas, recibe comandos
│
├── db/
│   ├── schema.sql             ← Definición de las tablas en Supabase
│   └── client.py              ← Conexión a Supabase (singleton)
│
└── scheduler/
    └── jobs.py                ← Define cuándo se ejecuta cada tarea
```

---

## Archivo por archivo

---

### `config.py` — La fuente de verdad de la configuración

**¿Qué hace?**
Define todo lo que el sistema necesita para funcionar: credenciales, ciudades monitoreadas, y parámetros de trading.

**Partes:**

`Settings` (dataclass frozen): Lee variables de entorno del `.env`. Son las credenciales:
- Supabase URL y service key
- Polymarket private key, API key, proxy address
- Telegram bot token y chat ID
- `DRY_RUN`: si es `true`, el sistema simula todo sin ejecutar trades reales

`CITIES`: Lista de 6 ciudades. Cada ciudad tiene:
- `id`: Identificador interno (ej: `"nyc"`)
- `icao`: Código del aeropuerto que Polymarket usa para resolver (ej: `"KLGA"` = LaGuardia)
- `timezone`: Para calcular si ya pasó la hora pico local
- `unit`: `"F"` o `"C"` — el mercado de NYC está en Fahrenheit, el de Madrid en Celsius
- `peak_hour_local`: La hora local después de la cual la temperatura ya no sube más ese día (típicamente las 15-16h)

Thresholds de señal:
- `SIGNAL_MIN_CONFIDENCE = 0.70` → Sólo enviar señales con ≥70% de confianza
- `EOD_MIN_HOURS_PAST_PEAK = 1.0` → Esperar al menos 1h después del peak para señal EOD
- `EOD_MAX_TEMP_DELTA_LAST_2H = 0.5` → Si la temperatura varió más de 0.5°C en 2h, no está estabilizada
- `FORECAST_MIN_DIVERGENCE_PCT = 0.15` → Divergencia mínima del 15% para señal de forecast
- `MAX_POSITION_USDC = 100.0` → Máximo $100 por trade en fase 1
- `MAX_POSITION_PCT_CAPITAL = 0.05` → Máximo 5% del capital por trade

---

### `db/schema.sql` — Las tablas en Supabase

**6 tablas:**

| Tabla | Para qué sirve |
|-------|----------------|
| `cities` | Ciudades monitoreadas con su configuración |
| `markets` | Cada mercado activo de Polymarket, mapeado a una ciudad |
| `outcomes` | Los brackets (rangos de temperatura) de cada mercado con su precio |
| `signals` | Señales generadas por el sistema (pending → approved → executed) |
| `trades` | Órdenes ejecutadas con resultado (open → won/lost) |
| `metar_snapshots` | Historial de lecturas METAR (temperatura por aeropuerto) |

**Flujo de estados:**
```
signals: pending → approved → executed
                 → rejected
                 → expired (si el precio se movió antes de ejecutar)

trades: open → won
             → lost
             → cancelled
```

---

### `db/client.py` — Conexión a Supabase

Un singleton usando `@lru_cache`. Llamas `get_supabase()` desde cualquier módulo y siempre obtienes la misma instancia. Sin esto, cada request crearía una nueva conexión.

---

### `data/metar.py` — El lector de temperatura real

**¿De dónde lee?**
`https://aviationweather.gov/api/data/metar?ids=KLGA&format=json`

**¿Qué retorna?**
```python
METARReading(
    icao="KLGA",
    temp_c=22.0,
    temp_f=71.6,
    observed_at=datetime(2026, 4, 10, 20, 30, tzinfo=UTC),
    raw={...}  # respuesta completa de la API
)
```

**Protecciones:**
- **Timeout de 5 segundos**: Si la API no responde rápido, cortamos
- **3 reintentos con backoff exponencial**: 1s → 2s → 4s de espera entre intentos
- **Validación de edad**: Si el dato tiene más de 90 minutos, retorna `None` (dato no confiable)
- **Guarda en Supabase**: Cada lectura se persiste en `metar_snapshots` para construir historial del día

**¿Por qué importa el historial?**
La estrategia End-of-Day necesita saber cuál fue la temperatura máxima del día. Ese máximo se calcula consultando todos los snapshots desde las 00:00 UTC del día actual.

---

### `data/forecast.py` — El lector de pronóstico meteorológico

**¿De dónde lee?**
`https://api.open-meteo.com/v1/forecast` — Modelo GFS (Global Forecast System, NOAA)

**¿Qué retorna?**
```python
{
    date(2026, 4, 11): 24.5,  # mañana: máx 24.5°C
    date(2026, 4, 12): 22.0,  # pasado: máx 22.0°C
    ...
}
```

**Cache de 6 horas**: Los modelos GFS se actualizan cada 6 horas. No tiene sentido pedir datos más seguido. El cache está en memoria (`_forecast_cache` dict global).

**Importante**: Siempre en Celsius internamente. La conversión a Fahrenheit ocurre solo al comparar con brackets del mercado o al formatear mensajes de Telegram.

---

### `data/polymarket.py` — El lector del mercado

**¿De dónde lee?**
- Mercados: `https://gamma-api.polymarket.com/markets?tag=temperature&closed=false`
- Precios: `https://clob.polymarket.com/prices-history`

**El problema del parsing:**
Los mercados de Polymarket tienen preguntas como:
> "Highest temperature in New York City on April 11?"

Hay que parsear esa pregunta para extraer:
1. La ciudad → mapearla al `city_id` interno (`"nyc"`)
2. La fecha → convertirla a `date(2026, 4, 11)`

Luego hay que parsear los outcomes (brackets):
> "58 - 59.99°F" → `bracket_low=58, bracket_high=59.99`

**Riesgo #1 del sistema — ICAO de resolución:**
El campo `description` o `rules` del mercado dice qué URL de Weather Underground usa para resolver. Esa URL contiene el ICAO real. Si el ICAO del mercado difiere del ICAO en nuestra config, hay que usar el del mercado (no el nuestro). El sistema loggea un WARNING si detecta discrepancia.

**Persistencia:**
Cada mercado y outcome se hace upsert en Supabase para tener registro histórico.

---

### `signals/models.py` — Los tipos de datos

Define los dataclasses que circulan por todo el sistema:

| Clase | Qué representa |
|-------|----------------|
| `CityConfig` | Una ciudad: ICAO, timezone, unit, peak_hour |
| `METARReading` | Una lectura de temperatura de un aeropuerto |
| `OutcomePrice` | Un bracket de precio en Polymarket |
| `PolyMarket` | Un mercado completo con todos sus brackets |
| `MarketState` | El estado actual: mercado + METAR + forecast |
| `Signal` | Una oportunidad detectada, lista para aprobar |
| `TradeResult` | Resultado de ejecutar una orden |
| `SignalStrategy` | Enum: `END_OF_DAY` o `FORECAST_DIVERGENCE` |
| `SignalSide` | Enum: `YES` o `NO` |

---

### `signals/end_of_day.py` — Estrategia 1: El máximo ya se alcanzó

**La idea central:**
Si son las 5 PM, llevamos 2 horas sin que la temperatura suba, y el mercado aún cotiza brackets más altos como si pudieran ocurrir... eso es dinero gratis.

**Las 4 condiciones que deben cumplirse:**
1. `hora_local >= peak_hour + 1h` → Ya pasó suficiente tiempo desde que se alcanza el máximo típico
2. Tenemos METAR reciente (menos de 90 min)
3. La temperatura es **estable**: la diferencia entre la máxima y mínima de las últimas 2 horas es < 0.5°C
4. Hay algún outcome con precio > 0.10 (para que valga la pena)

**Qué señales genera:**
- **YES** en el bracket que contiene la temperatura actual (si el precio < 0.85) → el mercado subestima que este bracket es el correcto
- **NO** en cualquier bracket por encima de la temperatura actual (si precio > 0.10) → esos brackets son inalcanzables

**Cálculo de confianza:**
- Base: 70%
- +10% si han pasado más de 2h desde el peak (más certeza de que no sube más)
- +10% si las últimas 3 lecturas METAR son muy estables (delta < 0.3°C)
- -10% si hay tormenta en el METAR (código `TS`, `CB`, `TCU`) → menos certeza

**Temperatura de referencia:**
Se obtiene el máximo de todos los `metar_snapshots` del día desde Supabase. No solo el METAR actual, sino el pico real del día.

---

### `signals/forecast_divergence.py` — Estrategia 2: El mercado no leyó el pronóstico

**La idea central:**
El modelo GFS dice que mañana máximo 24°C, pero el mercado está apostando más dinero al bracket de 19-21°C. Hay una divergencia del 15%. El mercado está equivocado (o simplemente no actualizó).

**Solo para D+1 y D+2:**
- D+0 (hoy): el METAR ya da información más confiable que cualquier forecast
- D+3, D+4: demasiada incertidumbre, el forecast puede fallar mucho

**Cálculo del precio implícito del mercado:**
El bracket con mayor precio es el "consenso". El punto medio de ese bracket es la temperatura que el mercado cree más probable.

```
Ejemplo:
- Bracket 58-60°F cotiza a 0.45 (el más alto)
- Precio implícito = (58 + 60) / 2 = 59°F
```

**Divergencia:**
```
divergencia = |forecast - precio_implícito| / |precio_implícito|
```
Si supera el 15%, hay señal.

**Dirección de la señal:**
- Forecast > precio implícito → mercado está bajo → comprar YES en el bracket correcto según forecast
- Forecast < precio implícito → mercado está alto → comprar NO en el bracket de consenso actual

---

### `signals/engine.py` — El orquestador

**¿Qué hace?**
Une todas las piezas y ejecuta el ciclo de detección cada 30 minutos.

**Pasos del ciclo `run_signal_cycle()`:**
1. Construir `CityConfig` desde `CITIES` de config
2. Obtener todos los mercados activos de Polymarket (llama a `polymarket.py`)
3. Para cada ciudad activa:
   - Obtener METAR (llama a `metar.py`)
   - Obtener forecast (llama a `forecast.py`)
   - Para cada mercado de esa ciudad:
     - Construir `MarketState` con toda la info
     - Evaluar `end_of_day` (solo si el mercado es de hoy)
     - Evaluar `forecast_divergence` (solo si es D+1 o D+2)
4. Filtrar señales con confidence ≥ 70%
5. Guardar cada señal calificada en Supabase con status `'pending'`

**Manejo de errores:**
Si una ciudad falla (API caída, timeout), el ciclo continúa con las demás. Nunca un fallo individual detiene el ciclo completo.

---

### `execution/position_sizer.py` — ¿Cuánto arriesgar?

**Fórmula: Half-Kelly Criterion**

Kelly completo es agresivo y puede arruinarte con una racha mala. Half-Kelly es más conservador:

```python
win_prob = confidence          # ej: 0.82
win_payout = (1 - price) / price  # si precio=0.38: payout = 1.63x

kelly = (win_prob * win_payout - lose_prob) / win_payout
size = capital * (kelly / 2)   # half-Kelly
```

**Tres caps (siempre se aplica el mínimo):**
1. `MAX_POSITION_USDC = $100` → nunca más de $100 por trade
2. `MAX_POSITION_PCT_CAPITAL = 5%` → nunca más del 5% del capital disponible
3. Mínimo $5 → si sale menos, retorna 0 (no vale la comisión)

**Ejemplo:**
```
confidence=0.82, price=0.38, capital=$2000

win_payout = (1-0.38)/0.38 = 1.63
kelly = (0.82 × 1.63 - 0.18) / 1.63 = 0.709
half_kelly = 0.354

size_calculado = $2000 × 0.354 = $708
caps: min($708, $100, $2000×0.05=$100) = $100
```

---

### `execution/polymarket_exec.py` — El ejecutor de órdenes

**`execute_approved_signal(signal_id)`** — El flujo completo de ejecución:

1. Obtener la señal de Supabase
2. Obtener el token_id del outcome correspondiente
3. **Validar precio:** Si el precio actual difiere más de 3¢ del precio cuando se generó la señal → expirar señal, alertar por Telegram, no ejecutar
4. Llamar a `place_order()`

**`place_order()`:**

En `DRY_RUN=true`:
- Genera un `order_id = "DRY_RUN_1234567890"`
- Loggea como si fuera real
- Guarda en Supabase
- Envía confirmación por Telegram

En `DRY_RUN=false`:
- Revisa el orderbook para verificar liquidez
- Si la orden supera el 20% de la liquidez disponible → reduce el tamaño automáticamente
- Coloca un **limit order** a `precio + 0.01¢` (taker premium) para asegurar mayor fill rate
- Guarda la orden real en Supabase

---

### `alerts/telegram.py` — El bot de comunicación

**Responsabilidades:**
1. Enviar alertas cuando se detecta una señal
2. Recibir aprobaciones/rechazos del operador
3. Responder a comandos de consulta

**Formato del mensaje de señal:**
```
🌡 SEÑAL WEATHERSNIPER

📍 New York City (KLGA)
📊 Estrategia: End of Day Lock

🎯 Posición: NO en bracket 62-63°F
💰 Precio actual: $0.38
🌡 METAR actual: 59°F

📈 Confianza: 82%
💵 Tamaño sugerido: $50 USDC

💭 Razonamiento: Max temp del día: 59°F...

ID señal: #247
```

**Botones inline:**
- ✅ Aprobar y ejecutar → llama a `execute_approved_signal(signal_id)`
- ❌ Rechazar → marca señal como `rejected`
- ℹ️ Ver detalles → muestra datos completos de la señal

**Comandos:**

| Comando | Qué hace |
|---------|----------|
| `/status` | PnL total, señales pendientes, modo DRY_RUN |
| `/pending` | Lista de las últimas 10 señales pendientes |
| `/history` | Últimos 10 trades con resultado |
| `/capital [amount]` | Consultar/notar el capital disponible |

---

### `scheduler/jobs.py` — El reloj del sistema

**5 jobs programados:**

| Job | Cuándo | Qué hace |
|-----|--------|----------|
| `update_markets` | Cada 5 minutos | Refresca mercados y precios de Polymarket |
| `signal_cycle` | :02 y :32 de cada hora | Ciclo principal de detección de señales |
| `forecast_update` | 00:30, 06:30, 12:30, 18:30 UTC | Actualiza pronósticos GFS para todas las ciudades |
| `cleanup_markets` | 02:00 UTC diario | Marca mercados viejos como `resolved`, expira señales pendientes |
| `daily_report` | 09:00 UTC diario | Envía resumen por Telegram: PnL, win rate, trades |

**¿Por qué :02 y :32 para el signal_cycle?**
Los METARs se emiten nominalmente a :00 y :30, pero pueden tardar 2-8 minutos en aparecer en la API. Al correr a :02 y :32 damos tiempo a que el METAR nuevo ya esté disponible.

---

### `main.py` — El punto de entrada

**Arranque (`lifespan`):**
1. Verificar conexión a Supabase
2. `init_cities()` → insertar/actualizar las 6 ciudades en la tabla `cities`
3. Iniciar el bot de Telegram (si hay token configurado)
4. Iniciar `AsyncIOScheduler` con los 5 jobs
5. Levantar la API FastAPI

**Endpoints REST:**

| Endpoint | Método | Para qué |
|----------|--------|----------|
| `/health` | GET | Saber si el sistema está funcionando y conectado |
| `/signals/pending` | GET | Ver señales pendientes de aprobar |
| `/stats` | GET | PnL, win rate, totales |
| `/signals/{id}/approve` | POST | Aprobar señal (alternativa al botón de Telegram) |
| `/signals/{id}/reject` | POST | Rechazar señal |

---

## Flujo completo con ejemplo real

**Escenario:** Son las 5 PM en Nueva York, la temperatura fue 59°F y no ha subido más. El mercado de Polymarket aún cotiza el bracket 62-63°F a $0.38.

```
[17:02 UTC-4]
  scheduler → signal_cycle()

  data/metar.py:
    GET aviationweather.gov/metar?ids=KLGA
    → temp_c=15.0, temp_f=59.0, observed_at=17:00
    → Guarda en metar_snapshots

  data/polymarket.py:
    GET gamma-api.polymarket.com/markets?tag=temperature
    → Encuentra "Highest temperature in New York City on April 10?"
    → outcome: "62-63°F" con price=0.38, token_id="abc123"
    → Guarda/actualiza en markets y outcomes

  signals/engine.py:
    MarketState(
      market_id="xyz", city_id="nyc", target_date=today,
      last_metar=METARReading(59°F),
      outcomes=[..., OutcomePrice(62,63, price=0.38), ...]
    )

  signals/end_of_day.py:
    ✓ Son las 17:00, peak_hour=15 → 2h pasadas (≥ 1h mínimo)
    ✓ METAR tiene 2 minutos (< 90 min)
    ✓ Últimas 2h: [59.2, 59.0, 59.1, 58.9] → delta=0.3°C (< 0.5)
    ✓ max_temp_dia = 59°F (del historial de snapshots)

    Para bracket 62-63°F (precio=0.38):
      62 > 59 → inalcanzable → señal NO
      confidence = 0.70 + 0.10 (>2h) + 0.10 (estable) = 0.90

    position_sizer.py:
      kelly → size = $100 (capped)

    → Signal(side=NO, bracket=62-63, price=0.38, confidence=0.90, size=$100)

  engine.py:
    confidence 0.90 ≥ 0.70 → calificada
    → INSERT signals (status='pending', id=247)

  alerts/telegram.py:
    → Envía mensaje con botones a tu chat de Telegram

[Tú ves el mensaje y presionas ✅]

  alerts/telegram.py → _approve_signal(247)
    → UPDATE signals SET status='approved' WHERE id=247

  execution/polymarket_exec.py → execute_approved_signal(247)
    → Lee señal de Supabase
    → Lee token_id="abc123" de outcomes
    → current_price = 0.38 (sin cambio, ok)
    → place_order(side="NO", price=0.38, size=100)

    [DRY_RUN=true]
    → order_id = "DRY_RUN_1712775720"
    → INSERT trades (order_id, shares=263, status='open')
    → UPDATE signals SET status='executed'
    → Telegram: "✅ Trade ejecutado [DRY RUN] | NO | $100 @ 0.38"
```

---

## Variables de entorno

El archivo `.env` (nunca en git) controla todo el comportamiento:

```env
# Base de datos
SUPABASE_URL=https://xxxxxxxx.supabase.co
SUPABASE_SERVICE_KEY=eyJ...

# Wallet Polygon para Polymarket
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_API_KEY=...           # opcional en fase 1
POLYMARKET_PROXY_ADDRESS=0x...

# Bot de Telegram
TELEGRAM_BOT_TOKEN=123456789:AAF...
TELEGRAM_CHAT_ID=987654321       # tu chat ID personal

# Comportamiento
DRY_RUN=true                     # SIEMPRE true hasta validar con datos reales
LOG_LEVEL=INFO                   # DEBUG para ver cada request de API
```

---

## Cómo arrancar

```bash
# 1. Instalar dependencias
pip install -r requirements.txt

# 2. Copiar y rellenar credenciales
cp .env.example .env
# editar .env con tus datos

# 3. Ejecutar schema.sql en Supabase
# (desde el SQL Editor de Supabase, pegar y ejecutar weathersniper/db/schema.sql)

# 4. Arrancar
uvicorn weathersniper.main:app --host 0.0.0.0 --port 8000

# 5. Verificar que todo está OK
curl http://localhost:8000/health
```

---

## Cómo correr los tests

```bash
pip install pytest pytest-asyncio pytest-httpx
pytest tests/ -v
```

Los tests mockean todas las APIs externas y Supabase, así que corren sin necesitar credenciales reales.

---

## Progresión de fases

**Fase 1 (actual) — DRY_RUN=true:**
- El sistema detecta señales y alerta
- Tú apruebas/rechazas en Telegram
- Las "órdenes" se guardan con `order_id=DRY_RUN_...` pero no se ejecutan
- Objetivo: validar que las señales son correctas durante 5-7 días

**Fase 2 — DRY_RUN=false:**
- Activar con capital mínimo ($200 USDC)
- Misma lógica, pero las aprobaciones resultan en órdenes reales
- Monitorear con `/stats` y Supabase

---

## Puntos críticos a vigilar

1. **ICAO de resolución**: Si Polymarket cambia el aeropuerto fuente de un mercado, el sistema detecta el nuevo ICAO en las `rules` del mercado y usa ese. Ver logs con nivel WARNING.

2. **Delay del METAR**: Los METARs se emiten a :00 y :30 pero llegan con 2-8 min de delay. El scheduler está configurado para correr a :02 y :32.

3. **Temperatura en Fahrenheit vs Celsius**: Open-Meteo siempre devuelve Celsius. Polymarket usa F para NYC y LA, C para el resto. La conversión ocurre solo al comparar con brackets del mercado, no antes.

4. **Liquidez**: Si el tamaño de la orden supera el 20% de la liquidez disponible en el orderbook, el sistema reduce el tamaño automáticamente en `polymarket_exec.py`.

5. **DRY_RUN**: Con `DRY_RUN=true` todo funciona igual excepto la llamada final al CLOB. Todos los logs, Telegram y registros en Supabase ocurren igual.
