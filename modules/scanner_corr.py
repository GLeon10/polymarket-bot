"""
Módulo CORR — Scanner de inconsistências entre mercados correlacionados.

Detecta quando a precificação de mercados relacionados é matematicamente
impossível, criando oportunidades de arbitragem cross-market.

Padrões detectados:
  1. OVERSUM  — soma de YES prices de outcomes exclusivos > 100%
               → comprar NO em todos garante lucro independente do resultado
  2. UNDERSUM — soma de YES prices de outcomes exaustivos < 97%
               → comprar YES em todos garante lucro independente do resultado
  3. SUBSET   — P(A) > P(A ou B) sendo ambos candidatos do mesmo evento
               → matematicamente impossível (subconjunto > superconjunto)

Ciclo: 10 minutos (janelas duram horas, velocidade não é crítica).
"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from config import config
from modules import clob_utils

logger = logging.getLogger("scanner_corr")

# Extrai "Will [team] win [event]?" — grupo 1 = time, grupo 2 = evento
_WIN_RE = re.compile(r"Will\s+(.+?)\s+win\s+(.+?)[\?\.]*$", re.IGNORECASE)

# Extrai "Will [A] or [B] win [event]?" — grupo 1 = A, grupo 2 = B, grupo 3 = evento
_OR_RE = re.compile(
    r"Will\s+(.+?)\s+or\s+(.+?)\s+win\s+(.+?)[\?\.]*$", re.IGNORECASE
)


@dataclass
class CorrSignal:
    pattern:     str          # "oversum" | "undersum" | "subset"
    description: str
    questions:   list[str]    # perguntas dos mercados envolvidos
    prices:      list[float]  # preços YES de cada mercado
    spread:      float        # % de lucro estimado
    pnl_est:     float        # P&L hipotético em USD
    found_at:    datetime = field(default_factory=datetime.utcnow)


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
        # Exclui linhas "or" (tratadas separadamente)
        if " or " not in team:
            groups[event].append(m)
    return groups


def scan() -> list[CorrSignal]:
    all_markets = clob_utils.fetch_sampling_markets()
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
            # Comprar NO em todos → um deles sempre perde mas os outros pagam $1
            # Lucro = soma_yes - 1.0 (por unidade investida)
            spread  = round(total - 1.0, 4)
            pnl_est = round(spread * config.MAX_TRADE_B_USD, 2)
            sig = CorrSignal(
                pattern     = "oversum",
                description = (
                    f"Soma YES = {total:.2%} para '{event}' ({n} candidatos) — "
                    f"comprar NO em todos garante lucro"
                ),
                questions = [m.get("question", "")[:80] for m in group],
                prices    = [round(p, 4) for p in prices],
                spread    = spread,
                pnl_est   = pnl_est,
            )
            signals.append(sig)

        elif total < 1.0 - config.CORR_MIN_SPREAD and n == 2:
            # Apenas 2 candidatos → muito provável ser exaustivo
            # Comprar YES em ambos → um sempre paga $1
            spread  = round(1.0 - total, 4)
            pnl_est = round(spread * config.MAX_TRADE_B_USD, 2)
            sig = CorrSignal(
                pattern     = "undersum",
                description = (
                    f"Soma YES = {total:.2%} para '{event}' (2 candidatos) — "
                    f"comprar YES em ambos garante lucro"
                ),
                questions = [m.get("question", "")[:80] for m in group],
                prices    = [round(p, 4) for p in prices],
                spread    = spread,
                pnl_est   = pnl_est,
            )
            signals.append(sig)

    # ── Padrão 3: SUBSET — P(A) > P(A ou B) ─────────────────────────────────
    # Indexa mercados individuais: (time_lower, evento_lower) → market
    individual: dict[tuple[str, str], dict] = {}
    for m in all_markets:
        match = _WIN_RE.search(m.get("question", ""))
        if not match:
            continue
        team  = match.group(1).strip().lower()
        event = match.group(2).strip().lower()
        if " or " not in team:
            individual[(team, event)] = m

    # Procura mercados "A ou B"
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

            pnl_est = round(spread * config.MAX_TRADE_B_USD, 2)
            sig = CorrSignal(
                pattern     = "subset",
                description = (
                    f"P({team}) = {p_ind:.2%} > "
                    f"P({team_a} ou {team_b}) = {p_ab:.2%} — "
                    f"subconjunto precificado acima do superconjunto"
                ),
                questions = [
                    market.get("question", "")[:80],
                    m_ind.get("question", "")[:80],
                ],
                prices  = [round(p_ab, 4), round(p_ind, 4)],
                spread  = spread,
                pnl_est = pnl_est,
            )
            signals.append(sig)

    logger.info("[CORR] %d mercados | %d inconsistência(s) (baixa confiança, sem ação)",
                len(all_markets), len(signals))
    return signals


def execute(signal: CorrSignal, client) -> bool:
    if config.MODE == "dry_run":
        return True
    logger.warning("Execução live CORR não implementada")
    return False
