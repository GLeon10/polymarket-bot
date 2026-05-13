"""
Módulo B5 — Crypto 5-Min Spread Capture (BTC / ETH / SOL)

Arbitragem intra-candle em mercados binários de 5 minutos.
Compra YES + NO do mesmo mercado quando o custo total (incluindo fee) < threshold:
    p_up_ask + p_down_ask + fee(p_up) + fee(p_down) < B5_THRESHOLD (0.982)
    fee(p) = B5_FEE_RATE × p × (1−p)

Mecânica:
  - Busca mercados 5-min BTC/ETH/SOL via Gamma API (REST, a cada B5_SCAN_INTERVAL)
  - Preços em tempo real via WebSocket CLOB; fallback automático para REST
  - Janela de entrada: T+30s a T+240s após abertura do candle
  - $25 por lado ($50 total); ordens FAK
  - Dry-run: registra sinal no tracker e notifica Telegram
  - Live: execução via CLOB client (não implementada nesta versão)
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
from modules import clob_utils, tracker

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
    size_usd:   float       # por lado
    end_time:   datetime
    slug:       str = ""
    found_at:   datetime = field(default_factory=datetime.utcnow)


# ── Fórmulas de fee e threshold ───────────────────────────────────────────────

def _fee(p: float) -> float:
    """Taker fee por unidade para mercados crypto: B5_FEE_RATE × p × (1−p)."""
    return config.B5_FEE_RATE * p * (1.0 - p)


def _total_cost(p_up: float, p_down: float) -> float:
    return p_up + p_down + _fee(p_up) + _fee(p_down)


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
    """Fallback REST: retorna o melhor ask do order book."""
    try:
        resp = requests.get(
            f"{config.CLOB_BASE_URL}/book",
            params={"token_id": token_id},
            timeout=5,
        )
        asks = resp.json().get("asks", [])
        return float(asks[0]["price"]) if asks else 0.0
    except Exception:
        return 0.0


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
    for mid, info in snapshot.items():
        with _exec_lock:
            if mid in _executed:
                continue

        if not _in_entry_window(info["end_time"]):
            continue
        in_window += 1

        # Ask via WS (prioritário) ou REST (fallback)
        p_up   = _get_ask(info["yes_id"])   or _rest_ask(info["yes_id"])
        p_down = _get_ask(info["no_id"])    or _rest_ask(info["no_id"])

        if p_up <= 0 or p_down <= 0:
            continue

        cost = _total_cost(p_up, p_down)
        if cost >= config.B5_THRESHOLD:
            continue

        fee_t  = _fee(p_up) + _fee(p_down)
        spread = round(1.0 - cost, 4)
        asset  = _detect_asset(info["question"])

        logger.info(
            "SINAL B5 | %s | p_yes=%.3f p_no=%.3f fee=%.4f | lucro=%.2f%%",
            asset, p_up, p_down, fee_t, spread * 100,
        )

        signals.append(CryptoSpreadSignal(
            market_id  = mid,
            question   = info["question"],
            asset      = asset,
            p_up_ask   = round(p_up, 4),
            p_down_ask = round(p_down, 4),
            fee_total  = round(fee_t, 4),
            spread     = spread,
            size_usd   = config.B5_TRADE_SIZE_USD,
            end_time   = info["end_time"],
            slug       = info["slug"],
        ))

    if not signals:
        logger.info(
            "B5: %d mercado(s) vigiados | %d em janela | sem sinais",
            len(snapshot), in_window,
        )

    return signals


# ── Execução ──────────────────────────────────────────────────────────────────

def execute(signal: CryptoSpreadSignal, client) -> bool:
    with _exec_lock:
        if signal.market_id in _executed:
            return False
        _executed.add(signal.market_id)

    if config.MODE == "dry_run":
        logger.info(
            "[DRY-RUN] B5 | %s | YES=%.3f NO=%.3f fee=%.4f | spread=%.2f%% | $%.0f/lado",
            signal.asset, signal.p_up_ask, signal.p_down_ask,
            signal.fee_total, signal.spread * 100, signal.size_usd,
        )
        tracker.record_signal(
            "B5",
            signal.market_id,
            f"[B5] {signal.asset} 5-min: {signal.question}",
            "Yes+No",
            round((signal.p_up_ask + signal.p_down_ask) / 2, 4),
            signal.spread,
            closes_at_iso=signal.end_time.isoformat(),
            market_slug=signal.slug,
            n_markets=2,
        )
        return True

    logger.warning("B5: execução live não implementada (market_id=%s)", signal.market_id)
    return False