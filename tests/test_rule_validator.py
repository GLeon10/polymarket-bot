"""Testes do módulo rule_validator."""

from unittest.mock import patch
from modules import rule_validator
from modules.rule_validator import _keyword_quality


# ── _keyword_quality ──────────────────────────────────────────────────────────

def test_keyword_high_all_three_signals():
    text = (
        "This market resolves via Reuters. "
        "If no data is available, we use the most recent available figure. "
        "Outcomes cover less than 50 or greater than 100."
    )
    assert _keyword_quality(text) == "HIGH"


def test_keyword_high_imf_with_fallback():
    text = (
        "Resolution source: IMF Portwatch. "
        "Fallback: if primary source unavailable, use most recent available data."
    )
    assert _keyword_quality(text) == "HIGH"


def test_keyword_medium_only_reliable_source():
    text = "This market will resolve according to Reuters wire reports."
    assert _keyword_quality(text) == "MEDIUM"


def test_keyword_medium_only_fallback():
    text = "If no data is available, the market uses the most recent figure."
    assert _keyword_quality(text) == "MEDIUM"


def test_keyword_medium_only_range_coverage():
    text = "Possible outcomes: less than 10 ships or greater than 50 ships."
    assert _keyword_quality(text) == "MEDIUM"


def test_keyword_low_no_signals():
    text = "The market resolves based on general consensus of traders."
    assert _keyword_quality(text) == "LOW"


def test_keyword_low_empty_text():
    assert _keyword_quality("") == "LOW"


# ── assess() — HIGH skips LLM ────────────────────────────────────────────────

def test_assess_high_skips_llm():
    rule_validator.clear_cache()
    text = (
        "Reuters is the resolution source. "
        "Fallback if no data available. "
        "Covers less than 10 or greater than 50."
    )
    with patch("modules.rule_validator._llm_validate") as mock_llm:
        quality, _ = rule_validator.assess(text, "cid-high-test")
        mock_llm.assert_not_called()
    assert quality == "HIGH"


def test_assess_high_no_api_key_skips_llm():
    rule_validator.clear_cache()
    text = (
        "Reuters source. If no data available, fallback. "
        "Less than 10 or greater than 50."
    )
    with patch("modules.rule_validator._llm_validate") as mock_llm, \
         patch("modules.rule_validator.config") as mock_cfg:
        mock_cfg.ANTHROPIC_API_KEY = ""
        quality, _ = rule_validator.assess(text, "cid-high-nokey")
        mock_llm.assert_not_called()
    assert quality == "HIGH"


# ── assess() — non-HIGH calls LLM ────────────────────────────────────────────

def test_assess_low_calls_llm():
    rule_validator.clear_cache()
    text = "The market resolves based on general conditions."
    with patch("modules.rule_validator._llm_validate",
               return_value={"quality": "MEDIUM", "reason": "LLM upgrade"}) as mock_llm, \
         patch("modules.rule_validator.config") as mock_cfg:
        mock_cfg.ANTHROPIC_API_KEY = "test-key"
        quality, reason = rule_validator.assess(text, "cid-low-test")
        mock_llm.assert_called_once()
    assert quality == "MEDIUM"
    assert "LLM upgrade" in reason


def test_assess_medium_calls_llm():
    rule_validator.clear_cache()
    text = "This market resolves via Reuters."
    with patch("modules.rule_validator._llm_validate",
               return_value={"quality": "LOW", "reason": "missing fallback"}) as mock_llm, \
         patch("modules.rule_validator.config") as mock_cfg:
        mock_cfg.ANTHROPIC_API_KEY = "test-key"
        quality, reason = rule_validator.assess(text, "cid-med-test")
        mock_llm.assert_called_once()
    assert quality == "LOW"


# ── assess() — no API key ─────────────────────────────────────────────────────

def test_assess_no_api_key_uses_keyword_quality():
    rule_validator.clear_cache()
    text = "This market resolves via Reuters."
    with patch("modules.rule_validator._llm_validate") as mock_llm, \
         patch("modules.rule_validator.config") as mock_cfg:
        mock_cfg.ANTHROPIC_API_KEY = ""
        quality, _ = rule_validator.assess(text, "cid-nokey-medium")
        mock_llm.assert_not_called()
    assert quality == "MEDIUM"


def test_assess_no_api_key_low_stays_low():
    rule_validator.clear_cache()
    text = "General conditions determine resolution."
    with patch("modules.rule_validator._llm_validate") as mock_llm, \
         patch("modules.rule_validator.config") as mock_cfg:
        mock_cfg.ANTHROPIC_API_KEY = ""
        quality, _ = rule_validator.assess(text, "cid-nokey-low")
        mock_llm.assert_not_called()
    assert quality == "LOW"


# ── Cache behavior ────────────────────────────────────────────────────────────

def test_assess_caches_result_per_condition_id():
    rule_validator.clear_cache()
    text = "General market conditions."
    with patch("modules.rule_validator._llm_validate",
               return_value={"quality": "MEDIUM", "reason": "ok"}) as mock_llm, \
         patch("modules.rule_validator.config") as mock_cfg:
        mock_cfg.ANTHROPIC_API_KEY = "test-key"
        rule_validator.assess(text, "cid-cache-1")
        rule_validator.assess(text, "cid-cache-1")  # second call same id
        rule_validator.assess(text, "cid-cache-2")  # different id
    assert mock_llm.call_count == 2  # once per unique condition_id


def test_clear_cache_allows_re_evaluation():
    rule_validator.clear_cache()
    text = "General market conditions."
    with patch("modules.rule_validator._llm_validate",
               return_value={"quality": "MEDIUM", "reason": "ok"}) as mock_llm, \
         patch("modules.rule_validator.config") as mock_cfg:
        mock_cfg.ANTHROPIC_API_KEY = "test-key"
        rule_validator.assess(text, "cid-clear")
        rule_validator.clear_cache()
        rule_validator.assess(text, "cid-clear")  # should call LLM again
    assert mock_llm.call_count == 2


# ── LLM response parsing ──────────────────────────────────────────────────────

def test_llm_validate_parses_clean_json():
    rule_validator.clear_cache()
    fake_response = {
        "content": [{"text": '{"exhaustive": true, "has_fallback": true, '
                              '"edge_cases_handled": false, "source_reliable": true, '
                              '"quality": "HIGH", "reason": "good rules"}'}]
    }
    with patch("modules.rule_validator.requests.post") as mock_post:
        mock_post.return_value.raise_for_status = lambda: None
        mock_post.return_value.json = lambda: fake_response
        result = rule_validator._llm_validate("some rules", "cid-llm")
    assert result["quality"] == "HIGH"
    assert result["reason"] == "good rules"


def test_llm_validate_handles_json_wrapped_in_text():
    rule_validator.clear_cache()
    json_block = '{"exhaustive": false, "has_fallback": false, "edge_cases_handled": false, "source_reliable": false, "quality": "LOW", "reason": "vague"}'
    fake_response = {
        "content": [{"text": f"Here is my evaluation:\n{json_block}\nEnd."}]
    }
    with patch("modules.rule_validator.requests.post") as mock_post:
        mock_post.return_value.raise_for_status = lambda: None
        mock_post.return_value.json = lambda: fake_response
        result = rule_validator._llm_validate("some rules", "cid-wrapped")
    assert result["quality"] == "LOW"


def test_llm_validate_returns_low_on_request_error():
    rule_validator.clear_cache()
    import requests as req
    with patch("modules.rule_validator.requests.post", side_effect=req.RequestException("timeout")):
        result = rule_validator._llm_validate("some rules", "cid-error")
    assert result["quality"] == "LOW"
    assert "falha" in result["reason"].lower()
