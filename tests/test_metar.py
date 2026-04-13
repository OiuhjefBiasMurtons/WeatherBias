import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import httpx

from weathersniper.data.metar import get_metar


@pytest.fixture
def mock_supabase():
    with patch("weathersniper.data.metar.get_supabase") as mock:
        sb = MagicMock()
        sb.table.return_value.insert.return_value.execute.return_value = MagicMock()
        mock.return_value = sb
        yield sb


@pytest.mark.asyncio
async def test_get_metar_success(httpx_mock, mock_supabase):
    """METAR valido con datos recientes retorna METARReading."""
    now = datetime.now(timezone.utc)
    httpx_mock.add_response(
        url="https://aviationweather.gov/api/data/metar?ids=KLGA&format=json",
        json=[{
            "temp": 22.0,
            "reportTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "wxString": "",
        }],
    )

    result = await get_metar("KLGA")

    assert result is not None
    assert result.icao == "KLGA"
    assert result.temp_c == 22.0
    assert result.temp_f == pytest.approx(71.6, abs=0.1)
    assert result.observed_at.tzinfo is not None


@pytest.mark.asyncio
async def test_get_metar_old_data(httpx_mock, mock_supabase):
    """METAR con dato viejo (> 90 min) retorna None."""
    old_time = datetime.now(timezone.utc) - timedelta(minutes=100)
    httpx_mock.add_response(
        url="https://aviationweather.gov/api/data/metar?ids=KLGA&format=json",
        json=[{
            "temp": 22.0,
            "reportTime": old_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }],
    )

    result = await get_metar("KLGA")
    assert result is None


@pytest.mark.asyncio
async def test_get_metar_empty_response(httpx_mock, mock_supabase):
    """API retorna lista vacia -> None."""
    httpx_mock.add_response(
        url="https://aviationweather.gov/api/data/metar?ids=KLGA&format=json",
        json=[],
    )

    result = await get_metar("KLGA")
    assert result is None


@pytest.mark.asyncio
async def test_get_metar_api_timeout(httpx_mock, mock_supabase):
    """API timeout despues de reintentos retorna None."""
    for _ in range(3):
        httpx_mock.add_exception(httpx.ReadTimeout("timeout"))

    result = await get_metar("KLGA")
    assert result is None


@pytest.mark.asyncio
async def test_get_metar_saves_snapshot(httpx_mock, mock_supabase):
    """Verifica que se guarda el snapshot en Supabase."""
    now = datetime.now(timezone.utc)
    httpx_mock.add_response(
        url="https://aviationweather.gov/api/data/metar?ids=KLGA&format=json",
        json=[{
            "temp": 25.0,
            "reportTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }],
    )

    await get_metar("KLGA")

    mock_supabase.table.assert_called_with("metar_snapshots")
    insert_call = mock_supabase.table.return_value.insert
    assert insert_call.called
    args = insert_call.call_args[0][0]
    assert args["temp_c"] == 25.0
    assert args["icao"] == "KLGA"
