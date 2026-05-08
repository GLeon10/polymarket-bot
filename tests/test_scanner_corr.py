"""Testes para scanner_corr — filtros de qualidade pós-detecção."""

from unittest.mock import patch, MagicMock

import pytest

from modules.scanner_corr import (
    CorrSignal,
    _filter_fee,
    _filter_liquidity,
    _filter_exhaustiveness,
    _filter_resolution,
    _validate,
)


# ── Factories ─────────────────────────────────────────────────────────────────

def _market(cid="0xABC", question="Will A win X?", price=0.40, fee=0.0, tags=None):
    return {
        "condition_id":   cid,
        "question":       question,
        "taker_base_fee": fee,
        "tags":           tags or [],
        "tokens": [
            {"outcome": "Yes", "token_id": f"yes-{cid}", "price": str(price)},
            {"outcome": "No",  "token_id": f"no-{cid}",  "price": str(1 - price)},
        ],
    }


def _sig(pattern="undersum", prices=None, spread=0.10, market_ids=None, questions=None):
    prices     = prices     or [0.45, 0.45]
    market_ids = market_ids or ["0xA1", "0xA2"]
    questions  = questions  or ["Will A win X?", "Will B win X?"]
    return CorrSignal(
        pattern     = pattern,
        description = "test signal",
        questions   = questions,
        prices      = prices,
        spread      = spread,
        pnl_est     = spread * 50,
        market_ids  = market_ids,
    )


# ── F1 — Fee ──────────────────────────────────────────────────────────────────

def test_f1_passes_zero_fee():
    sig     = _sig(pattern="undersum", prices=[0.45, 0.45], spread=0.10)
    markets = [_market("0xA1", fee=0.0), _market("0xA2", fee=0.0)]
    ok, reason, fee_val = _filter_fee(sig, markets)
    assert ok is True
    assert fee_val == 0.0


def test_f1_passes_low_fee():
    sig     = _sig(pattern="undersum", prices=[0.45, 0.45], spread=0.10)
    markets = [_market("0xA1", fee=0.01), _market("0xA2", fee=0.01)]
    ok, _, fee_val = _filter_fee(sig, markets)
    assert ok is True
    assert fee_val < 0.05  # fee < 50% of 10% spread


def test_f1_fails_high_fee():
    # weighted_fee=0.02, limit=0.03*0.5=0.015 → 0.02 > 0.015 → fail
    sig     = _sig(pattern="undersum", prices=[0.45, 0.45], spread=0.03)
    markets = [_market("0xA1", fee=0.02), _market("0xA2", fee=0.02)]
    ok, reason, _ = _filter_fee(sig, markets)
    assert ok is False
    assert "fee=" in reason


def test_f1_geo_tags_force_zero_fee():
    sig     = _sig(pattern="undersum", prices=[0.45, 0.45], spread=0.10)
    markets = [
        _market("0xA1", fee=0.02, tags=["Geopolitics"]),
        _market("0xA2", fee=0.02, tags=["World"]),
    ]
    ok, _, fee_val = _filter_fee(sig, markets)
    assert ok is True
    assert fee_val == 0.0


def test_f1_oversum_uses_no_side_prices():
    # OVERSUM: side_prices = [1-p1, 1-p2] = [0.60, 0.60]
    sig     = _sig(pattern="oversum", prices=[0.40, 0.70], spread=0.10)
    markets = [_market("0xA1", fee=0.0), _market("0xA2", fee=0.0)]
    ok, _, fee_val = _filter_fee(sig, markets)
    assert ok is True
    assert fee_val == 0.0


# ── F2 — Liquidez ─────────────────────────────────────────────────────────────

def test_f2_passes_sufficient_liquidity():
    sig     = _sig(pattern="undersum")
    markets = [_market("0xA1"), _market("0xA2")]
    with patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity", return_value=200.0):
        ok, _ = _filter_liquidity(sig, markets)
    assert ok is True


def test_f2_fails_insufficient_liquidity():
    sig     = _sig(pattern="undersum")
    markets = [_market("0xA1"), _market("0xA2")]
    with patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity", return_value=50.0):
        ok, reason = _filter_liquidity(sig, markets)
    assert ok is False
    assert "liq=$50" in reason


def test_f2_oversum_checks_no_side():
    sig     = _sig(pattern="oversum")
    markets = [_market("0xA1"), _market("0xA2")]
    calls   = []

    def mock_liq(token_id):
        calls.append(token_id)
        return 500.0

    with patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity", side_effect=mock_liq):
        ok, _ = _filter_liquidity(sig, markets)

    assert ok is True
    # Should check "no-" tokens for oversum
    assert all("no-" in tid for tid in calls)


def test_f2_subset_checks_yes_then_no():
    sig     = _sig(pattern="subset", market_ids=["0xAB", "0xA"])
    markets = [_market("0xAB"), _market("0xA")]
    calls   = []

    def mock_liq(token_id):
        calls.append(token_id)
        return 500.0

    with patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity", side_effect=mock_liq):
        ok, _ = _filter_liquidity(sig, markets)

    assert ok is True
    assert "yes-0xAB" in calls
    assert "no-0xA" in calls


# ── F3 — Exaustividade ────────────────────────────────────────────────────────

def test_f3_undersum_passes_when_sum_above_50pct():
    sig = _sig(pattern="undersum", prices=[0.45, 0.45], spread=0.10)
    ok, _ = _filter_exhaustiveness(sig)
    assert ok is True


def test_f3_undersum_fails_when_sum_below_50pct():
    sig = _sig(pattern="undersum", prices=[0.12, 0.15], spread=0.73)
    ok, reason = _filter_exhaustiveness(sig)
    assert ok is False
    assert "50%" in reason


def test_f3_oversum_passes_with_few_candidates():
    sig = _sig(pattern="oversum", market_ids=[f"0x{i}" for i in range(5)],
               questions=["Will A win X?"] * 5, prices=[0.25] * 5, spread=0.25)
    ok, _ = _filter_exhaustiveness(sig)
    assert ok is True


def test_f3_oversum_fails_with_too_many_candidates():
    sig = _sig(pattern="oversum", market_ids=[f"0x{i}" for i in range(11)],
               questions=["Will A win X?"] * 11, prices=[0.10] * 11, spread=0.10)
    ok, reason = _filter_exhaustiveness(sig)
    assert ok is False
    assert "11 candidatos" in reason


def test_f3_oversum_fails_on_cumulative_keyword():
    sig = _sig(pattern="oversum",
               questions=["Will at least 500 seats change?", "Will it happen?"])
    ok, reason = _filter_exhaustiveness(sig)
    assert ok is False
    assert "cumulativo" in reason


def test_f3_subset_always_passes():
    sig = _sig(pattern="subset", prices=[0.30, 0.45], spread=0.15)
    ok, _ = _filter_exhaustiveness(sig)
    assert ok is True


# ── F4 — Resolução ────────────────────────────────────────────────────────────

def test_f4_passes_same_source():
    sig = _sig(market_ids=["0xA1", "0xA2"])
    with patch("modules.scanner_corr._fetch_rules", return_value="resolves via reuters results"):
        ok, reason = _filter_resolution(sig)
    assert ok is True
    assert "reuters" in reason


def test_f4_fails_divergent_sources():
    sig = _sig(market_ids=["0xA1", "0xA2"])
    with patch("modules.scanner_corr._fetch_rules", side_effect=[
        "resolves via reuters results",
        "resolves via associated press data",
    ]):
        ok, reason = _filter_resolution(sig)
    assert ok is False
    assert "divergentes" in reason


def test_f4_fails_when_rules_unavailable():
    sig = _sig(market_ids=["0xA1"])
    with patch("modules.scanner_corr._fetch_rules", return_value=""):
        ok, reason = _filter_resolution(sig)
    assert ok is False
    assert "não disponíveis" in reason


def test_f4_fails_when_source_unidentified():
    # text with no resolv* words and no known keywords → _extract_source returns ""
    sig = _sig(market_ids=["0xA1"])
    with patch("modules.scanner_corr._fetch_rules", return_value="The question closes when the event ends."):
        ok, reason = _filter_resolution(sig)
    assert ok is False
    assert "não identificada" in reason


# ── _validate pipeline ────────────────────────────────────────────────────────

def _make_market_by_id(market_ids, fee=0.0, liq=200.0):
    return {
        mid: _market(mid, fee=fee)
        for mid in market_ids
    }


def test_validate_all_pass():
    sig        = _sig(pattern="undersum", prices=[0.45, 0.45], spread=0.10,
                      market_ids=["0xV1", "0xV2"])
    market_map = _make_market_by_id(["0xV1", "0xV2"], fee=0.0)

    with (
        patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity", return_value=200.0),
        patch("modules.scanner_corr._fetch_rules", return_value="resolves via official results"),
    ):
        result = _validate([sig], market_map)

    assert len(result) == 1
    assert result[0].spread < 0.10 or result[0].spread == 0.10  # líquido ≤ bruto


def test_validate_discards_on_f1():
    sig        = _sig(pattern="undersum", prices=[0.45, 0.45], spread=0.03,
                      market_ids=["0xX1", "0xX2"])
    market_map = _make_market_by_id(["0xX1", "0xX2"], fee=0.02)  # fee > 50% spread

    with patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity", return_value=200.0):
        result = _validate([sig], market_map)

    assert result == []


def test_validate_discards_on_f2():
    sig        = _sig(pattern="undersum", prices=[0.45, 0.45], spread=0.10,
                      market_ids=["0xY1", "0xY2"])
    market_map = _make_market_by_id(["0xY1", "0xY2"], fee=0.0)

    with (
        patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity", return_value=50.0),
        patch("modules.scanner_corr._fetch_rules", return_value="resolves via official results"),
    ):
        result = _validate([sig], market_map)

    assert result == []


def test_validate_discards_on_f3_low_sum():
    sig        = _sig(pattern="undersum", prices=[0.10, 0.15], spread=0.75,
                      market_ids=["0xZ1", "0xZ2"])
    market_map = _make_market_by_id(["0xZ1", "0xZ2"], fee=0.0)

    with (
        patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity", return_value=200.0),
        patch("modules.scanner_corr._fetch_rules", return_value="resolves via official results"),
    ):
        result = _validate([sig], market_map)

    assert result == []


def test_validate_net_spread_updated():
    sig        = _sig(pattern="undersum", prices=[0.45, 0.45], spread=0.10,
                      market_ids=["0xN1", "0xN2"])
    market_map = {
        "0xN1": _market("0xN1", fee=0.01),
        "0xN2": _market("0xN2", fee=0.01),
    }

    with (
        patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity", return_value=200.0),
        patch("modules.scanner_corr._fetch_rules", return_value="resolves via official results"),
    ):
        result = _validate([sig], market_map)

    assert len(result) == 1
    assert result[0].spread < 0.10       # net < bruto (fee deduzida)
    assert result[0].pnl_est == pytest.approx(result[0].spread * 50.0, abs=0.01)
