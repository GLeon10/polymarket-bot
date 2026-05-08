"""Testes para oracle_b4 — Sports Live-Game Oracle."""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

import modules.oracle_b4 as b4
from modules.oracle_b4 import (
    SportsSignal,
    _detect_ml_spread,
    _filter_sports,
    _is_live_window,
    _is_blocked_final,
    _normalize_team,
    scan,
)


# ── Factories de mercado ──────────────────────────────────────────────────────

def _market(question, price=0.50, condition_id=None, tags=None, fee=0.0, hours_to_end=2.0):
    end_dt = datetime.now(timezone.utc) + timedelta(hours=hours_to_end)
    cid    = condition_id or f"0x{abs(hash(question)):016x}"
    return {
        "condition_id": cid,
        "question":     question,
        "taker_base_fee": fee,
        "tags":         tags or ["NBA", "Basketball"],
        "end_date_iso": end_dt.isoformat(),
        "market_slug":  "test-slug",
        "tokens": [
            {"outcome": "Yes", "token_id": f"yes-{cid}", "price": str(price)},
            {"outcome": "No",  "token_id": f"no-{cid}",  "price": str(1 - price)},
        ],
    }


def _run_detect(markets, liquidity=600.0):
    with patch("modules.oracle_b4.clob_utils.get_orderbook_liquidity", return_value=liquidity):
        return _detect_ml_spread(markets)


# ── _normalize_team ───────────────────────────────────────────────────────────

def test_normalize_strips_whitespace():
    assert _normalize_team("  Lakers  ") == "lakers"


def test_normalize_collapses_spaces():
    assert _normalize_team("Los  Angeles  Lakers") == "los angeles lakers"


# ── _is_live_window ───────────────────────────────────────────────────────────

def test_live_window_within_range():
    m = _market("test", hours_to_end=2.0)
    assert _is_live_window(m) is True


def test_live_window_too_far():
    m = _market("test", hours_to_end=5.0)
    assert _is_live_window(m) is False


def test_live_window_blocked_final():
    m = _market("test", hours_to_end=0.05)   # 3 min — within block window
    assert _is_live_window(m) is False


# ── _is_blocked_final ─────────────────────────────────────────────────────────

def test_blocked_final_true():
    m = _market("test", hours_to_end=0.04)   # ~2.4 min
    assert _is_blocked_final(m) is True


def test_blocked_final_false():
    m = _market("test", hours_to_end=1.0)
    assert _is_blocked_final(m) is False


# ── _filter_sports ────────────────────────────────────────────────────────────

def test_filter_keeps_nba_tag():
    m = _market("Will the Lakers beat the Celtics?", tags=["NBA"])
    with patch("modules.oracle_b4.clob_utils.token_by_outcome",
               side_effect=lambda m, o: {"token_id": "x", "price": "0.5"}):
        result = _filter_sports([m])
    assert len(result) == 1


def test_filter_removes_wrong_tag():
    m = _market("Will the Lakers beat the Celtics?", tags=["Soccer"])
    with patch("modules.oracle_b4.clob_utils.token_by_outcome",
               side_effect=lambda m, o: {"token_id": "x"}):
        result = _filter_sports([m])
    assert result == []


def test_filter_removes_high_fee():
    m = _market("Will the Lakers beat the Celtics?", fee=0.02)
    with patch("modules.oracle_b4.clob_utils.token_by_outcome",
               side_effect=lambda m, o: {"token_id": "x"}):
        result = _filter_sports([m])
    assert result == []


# ── _detect_ml_spread ─────────────────────────────────────────────────────────

def test_detects_violation():
    ml     = _market("Will the Lakers beat the Celtics?",             price=0.55, condition_id="0xML1")
    spread = _market("Will the Lakers beat the Celtics by 5+ points?", price=0.65, condition_id="0xSP1")
    signals = _run_detect([ml, spread])
    assert len(signals) == 1
    assert signals[0].spread_margin == pytest.approx(0.10, abs=1e-4)
    assert signals[0].side == "Yes ML + No spread"


def test_no_signal_when_spread_below_ml():
    ml     = _market("Will the Lakers beat the Celtics?",             price=0.65, condition_id="0xML2")
    spread = _market("Will the Lakers beat the Celtics by 5+ points?", price=0.55, condition_id="0xSP2")
    signals = _run_detect([ml, spread])
    assert signals == []


def test_no_signal_when_violation_too_small():
    ml     = _market("Will the Lakers beat the Celtics?",             price=0.60, condition_id="0xML3")
    spread = _market("Will the Lakers beat the Celtics by 5+ points?", price=0.61, condition_id="0xSP3")
    signals = _run_detect([ml, spread])
    assert signals == []


def test_no_signal_when_liquidity_too_low():
    ml     = _market("Will the Lakers beat the Celtics?",             price=0.50, condition_id="0xML4")
    spread = _market("Will the Lakers beat the Celtics by 5+ points?", price=0.70, condition_id="0xSP4")
    signals = _run_detect([ml, spread], liquidity=100.0)  # below B4_MIN_LIQUIDITY=500
    assert signals == []


def test_blocked_when_game_ending():
    ml     = _market("Will the Lakers beat the Celtics?",             price=0.50, condition_id="0xML5", hours_to_end=0.04)
    spread = _market("Will the Lakers beat the Celtics by 5+ points?", price=0.70, condition_id="0xSP5", hours_to_end=0.04)
    signals = _run_detect([ml, spread])
    assert signals == []


def test_no_signal_when_game_not_live():
    ml     = _market("Will the Lakers beat the Celtics?",             price=0.50, condition_id="0xML6", hours_to_end=8.0)
    spread = _market("Will the Lakers beat the Celtics by 5+ points?", price=0.70, condition_id="0xSP6", hours_to_end=8.0)
    signals = _run_detect([ml, spread])
    assert signals == []


def test_deduplication_same_pair():
    ml     = _market("Will the Lakers beat the Celtics?",             price=0.50, condition_id="0xML7")
    sp1    = _market("Will the Lakers beat the Celtics by 5+ points?", price=0.70, condition_id="0xSP7a")
    sp2    = _market("Will the Lakers beat the Celtics by 8+ points?", price=0.75, condition_id="0xSP7b")
    signals = _run_detect([ml, sp1, sp2])
    # Both spreads pair with the same ML — each is a separate pair
    assert len(signals) == 2


# ── scan() mode switching ─────────────────────────────────────────────────────

def test_scan_standby_when_no_live_games():
    far_market = _market("Will the Lakers beat the Celtics?", hours_to_end=10.0)
    with (
        patch("modules.oracle_b4.clob_utils.fetch_sampling_markets", return_value=[far_market]),
        patch("modules.oracle_b4.clob_utils.token_by_outcome",
              side_effect=lambda m, o: {"token_id": "x", "price": "0.5"}),
    ):
        signals = scan()
    assert signals == []
    assert b4.is_active() is False


def test_scan_active_when_live_games_present():
    live_ml     = _market("Will the Lakers beat the Celtics?",             price=0.50,
                           condition_id="0xLM1", hours_to_end=2.0)
    live_spread = _market("Will the Lakers beat the Celtics by 5+ points?", price=0.70,
                           condition_id="0xLS1", hours_to_end=2.0)
    with (
        patch("modules.oracle_b4.clob_utils.fetch_sampling_markets",
              return_value=[live_ml, live_spread]),
        patch("modules.oracle_b4.clob_utils.token_by_outcome",
              side_effect=lambda m, o: {"token_id": f"{o}-tok", "price": "0.5"}),
        patch("modules.oracle_b4.clob_utils.get_orderbook_liquidity", return_value=600.0),
    ):
        scan()
    assert b4.is_active() is True
