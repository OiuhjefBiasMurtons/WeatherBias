import logging
from datetime import date, timedelta

from weathersniper.config import FORECAST_MIN_DIVERGENCE_PCT
from weathersniper.execution.position_sizer import calculate_size
from weathersniper.signals.models import (
    CityConfig,
    MarketState,
    OutcomePrice,
    Signal,
    SignalSide,
    SignalStrategy,
)

logger = logging.getLogger(__name__)

# Valores centinela usados en _parse_bracket para "or below" / "or higher"
_SENTINEL_LOW = -500
_SENTINEL_HIGH = 500


def _is_real_bracket(o: OutcomePrice) -> bool:
    """Descarta brackets centinela (or below / or higher) — sus midpoints son inútiles."""
    return o.bracket_low > _SENTINEL_LOW and o.bracket_high < _SENTINEL_HIGH


async def evaluate_forecast_divergence(
    city: CityConfig, market: MarketState
) -> list[Signal]:
    """
    Genera señales cuando el forecast GFS difiere del precio implícito del mercado.

    Solo para D+1 y D+2.
    Solo usa brackets "reales" (no centinelas) para el cálculo de consenso.
    """
    today = date.today()
    days_ahead = (market.target_date - today).days

    if days_ahead < 1 or days_ahead > 2:
        return []

    forecast_temp_c = market.forecast_temps.get(market.target_date)
    if forecast_temp_c is None:
        logger.debug(
            "Forecast divergence skip city_id=%s: no forecast for %s",
            city.id, market.target_date,
        )
        return []

    # Convertir forecast a la unidad del mercado
    if city.unit == "F":
        forecast_temp = forecast_temp_c * 9 / 5 + 32
    else:
        forecast_temp = forecast_temp_c

    # Usar solo brackets reales para el consenso (excluir "or below" / "or higher")
    real_outcomes = [o for o in market.outcomes if _is_real_bracket(o)]
    if not real_outcomes:
        logger.debug(
            "Forecast divergence skip city_id=%s: no real brackets (only sentinels)",
            city.id,
        )
        return []

    # Bracket con mayor precio entre los reales = consenso del mercado
    consensus = max(real_outcomes, key=lambda o: o.price)
    implied_market_temp = (consensus.bracket_low + consensus.bracket_high) / 2

    divergence = abs(forecast_temp - implied_market_temp) / abs(implied_market_temp)

    logger.info(
        "Forecast divergence city_id=%s date=%s forecast=%.1f implied=%.1f divergence=%.0f%%",
        city.id, market.target_date, forecast_temp, implied_market_temp, divergence * 100,
    )

    if divergence < FORECAST_MIN_DIVERGENCE_PCT:
        logger.debug(
            "Forecast divergence skip city_id=%s: %.0f%% < min %.0f%%",
            city.id, divergence * 100, FORECAST_MIN_DIVERGENCE_PCT * 100,
        )
        return []

    # Escalar confianza por magnitud de divergencia:
    # 15-20% → 0.70 | 20-30% → 0.75 | 30-40% → 0.80 | >40% → 0.85
    if divergence >= 0.40:
        confidence = 0.85
        confidence_detail = f"85% (divergencia {divergence:.0%} ≥40%)"
    elif divergence >= 0.30:
        confidence = 0.80
        confidence_detail = f"80% (divergencia {divergence:.0%} ≥30%)"
    elif divergence >= 0.20:
        confidence = 0.75
        confidence_detail = f"75% (divergencia {divergence:.0%} ≥20%)"
    else:
        confidence = 0.70
        confidence_detail = f"70% base (divergencia {divergence:.0%})"

    signals: list[Signal] = []

    if forecast_temp > implied_market_temp:
        # Forecast más alto que el consenso → buscar bracket real que contenga el forecast
        target = _find_real_bracket_for_temp(real_outcomes, forecast_temp)
        if target and target.price < 0.85:
            size = calculate_size(confidence, target.price, 2000.0)
            signals.append(Signal(
                city_id=city.id,
                market_id=market.market_id,
                strategy=SignalStrategy.FORECAST_DIVERGENCE,
                side=SignalSide.YES,
                bracket_low=target.bracket_low,
                bracket_high=target.bracket_high,
                entry_price=target.price,
                confidence=confidence,
                suggested_size_usdc=size,
                forecast_temp=forecast_temp,
                market_title=market.market_title,
                target_date=market.target_date,
                confidence_detail=confidence_detail,
                reasoning=(
                    f"GFS dice {forecast_temp:.1f}°{city.unit} para {market.target_date}. "
                    f"Mercado paga más al bracket {consensus.bracket_low:.0f}-{consensus.bracket_high:.0f}°{city.unit} "
                    f"(implica {implied_market_temp:.1f}°{city.unit}). "
                    f"Divergencia: {divergence:.0%} — forecast está {forecast_temp - implied_market_temp:.1f}° por encima."
                ),
            ))
    else:
        # Forecast más bajo → el bracket de consenso está sobrevaluado, comprar NO
        no_price = round(1 - consensus.price, 3)
        if no_price > 0.05:
            size = calculate_size(confidence, no_price, 2000.0)
            signals.append(Signal(
                city_id=city.id,
                market_id=market.market_id,
                strategy=SignalStrategy.FORECAST_DIVERGENCE,
                side=SignalSide.NO,
                bracket_low=consensus.bracket_low,
                bracket_high=consensus.bracket_high,
                entry_price=no_price,
                confidence=confidence,
                suggested_size_usdc=size,
                forecast_temp=forecast_temp,
                market_title=market.market_title,
                target_date=market.target_date,
                confidence_detail=confidence_detail,
                reasoning=(
                    f"GFS dice {forecast_temp:.1f}°{city.unit} para {market.target_date}. "
                    f"Mercado paga más al bracket {consensus.bracket_low:.0f}-{consensus.bracket_high:.0f}°{city.unit} "
                    f"(implica {implied_market_temp:.1f}°{city.unit}). "
                    f"Forecast está {implied_market_temp - forecast_temp:.1f}° por debajo — ese bracket no se alcanzará."
                ),
            ))

    return signals


def _find_real_bracket_for_temp(outcomes: list[OutcomePrice], temp: float) -> OutcomePrice | None:
    """Encuentra el bracket real (no centinela) que contiene la temperatura."""
    # Primero intentar coincidencia exacta
    for o in outcomes:
        if o.bracket_low <= temp <= o.bracket_high:
            return o
    # Si no hay coincidencia exacta, el más cercano entre reales
    if outcomes:
        return min(outcomes, key=lambda o: abs((o.bracket_low + o.bracket_high) / 2 - temp))
    return None
