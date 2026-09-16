"""
Acesso ao order book do CLOB com normalização defensiva.

Motivo de existir: o código antigo assumia que `asks[0]` era o melhor ask e
que `asks[:5]` eram os 5 melhores níveis. A API da Polymarket devolve os asks
do pior para o melhor, então ambas as suposições liam o lado errado do livro.

Aqui o livro é sempre reordenado localmente (asks crescente, bids decrescente),
o que torna o código correto independentemente da ordem que a API usar hoje ou
amanhã. Nenhuma função confia na ordem recebida.

Além do melhor preço, expõe o que de fato importa para dimensionar ordem:
profundidade acumulada e preço médio de execução (VWAP) para um tamanho alvo.
"""

import logging
from dataclasses import dataclass, field

import requests

from config import config

logger = logging.getLogger("book")


@dataclass
class Level:
    price: float
    size:  float          # em shares

    @property
    def usd(self) -> float:
        return self.price * self.size


@dataclass
class Book:
    token_id: str
    bids: list[Level] = field(default_factory=list)   # decrescente (melhor primeiro)
    asks: list[Level] = field(default_factory=list)   # crescente   (melhor primeiro)

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def spread(self) -> float:
        if not self.asks or not self.bids:
            return 1.0
        return self.best_ask - self.best_bid

    @property
    def mid(self) -> float:
        if not self.asks or not self.bids:
            return 0.0
        return (self.best_ask + self.best_bid) / 2.0


def _parse_levels(raw: list, reverse: bool) -> list[Level]:
    levels = []
    for entry in raw or []:
        try:
            price = float(entry["price"])
            size  = float(entry["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0 or price >= 1 or size <= 0:
            continue
        levels.append(Level(price, size))
    levels.sort(key=lambda l: l.price, reverse=reverse)
    return levels


def fetch(token_id: str, timeout: float = 5.0) -> Book | None:
    """Busca e normaliza o livro. Retorna None em qualquer falha."""
    if not token_id:
        return None
    try:
        resp = requests.get(
            f"{config.CLOB_BASE_URL}/book",
            params={"token_id": token_id},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.debug("book: falha ao buscar token %s — %s", token_id[:12], e)
        return None

    return Book(
        token_id = token_id,
        bids     = _parse_levels(data.get("bids"), reverse=True),
        asks     = _parse_levels(data.get("asks"), reverse=False),
    )


def best_ask(token_id: str) -> float:
    """Melhor (menor) preço de compra disponível. 0.0 se indisponível."""
    b = fetch(token_id)
    return b.best_ask if b else 0.0


def best_bid(token_id: str) -> float:
    b = fetch(token_id)
    return b.best_bid if b else 0.0


def vwap_buy(book: Book | None, usd: float) -> tuple[float, float, float]:
    """
    Simula a compra de `usd` dólares varrendo o livro de asks.

    Retorna (preco_medio, shares, usd_preenchido).
    Se o livro não tiver profundidade suficiente, devolve o que daria para
    preencher — o chamador compara `usd_preenchido` com `usd` para decidir.
    """
    if book is None or not book.asks or usd <= 0:
        return 0.0, 0.0, 0.0

    remaining = usd
    shares    = 0.0
    spent     = 0.0

    for level in book.asks:
        if remaining <= 0:
            break
        take_usd = min(remaining, level.usd)
        shares   += take_usd / level.price
        spent    += take_usd
        remaining -= take_usd

    if shares <= 0:
        return 0.0, 0.0, 0.0
    return spent / shares, shares, spent


def executable_ask(token_id: str, usd: float) -> float:
    """
    Preço médio real para comprar `usd` do token, ou 0.0 se o livro não
    comportar o tamanho. É este o preço que deve entrar em qualquer cálculo
    de arbitragem — não o mid, não o melhor ask isolado.
    """
    book = fetch(token_id)
    if book is None:
        return 0.0
    vwap, _, filled = vwap_buy(book, usd)
    if filled < usd * config.MIN_FILL_RATIO:
        return 0.0
    return vwap


def depth_usd(token_id: str, levels: int = 5) -> float:
    """Liquidez em dólares nos `levels` MELHORES níveis de ask."""
    book = fetch(token_id)
    if book is None:
        return 0.0
    return sum(l.usd for l in book.asks[:levels])


def depth_usd_at_or_better(book: Book | None, max_price: float) -> float:
    """Liquidez disponível a um preço igual ou melhor que `max_price`."""
    if book is None:
        return 0.0
    return sum(l.usd for l in book.asks if l.price <= max_price)
