import json
import logging
import re
from datetime import date, timedelta

import httpx

from weathersniper.config import CITIES
from weathersniper.db.client import get_supabase
from weathersniper.signals.models import OutcomePrice, PolyMarket

logger = logging.getLogger(__name__)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_PRICES_URL = "https://clob.polymarket.com/prices-history"
POLL_TIMEOUT = 10.0


def _parse_bracket(group_title: str) -> tuple[float, float] | None:
    """Parsea el groupItemTitle de un bracket de Polymarket.

    Formatos reales de la API:
      '59°F or below'   → (-999, 59)
      '60-61°F'         → (60, 61)
      '78°F or higher'  → (78, 999)
      Igual con °C.
    """
    # "or below" / "or lower": "59°F or below"
    below = re.search(r"(\d+\.?\d*).*or\s+(?:below|lower)", group_title, re.IGNORECASE)
    if below:
        return -999.0, float(below.group(1))

    # "or higher" / "or above": "78°F or higher"
    above = re.search(r"(\d+\.?\d*).*or\s+(?:higher|above)", group_title, re.IGNORECASE)
    if above:
        return float(above.group(1)), 999.0

    # Rango: "60-61°F", "20-22°C", "58 - 59.99°F"
    range_match = re.search(r"(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)", group_title)
    if range_match:
        return float(range_match.group(1)), float(range_match.group(2))

    return None


def _build_event_slug(city_polymarket_slug: str, target: date) -> str:
    """Construye el slug del evento de temperatura de Polymarket."""
    month_name = target.strftime("%B").lower()
    return f"highest-temperature-in-{city_polymarket_slug}-on-{month_name}-{target.day}-{target.year}"


def _parse_event(event: dict, city_id: str, target_date: date) -> PolyMarket | None:
    """Convierte un evento de Polymarket en un objeto PolyMarket."""
    condition_id = event.get("negRiskMarketID") or str(event.get("id", ""))
    if not condition_id:
        return None

    resolution_source = event.get("resolutionSource", "")

    # Extraer ICAO de la URL de resolución (ej: /KLGA en wunderground)
    icao_match = re.search(r"/([A-Z]{4})(?:\b|/|\.)", resolution_source)
    icao_resolution = icao_match.group(1) if icao_match else None

    outcomes: list[OutcomePrice] = []
    for bracket_mkt in event.get("markets", []):
        if not bracket_mkt.get("active") or bracket_mkt.get("closed"):
            continue

        group_title = bracket_mkt.get("groupItemTitle", "")
        bracket = _parse_bracket(group_title)
        if not bracket:
            logger.debug("Could not parse bracket title: %r", group_title)
            continue

        # clobTokenIds es un string JSON: '["token_yes", "token_no"]'
        try:
            token_ids = json.loads(bracket_mkt.get("clobTokenIds", "[]"))
        except (json.JSONDecodeError, TypeError):
            token_ids = []

        try:
            prices = json.loads(bracket_mkt.get("outcomePrices", "[]"))
        except (json.JSONDecodeError, TypeError):
            prices = []

        yes_token = token_ids[0] if token_ids else ""
        yes_price = float(prices[0]) if prices else 0.0

        outcomes.append(OutcomePrice(
            token_id=yes_token,
            bracket_low=bracket[0],
            bracket_high=bracket[1],
            side="YES",
            price=yes_price,
        ))

    if not outcomes:
        return None

    return PolyMarket(
        condition_id=condition_id,
        city_id=city_id,
        target_date=target_date,
        question=event.get("title", ""),
        resolution_source=resolution_source,
        icao_resolution=icao_resolution,
        outcomes=outcomes,
    )


async def fetch_temperature_markets() -> list[PolyMarket]:
    """
    Obtiene todos los mercados activos de temperatura de Polymarket.

    Construye slugs para D+0..D+4 para cada ciudad activa con polymarket_slug
    y los busca via GET /events?slug=... en la Gamma API.
    """
    today = date.today()
    slugs: list[tuple[str, date, str]] = []  # (city_id, target_date, slug)

    for city in CITIES:
        if not city.get("active") or not city.get("polymarket_slug"):
            continue
        city_slug = city["polymarket_slug"]
        for delta in range(5):  # D+0 a D+4
            target = today + timedelta(days=delta)
            slug = _build_event_slug(city_slug, target)
            slugs.append((city["id"], target, slug))

    markets: list[PolyMarket] = []
    async with httpx.AsyncClient(timeout=POLL_TIMEOUT) as client:
        for city_id, target_date, slug in slugs:
            try:
                resp = await client.get(GAMMA_EVENTS_URL, params={"slug": slug})
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, list) or not data:
                    continue
                market = _parse_event(data[0], city_id, target_date)
                if market:
                    markets.append(market)
                    logger.debug("Fetched market slug=%s outcomes=%d", slug, len(market.outcomes))
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                logger.debug("Failed to fetch event slug=%s: %s", slug, exc)
            except Exception as exc:
                logger.error("Unexpected error fetching slug=%s: %s", slug, exc)

    logger.info("Fetched %d temperature markets from Polymarket", len(markets))
    await _persist_markets(markets)
    return markets


async def get_market_prices(condition_id: str) -> list[OutcomePrice]:
    """Obtiene precios actuales de cada outcome (bracket) del mercado."""
    try:
        async with httpx.AsyncClient(timeout=POLL_TIMEOUT) as client:
            resp = await client.get(
                CLOB_PRICES_URL,
                params={
                    "market": condition_id,
                    "interval": "1m",
                    "fidelity": "1",
                },
            )
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.error("Polymarket prices fetch failed market=%s: %s", condition_id, exc)
        return []

    data = resp.json()
    prices: list[OutcomePrice] = []
    if isinstance(data, dict):
        history = data.get("history", [])
        if history:
            latest = history[-1]
            for token_id, price_val in latest.items():
                if token_id == "t":
                    continue
                prices.append(OutcomePrice(
                    token_id=token_id,
                    bracket_low=0,
                    bracket_high=0,
                    side="YES",
                    price=float(price_val),
                ))

    return prices


async def _persist_markets(markets: list[PolyMarket]) -> None:
    """Persiste mercados y outcomes en Supabase con bulk upsert."""
    if not markets:
        return
    try:
        sb = get_supabase()

        markets_rows = [
            {
                "id": mkt.condition_id,
                "city_id": mkt.city_id,
                "date": mkt.target_date.isoformat(),
                "question": mkt.question,
                "resolution_source": mkt.resolution_source,
                "icao_resolution": mkt.icao_resolution,
                "status": "active",
            }
            for mkt in markets
        ]
        sb.table("markets").upsert(markets_rows, on_conflict="id").execute()

        outcomes_rows = [
            {
                "market_id": mkt.condition_id,
                "token_id": outcome.token_id,
                "bracket_low": outcome.bracket_low,
                "bracket_high": outcome.bracket_high,
                "side": outcome.side,
                "last_price": outcome.price,
            }
            for mkt in markets
            for outcome in mkt.outcomes
        ]
        sb.table("outcomes").upsert(outcomes_rows, on_conflict="market_id,token_id").execute()

    except Exception as exc:
        logger.error("Failed to persist markets: %s", exc)
