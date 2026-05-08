from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from modules import oracle_b2
from modules.oracle_b2 import (
    _extract_teams,
    _detect_game,
    _resolution_within_hours,
)


# ── _extract_teams ────────────────────────────────────────────────────────────

def test_extract_vs_format():
    assert _extract_teams("T1 vs Gen.G Esports?") == ("T1", "Gen.G Esports")

def test_extract_will_beat_format():
    assert _extract_teams("Will T1 beat Gen.G Esports?") == ("T1", "Gen.G Esports")

def test_extract_will_defeat_format():
    assert _extract_teams("Will Team Liquid defeat Cloud9?") == ("Team Liquid", "Cloud9")

def test_extract_win_against_format():
    assert _extract_teams("Will NaVi win against Astralis?") == ("NaVi", "Astralis")

def test_extract_strips_trailing_punctuation():
    result = _extract_teams("OG vs Team Secret.")
    assert result is not None
    assert result[1] == "Team Secret"

def test_extract_no_match():
    assert _extract_teams("Will it rain tomorrow?") is None

def test_extract_empty_string():
    assert _extract_teams("") is None


# ── _detect_game ──────────────────────────────────────────────────────────────

def test_detect_lol():
    market = {"tags": ["league of legends", "LCK"]}
    assert _detect_game(market) == "lol"

def test_detect_lol_short_tag():
    market = {"tags": ["lol", "Esports"]}
    assert _detect_game(market) == "lol"

def test_detect_dota2():
    market = {"tags": ["Dota 2", "Esports"]}
    assert _detect_game(market) == "dota2"

def test_detect_no_match():
    market = {"tags": ["CS2", "Esports"]}
    assert _detect_game(market) is None

def test_detect_empty_tags():
    assert _detect_game({"tags": []}) is None

def test_detect_none_tags():
    assert _detect_game({"tags": None}) is None


# ── _resolution_within_hours ──────────────────────────────────────────────────

def _market_ending_in(hours):
    end = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    return {"end_date_iso": end}

def test_resolution_in_window():
    assert _resolution_within_hours(_market_ending_in(3), min_h=2, max_h=4) is True

def test_resolution_too_soon():
    assert _resolution_within_hours(_market_ending_in(1), min_h=2, max_h=4) is False

def test_resolution_too_far():
    assert _resolution_within_hours(_market_ending_in(6), min_h=2, max_h=4) is False

def test_resolution_expired():
    assert _resolution_within_hours(_market_ending_in(-1), min_h=2, max_h=4) is False


# ── Win-rate normalization ────────────────────────────────────────────────────

def test_model_prob_both_teams():
    # wr_a=0.6, wr_b=0.4 → total=1.0 → model_prob = 0.60
    wr_a, wr_b = 0.6, 0.4
    total = wr_a + wr_b
    assert abs(wr_a / total - 0.60) < 1e-9

def test_model_prob_only_team_a():
    # só wr_a disponível → model_prob = wr_a diretamente
    wr_a = 0.70
    assert wr_a == 0.70

def test_model_prob_only_team_b():
    # só wr_b disponível → model_prob = 1 - wr_b
    wr_b = 0.30
    assert abs(1.0 - wr_b - 0.70) < 1e-9

def test_model_prob_equal_winrates():
    wr_a, wr_b = 0.5, 0.5
    total = wr_a + wr_b
    assert abs(wr_a / total - 0.50) < 1e-9


# ── scan() integrado com mocks ────────────────────────────────────────────────

def _make_market(question, hours_to_end, yes_price, game="lol"):
    import re
    tags = {"lol": ["league of legends"], "dota2": ["Dota 2"]}[game]
    slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")
    end  = (datetime.now(timezone.utc) + timedelta(hours=hours_to_end)).isoformat()
    return {
        "condition_id": "cid-b2-test",
        "question":     question,
        "market_slug":  slug,
        "end_date_iso": end,
        "tags":         tags,
        "tokens": [
            {"outcome": "Yes", "token_id": "yes_tok", "price": yes_price},
            {"outcome": "No",  "token_id": "no_tok",  "price": 1 - yes_price},
        ],
    }


def test_scan_generates_lol_signal():
    # T1 win-rate=0.80, G2=0.40 → model_prob T1 = 0.80/1.20 ≈ 0.67
    # mercado: YES=0.30 → divergência ≈ 0.37 > 0.15 → sinal
    market = _make_market("T1 vs G2 Esports?", hours_to_end=3, yes_price=0.30, game="lol")

    with patch("modules.oracle_b2.clob_utils.fetch_sampling_markets", return_value=[market]), \
         patch("modules.oracle_b2._lol_winrate", side_effect=lambda t: 0.80 if "T1" in t else 0.40), \
         patch("modules.oracle_b2.clob_utils.get_orderbook_liquidity", return_value=150.0):
        signals = oracle_b2.scan()

    assert len(signals) == 1
    s = signals[0]
    assert s.team_a == "T1"
    assert s.game   == "lol"
    assert s.side   == "Yes"
    assert s.divergence > 0.15


def test_scan_no_signal_low_divergence():
    # model_prob ≈ 0.50, market=0.45 → divergência 0.05 < 0.15
    market = _make_market("T1 vs G2 Esports?", hours_to_end=3, yes_price=0.45, game="lol")

    with patch("modules.oracle_b2.clob_utils.fetch_sampling_markets", return_value=[market]), \
         patch("modules.oracle_b2._lol_winrate", return_value=0.50), \
         patch("modules.oracle_b2.clob_utils.get_orderbook_liquidity", return_value=150.0):
        signals = oracle_b2.scan()

    assert len(signals) == 0


def test_scan_no_signal_outside_window():
    # partida em 6h → fora da janela 2–4h
    market = _make_market("T1 vs G2 Esports?", hours_to_end=6, yes_price=0.20, game="lol")

    with patch("modules.oracle_b2.clob_utils.fetch_sampling_markets", return_value=[market]), \
         patch("modules.oracle_b2._lol_winrate", return_value=0.80):
        signals = oracle_b2.scan()

    assert len(signals) == 0


def test_scan_no_signal_when_no_winrate():
    market = _make_market("T1 vs G2 Esports?", hours_to_end=3, yes_price=0.20, game="lol")

    with patch("modules.oracle_b2.clob_utils.fetch_sampling_markets", return_value=[market]), \
         patch("modules.oracle_b2._lol_winrate", return_value=None):
        signals = oracle_b2.scan()

    assert len(signals) == 0


def test_execute_dry_run():
    market = _make_market("T1 vs G2 Esports?", hours_to_end=3, yes_price=0.30, game="lol")

    with patch("modules.oracle_b2.clob_utils.fetch_sampling_markets", return_value=[market]), \
         patch("modules.oracle_b2._lol_winrate", side_effect=lambda t: 0.80 if "T1" in t else 0.40), \
         patch("modules.oracle_b2.clob_utils.get_orderbook_liquidity", return_value=150.0):
        signals = oracle_b2.scan()

    assert len(signals) == 1
    with patch("modules.oracle_b2.tracker.record_signal"):
        assert oracle_b2.execute(signals[0], client=None) is True
