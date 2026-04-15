import logging
import time
from datetime import datetime, timezone

import httpx

from weathersniper.db.client import get_supabase
from weathersniper.signals.models import METARReading

logger = logging.getLogger(__name__)

METAR_API_URL = "https://aviationweather.gov/api/data/metar"
METAR_TIMEOUT = 5.0
METAR_MAX_AGE_MINUTES = 90
METAR_RETRIES = 3


async def get_metar(icao: str) -> METARReading | None:
    """
    Obtiene la lectura METAR mas reciente para el ICAO dado.
    Guarda snapshot en Supabase (tabla metar_snapshots).
    Retorna None si la API falla o no hay datos recientes (> 90 min).
    """
    for attempt in range(1, METAR_RETRIES + 1):
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=METAR_TIMEOUT) as client:
                resp = await client.get(
                    METAR_API_URL,
                    params={"ids": icao, "format": "json"},
                )
            elapsed = time.monotonic() - start
            logger.debug(
                "METAR request icao=%s attempt=%d status=%d latency=%.2fs",
                icao, attempt, resp.status_code, elapsed,
            )
            resp.raise_for_status()
            break
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            logger.warning(
                "METAR request failed icao=%s attempt=%d: %s", icao, attempt, exc,
            )
            if attempt == METAR_RETRIES:
                logger.error("METAR request exhausted retries icao=%s", icao)
                return None
            # Backoff exponencial: 1s, 2s, 4s
            import asyncio
            await asyncio.sleep(2 ** (attempt - 1))

    data = resp.json()
    if not data:
        logger.warning("METAR returned empty data icao=%s", icao)
        return None

    obs = data[0] if isinstance(data, list) else data

    # Parsear temperatura y timestamp
    try:
        temp_c = float(obs["temp"])
        observed_str = obs.get("reportTime") or obs.get("obsTime", "")
        observed_at = datetime.fromisoformat(observed_str.replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError) as exc:
        logger.error("METAR parse error icao=%s: %s | raw=%s", icao, exc, obs)
        return None

    # Validar edad del dato
    age_minutes = (datetime.now(timezone.utc) - observed_at).total_seconds() / 60
    if age_minutes > METAR_MAX_AGE_MINUTES:
        logger.warning(
            "METAR data too old icao=%s age=%.1f min (max %d)",
            icao, age_minutes, METAR_MAX_AGE_MINUTES,
        )
        return None

    temp_f = temp_c * 9 / 5 + 32

    reading = METARReading(
        icao=icao,
        temp_c=temp_c,
        temp_f=round(temp_f, 1),
        observed_at=observed_at,
        raw=obs,
    )

    # Guardar snapshot en Supabase
    # Intenta upsert con constraint; fallback a insert si la constraint no existe aún.
    try:
        sb = get_supabase()
        row = {
            "city_id": _icao_to_city_id(icao),
            "icao": icao,
            "temp_c": reading.temp_c,
            "temp_f": reading.temp_f,
            "observed_at": reading.observed_at.isoformat(),
            "raw": reading.raw,
        }
        try:
            sb.table("metar_snapshots").upsert(row, on_conflict="city_id,observed_at").execute()
        except Exception as upsert_exc:
            if "42P10" in str(upsert_exc) or "no unique or exclusion constraint" in str(upsert_exc):
                # Constraint no existe en el live DB todavía — usar insert ignorando duplicados
                try:
                    sb.table("metar_snapshots").insert(row).execute()
                except Exception as insert_exc:
                    if "23505" not in str(insert_exc) and "duplicate" not in str(insert_exc).lower():
                        logger.error("Failed to save METAR snapshot icao=%s: %s", icao, insert_exc)
                    else:
                        logger.debug("METAR snapshot already exists icao=%s", icao)
            else:
                raise upsert_exc
        logger.debug("METAR snapshot saved icao=%s temp_c=%.1f", icao, temp_c)
    except Exception as exc:
        logger.error("Failed to save METAR snapshot icao=%s: %s", icao, exc)

    return reading


def _icao_to_city_id(icao: str) -> str:
    """Mapea ICAO a city_id usando la config."""
    from weathersniper.config import CITIES
    for city in CITIES:
        if city["icao"] == icao:
            return city["id"]
    return icao.lower()
