"""
Módulo B2 — Esports Oracle (LoL + Dota 2)
Fontes de dados:
  - LoL:    LoL Esports API (lolesports.com) — pública, sem cadastro
  - Dota 2: Stratz GraphQL — pública, sem cadastro

Divergência > 15% + preço ≤ 40¢ + liquidez > $100 + partida em ≤ 4h → entrada.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from config import config
from modules import clob_utils, tracker

logger = logging.getLogger("oracle_b2")

LS_HEADERS = {
    "x-api-key": config.LOL_ESPORTS_API_KEY,
    "User-Agent": "polymarket-bot/1.0",
}

# ── Cache de win-rates ────────────────────────────────────────────────────────
# Stratz: { "dota2:team_name": (win_rate, timestamp) }
_stratz_cache: dict[str, tuple[float, float]] = {}

# LoL: dict global { team_key: win_rate } + timestamp da última atualização
_lol_winrates: dict[str, float] = {}
_lol_cache_ts: float = 0.0


def _cache_ttl_ok(ts: float) -> bool:
    return (time.time() - ts) / 3600 < config.B2_WINRATE_CACHE_H


# ── LoL Esports API ───────────────────────────────────────────────────────────

def _ls_get(endpoint: str, params: dict | None = None) -> dict:
    try:
        resp = requests.get(
            f"{config.LOL_ESPORTS_BASE_URL}/{endpoint}",
            params={"hl": "en-US", **(params or {})},
            headers=LS_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.debug("LoL Esports API erro (%s): %s", endpoint, e)
        return {}


def _active_tournament_id(league_id: str) -> str | None:
    """Retorna o ID do torneio ativo de uma liga."""
    data        = _ls_get("getTournamentsForLeague", {"leagueId": league_id})
    tournaments = (
        data.get("data", {}).get("leagues", [{}])[0].get("tournaments", [])
    )
    today = datetime.now(timezone.utc).date().isoformat()
    for t in tournaments:
        if t.get("startDate", "") <= today <= t.get("endDate", "9999-12-31"):
            return t.get("id")
    # Se não tem torneio ativo, pega o mais recente
    if tournaments:
        return tournaments[0].get("id")
    return None


def _parse_matches_winrates(standings_data: dict) -> dict[str, float]:
    """
    Agrega W/L de todos os matches completados e retorna:
    { team_slug: win_rate, team_name_lower: win_rate, team_code_lower: win_rate }
    Requer mínimo de B2_MIN_MATCHES partidas por time.
    """
    records: dict[str, dict] = {}

    stages = (
        standings_data.get("data", {})
        .get("standings", [{}])[0]
        .get("stages", [])
    )
    for stage in stages:
        for section in stage.get("sections", []):
            for match in section.get("matches", []):
                if match.get("state") != "completed":
                    continue
                for team in match.get("teams", []):
                    slug    = team.get("slug", "")
                    outcome = team.get("result", {}).get("outcome")
                    if not slug or outcome not in ("win", "loss"):
                        continue
                    if slug not in records:
                        records[slug] = {
                            "wins": 0, "losses": 0,
                            "name": team.get("name", ""),
                            "code": team.get("code", ""),
                        }
                    if outcome == "win":
                        records[slug]["wins"] += 1
                    else:
                        records[slug]["losses"] += 1

    result: dict[str, float] = {}
    for slug, rec in records.items():
        wins  = rec["wins"]
        total = wins + rec["losses"]
        if total < config.B2_MIN_MATCHES:
            continue
        wr = wins / total
        result[slug]                    = wr
        result[rec["name"].lower()]     = wr
        result[rec["code"].lower()]     = wr
    return result


def _refresh_lol_cache() -> None:
    """Busca standings de todas as ligas configuradas e popula _lol_winrates."""
    global _lol_winrates, _lol_cache_ts

    if _cache_ttl_ok(_lol_cache_ts):
        return

    combined: dict[str, float] = {}
    for league_name, league_id in config.LOL_LEAGUES.items():
        tournament_id = _active_tournament_id(league_id)
        if not tournament_id:
            logger.debug("LoL: sem torneio ativo para %s", league_name)
            continue
        data = _ls_get("getStandings", {"tournamentId": tournament_id})
        if not data:
            continue
        wr_map = _parse_matches_winrates(data)
        combined.update(wr_map)
        logger.debug("LoL: %s → %d times carregados", league_name, len(wr_map))

    _lol_winrates = combined
    _lol_cache_ts = time.time()
    logger.info("LoL cache atualizado: %d times com win-rate", len(combined))


def _lol_winrate(team_name: str) -> float | None:
    _refresh_lol_cache()
    key = team_name.lower().strip()
    # Tenta match direto por nome, code ou slug
    if key in _lol_winrates:
        return _lol_winrates[key]
    # Tenta match parcial (ex: "T1" dentro de "T1 Challengers")
    for k, wr in _lol_winrates.items():
        if key in k or k in key:
            return wr
    return None


# ── Stratz (Dota 2) ───────────────────────────────────────────────────────────

def _stratz_winrate(team_name: str) -> float | None:
    cache_key = f"dota2:{team_name.lower()}"
    if cache_key in _stratz_cache and _cache_ttl_ok(_stratz_cache[cache_key][1]):
        return _stratz_cache[cache_key][0]

    query = """
    query($name: String!) {
      stratz { search(query: $name) {
        teams { id name series(take: 20) { wins losses } }
      }}
    }
    """
    try:
        resp = requests.post(
            config.STRATZ_API_URL,
            json={"query": query, "variables": {"name": team_name}},
            timeout=15,
        )
        resp.raise_for_status()
        teams = (
            resp.json()
            .get("data", {}).get("stratz", {})
            .get("search", {}).get("teams", [])
        )
        if not teams:
            return None
        s     = teams[0].get("series", {})
        wins  = s.get("wins", 0)
        total = wins + s.get("losses", 0)
        if total < config.B2_MIN_MATCHES:
            return None
        wr = wins / total
        _stratz_cache[cache_key] = (wr, time.time())
        logger.debug("B2: win-rate Dota2 %s = %.1f%%", team_name, wr * 100)
        return wr
    except (requests.RequestException, KeyError, ZeroDivisionError):
        return None


def _estimate_winrate(team: str, game: str) -> float | None:
    if game == "lol":
        return _lol_winrate(team)
    if game == "dota2":
        return _stratz_winrate(team)
    return None


# ── Utilidades de mercado ─────────────────────────────────────────────────────

def _resolution_within_hours(market: dict, min_h: float, max_h: float) -> bool:
    end = market.get("end_date_iso") or market.get("endDate")
    if not end:
        return False
    try:
        end_dt  = datetime.fromisoformat(end.replace("Z", "+00:00"))
        now     = datetime.now(timezone.utc)
        delta_h = (end_dt - now).total_seconds() / 3600
        return min_h <= delta_h <= max_h
    except ValueError:
        return False


_TEAM_RE = re.compile(
    r"Will\s+(.+?)\s+(?:beat|defeat|win against|win over)\s+(.+?)(?:\s*\?|$)"
    r"|(.+?)\s+vs\.?\s+(.+?)(?:\s*\?|$)",
    re.IGNORECASE,
)


def _extract_teams(question: str) -> tuple[str, str] | None:
    match = _TEAM_RE.search(question)
    if not match:
        return None
    team_a = (match.group(1) or match.group(3) or "").strip()
    team_b = (match.group(2) or match.group(4) or "").strip()
    team_b = re.sub(r"\s*[\?\.]\s*$", "", team_b)
    return (team_a, team_b) if team_a and team_b else None


def _detect_game(market: dict) -> str | None:
    market_tags = {t for t in (market.get("tags") or [])}
    for game, tags in config.B2_TARGET_TAGS.items():
        if market_tags & tags:
            return game
    return None


# ── Scanner principal ─────────────────────────────────────────────────────────

@dataclass
class EsportsSignal:
    market_id: str
    question: str
    game: str
    team_a: str
    team_b: str
    model_prob: float
    market_price: float
    divergence: float
    side: str
    liquidity: float
    found_at: datetime
    end_date: str | None = None
    market_slug: str | None = None


def scan() -> list[EsportsSignal]:
    all_markets     = clob_utils.fetch_sampling_markets()
    all_tags        = set().union(*config.B2_TARGET_TAGS.values())
    esports_markets = [m for m in all_markets if clob_utils.has_tag(m, all_tags)]
    logger.info(
        "Mercados esports (LoL+Dota2): %d / %d totais",
        len(esports_markets), len(all_markets),
    )

    signals: list[EsportsSignal] = []

    for market in esports_markets:
        if not _resolution_within_hours(
            market,
            min_h=config.B2_ENTRY_BEFORE_H,
            max_h=config.B2_MAX_RESOLUTION_H,
        ):
            continue

        question = market.get("question", "")
        game     = _detect_game(market)
        if not game:
            continue

        teams = _extract_teams(question)
        if not teams:
            continue
        team_a, team_b = teams

        wr_a = _estimate_winrate(team_a, game)
        wr_b = _estimate_winrate(team_b, game)

        if wr_a is None and wr_b is None:
            continue

        if wr_a is not None and wr_b is not None:
            total      = wr_a + wr_b
            model_prob = wr_a / total if total > 0 else 0.5
        elif wr_a is not None:
            model_prob = wr_a
        else:
            model_prob = 1.0 - wr_b

        yes_token = clob_utils.token_by_outcome(market, "Yes")
        if not yes_token:
            continue

        market_price = float(yes_token.get("price", 0))
        if market_price <= 0:
            continue

        divergence = abs(model_prob - market_price)

        if (
            divergence   >= config.B2_MIN_DIVERGENCE
            and market_price <= config.B2_MAX_SHARE_PRICE
        ):
            liquidity = clob_utils.get_orderbook_liquidity(yes_token["token_id"])
            if liquidity < config.B2_MIN_LIQUIDITY:
                continue

            side   = "Yes" if model_prob > market_price else "No"
            signal = EsportsSignal(
                market_id    = market.get("condition_id", ""),
                question     = question,
                game         = game,
                team_a       = team_a,
                team_b       = team_b,
                model_prob   = model_prob,
                market_price = market_price,
                divergence   = divergence,
                side         = side,
                liquidity    = liquidity,
                found_at     = datetime.utcnow(),
                end_date     = market.get("end_date_iso") or market.get("endDate"),
                market_slug  = market.get("market_slug"),
            )
            signals.append(signal)
            logger.info(
                "SINAL B2 | %s | %s vs %s | model=%.2f market=%.2f div=%.1f%%",
                game.upper(), team_a, team_b,
                model_prob, market_price, divergence * 100,
            )

    logger.info(
        "Varredura B2: %d mercados esports, %d sinais",
        len(esports_markets), len(signals),
    )
    return signals


def execute(signal: EsportsSignal, client) -> bool:
    if config.MODE == "dry_run":
        logger.info(
            "[DRY-RUN] B2: %s vs %s | %s | side=%s price=%.3f div=%.1f%%",
            signal.team_a, signal.team_b, signal.game.upper(),
            signal.side, signal.market_price, signal.divergence * 100,
        )
        tracker.record_signal(
            "B2", signal.market_id, signal.question,
            signal.side, signal.market_price, signal.divergence,
            closes_at_iso=signal.end_date,
            market_slug=signal.market_slug,
        )
        return True
    logger.warning("Execução live não implementada: %s", signal.market_id)
    return False
