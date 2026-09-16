"""
Tracker de sinais: registro, contabilidade real e P&L.

O que mudou em relação à versão anterior, e por quê:

  Antes, para qualquer trade marcado como arbitragem (A, CORR, B3, B4, B5
  "Yes+No"), o P&L resolvido era `size_usd * edge` — o edge teórico do momento
  da detecção, independentemente do que o mercado de fato resolveu. Nenhum
  desses módulos conseguia registrar prejuízo. Win rate 100% por construção,
  gatilho de Fase 2 alimentado por lucro fabricado e stop-loss cego.

  Agora todo sinal carrega suas PERNAS explícitas (mercado, lado, preço,
  shares, fee). Na resolução, cada perna é consultada no CLOB e o payout é o
  que ela realmente pagou:

      pnl = Σ(shares da perna vencedora) − custo_total_pago

  Uma arbitragem mal formada — pernas que não eram mutuamente exclusivas,
  faixa de valores não exaustiva, perna que não preencheu — aparece como
  prejuízo, que é o ponto.

Arquivos:
  data/trades/signals.csv  — um registro por sinal emitido (com legs_json)
  data/trades/resolved.csv — um registro por sinal resolvido (P&L real)
  data/trades/ledger.json  — caixa, exposição e limites (modules/risk.py)
"""

import csv
import json
import logging
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

from config import config
from modules import fees, notifier, risk, sheets

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
    "signal_id", "timestamp", "module", "market_id", "question", "url",
    "side", "entry_price", "edge", "spread", "size_usd", "shares", "pnl_est_usd",
    "cost_usd", "fee_usd", "legs_json", "closes_at", "n_markets",
]
_RESOLVED_HEADER = [
    "signal_id", "market_id", "question", "module", "side",
    "entry_price", "size_usd", "shares",
    "resolution",       # outcome vencedor por perna, separado por "|"
    "cost_usd", "fee_usd", "payout_usd", "pnl_usd", "roi_pct",
    "result",           # "WIN" | "LOSS" | "FLAT"
    "edge", "spread", "edge_realizado",
    "resolved_at",
]


# ── Pernas ────────────────────────────────────────────────────────────────────

@dataclass
class Leg:
    """Uma perna executável de um sinal."""
    market_id: str
    outcome:   str      # "Yes" | "No" — o lado COMPRADO
    price:     float    # preço de execução por share (já com slippage estimado)
    shares:    float
    fee_usd:   float
    token_id:  str = ""  # necessário para execução real; vazio em dry-run puro

    @property
    def cost_usd(self) -> float:
        return self.price * self.shares + self.fee_usd


def build_legs(specs: list[tuple], budget_usd: float,
               fee_rate: float | None = None) -> list[Leg]:
    """
    Monta pernas com o MESMO número de shares a partir de um orçamento.

    Share count igual é o que torna a arbitragem válida: em qualquer resultado
    a cesta paga `shares` × (número de pernas vencedoras). Dimensionar por
    "$X por perna" — como o código antigo fazia — desbalanceia a cesta e
    destrói a garantia.

    specs aceita 3 a 5 elementos por perna:
        (market_id, outcome, preço)
        (market_id, outcome, preço, token_id)
        (market_id, outcome, preço, token_id, fee_rate)

    A taxa por perna existe porque cada mercado do Polymarket publica a sua em
    `taker_base_fee` — aplicar a taxa de crypto (7.2%) a um mercado de evento
    que cobra zero inventaria um custo que não existe.
    """
    norm = []
    for spec in specs:
        mid, outcome, price = spec[0], spec[1], spec[2]
        token_id = spec[3] if len(spec) > 3 else ""
        rate     = spec[4] if len(spec) > 4 else fee_rate
        if mid and 0.0 < price < 1.0:
            norm.append((mid, outcome, price, token_id, rate))

    if not norm or budget_usd <= 0:
        return []

    unit_cost = sum(fees.effective_price(p, r) for _, _, p, _, r in norm)
    if unit_cost <= 0:
        return []

    # Arredonda o número de shares PARA BAIXO. Com `round`, o custo montado
    # podia ficar alguns centésimos de centavo acima do orçamento — o bastante
    # para o portão de risco recusar uma cesta dimensionada exatamente no teto
    # por operação, e nenhum sinal passar.
    shares = math.floor((budget_usd / unit_cost) * 1e6) / 1e6
    if shares <= 0:
        return []

    return [
        Leg(
            market_id = mid,
            outcome   = outcome,
            price     = round(price, 6),
            shares    = shares,
            fee_usd   = round(fees.fee_usd(price, shares, rate), 6),
            token_id  = token_id,
        )
        for mid, outcome, price, token_id, rate in norm
    ]


def _legs_to_json(legs: list[Leg]) -> str:
    return json.dumps([asdict(l) for l in legs], separators=(",", ":"))


def _legs_from_json(raw: str) -> list[Leg]:
    if not raw:
        return []
    try:
        return [Leg(**d) for d in json.loads(raw)]
    except (json.JSONDecodeError, TypeError, ValueError):
        return []


# ── Helpers de arquivo ────────────────────────────────────────────────────────

def _fmt_spread(edge: float) -> str:
    return f"{edge:.4f} ({edge * 100:g}% de spread)"


def _banca_b() -> float:
    """Lê o valor da banca B do phase.json (evita import circular com phase_manager)."""
    try:
        return json.loads(_PHASE_PATH.read_text(encoding="utf-8")).get("banca_b", 0.0)
    except Exception:
        return 0.0


def _trade_size(module: str) -> float:
    """
    Teto nominal por operação do módulo. É apenas o ponto de partida:
    `risk.check_capacity` ainda pode recusar ou o chamador reduzir.
    """
    if module == "A":
        nominal = min(config.A_TRADE_SIZE_USD, config.CAPITAL_A * 0.10)
    elif module == "CORR":
        nominal = config.A_TRADE_SIZE_USD
    elif module.startswith("B5"):
        nominal = config.B5_TRADE_SIZE_USD
    elif module == "B3":
        nominal = config.B3_TRADE_SIZE_USD
    elif module == "B4":
        nominal = config.B4_TRADE_SIZE_USD
    else:
        banca = _banca_b()
        if banca <= 0:
            nominal = config.MAX_TRADE_B_USD
        elif module == "B1":
            nominal = min(config.MAX_TRADE_B_USD, banca * config.BANCA_B_ALLOC_B1)
        elif module == "B2":
            nominal = min(config.MAX_TRADE_B_USD, banca * config.BANCA_B_ALLOC_B2)
        else:
            nominal = config.MAX_TRADE_B_USD

    # Nunca acima do teto global por operação
    return min(nominal, config.MAX_TRADE_PCT * risk.equity())


def trade_budget(module: str) -> float:
    """
    Orçamento que o módulo deve usar para montar a cesta.

    Os módulos precisam dimensionar as pernas com ESTE valor, e não com o
    `*_TRADE_SIZE_USD` do config: o teto por operação do `risk` é menor, e uma
    cesta montada acima dele seria recusada no portão — silenciosamente, sinal
    após sinal, sem nenhum trade jamais acontecer.
    """
    return _trade_size(module)


def _ensure_dirs():
    _SIGNALS_PATH.parent.mkdir(parents=True, exist_ok=True)


def _append(path: Path, header: list[str], row: dict):
    """
    Anexa uma linha, recusando-se a escrever sobre um cabeçalho diferente.

    Sem essa checagem, um CSV gravado pelo esquema antigo receberia linhas na
    ordem de colunas nova sob o cabeçalho velho — os dados ficariam deslocados
    coluna a coluna, sem nenhum erro visível. Melhor parar e pedir a migração.
    """
    _ensure_dirs()
    write_header = not path.exists() or path.stat().st_size == 0

    if not write_header:
        with path.open(encoding="utf-8") as f:
            atual = next(csv.reader(f), [])
        if atual and atual != header:
            raise RuntimeError(
                f"{path.name} está no esquema antigo ({len(atual)} colunas; "
                f"o atual tem {len(header)}). Rode: python migrate_csv_schema.py"
            )

    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)


def _read_csv(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if any((v or "").strip() for v in r.values())]


def _signal_id(module: str, market_id: str) -> str:
    return f"{module}|{market_id}"


# ── API pública ───────────────────────────────────────────────────────────────

def record_signal(module: str, market_id: str, question: str,
                  side: str, entry_price: float, edge: float,
                  closes_at_iso: str | None = None,
                  market_slug: str | None = None,
                  n_markets: int = 1,
                  legs: list[Leg] | None = None,
                  fee_rate: float | None = None) -> bool:
    """
    Registra um sinal, se o risco permitir.

    `legs` é o que torna a contabilidade auditável. Módulos de arbitragem devem
    sempre passá-lo (via `build_legs`). Sem ele, o sinal é tratado como uma
    posição direcional de uma perna só, montada a partir de side/entry_price.

    Retorna True se o sinal foi registrado; False se foi recusado.
    """
    sid = _signal_id(module, market_id)

    if any(r.get("signal_id") == sid for r in _read_csv(_SIGNALS_PATH)):
        logger.debug("TRACKER | Já registrado, ignorando: %s", sid)
        return False

    budget = _trade_size(module)

    if legs is None:
        outcome = "No" if side.strip().lower().startswith("no") else "Yes"
        legs    = build_legs([(market_id, outcome, entry_price)], budget, fee_rate)

    if not legs:
        logger.warning("TRACKER | %s sem pernas válidas — sinal descartado (%s)", module, sid)
        return False

    cost_usd = round(sum(l.cost_usd for l in legs), 4)
    fee_paid = round(sum(l.fee_usd for l in legs), 4)

    ok, reason = risk.check_capacity(module, cost_usd)
    if not ok:
        logger.warning("TRACKER | RECUSADO pelo risco | %s | $%.2f | %s", sid, cost_usd, reason)
        return False

    # Payout mínimo garantido da cesta: número de pernas que vencem no pior caso.
    # Para arbitragem bem formada isso é ≥ 1 × shares; para direcional é 0.
    shares_total = round(sum(l.shares for l in legs), 4)
    pnl_est      = round(cost_usd * edge, 4)

    url = f"{_POLY_BASE}/{market_slug}" if market_slug else ""

    row = {
        "signal_id":   sid,
        "timestamp":   _now_brt(),
        "module":      module,
        "market_id":   market_id,
        "question":    question[:100],
        "url":         url,
        "side":        side,
        "entry_price": round(entry_price, 4),
        "edge":        round(edge, 4),
        "spread":      _fmt_spread(edge),
        "size_usd":    round(cost_usd, 2),
        "shares":      shares_total,
        "pnl_est_usd": pnl_est,
        "cost_usd":    cost_usd,
        "fee_usd":     fee_paid,
        "legs_json":   _legs_to_json(legs),
        "closes_at":   _to_brt(closes_at_iso),
        "n_markets":   n_markets,
    }
    _append(_SIGNALS_PATH, _SIGNALS_HEADER, row)
    risk.open_position(sid, module, cost_usd)
    sheets.append_signal(row)

    logger.info(
        "TRACKER | %s | %s | side=%s price=%.3f edge=%.1f%% custo=$%.2f fee=$%.2f pernas=%d",
        module, question[:50], side, entry_price, edge * 100, cost_usd, fee_paid, len(legs),
    )
    notifier.notify_signal(
        module=module,
        question=question,
        side=side,
        entry_price=entry_price,
        edge=edge,
        size_usd=cost_usd,
        closes_at=row["closes_at"],
        url=url,
        n_markets=n_markets,
    )
    return True


def _winning_outcome(market_id: str) -> str | None:
    """
    Outcome vencedor de um mercado, ou None se ainda não resolveu.
    Levanta nada: falha de rede é tratada como "ainda não resolvido".
    """
    try:
        resp = requests.get(f"{config.CLOB_BASE_URL}/markets/{market_id}", timeout=10)
        if resp.status_code != 200:
            return None
        market = resp.json()
    except (requests.RequestException, ValueError):
        return None

    if not market.get("closed") and not market.get("resolved"):
        return None

    winner = next((t for t in market.get("tokens", []) if t.get("winner")), None)
    if not winner:
        return None
    return (winner.get("outcome") or "").strip().lower()


def check_resolutions():
    """
    Resolve sinais abertos consultando CADA perna no CLOB e calculando o
    payout real da cesta. Sinais com alguma perna ainda aberta são pulados.
    """
    signals  = _read_csv(_SIGNALS_PATH)
    resolved = _read_csv(_RESOLVED_PATH)
    done_ids = {r.get("signal_id") or r.get("market_id") for r in resolved}

    open_signals = [
        s for s in signals
        if (s.get("signal_id") or _signal_id(s["module"], s["market_id"])) not in done_ids
    ]
    if not open_signals:
        return

    logger.info("TRACKER | Verificando %d sinais em aberto…", len(open_signals))

    for signal in open_signals:
        module = signal["module"]
        sid    = signal.get("signal_id") or _signal_id(module, signal["market_id"])
        legs   = _legs_from_json(signal.get("legs_json", ""))

        if not legs:
            # Sinal legado, gravado antes da contabilidade por pernas.
            outcome = "No" if signal["side"].strip().lower().startswith("no") else "Yes"
            try:
                price  = float(signal["entry_price"])
                shares = float(signal["shares"])
            except (KeyError, ValueError):
                continue
            legs = [Leg(signal["market_id"], outcome, price, shares,
                        fees.fee_usd(price, shares))]

        outcomes  = {}
        pendentes = False
        for leg in legs:
            if leg.market_id not in outcomes:
                res = _winning_outcome(leg.market_id)
                if res is None:
                    pendentes = True
                    break
                outcomes[leg.market_id] = res
        if pendentes:
            continue

        cost_usd   = round(sum(l.cost_usd for l in legs), 4)
        fee_paid   = round(sum(l.fee_usd for l in legs), 4)
        payout_usd = round(sum(
            l.shares for l in legs
            if l.outcome.strip().lower() == outcomes[l.market_id]
        ), 4)
        pnl     = round(payout_usd - cost_usd, 4)
        roi     = round(pnl / cost_usd * 100, 2) if cost_usd > 0 else 0.0
        edge    = float(signal.get("edge", 0) or 0)
        realizado = round(pnl / cost_usd, 4) if cost_usd > 0 else 0.0

        row = {
            "signal_id":      sid,
            "market_id":      signal["market_id"],
            "question":       signal["question"],
            "module":         module,
            "side":           signal["side"],
            "entry_price":    signal["entry_price"],
            "size_usd":       signal.get("size_usd", cost_usd),
            "shares":         signal.get("shares", ""),
            "resolution":     "|".join(f"{m}:{o}" for m, o in outcomes.items())[:200],
            "cost_usd":       cost_usd,
            "fee_usd":        fee_paid,
            "payout_usd":     payout_usd,
            "pnl_usd":        pnl,
            "roi_pct":        roi,
            "result":         "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "FLAT"),
            "edge":           round(edge, 4),
            "spread":         _fmt_spread(edge),
            "edge_realizado": realizado,
            "resolved_at":    _now_brt(),
        }
        _append(_RESOLVED_PATH, _RESOLVED_HEADER, row)
        risk.close_position(sid, pnl, module)
        sheets.append_resolved(row)

        # Divergência entre o edge prometido e o realizado é o sinal mais
        # honesto de que um detector está errado. Vale um WARNING.
        if edge > 0 and realizado < edge - 0.02:
            logger.warning(
                "TRACKER | %s | edge prometido %.2f%% vs realizado %.2f%% | %s",
                module, edge * 100, realizado * 100, signal["question"][:50],
            )

        logger.info(
            "TRACKER | Resolvido: %s | %s | custo=$%.2f payout=$%.2f pnl=$%.2f (%.1f%%)",
            signal["question"][:50], row["result"], cost_usd, payout_usd, pnl, roi,
        )


def get_module_pnl(module: str) -> float:
    """P&L total resolvido de um módulo."""
    return sum(
        float(r.get("pnl_usd", 0) or 0)
        for r in _read_csv(_RESOLVED_PATH) if r["module"] == module
    )


def get_module_resolved_count(module: str) -> int:
    """Número de operações resolvidas de um módulo."""
    return sum(1 for r in _read_csv(_RESOLVED_PATH) if r["module"] == module)


def get_banca_b_monthly_pnl() -> float:
    """P&L de B1+B2 no mês corrente (horário BRT)."""
    current_month = datetime.now(_BRT).strftime("%Y-%m")
    return sum(
        float(r.get("pnl_usd", 0) or 0) for r in _read_csv(_RESOLVED_PATH)
        if r["module"] in ("B1", "B2")
        and r.get("resolved_at", "")[:7] == current_month
    )


def print_summary():
    """Resumo de performance por módulo, com números realizados de verdade."""
    from config import config as _cfg

    signals  = _read_csv(_SIGNALS_PATH)
    resolved = _read_csv(_RESOLVED_PATH)

    signals_by_module:  dict[str, list[dict]] = {}
    resolved_by_module: dict[str, list[dict]] = {}
    for s in signals:
        signals_by_module.setdefault(s["module"], []).append(s)
    for r in resolved:
        resolved_by_module.setdefault(r["module"], []).append(r)

    try:
        phase_state = json.loads(_PHASE_PATH.read_text(encoding="utf-8"))
    except Exception:
        phase_state = {"phase": 1, "banca_b": 0.0, "b1_active": False,
                       "b2_active": False, "b_paused": False}

    banca_b = phase_state.get("banca_b", 0.0)
    phase   = phase_state.get("phase", 1)

    modo = "LIVE" if _cfg.MODE == "live" else "DRY-RUN"
    logger.info("=" * 68)
    logger.info("RESUMO %s | %s | FASE %d", modo, _now_brt(), phase)
    for line in risk.summary_lines():
        logger.info("  %s", line)
    if phase >= 2:
        paused = " [PAUSADA]" if phase_state.get("b_paused") else ""
        logger.info("  Banca B: $%.2f%s | B1: $%.2f | B2: $%.2f",
                    banca_b, paused,
                    banca_b * _cfg.BANCA_B_ALLOC_B1, banca_b * _cfg.BANCA_B_ALLOC_B2)
    else:
        a_pnl = get_module_pnl("A")
        logger.info("  Fase 2 quando A acumular $%.0f | A P&L real: $%.2f | Faltam: $%.2f",
                    _cfg.PHASE2_TRIGGER_USD, a_pnl,
                    max(0.0, _cfg.PHASE2_TRIGGER_USD - a_pnl))
    logger.info("  Sinais: %d total | %d resolvidos | %d em aberto",
                len(signals), len(resolved), len(signals) - len(resolved))
    logger.info("-" * 68)

    canonical = ["A", "B1", "B2", "B3", "B4", "B5", "B5_ARB",
                 "B5_NEAR_RES", "B5_REPRICING", "CORR"]
    seen, all_mods = set(), []
    for m in canonical + list(signals_by_module) + list(resolved_by_module):
        if m in seen:
            continue
        if m in signals_by_module or m in resolved_by_module:
            all_mods.append(m)
            seen.add(m)

    total_pnl = total_cost = 0.0
    for mod in all_mods:
        rows     = resolved_by_module.get(mod, [])
        n_sinais = len(signals_by_module.get(mod, []))

        if not rows:
            logger.info("  Módulo %-12s | %d sinal(is) | sem resoluções ainda", mod, n_sinais)
            continue

        pnls   = [float(r.get("pnl_usd", 0) or 0) for r in rows]
        costs  = [float(r.get("cost_usd", 0) or 0) for r in rows]
        mod_pnl, mod_cost = sum(pnls), sum(costs)
        wins     = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls) * 100
        roi      = (mod_pnl / mod_cost * 100) if mod_cost > 0 else 0.0
        pior     = min(pnls)

        logger.info(
            "  Módulo %-12s | %d op(s) | %d ganho(s) (%.0f%%) | capital $%.2f | "
            "P&L $%.2f | ROI %.2f%% | pior $%.2f",
            mod, len(pnls), wins, win_rate, mod_cost, mod_pnl, roi, pior,
        )
        total_pnl  += mod_pnl
        total_cost += mod_cost

    logger.info("-" * 68)
    roi_total = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0
    logger.info("  TOTAL | capital girado $%.2f | P&L $%.2f | ROI %.2f%%",
                total_cost, total_pnl, roi_total)
    logger.info("=" * 68)
