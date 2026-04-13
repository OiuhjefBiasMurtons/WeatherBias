# WeatherSniper — Prompt para Claude Code

## Contexto del proyecto

Construir un sistema automatizado llamado **WeatherSniper** que detecta y explota ineficiencias de precio en mercados de temperatura diaria de Polymarket.

**El edge:** Polymarket resuelve sus mercados de temperatura usando datos de Weather Underground (WU), que a su vez lee reportes METAR de aeropuertos cada 30 minutos. Existe un lag medible (5-30 min) entre cuando aparece el dato real en el METAR y cuando el mercado de Polymarket ajusta sus precios. WeatherSniper vive en ese lag.

**Stack obligatorio:** Python, FastAPI, Supabase, Telegram. Respetar estrictamente este stack — no proponer alternativas.

**Modo de ejecución inicial:** Semi-automático. El bot detecta señales y envía alertas por Telegram. El operador aprueba antes de ejecutar. La ejecución automática se activa en fase 2 (después de validación con datos reales).

---

## Arquitectura del sistema

```
┌─────────────────────────────────────────────────────┐
│                   WEATHERSNIPER                      │
│                                                      │
│  ┌─────────────┐    ┌─────────────┐                 │
│  │  Scheduler  │───▶│  Data Layer │                 │
│  │  (APScheduler│    │  METAR +    │                 │
│  │   / cron)   │    │  Forecast + │                 │
│  └─────────────┘    │  Polymarket │                 │
│                     └──────┬──────┘                 │
│                            │                        │
│                     ┌──────▼──────┐                 │
│                     │   Signal    │                 │
│                     │   Engine    │                 │
│                     └──────┬──────┘                 │
│                            │                        │
│           ┌────────────────┼────────────────┐       │
│           │                │                │       │
│    ┌──────▼─────┐  ┌──────▼─────┐  ┌──────▼─────┐ │
│    │  Telegram  │  │  Supabase  │  │  Executor  │ │
│    │  Alerts    │  │  Logging   │  │  (manual   │ │
│    └────────────┘  └────────────┘  │   confirm) │ │
│                                    └────────────┘ │
└─────────────────────────────────────────────────────┘
```

---

## Estructura de archivos

```
weathersniper/
├── main.py                    # FastAPI app + scheduler startup
├── config.py                  # Todas las variables de config y constantes
├── .env                       # Variables de entorno (nunca en git)
├── requirements.txt
│
├── data/
│   ├── metar.py               # Cliente METAR — aviationweather.gov
│   ├── forecast.py            # Cliente Open-Meteo — GFS forecast
│   └── polymarket.py          # Cliente Polymarket API — mercados y precios
│
├── signals/
│   ├── engine.py              # Orquestador principal de señales
│   ├── end_of_day.py          # Estrategia 1: end-of-day METAR lock
│   ├── forecast_divergence.py # Estrategia 2: GFS vs precio mercado
│   └── models.py              # Dataclasses: Signal, MarketState, CityConfig
│
├── execution/
│   ├── polymarket_exec.py     # py-clob-client wrapper — place/cancel orders
│   └── position_sizer.py     # Kelly criterion simplificado para sizing
│
├── alerts/
│   └── telegram.py            # Bot de Telegram — enviar alertas y recibir comandos
│
├── db/
│   ├── client.py              # Supabase client singleton
│   └── schema.sql             # Schema completo de todas las tablas
│
└── scheduler/
    └── jobs.py                # Definición de todos los cron jobs
```

---

## Variables de entorno (.env)

```env
# Supabase
SUPABASE_URL=
SUPABASE_SERVICE_KEY=

# Polymarket
POLYMARKET_PRIVATE_KEY=        # Private key de la wallet Polygon
POLYMARKET_API_KEY=            # Para CLOB autenticado (opcional en fase 1)
POLYMARKET_PROXY_ADDRESS=      # Proxy wallet address

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=              # Tu chat ID personal

# Modo
DRY_RUN=true                   # true = no ejecutar trades reales
LOG_LEVEL=INFO
```

---

## Schema de Supabase — `db/schema.sql`

```sql
-- Ciudades monitoreadas y su configuración
CREATE TABLE cities (
    id              TEXT PRIMARY KEY,        -- ej: 'nyc', 'madrid', 'london'
    name            TEXT NOT NULL,
    icao            TEXT NOT NULL UNIQUE,   -- ej: 'KLGA', 'LEMD', 'EGLC'
    latitude        FLOAT NOT NULL,
    longitude       FLOAT NOT NULL,
    timezone        TEXT NOT NULL,          -- ej: 'America/New_York'
    unit            TEXT NOT NULL,          -- 'F' o 'C'
    peak_hour_local INT NOT NULL,           -- hora local donde termina el calentamiento
    active          BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Mercados de Polymarket mapeados a ciudades
CREATE TABLE markets (
    id              TEXT PRIMARY KEY,        -- condition_id de Polymarket
    city_id         TEXT REFERENCES cities(id),
    date            DATE NOT NULL,
    question        TEXT NOT NULL,
    resolution_source TEXT,                 -- URL exacta de WU para este mercado
    icao_resolution TEXT,                   -- ICAO confirmado en las rules
    status          TEXT DEFAULT 'active',  -- active | resolved | error
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    resolved_temp   FLOAT,                  -- temperatura real de resolución
    UNIQUE(city_id, date)
);

-- Outcomes (brackets) de cada mercado
CREATE TABLE outcomes (
    id              BIGSERIAL PRIMARY KEY,
    market_id       TEXT REFERENCES markets(id),
    token_id        TEXT NOT NULL,          -- token_id de Polymarket
    bracket_low     FLOAT NOT NULL,         -- ej: 22.0
    bracket_high    FLOAT NOT NULL,         -- ej: 23.99 (siguiente bracket - 0.01)
    side            TEXT NOT NULL,          -- 'YES' o 'NO'
    last_price      FLOAT,                  -- precio más reciente
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(market_id, token_id)
);

-- Señales generadas por el signal engine
CREATE TABLE signals (
    id              BIGSERIAL PRIMARY KEY,
    city_id         TEXT REFERENCES cities(id),
    market_id       TEXT REFERENCES markets(id),
    strategy        TEXT NOT NULL,          -- 'end_of_day' | 'forecast_divergence'
    side            TEXT NOT NULL,          -- 'YES' o 'NO'
    bracket_low     FLOAT NOT NULL,
    bracket_high    FLOAT NOT NULL,
    entry_price     FLOAT NOT NULL,         -- precio en el momento de señal
    metar_temp      FLOAT,                  -- temperatura METAR al momento de señal
    forecast_temp   FLOAT,                  -- temperatura GFS forecast
    confidence      FLOAT NOT NULL,         -- 0.0 a 1.0
    suggested_size  FLOAT NOT NULL,         -- USDC sugerido
    status          TEXT DEFAULT 'pending', -- pending | approved | rejected | executed | expired
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    approved_at     TIMESTAMPTZ,
    notes           TEXT
);

-- Trades ejecutados
CREATE TABLE trades (
    id              BIGSERIAL PRIMARY KEY,
    signal_id       BIGINT REFERENCES signals(id),
    order_id        TEXT,                   -- order_id de Polymarket
    side            TEXT NOT NULL,
    size_usdc       FLOAT NOT NULL,
    entry_price     FLOAT NOT NULL,
    shares          FLOAT NOT NULL,
    status          TEXT DEFAULT 'open',    -- open | won | lost | cancelled
    exit_price      FLOAT,
    pnl_usdc        FLOAT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    closed_at       TIMESTAMPTZ
);

-- Snapshot de METAR por ciudad (histórico)
CREATE TABLE metar_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    city_id         TEXT REFERENCES cities(id),
    icao            TEXT NOT NULL,
    temp_c          FLOAT NOT NULL,
    temp_f          FLOAT NOT NULL,
    observed_at     TIMESTAMPTZ NOT NULL,
    raw             JSONB,                  -- respuesta completa de la API
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Índices útiles
CREATE INDEX idx_markets_city_date ON markets(city_id, date);
CREATE INDEX idx_signals_status ON signals(status);
CREATE INDEX idx_signals_created ON signals(created_at DESC);
CREATE INDEX idx_trades_status ON trades(status);
CREATE INDEX idx_metar_city_observed ON metar_snapshots(city_id, observed_at DESC);
```

---

## Datos de ciudades iniciales — `config.py`

```python
# Insertar en la tabla cities al inicializar
CITIES = [
    {
        "id": "nyc",
        "name": "New York City",
        "icao": "KLGA",          # LaGuardia — fuente de resolución de Polymarket
        "latitude": 40.7769,
        "longitude": -73.8740,
        "timezone": "America/New_York",
        "unit": "F",
        "peak_hour_local": 15,   # Después de las 3 PM el calentamiento cesa
        "active": True
    },
    {
        "id": "london",
        "name": "London",
        "icao": "EGLC",          # London City Airport
        "latitude": 51.5048,
        "longitude": 0.0495,
        "timezone": "Europe/London",
        "unit": "C",
        "peak_hour_local": 15,
        "active": True
    },
    {
        "id": "madrid",
        "name": "Madrid",
        "icao": "LEMD",          # Adolfo Suárez Barajas
        "latitude": 40.4936,
        "longitude": -3.5668,
        "timezone": "Europe/Madrid",
        "unit": "C",
        "peak_hour_local": 16,
        "active": True
    },
    {
        "id": "losangeles",
        "name": "Los Angeles",
        "icao": "KLAX",
        "latitude": 33.9425,
        "longitude": -118.4081,
        "timezone": "America/Los_Angeles",
        "unit": "F",
        "peak_hour_local": 15,
        "active": True
    },
    {
        "id": "hongkong",
        "name": "Hong Kong",
        "icao": "VHHH",
        "latitude": 22.3080,
        "longitude": 113.9185,
        "timezone": "Asia/Hong_Kong",
        "unit": "C",
        "peak_hour_local": 15,
        "active": True
    },
    {
        "id": "shanghai",
        "name": "Shanghai",
        "icao": "ZSPD",
        "latitude": 31.1443,
        "longitude": 121.8083,
        "timezone": "Asia/Shanghai",
        "unit": "C",
        "peak_hour_local": 15,
        "active": True
    },
]

# Thresholds de señal
SIGNAL_MIN_CONFIDENCE = 0.70       # Mínimo para generar señal
EOD_MIN_HOURS_PAST_PEAK = 1.0      # Horas después del peak_hour para señal EOD
EOD_MAX_TEMP_DELTA_LAST_2H = 0.5   # °C máximo de variación en últimas 2h para considerar estabilizado
FORECAST_MIN_DIVERGENCE_PCT = 0.15  # Diferencia mínima entre forecast y precio implícito
MAX_POSITION_USDC = 100.0           # Máximo por trade en fase 1
MAX_POSITION_PCT_CAPITAL = 0.05     # Máximo 5% del capital por trade
```

---

## Especificación por módulo

### `data/metar.py`

**Responsabilidad:** Obtener temperatura actual de cualquier estación ICAO.

**API:** `https://aviationweather.gov/api/data/metar?ids={icao}&format=json`

**Función principal:**
```python
async def get_metar(icao: str) -> METARReading | None:
    """
    Retorna la lectura METAR más reciente para el ICAO dado.
    Guarda snapshot en Supabase (tabla metar_snapshots).
    Retorna None si la API falla o no hay datos recientes (> 90 min).
    """
```

**Dataclass de retorno:**
```python
@dataclass
class METARReading:
    icao: str
    temp_c: float
    temp_f: float
    observed_at: datetime
    raw: dict
```

**Requerimientos:**
- Timeout de 5 segundos
- Retry con backoff exponencial (3 intentos)
- Validar que `observed_at` no tiene más de 90 minutos (dato viejo = no confiable)
- Loggear cada request con latencia

---

### `data/forecast.py`

**Responsabilidad:** Obtener forecast de temperatura máxima para los próximos 4 días.

**API:** `https://api.open-meteo.com/v1/forecast`

**Parámetros:**
```
latitude, longitude, daily=temperature_2m_max, forecast_days=4,
timezone=auto, temperature_unit=celsius
```

**Función principal:**
```python
async def get_forecast(city: CityConfig) -> dict[date, float] | None:
    """
    Retorna dict de {fecha: temp_max_celsius} para los próximos 4 días.
    Las temperaturas siempre en Celsius internamente — convertir al mostrar.
    """
```

**Requerimientos:**
- Cache de 6 horas por ciudad (los modelos GFS actualizan cada 6h)
- Cache en Supabase o en memoria con TTL
- Timeout 10 segundos

---

### `data/polymarket.py`

**Responsabilidad:** Obtener mercados activos de temperatura y sus precios por bracket.

**APIs:**
- Mercados: `https://gamma-api.polymarket.com/markets?tag=temperature&closed=false&limit=200`
- Precios: `https://clob.polymarket.com/prices-history?market={condition_id}&interval=1m&fidelity=1`

**Funciones principales:**
```python
async def fetch_temperature_markets() -> list[PolyMarket]:
    """
    Obtiene todos los mercados activos de temperatura.
    Filtra los que tienen fecha de resolución <= D+4.
    Parsea el question para extraer ciudad y fecha.
    Intenta matchear con cities de la config.
    """

async def get_market_prices(condition_id: str) -> list[OutcomePrice]:
    """
    Obtiene precios actuales de cada outcome (bracket) del mercado.
    """
```

**Parsing del question:** Los mercados tienen formato:
`"Highest temperature in {City} on {Month} {Day}?"`

Construir un parser robusto que maneje variaciones. Mapear el nombre de ciudad al `city_id` de la config. Si no se puede mapear, loggear y saltar.

**Requerimientos:**
- Polling cada 5 minutos (no más frecuente — respetar rate limits)
- Persistir/actualizar mercados y outcomes en Supabase
- Detectar cambios en la fuente de resolución (campo `description` o `rules`) y alertar por Telegram si cambia

---

### `signals/models.py`

```python
from dataclasses import dataclass, field
from datetime import datetime, date
from enum import Enum

class SignalStrategy(str, Enum):
    END_OF_DAY = "end_of_day"
    FORECAST_DIVERGENCE = "forecast_divergence"

class SignalSide(str, Enum):
    YES = "YES"
    NO = "NO"

@dataclass
class Signal:
    city_id: str
    market_id: str
    strategy: SignalStrategy
    side: SignalSide
    bracket_low: float
    bracket_high: float
    entry_price: float          # precio actual en Polymarket (0.0 a 1.0)
    confidence: float           # 0.0 a 1.0
    suggested_size_usdc: float
    metar_temp: float | None = None
    forecast_temp: float | None = None
    reasoning: str = ""         # Explicación en texto para el mensaje de Telegram

@dataclass
class MarketState:
    market_id: str
    city_id: str
    target_date: date
    outcomes: list[OutcomePrice]
    last_metar: METARReading | None
    forecast_temps: dict[date, float]
```

---

### `signals/end_of_day.py`

**Lógica completa:**

```python
async def evaluate_end_of_day(city: CityConfig, market: MarketState) -> list[Signal]:
    """
    Genera señales cuando el máximo del día ya está establecido.

    Condiciones para activar:
    1. Hora local actual >= peak_hour_local + EOD_MIN_HOURS_PAST_PEAK
    2. Ciudad tiene METAR reciente (< 90 min)
    3. Temperatura METAR estable: delta entre METAR actual y el de hace 2h < EOD_MAX_TEMP_DELTA_LAST_2H
    4. Temperatura actual < (bracket_high de algún outcome que tenga precio > 0.10)

    Si se cumplen todas:
    - El máximo del día fue temp_metar_actual (o el máximo registrado en snapshots del día)
    - Cualquier bracket > max_temp_del_dia → señal NO
    - El bracket correcto (contiene max_temp) si tiene precio < 0.85 → señal YES

    Confidence:
    - Base: 0.70
    - +0.10 si han pasado > 2h desde peak_hour
    - +0.10 si últimas 3 lecturas METAR son estables (delta < 0.3°C)
    - -0.10 si hay riesgo de tormenta (leer campo wx del METAR)
    """
```

**Cálculo del max_temp_dia:**
- Obtener todos los `metar_snapshots` del día para esa ciudad desde Supabase
- Tomar el máximo de `temp_c` (o `temp_f` según unidad)
- Si no hay snapshots (raro), usar el METAR actual

---

### `signals/forecast_divergence.py`

**Lógica:**

```python
async def evaluate_forecast_divergence(city: CityConfig, market: MarketState) -> list[Signal]:
    """
    Genera señales cuando el forecast GFS difiere significativamente del precio implícito.

    Solo para mercados de D+1 y D+2 (no D+0 que ya tiene METAR confiable,
    no D+3/D+4 que tienen demasiada incertidumbre para esta estrategia).

    Precio implícito del mercado:
    - Encontrar el bracket con mayor precio — ese es el "consenso" del mercado
    - El precio implícito = punto medio de ese bracket (ej: bracket 22-24°C → 23°C)

    Divergencia:
    - divergence = |forecast_temp - implied_market_temp| / implied_market_temp
    - Si divergence > FORECAST_MIN_DIVERGENCE_PCT → señal

    Dirección:
    - Si forecast > implied_market_temp: mercado está bajo → señal YES en bracket correcto según forecast
    - Si forecast < implied_market_temp: mercado está alto → señal NO en el bracket de consenso actual

    Confidence:
    - Base: 0.65 (menor que EOD porque hay más incertidumbre)
    - +0.10 si el modelo ha sido estable en las últimas 2 actualizaciones (6h cache)
    - -0.10 si hay diferencia de > 5°C entre GFS y ECMWF (señal de incertidumbre en modelos)
    """
```

---

### `signals/engine.py`

**Orquestador:**

```python
async def run_signal_cycle():
    """
    Ejecutar cada 30 minutos (sincronizado con ciclo METAR).

    Para cada ciudad activa:
    1. Obtener METAR más reciente
    2. Obtener mercados activos de esa ciudad (D+0 a D+4)
    3. Obtener forecast
    4. Evaluar end_of_day (solo para D+0)
    5. Evaluar forecast_divergence (solo para D+1 y D+2)
    6. Para cada señal con confidence >= SIGNAL_MIN_CONFIDENCE:
        a. Guardar en tabla signals con status='pending'
        b. Enviar alerta por Telegram
    """
```

---

### `alerts/telegram.py`

**Formato del mensaje de señal:**

```
🌡 SEÑAL WEATHERSNIPER

📍 Nueva York (KLGA)
📅 11 Abr 2026
📊 Estrategia: End of Day Lock

🎯 Posición: NO en bracket 62-63°F
💰 Precio actual: $0.38 (mercado cree 38% que llega a 62°F)
🌡 METAR actual: 58°F — lleva 2h estable
⏰ Son las 4:30 PM hora local (1.5h después del peak)

📈 Confianza: 82%
💵 Tamaño sugerido: $50 USDC (50 shares a $0.38 → ganancia si NO: $62)

💭 Razonamiento: La temperatura máxima del día fue 59°F a las 2:45 PM.
   Las últimas 4 lecturas METAR: 59, 58, 58, 58°F. No puede subir más.
   El bracket de 62-63°F cotiza a 38¢ — es prácticamente imposible.

ID señal: #247
```

**Botones inline de Telegram:**
- ✅ Aprobar y ejecutar
- ❌ Rechazar
- ℹ️ Ver detalles del mercado

**Comandos adicionales del bot:**
- `/status` — PnL del día, señales pendientes, capital disponible
- `/pending` — Lista de señales pendientes de aprobación
- `/history` — Últimos 10 trades con resultado
- `/capital [amount]` — Actualizar capital disponible en config

---

### `execution/polymarket_exec.py`

**Usando py-clob-client:**

```python
from py_clob_client.client import ClobClient

async def place_order(signal: Signal, approved_size_usdc: float) -> TradeResult:
    """
    Ejecutar orden en Polymarket.
    Solo se llama cuando DRY_RUN=false Y el operador aprobó vía Telegram.

    En DRY_RUN=true: simular la orden, loggear, y actualizar la señal como 'executed'
    con precio hipotético.

    Pasos:
    1. Validar que el mercado sigue activo
    2. Validar que el precio no se movió más de 3¢ desde la señal (precio stale)
    3. Calcular número de shares = size_usdc / current_price
    4. Colocar limit order con precio = current_price + 0.01 (taker) para mayor fill rate
    5. Guardar en tabla trades
    6. Confirmar por Telegram
    """
```

---

### `execution/position_sizer.py`

```python
def calculate_size(
    confidence: float,
    entry_price: float,
    available_capital_usdc: float
) -> float:
    """
    Kelly Criterion simplificado y conservador (half-Kelly).

    win_prob = confidence
    win_payout = (1 - entry_price) / entry_price  # odds a favor
    lose_prob = 1 - confidence

    kelly_fraction = (win_prob * win_payout - lose_prob) / win_payout
    half_kelly = kelly_fraction / 2

    size = min(
        available_capital * half_kelly,
        MAX_POSITION_USDC,
        available_capital * MAX_POSITION_PCT_CAPITAL
    )

    Nunca retornar < $5 (mínimo operativo) ni > MAX_POSITION_USDC.
    """
```

---

### `scheduler/jobs.py`

**Jobs con APScheduler:**

```python
# Job 1: Actualizar mercados de Polymarket
# Cada 5 minutos
# Llama a polymarket.fetch_temperature_markets()

# Job 2: Ciclo principal de señales
# Cada 30 minutos, offset de 2 min post-METAR (para dar tiempo a que WU procese)
# Llama a engine.run_signal_cycle()

# Job 3: Forecast update
# Cada 6 horas: 00:30, 06:30, 12:30, 18:30 UTC
# Llama a forecast.update_all_cities()

# Job 4: Limpieza de mercados viejos
# Diariamente a las 02:00 UTC
# Marca como 'resolved' los mercados con fecha < hoy
# Intenta obtener la temperatura de resolución de WU y guardarla

# Job 5: Reporte diario
# Diariamente a las 09:00 UTC
# Envía resumen por Telegram: PnL del día anterior, win rate, trades
```

---

### `main.py`

```python
from fastapi import FastAPI
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()          # Verificar conexión Supabase, correr migraciones pendientes
    await init_cities()      # Insertar/actualizar ciudades desde config
    scheduler = AsyncIOScheduler()
    register_jobs(scheduler)
    scheduler.start()
    yield
    # Shutdown
    scheduler.shutdown()

app = FastAPI(title="WeatherSniper", lifespan=lifespan)

# Endpoints de monitoreo
@app.get("/health")           # Status del sistema
@app.get("/signals/pending")  # Señales pendientes de aprobación
@app.get("/stats")            # PnL acumulado, win rate, métricas
@app.post("/signals/{id}/approve")  # Aprobar señal manualmente (además del botón Telegram)
@app.post("/signals/{id}/reject")   # Rechazar señal
```

---

## Requerimientos de calidad del código

### Manejo de errores
- Todo acceso a APIs externas en try/except con logging detallado
- Si una API falla, continuar con las ciudades que sí funcionan — no detener el ciclo completo
- Alertar por Telegram si una ciudad lleva > 2 horas sin METAR válido

### Logging
- Usar `logging` estándar de Python con formato estructurado
- Niveles: DEBUG para cada request API, INFO para cada ciclo y señal, WARNING para datos sospechosos, ERROR para fallas
- Incluir city_id en cada log relacionado a una ciudad

### Tipos
- Usar `dataclasses` o Pydantic para todos los modelos de datos
- Type hints en todas las funciones
- No usar `dict` sin tipado donde se puede usar un dataclass

### Tests mínimos
Crear `tests/` con:
- `test_metar.py` — mockear la API y verificar parsing correcto, incluyendo casos de dato viejo y timeout
- `test_signals.py` — casos: temperatura estabilizada genera señal EOD, temperatura en movimiento no genera señal, forecast diverge suficiente genera señal
- `test_position_sizer.py` — verificar que los límites se respetan siempre

### Configuración de ciudades
No hardcodear lógica por ciudad. Todo lo que varía por ciudad va en la tabla `cities` o en `CITIES` de config. El código debe funcionar igual para cualquier ciudad que se agregue a la tabla.

---

## Orden de implementación

### Fase 1 — Foundation (días 1-3)
1. Setup del proyecto: estructura de carpetas, `requirements.txt`, `.env.example`
2. Schema de Supabase: ejecutar `schema.sql`, verificar tablas
3. `data/metar.py` — implementar y testear con KLGA
4. `data/forecast.py` — implementar y testear con NYC
5. `data/polymarket.py` — fetch de mercados, parsing de questions, matching de ciudades
6. Insertar ciudades iniciales en Supabase

### Fase 2 — Signal Engine (días 4-6)
7. `signals/models.py` — todos los dataclasses
8. `signals/end_of_day.py` — implementar con tests
9. `signals/forecast_divergence.py` — implementar con tests
10. `signals/engine.py` — orquestador

### Fase 3 — Alerts y Ejecución (días 7-9)
11. `alerts/telegram.py` — bot con formato de mensaje y botones
12. `execution/position_sizer.py`
13. `execution/polymarket_exec.py` — solo DRY_RUN en esta fase
14. `scheduler/jobs.py` — todos los cron jobs
15. `main.py` — FastAPI + lifespan + endpoints de monitoreo

### Fase 4 — Validación y pulido (días 10-14)
16. Correr en DRY_RUN por 5-7 días con datos reales
17. Revisar señales generadas vs outcomes reales en Supabase
18. Ajustar thresholds de confidence y timing según datos
19. Activar DRY_RUN=false con capital mínimo ($200 USDC)

---

## Requirements.txt

```
fastapi>=0.110.0
uvicorn>=0.27.0
httpx>=0.27.0               # HTTP async client
apscheduler>=3.10.0         # Job scheduler
supabase>=2.4.0             # Supabase Python client
python-dotenv>=1.0.0
pydantic>=2.6.0
py-clob-client>=0.14.0      # Polymarket CLOB client
python-telegram-bot>=21.0.0 # Telegram bot
pytz>=2024.1                # Timezone handling
pytest>=8.0.0
pytest-asyncio>=0.23.0
pytest-httpx>=0.30.0        # Para mockear httpx en tests
```

---

## Notas críticas para la implementación

**Sobre la fuente de resolución:**
El campo de resolución de cada mercado en Polymarket está en el `description` o `rules`. Parsear la URL de Wunderground que aparece ahí para confirmar el ICAO. Si el ICAO de resolución difiere del ICAO en la config de la ciudad, loggear un WARNING y usar el ICAO de las rules (no el de la config). Este es el riesgo #1 del sistema.

**Sobre los horarios METAR:**
Los METARs se emiten nominalmente a :00 y :30 pero pueden aparecer con 2-8 minutos de delay en la API de aviationweather.gov. No asumir que el METAR de las 15:00 llega exactamente a las 15:00.

**Sobre la conversión de temperaturas:**
Open-Meteo siempre devuelve Celsius. Polymarket USA ciudades en Fahrenheit (NYC, LA). Internamente siempre trabajar en Celsius. Convertir solo al generar el mensaje de Telegram o al comparar con brackets del mercado (que pueden estar en F o C según la ciudad).

**Sobre la liquidez y el slippage:**
Antes de ejecutar cualquier orden, verificar el orderbook del mercado. Si el tamaño de la orden supera el 20% de la liquidez disponible en ese bracket, reducir el tamaño automáticamente o rechazar la señal. Implementar esto en `polymarket_exec.py`.

**Sobre el modo DRY_RUN:**
Con `DRY_RUN=true`, el sistema debe comportarse exactamente igual excepto por la llamada final al CLOB. Todos los logs, Telegram alerts, y registros en Supabase deben ocurrir igual. Los trades en DRY_RUN se marcan con `order_id='DRY_RUN_{timestamp}'`.