"""
Módulo B5 Pro — Crypto 5-Min Multi-Strategy (BTC / ETH / SOL)

Três estratégias rodando em paralelo sobre mercados binários de 5 minutos:

  ARB       — Arbitragem pura: p_up + p_down + fee < 0.982 (T+30s..T+240s)
              FAK separados por lado para eliminar risco de leg parcial.
  NEAR_RES  — Near-resolution: lado com preço em [0.96, 0.995] nos últimos
              60s do candle. Edge mínimo: 0.5%. Flag TAIL_RISK no Telegram.
  REPRICING — Fair value via Binance aggTrade: |fair_prob − poly| > 6%.
              Volatilidade estimada em janela rolante de 20 candles.

Fontes de dados:
  - WebSocket CLOB Polymarket → ask prices em tempo real
  - WebSocket Binance aggTrade → spot BTC/ETH/SOL em tempo real
  - REST fallback para CLOB quando WS cair (Binance: silencia sem dados)

Arquitetura:
  Um único event loop asyncio em thread daemon.
  CLOB WS e Binance WS como tasks asyncio dentro do mesmo loop.
  As 3 detecções rodam a cada tick (1s) no loop principal.
  scan() garante o loop e drena uma fila thread-safe de sinais.
  execute() deduplica, grava CSV + Sheets + Telegram.
"""

import asyncio
import json
import logging
import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import requests

try:
    import websockets as _websockets  # type: ignore
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

try:
    from scipy.stats import norm as _norm  # type: ignore
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

from config import config
from modules import clob_utils, tracker

logger = logging.getLogger("oracle_b5_pro")

# ── Constantes ────────────────────────────────────────────────────────────────

_CLOB_WS_URL    = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"

_BINANCE_SYMBOLS = {"BTC": "btcusdt", "ETH": "ethusdt", "SOL": "solusdt"}

_CRYPTO_KW = ["btc", "bitcoin", "eth", "ethereum", "sol", "solana"]
_5MIN_KW   = ["5-min", "5 min", "5 minute", "5-minute", "5min"]


# ── Data class de sinal ───────────────────────────────────────────────────────

@dataclass
class B5ProSignal:
    strategy:    str        # "ARB" | "NEAR_RES" | "REPRICING"
    market_id:   str
    question:    str
    asset:       str
    side:        str        # "Yes+No" (ARB) | "Yes" | "No"
    entry_price: float
    edge:        float
    size_usd:    float
    end_time:    datetime
    slug:        str = ""
    fair_prob:   float | None = None
    poly_price:  float | None = None
    latency_ms:  float | None = None
    tail_risk:   bool = False
    found_at:    datetime = field(default_factory=datetime.utcnow)


# ── Estado global (thread-safe) ───────────────────────────────────────────────

# CLOB: token_id → melhor ask conhecida via WS
_clob_ask:  dict[str, float] = {}
_clob_lock = threading.Lock()

# Binance: asset → {price, open, ts}
_binance_price: dict[str, dict] = {}
_binance_lock  = threading.Lock()

# Volatilidade: asset → deque de retornos candle a candle
_vol_window: dict[str, deque] = {
    a: deque(maxlen=config.B5_REPRICING_VOL_CANDLES) for a in config.B5_ASSETS
}
_vol_lock = threading.Lock()

# Mercados vigiados: market_id → metadados
_watched:    dict[str, dict] = {}
_watch_lock = threading.Lock()

# Dedup de execução: "{market_id}:{strategy_key}" → True
_executed:  set[str] = set()
_exec_lock  = threading.Lock()

# Fila de sinais (async → sync)
_signal_queue: "queue.Queue[B5ProSignal]" = queue.Queue()

# Controle do loop
_loop_thread:  threading.Thread | None = None
_loop_stop    = threading.Event()
_loop_started = threading.Event()
_loop_ctrl    = threading.Lock()


# ── Fórmulas ──────────────────────────────────────────────────────────────────

def _fee(p: float) -> float:
    """Taker fee B5: B5_FEE_RATE × p × (1−p)."""
    return config.B5_FEE_RATE * p * (1.0 - p)


def _total_cost(p_up: float, p_down: float) -> float:
    return p_up + p_down + _fee(p_up) + _fee(p_down)


# ── Janelas de tempo ──────────────────────────────────────────────────────────

def _in_arb_window(end_time: datetime) -> bool:
    """T+30s a T+240s após abertura do candle (60s..270s antes do fechamento)."""
    open_t  = end_time - timedelta(seconds=config.B5_CANDLE_SECS)
    elapsed = (datetime.now(timezone.utc) - open_t).total_seconds()
    return config.B5_T_ENTRY_MIN_S <= elapsed <= config.B5_T_ENTRY_MAX_S


def _in_near_res_window(end_time: datetime) -> bool:
    """T-60s a T-10s antes do fechamento do candle."""
    secs_left = (end_time - datetime.now(timezone.utc)).total_seconds()
    return config.B5_NEAR_RES_WINDOW_END_S <= secs_left <= config.B5_NEAR_RES_WINDOW_START_S


# ── Utilitários ───────────────────────────────────────────────────────────────

def _exec_key(market_id: str, strategy_key: str) -> str:
    return f"{market_id}:{strategy_key}"


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


# ── Preços CLOB ───────────────────────────────────────────────────────────────

def _update_clob_ask(token_id: str, price: float) -> None:
    with _clob_lock:
        _clob_ask[token_id] = price


def _get_clob_ask(token_id: str) -> float:
    with _clob_lock:
        return _clob_ask.get(token_id, 0.0)


def _rest_ask(token_id: str) -> float:
    """Fallback REST: melhor ask do order book."""
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


def _ask(token_id: str) -> float:
    """Retorna WS ask (prioritário) ou REST fallback."""
    p = _get_clob_ask(token_id)
    return p if p > 0 else _rest_ask(token_id)


# ── Preços Binance ────────────────────────────────────────────────────────────

def _binance_spot(asset: str) -> float | None:
    with _binance_lock:
        d = _binance_price.get(asset)
    return d["price"] if d else None


def _binance_open(asset: str) -> float | None:
    with _binance_lock:
        d = _binance_price.get(asset)
    return d["open"] if d else None


def _binance_ts(asset: str) -> float | None:
    with _binance_lock:
        d = _binance_price.get(asset)
    return d["ts"] if d else None


def _volatility(asset: str) -> float | None:
    """Desvio padrão dos retornos na janela rolante. Retorna None se dados insuficientes."""
    with _vol_lock:
        vals = list(_vol_window.get(asset, []))
    if len(vals) < 3:
        return None
    mean = sum(vals) / len(vals)
    var  = sum((v - mean) ** 2 for v in vals) / len(vals)
    return math.sqrt(var) if var > 0 else None


# ── Busca de mercados ─────────────────────────────────────────────────────────

def _find_markets() -> list[dict]:
    now    = datetime.now(timezone.utc)
    result = []
    seen: set[str] = set()

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
            logger.warning("B5Pro: falha ao buscar mercados %s — %s", asset, e)
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

    return result


def _refresh_watched() -> list[str]:
    """Atualiza _watched e retorna lista de token_ids para subscrição WS."""
    markets   = _find_markets()
    all_tids: list[str] = []

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
            all_tids.extend([yes_tok["token_id"], no_tok["token_id"]])

    logger.info("B5Pro: %d mercados 5-min crypto vigiados", len(markets))
    return all_tids


# ── Detectores de estratégia ──────────────────────────────────────────────────

def _detect_arb(mid: str, info: dict) -> B5ProSignal | None:
    """
    Estratégia 1 — Arbitragem pura.
    Compra YES + NO via FAK separados quando custo total < threshold.
    Janela: T+30s..T+240s após abertura do candle.
    """
    if not _in_arb_window(info["end_time"]):
        return None

    key = _exec_key(mid, "ARB")
    with _exec_lock:
        if key in _executed:
            return None

    p_up   = _ask(info["yes_id"])
    p_down = _ask(info["no_id"])
    if p_up <= 0 or p_down <= 0:
        return None

    cost = _total_cost(p_up, p_down)
    if cost >= config.B5_THRESHOLD:
        return None

    edge  = round(1.0 - cost, 4)
    asset = _detect_asset(info["question"])

    logger.info(
        "SINAL B5Pro ARB | %s | p_yes=%.3f p_no=%.3f fee=%.4f | edge=%.2f%%",
        asset, p_up, p_down, _fee(p_up) + _fee(p_down), edge * 100,
    )
    return B5ProSignal(
        strategy    = "ARB",
        market_id   = mid,
        question    = info["question"],
        asset       = asset,
        side        = "Yes+No",
        entry_price = round((p_up + p_down) / 2, 4),
        edge        = edge,
        size_usd    = config.B5_TRADE_SIZE_USD,
        end_time    = info["end_time"],
        slug        = info["slug"],
    )


def _detect_near_res(mid: str, info: dict) -> B5ProSignal | None:
    """
    Estratégia 2 — Near-resolution.
    Entra no lado vencedor (preço 0.96..0.995) nos últimos 60s do candle.
    Edge mínimo de 0.5% após fee. Emite flag TAIL_RISK.
    """
    if not _in_near_res_window(info["end_time"]):
        return None

    for outcome, tid in (("Yes", info["yes_id"]), ("No", info["no_id"])):
        key = _exec_key(mid, f"NEAR_RES_{outcome}")
        with _exec_lock:
            if key in _executed:
                continue

        p = _ask(tid)
        if not (config.B5_NEAR_RES_PRICE_MIN <= p <= config.B5_NEAR_RES_PRICE_MAX):
            continue

        edge = round(1.0 - p - _fee(p), 4)
        if edge < config.B5_NEAR_RES_MIN_EDGE:
            continue

        secs_left = (info["end_time"] - datetime.now(timezone.utc)).total_seconds()
        asset     = _detect_asset(info["question"])

        logger.info(
            "SINAL B5Pro NEAR_RES | %s | side=%s p=%.3f edge=%.2f%% secs_left=%.0f ⚠️ TAIL_RISK",
            asset, outcome, p, edge * 100, secs_left,
        )
        return B5ProSignal(
            strategy    = "NEAR_RES",
            market_id   = mid,
            question    = info["question"],
            asset       = asset,
            side        = outcome,
            entry_price = round(p, 4),
            edge        = edge,
            size_usd    = config.B5_TRADE_SIZE_USD,
            end_time    = info["end_time"],
            slug        = info["slug"],
            tail_risk   = True,
        )

    return None


def _detect_repricing(mid: str, info: dict) -> B5ProSignal | None:
    """
    Estratégia 3 — Repricing / Fair Value.
    Calcula probabilidade teórica via Binance spot + volatilidade histórica.
    Emite sinal quando |fair_prob − poly_price| > B5_REPRICING_MIN_DIVERGENCE.
    Loga sempre: fair_prob, poly_price, divergência, latência.
    """
    if not _HAS_SCIPY:
        return None

    asset = _detect_asset(info["question"])
    if asset == "CRYPTO":
        return None

    spot   = _binance_spot(asset)
    open_p = _binance_open(asset)
    ts     = _binance_ts(asset)

    if spot is None or open_p is None or open_p <= 0:
        return None

    vol = _volatility(asset)
    if vol is None or vol <= 0:
        return None

    pct_change = (spot - open_p) / open_p
    z_score    = pct_change / vol
    fair_prob  = float(_norm.cdf(z_score))

    yes_ask = _ask(info["yes_id"])
    no_ask  = _ask(info["no_id"])

    latency_ms = round((time.time() - ts) * 1000, 1) if ts else None

    # Logar sempre (mesmo sem sinal)
    logger.info(
        "B5Pro REPRICING | %s | fair_yes=%.3f poly_yes=%.3f poly_no=%.3f "
        "div_yes=%.2f%% div_no=%.2f%% lat=%sms",
        asset, fair_prob,
        yes_ask, no_ask,
        (fair_prob - yes_ask) * 100 if yes_ask > 0 else 0.0,
        ((1 - fair_prob) - no_ask) * 100 if no_ask > 0 else 0.0,
        f"{latency_ms:.0f}" if latency_ms else "?",
    )

    # Determina lado e divergência
    fair_no    = 1.0 - fair_prob
    div_yes    = fair_prob - yes_ask if yes_ask > 0 else -1
    div_no     = fair_no   - no_ask  if no_ask  > 0 else -1

    if div_yes >= config.B5_REPRICING_MIN_DIVERGENCE:
        side        = "Yes"
        tid         = info["yes_id"]
        entry_price = yes_ask
        divergence  = div_yes
    elif div_no >= config.B5_REPRICING_MIN_DIVERGENCE:
        side        = "No"
        tid         = info["no_id"]
        entry_price = no_ask
        divergence  = div_no
        fair_prob   = fair_no
    else:
        return None

    key = _exec_key(mid, "REPRICING")
    with _exec_lock:
        if key in _executed:
            return None

    liq = clob_utils.get_orderbook_liquidity(tid)
    if liq < config.B5_REPRICING_MIN_LIQUIDITY:
        logger.info(
            "B5Pro REPRICING descartado: liq=$%.0f < $%.0f",
            liq, config.B5_REPRICING_MIN_LIQUIDITY,
        )
        return None

    edge = round(divergence - _fee(entry_price), 4)
    if edge <= 0:
        return None

    logger.info(
        "SINAL B5Pro REPRICING | %s | side=%s entry=%.3f fair=%.3f edge=%.2f%% lat=%sms",
        asset, side, entry_price, fair_prob, edge * 100,
        f"{latency_ms:.0f}" if latency_ms else "?",
    )
    return B5ProSignal(
        strategy    = "REPRICING",
        market_id   = mid,
        question    = info["question"],
        asset       = asset,
        side        = side,
        entry_price = round(entry_price, 4),
        edge        = edge,
        size_usd    = config.B5_TRADE_SIZE_USD,
        end_time    = info["end_time"],
        slug        = info["slug"],
        fair_prob   = round(fair_prob, 4),
        poly_price  = round(entry_price, 4),
        latency_ms  = latency_ms,
    )


# ── WebSocket CLOB ────────────────────────────────────────────────────────────

async def _ws_clob(token_ids: list[str], stop: threading.Event) -> None:
    """Conecta ao CLOB WS e atualiza _clob_ask em tempo real."""
    backoff = 5
    while not stop.is_set():
        try:
            async with _websockets.connect(_CLOB_WS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps({"assets_ids": token_ids, "type": "market"}))
                logger.info("B5Pro WS CLOB: subscrito a %d tokens", len(token_ids))
                backoff = 5

                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    except asyncio.TimeoutError:
                        continue

                    try:
                        events = json.loads(raw)
                        if not isinstance(events, list):
                            events = [events]
                        for ev in events:
                            if ev.get("event_type") != "price_change":
                                continue
                            tid  = ev.get("asset_id", "")
                            side = ev.get("side", "").upper()
                            p    = ev.get("price", "")
                            if tid and side == "ASK" and p:
                                try:
                                    _update_clob_ask(tid, float(p))
                                except ValueError:
                                    pass
                                continue
                            for chg in ev.get("changes", []):
                                if chg.get("side", "").upper() == "ASK":
                                    try:
                                        _update_clob_ask(tid, float(chg["price"]))
                                    except (KeyError, ValueError):
                                        pass
                    except (json.JSONDecodeError, TypeError):
                        pass

        except Exception as e:
            if not stop.is_set():
                logger.warning("B5Pro WS CLOB: %s | retry em %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


# ── WebSocket Binance ─────────────────────────────────────────────────────────

async def _ws_binance(stop: threading.Event) -> None:
    """
    Binance aggTrade WS — atualiza _binance_price e _vol_window em tempo real.
    Rastreia preço de abertura do candle atual para pct_change.
    """
    streams = "/".join(f"{sym}@aggTrade" for sym in _BINANCE_SYMBOLS.values())
    url     = f"{_BINANCE_WS_URL}/{streams}"
    backoff = 5

    # asset → (candle_bucket_id, open_price)
    _candle_open: dict[str, tuple[int, float]] = {}

    while not stop.is_set():
        try:
            async with _websockets.connect(url, ping_interval=20) as ws:
                logger.info("B5Pro WS Binance: conectado (%s)", streams)
                backoff = 5

                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    except asyncio.TimeoutError:
                        continue

                    try:
                        msg  = json.loads(raw)
                        data = msg.get("data", msg)        # combined stream wrapper
                        sym  = data.get("s", "").upper()   # e.g. "BTCUSDT"
                        p    = float(data.get("p", 0))     # trade price
                        ts   = data.get("T", 0) / 1000.0   # epoch seconds

                        asset = next(
                            (a for a, s in _BINANCE_SYMBOLS.items() if s.upper() == sym),
                            None,
                        )
                        if asset is None or p <= 0:
                            continue

                        # Candle bucket: cada 5 minutos (300s)
                        bucket = int(ts // 300)
                        prev   = _candle_open.get(asset)

                        if prev is None or prev[0] != bucket:
                            # Novo candle: guarda retorno do anterior para vol
                            if prev is not None:
                                ret = (p - prev[1]) / prev[1]
                                with _vol_lock:
                                    _vol_window[asset].append(ret)
                            _candle_open[asset] = (bucket, p)

                        open_p = _candle_open[asset][1]

                        with _binance_lock:
                            _binance_price[asset] = {
                                "price": p,
                                "open":  open_p,
                                "ts":    ts,
                            }

                    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                        pass

        except Exception as e:
            if not stop.is_set():
                logger.warning("B5Pro WS Binance: %s | retry em %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


# ── Loop principal assíncrono ─────────────────────────────────────────────────

async def _main_loop(stop: threading.Event, started: threading.Event) -> None:
    """
    Loop único: atualiza mercados a cada B5_SCAN_INTERVAL, mantém WS vivos,
    roda as 3 detecções a cada segundo sobre todos os mercados vigiados.
    """
    token_ids:   list[str]     = []
    last_fetch:  float         = 0.0
    clob_task:   asyncio.Task | None  = None
    binance_task: asyncio.Task | None = None

    started.set()

    while not stop.is_set():
        now = time.monotonic()

        # Atualiza lista de mercados a cada scan_interval
        if now - last_fetch >= config.B5_SCAN_INTERVAL:
            token_ids  = _refresh_watched()
            last_fetch = now

            if token_ids and _HAS_WS:
                if clob_task is None or clob_task.done():
                    clob_task = asyncio.create_task(
                        _ws_clob(token_ids, stop), name="ws-clob-pro"
                    )
                if binance_task is None or binance_task.done():
                    binance_task = asyncio.create_task(
                        _ws_binance(stop), name="ws-binance-pro"
                    )

        # Detecção a cada tick sobre snapshot thread-safe
        with _watch_lock:
            snapshot = dict(_watched)

        for mid, info in snapshot.items():
            for detector in (_detect_arb, _detect_near_res, _detect_repricing):
                try:
                    sig = detector(mid, info)
                    if sig is not None:
                        _signal_queue.put_nowait(sig)
                except Exception as e:
                    logger.warning("B5Pro detector %s: %s", detector.__name__, e)

        await asyncio.sleep(1.0)

    # Limpeza ao parar
    for task in (clob_task, binance_task):
        if task and not task.done():
            task.cancel()


def _loop_thread_fn(stop: threading.Event, started: threading.Event) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_main_loop(stop, started))
    finally:
        loop.close()


def _ensure_loop() -> None:
    """Garante que o loop assíncrono está em execução (lazy-start)."""
    global _loop_thread, _loop_stop, _loop_started
    with _loop_ctrl:
        if _loop_thread and _loop_thread.is_alive():
            return
        _loop_stop    = threading.Event()
        _loop_started = threading.Event()
        _loop_thread  = threading.Thread(
            target=_loop_thread_fn,
            args=(_loop_stop, _loop_started),
            daemon=True, name="thread-B5Pro-loop",
        )
        _loop_thread.start()
    _loop_started.wait(timeout=5)
    logger.info("B5Pro: loop assíncrono iniciado")


# ── API pública ───────────────────────────────────────────────────────────────

def scan() -> list[B5ProSignal]:
    """
    Garante que o loop interno está rodando e drena a fila de sinais.
    O loop detecta a cada 1s internamente; main.py pode chamar a cada B5_SCAN_INTERVAL.
    """
    _ensure_loop()
    signals: list[B5ProSignal] = []
    while not _signal_queue.empty():
        try:
            signals.append(_signal_queue.get_nowait())
        except queue.Empty:
            break
    if signals:
        logger.info("B5Pro: %d sinal(is) drenados da fila", len(signals))
    return signals


def execute(signal: B5ProSignal, client) -> bool:
    """
    Deduplica, registra no tracker (CSV + Sheets) e notifica Telegram.
    O campo "module" no CSV encoda a estratégia: B5_ARB, B5_NEAR_RES, B5_REPRICING.
    """
    # Chave de dedup consistente com o detector
    if signal.strategy == "NEAR_RES":
        key = _exec_key(signal.market_id, f"NEAR_RES_{signal.side}")
    else:
        key = _exec_key(signal.market_id, signal.strategy)

    with _exec_lock:
        if key in _executed:
            return False
        _executed.add(key)

    if config.MODE == "dry_run":
        tail_tag = " ⚠️ TAIL_RISK" if signal.tail_risk else ""
        extra = ""
        if signal.strategy == "REPRICING" and signal.fair_prob is not None:
            extra = (
                f" | fair={signal.fair_prob:.3f} poly={signal.poly_price:.3f}"
                + (f" lat={signal.latency_ms:.0f}ms" if signal.latency_ms else "")
            )

        logger.info(
            "[DRY-RUN] B5Pro %s | %s | side=%s edge=%.2f%%%s%s",
            signal.strategy, signal.asset,
            signal.side, signal.edge * 100, extra, tail_tag,
        )

        # Questão formatada para Telegram — estratégia, edge e flag de risco visíveis
        question = (
            f"[{signal.strategy}]{tail_tag} {signal.asset} "
            f"edge={signal.edge*100:.1f}%{extra}: {signal.question}"
        )

        # Módulo encoda a estratégia no CSV/Sheets (ex: "B5_ARB")
        module   = f"B5_{signal.strategy}"
        n_markets = 2 if signal.strategy == "ARB" else 1

        tracker.record_signal(
            module,
            signal.market_id,
            question[:100],
            signal.side,
            signal.entry_price,
            signal.edge,
            closes_at_iso = signal.end_time.isoformat(),
            market_slug   = signal.slug,
            n_markets     = n_markets,
        )
        return True

    logger.warning("B5Pro: execução live não implementada (%s)", signal.market_id)
    return False
