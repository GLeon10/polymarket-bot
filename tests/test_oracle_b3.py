"""Testes para oracle_b3 — Politics / Elections Oracle."""

from unittest.mock import patch

import pytest

from modules.oracle_b3 import (
    PoliticsSignal,
    _detect_candidate_party,
    _detect_dates,
    _detect_sum,
    _filter_politics,
    _vote_criterion,
    _check_resolution_rules,
)


# ── Factories de mercado ──────────────────────────────────────────────────────

def _market(question, price=0.30, condition_id=None, tags=None, fee=0.0, end_days=30):
    from datetime import datetime, timezone, timedelta
    end_dt = datetime.now(timezone.utc) + timedelta(days=end_days)
    return {
        "condition_id": condition_id or f"0x{abs(hash(question)):016x}",
        "question":     question,
        "taker_base_fee": fee,
        "tags":         tags or ["Politics", "Elections"],
        "end_date_iso": end_dt.isoformat(),
        "market_slug":  "test-slug",
        "tokens": [
            {"outcome": "Yes", "token_id": f"yes-{abs(hash(question)):08x}", "price": str(price)},
            {"outcome": "No",  "token_id": f"no-{abs(hash(question)):08x}",  "price": str(1 - price)},
        ],
    }


def _run_detect(fn, markets, rules="official results"):
    with (
        patch("modules.oracle_b3.clob_utils.get_orderbook_liquidity", return_value=500.0),
        patch("modules.oracle_b3._fetch_resolution_rules", return_value=f"resolves via {rules}"),
        patch("modules.oracle_b3._extract_resolution_source", return_value=rules),
    ):
        return fn(markets)


# ── _filter_politics ──────────────────────────────────────────────────────────

def test_filter_keeps_politics_tag():
    m = _market("Will Biden win the 2024 election?", tags=["Politics"])
    with patch("modules.oracle_b3.clob_utils.token_by_outcome", side_effect=lambda m, o: {"token_id": "x", "price": "0.4"}):
        result = _filter_politics([m])
    assert len(result) == 1


def test_filter_removes_wrong_tag():
    m = _market("Will Biden win the 2024 election?", tags=["Sports"])
    with patch("modules.oracle_b3.clob_utils.token_by_outcome", side_effect=lambda m, o: {"token_id": "x"}):
        result = _filter_politics([m])
    assert result == []


def test_filter_removes_high_fee():
    m = _market("Will Biden win the election?", fee=0.02)
    with patch("modules.oracle_b3.clob_utils.token_by_outcome", side_effect=lambda m, o: {"token_id": "x"}):
        result = _filter_politics([m])
    assert result == []


def test_filter_removes_expired():
    m = _market("Will Biden win the election?", end_days=100)
    with patch("modules.oracle_b3.clob_utils.token_by_outcome", side_effect=lambda m, o: {"token_id": "x"}):
        result = _filter_politics([m])
    assert result == []


# ── _vote_criterion ───────────────────────────────────────────────────────────

def test_vote_criterion_popular():
    assert _vote_criterion("resolves based on popular vote results") == "popular_vote"


def test_vote_criterion_electoral():
    assert _vote_criterion("winner determined by electoral college") == "electoral_college"


def test_vote_criterion_unknown():
    assert _vote_criterion("resolves via official AP results") == "unknown"


# ── _detect_sum (Tipo 4a) ─────────────────────────────────────────────────────

def test_detect_sum_undersum():
    markets = [
        _market("Will Alice win the 2026 senate race?", price=0.30, condition_id="0xA1"),
        _market("Will Bob win the 2026 senate race?",   price=0.25, condition_id="0xA2"),
        _market("Will Carol win the 2026 senate race?", price=0.20, condition_id="0xA3"),
    ]
    signals = _run_detect(_detect_sum, markets)
    assert len(signals) == 1
    assert "UNDERSUM" in signals[0].description
    assert signals[0].signal_type == 4


def test_detect_sum_oversum():
    markets = [
        _market("Will Alice win the 2026 senate race?", price=0.55, condition_id="0xB1"),
        _market("Will Bob win the 2026 senate race?",   price=0.50, condition_id="0xB2"),
    ]
    signals = _run_detect(_detect_sum, markets)
    assert len(signals) == 1
    assert "OVERSUM" in signals[0].description


def test_detect_sum_no_signal_when_spread_too_low():
    markets = [
        _market("Will Alice win the 2026 senate race?", price=0.490, condition_id="0xC1"),
        _market("Will Bob win the 2026 senate race?",   price=0.495, condition_id="0xC2"),
    ]
    signals = _run_detect(_detect_sum, markets)
    assert signals == []


def test_detect_sum_rejects_divergent_vote_criterion():
    markets = [
        _market("Will Alice win the 2026 presidential race?", price=0.30, condition_id="0xD1"),
        _market("Will Bob win the 2026 presidential race?",   price=0.20, condition_id="0xD2"),
        _market("Will Carol win the 2026 presidential race?", price=0.10, condition_id="0xD3"),
    ]
    with (
        patch("modules.oracle_b3.clob_utils.get_orderbook_liquidity", return_value=500.0),
        patch("modules.oracle_b3._fetch_resolution_rules", side_effect=[
            "resolves via popular vote",
            "resolves via electoral college",
            "resolves via popular vote",
        ]),
    ):
        signals = _detect_sum(markets)
    assert all(not s.resolution_ok for s in signals)


# ── _detect_dates (Tipo 4b) ───────────────────────────────────────────────────

def test_detect_dates_violation():
    markets = [
        _market("Will Trump be elected by January 2026?",  price=0.60, condition_id="0xE1"),
        _market("Will Trump be elected by December 2026?", price=0.40, condition_id="0xE2"),
    ]
    signals = _run_detect(_detect_dates, markets)
    assert len(signals) == 1
    assert "DATA" in signals[0].description


def test_detect_dates_no_violation():
    markets = [
        _market("Will Trump be elected by January 2026?",  price=0.30, condition_id="0xF1"),
        _market("Will Trump be elected by December 2026?", price=0.50, condition_id="0xF2"),
    ]
    signals = _run_detect(_detect_dates, markets)
    assert signals == []


# ── _detect_candidate_party (Tipo 4c) ─────────────────────────────────────────

def test_detect_candidate_party_violation():
    markets = [
        _market("Will John Smith win the 2026 senate election?",    price=0.55, condition_id="0xG1"),
        _market("Will the Republican Party win the 2026 senate election?", price=0.40, condition_id="0xG2",
                tags=["Politics"]),
    ]
    signals = _run_detect(_detect_candidate_party, markets)
    assert len(signals) == 1
    assert "CAND>PARTIDO" in signals[0].description


def test_detect_candidate_party_no_signal_when_ok():
    markets = [
        _market("Will John Smith win the 2026 senate election?",    price=0.35, condition_id="0xH1"),
        _market("Will the Republican Party win the 2026 senate election?", price=0.50, condition_id="0xH2",
                tags=["Politics"]),
    ]
    signals = _run_detect(_detect_candidate_party, markets)
    assert signals == []
