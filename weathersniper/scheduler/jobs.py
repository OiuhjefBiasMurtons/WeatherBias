import logging
from datetime import date, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from weathersniper.alerts.telegram import send_message
from weathersniper.config import CITIES
from weathersniper.data.forecast import update_all_cities
from weathersniper.data.polymarket import fetch_temperature_markets
from weathersniper.db.client import get_supabase
from weathersniper.signals.engine import run_signal_cycle
from weathersniper.signals.models import CityConfig

logger = logging.getLogger(__name__)


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    """Registra todos los cron jobs en el scheduler."""

    # Job 1: Actualizar mercados de Polymarket — cada 5 minutos
    scheduler.add_job(
        _job_update_markets,
        "interval",
        minutes=5,
        id="update_markets",
        name="Update Polymarket markets",
    )

    # Job 2: Ciclo principal de senales — cada 30 min, offset +2 min
    scheduler.add_job(
        _job_signal_cycle,
        "cron",
        minute="2,32",
        id="signal_cycle",
        name="Signal cycle",
    )

    # Job 3: Forecast update — cada 6 horas
    scheduler.add_job(
        _job_forecast_update,
        "cron",
        hour="0,6,12,18",
        minute=30,
        id="forecast_update",
        name="Forecast update",
    )

    # Job 4: Limpieza de mercados viejos — diariamente a las 02:00 UTC
    scheduler.add_job(
        _job_cleanup_markets,
        "cron",
        hour=2,
        minute=0,
        id="cleanup_markets",
        name="Cleanup old markets",
    )

    # Job 5: Reporte diario — 09:00 UTC
    scheduler.add_job(
        _job_daily_report,
        "cron",
        hour=9,
        minute=0,
        id="daily_report",
        name="Daily report",
    )

    logger.info("All scheduler jobs registered")


async def _job_update_markets() -> None:
    """Actualiza mercados de temperatura de Polymarket."""
    try:
        markets = await fetch_temperature_markets()
        logger.info("Markets update: %d markets fetched", len(markets))
    except Exception as exc:
        logger.error("Markets update failed: %s", exc)


async def _job_signal_cycle() -> None:
    """Ejecuta el ciclo de senales y envia alertas."""
    from datetime import datetime
    logger.info("=== SIGNAL CYCLE START %s ===", datetime.utcnow().strftime("%H:%M:%S UTC"))
    try:
        new_alerts = await run_signal_cycle()
        logger.info("=== SIGNAL CYCLE END: %d new alerts sent ===", new_alerts)
    except Exception as exc:
        logger.error("=== SIGNAL CYCLE FAILED: %s ===", exc, exc_info=True)


async def _job_forecast_update() -> None:
    """Actualiza forecasts de todas las ciudades."""
    try:
        cities = [CityConfig(**c) for c in CITIES if c.get("active", True)]
        await update_all_cities(cities)
        logger.info("Forecast update complete for %d cities", len(cities))
    except Exception as exc:
        logger.error("Forecast update failed: %s", exc)


async def _job_cleanup_markets() -> None:
    """Limpieza de mercados expirados."""
    try:
        sb = get_supabase()
        today_str = date.today().isoformat()

        # Marcar como resolved los mercados con fecha < hoy
        resp = (
            sb.table("markets")
            .select("id, city_id, date")
            .eq("status", "active")
            .lt("date", today_str)
            .execute()
        )

        for mkt in resp.data or []:
            sb.table("markets").update({
                "status": "resolved",
                "resolved_at": datetime.utcnow().isoformat(),
            }).eq("id", mkt["id"]).execute()

        count = len(resp.data or [])
        if count:
            logger.info("Cleaned up %d expired markets", count)

        # Expirar senales pendientes de mercados resueltos
        sb.table("signals").update({"status": "expired"}).eq("status", "pending").lt(
            "created_at", f"{today_str}T00:00:00+00:00"
        ).execute()

    except Exception as exc:
        logger.error("Market cleanup failed: %s", exc)


async def _job_daily_report() -> None:
    """Envia reporte diario por Telegram."""
    try:
        from datetime import date, timedelta
        sb = get_supabase()

        yesterday = date.today() - timedelta(days=1)
        since = f"{yesterday.isoformat()}T00:00:00+00:00"
        until = f"{date.today().isoformat()}T00:00:00+00:00"

        # Trades del dia anterior
        trades = (
            sb.table("trades")
            .select("side, size_usdc, pnl_usdc, status")
            .gte("created_at", since)
            .lt("created_at", until)
            .execute()
        )

        all_trades = trades.data or []
        total_trades = len(all_trades)
        won = sum(1 for t in all_trades if t["status"] == "won")
        lost = sum(1 for t in all_trades if t["status"] == "lost")
        total_pnl = sum(t["pnl_usdc"] or 0 for t in all_trades)
        win_rate = won / (won + lost) * 100 if (won + lost) > 0 else 0

        report = (
            f"\U0001f4ca Reporte Diario WeatherSniper — {yesterday.strftime('%d/%m/%Y')}\n\n"
            f"Total trades: {total_trades}\n"
            f"Ganados: {won} | Perdidos: {lost}\n"
            f"Win rate: {win_rate:.0f}%\n"
            f"PnL: ${total_pnl:.2f} USDC\n"
        )

        await send_message(report)
        logger.info("Daily report sent")

    except Exception as exc:
        logger.error("Daily report failed: %s", exc)
