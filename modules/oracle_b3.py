"""
Módulo B3 — Politics / Elections Oracle

Detecta arbitragem entre mercados eleitorais no Polymarket:
  Tipo 4a — Soma de candidatos: P(c1)+…+P(cN) ≠ 1
  Tipo 4b — Ordenação de datas: P(by earlier) > P(by later)
  Tipo 4c — Candidato > Partido: P(candidate wins) > P(party wins)

Filtros adicionais:
  - Descarta grupos onde mercados usam critérios divergentes
    (popular vote vs. electoral college)
  - Regras de qualidade LOW → descartado silenciosamente

Ciclo: 15 minutos.
"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from config import config
from modules import clob_utils, rule_validator, tracker

logger = logging.getLogger("oracle_b3")

# ── Padrões de questão ────────────────────────────────────────────────────────

_WIN_ELECTION_RE = re.compile(
    r"Will\s+(.+?)\s+win\s+(?:the\s+)?(.+?(?:election|race|presidency|primary|"
    r"caucus|seat|vote)[^?]*?)[\?\.]*$",
    re.IGNORECASE,
)

_PARTY_WIN_RE = re.compile(
    r"Will\s+(?:the\s+)?(?:Republican|Democrat|Labour|Conservative|Liberal|Green|"
    r"Libertarian|Independent|GOP|DNC|RNC).+?\s+win\b",
    re.IGNORECASE,
)

_CANDIDATE_WIN_RE = re.compile(
    r"Will\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s+win\b",
    re.IGNORECASE,
)

_BY_DATE_RE = re.compile(
    r"\bby\s+((?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[\s\d,]+\d{4}|end of \d{4}|\d{4})\b",
    re.IGNORECASE,
)

_POPULAR_VOTE_RE      = re.compile(r"\bpopular\s+vote\b", re.IGNORECASE)
_ELECTORAL_COLLEGE_RE = re.compile(r"\belectoral\s+college\b", re.IGNORECASE)

_SOURCE_KEYWORDS = [
    "associated press", "ap news", "reuters", "official results",
    "official website", "uma optimistic oracle", "polymarket resolution",
    "the new york times", "bbc", "cnn",
]

_rules_cache: dict[str, str] = {}


# ── Data class de sinal ───────────────────────────────────────────────────────

@dataclass
class PoliticsSignal:
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


def _filter_politics(markets: list[dict]) -> list[dict]:
    result = []
    for m in markets:
        if not clob_utils.has_tag(m, config.B3_TARGET_TAGS):
            continue
        if float(m.get("taker_base_fee", 1)) > config.B3_MAX_FEE:
            continue
        if not clob_utils.token_by_outcome(m, "Yes") or not clob_utils.token_by_outcome(m, "No"):
            continue
        price = _yes_price(m)
        if not (0 < price < 1):
            continue
        if not _within_resolution_days(m, config.B3_MAX_RESOLUTION_DAYS):
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


def _vote_criterion(rules_text: str) -> str:
    if _POPULAR_VOTE_RE.search(rules_text):
        return "popular_vote"
    if _ELECTORAL_COLLEGE_RE.search(rules_text):
        return "electoral_college"
    return "unknown"


def _check_resolution_rules(market_ids: list[str]) -> tuple[bool, str]:
    sources: dict[str, str] = {}
    criteria: set[str]      = set()
    for mid in market_ids:
        text = _fetch_resolution_rules(mid)
        if not text:
            return False, f"Regras não disponíveis para {mid[:16]}"
        src = _extract_resolution_source(text)
        if not src:
            return False, f"Fonte não identificada em {mid[:16]}"
        sources[mid] = src
        crit = _vote_criterion(text)
        if crit != "unknown":
            criteria.add(crit)
    unique_src = set(sources.values())
    if len(unique_src) > 1:
        return False, f"Fontes divergentes: {' | '.join(sorted(unique_src))}"
    if len(criteria) > 1:
        return False, f"Critério de voto divergente: {' vs '.join(sorted(criteria))}"
    return True, f"Fonte confirmada: {unique_src.pop()}"


# ── Construtor genérico de PoliticsSignal ─────────────────────────────────────

def _make_signal(sig_type: int, description: str, markets: list[dict],
                 prices: list[float], spread: float, min_liq: float,
                 res_ok: bool, res_notes: str) -> PoliticsSignal:
    return PoliticsSignal(
        signal_type      = sig_type,
        description      = description,
        market_ids       = [m["condition_id"] for m in markets],
        questions        = [m.get("question", "")[:80] for m in markets],
        yes_prices       = [round(p, 4) for p in prices],
        spread           = spread,
        min_liquidity    = min_liq,
        resolution_ok    = res_ok,
        resolution_notes = res_notes,
        pnl_est          = round(spread * config.B3_TRADE_SIZE_USD, 2),
        market_slugs     = [m.get("market_slug", "") for m in markets],
        end_dates        = [m.get("end_date_iso") or m.get("endDate") or "" for m in markets],
    )


# ── Tipo 4a: Soma de candidatos ───────────────────────────────────────────────

def _actual_return(gap: float, n: int, oversum: bool) -> float:
    if oversum:
        denom = n - 1 - gap
        return gap / denom if denom > 0 else 0.0
    return gap / (1.0 - gap)


def _detect_sum(candidates: list[dict]) -> list[PoliticsSignal]:
    signals = []
    groups: dict[str, list[dict]] = defaultdict(list)
    for m in candidates:
        match = _WIN_ELECTION_RE.search(m.get("question", ""))
        if not match:
            continue
        candidate = match.group(1).strip().lower()
        event     = match.group(2).strip().lower()
        if " or " in candidate:
            continue
        groups[event].append(m)

    for event, group in groups.items():
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
        if ret < config.B3_MIN_SPREAD:
            continue
        min_liq = min(_get_min_liquidity(m) for m in group)
        if min_liq < config.B3_MIN_LIQUIDITY:
            continue
        direction = "OVERSUM" if oversum else "UNDERSUM"
        action    = "comprar NO em todos" if oversum else "comprar YES em todos"
        res_ok, res_notes = _check_resolution_rules([m["condition_id"] for m in group])
        signals.append(_make_signal(
            4,
            f"Tipo 4a {direction} — '{event[:50]}' ({n} candidatos): "
            f"soma YES={total:.2%} → {action}",
            group, prices, round(ret, 4), min_liq, res_ok, res_notes,
        ))
    return signals


# ── Tipo 4b: Ordenação de datas ───────────────────────────────────────────────

def _stem_date(question: str) -> str | None:
    m = _BY_DATE_RE.search(question)
    if not m:
        return None
    stem = question[:m.start()].strip().lower()
    return stem if len(stem) >= 10 else None


def _parse_date_value(text: str) -> datetime | None:
    text = text.strip().rstrip("?.")
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%B %d,%Y", "%B %Y", "%b %Y"):
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


def _detect_dates(candidates: list[dict]) -> list[PoliticsSignal]:
    signals    = []
    seen_pairs: set[tuple[str, str]] = set()
    groups: dict[str, list[tuple[datetime, dict]]] = defaultdict(list)

    for m in candidates:
        q     = m.get("question", "")
        stem  = _stem_date(q)
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
            if spread < config.B3_MIN_SPREAD:
                continue
            pair_key = (
                min(m_early["condition_id"], m_late["condition_id"]),
                max(m_early["condition_id"], m_late["condition_id"]),
            )
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            min_liq = min(_get_min_liquidity(m_early), _get_min_liquidity(m_late))
            if min_liq < config.B3_MIN_LIQUIDITY:
                continue
            res_ok, res_notes = _check_resolution_rules(
                [m_early["condition_id"], m_late["condition_id"]]
            )
            early_str = dt_early.strftime("%b %d")
            late_str  = dt_late.strftime("%b %d")
            signals.append(_make_signal(
                4,
                f"Tipo 4b DATA — P(by {early_str})={p_early:.2%} > "
                f"P(by {late_str})={p_late:.2%}",
                [m_early, m_late], [p_early, p_late], spread, min_liq, res_ok, res_notes,
            ))
    return signals


# ── Tipo 4c: Candidato > Partido ──────────────────────────────────────────────

def _detect_candidate_party(candidates: list[dict]) -> list[PoliticsSignal]:
    """P(candidate wins election) > P(party wins election) é impossível."""
    signals    = []
    seen_pairs: set[tuple[str, str]] = set()
    parties: dict[str, dict] = {}

    for m in candidates:
        q = m.get("question", "")
        if _PARTY_WIN_RE.search(q):
            match = _WIN_ELECTION_RE.search(q)
            if match:
                event = match.group(2).strip().lower()
                parties[event] = m

    for market in candidates:
        q   = market.get("question", "")
        mat = _CANDIDATE_WIN_RE.search(q)
        if not mat:
            continue
        if _PARTY_WIN_RE.search(q):
            continue
        win_match = _WIN_ELECTION_RE.search(q)
        if not win_match:
            continue
        event   = win_match.group(2).strip().lower()
        m_party = parties.get(event)
        if not m_party:
            continue
        p_cand  = _yes_price(market)
        p_party = _yes_price(m_party)
        if p_cand <= 0 or p_party <= 0:
            continue
        spread = round(p_cand - p_party, 4)
        if spread < config.B3_MIN_SPREAD:
            continue
        pair_key = (
            min(market["condition_id"], m_party["condition_id"]),
            max(market["condition_id"], m_party["condition_id"]),
        )
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        min_liq = min(_get_min_liquidity(market), _get_min_liquidity(m_party))
        if min_liq < config.B3_MIN_LIQUIDITY:
            continue
        cand_name = mat.group(1)[:30]
        res_ok, res_notes = _check_resolution_rules(
            [market["condition_id"], m_party["condition_id"]]
        )
        signals.append(_make_signal(
            4,
            f"Tipo 4c CAND>PARTIDO — P({cand_name})={p_cand:.2%} > P(partido)={p_party:.2%}",
            [market, m_party], [p_cand, p_party], spread, min_liq, res_ok, res_notes,
        ))
    return signals


# ── Logging de sinal válido ───────────────────────────────────────────────────

def _log_valid(sig: PoliticsSignal) -> None:
    m       = re.search(r"Tipo \d+\w*\s+(\w+)", sig.description)
    subtype = m.group(1) if m else ""
    logger.info(
        ">> SINAL B3 | Tipo%d %s | spread=%.2f%% | P&L=$%.2f | liq=$%.0f",
        sig.signal_type, subtype, sig.spread * 100, sig.pnl_est, sig.min_liquidity,
    )
    for q, p, mid in zip(sig.questions, sig.yes_prices, sig.market_ids):
        logger.info("   YES=%.3f | %s [%s]", p, q[:65], mid[:12])
    logger.info("   Resolucao: %s | Qualidade: %s", sig.resolution_notes, sig.rule_quality)


# ── Scan principal ────────────────────────────────────────────────────────────

def scan() -> list[PoliticsSignal]:
    _rules_cache.clear()

    all_markets = clob_utils.fetch_sampling_markets()
    candidates  = _filter_politics(all_markets)
    logger.info(
        "Scanner B3: %d mercados totais → %d candidatos políticos (fee≤%.0f%%, <=%dd)",
        len(all_markets), len(candidates),
        config.B3_MAX_FEE * 100, config.B3_MAX_RESOLUTION_DAYS,
    )

    all_signals: list[PoliticsSignal] = []
    all_signals.extend(_detect_sum(candidates))
    all_signals.extend(_detect_dates(candidates))
    all_signals.extend(_detect_candidate_party(candidates))

    res_ok = [s for s in all_signals if s.resolution_ok]

    valid: list[PoliticsSignal] = []
    for sig in res_ok:
        rules_text = _rules_cache.get(sig.market_ids[0], "")
        quality, _ = rule_validator.assess(rules_text, sig.market_ids[0])
        sig.rule_quality = quality
        if quality in ("HIGH", "MEDIUM"):
            valid.append(sig)

    for sig in valid:
        _log_valid(sig)

    if valid:
        logger.info("[B3] %d mercados | %d candidatos | %d sinal(is) válido(s)",
                    len(all_markets), len(candidates), len(valid))
    else:
        logger.info("[B3] %d mercados | %d candidatos | sem sinais",
                    len(all_markets), len(candidates))

    return valid


# ── Execução (dry-run) ────────────────────────────────────────────────────────

def execute(signal: PoliticsSignal, client) -> bool:
    if config.MODE == "dry_run":
        first_slug = signal.market_slugs[0] if signal.market_slugs else ""
        event_slug = clob_utils.get_event_slug(first_slug) if first_slug else ""
        tracker.record_signal(
            "B3",
            signal.market_ids[0],
            signal.description[:100],
            f"arb-t{signal.signal_type}",
            round(sum(signal.yes_prices) / len(signal.yes_prices), 4),
            signal.spread,
            closes_at_iso=signal.end_dates[0] if signal.end_dates else None,
            market_slug=event_slug or first_slug,
            n_markets=len(signal.yes_prices),
        )
        logger.info("   [DRY-RUN] B3 Tipo%d gravado em signals.csv", signal.signal_type)
        return True
    logger.warning("Execução live não implementada — B3 Tipo %d", signal.signal_type)
    return False
