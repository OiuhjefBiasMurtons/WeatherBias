import pytest
from datetime import date, datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

from weathersniper.signals.end_of_day import evaluate_end_of_day
from weathersniper.signals.forecast_divergence import evaluate_forecast_divergence
from weathersniper.signals.models import (
    CityConfig,
    MarketState,
    METARReading,
    OutcomePrice,
    SignalSide,
    SignalStrategy,
)


@pytest.fixture
def nyc_city():
    return CityConfig(
        id="nyc",
        name="New York City",
        icao="KLGA",
        latitude=40.7769,
        longitude=-73.8740,
        timezone="America/New_York",
        unit="F",
        peak_hour_local=15,
    )


@pytest.fixture
def market_state_today():
    return MarketState(
        market_id="test_market_1",
        city_id="nyc",
        target_date=date.today(),
        outcomes=[
            OutcomePrice(token_id="t1", bracket_low=56, bracket_high=57.99, side="YES", price=0.25),
            OutcomePrice(token_id="t2", bracket_low=58, bracket_high=59.99, side="YES", price=0.40),
            OutcomePrice(token_id="t3", bracket_low=60, bracket_high=61.99, side="YES", price=0.20),
            OutcomePrice(token_id="t4", bracket_low=62, bracket_high=63.99, side="YES", price=0.15),
        ],
        last_metar=METARReading(
            icao="KLGA",
            temp_c=15.0,  # ~59°F
            temp_f=59.0,
            observed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        ),
        forecast_temps={},
    )


@pytest.fixture
def market_state_tomorrow():
    tomorrow = date.today() + timedelta(days=1)
    return MarketState(
        market_id="test_market_2",
        city_id="nyc",
        target_date=tomorrow,
        outcomes=[
            OutcomePrice(token_id="t1", bracket_low=56, bracket_high=57.99, side="YES", price=0.15),
            OutcomePrice(token_id="t2", bracket_low=58, bracket_high=59.99, side="YES", price=0.45),
            OutcomePrice(token_id="t3", bracket_low=60, bracket_high=61.99, side="YES", price=0.25),
            OutcomePrice(token_id="t4", bracket_low=62, bracket_high=63.99, side="YES", price=0.15),
        ],
        last_metar=None,
        forecast_temps={tomorrow: 20.0},  # 20°C = 68°F
    )


# --- End of Day tests ---

@pytest.mark.asyncio
async def test_eod_skip_before_peak(nyc_city, market_state_today):
    """No genera senal si no paso suficiente tiempo despues del peak."""
    # Mock datetime.now para que sea antes del peak
    with patch("weathersniper.signals.end_of_day.datetime") as mock_dt:
        import pytz
        tz = pytz.timezone("America/New_York")
        early_time = datetime.now(timezone.utc).replace(hour=12, minute=0)  # noon UTC ~8AM ET
        mock_dt.now.return_value = early_time.astimezone(tz)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        # Forzar hora antes del peak
        with patch("weathersniper.signals.end_of_day.pytz") as mock_pytz:
            mock_tz = MagicMock()
            # Simular que son las 14:00 hora local (antes de peak 15 + 1h)
            mock_now = MagicMock()
            mock_now.hour = 14
            mock_now.minute = 0
            mock_now.strftime.return_value = "14:00"

            import weathersniper.signals.end_of_day as eod_mod
            original_datetime = datetime

            class MockDatetime(datetime):
                @classmethod
                def now(cls, tz=None):
                    result = MagicMock()
                    result.hour = 14
                    result.minute = 0
                    result.strftime = lambda fmt: "14:00"
                    return result

            with patch.object(eod_mod, "datetime", MockDatetime):
                with patch.object(eod_mod, "pytz") as mp:
                    mp.timezone.return_value = MagicMock()
                    signals = await evaluate_end_of_day(nyc_city, market_state_today)

    assert len(signals) == 0


@pytest.mark.asyncio
async def test_eod_generates_signal_stable_temp(nyc_city, market_state_today):
    """Genera senal cuando temp es estable despues del peak."""
    with patch("weathersniper.signals.end_of_day.pytz") as mock_pytz, \
         patch("weathersniper.signals.end_of_day._get_day_max_temp") as mock_max, \
         patch("weathersniper.signals.end_of_day._get_recent_temps") as mock_recent:

        # Simular las 17:00 hora local (2h despues del peak)
        mock_tz = MagicMock()
        mock_now = MagicMock()
        mock_now.hour = 17
        mock_now.minute = 0
        mock_now.strftime.return_value = "17:00"
        mock_tz.localize = lambda dt: dt
        mock_pytz.timezone.return_value = mock_tz
        mock_pytz.utc = timezone.utc

        import weathersniper.signals.end_of_day as eod_mod
        original_now = datetime.now

        with patch("weathersniper.signals.end_of_day.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            # Max temp del dia: 59°F = 15°C
            mock_max.return_value = (15.0, [{"temp_c": 15.0}])
            # Temperaturas estables
            mock_recent.return_value = [15.0, 15.1, 14.9]

            signals = await evaluate_end_of_day(nyc_city, market_state_today)

    # Deberia generar senales NO para brackets > 59°F
    no_signals = [s for s in signals if s.side == SignalSide.NO]
    yes_signals = [s for s in signals if s.side == SignalSide.YES]
    assert len(no_signals) + len(yes_signals) > 0


@pytest.mark.asyncio
async def test_eod_skip_not_today(nyc_city):
    """No genera senal para mercados que no son de hoy."""
    market = MarketState(
        market_id="test",
        city_id="nyc",
        target_date=date.today() + timedelta(days=1),
        outcomes=[],
        last_metar=None,
        forecast_temps={},
    )
    signals = await evaluate_end_of_day(nyc_city, market)
    assert len(signals) == 0


# --- Forecast Divergence tests ---

@pytest.mark.asyncio
async def test_forecast_divergence_generates_signal(nyc_city, market_state_tomorrow):
    """Genera senal cuando forecast diverge del precio implicito."""
    # forecast: 68°F, mercado implica ~59°F (bracket 58-59.99 con mayor precio)
    # divergencia = |68 - 59| / 59 = 15.2% > 15% threshold
    signals = await evaluate_forecast_divergence(nyc_city, market_state_tomorrow)

    assert len(signals) > 0
    assert signals[0].strategy == SignalStrategy.FORECAST_DIVERGENCE


@pytest.mark.asyncio
async def test_forecast_divergence_skip_d0(nyc_city):
    """No genera senal para D+0."""
    market = MarketState(
        market_id="test",
        city_id="nyc",
        target_date=date.today(),
        outcomes=[
            OutcomePrice(token_id="t1", bracket_low=58, bracket_high=59.99, side="YES", price=0.50),
        ],
        forecast_temps={date.today(): 25.0},
    )
    signals = await evaluate_forecast_divergence(nyc_city, market)
    assert len(signals) == 0


@pytest.mark.asyncio
async def test_forecast_divergence_skip_d3(nyc_city):
    """No genera senal para D+3."""
    d3 = date.today() + timedelta(days=3)
    market = MarketState(
        market_id="test",
        city_id="nyc",
        target_date=d3,
        outcomes=[
            OutcomePrice(token_id="t1", bracket_low=58, bracket_high=59.99, side="YES", price=0.50),
        ],
        forecast_temps={d3: 25.0},
    )
    signals = await evaluate_forecast_divergence(nyc_city, market)
    assert len(signals) == 0


@pytest.mark.asyncio
async def test_forecast_divergence_skip_no_forecast(nyc_city):
    """No genera senal si no hay forecast."""
    tomorrow = date.today() + timedelta(days=1)
    market = MarketState(
        market_id="test",
        city_id="nyc",
        target_date=tomorrow,
        outcomes=[
            OutcomePrice(token_id="t1", bracket_low=58, bracket_high=59.99, side="YES", price=0.50),
        ],
        forecast_temps={},  # sin forecast
    )
    signals = await evaluate_forecast_divergence(nyc_city, market)
    assert len(signals) == 0
