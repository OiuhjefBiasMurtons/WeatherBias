from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class SignalStrategy(str, Enum):
    END_OF_DAY = "end_of_day"
    FORECAST_DIVERGENCE = "forecast_divergence"


class SignalSide(str, Enum):
    YES = "YES"
    NO = "NO"


@dataclass
class CityConfig:
    id: str
    name: str
    icao: str
    latitude: float
    longitude: float
    timezone: str
    unit: str  # 'F' o 'C'
    peak_hour_local: int
    active: bool = True
    polymarket_slug: str | None = None


@dataclass
class METARReading:
    icao: str
    temp_c: float
    temp_f: float
    observed_at: datetime
    raw: dict = field(default_factory=dict)


@dataclass
class OutcomePrice:
    token_id: str
    bracket_low: float
    bracket_high: float
    side: str  # 'YES' o 'NO'
    price: float


@dataclass
class PolyMarket:
    condition_id: str
    city_id: str
    target_date: date
    question: str
    resolution_source: str | None = None
    icao_resolution: str | None = None
    outcomes: list[OutcomePrice] = field(default_factory=list)


@dataclass
class MarketState:
    market_id: str
    city_id: str
    target_date: date
    market_title: str = ""
    outcomes: list[OutcomePrice] = field(default_factory=list)
    last_metar: METARReading | None = None
    forecast_temps: dict = field(default_factory=dict)  # date -> float (celsius)


@dataclass
class Signal:
    city_id: str
    market_id: str
    strategy: SignalStrategy
    side: SignalSide
    bracket_low: float
    bracket_high: float
    entry_price: float
    confidence: float
    suggested_size_usdc: float
    metar_temp: float | None = None
    forecast_temp: float | None = None
    market_title: str = ""
    target_date: date | None = None
    confidence_detail: str = ""
    reasoning: str = ""


@dataclass
class TradeResult:
    order_id: str
    signal_id: int
    side: str
    size_usdc: float
    entry_price: float
    shares: float
    status: str = "open"
