from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from modules import oracle_b1
from modules.oracle_b1 import (
    _city_from_question,
    _model_probability,
    _resolution_within_hours,
)


# ── _city_from_question ───────────────────────────────────────────────────────

def test_city_detected_new_york():
    assert _city_from_question("Will New York max temp exceed 30C?") == "New York"

def test_city_detected_london():
    assert _city_from_question("London high temperature above 25 degrees") == "London"

def test_city_detected_case_insensitive():
    assert _city_from_question("Will SEOUL temperature exceed 35?") == "Seoul"

def test_city_not_detected():
    assert _city_from_question("Will it rain in Paris?") is None

def test_city_detected_hong_kong():
    assert _city_from_question("Hong Kong high above 33C on May 8?") == "Hong Kong"


# ── _model_probability ────────────────────────────────────────────────────────

def _forecast(temp_max):
    return {"daily": {"temperature_2m_max": [temp_max]}}


def test_exceed_clearly_above():
    p = _model_probability(_forecast(35), "Will temp exceed 30?")
    assert p == 0.85

def test_exceed_slightly_above():
    p = _model_probability(_forecast(31), "Will temp exceed 30?")
    assert p == 0.60

def test_exceed_slightly_below():
    p = _model_probability(_forecast(29), "Will temp exceed 30?")
    assert p == 0.40

def test_exceed_clearly_below():
    p = _model_probability(_forecast(25), "Will temp exceed 30?")
    assert p == 0.15

def test_below_clearly_below():
    p = _model_probability(_forecast(5), "Will temp be below 10?")
    assert p == 0.85

def test_below_slightly_below():
    p = _model_probability(_forecast(9), "Will temp be below 10?")
    assert p == 0.60

def test_below_slightly_above():
    p = _model_probability(_forecast(11), "Will temp be below 10?")
    assert p == 0.40

def test_below_clearly_above():
    p = _model_probability(_forecast(20), "Will temp be below 10?")
    assert p == 0.15

def test_higher_than_keyword():
    p = _model_probability(_forecast(35), "Will temp be higher than 30?")
    assert p == 0.85

def test_lower_than_keyword():
    p = _model_probability(_forecast(5), "Will temp be lower than 10?")
    assert p == 0.85

def test_no_threshold_in_question():
    assert _model_probability(_forecast(30), "Will it be hot today?") is None

def test_missing_forecast_data():
    assert _model_probability({"daily": {"temperature_2m_max": [None]}}, "exceed 30") is None


# ── _resolution_within_hours ──────────────────────────────────────────────────

def _market_ending_in(hours):
    end = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    return {"end_date_iso": end}

def test_resolution_within_window():
    assert _resolution_within_hours(_market_ending_in(12), 24) is True

def test_resolution_outside_window():
    assert _resolution_within_hours(_market_ending_in(30), 24) is False

def test_resolution_already_expired():
    assert _resolution_within_hours(_market_ending_in(-1), 24) is False

def test_resolution_missing_date():
    assert _resolution_within_hours({"end_date_iso": None}, 24) is False


# ── scan() integrado com mocks ────────────────────────────────────────────────

def _make_market(question, hours_to_end, yes_price, tags=None):
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")
    end  = (datetime.now(timezone.utc) + timedelta(hours=hours_to_end)).isoformat()
    return {
        "condition_id": "cid-b1-test",
        "question": question,
        "market_slug": slug,
        "end_date_iso": end,
        "tags": tags or ["Weather", "Daily Temperature"],
        "tokens": [
            {"outcome": "Yes", "token_id": "yes_tok", "price": yes_price},
            {"outcome": "No",  "token_id": "no_tok",  "price": 1 - yes_price},
        ],
    }


def test_scan_generates_signal_on_divergence():
    # Mercado: London, exceed 20°C, preço = 0.10 → modelo diz 0.85 (diff=5°C acima)
    market   = _make_market("Will London temp exceed 20C tomorrow?", hours_to_end=10, yes_price=0.10)
    forecast = {"daily": {"temperature_2m_max": [25.0]}}  # 5°C acima → 0.85

    with patch("modules.oracle_b1.clob_utils.fetch_sampling_markets", return_value=[market]), \
         patch("modules.oracle_b1._fetch_forecast", return_value=forecast), \
         patch("modules.oracle_b1.clob_utils.get_orderbook_liquidity", return_value=200.0):
        signals = oracle_b1.scan()

    assert len(signals) == 1
    s = signals[0]
    assert s.city        == "London"
    assert s.model_prob  == 0.85
    assert s.market_price == 0.10
    assert abs(s.divergence - 0.75) < 1e-9
    assert s.side == "Yes"


def test_scan_no_signal_low_divergence():
    # modelo=0.60, market=0.55 → divergência 0.05 < 0.20
    market   = _make_market("Will London temp exceed 20C tomorrow?", hours_to_end=10, yes_price=0.55)
    forecast = {"daily": {"temperature_2m_max": [21.0]}}  # 1°C acima → 0.60

    with patch("modules.oracle_b1.clob_utils.fetch_sampling_markets", return_value=[market]), \
         patch("modules.oracle_b1._fetch_forecast", return_value=forecast), \
         patch("modules.oracle_b1.clob_utils.get_orderbook_liquidity", return_value=200.0):
        signals = oracle_b1.scan()

    assert len(signals) == 0


def test_scan_no_signal_price_above_max():
    # preço = 0.50 > B1_MAX_SHARE_PRICE (0.40)
    market   = _make_market("Will London temp exceed 20C tomorrow?", hours_to_end=10, yes_price=0.50)
    forecast = {"daily": {"temperature_2m_max": [26.0]}}  # 0.85

    with patch("modules.oracle_b1.clob_utils.fetch_sampling_markets", return_value=[market]), \
         patch("modules.oracle_b1._fetch_forecast", return_value=forecast), \
         patch("modules.oracle_b1.clob_utils.get_orderbook_liquidity", return_value=200.0):
        signals = oracle_b1.scan()

    assert len(signals) == 0


def test_scan_no_signal_low_liquidity():
    market   = _make_market("Will London temp exceed 20C tomorrow?", hours_to_end=10, yes_price=0.10)
    forecast = {"daily": {"temperature_2m_max": [25.0]}}

    with patch("modules.oracle_b1.clob_utils.fetch_sampling_markets", return_value=[market]), \
         patch("modules.oracle_b1._fetch_forecast", return_value=forecast), \
         patch("modules.oracle_b1.clob_utils.get_orderbook_liquidity", return_value=50.0):
        signals = oracle_b1.scan()

    assert len(signals) == 0


def test_execute_dry_run():
    market   = _make_market("Will London temp exceed 20C tomorrow?", hours_to_end=10, yes_price=0.10)
    forecast = {"daily": {"temperature_2m_max": [25.0]}}

    with patch("modules.oracle_b1.clob_utils.fetch_sampling_markets", return_value=[market]), \
         patch("modules.oracle_b1._fetch_forecast", return_value=forecast), \
         patch("modules.oracle_b1.clob_utils.get_orderbook_liquidity", return_value=200.0):
        signals = oracle_b1.scan()

    with patch("modules.oracle_b1.tracker.record_signal"):
        assert oracle_b1.execute(signals[0], client=None) is True
