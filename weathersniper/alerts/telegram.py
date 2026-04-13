import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from weathersniper.config import settings
from weathersniper.db.client import get_supabase
from weathersniper.signals.models import Signal

logger = logging.getLogger(__name__)

_app: Application | None = None


async def init_telegram() -> Application:
    """Inicializa el bot de Telegram."""
    global _app
    _app = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .build()
    )
    _app.add_handler(CommandHandler("status", _cmd_status))
    _app.add_handler(CommandHandler("pending", _cmd_pending))
    _app.add_handler(CommandHandler("history", _cmd_history))
    _app.add_handler(CommandHandler("capital", _cmd_capital))
    await _app.initialize()
    await _app.start()
    await _app.updater.start_polling()
    logger.info("Telegram bot started")
    return _app


async def stop_telegram() -> None:
    """Detiene el bot de Telegram."""
    global _app
    if _app:
        await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()
        _app = None


def _get_market_url(city_id: str, target_date) -> str:
    """Construye la URL del mercado en Polymarket."""
    from weathersniper.config import CITIES
    if not target_date:
        return ""
    for c in CITIES:
        if c["id"] == city_id and c.get("polymarket_slug"):
            month_name = target_date.strftime("%B").lower()
            slug = (
                f"highest-temperature-in-{c['polymarket_slug']}"
                f"-on-{month_name}-{target_date.day}-{target_date.year}"
            )
            return f"https://polymarket.com/event/{slug}"
    return ""


def _format_bracket(low: float, high: float, unit: str) -> str:
    """Convierte un bracket a texto legible, manejando sentinelas."""
    if low <= -500:
        return f"{high:.0f}°{unit} o menos"
    if high >= 500:
        return f"{low:.0f}°{unit} o más"
    return f"{low:.0f}-{high:.0f}°{unit}"


def _format_signal_message(signal: Signal, signal_id: int | None = None) -> str:
    """Formatea el mensaje de señal para Telegram."""
    from weathersniper.config import CITIES

    city_name = signal.city_id
    unit = "C"
    for c in CITIES:
        if c["id"] == signal.city_id:
            city_name = c["name"]
            unit = c["unit"]
            break

    bracket_str = _format_bracket(signal.bracket_low, signal.bracket_high, unit)
    title = signal.market_title or f"Temperatura en {city_name}"
    side_str = signal.side.value if hasattr(signal.side, "value") else str(signal.side)

    # Precio del mercado = entry_price (probabilidad implícita)
    market_prob = signal.entry_price  # 0.21 = mercado dice 21%
    edge = signal.confidence - market_prob  # cuánto nos alejamos del mercado

    # Temperatura de referencia
    if signal.forecast_temp is not None:
        ref_temp = f"{signal.forecast_temp:.1f}°{unit}"
        ref_label = "GFS forecast"
    elif signal.metar_temp is not None:
        ref_temp = f"{signal.metar_temp:.1f}°{unit}"
        ref_label = "METAR max"
    else:
        ref_temp = "N/A"
        ref_label = "Temp ref"

    sid_line = f"\nID: #{signal_id}" if signal_id else ""
    url = _get_market_url(signal.city_id, signal.target_date)
    url_line = f"\n{url}" if url else ""

    msg = (
        f"{title}\n"
        f"Bracket: {bracket_str} — {side_str}\n"
        f"Precio mercado: ${market_prob:.2f} ({market_prob:.0%})\n"
        f"Confianza WS: {signal.confidence:.0%}  |  Edge: {edge:+.0%}\n"
        f"{ref_label}: {ref_temp}\n"
        f"Tamaño apuesta: ${signal.suggested_size_usdc:.0f}\n"
        f"Detalle: {signal.reasoning}"
        f"{sid_line}"
        f"{url_line}"
    )

    return msg


async def send_signal_alert(signal: Signal, signal_id: int | None = None) -> None:
    """Envia alerta de senal por Telegram."""
    if not _app:
        logger.error("Telegram alert SKIPPED: bot not initialized (_app is None)")
        return
    if not settings.TELEGRAM_CHAT_ID:
        logger.error("Telegram alert SKIPPED: TELEGRAM_CHAT_ID not set in .env")
        return

    text = _format_signal_message(signal, signal_id)
    logger.info("Sending Telegram alert to chat_id=%s signal_id=%s", settings.TELEGRAM_CHAT_ID, signal_id)

    try:
        await _app.bot.send_message(
            chat_id=settings.TELEGRAM_CHAT_ID,
            text=text,
        )
        logger.info("Telegram alert SENT OK signal_id=%s", signal_id)
    except Exception as exc:
        logger.error("Telegram alert FAILED signal_id=%s error=%s", signal_id, exc)


async def send_message(text: str) -> None:
    """Envia un mensaje simple por Telegram."""
    if not _app or not settings.TELEGRAM_CHAT_ID:
        return
    try:
        await _app.bot.send_message(
            chat_id=settings.TELEGRAM_CHAT_ID,
            text=text,
        )
    except Exception as exc:
        logger.error("Failed to send Telegram message: %s", exc)


# --- Command handlers ---

async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — PnL del dia, senales pendientes, capital."""
    try:
        sb = get_supabase()
        pending = sb.table("signals").select("id", count="exact").eq("status", "pending").execute()
        trades_today = sb.table("trades").select("pnl_usdc").eq("status", "won").execute()
        losses = sb.table("trades").select("pnl_usdc").eq("status", "lost").execute()

        pnl = sum(t["pnl_usdc"] or 0 for t in (trades_today.data or []))
        pnl += sum(t["pnl_usdc"] or 0 for t in (losses.data or []))

        text = (
            f"\U0001f4ca WeatherSniper Status\n\n"
            f"Senales pendientes: {pending.count or 0}\n"
            f"PnL total: ${pnl:.2f} USDC\n"
            f"DRY_RUN: {settings.DRY_RUN}"
        )
        await update.message.reply_text(text)
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def _cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pending — Lista de senales pendientes."""
    try:
        sb = get_supabase()
        resp = (
            sb.table("signals")
            .select("id, city_id, strategy, side, bracket_low, bracket_high, entry_price, confidence")
            .eq("status", "pending")
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )

        if not resp.data:
            await update.message.reply_text("No hay senales pendientes.")
            return

        lines = ["\U0001f4cb Senales pendientes:\n"]
        for s in resp.data:
            lines.append(
                f"#{s['id']} {s['city_id']} {s['strategy']} "
                f"{s['side']} {s['bracket_low']}-{s['bracket_high']} "
                f"@{s['entry_price']:.2f} conf={s['confidence']:.0%}"
            )
        await update.message.reply_text("\n".join(lines))
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def _cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/history — Ultimos 10 trades."""
    try:
        sb = get_supabase()
        resp = (
            sb.table("trades")
            .select("id, side, size_usdc, entry_price, status, pnl_usdc, created_at")
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )

        if not resp.data:
            await update.message.reply_text("No hay trades registrados.")
            return

        lines = ["\U0001f4c8 Ultimos trades:\n"]
        for t in resp.data:
            pnl = t["pnl_usdc"] or 0
            emoji = "\U0001f7e2" if pnl > 0 else "\U0001f534" if pnl < 0 else "\u26aa"
            lines.append(
                f"{emoji} #{t['id']} {t['side']} ${t['size_usdc']:.0f} "
                f"@{t['entry_price']:.2f} -> {t['status']} "
                f"PnL: ${pnl:.2f}"
            )
        await update.message.reply_text("\n".join(lines))
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def _cmd_capital(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/capital [amount] — Ver/actualizar capital."""
    args = context.args
    if args:
        await update.message.reply_text(
            f"Capital actualizado a ${args[0]} USDC (nota: persistir en config manualmente)"
        )
    else:
        await update.message.reply_text(
            "Uso: /capital <amount>\nEj: /capital 500"
        )
