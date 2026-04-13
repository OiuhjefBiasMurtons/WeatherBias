import logging
import time
from datetime import datetime

from weathersniper.config import settings
from weathersniper.db.client import get_supabase
from weathersniper.signals.models import TradeResult

logger = logging.getLogger(__name__)

PRICE_STALE_THRESHOLD = 0.03  # 3 centavos


async def place_order(
    signal_id: int,
    side: str,
    token_id: str,
    entry_price: float,
    size_usdc: float,
) -> TradeResult | None:
    """
    Ejecuta una orden en Polymarket.
    En DRY_RUN=true simula la orden.
    """
    shares = size_usdc / entry_price if entry_price > 0 else 0

    if settings.DRY_RUN:
        order_id = f"DRY_RUN_{int(time.time())}"
        logger.info(
            "DRY_RUN order signal_id=%d side=%s size=%.2f price=%.2f shares=%.1f",
            signal_id, side, size_usdc, entry_price, shares,
        )
    else:
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs, OrderType

            clob = ClobClient(
                host="https://clob.polymarket.com",
                key=settings.POLYMARKET_PRIVATE_KEY,
                chain_id=137,  # Polygon
            )

            # Verificar liquidez antes de ejecutar
            orderbook = clob.get_order_book(token_id)
            available_liq = _check_liquidity(orderbook, side, entry_price)
            if available_liq < size_usdc * 0.2:
                logger.warning(
                    "Insufficient liquidity signal_id=%d available=%.2f needed=%.2f",
                    signal_id, available_liq, size_usdc,
                )
                # Reducir tamano al 20% de la liquidez
                size_usdc = min(size_usdc, available_liq * 0.2)
                shares = size_usdc / entry_price

            # Limit order con +1 centavo para mejor fill rate
            limit_price = entry_price + 0.01 if side == "YES" else entry_price - 0.01
            limit_price = max(0.01, min(0.99, limit_price))

            order = clob.create_and_post_order(
                OrderArgs(
                    token_id=token_id,
                    price=limit_price,
                    size=shares,
                    side=side,
                ),
            )
            order_id = order.get("orderID", f"UNKNOWN_{int(time.time())}")

            logger.info(
                "Order placed signal_id=%d order_id=%s side=%s size=%.2f",
                signal_id, order_id, side, size_usdc,
            )
        except Exception as exc:
            logger.error("Order failed signal_id=%d: %s", signal_id, exc)
            return None

    result = TradeResult(
        order_id=order_id,
        signal_id=signal_id,
        side=side,
        size_usdc=size_usdc,
        entry_price=entry_price,
        shares=shares,
    )

    # Persistir trade
    try:
        sb = get_supabase()
        sb.table("trades").insert({
            "signal_id": signal_id,
            "order_id": order_id,
            "side": side,
            "size_usdc": size_usdc,
            "entry_price": entry_price,
            "shares": shares,
            "status": "open",
        }).execute()
    except Exception as exc:
        logger.error("Failed to persist trade signal_id=%d: %s", signal_id, exc)

    return result


async def execute_approved_signal(signal_id: int) -> TradeResult | None:
    """
    Ejecuta un trade para una senal aprobada.
    Valida que el mercado sigue activo y el precio no se movio mucho.
    """
    sb = get_supabase()

    # Obtener la senal
    resp = sb.table("signals").select("*").eq("id", signal_id).execute()
    if not resp.data:
        logger.error("Signal not found id=%d", signal_id)
        return None

    signal = resp.data[0]

    # Obtener el token_id del outcome correspondiente
    outcomes_resp = (
        sb.table("outcomes")
        .select("token_id, last_price")
        .eq("market_id", signal["market_id"])
        .gte("bracket_low", signal["bracket_low"] - 0.01)
        .lte("bracket_high", signal["bracket_high"] + 0.01)
        .execute()
    )

    if not outcomes_resp.data:
        logger.error("No matching outcome for signal %d", signal_id)
        return None

    outcome = outcomes_resp.data[0]
    current_price = outcome.get("last_price", 0) or signal["entry_price"]

    # Validar que el precio no se movio mas de 3 centavos
    if abs(current_price - signal["entry_price"]) > PRICE_STALE_THRESHOLD:
        logger.warning(
            "Price moved too much signal_id=%d entry=%.2f current=%.2f",
            signal_id, signal["entry_price"], current_price,
        )
        sb.table("signals").update({"status": "expired"}).eq("id", signal_id).execute()

        from weathersniper.alerts.telegram import send_message
        await send_message(
            f"\u26a0\ufe0f Senal #{signal_id} expirada: precio se movio de "
            f"${signal['entry_price']:.2f} a ${current_price:.2f}"
        )
        return None

    result = await place_order(
        signal_id=signal_id,
        side=signal["side"],
        token_id=outcome["token_id"],
        entry_price=current_price,
        size_usdc=signal["suggested_size"],
    )

    if result:
        sb.table("signals").update({"status": "executed"}).eq("id", signal_id).execute()

        from weathersniper.alerts.telegram import send_message
        dry_tag = " [DRY RUN]" if settings.DRY_RUN else ""
        await send_message(
            f"\u2705 Trade ejecutado{dry_tag}\n"
            f"Senal #{signal_id} | {result.side} | "
            f"${result.size_usdc:.0f} @ {result.entry_price:.2f}\n"
            f"Order ID: {result.order_id}"
        )

    return result


def _check_liquidity(orderbook, side: str, price: float) -> float:
    """Calcula liquidez disponible en el orderbook para el lado y precio dado."""
    try:
        if side == "YES":
            asks = orderbook.get("asks", [])
            return sum(
                float(a.get("size", 0))
                for a in asks
                if float(a.get("price", 0)) <= price + 0.05
            )
        else:
            bids = orderbook.get("bids", [])
            return sum(
                float(b.get("size", 0))
                for b in bids
                if float(b.get("price", 0)) >= price - 0.05
            )
    except Exception:
        return 0.0
