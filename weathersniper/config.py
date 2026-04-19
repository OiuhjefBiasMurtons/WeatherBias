import logging
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    # Supabase
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "")

    # Polymarket
    POLYMARKET_PRIVATE_KEY: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    POLYMARKET_API_KEY: str = os.getenv("POLYMARKET_API_KEY", "")
    POLYMARKET_PROXY_ADDRESS: str = os.getenv("POLYMARKET_PROXY_ADDRESS", "")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Modo
    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")


settings = Settings()

# Configurar logging global
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# --- Ciudades iniciales ---

CITIES = [
    {
        "id": "nyc",
        "name": "New York City",
        "icao": "KLGA",
        "latitude": 40.7769,
        "longitude": -73.8740,
        "timezone": "America/New_York",
        "unit": "F",
        "peak_hour_local": 15,
        "active": True,
        "polymarket_slug": "nyc",
    },
    {
        "id": "london",
        "name": "London",
        "icao": "EGLC",
        "latitude": 51.5048,
        "longitude": 0.0495,
        "timezone": "Europe/London",
        "unit": "C",
        "peak_hour_local": 15,
        "active": True,
        "polymarket_slug": "london",
    },
    {
        "id": "madrid",
        "name": "Madrid",
        "icao": "LEMD",
        "latitude": 40.4936,
        "longitude": -3.5668,
        "timezone": "Europe/Madrid",
        "unit": "C",
        "peak_hour_local": 16,
        "active": True,
        "polymarket_slug": "madrid",
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
        "active": True,
        "polymarket_slug": "los-angeles",
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
        "active": True,
        "polymarket_slug": "hong-kong",
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
        "active": True,
        "polymarket_slug": "shanghai",
    },
    {
        "id": "seoul",
        "name": "Seoul",
        "icao": "RKSS",
        "latitude": 37.5585,
        "longitude": 126.7942,
        "timezone": "Asia/Seoul",
        "unit": "C",
        "peak_hour_local": 15,
        "active": True,
        "polymarket_slug": "seoul",
    },
    {
        "id": "tokyo",
        "name": "Tokyo",
        "icao": "RJTT",
        "latitude": 35.5494,
        "longitude": 139.7798,
        "timezone": "Asia/Tokyo",
        "unit": "C",
        "peak_hour_local": 15,
        "active": True,
        "polymarket_slug": "tokyo",
    },
    {
        "id": "wellington",
        "name": "Wellington",
        "icao": "NZWN",
        "latitude": -41.3272,
        "longitude": 174.8052,
        "timezone": "Pacific/Auckland",
        "unit": "C",
        "peak_hour_local": 15,
        "active": True,
        "polymarket_slug": "wellington",
    },
]

# --- Thresholds de senal ---

SIGNAL_MIN_CONFIDENCE = 0.70
EOD_MIN_HOURS_PAST_PEAK = 1.0
EOD_MAX_TEMP_DELTA_LAST_2H = 0.5  # grados C
FORECAST_MIN_DIVERGENCE_PCT = 0.08
MAX_POSITION_USDC = 100.0
MAX_POSITION_PCT_CAPITAL = 0.05
