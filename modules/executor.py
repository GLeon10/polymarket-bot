"""
Execução de ordens reais no CLOB da Polymarket.

Antes, os nove módulos terminavam em `logger.warning("execução live não
implementada")`. Este módulo é a única porta por onde ordem real sai, e ele
recusa mais do que aceita, de propósito.

Três coisas que ele faz e que um `post_order` direto não faria:

1. REVALIDA antes de enviar. Entre detectar e executar passam segundos, e em
   mercado de 5 minutos isso é uma eternidade. O livro é lido de novo e o edge
   recalculado; se caiu abaixo de EXEC_MIN_EDGE, nenhuma ordem sai.

2. USA FOK, não FAK. Fill-or-kill: ou a perna inteira preenche, ou nada
   preenche. Perna meio-preenchida numa arbitragem é posição direcional nua,
   que é exatamente o risco que a arbitragem existe para não ter.

3. DESMONTA o que já preencheu. Se a perna 2 morre depois da perna 1 ter
   preenchido, a perna 1 é vendida a mercado imediatamente. Assume-se o
   prejuízo do spread — que é pequeno e conhecido — em vez de carregar uma
   direcional que ninguém dimensionou.

Cadeados para chegar até aqui, todos simultâneos:
    MODE=live  +  LIVE_CONFIRMED=true  +  módulo em LIVE_MODULES
    +  kill switch desarmado  +  risco com capacidade
"""

import logging
import math

from config import config
from modules import book, fees, notifier, risk

logger = logging.getLogger("executor")


# ── Autorização ───────────────────────────────────────────────────────────────

def live_enabled(module: str) -> bool:
    """
    True apenas se TODOS os cadeados estiverem abertos para este módulo.
    Qualquer módulo fora de LIVE_MODULES segue em dry-run mesmo com MODE=live.
    """
    if config.MODE != "live":
        return False
    if not config.LIVE_CONFIRMED:
        return False
    if module not in config.LIVE_MODULES:
        return False
    if risk.is_tripped():
        logger.warning("executor: %s bloqueado — kill switch armado", module)
        return False
    return True


# ── Utilitários de preço e tamanho ────────────────────────────────────────────

def _tick_size(client, token_id: str) -> float:
    try:
        return float(client.get_tick_size(token_id))
    except Exception:
        return 0.01


def _round_buy_price(price: float, tick: float) -> float:
    """Arredonda PARA CIMA no tick: ordem de compra precisa cruzar o ask."""
    if tick <= 0:
        return round(price, 4)
    return min(0.999, math.ceil(price / tick) * tick)


def _round_sell_price(price: float, tick: float) -> float:
    """Arredonda PARA BAIXO no tick: ordem de venda precisa cruzar o bid."""
    if tick <= 0:
        return round(price, 4)
    return max(0.001, math.floor(price / tick) * tick)


def _order_ok(resp) -> bool:
    """Interpreta a resposta do CLOB. Conservador: dúvida conta como falha."""
    if not isinstance(resp, dict):
        return False
    if resp.get("success") is False:
        return False
    status = str(resp.get("status", "")).lower()
    if status in ("matched", "live", "delayed"):
        return True
    return bool(resp.get("orderID") or resp.get("orderId")) and resp.get("errorMsg") in (None, "")


# ── Revalidação ───────────────────────────────────────────────────────────────

def _revalidate(legs, min_payout_per_share: float) -> tuple[bool, str, float]:
    """
    Relê o livro e recalcula o edge com os preços que existem AGORA.
    Retorna (ok, motivo, edge_atual).
    """
    cost_per_share = 0.0
    for leg in legs:
        if not leg.token_id:
            return False, f"perna {leg.market_id[:10]} sem token_id", 0.0
        b = book.fetch(leg.token_id)
        if b is None or not b.asks:
            return False, f"livro indisponível para {leg.token_id[:10]}", 0.0
        vwap, _, filled = book.vwap_buy(b, leg.price * leg.shares)
        if filled < leg.price * leg.shares * config.MIN_FILL_RATIO:
            return False, f"profundidade insuficiente em {leg.token_id[:10]}", 0.0
        cost_per_share += fees.effective_price(vwap)

    if cost_per_share <= 0:
        return False, "custo recalculado inválido", 0.0

    edge = (min_payout_per_share - cost_per_share) / cost_per_share
    if edge < config.EXEC_MIN_EDGE:
        return False, f"edge caiu para {edge:.2%} (mínimo {config.EXEC_MIN_EDGE:.2%})", edge
    return True, "", edge


# ── Envio e desmonte ──────────────────────────────────────────────────────────

def _send_leg(client, leg, side: str) -> tuple[bool, dict | None]:
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL

    tick = _tick_size(client, leg.token_id)
    if side == "BUY":
        price = _round_buy_price(leg.price * (1 + config.EXEC_PRICE_BUFFER), tick)
        poly_side = BUY
    else:
        b = book.fetch(leg.token_id)
        bid = b.best_bid if b else 0.0
        if bid <= 0:
            return False, None
        price = _round_sell_price(bid * (1 - config.EXEC_PRICE_BUFFER), tick)
        poly_side = SELL

    size = math.floor(leg.shares * 100) / 100.0   # 2 casas, sempre para baixo
    if size <= 0:
        return False, None

    try:
        resp = client.create_and_post_order(
            OrderArgs(token_id=leg.token_id, price=price, size=size, side=poly_side),
            OrderType.FOK,
        )
    except Exception as e:
        logger.error("executor: falha ao enviar %s %s @ %.4f x %.2f — %s",
                     side, leg.token_id[:12], price, size, e)
        return False, None

    ok = _order_ok(resp)
    logger.info("executor: %s %s @ %.4f x %.2f → %s",
                side, leg.token_id[:12], price, size, "OK" if ok else f"FALHOU ({resp})")
    return ok, resp


def _unwind(client, filled_legs) -> None:
    """Vende a mercado tudo que já preencheu. Best effort, mas ruidoso."""
    if not filled_legs:
        return
    logger.warning("executor: DESMONTANDO %d perna(s) já preenchida(s)", len(filled_legs))
    falhas = []
    for leg in filled_legs:
        ok, _ = _send_leg(client, leg, "SELL")
        if not ok:
            falhas.append(leg.token_id[:12])

    if falhas:
        # Posição direcional que não conseguimos desfazer é a situação mais
        # perigosa possível. Trava o bot e chama o humano.
        risk.trip(f"desmonte falhou nas pernas {falhas} — posição direcional aberta sem cobertura")
        notifier.send(
            "<b>[ATENÇÃO] Desmonte falhou</b>\n"
            f"Pernas ainda abertas: {', '.join(falhas)}\n"
            "Posição direcional descoberta — intervenção manual necessária."
        )


# ── API pública ───────────────────────────────────────────────────────────────

def execute_basket(client, module: str, signal_id: str, legs,
                   min_payout_per_share: float = 1.0) -> bool:
    """
    Executa todas as pernas da cesta. True apenas se TODAS preencheram.

    Em caso de falha parcial, desmonta o que preencheu antes de retornar.
    """
    if client is None:
        logger.error("executor: cliente CLOB ausente — %s não executado", signal_id)
        return False
    if not legs:
        return False

    ok, motivo, edge = _revalidate(legs, min_payout_per_share)
    if not ok:
        logger.warning("executor: %s ABORTADO na revalidação — %s", signal_id, motivo)
        return False

    logger.info("executor: %s | %d pernas | edge revalidado %.2f%% | ENVIANDO",
                signal_id, len(legs), edge * 100)

    filled = []
    for i, leg in enumerate(legs, 1):
        sent, _ = _send_leg(client, leg, "BUY")
        if not sent:
            logger.error("executor: %s | perna %d/%d falhou — abortando cesta",
                         signal_id, i, len(legs))
            _unwind(client, filled)
            return False
        filled.append(leg)

    total_cost = sum(l.cost_usd for l in legs)
    logger.info("executor: %s EXECUTADO | %d pernas | custo $%.2f | edge %.2f%%",
                signal_id, len(legs), total_cost, edge * 100)
    notifier.send(
        f"<b>[EXECUTADO {module}]</b>\n"
        f"{len(legs)} perna(s) | custo ${total_cost:.2f} | edge {edge * 100:.2f}%"
    )
    return True


def execute_single(client, module: str, signal_id: str, leg) -> bool:
    """Posição direcional de uma perna só (B1/B2). Sem cesta, sem desmonte."""
    if client is None or leg is None:
        return False
    b = book.fetch(leg.token_id)
    if b is None or not b.asks:
        logger.warning("executor: %s sem livro — não executado", signal_id)
        return False

    vwap, _, filled = book.vwap_buy(b, leg.price * leg.shares)
    if filled < leg.price * leg.shares * config.MIN_FILL_RATIO:
        logger.warning("executor: %s sem profundidade — não executado", signal_id)
        return False
    if vwap > leg.price * (1 + config.EXEC_MAX_SLIPPAGE):
        logger.warning("executor: %s preço saiu de %.4f para %.4f — não executado",
                       signal_id, leg.price, vwap)
        return False

    ok, _ = _send_leg(client, leg, "BUY")
    if ok:
        logger.info("executor: %s EXECUTADO | %.2f shares @ %.4f", signal_id, leg.shares, vwap)
    return ok
