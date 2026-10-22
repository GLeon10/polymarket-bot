"""
Matemática compartilhada de arbitragem entre cestas de mercados.

A, B3 e CORR detectam inconsistências diferentes mas montam o mesmo tipo de
trade: uma cesta de pernas, cada uma comprando YES ou NO em um mercado, com o
MESMO número de shares em todas. O que muda entre elas é quanto a cesta paga
no pior resultado possível:

    UNDERSUM de n faixas exaustivas e disjuntas  → paga 1 share
    OVERSUM  de n faixas disjuntas               → paga n−1 shares
    par hedgeado (subset, threshold, data)       → paga 1 share

Com esse número em mãos, o retorno é sempre o mesmo cálculo:

    custo  = Σ eff(preço_perna)          eff(p) = p + fee(p)
    edge   = (payout_mínimo − custo) / custo

Tudo aqui trabalha com preço de EXECUÇÃO — o VWAP para o tamanho pretendido,
não o mid. Foi a diferença entre as duas coisas que fez o bot registrar
arbitragens de 2% que não existiam no livro.
"""

import logging
import re
from collections import Counter

from config import config
from modules import book, clob_utils, fees

logger = logging.getLogger("arb")


# ── Funil de descartes ────────────────────────────────────────────────────────

class Funnel:
    """
    Conta, por varredura, onde cada oportunidade morreu.

    Existe porque todos os motivos de descarte eram `logger.debug` e o serviço
    roda em INFO: depois de semanas sem nenhum sinal, o log só dizia "0
    válidos", sem separar três situações muito diferentes —

      * nada passou nem na triagem pelo mid     → o mercado está eficiente
      * passou no mid e morreu no livro         → o spread executável come o edge
      * morreu por liquidez/exclusividade/regra → filtro de qualidade

    `melhor retorno` é o quase-acerto: o maior retorno líquido já precificado
    no livro nesta varredura e o mínimo exigido. Se o melhor é 0,4% contra 3,5%
    exigidos, o mercado não tem arbitragem; se é 3,3%, o limiar está apertado.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.counts: Counter = Counter()
        self.best_return: float | None = None
        self.best_needed: float = 0.0
        self.best_desc: str = ""

    def hit(self, chave: str) -> None:
        self.counts[chave] += 1

    drop = hit

    def near_miss(self, retorno: float, exigido: float, desc: str) -> None:
        if self.best_return is None or retorno > self.best_return:
            self.best_return, self.best_needed, self.best_desc = retorno, exigido, desc

    def summary(self, tag: str) -> str:
        c = self.counts
        partes = [f"{k}={v}" for k, v in sorted(c.items()) if k != "triagem_mid"]
        linha = f"[{tag}] funil | passou triagem no mid={c.get('triagem_mid', 0)}"
        if partes:
            linha += " | " + " | ".join(partes)
        if self.best_return is not None:
            linha += (f" | melhor retorno no livro={self.best_return:.2%}"
                      f" (exigido {self.best_needed:.2%}): {self.best_desc[:50]}")
        return linha


# ── Exclusividade mútua ───────────────────────────────────────────────────────
#
# A soma dos preços YES só carrega informação quando os eventos são mutuamente
# exclusivos. Se vários podem acontecer juntos, a soma não significa nada: três
# partidos com 47% de chance cada de ficar numa faixa de votos somam 141%, e
# isso está certo.
#
# Esse era o buraco: o agrupamento "Will X win <evento>" foi tratado como
# exclusivo sempre, mas <evento> pode ser uma FAIXA em vez de um posto. Na
# eleição de Sachsen-Anhalt o bot juntou
#
#     "Will SPD win between 5% and 7% of all valid second votes?"
#     "Will Grüne win between 5% and 7% of all valid second votes?"
#     "Will BSW win between 5% and 7% of all valid second votes?"
#
# como se apenas um pudesse vencer, leu a soma de 138% como "spread de 38%" e
# registrou $19 de lucro. Os três podem ficar na faixa ao mesmo tempo — e num
# pleito alemão isso é até provável.

# Postos únicos: só uma entidade pode ocupar. A soma TEM de fechar em 100%.
_RANK_UNICO_RE = re.compile(
    r"\b(most seats|second[- ]most seats|third[- ]most seats|greatest number|"
    r"largest number|highest number)\b",
    re.IGNORECASE,
)

# O que distingue um grupo não exclusivo não é a presença de uma faixa: é a
# faixa incidir sobre o PLACAR DO PRÓPRIO sujeito. "Ficar entre 5% e 7% dos
# votos" é algo que vários partidos fazem ao mesmo tempo; "vencer a eleição
# por menos de 5%" só o vencedor faz, e vencedor há um só.

# (a) contagem de cadeiras do próprio sujeito
_PLACAR_CADEIRAS_RE = re.compile(
    r"\b(?:a|\d+(?:[.,]\d+)?(?:\s*[-–—]\s*\d+(?:[.,]\d+)?)?)\s+seats?\b"
    r"|\b(?:fewer|less|more|greater)\s+than\s+\d+\s+seats?\b"
    r"|\b(?:at least|at most|or more|or fewer)\s+\d*\s*seats?\b"
    r"|\bseats?\s+(?:or more|or fewer)\b",
    re.IGNORECASE,
)

# (b) parcela de votos do próprio sujeito
_PLACAR_VOTOS_RE = re.compile(
    r"\bvote share\b"
    r"|\bof (?:all |the )?(?:valid )?(?:second |first )?votes?\b"
    r"|\d+(?:[.,]\d+)?\s*%?\s*(?:[-–—]\s*\d+(?:[.,]\d+)?\s*%?)?\s*of the vote\b",
    re.IGNORECASE,
)

# Qualificador que pertence ao vencedor único, não ao placar do sujeito.
_MARGEM_DO_VENCEDOR_RE = re.compile(
    r"\bby (?:a margin of|less than|more than|over|under|\d)", re.IGNORECASE
)


def is_exclusive_event(stem: str) -> tuple[bool, str]:
    """
    Decide se o evento que dá nome ao grupo admite um único vencedor.

    Retorna (exclusivo, motivo).

    A pergunta certa não é "tem faixa no texto?", e sim "a faixa é sobre o
    placar do próprio sujeito?". Comparar:

        'between 5% and 7% of all valid second votes'  → vários partidos cabem
        'the election by a margin of 5-10%'            → só o vencedor tem margem
    """
    if not stem or not stem.strip():
        return False, "evento vazio"

    if _RANK_UNICO_RE.search(stem):
        return True, "posto único"

    m = _PLACAR_CADEIRAS_RE.search(stem)
    if m:
        return False, f"contagem de cadeiras do próprio sujeito ('{m.group(0).strip()}')"

    m = _PLACAR_VOTOS_RE.search(stem)
    if m:
        return False, f"parcela de votos do próprio sujeito ('{m.group(0).strip()}')"

    if _MARGEM_DO_VENCEDOR_RE.search(stem):
        return True, "margem do vencedor único"

    return True, "vencedor único"


def market_fee_rate(market: dict) -> float:
    """
    Taxa taker do mercado. Cada mercado do Polymarket publica a sua em
    `taker_base_fee`; usar a taxa de crypto (7.2%) aqui superestimaria o custo
    em mercados de evento, que em geral cobram zero.
    """
    try:
        return max(0.0, float(market.get("taker_base_fee", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def exec_price(market: dict, outcome: str, usd: float) -> float:
    """Preço médio real para comprar `usd` do lado `outcome`. 0.0 se não couber."""
    tok = clob_utils.token_by_outcome(market, outcome)
    if not tok:
        return 0.0
    if getattr(config, "A_PRICING_MODE", "ask") == "mid":
        price = float(tok.get("price", 0) or 0)
        return price if 0 < price < 1 else 0.0
    return book.executable_ask(tok["token_id"], usd)


def price_basket(markets: list[dict], outcomes: list[str],
                 budget_usd: float) -> tuple[list[float], float]:
    """
    Precifica a cesta ao tamanho pretendido, em duas passadas.

    1ª: estima o número de shares que o orçamento compra, usando mids.
    2ª: com o volume por perna definido, pergunta ao livro o preço médio real.

    Retorna ([], 0.0) se qualquer perna não tiver profundidade — uma cesta com
    perna faltando deixa de ser arbitragem e vira posição direcional.
    """
    rates = [market_fee_rate(m) for m in markets]

    mids = []
    for m, outcome in zip(markets, outcomes):
        tok = clob_utils.token_by_outcome(m, outcome)
        if not tok:
            return [], 0.0
        price = float(tok.get("price", 0) or 0)
        if not (0 < price < 1):
            return [], 0.0
        mids.append(price)

    unit_cost = sum(fees.effective_price(p, r) for p, r in zip(mids, rates))
    if unit_cost <= 0:
        return [], 0.0
    shares = budget_usd / unit_cost

    exec_prices = []
    for m, outcome, mid in zip(markets, outcomes, mids):
        price = exec_price(m, outcome, max(shares * mid, 1.0))
        if not (0 < price < 1):
            return [], 0.0
        exec_prices.append(price)

    return exec_prices, basket_cost(exec_prices, rates)


def basket_cost(exec_prices: list[float], rates: list[float] | None = None) -> float:
    """Custo all-in por share da cesta, com a taxa própria de cada mercado."""
    if rates is None:
        rates = [0.0] * len(exec_prices)
    return sum(fees.effective_price(p, r) for p, r in zip(exec_prices, rates))


def basket_return(exec_prices: list[float], min_payout: float,
                  rates: list[float] | None = None) -> float:
    """Retorno líquido sobre o capital investido. Fees já embutidos."""
    cost = basket_cost(exec_prices, rates)
    if cost <= 0:
        return 0.0
    return (min_payout - cost) / cost


def token_ids(markets: list[dict], outcomes: list[str]) -> list[str]:
    return [
        (clob_utils.token_by_outcome(m, o) or {}).get("token_id", "")
        for m, o in zip(markets, outcomes)
    ]


def min_required_return(n_legs: int) -> float:
    """Retorno mínimo exigido, crescente no número de pernas a executar."""
    return config.A_MIN_SPREAD + (n_legs - 1) * config.A_SPREAD_PER_MARKET


def sum_pattern_legs(n: int, oversum: bool) -> tuple[list[str], float]:
    """
    Lados e payout mínimo para uma cesta de soma.

    OVERSUM compra NO em todas: no máximo uma vence, então pelo menos n−1 NOs
    pagam. Exige apenas exclusividade mútua.

    UNDERSUM compra YES em todas e depende de alguma vencer — exige também que
    o conjunto cubra todos os resultados possíveis. Sem essa garantia, o que
    parece spread é o preço da cauda descoberta.
    """
    if oversum:
        return ["No"] * n, float(n - 1)
    return ["Yes"] * n, 1.0
