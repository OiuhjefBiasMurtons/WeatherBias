import logging
from datetime import date

from weathersniper.config import CITIES, SIGNAL_MIN_CONFIDENCE
from weathersniper.data.forecast import get_forecast
from weathersniper.data.metar import get_metar
from weathersniper.data.polymarket import fetch_temperature_markets
from weathersniper.db.client import get_supabase
from weathersniper.signals.end_of_day import evaluate_end_of_day
from weathersniper.signals.forecast_divergence import evaluate_forecast_divergence
from weathersniper.signals.models import CityConfig, MarketState, Signal

logger = logging.getLogger(__name__)


def _build_city_configs() -> dict[str, CityConfig]:
    """Construye CityConfig a partir de la config."""
    configs: dict[str, CityConfig] = {}
    for c in CITIES:
        configs[c["id"]] = CityConfig(**c)
    return configs


async def run_signal_cycle() -> int:
    """
    Ciclo principal de senales. Ejecutar cada 30 minutos.

    Para cada ciudad activa:
    1. Obtener METAR
    2. Obtener mercados activos
    3. Obtener forecast
    4. Evaluar end_of_day (D+0)
    5. Evaluar forecast_divergence (D+1, D+2)
    6. Guardar y alertar senales con confidence >= min
    """
    city_configs = _build_city_configs()
    all_markets = await fetch_temperature_markets()
    all_signals: list[Signal] = []

    # Agrupar mercados por ciudad
    markets_by_city: dict[str, list] = {}
    for mkt in all_markets:
        markets_by_city.setdefault(mkt.city_id, []).append(mkt)

    for city_id, city in city_configs.items():
        if not city.active:
            continue

        logger.info("Signal cycle starting city_id=%s", city_id)

        # 1. METAR
        metar = await get_metar(city.icao)
        if metar:
            logger.info("METAR city_id=%s temp=%.1f°C (%.1f°%s) observed=%s",
                        city_id, metar.temp_c, metar.temp_f if city.unit == "F" else metar.temp_c,
                        city.unit, metar.observed_at.strftime("%H:%M UTC"))
        else:
            logger.warning("No METAR available city_id=%s", city_id)

        # 2. Forecast
        forecast_temps = await get_forecast(city) or {}
        logger.info("Forecast city_id=%s dates=%d", city_id, len(forecast_temps))

        # 3. Mercados de esta ciudad
        city_markets = markets_by_city.get(city_id, [])
        if not city_markets:
            logger.info("No markets for city_id=%s (skipping)", city_id)
            continue
        logger.info("Markets city_id=%s count=%d", city_id, len(city_markets))

        for poly_market in city_markets:
            market_state = MarketState(
                market_id=poly_market.condition_id,
                city_id=city_id,
                target_date=poly_market.target_date,
                market_title=poly_market.question,
                outcomes=poly_market.outcomes,
                last_metar=metar,
                forecast_temps=forecast_temps,
            )

            # 4. End of day (solo D+0)
            try:
                eod_signals = await evaluate_end_of_day(city, market_state)
                all_signals.extend(eod_signals)
            except Exception as exc:
                logger.error(
                    "EOD evaluation failed city_id=%s market=%s: %s",
                    city_id, poly_market.condition_id, exc,
                )

            # 5. Forecast divergence (D+1, D+2)
            try:
                fd_signals = await evaluate_forecast_divergence(city, market_state)
                all_signals.extend(fd_signals)
            except Exception as exc:
                logger.error(
                    "Forecast divergence failed city_id=%s market=%s: %s",
                    city_id, poly_market.condition_id, exc,
                )

    # 6. Limitar a 1 señal por mercado: la de mayor confianza (YES preferido en empate)
    best_by_market: dict[str, Signal] = {}
    for s in all_signals:
        existing = best_by_market.get(s.market_id)
        if existing is None:
            best_by_market[s.market_id] = s
        elif s.confidence > existing.confidence:
            best_by_market[s.market_id] = s
        elif s.confidence == existing.confidence and s.side.value == "YES":
            best_by_market[s.market_id] = s
    all_signals = list(best_by_market.values())

    # 7. Filtrar por confidence minima
    qualified = [s for s in all_signals if s.confidence >= SIGNAL_MIN_CONFIDENCE]

    from weathersniper.alerts.telegram import send_signal_alert
    new_count = 0
    skipped_count = 0
    for signal in qualified:
        # Deduplicar: no re-alertar si ya existe una señal pending para mismo
        # mercado + bracket + side
        if await _signal_already_pending(signal):
            logger.info(
                "Duplicate pending — skipping city_id=%s bracket=%.0f-%.0f side=%s",
                signal.city_id, signal.bracket_low, signal.bracket_high, signal.side.value,
            )
            skipped_count += 1
            continue
        signal_id = await _persist_signal(signal)
        await send_signal_alert(signal, signal_id)
        new_count += 1

    logger.info(
        "Signal cycle complete: %d total, %d qualified, %d sent, %d skipped (already pending)",
        len(all_signals), len(qualified), new_count, skipped_count,
    )
    return new_count


_SIGNAL_DEDUP_HOURS = 4  # No re-alertar si ya existe una señal <= N horas de antiguedad


async def _signal_already_pending(signal: Signal) -> bool:
    """Retorna True si ya existe una señal pending para el mismo mercado+bracket+side
    creada en las últimas _SIGNAL_DEDUP_HOURS horas."""
    try:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=_SIGNAL_DEDUP_HOURS)).isoformat()
        sb = get_supabase()
        resp = (
            sb.table("signals")
            .select("id")
            .eq("market_id", signal.market_id)
            .eq("bracket_low", signal.bracket_low)
            .eq("bracket_high", signal.bracket_high)
            .eq("side", signal.side.value)
            .eq("status", "pending")
            .gte("created_at", cutoff)
            .limit(1)
            .execute()
        )
        return bool(resp.data)
    except Exception as exc:
        logger.error("Failed to check duplicate signal: %s", exc)
        return False


async def _persist_signal(signal: Signal) -> int | None:
    """Guarda la senal en Supabase y retorna el id."""
    try:
        sb = get_supabase()
        resp = sb.table("signals").insert({
            "city_id": signal.city_id,
            "market_id": signal.market_id,
            "strategy": signal.strategy.value,
            "side": signal.side.value,
            "bracket_low": signal.bracket_low,
            "bracket_high": signal.bracket_high,
            "entry_price": signal.entry_price,
            "metar_temp": signal.metar_temp,
            "forecast_temp": signal.forecast_temp,
            "confidence": signal.confidence,
            "suggested_size": signal.suggested_size_usdc,
            "status": "pending",
            "notes": signal.reasoning,
        }).execute()
        if resp.data:
            signal_id = resp.data[0].get("id")
            logger.info("Signal saved id=%s city_id=%s", signal_id, signal.city_id)
            return signal_id
    except Exception as exc:
        logger.error("Failed to persist signal city_id=%s: %s", signal.city_id, exc)
    return None
