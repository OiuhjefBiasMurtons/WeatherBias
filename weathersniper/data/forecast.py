import logging
import time
from datetime import date, datetime, timezone

import httpx

from weathersniper.signals.models import CityConfig

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_TIMEOUT = 10.0
CACHE_TTL_SECONDS = 6 * 3600  # 6 horas

# Cache en memoria: {city_id: (timestamp, data)}
_forecast_cache: dict[str, tuple[float, dict[date, float]]] = {}


async def get_forecast(city: CityConfig) -> dict[date, float] | None:
    """
    Retorna dict de {fecha: temp_max_celsius} para los proximos 4 dias.
    Las temperaturas siempre en Celsius internamente.
    Cache de 6 horas por ciudad.
    """
    now = time.time()
    cached = _forecast_cache.get(city.id)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        logger.debug("Forecast cache hit city_id=%s", city.id)
        return cached[1]

    try:
        start = time.monotonic()
        async with httpx.AsyncClient(timeout=FORECAST_TIMEOUT) as client:
            resp = await client.get(
                OPEN_METEO_URL,
                params={
                    "latitude": city.latitude,
                    "longitude": city.longitude,
                    "daily": "temperature_2m_max",
                    "forecast_days": 4,
                    "timezone": "auto",
                    "temperature_unit": "celsius",
                },
            )
        elapsed = time.monotonic() - start
        logger.debug(
            "Forecast request city_id=%s status=%d latency=%.2fs",
            city.id, resp.status_code, elapsed,
        )
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.error("Forecast request failed city_id=%s: %s", city.id, exc)
        return None

    data = resp.json()
    daily = data.get("daily", {})
    dates_raw = daily.get("time", [])
    temps = daily.get("temperature_2m_max", [])

    if not dates_raw or not temps or len(dates_raw) != len(temps):
        logger.warning("Forecast unexpected format city_id=%s: %s", city.id, daily)
        return None

    result: dict[date, float] = {}
    for date_str, temp in zip(dates_raw, temps):
        if temp is not None:
            result[date.fromisoformat(date_str)] = float(temp)

    _forecast_cache[city.id] = (now, result)
    logger.info("Forecast updated city_id=%s days=%d", city.id, len(result))
    return result


async def update_all_cities(cities: list[CityConfig]) -> None:
    """Actualiza el forecast de todas las ciudades activas."""
    for city in cities:
        if city.active:
            await get_forecast(city)
