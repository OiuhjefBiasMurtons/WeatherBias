import pytest

from weathersniper.execution.position_sizer import calculate_size
from weathersniper.config import MAX_POSITION_USDC, MAX_POSITION_PCT_CAPITAL


def test_basic_sizing():
    """Caso base: retorna un tamano razonable."""
    size = calculate_size(confidence=0.80, entry_price=0.30, available_capital_usdc=2000)
    assert size > 0
    assert size <= MAX_POSITION_USDC


def test_never_exceeds_max_usdc():
    """Nunca supera MAX_POSITION_USDC."""
    size = calculate_size(confidence=0.99, entry_price=0.10, available_capital_usdc=100000)
    assert size <= MAX_POSITION_USDC


def test_never_exceeds_max_pct_capital():
    """Nunca supera MAX_POSITION_PCT_CAPITAL del capital."""
    capital = 1000.0
    size = calculate_size(confidence=0.95, entry_price=0.20, available_capital_usdc=capital)
    assert size <= capital * MAX_POSITION_PCT_CAPITAL


def test_minimum_size():
    """Si el tamano calculado es < $5, retorna 0 (no operar)."""
    size = calculate_size(confidence=0.71, entry_price=0.80, available_capital_usdc=100)
    assert size == 0.0 or size >= 5.0


def test_negative_edge_returns_zero():
    """Si el edge es negativo (confidence baja vs precio alto), retorna 0."""
    size = calculate_size(confidence=0.30, entry_price=0.80, available_capital_usdc=2000)
    assert size == 0.0


def test_edge_price_zero():
    """Precio 0 retorna 0."""
    assert calculate_size(0.80, 0.0, 2000) == 0.0


def test_edge_price_one():
    """Precio 1.0 retorna 0."""
    assert calculate_size(0.80, 1.0, 2000) == 0.0


def test_high_confidence_low_price():
    """Alta confianza + precio bajo = tamano grande (capped)."""
    size = calculate_size(confidence=0.95, entry_price=0.10, available_capital_usdc=5000)
    assert size > 0
    assert size <= MAX_POSITION_USDC
