import logging
from contextlib import asynccontextmanager
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException

from weathersniper.alerts.telegram import init_telegram, stop_telegram
from weathersniper.config import CITIES, settings
from weathersniper.db.client import get_supabase
from weathersniper.scheduler.jobs import register_jobs

logger = logging.getLogger(__name__)


_CITY_DB_FIELDS = {"id", "name", "icao", "latitude", "longitude", "timezone", "unit", "peak_hour_local", "active"}


async def init_cities() -> None:
    """Inserta o actualiza ciudades iniciales en Supabase."""
    sb = get_supabase()
    for city in CITIES:
        row = {k: v for k, v in city.items() if k in _CITY_DB_FIELDS}
        sb.table("cities").upsert(row, on_conflict="id").execute()
    logger.info("Cities initialized: %d", len(CITIES))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("WeatherSniper starting up — DRY_RUN=%s", settings.DRY_RUN)

    # Verificar conexion Supabase
    try:
        sb = get_supabase()
        sb.table("cities").select("id").limit(1).execute()
        logger.info("Supabase connection OK")
    except Exception as exc:
        logger.error("Supabase connection failed: %s", exc)

    await init_cities()

    # Iniciar Telegram bot
    if settings.TELEGRAM_BOT_TOKEN:
        try:
            await init_telegram()
        except Exception as exc:
            logger.error("Telegram init failed: %s", exc)

    # Iniciar scheduler
    scheduler = AsyncIOScheduler(job_defaults={"misfire_grace_time": 120})
    register_jobs(scheduler)
    scheduler.start()
    logger.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    # Correr ciclo de senales inmediatamente al arrancar (no esperar al :02/:32)
    from weathersniper.scheduler.jobs import _job_signal_cycle
    import asyncio
    asyncio.ensure_future(_job_signal_cycle())

    yield

    # Shutdown
    scheduler.shutdown()
    await stop_telegram()
    logger.info("WeatherSniper shut down")


app = FastAPI(title="WeatherSniper", lifespan=lifespan)


@app.get("/health")
async def health():
    """Status del sistema."""
    try:
        sb = get_supabase()
        sb.table("cities").select("id").limit(1).execute()
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "status": "ok" if db_ok else "degraded",
        "dry_run": settings.DRY_RUN,
        "db_connected": db_ok,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/signals/pending")
async def get_pending_signals():
    """Senales pendientes de aprobacion."""
    sb = get_supabase()
    resp = (
        sb.table("signals")
        .select("*")
        .eq("status", "pending")
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    return {"signals": resp.data or []}


@app.get("/stats")
async def get_stats():
    """PnL acumulado, win rate, metricas."""
    sb = get_supabase()

    trades = sb.table("trades").select("pnl_usdc, status").execute()
    all_trades = trades.data or []

    won = sum(1 for t in all_trades if t["status"] == "won")
    lost = sum(1 for t in all_trades if t["status"] == "lost")
    total_pnl = sum(t["pnl_usdc"] or 0 for t in all_trades)
    win_rate = won / (won + lost) if (won + lost) > 0 else 0

    signals = sb.table("signals").select("status", count="exact").execute()

    return {
        "total_trades": len(all_trades),
        "won": won,
        "lost": lost,
        "win_rate": round(win_rate, 4),
        "total_pnl_usdc": round(total_pnl, 2),
        "total_signals": signals.count or 0,
    }


@app.post("/signals/{signal_id}/approve")
async def approve_signal(signal_id: int):
    """Aprueba una senal manualmente."""
    sb = get_supabase()
    resp = sb.table("signals").select("status").eq("id", signal_id).execute()
    if not resp.data:
        raise HTTPException(status_code=404, detail="Signal not found")

    if resp.data[0]["status"] != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Signal is {resp.data[0]['status']}, not pending",
        )

    sb.table("signals").update({
        "status": "approved",
        "approved_at": datetime.utcnow().isoformat(),
    }).eq("id", signal_id).execute()

    from weathersniper.execution.polymarket_exec import execute_approved_signal
    result = await execute_approved_signal(signal_id)

    return {"status": "approved", "trade": result.__dict__ if result else None}


@app.post("/test-telegram")
async def test_telegram():
    """Envia un mensaje de prueba por Telegram para verificar la configuracion."""
    from weathersniper.alerts.telegram import send_message, _app
    from weathersniper.config import settings

    if not settings.TELEGRAM_BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}
    if not settings.TELEGRAM_CHAT_ID:
        return {"ok": False, "error": "TELEGRAM_CHAT_ID not set"}
    if not _app:
        return {"ok": False, "error": "Bot not initialized"}

    try:
        from datetime import timezone
        now = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
        await send_message(f"WeatherSniper test OK — {now}")
        return {"ok": True, "chat_id": settings.TELEGRAM_CHAT_ID}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/signals/{signal_id}/reject")
async def reject_signal(signal_id: int):
    """Rechaza una senal."""
    sb = get_supabase()
    resp = sb.table("signals").select("status").eq("id", signal_id).execute()
    if not resp.data:
        raise HTTPException(status_code=404, detail="Signal not found")

    sb.table("signals").update({"status": "rejected"}).eq("id", signal_id).execute()
    return {"status": "rejected"}


@app.post("/debug/cycle")
async def debug_cycle():
    """
    Ejecuta el ciclo de señales manualmente y devuelve diagnóstico completo.
    Util para verificar que el flujo funciona sin esperar al cron.
    """
    from weathersniper.config import CITIES, SIGNAL_MIN_CONFIDENCE
    from weathersniper.data.forecast import get_forecast
    from weathersniper.data.metar import get_metar
    from weathersniper.data.polymarket import fetch_temperature_markets
    from weathersniper.signals.models import CityConfig
    from weathersniper.alerts.telegram import _app as tg_app

    result: dict = {
        "timestamp": datetime.utcnow().isoformat(),
        "telegram_bot_initialized": tg_app is not None,
        "markets_total": 0,
        "cities": {},
        "signals_generated": 0,
        "signals_sent": 0,
    }

    # 1. Polymarket markets
    all_markets = await fetch_temperature_markets()
    result["markets_total"] = len(all_markets)
    markets_by_city: dict = {}
    for mkt in all_markets:
        markets_by_city.setdefault(mkt.city_id, []).append(mkt)

    # 2. Por ciudad: METAR + forecast + mercados disponibles
    for city_cfg in CITIES:
        if not city_cfg.get("active", True):
            continue
        city_id = city_cfg["id"]
        city = CityConfig(**city_cfg)

        metar = await get_metar(city.icao)
        forecast = await get_forecast(city) or {}

        city_markets = markets_by_city.get(city_id, [])
        result["cities"][city_id] = {
            "metar_ok": metar is not None,
            "metar_temp_c": metar.temp_c if metar else None,
            "metar_observed": metar.observed_at.isoformat() if metar else None,
            "forecast_dates": len(forecast),
            "markets_found": len(city_markets),
        }

    # 3. Correr ciclo real y contar
    from weathersniper.scheduler.jobs import _job_signal_cycle
    # Capturar cuántas señales se envían chequeando conteo antes/después
    from weathersniper.db.client import get_supabase
    sb = get_supabase()
    before = sb.table("signals").select("id", count="exact").execute().count or 0
    new_alerts = await _job_signal_cycle()
    after = sb.table("signals").select("id", count="exact").execute().count or 0

    result["signals_generated"] = after - before
    result["signals_sent"] = new_alerts

    return result