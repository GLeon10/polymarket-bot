"""
Controle de risco: livro-caixa, exposição aberta, limites de perda e kill switch.

O bot não tinha nada disso. `_trade_size()` devolvia um valor fixo sem olhar
posições abertas — 20 sinais alocavam $1.000 numa conta de $500 — e o único
stop-loss existente cobria apenas B1/B2, deixando A (90% do capital), CORR,
B3, B4 e todo o B5 sem limite algum.

Aqui todo módulo passa pelo mesmo portão antes de abrir posição:

    ok, motivo = risk.check_capacity("A", custo_usd)

Camadas, da mais branda para a mais dura:
  1. caixa disponível      — equity − reserva − exposição aberta
  2. teto por operação     — MAX_TRADE_PCT do capital
  3. teto por módulo       — MAX_MODULE_EXPOSURE_PCT
  4. teto de exposição     — MAX_OPEN_EXPOSURE_PCT + MAX_CONCURRENT_POSITIONS
  5. perda diária/mensal   — bloqueia novas entradas no período
  6. drawdown              — MAX_DRAWDOWN_PCT arma o kill switch

O kill switch é persistente e só sai com intervenção explícita
(`python -m modules.risk reset`), de propósito: se o bot se destravasse
sozinho, não seria um stop.
"""

import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config import config

logger = logging.getLogger("risk")

_BRT         = timezone(timedelta(hours=-3))
_LEDGER_PATH = Path(__file__).parent.parent / "data" / "trades" / "ledger.json"
_lock        = threading.RLock()

# Tolerancia de comparacao em dolares. Os custos das pernas passam por
# arredondamento de shares e de fee, o que deixa o total alguns centesimos de
# centavo fora do orcamento. Com tolerancia de 1e-9, uma cesta dimensionada
# exatamente no teto por operacao era recusada por ruido de ponto flutuante —
# e nenhum sinal passava. Um decimo de centavo e folga de arredondamento, nao
# afrouxamento de limite.
_TOL = 1e-4

_DEFAULT: dict = {
    "open":               {},     # signal_id -> {module, cost_usd, opened_at}
    "realized_by_day":    {},     # "YYYY-MM-DD" -> pnl
    "realized_by_month":  {},     # "YYYY-MM"    -> pnl
    "realized_by_module": {},     # module       -> pnl
    "realized_total":     0.0,
    "peak_equity":        None,   # inicializado com TOTAL_CAPITAL
    "kill_switch":        {"active": False, "reason": "", "at": ""},
}


# -- Persistência -------------------------------------------------------------

def _today() -> str:
    return datetime.now(_BRT).strftime("%Y-%m-%d")


def _month() -> str:
    return datetime.now(_BRT).strftime("%Y-%m")


def _load() -> dict:
    if not _LEDGER_PATH.exists():
        state = json.loads(json.dumps(_DEFAULT))
        state["peak_equity"] = config.TOTAL_CAPITAL
        return state
    try:
        state = json.loads(_LEDGER_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("risk: ledger corrompido — recriando do zero")
        state = json.loads(json.dumps(_DEFAULT))
    for k, v in _DEFAULT.items():
        state.setdefault(k, json.loads(json.dumps(v)))
    if state.get("peak_equity") is None:
        state["peak_equity"] = config.TOTAL_CAPITAL
    return state


def _save(state: dict) -> None:
    _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _LEDGER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_LEDGER_PATH)


# -- Leitura de estado --------------------------------------------------------

def open_exposure(state: dict | None = None, module: str | None = None) -> float:
    s = state if state is not None else _load()
    return round(sum(
        p["cost_usd"] for p in s["open"].values()
        if module is None or p.get("module") == module
    ), 4)


def equity(state: dict | None = None) -> float:
    """Capital total + P&L realizado."""
    s = state if state is not None else _load()
    return round(config.TOTAL_CAPITAL + s["realized_total"], 4)


def available_cash(state: dict | None = None) -> float:
    """Caixa livre para novas entradas (já descontadas reserva e exposição)."""
    s = state if state is not None else _load()
    reserve = config.TOTAL_CAPITAL * config.ALLOC_RSRV
    return round(equity(s) - reserve - open_exposure(s), 4)


def drawdown(state: dict | None = None) -> float:
    """Queda percentual desde o pico de equity. 0.0 = no topo."""
    s    = state if state is not None else _load()
    peak = s.get("peak_equity") or config.TOTAL_CAPITAL
    if peak <= 0:
        return 0.0
    return max(0.0, round((peak - equity(s)) / peak, 4))


def is_tripped(state: dict | None = None) -> bool:
    s = state if state is not None else _load()
    return bool(s["kill_switch"]["active"])


def get_state() -> dict:
    return _load()


# -- Kill switch --------------------------------------------------------------

def trip(reason: str) -> None:
    """Arma o kill switch. Bloqueia toda nova entrada até reset manual."""
    with _lock:
        state = _load()
        if state["kill_switch"]["active"]:
            return
        state["kill_switch"] = {
            "active": True,
            "reason": reason,
            "at":     datetime.now(_BRT).strftime("%Y-%m-%d %H:%M BRT"),
        }
        _save(state)
    logger.critical("KILL SWITCH ARMADO | %s | nenhuma nova posicao sera aberta", reason)
    try:
        from modules import notifier
        notifier.send(
            "<b>[KILL SWITCH]</b>\n" + reason + "\n\nBot bloqueado para novas entradas."
        )
    except Exception:
        pass


def reset_kill_switch() -> None:
    """Desarma o kill switch. Intencionalmente manual."""
    with _lock:
        state = _load()
        state["kill_switch"] = {"active": False, "reason": "", "at": ""}
        _save(state)
    logger.warning("KILL SWITCH desarmado manualmente")


# -- Portão de entrada --------------------------------------------------------

def check_capacity(module: str, cost_usd: float) -> tuple[bool, str]:
    """
    Decide se uma posição de `cost_usd` pode ser aberta agora.
    Retorna (permitido, motivo_da_recusa).
    """
    with _lock:
        state = _load()

    if state["kill_switch"]["active"]:
        return False, "kill switch armado: " + state["kill_switch"]["reason"]

    if cost_usd <= 0:
        return False, "custo nao positivo"

    eq = equity(state)

    dd = drawdown(state)
    if dd >= config.MAX_DRAWDOWN_PCT:
        trip("drawdown %.1f%% >= limite %.1f%%" % (dd * 100, config.MAX_DRAWDOWN_PCT * 100))
        return False, "drawdown %.1f%%" % (dd * 100)

    day_pnl = state["realized_by_day"].get(_today(), 0.0)
    day_cap = -(config.DAILY_LOSS_LIMIT_PCT * eq)
    if day_pnl <= day_cap:
        return False, "perda diaria $%.2f atingiu o limite $%.2f" % (day_pnl, day_cap)

    month_pnl = state["realized_by_month"].get(_month(), 0.0)
    month_cap = -(config.MONTHLY_LOSS_LIMIT_PCT * eq)
    if month_pnl <= month_cap:
        return False, "perda mensal $%.2f atingiu o limite $%.2f" % (month_pnl, month_cap)

    max_trade = config.MAX_TRADE_PCT * eq
    if cost_usd > max_trade + _TOL:
        return False, "operacao $%.2f > teto por operacao $%.2f" % (cost_usd, max_trade)

    if len(state["open"]) >= config.MAX_CONCURRENT_POSITIONS:
        return False, "%d posicoes abertas (max %d)" % (
            len(state["open"]), config.MAX_CONCURRENT_POSITIONS)

    mod_exp = open_exposure(state, module)
    mod_cap = config.MAX_MODULE_EXPOSURE_PCT * eq
    if mod_exp + cost_usd > mod_cap + _TOL:
        return False, "exposicao do modulo %s $%.2f > teto $%.2f" % (
            module, mod_exp + cost_usd, mod_cap)

    total_exp = open_exposure(state)
    total_cap = config.MAX_OPEN_EXPOSURE_PCT * eq
    if total_exp + cost_usd > total_cap + _TOL:
        return False, "exposicao total $%.2f > teto $%.2f" % (total_exp + cost_usd, total_cap)

    cash = available_cash(state)
    if cost_usd > cash + _TOL:
        return False, "caixa insuficiente: $%.2f disponivel, $%.2f necessario" % (cash, cost_usd)

    return True, ""


def open_position(signal_id: str, module: str, cost_usd: float) -> bool:
    """Registra capital comprometido. Idempotente por signal_id."""
    with _lock:
        state = _load()
        if signal_id in state["open"]:
            return False
        state["open"][signal_id] = {
            "module":    module,
            "cost_usd":  round(cost_usd, 4),
            "opened_at": datetime.now(_BRT).strftime("%Y-%m-%d %H:%M BRT"),
        }
        _save(state)
    return True


def close_position(signal_id: str, pnl_usd: float, module: str | None = None) -> None:
    """Libera o capital e contabiliza o P&L realizado nos acumuladores."""
    with _lock:
        state = _load()
        entry = state["open"].pop(signal_id, None)
        mod   = module or (entry or {}).get("module", "?")

        day, month = _today(), _month()
        state["realized_by_day"][day] = round(
            state["realized_by_day"].get(day, 0.0) + pnl_usd, 4)
        state["realized_by_month"][month] = round(
            state["realized_by_month"].get(month, 0.0) + pnl_usd, 4)
        state["realized_by_module"][mod] = round(
            state["realized_by_module"].get(mod, 0.0) + pnl_usd, 4)
        state["realized_total"] = round(state["realized_total"] + pnl_usd, 4)

        eq = config.TOTAL_CAPITAL + state["realized_total"]
        if eq > (state.get("peak_equity") or config.TOTAL_CAPITAL):
            state["peak_equity"] = round(eq, 4)

        _save(state)

    dd = drawdown()
    if dd >= config.MAX_DRAWDOWN_PCT:
        trip("drawdown %.1f%% >= limite %.1f%%" % (dd * 100, config.MAX_DRAWDOWN_PCT * 100))


def summary_lines() -> list[str]:
    """Linhas prontas para o resumo periódico do tracker."""
    s  = _load()
    ks = s["kill_switch"]
    lines = [
        "Equity $%.2f | caixa livre $%.2f | exposto $%.2f em %d posicao(oes)" % (
            equity(s), available_cash(s), open_exposure(s), len(s["open"])),
        "Drawdown %.1f%% (pico $%.2f) | dia $%.2f | mes $%.2f" % (
            drawdown(s) * 100, s["peak_equity"],
            s["realized_by_day"].get(_today(), 0.0),
            s["realized_by_month"].get(_month(), 0.0)),
    ]
    if ks["active"]:
        lines.append("KILL SWITCH ARMADO desde %s — %s" % (ks["at"], ks["reason"]))
    return lines


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        reset_kill_switch()
        print("Kill switch desarmado.")
    else:
        for line in summary_lines():
            print(line)
