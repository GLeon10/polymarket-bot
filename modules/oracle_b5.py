"""
Módulo B5 — Crypto 5-Min Spread Capture (BTC / ETH / SOL)

Arbitragem intra-candle em mercados binários de 5 minutos.
Compra YES + NO do mesmo mercado: exatamente uma perna vence, então o par paga
$1 sempre. Há lucro quando o custo all-in fica abaixo disso com folga:

    edge = (1 − custo) / custo  ≥  B5_MIN_NET_EDGE
    custo = eff(p_yes) + eff(p_no),  eff(p) = p + rate × min(p, 1−p)

Duas correções importantes em relação à versão anterior:

  Fee. A fórmula era `rate × p × (1−p)`, que cobra metade do fee real em
  p ≈ 0.50 — justamente onde este módulo opera. Um par a 0.46/0.47 aparecia
  com "edge 3.4%" quando o líquido verdadeiro era 0.31%.

  Profundidade. O gate usava só o melhor ask, que pode ter 1 share atrás.
  Agora o preço é o VWAP para o tamanho pretendido e ambos os lados precisam
  ter B5_MIN_DEPTH_USD de livro.

Mecânica:
  - Busca mercados 5-min BTC/ETH/SOL via Gamma API (REST, a cada B5_SCAN_INTERVAL)
  - WebSocket CLOB faz a triagem; o livro REST confirma preço e profundidade
  - Janela de entrada: T+30s a T+240s após abertura do candle
  - Orcamento total da cesta vem de tracker.trade_budget (teto de risco)
  - Ordens FOK por perna, com desmonte se a segunda falhar (executor.py)
  - Dry-run por padrão; live exige MODE=live + LIVE_CONFIRMED + B5 em LIVE_MODULES
"""

import asyncio
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import requests

try:
    import websockets as _websockets  # type: ignore
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

from config import config
from modules import book, clob_utils, executor, fees, tracker

logger = logging.getLogger("oracle_b5")

# ── Padrões de detecção ───────────────────────────────────────────────────────

_CRYPTO_KW = ["btc", "bitcoin", "eth", "ethereum", "sol", "solana"]
_5MIN_KW   = ["5-min", "5 min", "5 minute", "5-minute", "5min"]

_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ── Estado global (thread-safe) ───────────────────────────────────────────────

# token_id → melhor ask conhecida (atualizada via WS)
_ask: dict[str, float] = {}
_ask_lock = threading.Lock()

# market_id → metadados do mercado monitorado
_watched: dict[str, dict] = {}
_watch_lock = threading.Lock()

# market_ids já executados (dedup por candle)
_executed: set[str] = set()
_exec_lock = threading.Lock()

# controle do thread WebSocket
_ws_stop      = threading.Event()
_ws_thread: threading.Thread | None = None
_ws_tokens: frozenset[str] = frozenset()
_ws_ctrl_lock = threading.Lock()


# ── Data class de sinal ───────────────────────────────────────────────────────

@dataclass
class CryptoSpreadSignal:
    market_id:  str
    question:   str
    asset:      str
    p_up_ask:   float
    p_down_ask: float
    fee_total:  float
    spread:     float       # 1 − (p_up + p_down + fee_total)
    size_usd:   float       # orcamento total da cesta YES+NO
    end_time:   datetime
    slug:       str = ""
    found_at:   datetime = field(default_factory=datetime.utcnow)
    yes_token:  str = ""
    no_token:   str = ""


# ── Fórmulas de fee e threshold ───────────────────────────────────────────────

def _fee(p: float) -> float:
    """
    Taker fee por share. Delega para modules/fees.py.

    A fórmula usada antes aqui era `rate × p × (1−p)`, que em p≈0.50 cobra
    metade do fee real. Como a arbitragem YES+NO opera exatamente nessa
    região, o módulo enxergava o dobro do edge que existia: um par a
    0.46/0.47 aparecia com "3.4% de lucro" quando o líquido real era 0.31%.
    """
    return fees.fee_per_share(p, config.B5_FEE_RATE)


def _total_cost(p_up: float, p_down: float) -> float:
    """Custo all-in de 1 par YES+NO, que paga exatamente $1 na resolução."""
    return fees.binary_arb_cost(p_up, p_down, config.B5_FEE_RATE)


def _net_edge(p_up: float, p_down: float) -> float:
    """Retorno líquido sobre o capital do par. Negativo = prejuízo."""
    cost = _total_cost(p_up, p_down)
    return (1.0 - cost) / cost if cost > 0 else 0.0


def _in_entry_window(end_time: datetime) -> bool:
    """Retorna True se estamos no intervalo [T+30s, T+240s] após abertura do candle."""
    open_t  = end_time - timedelta(seconds=config.B5_CANDLE_SECS)
    elapsed = (datetime.now(timezone.utc) - open_t).total_seconds()
    return config.B5_T_ENTRY_MIN_S <= elapsed <= config.B5_T_ENTRY_MAX_S


# ── Utilitários ───────────────────────────────────────────────────────────────

def _parse_end(market: dict) -> datetime | None:
    iso = market.get("end_date_iso") or market.get("endDate")
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_5min_crypto(market: dict) -> bool:
    q = (market.get("question") or "").lower()
    return any(k in q for k in _CRYPTO_KW) and any(k in q for k in _5MIN_KW)


def _detect_asset(question: str) -> str:
    q = question.lower()
    if "btc" in q or "bitcoin" in q:
        return "BTC"
    if "eth" in q or "ethereum" in q:
        return "ETH"
    if "sol" in q or "solana" in q:
        return "SOL"
    return "CRYPTO"


# ── Busca de mercados via Gamma API ───────────────────────────────────────────

def _find_markets() -> list[dict]:
    """Busca mercados 5-min crypto ativos via Gamma API, um asset por vez."""
    now    = datetime.now(timezone.utc)
    result = []
    seen   = set()

    for asset in config.B5_ASSETS:
        try:
            resp = requests.get(
                f"{config.GAMMA_API_URL}/markets",
                params={"active": "true", "closed": "false",
                        "keyword": f"{asset} 5 minute", "limit": "50"},
                timeout=15,
            )
            resp.raise_for_status()
            data  = resp.json()
            batch = data if isinstance(data, list) else data.get("data", [])
        except requests.RequestException as e:
            logger.warning("B5: falha ao buscar mercados %s — %s", asset, e)
            continue

        for m in batch:
            mid = m.get("condition_id") or m.get("id", "")
            if not mid or mid in seen:
                continue
            if not _is_5min_crypto(m):
                continue
            end_t = _parse_end(m)
            if end_t is None or end_t <= now:
                continue
            seen.add(mid)
            result.append(m)

    logger.info("B5: %d mercados 5-min crypto ativos encontrados", len(result))
    return result


# ── Ask prices ────────────────────────────────────────────────────────────────

def _update_ask(token_id: str, price: float) -> None:
    with _ask_lock:
        _ask[token_id] = price


def _get_ask(token_id: str) -> float:
    with _ask_lock:
        return _ask.get(token_id, 0.0)


def _rest_ask(token_id: str) -> float:
    """
    Fallback REST: melhor ask do livro.

    Antes retornava `asks[0]`, que na resposta da Polymarket é o PIOR ask —
    a lista vem do pior para o melhor. `book.best_ask` reordena localmente.
    """
    return book.best_ask(token_id)


# ── WebSocket ─────────────────────────────────────────────────────────────────

async def _run_ws(token_ids: list[str], stop: threading.Event) -> None:
    """Conecta ao CLOB WebSocket, subscreve tokens e atualiza _ask em tempo real."""
    backoff = 5
    while not stop.is_set():
        try:
            async with _websockets.connect(_WS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps({"assets_ids": token_ids, "type": "market"}))
                logger.info("B5 WS: subscrito a %d tokens", len(token_ids))
                backoff = 5

                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    except asyncio.TimeoutError:
                        continue  # keepalive

                    try:
                        events = json.loads(raw)
                        if not isinstance(events, list):
                            events = [events]
                        for ev in events:
                            if ev.get("event_type") != "price_change":
                                continue
                            tid = ev.get("asset_id", "")
                            if not tid:
                                continue
                            # Formato 1: side/price direto no evento
                            side  = ev.get("side", "").upper()
                            price = ev.get("price", "")
                            if side == "ASK" and price:
                                try:
                                    _update_ask(tid, float(price))
                                except ValueError:
                                    pass
                                continue
                            # Formato 2: lista "changes"
                            for chg in ev.get("changes", []):
                                if chg.get("side", "").upper() == "ASK":
                                    try:
                                        _update_ask(tid, float(chg["price"]))
                                    except (KeyError, ValueError):
                                        pass
                    except (json.JSONDecodeError, TypeError):
                        pass

        except Exception as e:
            if not stop.is_set():
                logger.warning("B5 WS: desconectado — %s | reconectando em %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


def _ws_thread_fn(token_ids: list[str], stop: threading.Event) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_ws(token_ids, stop))
    finally:
        loop.close()


def _start_ws(token_ids: list[str]) -> None:
    """Inicia (ou reinicia) o thread WebSocket se o conjunto de tokens mudou."""
    global _ws_thread, _ws_stop, _ws_tokens

    if not _HAS_WS:
        return

    new_set = frozenset(token_ids)
    with _ws_ctrl_lock:
        if new_set == _ws_tokens and _ws_thread and _ws_thread.is_alive():
            return

        # Para thread anterior
        _ws_stop.set()
        if _ws_thread and _ws_thread.is_alive():
            _ws_thread.join(timeout=3)

        _ws_stop   = threading.Event()
        _ws_tokens = new_set
        _ws_thread = threading.Thread(
            target=_ws_thread_fn,
            args=(list(_ws_tokens), _ws_stop),
            daemon=True,
            name="thread-B5-ws",
        )
        _ws_thread.start()
        logger.info("B5: thread WS (re)iniciada com %d tokens", len(_ws_tokens))


# ── Scanner ───────────────────────────────────────────────────────────────────

def scan() -> list[CryptoSpreadSignal]:
    """
    Atualiza a lista de mercados vigiados, garante subscrição WebSocket
    e retorna sinais de spread para candles dentro da janela de entrada.
    """
    markets = _find_markets()

    all_token_ids: set[str] = set()
    with _watch_lock:
        for m in markets:
            mid = m.get("condition_id") or m.get("id", "")
            if not mid:
                continue
            yes_tok = clob_utils.token_by_outcome(m, "Yes")
            no_tok  = clob_utils.token_by_outcome(m, "No")
            if not yes_tok or not no_tok:
                continue
            end_t = _parse_end(m)
            if end_t is None:
                continue
            _watched[mid] = {
                "yes_id":   yes_tok["token_id"],
                "no_id":    no_tok["token_id"],
                "end_time": end_t,
                "slug":     m.get("market_slug", ""),
                "question": m.get("question", "")[:100],
            }
            all_token_ids.update({yes_tok["token_id"], no_tok["token_id"]})

    if all_token_ids:
        _start_ws(list(all_token_ids))

    signals: list[CryptoSpreadSignal] = []

    with _watch_lock:
        snapshot = dict(_watched)

    in_window = 0
    best_ws: float | None = None     # melhor edge no topo do livro (WS)
    best_exec: float | None = None   # melhor edge ja com VWAP e profundidade
    for mid, info in snapshot.items():
        with _exec_lock:
            if mid in _executed:
                continue

        if not _in_entry_window(info["end_time"]):
            continue
        in_window += 1

        # Passo 1 — triagem barata pelo WebSocket. Só o melhor ask, sem tamanho.
        p_up_ws   = _get_ask(info["yes_id"])
        p_down_ws = _get_ask(info["no_id"])
        if p_up_ws > 0 and p_down_ws > 0:
            e_ws = _net_edge(p_up_ws, p_down_ws)
            best_ws = e_ws if best_ws is None else max(best_ws, e_ws)
            if e_ws <= 0:
                continue

        # Passo 2 — confirmação no livro, com profundidade para o tamanho real.
        #
        # O melhor ask isolado não diz nada sobre quanto dá para comprar por
        # aquele preço: pode haver 1 share ali. A versão anterior dimensionava
        # $25 por lado em cima desse preço e chamava de arbitragem. Aqui o preço
        # usado é o VWAP para o tamanho pretendido, e a cesta é descartada se
        # qualquer um dos lados não comportar o volume.
        # Orcamento TOTAL da cesta YES+NO, ja limitado pelo teto de risco.
        # Cada lado precisa comportar metade dele no livro.
        size   = tracker.trade_budget("B5")
        p_up   = book.executable_ask(info["yes_id"], size / 2)
        p_down = book.executable_ask(info["no_id"],  size / 2)
        if not (0 < p_up < 1) or not (0 < p_down < 1):
            continue

        depth_yes = clob_utils.get_orderbook_liquidity(info["yes_id"])
        depth_no  = clob_utils.get_orderbook_liquidity(info["no_id"])
        if min(depth_yes, depth_no) < config.B5_MIN_DEPTH_USD:
            logger.debug(
                "B5: profundidade insuficiente (yes=$%.0f no=$%.0f, mínimo $%.0f)",
                depth_yes, depth_no, config.B5_MIN_DEPTH_USD,
            )
            continue

        edge = _net_edge(p_up, p_down)
        best_exec = edge if best_exec is None else max(best_exec, edge)
        if edge < config.B5_MIN_NET_EDGE:
            continue

        fee_t = _fee(p_up) + _fee(p_down)
        asset = _detect_asset(info["question"])

        logger.info(
            "SINAL B5 | %s | p_yes=%.3f p_no=%.3f fee=%.4f | edge_liq=%.2f%% | "
            "profundidade yes=$%.0f no=$%.0f",
            asset, p_up, p_down, fee_t, edge * 100, depth_yes, depth_no,
        )

        signals.append(CryptoSpreadSignal(
            market_id  = mid,
            question   = info["question"],
            asset      = asset,
            p_up_ask   = round(p_up, 4),
            p_down_ask = round(p_down, 4),
            fee_total  = round(fee_t, 4),
            spread     = round(edge, 4),
            size_usd   = size,
            end_time   = info["end_time"],
            slug       = info["slug"],
            yes_token  = info["yes_id"],
            no_token   = info["no_id"],
        ))

    if not signals:
        # "0 mercados vigiados" e "mercados sem edge" sao problemas opostos:
        # o primeiro e descoberta de mercado quebrada; o segundo, mercado
        # eficiente. O melhor edge visto separa os dois casos no log.
        fmt = lambda v: "n/d" if v is None else f"{v:.2%}"
        logger.info(
            "B5: %d mercado(s) vigiados | %d em janela | sem sinais | "
            "melhor edge topo-do-livro=%s | melhor edge executavel=%s (exigido %.2f%%)",
            len(snapshot), in_window, fmt(best_ws), fmt(best_exec),
            config.B5_MIN_NET_EDGE * 100,
        )

    return signals


# ── Execução ──────────────────────────────────────────────────────────────────

def execute(signal: CryptoSpreadSignal, client) -> bool:
    with _exec_lock:
        if signal.market_id in _executed:
            return False
        _executed.add(signal.market_id)

    # Uma cesta YES+NO do MESMO mercado: exatamente uma perna vence, sempre.
    # Por isso min_payout = 1 share e o par é auto-liquidante na resolução.
    legs = tracker.build_legs(
        [
            (signal.market_id, "Yes", signal.p_up_ask,   signal.yes_token),
            (signal.market_id, "No",  signal.p_down_ask, signal.no_token),
        ],
        tracker.trade_budget("B5"),
        fee_rate=config.B5_FEE_RATE,
    )
    if len(legs) != 2:
        return False

    registered = tracker.record_signal(
        "B5",
        signal.market_id,
        f"[B5] {signal.asset} 5-min: {signal.question}",
        "Yes+No",
        round((signal.p_up_ask + signal.p_down_ask) / 2, 4),
        signal.spread,
        closes_at_iso=signal.end_time.isoformat(),
        market_slug=signal.slug,
        n_markets=2,
        legs=legs,
        fee_rate=config.B5_FEE_RATE,
    )
    if not registered:
        return False

    if executor.live_enabled("B5"):
        return executor.execute_basket(
            client, "B5", signal.market_id, legs, min_payout_per_share=1.0,
        )

    logger.info(
        "[DRY-RUN] B5 | %s | YES=%.3f NO=%.3f fee=%.4f | edge_liq=%.2f%% | $%.0f/lado",
        signal.asset, signal.p_up_ask, signal.p_down_ask,
        signal.fee_total, signal.spread * 100, signal.size_usd,
    )
    return True