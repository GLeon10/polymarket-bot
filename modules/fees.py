"""
Modelo canônico de fees da Polymarket.

O bot usava `rate * p * (1-p)`, que subestima o custo real em até 2x na
região p ≈ 0.50 — exatamente onde a arbitragem YES+NO opera. A taxa taker
publicada pela Polymarket é proporcional a min(p, 1-p):

    fee_por_share = rate * min(p, 1 - p)

Comparação em p = 0.50 (por share, rate = 7.2%):
    modelo "min"      → 0.072 * 0.50 = 0.0360
    modelo "product"  → 0.072 * 0.25 = 0.0180   ← metade do custo real

`config.FEE_MODEL` controla qual usar:
    "min"      — modelo publicado pela Polymarket (padrão)
    "product"  — modelo legado, mantido só para comparação histórica
    "auto"     — usa o mais caro dos dois (mais conservador de todos)

Regra da casa: na dúvida sobre a tabela vigente, usar o modelo mais caro.
Um fee superestimado custa sinais perdidos; um fee subestimado custa dinheiro.
"""

from config import config


def _rate(rate: float | None) -> float:
    return config.B5_FEE_RATE if rate is None else rate


def fee_per_share(price: float, rate: float | None = None) -> float:
    """Fee taker por share comprado ao preço `price`."""
    if not (0.0 < price < 1.0):
        return 0.0
    r     = _rate(rate)
    f_min = r * min(price, 1.0 - price)
    f_prd = r * price * (1.0 - price)

    model = getattr(config, "FEE_MODEL", "min")
    if model == "product":
        return f_prd
    if model == "auto":
        return max(f_min, f_prd)
    return f_min


def fee_usd(price: float, shares: float, rate: float | None = None) -> float:
    """Fee total em dólares para `shares` shares compradas a `price`."""
    return fee_per_share(price, rate) * shares


def effective_price(price: float, rate: float | None = None) -> float:
    """Preço all-in por share: preço de execução + fee."""
    return price + fee_per_share(price, rate)


def binary_arb_cost(p_yes: float, p_no: float, rate: float | None = None) -> float:
    """
    Custo all-in de 1 par YES+NO (payout garantido de $1.00).
    Arbitragem só existe se o retorno for < 1.0 com folga.
    """
    return effective_price(p_yes, rate) + effective_price(p_no, rate)


def binary_arb_edge(p_yes: float, p_no: float, rate: float | None = None) -> float:
    """Lucro líquido por par YES+NO. Negativo = prejuízo."""
    return 1.0 - binary_arb_cost(p_yes, p_no, rate)


def net_edge_directional(price: float, gross_edge: float,
                         rate: float | None = None) -> float:
    """Edge direcional descontado o fee de entrada."""
    return gross_edge - fee_per_share(price, rate)
