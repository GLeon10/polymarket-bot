"""
Módulo B4 — Sports Live-Game Oracle (NBA / NFL / MLB / NHL)

Detecta violação: P(win by X+ points) > P(win outright) — impossível matematicamente.
Exemplo: P(Lakers +5) = 62¢ > P(Lakers win) = 55¢ → comprar YES ML + NO spread.

Modo dinâmico:
  - Standby (padrão): sem jogos ao vivo detectados.
  - Ativo: ao menos um jogo resolve em ≤ 4h → intervalo cai para 60 s.
  - Bloqueio: sem entrada nos últimos 5 min de jogo.

O intervalo dinâmico é gerenciado por _run_b4_loop() em main.py.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import config
from modules import clob_utils, tracker

logger = logging.getLogger("oracle_b4")

# ── Estado de modo ────────────────────────────────────────────────────────────

_active_mode: bool = False


def is_active() -> bool:
    return _active_mode


# ── Padrões de questão ────────────────────────────────────────────────────────

_SPREAD_KEYWORDS = re.compile(r"\bby\s+\d+\b", re.IGNORECASE)

# Spread: "Will [A] beat [B] by [N]+ points?"
_SPREAD_RE = re.compile(
    r"Will\s+(?:the\s+)?(.+?)\s+(?:beat|defeat|win against|win over|win by)\s+"
    r"(?:the\s+)?(.+?)\s+by\s+(?:at\s+least\s+|more\s+than\s+)?(\d+)\+?\s*"
    r"(?:points?|runs?|goals?|strokes?)?[\?\.]*$",
    re.IGNORECASE,
)

# Moneyline: "Will [A] beat [B]?" — sem "by N"
_MONEYLINE_RE = re.compile(
    r"Will\s+(?:the\s+)?(.+?)\s+(?:beat|defeat|win against|win over)\s+"
    r"(?:the\s+)?(.+?)[\?\.]*$",
    re.IGNORECASE,
)


# ── Data class de sinal ───────────────────────────────────────────────────────

@dataclass
class SportsSignal:
    market_id_ml:     str
    market_id_spread: str
    question_ml:      str
    question_spread:  str
    team_a:           str
    team_b:           str
    league:           str
    price_ml:         float
    price_spread:     float
    spread_margin:    float
    side:             str
    liquidity:        float
    found_at:         datetime = field(default_factory=datetime.utcnow)
    end_date:         str | None = None
    slug_ml:          str | None = None


# ── Utilitários de mercado ────────────────────────────────────────────────────

def _yes_price(market: dict) -> float:
    for t in market.get("tokens", []):
        if t.get("outcome", "").lower() == "yes":
            return float(t.get("price", 0))
    return 0.0


def _hours_to_end(market: dict) -> float | None:
    end = market.get("end_date_iso") or market.get("endDate")
    if not end:
        return None
    try:
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
    except ValueError:
        return None


def _is_live_window(market: dict) -> bool:
    h = _hours_to_end(market)
    if h is None:
        return False
    min_h = config.B4_BLOCK_FINAL_MIN / 60.0
    return min_h < h <= config.B4_LIVE_WINDOW_H


def _is_blocked_final(market: dict) -> bool:
    h = _hours_to_end(market)
    if h is None:
        return True
    return h <= config.B4_BLOCK_FINAL_MIN / 60.0


def _filter_sports(markets: list[dict]) -> list[dict]:
    result = []
    for m in markets:
        if not clob_utils.has_tag(m, config.B4_TARGET_TAGS):
            continue
        if float(m.get("taker_base_fee", 1)) > config.B4_MAX_FEE:
            continue
        if not clob_utils.token_by_outcome(m, "Yes") or not clob_utils.token_by_outcome(m, "No"):
            continue
        price = _yes_price(m)
        if not (0 < price < 1):
            continue
        result.append(m)
    return result


def _detect_league(market: dict) -> str:
    tags = set(market.get("tags") or [])
    for league in config.B4_LEAGUES:
        if league in tags:
            return league
    return "Sports"


def _normalize_team(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().lower()


# ── Detector: moneyline vs spread ─────────────────────────────────────────────

def _detect_ml_spread(candidates: list[dict]) -> list[SportsSignal]:
    signals    = []
    seen_pairs: set[tuple[str, str]] = set()

    moneylines: dict[tuple[str, str], dict] = {}
    spreads:    list[tuple[str, str, int, dict]] = []

    for m in candidates:
        q = m.get("question", "")
        if _SPREAD_KEYWORDS.search(q):
            match = _SPREAD_RE.match(q)
            if match:
                ta = _normalize_team(match.group(1))
                tb = _normalize_team(match.group(2))
                spreads.append((ta, tb, int(match.group(3)), m))
        else:
            match = _MONEYLINE_RE.match(q)
            if match:
                ta = _normalize_team(match.group(1))
                tb = _normalize_team(match.group(2))
                moneylines[(ta, tb)] = m

    for ta, tb, margin, m_spread in spreads:
        if not _is_live_window(m_spread) or _is_blocked_final(m_spread):
            continue

        m_ml = moneylines.get((ta, tb))
        if not m_ml:
            continue
        if _is_blocked_final(m_ml):
            continue

        p_ml     = _yes_price(m_ml)
        p_spread = _yes_price(m_spread)
        if p_ml <= 0 or p_spread <= 0:
            continue

        violation = round(p_spread - p_ml, 4)
        if violation < config.B4_MIN_SPREAD:
            continue

        pair_key = (
            min(m_ml["condition_id"], m_spread["condition_id"]),
            max(m_ml["condition_id"], m_spread["condition_id"]),
        )
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        yes_tok = clob_utils.token_by_outcome(m_ml, "Yes")
        no_tok  = clob_utils.token_by_outcome(m_spread, "No")
        if not yes_tok or not no_tok:
            continue

        liq = min(
            clob_utils.get_orderbook_liquidity(yes_tok["token_id"]),
            clob_utils.get_orderbook_liquidity(no_tok["token_id"]),
        )
        if liq < config.B4_MIN_LIQUIDITY:
            continue

        league = _detect_league(m_spread)
        signals.append(SportsSignal(
            market_id_ml     = m_ml["condition_id"],
            market_id_spread = m_spread["condition_id"],
            question_ml      = m_ml.get("question", "")[:80],
            question_spread  = m_spread.get("question", "")[:80],
            team_a           = ta,
            team_b           = tb,
            league           = league,
            price_ml         = p_ml,
            price_spread     = p_spread,
            spread_margin    = violation,
            side             = "Yes ML + No spread",
            liquidity        = liq,
            found_at         = datetime.utcnow(),
            end_date         = m_ml.get("end_date_iso") or m_ml.get("endDate"),
            slug_ml          = m_ml.get("market_slug"),
        ))
        logger.info(
            "SINAL B4 | %s | %s vs %s | P(spread=%d+)=%.2f > P(ML)=%.2f | viol=%.1f%%",
            league, ta, tb, margin, p_spread, p_ml, violation * 100,
        )

    return signals


# ── Scanner principal ─────────────────────────────────────────────────────────

def scan() -> list[SportsSignal]:
    global _active_mode

    all_markets = clob_utils.fetch_sampling_markets()
    sports      = _filter_sports(all_markets)
    live_count  = sum(1 for m in sports if _is_live_window(m))
    _active_mode = live_count > 0

    logger.info(
        "Scanner B4: %d mercados sports | %d ao vivo | modo=%s",
        len(sports), live_count, "ATIVO" if _active_mode else "standby",
    )

    if not _active_mode:
        return []

    signals = _detect_ml_spread(sports)
    if signals:
        logger.info("[B4] %d sinal(is) encontrado(s)", len(signals))
    else:
        logger.info("[B4] %d mercados ao vivo | sem sinais", live_count)

    return signals


# ── Execução (dry-run) ────────────────────────────────────────────────────────

def execute(signal: SportsSignal, client) -> bool:
    if config.MODE == "dry_run":
        logger.info(
            "[DRY-RUN] B4: %s vs %s | %s | viol=%.1f%%",
            signal.team_a, signal.team_b, signal.league,
            signal.spread_margin * 100,
        )
        tracker.record_signal(
            "B4", signal.market_id_ml,
            f"{signal.question_ml} | spread: {signal.question_spread[:40]}",
            signal.side, signal.price_ml, signal.spread_margin,
            closes_at_iso=signal.end_date,
            market_slug=signal.slug_ml,
        )
        return True
    logger.warning("Execução live não implementada: %s", signal.market_id_ml)
    return False
