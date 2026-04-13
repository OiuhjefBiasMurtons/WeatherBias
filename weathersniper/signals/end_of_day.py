import logging
from datetime import datetime, date, timedelta, timezone

import pytz

from weathersniper.config import (
    EOD_MAX_TEMP_DELTA_LAST_2H,
    EOD_MIN_HOURS_PAST_PEAK,
    CITIES,
)
from weathersniper.db.client import get_supabase
from weathersniper.execution.position_sizer import calculate_size
from weathersniper.signals.models import (
    CityConfig,
    MarketState,
    Signal,
    SignalSide,
    SignalStrategy,
)

logger = logging.getLogger(__name__)


async def evaluate_end_of_day(
    city: CityConfig, market: MarketState
) -> list[Signal]:
    """
    Genera senales cuando el maximo del dia ya esta establecido.

    Condiciones:
    1. Hora local >= peak_hour + EOD_MIN_HOURS_PAST_PEAK
    2. METAR reciente (< 90 min)
    3. Temperatura estable: delta ultimas 2h < EOD_MAX_TEMP_DELTA_LAST_2H
    4. Temperatura actual < bracket_high de algun outcome con precio > 0.10
    """
    today_utc = datetime.now(timezone.utc).date()
    if market.target_date != today_utc:
        return []

    tz = pytz.timezone(city.timezone)
    now_local = datetime.now(tz)
    hours_past_peak = (
        now_local.hour + now_local.minute / 60 - city.peak_hour_local
    )

    logger.info(
        "EOD eval city_id=%s local_time=%s hours_past_peak=%.1fh (need >= %.1f)",
        city.id, now_local.strftime("%H:%M"), hours_past_peak, EOD_MIN_HOURS_PAST_PEAK,
    )

    if hours_past_peak < EOD_MIN_HOURS_PAST_PEAK:
        logger.info(
            "EOD skip city_id=%s: only %.1fh past peak, need %.1f",
            city.id, hours_past_peak, EOD_MIN_HOURS_PAST_PEAK,
        )
        return []

    if not market.last_metar:
        logger.info("EOD skip city_id=%s: no METAR available", city.id)
        return []

    # Obtener max temp del dia de metar_snapshots
    max_temp_c, snapshots = await _get_day_max_temp(city.id, city.unit)
    if max_temp_c is None:
        max_temp_c = market.last_metar.temp_c
    logger.info("EOD city_id=%s max_temp_c=%.1f snapshots=%d", city.id, max_temp_c, len(snapshots))

    # Verificar estabilidad: delta entre temp actual y max de ultimas 2h
    recent_temps = await _get_recent_temps(city.id, hours=2)
    logger.info("EOD city_id=%s recent_temps=%s", city.id, recent_temps[:5])
    if recent_temps:
        delta = max(recent_temps) - min(recent_temps)
        if delta > EOD_MAX_TEMP_DELTA_LAST_2H:
            logger.info(
                "EOD skip city_id=%s: temp not stable delta=%.2f°C (max %.2f)",
                city.id, delta, EOD_MAX_TEMP_DELTA_LAST_2H,
            )
            return []

    # Determinar la temperatura de referencia en la unidad del mercado
    if city.unit == "F":
        max_temp_market = max_temp_c * 9 / 5 + 32
    else:
        max_temp_market = max_temp_c

    signals: list[Signal] = []

    for outcome in market.outcomes:
        if outcome.price < 0.10:
            continue

        # Bracket que contiene el max temp -> senal YES si precio bajo
        if outcome.bracket_low <= max_temp_market <= outcome.bracket_high:
            if outcome.price < 0.85:
                confidence, conf_detail = _calculate_confidence(
                    hours_past_peak, recent_temps, market.last_metar.raw
                )
                size = calculate_size(confidence, outcome.price, 2000.0)
                signals.append(Signal(
                    city_id=city.id,
                    market_id=market.market_id,
                    strategy=SignalStrategy.END_OF_DAY,
                    side=SignalSide.YES,
                    bracket_low=outcome.bracket_low,
                    bracket_high=outcome.bracket_high,
                    entry_price=outcome.price,
                    confidence=confidence,
                    suggested_size_usdc=size,
                    metar_temp=max_temp_market,
                    market_title=market.market_title,
                    target_date=market.target_date,
                    confidence_detail=conf_detail,
                    reasoning=(
                        f"Max del día: {max_temp_market:.1f}°{city.unit} "
                        f"({now_local.strftime('%H:%M')} hora local, "
                        f"{hours_past_peak:.1f}h después del pico). "
                        f"Temp estable las últimas 2h."
                    ),
                ))

        # Bracket por encima del max temp -> senal NO
        # Solo si el mercado aun cree que hay >15% de probabilidad (YES > 0.15)
        # Si YES ya esta en 0.02, el mercado ya lo sabe — no hay edge.
        elif outcome.bracket_low > max_temp_market and 0.15 < outcome.price < 0.85:
            no_price = round(1 - outcome.price, 3)
            confidence, conf_detail = _calculate_confidence(
                hours_past_peak, recent_temps, market.last_metar.raw
            )
            size = calculate_size(confidence, no_price, 2000.0)
            signals.append(Signal(
                city_id=city.id,
                market_id=market.market_id,
                strategy=SignalStrategy.END_OF_DAY,
                side=SignalSide.NO,
                bracket_low=outcome.bracket_low,
                bracket_high=outcome.bracket_high,
                entry_price=no_price,
                confidence=confidence,
                suggested_size_usdc=size,
                metar_temp=max_temp_market,
                market_title=market.market_title,
                target_date=market.target_date,
                confidence_detail=conf_detail,
                reasoning=(
                    f"Max del día: {max_temp_market:.1f}°{city.unit}. "
                    f"Faltan {outcome.bracket_low - max_temp_market:.1f}° para llegar a este bracket — "
                    f"con {hours_past_peak:.1f}h pasadas desde el pico, es inalcanzable."
                ),
            ))

    return signals


def _calculate_confidence(
    hours_past_peak: float,
    recent_temps: list[float],
    metar_raw: dict,
) -> tuple[float, str]:
    """Calcula confidence para EOD. Retorna (score, detalle_legible)."""
    confidence = 0.70
    parts = ["base 70%"]

    if hours_past_peak > 2:
        confidence += 0.10
        parts.append(f"+10% ({hours_past_peak:.1f}h desde pico)")

    if recent_temps and len(recent_temps) >= 3:
        delta = max(recent_temps) - min(recent_temps)
        if delta < 0.3:
            confidence += 0.10
            parts.append("+10% (temp muy estable)")

    wx = metar_raw.get("wxString", "") or ""
    if any(code in wx for code in ["TS", "CB", "TCU"]):
        confidence -= 0.10
        parts.append("-10% (riesgo tormenta)")

    return min(max(confidence, 0.0), 1.0), " ".join(parts)


async def _get_day_max_temp(city_id: str, unit: str) -> tuple[float | None, list]:
    """Obtiene la temperatura maxima del dia de los snapshots de METAR."""
    try:
        sb = get_supabase()
        today_str = date.today().isoformat()
        resp = (
            sb.table("metar_snapshots")
            .select("temp_c, temp_f, observed_at")
            .eq("city_id", city_id)
            .gte("observed_at", f"{today_str}T00:00:00+00:00")
            .order("observed_at", desc=True)
            .execute()
        )
        snapshots = resp.data or []
        if not snapshots:
            return None, []

        max_temp_c = max(s["temp_c"] for s in snapshots)
        return max_temp_c, snapshots
    except Exception as exc:
        logger.error("Failed to get day max temp city_id=%s: %s", city_id, exc)
        return None, []


async def _get_recent_temps(city_id: str, hours: int = 2) -> list[float]:
    """Obtiene las temperaturas de los ultimos N horas de snapshots."""
    try:
        sb = get_supabase()
        cutoff = (
            datetime.now(pytz.utc) - timedelta(hours=hours)
        ).isoformat()
        resp = (
            sb.table("metar_snapshots")
            .select("temp_c")
            .eq("city_id", city_id)
            .gte("observed_at", cutoff)
            .order("observed_at", desc=True)
            .execute()
        )
        return [s["temp_c"] for s in (resp.data or [])]
    except Exception as exc:
        logger.error("Failed to get recent temps city_id=%s: %s", city_id, exc)
        return []
