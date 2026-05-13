"""
Módulo CORR — Scanner de inconsistências entre mercados correlacionados.

Detecta quando a precificação de mercados relacionados é matematicamente
impossível, criando oportunidades de arbitragem cross-market.

Padrões detectados:
  1. OVERSUM  — soma de YES prices de outcomes exclusivos > 100%
  2. UNDERSUM — soma de YES prices de outcomes exaustivos < 97%
  3. SUBSET   — P(A) > P(A ou B) sendo ambos candidatos do mesmo evento

Cada sinal bruto passa por 4 filtros de qualidade pós-detecção:
  F1 — Fee viável: fee total < 50% do spread bruto
  F2 — Liquidez mínima: $100 no lado negociado
  F3 — Exaustividade real: anti-falso-positivo (soma < 50%, outcomes cumulativos, >10 candidatos)
  F4 — Regras de resolução: mesma fonte em todos os mercados do grupo

Ciclo: 10 minutos.
"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

import requests

from config import config
from modules import clob_utils, tracker

logger = logging.getLogger("scanner_corr")

# Extrai "Will [team] win [event]?" — grupo 1 = time, grupo 2 = evento
_WIN_RE = re.compile(r"Will\s+(.+?)\s+win\s+(.+?)[\?\.]*$", re.IGNORECASE)

# Extrai "Will [A] or [B] win [event]?" — grupo 1 = A, grupo 2 = B, grupo 3 = evento
_OR_RE = re.compile(
    r"Will\s+(.+?)\s+or\s+(.+?)\s+win\s+(.+?)[\?\.]*$", re.IGNORECASE
)

# Tags que indicam fee 0% por categoria
_GEO_TAGS = {"Geopolitics", "World"}

# Outcomes cumulativos — não são mutuamente exclusivos
_CUMULATIVE_RE = re.compile(
    r"\b(at least|pelo menos|more than|at most|fewer than|≥)\b", re.IGNORECASE
)

_SOURCE_KEYWORDS = [
    "associated press", "ap news", "reuters", "official results",
    "official website", "uma optimistic oracle", "polymarket resolution",
    "the new york times", "bbc", "espn",
]

_rules_cache: dict[str, str] = {}

_CORR_TRADE_SIZE_USD = 50.0


@dataclass
class CorrSignal:
    pattern:     str          # "oversum" | "undersum" | "subset"
    description: str
    questions:   list[str]    # perguntas dos mercados envolvidos
    prices:      list[float]  # preços YES de cada mercado
    spread:      float        # spread bruto (pré-fee); atualizado para líquido após validação
    pnl_est:     float        # P&L hipotético em USD ($50 de capital)
    market_ids:   list[str] = field(default_factory=list)
    market_slugs: list[str] = field(default_factory=list)
    found_at:     datetime  = field(default_factory=datetime.utcnow)


def _yes_price(market: dict) -> float:
    for t in market.get("tokens", []):
        if t.get("outcome", "").lower() == "yes":
            return float(t.get("price", 0))
    return 0.0


def _group_by_event(markets: list[dict]) -> dict[str, list[dict]]:
    """Agrupa mercados com padrão 'Will X win [Event]?' pelo mesmo evento."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for m in markets:
        match = _WIN_RE.search(m.get("question", ""))
        if not match:
            continue
        team  = match.group(1).strip().lower()
        event = match.group(2).strip().lower()
        if " or " not in team:
            groups[event].append(m)
    return groups


# ── Filtros de qualidade pós-detecção ────────────────────────────────────────

def _fee_for_market(market: dict) -> float:
    """Fee de um mercado: 0 para categorias geo/world, senão taker_base_fee do dict."""
    tags = set(market.get("tags") or [])
    if tags & _GEO_TAGS:
        return 0.0
    return float(market.get("taker_base_fee", 0))


def _filter_fee(sig: CorrSignal, markets: list[dict]) -> tuple[bool, str, float]:
    """
    F1: fee total ponderada pelo lado negociado < 50% do spread bruto.
    Retorna (ok, motivo, fee_value).
    """
    oversum     = sig.pattern == "oversum"
    side_prices = [1.0 - p for p in sig.prices] if oversum else list(sig.prices)
    total       = sum(side_prices)
    if total == 0:
        return False, "sem preços válidos", 0.0
    weighted_fee = sum(sp * _fee_for_market(m) for m, sp in zip(markets, side_prices)) / total
    limit        = sig.spread * 0.5
    if weighted_fee > limit:
        return False, f"fee={weighted_fee:.2%} > 50% × spread={sig.spread:.2%}", weighted_fee
    return True, f"fee={weighted_fee:.2%}", weighted_fee


def _filter_liquidity(sig: CorrSignal, markets: list[dict]) -> tuple[bool, str]:
    """
    F2: liquidez mínima no lado negociado de cada mercado.
    OVERSUM → NO; UNDERSUM → YES; SUBSET → YES no [0] (A ou B), NO no [1] (A).
    """
    for i, m in enumerate(markets):
        if sig.pattern == "oversum":
            outcome = "No"
        elif sig.pattern == "undersum":
            outcome = "Yes"
        else:
            outcome = "Yes" if i == 0 else "No"

        tok = clob_utils.token_by_outcome(m, outcome)
        if not tok:
            return False, f"sem token {outcome} em {m.get('condition_id', '')[:12]}"
        liq = clob_utils.get_orderbook_liquidity(tok["token_id"])
        if liq < config.CORR_MIN_LIQUIDITY:
            return False, f"liq=${liq:.0f} < ${config.CORR_MIN_LIQUIDITY:.0f} em '{m.get('question', '')[:40]}'"
    return True, "liquidez ok"


def _filter_exhaustiveness(sig: CorrSignal) -> tuple[bool, str]:
    """
    F3: anti-falso-positivo.
    UNDERSUM: soma < 50% → faltam candidatos.
    OVERSUM:  >10 candidatos (fee inviável) ou outcomes cumulativos.
    """
    if sig.pattern == "undersum":
        total = sum(sig.prices)
        if total < 0.50:
            return False, f"soma={total:.2%} < 50% — provável grupo incompleto"
    elif sig.pattern == "oversum":
        n = len(sig.market_ids)
        if n > 10:
            return False, f"{n} candidatos — fee combinada inviável (>10)"
        for q in sig.questions:
            if _CUMULATIVE_RE.search(q):
                return False, f"outcome cumulativo: '{q[:50]}'"
    return True, "exaustividade ok"


def _fetch_rules(condition_id: str) -> str:
    if condition_id in _rules_cache:
        return _rules_cache[condition_id]
    try:
        resp = requests.get(f"{config.CLOB_BASE_URL}/markets/{condition_id}", timeout=10)
        if resp.status_code != 200:
            _rules_cache[condition_id] = ""
            return ""
        data = resp.json()
        text = data.get("description", "") or data.get("rules", "") or ""
        _rules_cache[condition_id] = text
        return text
    except requests.RequestException:
        _rules_cache[condition_id] = ""
        return ""


def _extract_source(text: str) -> str:
    lower = text.lower()
    for kw in _SOURCE_KEYWORDS:
        if kw in lower:
            return kw
    m = re.search(r"resolution source[:\s]+([^\n.]{5,60})", lower)
    if m:
        return m.group(1).strip()
    m = re.search(r"resolv\w+[^.]{0,80}", lower)
    if m:
        return m.group(0).strip()[:60]
    return ""


def _filter_resolution(sig: CorrSignal) -> tuple[bool, str]:
    """F4: todos os mercados do grupo têm a mesma fonte de resolução."""
    sources: set[str] = set()
    for mid in sig.market_ids:
        text = _fetch_rules(mid)
        if not text:
            return False, f"regras não disponíveis para {mid[:12]}"
        src = _extract_source(text)
        if not src:
            return False, f"fonte não identificada em {mid[:12]}"
        sources.add(src)
    if len(sources) > 1:
        return False, f"fontes divergentes: {' | '.join(sorted(sources))}"
    return True, f"fonte: {next(iter(sources))}"


def _validate(signals: list[CorrSignal], market_by_id: dict[str, dict]) -> list[CorrSignal]:
    """
    Aplica os 4 filtros em sequência. Loga descarte por filtro em debug,
    e emite resumo de funil em info. Retorna apenas sinais válidos com spread líquido.
    """
    counts = {"raw": len(signals), "f1": 0, "f2": 0, "f3": 0, "f4": 0, "valid": 0}
    valid: list[CorrSignal] = []

    for sig in signals:
        markets = [market_by_id[mid] for mid in sig.market_ids if mid in market_by_id]
        if len(markets) != len(sig.market_ids):
            logger.debug("[CORR] Descartado — mercado ausente no índice: %s", sig.description[:60])
            continue

        # F1 — Fee
        ok, reason, fee_val = _filter_fee(sig, markets)
        if not ok:
            logger.debug("[CORR] F1(fee) ✗ %s | %s", reason, sig.description[:60])
            continue
        counts["f1"] += 1

        # F2 — Liquidez
        ok, reason = _filter_liquidity(sig, markets)
        if not ok:
            logger.debug("[CORR] F2(liq) ✗ %s | %s", reason, sig.description[:60])
            continue
        counts["f2"] += 1

        # F3 — Exaustividade
        ok, reason = _filter_exhaustiveness(sig)
        if not ok:
            logger.debug("[CORR] F3(exaust) ✗ %s | %s", reason, sig.description[:60])
            continue
        counts["f3"] += 1

        # F4 — Resolução
        ok, reason = _filter_resolution(sig)
        if not ok:
            logger.debug("[CORR] F4(resolucao) ✗ %s | %s", reason, sig.description[:60])
            continue
        counts["f4"] += 1
        counts["valid"] += 1

        # Atualiza spread para valor líquido (após fee)
        sig.spread  = round(sig.spread - fee_val, 4)
        sig.pnl_est = round(sig.spread * _CORR_TRADE_SIZE_USD, 2)
        valid.append(sig)

    logger.info(
        "[CORR] %d brutos → F1(fee):%d → F2(liq):%d → F3(exaust):%d → F4(rules):%d → %d válido(s)",
        counts["raw"], counts["f1"], counts["f2"], counts["f3"], counts["f4"], counts["valid"],
    )
    return valid


def scan() -> list[CorrSignal]:
    _rules_cache.clear()

    all_markets  = clob_utils.fetch_sampling_markets()
    market_by_id = {m["condition_id"]: m for m in all_markets if "condition_id" in m}
    signals: list[CorrSignal] = []

    # ── Padrões 1 e 2: OVERSUM / UNDERSUM por evento ─────────────────────────
    groups = _group_by_event(all_markets)

    for event, group in groups.items():
        if len(group) < 2:
            continue

        prices = [_yes_price(m) for m in group]
        if not all(0 < p < 1 for p in prices):
            continue

        total = sum(prices)
        n     = len(group)

        if total > 1.0 + config.CORR_MIN_SPREAD:
            spread = round(total - 1.0, 4)
            signals.append(CorrSignal(
                pattern     = "oversum",
                description = (
                    f"Soma YES = {total:.2%} para '{event}' ({n} candidatos) — "
                    f"comprar NO em todos garante lucro"
                ),
                questions  = [m.get("question", "")[:80] for m in group],
                prices     = [round(p, 4) for p in prices],
                spread     = spread,
                pnl_est    = round(spread * _CORR_TRADE_SIZE_USD, 2),
                market_ids   = [m["condition_id"] for m in group],
                market_slugs = [m.get("market_slug", "") for m in group],
            ))

        elif total < 1.0 - config.CORR_MIN_SPREAD and n == 2:
            spread = round(1.0 - total, 4)
            signals.append(CorrSignal(
                pattern     = "undersum",
                description = (
                    f"Soma YES = {total:.2%} para '{event}' (2 candidatos) — "
                    f"comprar YES em ambos garante lucro"
                ),
                questions  = [m.get("question", "")[:80] for m in group],
                prices     = [round(p, 4) for p in prices],
                spread     = spread,
                pnl_est    = round(spread * _CORR_TRADE_SIZE_USD, 2),
                market_ids   = [m["condition_id"] for m in group],
                market_slugs = [m.get("market_slug", "") for m in group],
            ))

    # ── Padrão 3: SUBSET — P(A) > P(A ou B) ─────────────────────────────────
    individual: dict[tuple[str, str], dict] = {}
    for m in all_markets:
        match = _WIN_RE.search(m.get("question", ""))
        if not match:
            continue
        team  = match.group(1).strip().lower()
        event = match.group(2).strip().lower()
        if " or " not in team:
            individual[(team, event)] = m

    for market in all_markets:
        match = _OR_RE.search(market.get("question", ""))
        if not match:
            continue
        team_a = match.group(1).strip().lower()
        team_b = match.group(2).strip().lower()
        event  = match.group(3).strip().lower()
        p_ab   = _yes_price(market)
        if p_ab <= 0:
            continue

        for team in (team_a, team_b):
            m_ind = individual.get((team, event))
            if not m_ind:
                continue
            p_ind = _yes_price(m_ind)
            if p_ind <= 0:
                continue

            spread = round(p_ind - p_ab, 4)
            if spread < config.CORR_MIN_SPREAD:
                continue

            signals.append(CorrSignal(
                pattern     = "subset",
                description = (
                    f"P({team}) = {p_ind:.2%} > "
                    f"P({team_a} ou {team_b}) = {p_ab:.2%} — "
                    f"subconjunto precificado acima do superconjunto"
                ),
                questions  = [
                    market.get("question", "")[:80],
                    m_ind.get("question", "")[:80],
                ],
                prices     = [round(p_ab, 4), round(p_ind, 4)],
                spread     = spread,
                pnl_est    = round(spread * _CORR_TRADE_SIZE_USD, 2),
                market_ids   = [market["condition_id"], m_ind["condition_id"]],
                market_slugs = [market.get("market_slug", ""), m_ind.get("market_slug", "")],
            ))

    logger.info("[CORR] %d mercados | %d brutos detectados — aplicando filtros…",
                len(all_markets), len(signals))
    return _validate(signals, market_by_id)


def execute(signal: CorrSignal, client) -> bool:
    if config.MODE == "dry_run":
        logger.info(
            ">> SINAL CORR | %s | spread_líq=%.2f%% | P&L=$%.2f",
            signal.pattern.upper(), signal.spread * 100, signal.pnl_est,
        )
        for q, p in zip(signal.questions, signal.prices):
            logger.info("   YES=%.3f | %s", p, q[:70])
        logger.info("   %s", signal.description)

        # Lado e preço médio de entrada dependem do padrão
        if signal.pattern == "oversum":
            side        = "No"
            entry_price = sum(1.0 - p for p in signal.prices) / len(signal.prices)
        elif signal.pattern == "undersum":
            side        = "Yes"
            entry_price = sum(signal.prices) / len(signal.prices)
        else:  # subset
            side        = "Yes/No"
            entry_price = signal.prices[0]

        first_slug = signal.market_slugs[0] if signal.market_slugs else ""
        event_slug = clob_utils.get_event_slug(first_slug) if first_slug else ""

        tracker.record_signal(
            "CORR",
            signal.market_ids[0] if signal.market_ids else "unknown",
            signal.description[:100],
            side,
            round(entry_price, 4),
            signal.spread,
            market_slug=event_slug or first_slug,
            n_markets=len(signal.market_ids),
        )
        return True
    logger.warning("Execução live CORR não implementada")
    return False
