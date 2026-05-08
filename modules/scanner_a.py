"""
Módulo A — Scanner de arbitragem combinatória entre mercados correlacionados.

Detecta inconsistências lógicas entre mercados relacionados no Polymarket:

  Tipo 1 — Soma de partes (Will X win Y? | ranges exclusivos)
  Tipo 2 — Subconjunto/ordering:
            - P(A) > P(A ou B): subconjunto acima do superconjunto
            - P(above high) > P(above low): threshold violado
            - P(by earlier_date) > P(by later_date): data aninhada violada
  Tipo 3 — Implicação direta (win title implica qualify for final)

Toda oportunidade passa por verificação de regras de resolução antes de ser sinalizada.
Sinal é descartado silenciosamente se as fontes de resolução divergirem.

Ciclo: 10 minutos.
"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from config import config
from modules import clob_utils, rule_validator, tracker

logger = logging.getLogger("scanner_a")

# ── Padrões de questão ────────────────────────────────────────────────────────

_WIN_RE = re.compile(r"Will\s+(.+?)\s+win\s+(.+?)[\?\.]*$", re.IGNORECASE)
_OR_RE  = re.compile(r"Will\s+(.+?)\s+or\s+(.+?)\s+win\s+(.+?)[\?\.]*$", re.IGNORECASE)

_IMPLICATION_PAIRS = [
    (r"win.+\b(finals?|championship|title|cup)\b",
     r"\b(qualify|reach|advance|make).+\b(finals?|semifinals?)\b"),
    (r"win.+\b(gold|first place)\b",
     r"win.+\b(medal|podium|top.?3)\b"),
    (r"\bbe elected president\b",
     r"win.+\b(primary|nomination)\b"),
]

# Padrões para novos detectores
_ABOVE_NUM_RE  = re.compile(r"\babove\s+\$?([\d,\.]+\s*[BbMmKkTt]?)", re.IGNORECASE)
_BY_DATE_RE    = re.compile(
    r"\bby\s+((?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[\s\d,]+\d{4}|end of \d{4}|\d{4})\b",
    re.IGNORECASE,
)
_RANGE_PART_RE = re.compile(
    r"\b(between\s.+|less\s+than\s.+|greater\s+than\s.+|"
    r"above\s.+|below\s.+|more\s+than\s.+|over\s.+|under\s.+|"
    r"fewer\s+than\s.+|at\s+most\s.+|at\s+least\s.+)$",
    re.IGNORECASE,
)
# Extremo inferior: sem limite abaixo (less than X, below X…)
_RANGE_LOWER_RE = re.compile(
    r"\b(less\s+than|below|under|fewer\s+than|at\s+most)\b", re.IGNORECASE
)
# Extremo superior: sem limite acima (greater than X, above X…)
_RANGE_UPPER_RE = re.compile(
    r"\b(greater\s+than|above|more\s+than|over|at\s+least)\b", re.IGNORECASE
)

_SOURCE_KEYWORDS = [
    "associated press", "ap news", "reuters", "official results",
    "official website", "uma optimistic oracle", "polymarket resolution",
    "the new york times", "bbc", "espn", "fifa", "ioc",
]

_rules_cache: dict[str, str] = {}


# ── Data class de sinal ───────────────────────────────────────────────────────

@dataclass
class ArbSignal:
    signal_type:       int
    description:       str
    market_ids:        list[str]
    questions:         list[str]
    yes_prices:        list[float]
    spread:            float
    min_liquidity:     float
    resolution_ok:     bool
    resolution_notes:  str
    pnl_est:           float
    found_at:          datetime = field(default_factory=datetime.utcnow)
    market_slugs:      list[str] = field(default_factory=list)
    end_dates:         list[str] = field(default_factory=list)
    rule_quality:      str = "LOW"


# ── Utilitários de mercado ────────────────────────────────────────────────────

def _yes_price(market: dict) -> float:
    for t in market.get("tokens", []):
        if t.get("outcome", "").lower() == "yes":
            return float(t.get("price", 0))
    return 0.0


def _within_resolution_days(market: dict, days: int) -> bool:
    end = market.get("end_date_iso") or market.get("endDate") or ""
    if not end:
        return True
    try:
        dt    = datetime.fromisoformat(end.replace("Z", "+00:00"))
        delta = (dt - datetime.now(timezone.utc)).days
        return 0 <= delta <= days
    except ValueError:
        return True


def _get_min_liquidity(market: dict) -> float:
    yes_tok = clob_utils.token_by_outcome(market, "Yes")
    no_tok  = clob_utils.token_by_outcome(market, "No")
    if not yes_tok or not no_tok:
        return 0.0
    return min(
        clob_utils.get_orderbook_liquidity(yes_tok["token_id"]),
        clob_utils.get_orderbook_liquidity(no_tok["token_id"]),
    )


def _filter_candidates(markets: list[dict]) -> list[dict]:
    result = []
    for m in markets:
        if float(m.get("taker_base_fee", 1)) > config.A_MAX_FEE:
            continue
        if not clob_utils.token_by_outcome(m, "Yes") or not clob_utils.token_by_outcome(m, "No"):
            continue
        price = _yes_price(m)
        if not (0 < price < 1):
            continue
        if not _within_resolution_days(m, config.A_MAX_RESOLUTION_DAYS):
            continue
        result.append(m)
    return result


# ── Verificação de regras de resolução ───────────────────────────────────────

def _fetch_resolution_rules(condition_id: str) -> str:
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


def _extract_resolution_source(text: str) -> str:
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


def _check_resolution_rules(market_ids: list[str]) -> tuple[bool, str]:
    sources: dict[str, str] = {}
    for mid in market_ids:
        text = _fetch_resolution_rules(mid)
        if not text:
            return False, f"Regras não disponíveis para {mid[:16]}"
        src = _extract_resolution_source(text)
        if not src:
            return False, f"Fonte não identificada em {mid[:16]}"
        sources[mid] = src
    unique = set(sources.values())
    if len(unique) > 1:
        return False, f"Fontes divergentes: {' | '.join(sorted(unique))}"
    return True, f"Fonte confirmada: {unique.pop()}"


# ── Helpers de agrupamento ────────────────────────────────────────────────────

def _group_by_event(markets: list[dict]) -> dict[str, list[dict]]:
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


def _extract_entity(question: str) -> str:
    m = re.match(r"Will\s+(.+?)\s+(?:win|qualify|reach|advance|be|make)\b",
                 question, re.IGNORECASE)
    return m.group(1).strip().lower() if m else ""


def _stem_threshold(question: str) -> str | None:
    """Chave de grupo para mercados 'X above N': (stem_antes | sufixo_depois)."""
    m = _ABOVE_NUM_RE.search(question)
    if not m:
        return None
    stem   = question[:m.start()].strip().lower()
    suffix = question[m.end():].strip().lower().rstrip("?.")
    return f"{stem}|{suffix}"


def _parse_threshold_value(text: str) -> float | None:
    text = text.strip().replace(",", "")
    m = re.match(r"([\d\.]+)\s*([BbMmKkTt]?)", text)
    if not m:
        return None
    try:
        num  = float(m.group(1))
        mult = {"b": 1e9, "m": 1e6, "k": 1e3, "t": 1e12, "": 1.0}.get(
            m.group(2).lower(), 1.0
        )
        return num * mult
    except ValueError:
        return None


def _stem_date(question: str) -> str | None:
    """Chave de grupo para mercados 'X by [date]': tudo antes do 'by [date]'."""
    m = _BY_DATE_RE.search(question)
    if not m:
        return None
    stem = question[:m.start()].strip().lower()
    return stem if len(stem) >= 10 else None


def _parse_date_value(text: str) -> datetime | None:
    text = text.strip().rstrip("?.")
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y",
                "%B %d,%Y", "%B %Y", "%b %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    m = re.search(r"end of (\d{4})", text, re.IGNORECASE)
    if m:
        return datetime(int(m.group(1)), 12, 31)
    m = re.match(r"^(\d{4})$", text.strip())
    if m:
        return datetime(int(m.group(1)), 12, 31)
    return None


# ── Construtor genérico de ArbSignal ─────────────────────────────────────────

def _make_signal(sig_type: int, description: str, markets: list[dict],
                 prices: list[float], spread: float, min_liq: float,
                 res_ok: bool, res_notes: str) -> ArbSignal:
    return ArbSignal(
        signal_type      = sig_type,
        description      = description,
        market_ids       = [m["condition_id"] for m in markets],
        questions        = [m.get("question", "")[:80] for m in markets],
        yes_prices       = [round(p, 4) for p in prices],
        spread           = spread,
        min_liquidity    = min_liq,
        resolution_ok    = res_ok,
        resolution_notes = res_notes,
        pnl_est          = round(spread * config.A_TRADE_SIZE_USD, 2),
        market_slugs     = [m.get("market_slug", "") for m in markets],
        end_dates        = [m.get("end_date_iso") or m.get("endDate") or "" for m in markets],
    )


# ── Tipo 1a: Soma de partes — "Will X win Y?" ─────────────────────────────────

def _actual_return(gap: float, n: int, oversum: bool) -> float:
    """Retorno real sobre o capital investido para arbitragem Tipo 1."""
    if oversum:
        denom = n - 1 - gap
        return gap / denom if denom > 0 else 0.0
    return gap / (1.0 - gap)


def _fee_cost(markets: list[dict], prices: list[float], oversum: bool) -> float:
    """
    Custo de fee ponderado pelo lado negociado.
    UNDERSUM: compra YES → ponderado pelos preços YES.
    OVERSUM:  compra NO  → ponderado pelos preços NO.
    """
    side_prices = [1.0 - p for p in prices] if oversum else list(prices)
    total = sum(side_prices)
    if total == 0:
        return 0.0
    return sum(
        sp * float(m.get("taker_base_fee", 0))
        for m, sp in zip(markets, side_prices)
    ) / total


def _detect_type1(candidates: list[dict]) -> list[ArbSignal]:
    signals = []
    for event, group in _group_by_event(candidates).items():
        if len(group) < 2:
            continue
        prices = [_yes_price(m) for m in group]
        if not all(0 < p < 1 for p in prices):
            continue
        total    = sum(prices)
        gap      = abs(total - 1.0)
        oversum  = total > 1.0
        n        = len(group)
        ret      = _actual_return(gap, n, oversum)
        fee      = _fee_cost(group, prices, oversum)
        net_ret  = ret - fee
        min_ret  = config.A_MIN_SPREAD + (n - 1) * config.A_SPREAD_PER_MARKET
        if net_ret < min_ret:
            continue
        min_liq = min(_get_min_liquidity(m) for m in group)
        if min_liq < config.A_MIN_LIQUIDITY:
            continue
        direction = "OVERSUM" if oversum else "UNDERSUM"
        action    = "comprar NO em todos" if oversum else "comprar YES em todos"
        res_ok, res_notes = _check_resolution_rules([m["condition_id"] for m in group])
        signals.append(_make_signal(
            1,
            f"Tipo 1 {direction} — '{event}' ({n} candidatos): "
            f"soma YES={total:.2%} → {action}",
            group, prices, round(net_ret, 4), min_liq, res_ok, res_notes,
        ))
    return signals


# ── Tipo 1b: Soma de partes — ranges exclusivos ───────────────────────────────

def _detect_ranges(candidates: list[dict]) -> list[ArbSignal]:
    signals = []
    groups: dict[str, list[dict]] = defaultdict(list)
    for m in candidates:
        q     = m.get("question", "")
        match = _RANGE_PART_RE.search(q)
        if not match:
            continue
        stem = q[:match.start()].strip().lower()
        if len(stem) >= 10:
            groups[stem].append(m)

    for stem, group in groups.items():
        if len(group) < 3:
            continue
        # Conjunto só é exaustivo se tiver extremo inferior E superior
        has_lower = any(_RANGE_LOWER_RE.search(m.get("question", "")) for m in group)
        has_upper = any(_RANGE_UPPER_RE.search(m.get("question", "")) for m in group)
        if not (has_lower and has_upper):
            logger.debug(
                "[A] Ranges descartados — conjunto incompleto (lower=%s upper=%s): %s",
                has_lower, has_upper, stem[:50],
            )
            continue
        prices = [_yes_price(m) for m in group]
        if not all(0 < p < 1 for p in prices):
            continue
        total   = sum(prices)
        gap     = abs(total - 1.0)
        oversum = total > 1.0
        n       = len(group)
        ret     = _actual_return(gap, n, oversum)
        fee     = _fee_cost(group, prices, oversum)
        net_ret = ret - fee
        min_ret = config.A_MIN_SPREAD + (n - 1) * config.A_SPREAD_PER_MARKET
        if net_ret < min_ret:
            continue
        min_liq = min(_get_min_liquidity(m) for m in group)
        if min_liq < config.A_MIN_LIQUIDITY:
            continue
        direction = "OVERSUM" if oversum else "UNDERSUM"
        action    = "comprar NO em todos" if oversum else "comprar YES em todos"
        res_ok, res_notes = _check_resolution_rules([m["condition_id"] for m in group])
        signals.append(_make_signal(
            1,
            f"Tipo 1 RANGE {direction} — '{stem[:50]}' ({n} faixas): "
            f"soma YES={total:.2%} → {action}",
            group, prices, round(net_ret, 4), min_liq, res_ok, res_notes,
        ))
    return signals


# ── Tipo 2a: Subconjunto lógico — P(A) > P(A ou B) ───────────────────────────

def _detect_type2(candidates: list[dict]) -> list[ArbSignal]:
    signals = []
    individual: dict[tuple[str, str], dict] = {}
    for m in candidates:
        match = _WIN_RE.search(m.get("question", ""))
        if not match:
            continue
        team  = match.group(1).strip().lower()
        event = match.group(2).strip().lower()
        if " or " not in team:
            individual[(team, event)] = m

    for market in candidates:
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
            spread = round(p_ind - p_ab, 4)
            if spread < config.A_MIN_SPREAD:
                continue
            min_liq = min(_get_min_liquidity(market), _get_min_liquidity(m_ind))
            if min_liq < config.A_MIN_LIQUIDITY:
                continue
            res_ok, res_notes = _check_resolution_rules(
                [market["condition_id"], m_ind["condition_id"]]
            )
            signals.append(_make_signal(
                2,
                f"Tipo 2 SUBSET — P({team})={p_ind:.2%} > P({team_a} ou {team_b})={p_ab:.2%}",
                [market, m_ind], [p_ab, p_ind], spread, min_liq, res_ok, res_notes,
            ))
    return signals


# ── Tipo 2b: Threshold ordering — P(above low) ≥ P(above high) ──────────────

def _detect_thresholds(candidates: list[dict]) -> list[ArbSignal]:
    signals   = []
    seen_pairs: set[tuple[str, str]] = set()

    groups: dict[str, list[tuple[float, dict]]] = defaultdict(list)
    for m in candidates:
        q    = m.get("question", "")
        stem = _stem_threshold(q)
        if stem is None:
            continue
        match = _ABOVE_NUM_RE.search(q)
        if not match:
            continue
        val = _parse_threshold_value(match.group(1))
        if val is not None:
            groups[stem].append((val, m))

    for stem, entries in groups.items():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda x: x[0])
        for i in range(len(entries) - 1):
            t_low,  m_low  = entries[i]
            t_high, m_high = entries[i + 1]
            p_low  = _yes_price(m_low)
            p_high = _yes_price(m_high)
            if p_low <= 0 or p_high <= 0:
                continue
            spread = round(p_high - p_low, 4)
            if spread < config.A_MIN_SPREAD:
                continue
            pair_key = (
                min(m_low["condition_id"], m_high["condition_id"]),
                max(m_low["condition_id"], m_high["condition_id"]),
            )
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            min_liq = min(_get_min_liquidity(m_low), _get_min_liquidity(m_high))
            if min_liq < config.A_MIN_LIQUIDITY:
                continue
            res_ok, res_notes = _check_resolution_rules(
                [m_low["condition_id"], m_high["condition_id"]]
            )
            signals.append(_make_signal(
                2,
                f"Tipo 2 THRESHOLD — P(above {t_high:.4g})={p_high:.2%} > "
                f"P(above {t_low:.4g})={p_low:.2%} — barra maior não pode custar mais",
                [m_low, m_high], [p_low, p_high], spread, min_liq, res_ok, res_notes,
            ))
    return signals


# ── Tipo 2c: Date ordering — P(by early) ≤ P(by late) ───────────────────────

def _detect_dates(candidates: list[dict]) -> list[ArbSignal]:
    signals   = []
    seen_pairs: set[tuple[str, str]] = set()

    groups: dict[str, list[tuple[datetime, dict]]] = defaultdict(list)
    for m in candidates:
        q    = m.get("question", "")
        stem = _stem_date(q)
        if not stem:
            continue
        match = _BY_DATE_RE.search(q)
        if not match:
            continue
        dt = _parse_date_value(match.group(1))
        if dt is not None:
            groups[stem].append((dt, m))

    for stem, entries in groups.items():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda x: x[0])
        for i in range(len(entries) - 1):
            dt_early, m_early = entries[i]
            dt_late,  m_late  = entries[i + 1]
            p_early = _yes_price(m_early)
            p_late  = _yes_price(m_late)
            if p_early <= 0 or p_late <= 0:
                continue
            spread = round(p_early - p_late, 4)
            if spread < config.A_MIN_SPREAD:
                continue
            pair_key = (
                min(m_early["condition_id"], m_late["condition_id"]),
                max(m_early["condition_id"], m_late["condition_id"]),
            )
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            min_liq = min(_get_min_liquidity(m_early), _get_min_liquidity(m_late))
            if min_liq < config.A_MIN_LIQUIDITY:
                continue
            res_ok, res_notes = _check_resolution_rules(
                [m_early["condition_id"], m_late["condition_id"]]
            )
            early_str = dt_early.strftime("%b %d")
            late_str  = dt_late.strftime("%b %d")
            signals.append(_make_signal(
                2,
                f"Tipo 2 DATA — P(by {early_str})={p_early:.2%} > "
                f"P(by {late_str})={p_late:.2%} — prazo menor não pode custar mais",
                [m_early, m_late], [p_early, p_late], spread, min_liq, res_ok, res_notes,
            ))
    return signals


# ── Tipo 3: Implicação direta ─────────────────────────────────────────────────

def _detect_type3(candidates: list[dict]) -> list[ArbSignal]:
    signals   = []
    seen_pairs: set[tuple[str, str]] = set()
    for m_strong in candidates:
        q_strong = m_strong.get("question", "")
        p_strong = _yes_price(m_strong)
        entity_s = _extract_entity(q_strong)
        if p_strong <= 0 or not entity_s:
            continue
        for strong_pat, weak_pat in _IMPLICATION_PAIRS:
            if not re.search(strong_pat, q_strong, re.IGNORECASE):
                continue
            for m_weak in candidates:
                if m_weak["condition_id"] == m_strong["condition_id"]:
                    continue
                q_weak   = m_weak.get("question", "")
                entity_w = _extract_entity(q_weak)
                if entity_s != entity_w:
                    continue
                if not re.search(weak_pat, q_weak, re.IGNORECASE):
                    continue
                p_weak = _yes_price(m_weak)
                if p_weak <= 0:
                    continue
                spread = round(p_strong - p_weak, 4)
                if spread < config.A_MIN_SPREAD:
                    continue
                pair_key = (
                    min(m_strong["condition_id"], m_weak["condition_id"]),
                    max(m_strong["condition_id"], m_weak["condition_id"]),
                )
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                min_liq = min(_get_min_liquidity(m_strong), _get_min_liquidity(m_weak))
                if min_liq < config.A_MIN_LIQUIDITY:
                    continue
                res_ok, res_notes = _check_resolution_rules(
                    [m_strong["condition_id"], m_weak["condition_id"]]
                )
                signals.append(_make_signal(
                    3,
                    f"Tipo 3 IMPLICACAO — P({q_strong[:40]})={p_strong:.2%} > "
                    f"P({q_weak[:40]})={p_weak:.2%}",
                    [m_strong, m_weak], [p_strong, p_weak], spread, min_liq, res_ok, res_notes,
                ))
    return signals


# ── Logging de sinal válido ───────────────────────────────────────────────────

def _log_valid(sig: ArbSignal) -> None:
    m       = re.search(r"Tipo \d+\s+(\w+)", sig.description)
    subtype = m.group(1) if m else ""
    logger.info(
        ">> SINAL A | Tipo%d %s | spread=%.2f%% | P&L=$%.2f | liq=$%.0f",
        sig.signal_type, subtype, sig.spread * 100, sig.pnl_est, sig.min_liquidity,
    )
    for q, p, mid in zip(sig.questions, sig.yes_prices, sig.market_ids):
        logger.info("   YES=%.3f | %s [%s]", p, q[:65], mid[:12])
    logger.info("   Resolucao: %s | Qualidade: %s", sig.resolution_notes, sig.rule_quality)


# ── Scan principal ────────────────────────────────────────────────────────────

def scan() -> list[ArbSignal]:
    _rules_cache.clear()

    all_markets = clob_utils.fetch_sampling_markets()
    candidates  = _filter_candidates(all_markets)
    logger.info(
        "Scanner A: %d mercados totais → %d candidatos (fee≤%.0f%%, preco valido, <=%dd)",
        len(all_markets), len(candidates), config.A_MAX_FEE * 100, config.A_MAX_RESOLUTION_DAYS,
    )

    all_signals: list[ArbSignal] = []
    all_signals.extend(_detect_type1(candidates))      # Will X win Y?
    all_signals.extend(_detect_ranges(candidates))     # between X and Y
    all_signals.extend(_detect_type2(candidates))      # P(A) > P(A ou B)
    all_signals.extend(_detect_thresholds(candidates)) # above N ordering
    all_signals.extend(_detect_dates(candidates))      # by [date] ordering
    all_signals.extend(_detect_type3(candidates))      # win title > qualify

    res_ok = [s for s in all_signals if s.resolution_ok]

    # Assess rule quality; LOW signals are discarded silently
    valid: list[ArbSignal] = []
    for sig in res_ok:
        rules_text = _rules_cache.get(sig.market_ids[0], "")
        quality, _ = rule_validator.assess(rules_text, sig.market_ids[0])
        sig.rule_quality = quality
        if quality in ("HIGH", "MEDIUM"):
            valid.append(sig)
        else:
            logger.debug(
                "[A] Descartado por qualidade LOW | %s", sig.description[:60]
            )

    for sig in valid:
        _log_valid(sig)

    if valid:
        logger.info("[A] %d mercados | %d candidatos | %d sinal(is) valido(s)",
                    len(all_markets), len(candidates), len(valid))
    else:
        logger.info("[A] %d mercados | %d candidatos | sem sinais",
                    len(all_markets), len(candidates))

    return valid


# ── Execução (dry-run) ────────────────────────────────────────────────────────

def execute(signal: ArbSignal, client) -> bool:
    if config.MODE == "dry_run":
        first_slug = signal.market_slugs[0] if signal.market_slugs else ""
        event_slug = clob_utils.get_event_slug(first_slug) if first_slug else ""
        tracker.record_signal(
            "A",
            signal.market_ids[0],
            signal.description[:100],
            f"arb-t{signal.signal_type}",
            round(sum(signal.yes_prices) / len(signal.yes_prices), 4),
            signal.spread,
            closes_at_iso=signal.end_dates[0] if signal.end_dates else None,
            market_slug=event_slug or first_slug,
            n_markets=len(signal.yes_prices),
        )
        logger.info("   [DRY-RUN] Tipo%d gravado em signals.csv", signal.signal_type)
        return True
    logger.warning("Execução live não implementada — Tipo %d", signal.signal_type)
    return False
