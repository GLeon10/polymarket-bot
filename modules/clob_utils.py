"""Utilitários compartilhados para acesso ao CLOB API do Polymarket."""

import logging
import requests
from config import config
from modules import book

logger = logging.getLogger("clob_utils")


def fetch_sampling_markets(max_pages: int = 25) -> list[dict]:
    """
    Busca todos os mercados ativos via CLOB /sampling-markets com paginação.
    Retorna apenas mercados com accepting_orders=True.
    """
    markets = []
    cursor = ""
    for _ in range(max_pages):
        try:
            resp = requests.get(
                f"{config.CLOB_BASE_URL}/sampling-markets",
                params={"next_cursor": cursor},
                timeout=15,
            )
            resp.raise_for_status()
            data   = resp.json()
            batch  = data.get("data", [])
            markets.extend(m for m in batch if m.get("accepting_orders"))
            cursor = data.get("next_cursor", "")
            if not cursor or cursor == "LTE=":
                break
        except requests.RequestException as e:
            logger.warning("Falha ao buscar sampling-markets (cursor=%s): %s", cursor, e)
            break
    return markets


def get_orderbook_liquidity(token_id: str, levels: int = 5) -> float:
    """
    Liquidez em dólares nos `levels` MELHORES níveis de ask.

    A versão anterior fazia `asks[:5]` sobre a resposta crua da API — que
    devolve os asks do pior para o melhor —, medindo portanto os cinco piores
    níveis do livro. `modules/book.py` reordena localmente, o que deixa o
    cálculo correto independentemente da ordem que a API usar.

    Para dimensionar ordem, prefira `book.executable_ask(token_id, usd)`:
    liquidez agregada não garante preço.
    """
    return book.depth_usd(token_id, levels)


def get_market(condition_id: str) -> dict | None:
    """Busca um mercado pelo condition_id. None em qualquer falha."""
    if not condition_id:
        return None
    try:
        resp = requests.get(
            f"{config.CLOB_BASE_URL}/markets/{condition_id}",
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def token_by_outcome(market: dict, outcome: str) -> dict | None:
    """Retorna o token do mercado pelo nome do outcome (case-insensitive)."""
    return next(
        (t for t in (market.get("tokens") or []) if t.get("outcome", "").upper() == outcome.upper()),
        None,
    )


def get_event_slug(market_slug: str) -> str:
    """
    Retorna o event slug do Polymarket via Gamma API.
    Usado para construir URLs funcionais: polymarket.com/event/{event_slug}
    """
    try:
        resp = requests.get(
            f"{config.GAMMA_API_URL}/markets",
            params={"slug": market_slug},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data:
            events = data[0].get("events", [])
            if events:
                return events[0].get("slug", "")
    except (requests.RequestException, KeyError, ValueError, IndexError):
        pass
    return ""


def has_tag(market: dict, tags: set[str]) -> bool:
    """Verifica se o mercado possui ao menos uma das tags fornecidas."""
    market_tags = {t for t in (market.get("tags") or [])}
    return bool(market_tags & tags)
