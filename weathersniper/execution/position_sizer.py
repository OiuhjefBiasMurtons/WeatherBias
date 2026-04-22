MIN_ORDER_SIZE = 5.0
MAX_POSITION_USDC = 100.0
_FEE_RATE = 0.05  # taker fee para weather markets en Polymarket


def _taker_fee(price: float) -> float:
    """Fee parabólica: 0% en extremos, ~1.25% en p=0.50."""
    return _FEE_RATE * price * (1 - price)


def calculate_size(confidence: float, entry_price: float, available_capital_usdc: float) -> float:
    """
    Half-Kelly sizing con fee real.

    edge = confidence - entry_price - taker_fee(entry_price)
    kelly = edge / (1 - entry_price)   [fracción óptima del capital]
    size = capital * kelly / 2         [half-Kelly]

    Retorna 0 si edge <= 0 (sin ventaja).
    Clampea entre MIN_ORDER_SIZE y MAX_POSITION_USDC.
    """
    if entry_price <= 0 or entry_price >= 1:
        return 0.0

    fee = _taker_fee(entry_price)
    edge = confidence - entry_price - fee
    if edge <= 0:
        return 0.0

    kelly = edge / (1 - entry_price)
    half_kelly = kelly / 2
    size = available_capital_usdc * half_kelly
    return round(max(MIN_ORDER_SIZE, min(size, MAX_POSITION_USDC)), 2)
