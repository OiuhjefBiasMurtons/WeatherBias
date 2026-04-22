# WeatherSniper — Informe de Auditoría

**Fecha**: 20 de abril de 2026
**Alcance**: Análisis completo del código, validación contra API live de Polymarket, evaluación de viabilidad

---

## Resumen Ejecutivo

WeatherSniper tiene **17 bugs y errores de diseño** — 5 de ellos críticos, suficientes para que el sistema sea incapaz de generar operaciones rentables en su estado actual. El problema más grave, no identificado en ningún análisis previo, es que **Polymarket cobra una comisión del 5% (taker fee) en mercados meteorológicos** y esto no está contemplado en ningún punto del código. Con edges reportados del 8-10%, el edge real después de comisiones baja al 3-5%, convirtiendo muchas señales "positivas" en pérdidas netas.

El código está bien estructurado y la idea central (explotar el retraso de los modelos de previsión meteorológica frente a los precios del mercado de predicción) tiene mérito teórico. Sin embargo, el mercado es bastante más eficiente de lo que el sistema asume: las páginas de eventos de Polymarket muestran análisis generado por IA que ya incorpora GFS, ECMWF, NWS y datos históricos. El camino viable es estrecho: centrarse exclusivamente en la estrategia EOD METAR con precios reales del orderbook, comisiones correctamente calculadas y filtros más estrictos.

---

## Índice

1. [Problemas Críticos (Bloquean toda rentabilidad)](#1-problemas-críticos)
2. [Problemas Graves (Corrompen la calidad de señales)](#2-problemas-graves)
3. [Problemas Moderados (Reducen el edge / causan errores)](#3-problemas-moderados)
4. [Problemas Menores (Calidad de código / robustez)](#4-problemas-menores)
5. [Evaluación de Viabilidad](#5-evaluación-de-viabilidad)
6. [Prioridad de Correcciones](#6-prioridad-de-correcciones)

---

## 1. Problemas Críticos

Estos problemas implican que el sistema **no puede ser rentable** en su estado actual, independientemente de las condiciones del mercado.

### 1.1 — Comisión del 5% (taker fee) no contemplada (NUEVO — no estaba en el análisis previo)

**Archivo**: Ninguno — completamente ausente del código
**Impacto**: Cada cálculo de edge está sobreestimado en 5 puntos porcentuales

Los mercados meteorológicos de Polymarket tienen `feeSchedule: {rate: 0.05, takerOnly: true}`. Confirmado obteniendo datos live de la Gamma API para mercados de temperatura de NYC y Londres.

La comisión se aplica sobre la ganancia de las operaciones ganadoras. Para un token YES comprado a $0.60 que resuelve a $1.00:
- Ganancia bruta: $0.40
- Comisión: $0.40 × 5% = $0.02
- Ganancia neta: $0.38
- Precio efectivo real: $0.62

Ningún archivo del código hace referencia a esta comisión. `position_sizer.py` no la descuenta, `forecast_divergence.py` no la resta del edge, `end_of_day.py` no la considera, y `polymarket_exec.py` no la incorpora en las decisiones de orden.

**Consecuencia**: Una señal con 8% de edge reportado tiene ~3% de edge real después de comisiones. Muchas señales que superan el umbral `FORECAST_MIN_DIVERGENCE_PCT = 0.08` son en realidad operaciones con edge negativo.

**Corrección**:
```python
# En el cálculo de edge (ambas estrategias):
gross_edge = model_prob - market_price
fee_on_profit = (1.0 - market_price) * 0.05  # comisión sobre ganancia potencial
net_edge = gross_edge - fee_on_profit

# Solo señal si net_edge > umbral
if net_edge < settings.MIN_NET_EDGE:
    continue
```

### 1.2 — Fuente de precios incorrecta (Punto #2 previo, confirmado)

**Archivo**: `weathersniper/data/polymarket.py` — usa `outcomePrices` de la Gamma API
**Impacto**: Las operaciones se ejecutan a precios peores que los asumidos por la señal

El campo `outcomePrices` de la Gamma API es un precio indicativo (mid-market o última operación), no el precio al que realmente se ejecutaría una orden. Ejemplo live de NYC 21 Apr, bracket 50-51°F:
- `outcomePrices`: 0.375
- CLOB `bestBid`: 0.36
- CLOB `bestAsk`: 0.39

El spread es de 3 céntimos (8% del precio). Combinado con la comisión del 5%, una señal calculada sobre 0.375 que ejecuta a 0.39 pierde 1.5 puntos porcentuales adicionales de edge antes de que se aplique siquiera la comisión.

**Corrección**: Obtener el orderbook del CLOB API (`https://clob.polymarket.com/book?token_id=TOKEN_ID`), usar `bestAsk` para señales de compra y `bestBid` para señales de venta. Recalcular el edge contra el precio ejecutable, no el indicativo.

### 1.3 — Brackets de Londres/Celsius completamente rotos (NUEVO — no estaba en el análisis previo)

**Archivo**: `weathersniper/data/polymarket.py`, función `_parse_bracket()`
**Impacto**: Todas las ciudades no-US con brackets de un solo grado Celsius producen cero outcomes

El parser de brackets maneja estos patrones:
- `"60-61°F"` → match de rango ✓
- `"55°F or below"` → match de límite inferior ✓
- `"75°F or higher"` → match de límite superior ✓

Los mercados de Londres usan brackets de un solo grado: `"14°C"`, `"13°C"`. Estos no coinciden con **ninguno** de los patrones regex. `_parse_bracket()` devuelve `None`, el outcome se descarta silenciosamente, y el mercado acaba con cero outcomes parseables.

Afecta a Londres, París, Tokio y cualquier otra ciudad que use Celsius o brackets de un grado. El sistema se probó principalmente contra mercados US/Fahrenheit, y toda esta clase de mercados es inoperante desde el inicio.

**Corrección**:
```python
# Añadir patrón de un solo grado a _parse_bracket():
single_match = re.match(r"^(\d+)°([FC])$", outcome_title)
if single_match:
    temp = int(single_match.group(1))
    unit = single_match.group(2)
    return Bracket(low=temp, high=temp, unit=unit)
```

### 1.4 — Tamaño mínimo de orden rechazado (NUEVO — no estaba en el análisis previo)

**Archivo**: `weathersniper/execution/position_sizer.py`
**Impacto**: Todas las órdenes serían rechazadas por Polymarket en trading real

Los mercados meteorológicos de Polymarket tienen `orderMinSize: 5` (mínimo $5 por orden). El position sizer es un stub que devuelve `$1` o `$2`:

```python
def compute_size(self, signal: Signal) -> float:
    if signal.confidence >= 0.7:
        return 2.0
    return 1.0
```

Cada orden que el sistema intente sería rechazada por el exchange. Esto no genera pérdidas directas (las órdenes simplemente fallan), pero significa que el sistema nunca ha podido ejecutar una operación real y todo el testing ha sido en modo DRY_RUN sin que esto haya aflorado.

**Corrección**: Implementar sizing real por criterio de Kelly o fracción fija, con un mínimo de `$5` y un techo basado en el tamaño del portfolio y la liquidez disponible.

### 1.5 — Sin cálculo de edge real — semántica YES vs NO ignorada (Punto #5 previo, confirmado)

**Archivo**: `weathersniper/signals/forecast_divergence.py`, `weathersniper/signals/end_of_day.py`
**Impacto**: El sistema no sabe si comprar tokens YES o NO

La generación de señales calcula `confidence` como un score genérico 0-1, pero nunca resuelve la pregunta fundamental: **"¿Está el mercado infravalorando YES o infravalorando NO?"**

En forecast_divergence.py, la divergencia se calcula como `abs(model_prob - market_price)` y si supera el umbral se emite una señal. Pero la señal no codifica correctamente si el modelo dice que YES está barato (comprar YES) o NO está barato (comprar NO). El campo `side` existe en el dataclass Signal pero se rellena de forma inconsistente.

En end_of_day.py, el filtro del lado NO es `no_price > 0.05` sin límite superior — compraría NO a 0.998 (pagando 99.8 céntimos para ganar como máximo 0.2 céntimos menos la comisión del 5%).

**Corrección**: Cada estrategia debe generar:
- `direction`: BUY_YES o BUY_NO
- `entry_price`: precio ejecutable real del CLOB
- `model_probability`: probabilidad YES del modelo
- `net_edge`: `model_prob - entry_price - fee_adjustment` (para YES) o `(1 - model_prob) - entry_price - fee_adjustment` (para NO)

---

## 2. Problemas Graves

Corrompen la calidad de las señales y llevan a malas decisiones de trading, pero el sistema "funciona" con ellos presentes.

### 2.1 — Ventana de deduplicación demasiado corta (Punto #1 previo, confirmado)

**Archivo**: `weathersniper/signals/engine.py:898` — `_SIGNAL_DEDUP_HOURS = 4`
**Impacto**: Señales duplicadas sobre el mismo mercado dentro de un ciclo GFS

GFS actualiza cada 6 horas (00z, 06z, 12z, 18z). La ventana de dedup es de 4 horas, por lo que el sistema puede emitir una señal en la hora 0 y volver a emitir la misma señal en la hora 5 con los mismos datos GFS obsoletos, antes de que llegue la siguiente actualización a la hora 6.

**Corrección**: Establecer `_SIGNAL_DEDUP_HOURS = 7` (> ciclo GFS de 6h), o mejor aún, deduplicar por `(market_id, strategy, gfs_cycle_id)`.

### 2.2 — Umbral de divergencia no coincide con la especificación (Punto #4 previo, confirmado)

**Archivo**: `weathersniper/config.py` — `FORECAST_MIN_DIVERGENCE_PCT = 0.08`
**Especificación**: `prompt.md` indica `0.15`
**Impacto**: El sistema dispara señales con divergencias mucho menores de lo diseñado

El umbral del 8% se estableció probablemente durante pruebas para ver más señales. Pero sin la comisión del 5% contabilizada, una divergencia bruta del 8% es solo un 3% neto — y antes de que el error de fuente de precio consuma otros 1-2 puntos. En la práctica, el sistema dispara operaciones con edge real nulo o negativo.

**Corrección**: Establecer un mínimo de `0.15`. Tras implementar el edge ajustado por comisiones, este umbral debe aplicarse sobre el edge **neto**, no sobre la divergencia bruta.

### 2.3 — Sin validación cruzada de previsiones (Punto #3 previo, confirmado)

**Archivo**: `weathersniper/data/forecast.py` — solo Open-Meteo (GFS)
**Impacto**: Previsiones de una sola fuente vs. mercados que agregan múltiples fuentes

Las páginas de eventos de Polymarket incluyen análisis de IA que referencia explícitamente GFS, ECMWF, NWS y registros históricos. Operar solo con GFS contra un mercado que ya tiene GFS en precio no es una ventaja — es pagar el spread para coincidir con el consenso.

La validación cruzada también detectaría ejecuciones anómalas del GFS que a veces producen previsiones aberrantes que se corrigen en el siguiente ciclo.

**Corrección**: Añadir al menos una fuente adicional (la API de punto de previsión de NWS es gratuita y sencilla). Solo señalar cuando 2+ fuentes coincidan en una divergencia respecto al precio del mercado.

### 2.4 — Señales con tamaño cero persistidas y alertadas (Punto #6 previo, confirmado)

**Archivo**: `weathersniper/signals/engine.py`
**Impacto**: Señales de ruido con `suggested_size_usdc = 0` disparan alertas en Telegram

El position sizer puede devolver 0 para señales de muy baja confianza, pero el engine las persiste y alerta igualmente. El usuario recibe mensajes de Telegram de operaciones que el propio sistema no ejecutaría.

**Corrección**: Añadir `if signal.suggested_size_usdc <= 0: continue` antes de persistir y alertar.

### 2.5 — Filtro 1-por-mercado usa clave incorrecta (Punto #8 previo, confirmado)

**Archivo**: `weathersniper/signals/engine.py`
**Impacto**: Señales múltiples del mismo mercado de distintas estrategias, o supresión de señales válidas

La lógica de dedup/filtro usa solo `market_id`, pero tanto `end_of_day` como `forecast_divergence` pueden apuntar al mismo mercado. Una señal de forecast_divergence puede suprimir una señal mejor de end_of_day (o viceversa) porque comparten el mismo `market_id`.

**Corrección**: Clave compuesta por `(market_id, outcome_id, strategy)` — cada par estrategia-outcome debe evaluarse de forma independiente.

---

## 3. Problemas Moderados

### 3.1 — Filtro de lado NO en EOD demasiado permisivo (Punto #7 previo, confirmado)

**Archivo**: `weathersniper/signals/end_of_day.py`
**Impacto**: Compra tokens NO a precios absurdos (0.95+) con ganancia esperada de céntimos

El filtro es `no_price > 0.05` sin límite superior. Un token NO a $0.95 significa pagar 95 céntimos para ganar quizás 5 céntimos (menos comisión del 5% = 4.75 céntimos), con el riesgo de perder 95 céntimos. La relación riesgo/beneficio es terrible incluso con alta confianza del modelo.

**Corrección**: Añadir límite superior `no_price < 0.85` y exigir edge neto (después de comisiones) > 5%.

### 3.2 — Caché de previsiones no persistida (Punto #9 previo, confirmado)

**Archivo**: `weathersniper/data/forecast.py` — diccionario en memoria con TTL de 6h
**Impacto**: Sin historial de previsiones para análisis de tendencia; arranque en frío en cada reinicio

Cada reinicio de la app pierde todas las previsiones cacheadas. No hay forma de detectar la tendencia de la previsión (¿está el GFS convergiendo o divergiendo respecto al precio del mercado?). Una previsión estable durante 3 ciclos es mucho más fiable que una que acaba de cambiar.

**Corrección**: Guardar previsiones en una tabla `forecast_snapshots`. Usar datos de tendencia para ponderar la confianza.

### 3.3 — Sin verificación de profundidad de liquidez (Punto #10 previo, confirmado)

**Archivo**: `weathersniper/execution/polymarket_exec.py`
**Impacto**: Las órdenes pueden mover el mercado o ejecutarse parcialmente a malos precios

Existe una verificación básica de liquidez pero no comprueba la profundidad del orderbook. Una orden de $5 en un bracket con $2 de asks al mejor precio se ejecutaría parcialmente y el resto quedaría como orden resting o se llenaría a peor precio.

**Corrección**: Obtener profundidad del orderbook, verificar liquidez suficiente al nivel de precio objetivo y limitar el tamaño de la orden a la profundidad disponible con slippage aceptable.

### 3.4 — Sin seguimiento de resolución de mercados (Punto #11 previo, confirmado)

**Archivo**: Ausente del código
**Impacto**: Sin tracking de P&L, sin medición del rendimiento por estrategia

El sistema no tiene mecanismo para verificar si las señales pasadas fueron correctas una vez resueltos los mercados. Sin tracking de resolución, es imposible saber si el sistema es rentable, qué estrategia funciona mejor, o si los cambios de parámetros mejoran los resultados.

**Corrección**: Añadir tabla `resolutions` y un job periódico que cruce mercados resueltos con señales históricas. Calcular tasa de acierto, edge medio capturado y ratio de Sharpe por estrategia.

### 3.5 — Sin degradación controlada ante fallos de API (Punto #12 previo, confirmado)

**Archivo**: Varios — `polymarket.py`, `forecast.py`, `metar.py`
**Impacto**: Fallos transitorios de API pueden causar ciclos perdidos o datos obsoletos

METAR tiene lógica de reintento con backoff, pero las llamadas a la Gamma API y Open-Meteo no tienen robustez equivalente. Un único fallo en la Gamma API puede hacer que todo el ciclo de señales genere señales basadas en precios de mercado obsoletos.

**Corrección**: Añadir reintento con backoff exponencial en todas las llamadas a APIs externas. Rastrear la frescura de los datos y omitir la generación de señales si los datos clave están obsoletos más allá de un umbral.

---

## 4. Problemas Menores

### 4.1 — Config CITIES tiene 9 ciudades, la mayoría no parsearán

Solo las ciudades US con brackets de rango en Fahrenheit parsearán correctamente. Las 4-5 ciudades internacionales (Londres, París, Tokio, etc.) fallan silenciosamente por el §1.3. La cobertura efectiva es de ~4-5 ciudades.

### 4.2 — El timing del scheduler crea puntos ciegos

Los ciclos de señales se ejecutan a los :02 y :32 de cada hora. Las actualizaciones de precios de mercado corren cada 5 minutos. Una actualización del GFS a las :15 no se reflejará en señales hasta las :32 — un retraso de 17 minutos en el que la nueva previsión no genera acción.

### 4.3 — Sin rate limiting en la API de Polymarket

La Gamma API y el CLOB API tienen límites de tasa no documentados. Consultar 9 ciudades × múltiples mercados cada 5 minutos podría provocar throttling. No existe backoff ni seguimiento de tasa de peticiones.

### 4.4 — El modo DRY_RUN no simula comisiones ni fills

DRY_RUN registra la operación pero no simula cuál habría sido el P&L real incluyendo comisiones, slippage y precios de ejecución reales. Los resultados de paper trading parecerán mejores que la realidad.

---

## 5. Evaluación de Viabilidad

### ¿Es viable la estrategia central?

**Forecast Divergence (D+1/D+2): Probablemente no viable.**

El mercado es eficiente frente a previsiones GFS de una sola fuente. Las propias páginas de eventos de Polymarket muestran análisis de IA que incorpora GFS, ECMWF, NWS y datos históricos. Los traders que fijan los precios ya tienen acceso a los mismos datos (o mejores). El edge teórico es pequeño y probablemente negativo después del 5% de taker fee y el spread bid-ask.

Para que forecast_divergence funcione se necesitaría:
- Múltiples fuentes de previsión independientes que diverjan del mercado
- Un modelo propio que combine previsiones mejor que el consenso del mercado
- Edge suficiente para sobrevivir 5% de comisión + 2-3% de spread = mínimo 7-8% de edge bruto

Es un listón muy alto para un mercado que ya es razonablemente eficiente.

**End-of-Day METAR (D+0): Potencialmente viable, ventana estrecha.**

Esta estrategia tiene una fuente de edge legítima: lecturas de temperatura de aeropuertos en tiempo real en las últimas horas antes de la resolución de un mercado. Si a las 3pm la temperatura actual es 22°C y el mercado de máxima del día para 20-21°C sigue cotizando al 30%, hay alpha real en saber que ya se ha superado la máxima.

El edge existe porque:
1. Los datos METAR se actualizan cada ~20 minutos y son públicos pero no los monitoriza la mayoría de traders
2. En las últimas 2-4 horas antes de la resolución, la incertidumbre de temperatura restante es pequeña
3. El mercado puede no re-preciarse con suficiente rapidez tras las actualizaciones METAR

Sin embargo, la ventana es estrecha (quizás 2-4 horas por día por ciudad), el edge por operación es pequeño, y después del 5% de comisión se reduce más. El volumen sería limitado.

### Potencial de beneficio realista

Asumiendo todos los bugs corregidos y usando solo la estrategia EOD METAR:
- ~5 ciudades US × ~1 operación/día × ~$10 tamaño medio × ~3% edge neto = **~$1.50/día**
- Con tamaños mayores en señales de alta confianza: quizás **$5-15/día**

Es un proyecto de hobby, no un negocio. Puede ser un buen ejercicio de aprendizaje en sistemas de trading cuantitativo.

### ¿Qué lo haría más viable?

1. **Forecasting ensemble multi-fuente**: Combinar GFS + ECMWF + NWS + modelo de persistencia. Solo operar cuando el ensemble diverja del mercado.
2. **Trading METAR event-driven**: En lugar de ciclos programados, procesar actualizaciones METAR en streaming y reaccionar en segundos. La ventaja de first-mover importa.
3. **Expandir a mercados meteorológicos no-temperatura**: Los mercados de lluvia/nieve pueden ser menos eficientes que los de temperatura (más difíciles de predecir con precisión).
4. **Market-making en lugar de tomar liquidez**: Si se identifican rangos estables, proveer liquidez (ganando el spread) puede ser más rentable que consumirla.

---

## 6. Prioridad de Correcciones

Correcciones ordenadas por impacto y esfuerzo. Hacer en este orden.

| Prioridad | Problema | Esfuerzo | Impacto |
|-----------|----------|----------|---------|
| **P0** | 1.1 — Añadir comisión 5% al cálculo de edge | Pequeño | Evita todas las operaciones con edge negativo |
| **P0** | 1.2 — Usar precios del orderbook CLOB | Medio | Cálculo de edge correcto |
| **P0** | 1.5 — Implementar lógica de dirección YES/NO | Medio | El sistema sabe qué comprar |
| **P0** | 1.4 — Corregir position sizer (mínimo $5) | Pequeño | Las órdenes son aceptadas |
| **P1** | 1.3 — Corregir parser de brackets Celsius/un grado | Pequeño | Desbloquea mercados internacionales |
| **P1** | 2.2 — Establecer umbral de divergencia en 0.15+ neto | Pequeño | Filtra señales débiles |
| **P1** | 3.1 — Limitar precio lado NO a 0.85 | Pequeño | Evita operaciones absurdas |
| **P1** | 2.4 — Filtrar señales de tamaño cero | Pequeño | Reduce ruido en alertas |
| **P2** | 2.1 — Corregir ventana de dedup a 7h | Pequeño | Evita señales duplicadas |
| **P2** | 2.5 — Corregir clave de dedup para incluir estrategia | Pequeño | Comportamiento multi-estrategia correcto |
| **P2** | 2.3 — Añadir validación cruzada con NWS | Medio | Mejor calidad de señales |
| **P2** | 3.3 — Añadir verificación de profundidad del orderbook | Medio | Evita malos fills |
| **P3** | 3.2 — Persistir historial de previsiones | Medio | Permite análisis de tendencia |
| **P3** | 3.4 — Añadir tracking de resoluciones / P&L | Grande | Mide rendimiento real |
| **P3** | 3.5 — Añadir retry/backoff en todas las APIs | Medio | Fiabilidad |
| **P3** | 4.1-4.4 — Correcciones menores | Pequeño cada uno | Pulido |

### Conjunto mínimo antes de cualquier trading real:
- **1.1** (comisión), **1.2** (precios CLOB), **1.4** (tamaño mínimo), **1.5** (lógica YES/NO) — sin estos cuatro, cada operación que el sistema intente será incorrecta o rechazada.

---

## Apéndice: Datos Live de Mercado

### NYC 21 de Abril — Temperatura Máxima (obtenido el 20 Apr 2026)

```
Mercado: "NYC High Temperature for April 21"
feeSchedule: { rate: 0.05, takerOnly: true }
orderMinSize: 5

Bracket: 50-51°F
  outcomePrices: 0.375
  CLOB bestBid: 0.36
  CLOB bestAsk: 0.39

Bracket: 52-53°F
  outcomePrices: 0.28
  CLOB bestBid: 0.27
  CLOB bestAsk: 0.30
```

### Londres 21 de Abril — Temperatura Máxima (obtenido el 20 Apr 2026)

```
Mercado: "London High Temperature for April 21"
feeSchedule: { rate: 0.05, takerOnly: true }

Ejemplos de brackets: "14°C", "13°C", "15°C or higher", "12°C or below"
→ Brackets de un grado "14°C" fallan en _parse_bracket()
→ Solo "or higher"/"or below" parsearían (si se gestionase °C)
```

### Contexto del evento en Polymarket (generado por IA, visible en la página del evento)

```
"This market resolves based on the reported high temperature at Central Park,
NYC on April 21, 2026. Current NWS forecast indicates a high of 51°F.
GFS model run from 06Z shows 52°F. ECMWF shows 50°F. Historical average
for this date is 63°F. Recent temperature trend has been below normal..."
```

Esto confirma que el mercado ya incorpora los mismos datos GFS que usa WeatherSniper.
