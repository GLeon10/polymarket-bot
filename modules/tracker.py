"""
Tracker de sinais em dry-run.
Grava sinais em CSV, verifica resoluções via CLOB e calcula P&L hipotético.

Arquivos:
  data/trades/signals.csv  — um registro por sinal emitido
  data/trades/resolved.csv — um registro por mercado resolvido
"""

import csv
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

from config import config
from modules import notifier, sheets

logger = logging.getLogger("tracker")

_BRT = timezone(timedelta(hours=-3))


def _now_brt() -> str:
    return datetime.now(_BRT).strftime("%Y-%m-%d %H:%M BRT")


def _to_brt(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(_BRT).strftime("%Y-%m-%d %H:%M BRT")
    except ValueError:
        return iso or ""


_SIGNALS_PATH  = Path(__file__).parent.parent / "data" / "trades" / "signals.csv"
_RESOLVED_PATH = Path(__file__).parent.parent / "data" / "trades" / "resolved.csv"
_PHASE_PATH    = Path(__file__).parent.parent / "data" / "trades" / "phase.json"

_POLY_BASE = "https://polymarket.com/event"

_SIGNALS_HEADER = [
    "timestamp", "module", "market_id", "question", "url",
    "side", "entry_price", "edge", "size_usd", "shares", "pnl_est_usd",
    "closes_at", "n_markets",
]
_RESOLVED_HEADER = [
    "market_id", "question", "module", "side",
    "entry_price", "size_usd", "shares",
    "resolution",       # "yes" | "no"
    "pnl_usd",
    "resolved_at",
]

def _banca_b() -> float:
    """Lê o valor da banca B do phase.json (evita import circular com phase_manager)."""
    try:
        return json.loads(_PHASE_PATH.read_text(encoding="utf-8")).get("banca_b", 0.0)
    except Exception:
        return 0.0


def _trade_size(module: str) -> float:
    """Tamanho hipotético por operação, respeitando as fases."""
    if module == "A":
        return min(config.A_TRADE_SIZE_USD, config.CAPITAL_A * 0.10)
    if module == "CORR":
        return config.A_TRADE_SIZE_USD   # arbitragem pura, mesmo porte que A
    if module.startswith("B5"):
        return config.B5_TRADE_SIZE_USD  # B5, B5_ARB, B5_NEAR_RES, B5_REPRICING
    if module == "B3":
        return config.B3_TRADE_SIZE_USD
    if module == "B4":
        return config.B4_TRADE_SIZE_USD
    banca = _banca_b()
    if banca <= 0:
        return config.MAX_TRADE_B_USD  # fallback para testes
    if module == "B1":
        return min(config.MAX_TRADE_B_USD, banca * config.BANCA_B_ALLOC_B1)
    if module == "B2":
        return min(config.MAX_TRADE_B_USD, banca * config.BANCA_B_ALLOC_B2)
    return config.MAX_TRADE_B_USD


def _ensure_dirs():
    _SIGNALS_PATH.parent.mkdir(parents=True, exist_ok=True)


def _append(path: Path, header: list[str], row: dict):
    _ensure_dirs()
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if write_header:
            w.writeheader()
        w.writerow(row)


def _read_csv(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── API pública ───────────────────────────────────────────────────────────────

def record_signal(module: str, market_id: str, question: str,
                  side: str, entry_price: float, edge: float,
                  closes_at_iso: str | None = None,
                  market_slug: str | None = None,
                  n_markets: int = 1):
    """
    Registra um sinal emitido.
    edge = spread (módulo A) ou divergência (B1/B2).
    Ignora se o market_id já foi registrado (dedup por mercado).
    """
    existing = _read_csv(_SIGNALS_PATH)
    if any(r.get("market_id") == market_id for r in existing):
        logger.debug("TRACKER | Já registrado, ignorando: %s", market_id)
        return

    size   = _trade_size(module)
    shares = round(size / entry_price, 4) if entry_price > 0 else 0

    # Estimated P&L: arb modules lock in the edge; directional wins if side hits
    _is_arb = module in ("A", "CORR") or "+" in side
    pnl_est = round(size * edge, 4) if _is_arb else round(shares * (1.0 - entry_price), 4)

    url = f"{_POLY_BASE}/{market_slug}" if market_slug else ""

    row = {
        "timestamp":   _now_brt(),
        "module":      module,
        "market_id":   market_id,
        "question":    question[:100],
        "url":         url,
        "side":        side,
        "entry_price": round(entry_price, 4),
        "edge":        round(edge, 4),
        "size_usd":    round(size, 2),
        "shares":      shares,
        "pnl_est_usd": pnl_est,
        "closes_at":   _to_brt(closes_at_iso),
        "n_markets":   n_markets,
    }
    _append(_SIGNALS_PATH, _SIGNALS_HEADER, row)
    sheets.append_signal(row)
    logger.info(
        "TRACKER | %s | %s | side=%s price=%.3f edge=%.1f%% size=$%.2f",
        module, question[:50], side, entry_price, edge * 100, size,
    )
    notifier.notify_signal(
        module=module,
        question=question,
        side=side,
        entry_price=entry_price,
        edge=edge,
        size_usd=size,
        closes_at=row["closes_at"],
        url=url,
        n_markets=n_markets,
    )


def check_resolutions():
    """
    Verifica quais mercados sinalizados já resolveram.
    Chama o CLOB para cada market_id aberto e registra o resultado.
    """
    signals  = _read_csv(_SIGNALS_PATH)
    resolved = _read_csv(_RESOLVED_PATH)
    resolved_ids = {r["market_id"] for r in resolved}

    open_signals = [s for s in signals if s["market_id"] not in resolved_ids]
    if not open_signals:
        return

    logger.info("TRACKER | Verificando %d sinais em aberto…", len(open_signals))

    for signal in open_signals:
        market_id = signal["market_id"]
        try:
            resp = requests.get(
                f"{config.CLOB_BASE_URL}/markets/{market_id}",
                timeout=10,
            )
            if resp.status_code != 200:
                continue
            market = resp.json()
        except requests.RequestException:
            continue

        if not market.get("closed") and not market.get("resolved"):
            continue

        # Descobre qual token venceu
        tokens   = market.get("tokens", [])
        winner   = next((t for t in tokens if t.get("winner")), None)
        if not winner:
            continue

        resolution    = winner.get("outcome", "").lower()   # "yes" ou "no"
        side          = signal["side"].lower()
        entry_price   = float(signal["entry_price"])
        size_usd      = float(signal["size_usd"])
        shares        = float(signal["shares"])
        edge          = float(signal.get("edge", 0))
        module        = signal["module"]

        # Arbitrage trades (A, CORR, or any side with "+" like "Yes+No"):
        # the edge is locked in regardless of which outcome wins.
        # Directional trades: win if resolution matches the traded side.
        is_arb = module in ("A", "CORR") or "+" in side
        if is_arb:
            pnl = round(size_usd * edge, 4)
        else:
            # Normalise: signal side may be "Yes" or "No"; resolution is "yes"/"no"
            if resolution == side.lower():
                pnl = round(shares * (1.0 - entry_price), 4)
            else:
                pnl = round(-size_usd, 4)

        row = {
            "market_id":   market_id,
            "question":    signal["question"],
            "module":      module,
            "side":        signal["side"],
            "entry_price": signal["entry_price"],
            "size_usd":    signal["size_usd"],
            "shares":      signal["shares"],
            "resolution":  resolution,
            "pnl_usd":     pnl,
            "resolved_at": _now_brt(),
        }
        _append(_RESOLVED_PATH, _RESOLVED_HEADER, row)
        sheets.append_resolved(row)
        logger.info(
            "TRACKER | Resolvido: %s | side=%s res=%s pnl=$%.2f",
            signal["question"][:50], signal["side"], resolution, pnl,
        )


def get_module_pnl(module: str) -> float:
    """P&L total resolvido de um módulo."""
    resolved = _read_csv(_RESOLVED_PATH)
    return sum(float(r["pnl_usd"]) for r in resolved if r["module"] == module)


def get_module_resolved_count(module: str) -> int:
    """Número de operações resolvidas de um módulo."""
    resolved = _read_csv(_RESOLVED_PATH)
    return sum(1 for r in resolved if r["module"] == module)


def get_banca_b_monthly_pnl() -> float:
    """P&L de B1+B2 no mês corrente (horário BRT)."""
    current_month = datetime.now(_BRT).strftime("%Y-%m")
    resolved = _read_csv(_RESOLVED_PATH)
    return sum(
        float(r["pnl_usd"]) for r in resolved
        if r["module"] in ("B1", "B2")
        and r.get("resolved_at", "")[:7] == current_month
    )


def print_summary():
    """Imprime resumo de performance hipotética por módulo."""
    from config import config as _cfg

    signals  = _read_csv(_SIGNALS_PATH)
    resolved = _read_csv(_RESOLVED_PATH)

    total_signals  = len(signals)
    total_resolved = len(resolved)
    open_count     = total_signals - total_resolved

    signals_by_module:  dict[str, list[dict]]  = {}
    resolved_by_module: dict[str, list[float]] = {}
    for s in signals:
        signals_by_module.setdefault(s["module"], []).append(s)
    for r in resolved:
        resolved_by_module.setdefault(r["module"], []).append(float(r["pnl_usd"]))

    # Lê estado da fase
    try:
        phase_state = json.loads(_PHASE_PATH.read_text(encoding="utf-8"))
    except Exception:
        phase_state = {"phase": 1, "banca_b": 0.0, "b1_active": False,
                       "b2_active": False, "b_paused": False}

    banca_b    = phase_state.get("banca_b", 0.0)
    phase      = phase_state.get("phase", 1)
    b1_capital = banca_b * _cfg.BANCA_B_ALLOC_B1
    b2_capital = banca_b * _cfg.BANCA_B_ALLOC_B2

    capital_map = {
        "A":  (_cfg.CAPITAL_A, min(50.0, _cfg.CAPITAL_A * 0.10)),
        "B1": (b1_capital, min(_cfg.MAX_TRADE_B_USD, b1_capital) if b1_capital > 0 else _cfg.MAX_TRADE_B_USD),
        "B2": (b2_capital, min(_cfg.MAX_TRADE_B_USD, b2_capital) if b2_capital > 0 else _cfg.MAX_TRADE_B_USD),
    }

    logger.info("=" * 60)
    logger.info("RESUMO DRY-RUN | %s | FASE %d", _now_brt(), phase)
    logger.info("  Capital total: $%.0f | A: $%.0f | Reserva: $%.0f",
                _cfg.TOTAL_CAPITAL, _cfg.CAPITAL_A, _cfg.CAPITAL_RSRV)
    if phase >= 2:
        paused = " [PAUSADA]" if phase_state.get("b_paused") else ""
        logger.info("  Banca B: $%.2f%s | B1: $%.2f | B2: $%.2f",
                    banca_b, paused, b1_capital, b2_capital)
    else:
        a_pnl   = get_module_pnl("A")
        faltam  = max(0.0, _cfg.PHASE2_TRIGGER_USD - a_pnl)
        logger.info("  Fase 2 ativa quando A acumular $%.0f | A P&L atual: $%.2f | Faltam: $%.2f",
                    _cfg.PHASE2_TRIGGER_USD, a_pnl, faltam)
    logger.info("  Sinais: %d total | %d resolvidos | %d em aberto",
                total_signals, total_resolved, open_count)
    logger.info("-" * 60)

    total_pnl      = 0.0
    total_deployed = 0.0

    for mod in ("A", "B1", "B2"):
        capital_alocado, por_op = capital_map[mod]
        pnls     = resolved_by_module.get(mod, [])
        n_sinais = len(signals_by_module.get(mod, []))
        deployed = n_sinais * por_op

        if not pnls:
            logger.info(
                "  Módulo %-2s | capital $%.2f | por op $%.2f | "
                "%d sinal(is) | sem resoluções ainda",
                mod, capital_alocado, por_op, n_sinais,
            )
            total_deployed += deployed
            continue

        mod_pnl  = sum(pnls)
        wins     = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls) * 100
        roi      = (mod_pnl / deployed * 100) if deployed > 0 else 0.0

        logger.info(
            "  Módulo %-2s | capital $%.2f | por op $%.2f | "
            "%d op(s) | %d ganhos (%.0f%%) | P&L $%.2f | ROI %.1f%%",
            mod, capital_alocado, por_op,
            len(pnls), wins, win_rate, mod_pnl, roi,
        )
        total_pnl      += mod_pnl
        total_deployed += deployed

    logger.info("-" * 60)
    roi_total = (total_pnl / total_deployed * 100) if total_deployed > 0 else 0.0
    logger.info("  TOTAL | P&L hipotético $%.2f | ROI %.2f%%", total_pnl, roi_total)
    logger.info("=" * 60)
