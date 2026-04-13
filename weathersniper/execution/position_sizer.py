def calculate_size(confidence: float, entry_price: float, available_capital_usdc: float) -> float:
    """
    Fase 1 — tamaño fijo conservador:
      - $2 si confidence > 85%
      - $1 en cualquier otro caso
      - $0 si entry_price <= 0 o >= 1 (inválido)
    """
    if entry_price <= 0 or entry_price >= 1:
        return 0.0
    return 2.0 if confidence > 0.85 else 1.0
